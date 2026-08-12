"""Token resolution — exact, then measured, then declared (research §6).

Every size in this project passes through here, and every size it returns carries the tier it
came from as its ``basis`` (``exact`` / ``measured`` / ``estimated``, defined once in
``config/components.py``) plus a human-readable ``method`` a skeptic can check without
rerunning the tool (FR-018, Principle X).

**The rule this module exists to enforce: `chars // 4` is never applied to an image.** Base64
image payloads were 95% of all tool-result tokens (8.19M of 8.62M) on the measured corpus under
`chars // 4`, and the true cost of a 2560x1430 image is ~4.9k tokens, not ~500k — an error
large enough to invert the tool's central conclusion (PITFALLS, pass-2 §5.2). An image is sized
from its decoded header's pixel dimensions or it is **withheld**; there is no third path.

The ladder, per quantity:

1. **exact** — a count recorded in the transcript. Always preferred.
2. **measured** — computed by a documented rule from data we hold: image tokens from pixel
   dimensions via ``width * height / 750``, capped at the model's per-image maximum.
3. **declared** — neither is available, so the figure is either a character-based estimate
   marked ``estimated`` with low confidence and a method string that says so plainly, or it is
   withheld with a stated reason (FR-019: decline rather than estimate, and show the gap).
"""

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Any

from ccaudit.config.components import BASIS_VALUES, CONFIDENCE_VALUES
from ccaudit.ingest.records import ToolResultRecord

# The published area rule for image tokens: tokens ~= width * height / 750
# (docs/research/prior-art-pass-2.md §5.2, Anthropic vision documentation). It is applied to the
# *decoded header's* pixel dimensions, never to the length of the base64 payload.
PIXELS_PER_IMAGE_TOKEN = 750

# Per-image maximum, by model tier. The API resizes an oversized image before tokenizing, so the
# area rule saturates rather than growing without bound; the cap IS that resize expressed in
# tokens.
#
# - High-resolution tier (Opus 4.7 and later, Sonnet 5): long edge 2576px, ~4784 tokens.
# - Earlier tier: long edge 1568px, ~1600 tokens.
#
# The tiers are NOT ordered by model recency in any derivable way — high-resolution support
# arrived at Opus 4.7 and did not exist on the newer-numbered Opus 4.6. So this is an explicit
# per-model table, for the same reason the cacheability minimum is (docs/cost-model.md §2): a
# value that cannot be derived from a version ordering must be written down per model.
HIGH_RESOLUTION_IMAGE_TOKEN_CAP = 4784
LEGACY_IMAGE_TOKEN_CAP = 1600

IMAGE_TOKEN_CAP_BY_MODEL: dict[str, int] = {
    "claude-opus-5": HIGH_RESOLUTION_IMAGE_TOKEN_CAP,
    "claude-fable-5": HIGH_RESOLUTION_IMAGE_TOKEN_CAP,
    "claude-mythos-5": HIGH_RESOLUTION_IMAGE_TOKEN_CAP,
    "claude-sonnet-5": HIGH_RESOLUTION_IMAGE_TOKEN_CAP,
    "claude-opus-4-8": HIGH_RESOLUTION_IMAGE_TOKEN_CAP,
    "claude-opus-4-7": HIGH_RESOLUTION_IMAGE_TOKEN_CAP,
    "claude-opus-4-6": LEGACY_IMAGE_TOKEN_CAP,
    "claude-opus-4-5": LEGACY_IMAGE_TOKEN_CAP,
    "claude-opus-4-1": LEGACY_IMAGE_TOKEN_CAP,
    "claude-opus-4-0": LEGACY_IMAGE_TOKEN_CAP,
    "claude-sonnet-4-6": LEGACY_IMAGE_TOKEN_CAP,
    "claude-sonnet-4-5": LEGACY_IMAGE_TOKEN_CAP,
    "claude-sonnet-4-0": LEGACY_IMAGE_TOKEN_CAP,
    "claude-haiku-4-5": LEGACY_IMAGE_TOKEN_CAP,
}

