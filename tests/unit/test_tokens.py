"""Contract on token resolution.

The invariant these tests fence is the one that would otherwise invert the product's central
conclusion: **an image is sized from its pixel dimensions or it is withheld — never from its
character count.** Under ``chars // 4``, base64 image payloads were 95% of all tool-result
tokens on the measured corpus. A test here failing means that defect is back.
"""

import base64
from collections.abc import Callable

import pytest

from claude_cost_tracker.config.components import BASIS_VALUES, CONFIDENCE_VALUES
from claude_cost_tracker.ingest.records import ToolResultRecord
from claude_cost_tracker.ingest.tokens import (
    HIGH_RESOLUTION_IMAGE_TOKEN_CAP,
    LEGACY_IMAGE_TOKEN_CAP,
    PIXELS_PER_IMAGE_TOKEN,
    TokenQuantity,
    UnknownImageModelError,
    estimate_from_characters,
    image_token_cap,
    image_tokens,
    normalize_model_id,
    read_image_dimensions,
    resolve_payload_tokens,
    resolve_tool_result_tokens,
)

HIGH_RES_MODEL = "claude-opus-4-8[1m]"
LEGACY_MODEL = "claude-opus-4-6"


# --- header builders: synthetic images, so no binary fixtures are committed ------------------


def png_bytes(width: int, height: int, filler: int = 0) -> bytes:
    """A minimal but structurally valid PNG header, optionally padded with image data."""
    ihdr = (
        (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )
    return b"\x89PNG\r\n\x1a\n" + ihdr + b"\x00" * filler


def jpeg_bytes(width: int, height: int, leading_segments: int = 0) -> bytes:
    """A JPEG whose SOF0 sits after ``leading_segments`` application segments."""
    out = b"\xff\xd8"
    for _ in range(leading_segments):
        payload = b"\x00" * 100
        out += b"\xff\xe0" + (len(payload) + 2).to_bytes(2, "big") + payload
    sof = b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03" + b"\x00" * 9
    return out + b"\xff\xc0" + (len(sof) + 2).to_bytes(2, "big") + sof


def webp_vp8x_bytes(width: int, height: int) -> bytes:
    payload = b"\x00" * 4 + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    chunk = b"VP8X" + len(payload).to_bytes(4, "little") + payload
    return b"RIFF" + (len(chunk) + 4).to_bytes(4, "little") + b"WEBP" + chunk


def webp_lossy_bytes(width: int, height: int) -> bytes:
    payload = (
        b"\x00\x00\x00"
        + b"\x9d\x01\x2a"
        + width.to_bytes(2, "little")
        + height.to_bytes(2, "little")
    )
    chunk = b"VP8 " + len(payload).to_bytes(4, "little") + payload
    return b"RIFF" + (len(chunk) + 4).to_bytes(4, "little") + b"WEBP" + chunk


def webp_lossless_bytes(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + bits.to_bytes(4, "little")
    chunk = b"VP8L" + len(payload).to_bytes(4, "little") + payload
    return b"RIFF" + (len(chunk) + 4).to_bytes(4, "little") + b"WEBP" + chunk


def image_block(raw: bytes, media_type: str = "image/png") -> dict[str, object]:
    """One base64 image content block, in the shape the transcript uses."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(raw).decode("ascii"),
        },
    }


def image_result(raw: bytes, media_type: str = "image/png") -> dict[str, object]:
    """A tool result carrying a single base64 image."""
    return {"content": [image_block(raw, media_type)]}


# --- the ladder ------------------------------------------------------------------------------


class TestTokenQuantity:
    def test_basis_must_be_a_declared_value(self) -> None:
        """The basis vocabulary lives in config/components.py, not at the call site."""
        with pytest.raises(ValueError, match="unknown basis"):
            TokenQuantity(tokens=1, basis="guessed", confidence="low", method="x")

    def test_confidence_must_be_a_declared_value(self) -> None:
        with pytest.raises(ValueError, match="unknown confidence"):
            TokenQuantity(tokens=1, basis="exact", confidence="pretty sure", method="x")

    def test_every_quantity_carries_a_method(self) -> None:
        """A figure nobody can trace back is a defect, not a figure (Principle X)."""
        with pytest.raises(ValueError, match="method"):
            TokenQuantity(tokens=1, basis="exact", confidence="high", method="")

    def test_a_withheld_quantity_has_no_number_and_says_why(self) -> None:
        quantity = resolve_payload_tokens({"isImage": True}, model=HIGH_RES_MODEL)
        assert quantity.is_withheld
        assert quantity.tokens is None
        assert "image" in quantity.method


class TestExactTier:
    def test_a_recorded_count_beats_every_estimate(self) -> None:
        """Never estimate where the source data provides a count (python.md, Numeric handling)."""
        payload = {"stdout": "x" * 4000, "result_tokens": 37}
        quantity = resolve_payload_tokens(payload, tool_name="Bash")
        assert quantity.tokens == 37
        assert quantity.basis == "exact"
        assert quantity.confidence == "high"


class TestImageDimensions:
    @pytest.mark.parametrize(
        "builder",
        [png_bytes, jpeg_bytes, webp_vp8x_bytes, webp_lossy_bytes, webp_lossless_bytes],
    )
    def test_reads_dimensions_from_each_supported_header(
        self, builder: Callable[[int, int], bytes]
    ) -> None:
        assert read_image_dimensions(builder(1280, 720)) == (1280, 720)

    def test_finds_a_jpeg_sof_behind_earlier_segments(self) -> None:
        """Real screenshots carry EXIF ahead of the frame header; a fixed offset would miss it."""
        assert read_image_dimensions(jpeg_bytes(800, 600, leading_segments=5)) == (800, 600)

    def test_unrecognised_format_reads_as_unknown_rather_than_a_guess(self) -> None:
        assert read_image_dimensions(b"GIF89a" + b"\x00" * 40) is None

    def test_format_is_sniffed_not_trusted_from_the_declared_media_type(self) -> None:
        """A wrong number is worse than no number, so the signature decides."""
        quantity = resolve_payload_tokens(
            image_result(jpeg_bytes(1000, 500), media_type="image/png"),
            model=HIGH_RES_MODEL,
        )
        assert quantity.tokens == -(-(1000 * 500) // PIXELS_PER_IMAGE_TOKEN)


class TestImageTokens:
    def test_area_formula(self) -> None:
        quantity = image_tokens(1000, 750, HIGH_RES_MODEL)
        assert quantity.tokens == 1000
        assert quantity.basis == "measured"
        assert "1000*750/750" in quantity.method

    def test_the_per_image_cap_is_applied(self) -> None:
        """A 2560x1430 screenshot saturates: the API resizes before tokenizing."""
        quantity = image_tokens(2560, 1430, HIGH_RES_MODEL)
        uncapped = -(-(2560 * 1430) // PIXELS_PER_IMAGE_TOKEN)
        assert uncapped == 4882
        assert quantity.tokens == HIGH_RESOLUTION_IMAGE_TOKEN_CAP
        assert "capped" in quantity.method

    def test_the_cap_is_per_model_and_not_ordered_by_recency(self) -> None:
        """Opus 4.7 is high-resolution and the newer-numbered 4.6 is not — hence a table."""
        assert image_token_cap("claude-opus-4-7") == HIGH_RESOLUTION_IMAGE_TOKEN_CAP
        assert image_token_cap("claude-opus-4-6") == LEGACY_IMAGE_TOKEN_CAP
        assert image_tokens(2560, 1430, LEGACY_MODEL).tokens == LEGACY_IMAGE_TOKEN_CAP

    def test_model_id_suffixes_do_not_change_the_tier(self) -> None:
        assert normalize_model_id("claude-opus-4-8[1m]") == "claude-opus-4-8"
        assert normalize_model_id("claude-opus-4-5-20251101") == "claude-opus-4-5"
        assert image_token_cap("claude-opus-4-5-20251101") == LEGACY_IMAGE_TOKEN_CAP

    def test_unknown_model_raises_rather_than_defaulting_a_cap(self) -> None:
        with pytest.raises(UnknownImageModelError, match="claude-opus-9"):
            image_token_cap("claude-opus-9")

    def test_missing_model_raises(self) -> None:
        with pytest.raises(UnknownImageModelError, match="no model recorded"):
            image_token_cap(None)

    def test_zero_dimensions_raise(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            image_tokens(0, 100, HIGH_RES_MODEL)


class TestImagesAreNeverSizedFromCharacters:
    def test_a_large_base64_image_does_not_produce_an_absurd_token_count(self) -> None:
        """The headline defect, pinned.

        A ~3 MB screenshot is ~4M base64 characters, which `chars // 4` reports as ~1,000,000
        tokens. Its real cost is the per-image cap. Anything above the cap here means image
        payloads are being counted as prose again.
        """
        raw = png_bytes(2560, 1430, filler=3_000_000)
        block = image_block(raw)
        payload = {"content": [block]}
        naive = len(base64.b64encode(raw).decode("ascii")) // 4
        assert naive > 900_000, "fixture is not large enough to demonstrate the defect"

        quantity = resolve_payload_tokens(payload, model=HIGH_RES_MODEL)
        assert quantity.tokens == HIGH_RESOLUTION_IMAGE_TOKEN_CAP
        assert quantity.basis == "measured"
        assert quantity.tokens is not None and quantity.tokens < naive / 100

    def test_an_unreadable_image_is_withheld_not_estimated(self) -> None:
        """FR-019: decline the figure and show the gap. There is no character fallback here."""
        payload = image_result(b"GIF89a" + b"\x00" * 100_000)
        quantity = resolve_payload_tokens(payload, model=HIGH_RES_MODEL)
        assert quantity.is_withheld
        assert "pixel dimensions" in quantity.method
        assert "character count" in quantity.method

    def test_an_undecodable_payload_is_withheld(self) -> None:
        payload = {"content": [{"type": "image", "source": {"data": "!!! not base64 !!!"}}]}
        quantity = resolve_payload_tokens(payload, model=HIGH_RES_MODEL)
        assert quantity.is_withheld
        assert "could not be decoded" in quantity.method

    def test_an_image_on_an_unknown_model_is_withheld(self) -> None:
        quantity = resolve_payload_tokens(image_result(png_bytes(100, 100)), model="claude-opus-9")
        assert quantity.is_withheld
        assert "claude-opus-9" in quantity.method

    def test_a_bash_result_flagged_as_an_image_with_no_pixels_is_withheld(self) -> None:
        payload = {"stdout": "x" * 40_000, "stderr": "", "isImage": True}
        quantity = resolve_payload_tokens(payload, model=HIGH_RES_MODEL, tool_name="Bash")
        assert quantity.is_withheld

    def test_text_beside_an_image_is_counted_but_the_base64_is_not(self) -> None:
        raw = png_bytes(750, 1000, filler=50_000)
        payload = {"content": [{"type": "text", "text": "y" * 400}, image_block(raw)]}
        quantity = resolve_payload_tokens(payload, model=HIGH_RES_MODEL)
        assert quantity.tokens == 1000 + 100
        assert quantity.basis == "estimated", "the weakest component sets the basis"


class TestReadResults:
    def test_uses_file_content_and_reports_num_lines_and_total_lines(self) -> None:
        """`file.numLines`/`totalLines` are a better size signal than the rendered result."""
        payload = {
            "type": "text",
            "file": {
                "filePath": "/tmp/a.py",
                "content": "z" * 4000,
                "numLines": 120,
                "startLine": 1,
                "totalLines": 120,
            },
        }
        quantity = resolve_tool_result_tokens(
            ToolResultRecord(
                uuid="u",
                line=1,
                tool_use_id="t",
                tool_name="Read",
                payload=payload,
                text_length=999_999,
            )
        )
        assert quantity.tokens == 1000, "sized from file.content, not the rendered text"
        assert "120 of 120 lines" in quantity.method
        assert "whole file" in quantity.method
        assert quantity.basis == "estimated"

    def test_a_partial_read_is_named_as_partial(self) -> None:
        payload = {"file": {"content": "z" * 80, "numLines": 50, "totalLines": 4000}}
        quantity = resolve_payload_tokens(payload, tool_name="Read")
        assert "PARTIAL read" in quantity.method
        assert "50 of 4000 lines" in quantity.method

    def test_a_read_with_no_content_falls_back_to_the_rendered_length(self) -> None:
        payload = {"file": {"numLines": 10, "totalLines": 10}}
        quantity = resolve_payload_tokens(payload, tool_name="Read", text_length=400)
        assert quantity.tokens == 100
        assert "file.content absent" in quantity.method


class TestOtherPayloadShapes:
    def test_a_bare_string_payload_is_handled(self) -> None:
        """48 of 1,595 observed results were bare strings; none may crash the sizer."""
        quantity = resolve_payload_tokens("hello world" * 10, tool_name="Task")
        assert quantity.tokens == 27
        assert quantity.basis == "estimated"

    def test_shell_output_sums_stdout_and_stderr(self) -> None:
        payload = {"stdout": "a" * 400, "stderr": "b" * 400, "interrupted": True}
        quantity = resolve_payload_tokens(payload, tool_name="Bash")
        assert quantity.tokens == 200
        assert "interrupted" in quantity.method

    def test_a_structured_edit_payload_falls_back_to_its_serialised_size(self) -> None:
        payload = {
            "filePath": "/tmp/a.py",
            "oldString": "o" * 200,
            "newString": "n" * 200,
            "structuredPatch": [{"lines": ["+" * 50]}],
        }
        quantity = resolve_payload_tokens(payload, tool_name="Edit")
        assert quantity.tokens is not None and quantity.tokens > 0
        assert "serialised" in quantity.method

    def test_an_empty_payload_reports_zero_rather_than_crashing(self) -> None:
        quantity = resolve_payload_tokens(None, tool_name="Bash")
        assert quantity.tokens == 0
        assert quantity.basis == "estimated"

    @pytest.mark.parametrize(
        "payload",
        [
            "a bare string",
            None,
            {"stdout": "x", "stderr": ""},
            {"file": {"content": "x", "numLines": 1, "totalLines": 1}},
            {"filePath": "/x", "oldString": "a", "newString": "b"},
            [{"type": "text", "text": "hello"}],
            {"result_tokens": 5},
        ],
    )
    def test_every_quantity_carries_a_declared_basis_and_confidence(self, payload: object) -> None:
        quantity = resolve_payload_tokens(payload, model=HIGH_RES_MODEL, tool_name="Tool")
        assert quantity.basis in BASIS_VALUES
        assert quantity.confidence in CONFIDENCE_VALUES
        assert quantity.method


class TestCharacterEstimates:
    def test_the_method_says_plainly_that_it_is_an_estimate(self) -> None:
        """Never present an estimate as a measurement (Principle X)."""
        quantity = estimate_from_characters(4000, "a file")
        assert quantity.tokens == 1000
        assert quantity.basis == "estimated"
        assert quantity.confidence == "low"
        assert "NOT a measurement" in quantity.method

    def test_negative_characters_raise(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            estimate_from_characters(-1, "a file")