# Characters per token for the declared tier. Four is the conventional English-prose ratio and
# it is wrong by a wide, non-constant margin on code, JSON, and non-English text — which is
# exactly why every figure derived from it is marked `estimated` with low confidence and says
# "character-based estimate" in its method string.
CHARACTERS_PER_TOKEN_ESTIMATE = 4

# The span the true ratio actually falls in, which is what a range around a character-based
# figure must be built from. Dense code and JSON tokenize nearer 3 characters per token; English
# prose and repetitive text run to 5 and beyond. So a `chars // 4` figure is wrong by roughly
# -20% to +33%, and *not* by 100%: an item that demonstrably occupied a charged cached block did
# not plausibly cost nothing. A band whose low end is zero states no constraint at all, and a
# range that says nothing is not a cautious figure — it is an absent one.
CHARACTERS_PER_TOKEN_RANGE: tuple[int, int] = (3, 5)

# How far into an image we are willing to decode looking for its dimensions. PNG and WebP carry
# them in the first 32 bytes; JPEG puts them in a SOF segment that sits after any embedded EXIF
# thumbnail or colour profile, so it needs a scan. 64 KiB covers every real screenshot while
# keeping this bounded — we decode a prefix of the base64, never the whole payload.
IMAGE_HEADER_SCAN_BYTES = 64 * 1024

_BASIS_RANK: dict[str, int] = {"exact": 0, "measured": 1, "estimated": 2}
_CONFIDENCE_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# Keys under which a tool result may carry its own token count. Present only when an optional
# richer source (telemetry) enriched the record; the transcript alone does not carry one.
_RECORDED_TOKEN_KEYS: tuple[str, ...] = (
    "result_tokens",
    "resultTokens",
    "totalTokens",
    "tokenCount",
)

# JPEG start-of-frame markers. 0xC4 (Huffman table), 0xC8 (JPEG extension) and 0xCC (arithmetic
# coding table) share the range but are not frame headers.
_JPEG_SOF_MARKERS: frozenset[int] = frozenset(
    m for m in range(0xC0, 0xD0) if m not in (0xC4, 0xC8, 0xCC)
)


class UnknownImageModelError(ValueError):
    """The per-image token cap for this model is not in the table, so an image cannot be sized.

    Raised rather than defaulted: guessing the cap misprices every image on that model in the
    direction that hides the error, and the tiers are not derivable from a version ordering.
    """


@dataclass(frozen=True)
class TokenQuantity:
    """A token count, the tier it came from, and how it was derived.

    ``tokens`` is ``None`` for a *withheld* figure — the data could not support a number and we
    declined to invent one (FR-019). A withheld quantity still carries a basis and a method:
    the method names the gap, which is the thing the report has to show.
    """

    tokens: int | None
    basis: str
    confidence: str
    method: str

    def __post_init__(self) -> None:
        if self.basis not in BASIS_VALUES:
            raise ValueError(f"unknown basis {self.basis!r}; known: {list(BASIS_VALUES)}")
        if self.confidence not in CONFIDENCE_VALUES:
            raise ValueError(
                f"unknown confidence {self.confidence!r}; known: {list(CONFIDENCE_VALUES)}"
            )
        if self.tokens is not None and self.tokens < 0:
            raise ValueError(f"token count must be non-negative, got {self.tokens}")
        if not self.method:
            raise ValueError(
                "every quantity must carry a method; an unexplained figure is a defect"
            )

    @property
    def is_withheld(self) -> bool:
        """True when no figure is being reported, and ``method`` says why."""
        return self.tokens is None


def exact(tokens: int, method: str) -> TokenQuantity:
    """A count read straight from the records."""
    return TokenQuantity(tokens=tokens, basis="exact", confidence="high", method=method)


def measured(tokens: int, method: str, confidence: str = "medium") -> TokenQuantity:
    """A count computed by a documented rule from data we hold."""
    return TokenQuantity(tokens=tokens, basis="measured", confidence=confidence, method=method)


def estimated(tokens: int, method: str) -> TokenQuantity:
    """A declared-tier figure. Always low confidence — an estimate is never a measurement."""
    return TokenQuantity(tokens=tokens, basis="estimated", confidence="low", method=method)


def withheld(reason: str) -> TokenQuantity:
    """No figure. The declared tier's other branch: say what is missing (FR-019)."""
    return TokenQuantity(
        tokens=None, basis="estimated", confidence="low", method=f"withheld: {reason}"
    )


def image_token_cap(model: str | None) -> int:
    """The model's per-image token maximum. Raises when the model is unknown or unrecorded."""
    if not model:
        raise UnknownImageModelError(
            "no model recorded on this turn, so the per-image token cap cannot be resolved"
        )
    normalized = normalize_model_id(model)
    try:
        return IMAGE_TOKEN_CAP_BY_MODEL[normalized]
    except KeyError:
        raise UnknownImageModelError(
            f"no per-image token cap for model {model!r} (normalized {normalized!r}); "
            f"add it to IMAGE_TOKEN_CAP_BY_MODEL in ingest/tokens.py — the high-resolution tier "
            f"is not derivable from a version ordering"
        ) from None


def normalize_model_id(model: str) -> str:
    """Reduce a recorded model string to its table key.

    Records carry context-window suffixes (``claude-opus-4-8[1m]``) and dated snapshots
    (``claude-opus-4-5-20251101``); neither changes the image tier.
    """
    normalized = model.strip().lower()
    bracket = normalized.find("[")
    if bracket != -1:
        normalized = normalized[:bracket]
    parts = normalized.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) == 8 and parts[1].isdigit():
        normalized = parts[0]
    return normalized


def image_tokens(width: int, height: int, model: str | None) -> TokenQuantity:
    """Size one image from its pixel dimensions: ``width * height / 750``, capped per model.

    Measured, not estimated: the rule is published and the inputs are read from the image's own
    header. The cap reflects the API resizing an oversized image before tokenizing, so a very
    large screenshot saturates rather than growing without bound.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"image dimensions must be positive, got {width}x{height}")
    cap = image_token_cap(model)
    # Ceiling: a partial token is still charged as a token, and rounding down would understate.
    raw = -(-(width * height) // PIXELS_PER_IMAGE_TOKEN)
    if raw > cap:
        return measured(
            cap,
            f"image {width}x{height}px: {width}*{height}/{PIXELS_PER_IMAGE_TOKEN} = {raw} tokens, "
            f"capped at the {model} per-image maximum of {cap} (the API resizes before tokenizing)",
        )
    return measured(
        raw,
        f"image {width}x{height}px: {width}*{height}/{PIXELS_PER_IMAGE_TOKEN} = {raw} tokens "
        f"(under the {model} per-image maximum of {cap})",
    )


def estimate_from_characters(characters: int, what: str) -> TokenQuantity:
    """The declared tier: a character-based estimate, named as such at every surface."""
    if characters < 0:
        raise ValueError(f"character count must be non-negative, got {characters}")
    tokens = characters // CHARACTERS_PER_TOKEN_ESTIMATE
    return estimated(
        tokens,
        f"character-based estimate (NOT a measurement): {characters} chars of {what} / "
        f"{CHARACTERS_PER_TOKEN_ESTIMATE}",
    )


def read_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Pixel dimensions from a PNG / JPEG / WebP header, or ``None`` if they cannot be read.

    Format is sniffed from the signature bytes rather than trusted from the declared media type,
    because a wrong number here is worse than no number. No image library: four integers out of
    a header does not justify a runtime dependency (Principle II).
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png_dimensions(data)
    if data.startswith(b"\xff\xd8"):
        return _jpeg_dimensions(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp_dimensions(data)
    return None


def resolve_tool_result_tokens(record: ToolResultRecord, model: str | None = None) -> TokenQuantity:
    """Size one tool result. ``model`` is the model of the turn that received it.

    Two places have to be searched, not one. ``toolUseResult`` is Claude Code's own summary of
    what the tool returned, and for a screenshot it is often just ``{"isImage": true}`` — the
    pixels live in the API message's ``tool_result`` content block instead. Sizing only the
    first would withhold every embedded image, silently dropping what the corpus says is ~95%
    of tool-result token volume.
    """
    quantity = resolve_payload_tokens(
        record.payload,
        model=model,
        tool_name=record.tool_name,
        text_length=record.text_length,
    )
    if not quantity.is_withheld or record.content is None:
        return quantity

    from_content = resolve_payload_tokens(
        record.content,
        model=model,
        tool_name=record.tool_name,
        text_length=record.text_length,
    )
    # Only an actual figure displaces the withheld one; a second withheld result keeps the
    # first one's reason, which is the more specific of the two.
    return quantity if from_content.is_withheld else from_content


def resolve_payload_tokens(
    payload: Any,
    *,
    model: str | None = None,
    tool_name: str | None = None,
    text_length: int = 0,
) -> TokenQuantity:
    """Walk the ladder over a ``toolUseResult`` payload of any shape.

    The payload shape varies by tool and is not part of the API message: ``Bash`` returns
    ``{stdout, stderr, interrupted, isImage, ...}``, ``Read`` returns ``{type, file: {...}}``,
    ``Edit`` returns ``{filePath, oldString, newString, ...}``, and 48 of 1,595 observed results
    were bare strings (pass-2 §5.4). Every one of those has to size without crashing.
    """
    recorded = _recorded_token_count(payload)
    if recorded is not None:
        return exact(recorded, f"count recorded in the tool result ({recorded} tokens)")

    images = _find_image_sources(payload)
    if images:
        return _image_result_tokens(images, payload, model)

    if _declares_image_without_payload(payload):
        # Bash marks screenshot-producing commands with `isImage`. If the pixels are not in the
        # record we cannot size it — and falling back to characters here is exactly the ~100x
        # error this module exists to prevent, so the figure is withheld instead.
        return withheld(
            "the result is marked as an image but carries no decodable image payload, and an "
            "image must never be sized from its character count"
        )

    if isinstance(payload, str):
        return estimate_from_characters(len(payload), "bare-string tool result")

    if isinstance(payload, dict):
        file_block = payload.get("file")
        if isinstance(file_block, dict):
            return _read_result_tokens(file_block, text_length)
        if "stdout" in payload or "stderr" in payload:
            return _shell_result_tokens(payload)
        return _generic_dict_tokens(payload, tool_name, text_length)

    if isinstance(payload, list):
        characters = _text_characters(payload)
        if characters:
            return estimate_from_characters(characters, f"{tool_name or 'tool'} result content")
        return _fallback_from_text_length(text_length, tool_name)

    return _fallback_from_text_length(text_length, tool_name)


@dataclass(frozen=True)
class _ImageSource:
    """One image found in a payload, and where it was found (for the method string)."""

    data: Any
    media_type: str | None
    where: str


def _image_result_tokens(
    images: list[_ImageSource], payload: Any, model: str | None
) -> TokenQuantity:
    parts: list[TokenQuantity] = []
    for index, image in enumerate(images):
        parts.append(_one_image_tokens(image, index, len(images), model))

    # Text alongside the images (a Read of a PDF page, a Bash screenshot with a caption) is a
    # separate quantity. `_text_characters` reads `text` fields only, so base64 payloads under
    # `data` can never leak into a character count.
    characters = _text_characters(payload)
    if characters:
        parts.append(estimate_from_characters(characters, "text accompanying the image"))
    return _combine(parts)


def _one_image_tokens(
    image: _ImageSource, index: int, total: int, model: str | None
) -> TokenQuantity:
    label = f"image {index + 1} of {total} ({image.where}"
    label += f", declared {image.media_type})" if image.media_type else ")"

    header = _decode_image_prefix(image.data, IMAGE_HEADER_SCAN_BYTES)
    if header is None:
        return withheld(f"{label}: the image payload could not be decoded, so it cannot be sized")
    dimensions = read_image_dimensions(header)
    if dimensions is None:
        return withheld(
            f"{label}: pixel dimensions are not readable from the first "
            f"{IMAGE_HEADER_SCAN_BYTES} bytes (recognised formats: PNG, JPEG, WebP), and an image "
            f"is never sized from its character count"
        )
    width, height = dimensions
    try:
        quantity = image_tokens(width, height, model)
    except UnknownImageModelError as error:
        # Narrow catch: an unknown model is a normal outcome for a corpus that spans releases,
        # and the honest response is a named gap rather than a guessed cap.
        return withheld(f"{label}: {error}")
    return TokenQuantity(
        tokens=quantity.tokens,
        basis=quantity.basis,
        confidence=quantity.confidence,
        method=f"{label}: {quantity.method}",
    )


def _read_result_tokens(file_block: dict[str, Any], text_length: int) -> TokenQuantity:
    """Size a ``Read`` result from the payload's own file object.

    ``file.content`` is what actually entered the conversation, and ``file.numLines`` /
    ``file.totalLines`` say whether that is the whole file or a slice — a better size signal than
    the rendered result text, which carries the tool's own decoration (pass-2 §5.4). The count is
    still character-based, so the basis stays ``estimated``; what the line counts buy is an
    honest method string that names the slice a reader would otherwise have to guess at.
    """
    content = file_block.get("content")
    num_lines = _as_optional_int(file_block.get("numLines"))
    total_lines = _as_optional_int(file_block.get("totalLines"))
    path = file_block.get("filePath")

    span = ""
    if num_lines is not None and total_lines is not None:
        whole = "whole file" if num_lines >= total_lines else "PARTIAL read"
        span = f"; {num_lines} of {total_lines} lines ({whole})"
    elif num_lines is not None:
        span = f"; {num_lines} lines read, total unknown"

    if isinstance(content, str):
        quantity = estimate_from_characters(len(content), f"file.content of {path or 'a Read'}")
        return estimated(quantity.tokens or 0, f"{quantity.method}{span}")

    if text_length:
        quantity = estimate_from_characters(text_length, "the rendered Read result")
        return estimated(
            quantity.tokens or 0,
            f"{quantity.method}; file.content absent from the payload{span}",
        )
    return withheld(f"Read result carries neither file.content nor rendered text{span}")


def _shell_result_tokens(payload: dict[str, Any]) -> TokenQuantity:
    stdout = payload.get("stdout")
    stderr = payload.get("stderr")
    characters = (len(stdout) if isinstance(stdout, str) else 0) + (
        len(stderr) if isinstance(stderr, str) else 0
    )
    quantity = estimate_from_characters(characters, "shell stdout + stderr")
    notes = []
    if payload.get("interrupted"):
        notes.append("interrupted")
    if payload.get("noOutputExpected"):
        notes.append("no output expected")
    suffix = f" ({', '.join(notes)})" if notes else ""
    return estimated(quantity.tokens or 0, f"{quantity.method}{suffix}")


def _generic_dict_tokens(
    payload: dict[str, Any], tool_name: str | None, text_length: int
) -> TokenQuantity:
    characters = _text_characters(payload)
    if characters:
        return estimate_from_characters(characters, f"{tool_name or 'tool'} result text fields")
    if text_length:
        return estimate_from_characters(text_length, f"the rendered {tool_name or 'tool'} result")
    # Structured payloads such as Edit's `structuredPatch` carry their content in fields we do
    # not enumerate; the serialised form is the honest upper bound on what entered the prompt.
    try:
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return withheld(f"{tool_name or 'tool'} result payload could not be serialised to size it")
    return estimate_from_characters(
        len(serialized), f"the serialised {tool_name or 'tool'} result payload"
    )


def _fallback_from_text_length(text_length: int, tool_name: str | None) -> TokenQuantity:
    if text_length:
        return estimate_from_characters(text_length, f"the rendered {tool_name or 'tool'} result")
    return estimated(0, f"no content observed in the {tool_name or 'tool'} result")


def _combine(parts: list[TokenQuantity]) -> TokenQuantity:
    """Fold several quantities into one, at the weakest basis and confidence of its parts.

    A withheld part withholds the whole: a total that silently omits a component it could not
    size is worse than no total (Principle X — missing beats wrong).
    """
    if not parts:
        raise ValueError("cannot combine zero quantities")
    if len(parts) == 1:
        return parts[0]
    for part in parts:
        if part.is_withheld:
            others = "; ".join(p.method for p in parts if p is not part)
            return withheld(f"one component could not be sized [{part.method}]; the rest: {others}")
    total = sum(part.tokens or 0 for part in parts)
    basis = max((p.basis for p in parts), key=lambda b: _BASIS_RANK[b])
    confidence = max((p.confidence for p in parts), key=lambda c: _CONFIDENCE_RANK[c])
    return TokenQuantity(
        tokens=total,
        basis=basis,
        confidence=confidence,
        method=" + ".join(part.method for part in parts) + f" = {total} tokens",
    )


def _recorded_token_count(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in _RECORDED_TOKEN_KEYS:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
    return None


def _find_image_sources(payload: Any) -> list[_ImageSource]:
    """Collect every image block in a payload, at any depth."""
    found: list[_ImageSource] = []
    _walk_for_images(payload, "toolUseResult", found)
    return found


def _walk_for_images(node: Any, path: str, found: list[_ImageSource]) -> None:
    if isinstance(node, dict):
        source = node.get("source")
        if node.get("type") == "image" and isinstance(source, dict):
            found.append(
                _ImageSource(
                    data=source.get("data"),
                    media_type=_as_optional_str(source.get("media_type")),
                    where=path,
                )
            )
            return
        for key, value in node.items():
            _walk_for_images(value, f"{path}.{key}", found)
        return
    if isinstance(node, list):
        for index, value in enumerate(node):
            _walk_for_images(value, f"{path}[{index}]", found)


def _declares_image_without_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload.get("isImage"))


def _text_characters(node: Any) -> int:
    """Characters of human-readable text in a payload.

    Deliberately reads ``text`` fields only. A walk that also counted ``data`` would be counting
    base64 image bytes as prose — the exact defect this module exists to prevent.
    """
    if isinstance(node, dict):
        total = 0
        for key, value in node.items():
            if key == "text" and isinstance(value, str):
                total += len(value)
            elif isinstance(value, (dict, list)):
                total += _text_characters(value)
        return total
    if isinstance(node, list):
        return sum(_text_characters(value) for value in node)
    return 0


def _decode_image_prefix(data: Any, limit: int) -> bytes | None:
    """Decode at most ``limit`` bytes of an image payload — never the whole thing."""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data[:limit])
    if not isinstance(data, str):
        return None

    text = data
    if text.startswith("data:"):
        comma = text.find(",")
        if comma == -1:
            return None
        text = text[comma + 1 :]

    # Four base64 characters per three bytes, plus slack for any line breaks in the prefix.
    wanted = ((limit + 2) // 3) * 4
    chunk = "".join(text[: wanted + 4096].split())
    chunk = chunk[: len(chunk) - len(chunk) % 4]
    if not chunk:
        return None
    try:
        return base64.b64decode(chunk, validate=True)
    except (binascii.Error, ValueError):
        return None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return (width, height) if width and height else None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    position = 2
    length = len(data)
    while position + 9 <= length:
        if data[position] != 0xFF:
            position += 1
            continue
        marker = data[position + 1]
        if marker == 0xFF:
            position += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            position += 2
            continue
        if marker == 0xD9:
            return None
        segment_length = int.from_bytes(data[position + 2 : position + 4], "big")
        if segment_length < 2:
            return None
        if marker in _JPEG_SOF_MARKERS:
            height = int.from_bytes(data[position + 5 : position + 7], "big")
            width = int.from_bytes(data[position + 7 : position + 9], "big")
            return (width, height) if width and height else None
        position += 2 + segment_length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    fourcc = data[12:16]
    if fourcc == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if fourcc == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return (width, height) if width and height else None
    if fourcc == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def _as_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
