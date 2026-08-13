"""What ccost itself costs a session it is installed in.

**The tool must not become the thing it measures.** Always-resident tool descriptions are the
single largest block of resident context — roughly 50x a project's instruction file — so a
cost-observability tool that added to that block would corrupt the baseline it exists to
measure and show up in its own reports. That is why there is no MCP server (see
`plugin/README.md`), and why FR-056 requires the tool to **measure and disclose** its own
contribution rather than assert it is negligible. SC-017 puts the bar at 0.5% of session cost.

What is actually resident when the plugin is installed and unused:

============================  ==============================================================
Slash command                 Nothing. Not in context until typed.
`SessionEnd` hook             Nothing. Runs outside the conversation.
Skill **body**                Nothing. Loads on invocation.
Skill **description**         **This is the whole footprint** — one frontmatter line, present
                              in the skill listing of every session.
============================  ==============================================================

So the measurement is: price that description at the session's own rates, for as many turns as
it was resident, and show it as a share of the session total.

**It is an estimate and says so.** The description's token count is character-based (the
records do not itemise the skill listing), so the figure carries an `estimated` basis and low
confidence, and the number is deliberately rounded coarsely. An over-estimate is the safe
direction here: this figure exists to be checked against a ceiling, and a tool flattering
itself about its own cost would be the worst possible place to be optimistic.
"""

from dataclasses import dataclass
from pathlib import Path

from claude_cost_tracker.analyse import SessionAnalysis
from claude_cost_tracker.ingest.tokens import estimate_from_characters
from claude_cost_tracker.money import cost_micros

SKILL_PATH = Path(__file__).parent / "plugin" / "skills" / "ccost" / "SKILL.md"

# The share of session cost the tool's own resident content must stay under (SC-017).
FOOTPRINT_CEILING_SHARE = 0.005


@dataclass(frozen=True)
class Footprint:
    """claude-cost-tracker's own contribution to a session, and whether it is within its own bar."""

    description_chars: int
    description_tokens: int
    turns_resident: int
    cost_micros: int
    session_total_micros: int
    method: str
    basis: str = "estimated"
    confidence: str = "low"

    @property
    def share(self) -> float:
        if self.session_total_micros == 0:
            return 0.0
        return self.cost_micros / self.session_total_micros

    @property
    def within_ceiling(self) -> bool:
        return self.share <= FOOTPRINT_CEILING_SHARE

    def lines(self) -> list[str]:
        """The disclosure, worded so a reader can judge it rather than take it on trust."""
        verdict = (
            f"within the {FOOTPRINT_CEILING_SHARE:.1%} ceiling this tool sets for itself"
            if self.within_ceiling
            else f"ABOVE the {FOOTPRINT_CEILING_SHARE:.1%} ceiling this tool sets for itself"
        )
        return [
            "What ccost itself cost this session",
            "",
            f"  resident content: the skill description only ({self.description_chars} characters)",
            f"  estimated size:   {self.description_tokens} tokens",
            f"  turns resident:   {self.turns_resident}",
            (
                f"  estimated cost:   {self.cost_micros / 1_000_000:.4f} USD "
                f"({self.share:.3%} of this session) — {verdict}"
            ),
            "",
            f"  how: {self.method}",
            "",
            "  Nothing else ccost ships is resident when you are not using it: the slash",
            "  command costs nothing until typed, the skill body loads only on invocation, and",
            "  the SessionEnd hook runs outside the conversation. There is no MCP server, by",
            "  design — its tool descriptions would sit in every session, permanently.",
            "",
            "  This is an estimate, not a measurement: the transcript does not itemise the",
            "  skill listing, so the size is character-based. It is rounded generously upward,",
            "  because a tool flattering itself about its own cost would be the worst place to",
            "  be optimistic.",
        ]


def measure(analysis: SessionAnalysis, *, skill_path: Path | None = None) -> Footprint:
    """Measure claude-cost-tracker's own resident cost against one analysed session."""
    path = skill_path or SKILL_PATH
    description_chars = _description_length(path)
    quantity = estimate_from_characters(
        description_chars, "claude-cost-tracker's own skill description"
    )
    tokens = quantity.tokens or 0

    turns = len(analysis.timeline.turns)
    model = analysis.pricing.for_model(analysis.timeline.turns[0].model) if turns else None

    if model is None or tokens == 0:
        return Footprint(
            description_chars=description_chars,
            description_tokens=tokens,
            turns_resident=0,
            cost_micros=0,
            session_total_micros=analysis.total_micros,
            method="no priced turns in this session, so there is nothing to be resident through",
        )

    # Charged at the cache-read rate on every turn, which is the cheapest lane it could
    # possibly sit in — and therefore the *lower* bound on its cost. Stated plainly so the
    # figure is read as the floor it is.
    per_turn = cost_micros(tokens, model.input_micros_per_mtok, analysis.pricing.cache.read)
    return Footprint(
        description_chars=description_chars,
        description_tokens=tokens,
        turns_resident=turns,
        cost_micros=per_turn * turns,
        session_total_micros=analysis.total_micros,
        method=(
            f"{tokens} tokens x {turns} turns at the cache-read rate for "
            f"{model.model_id}. Character-based size estimate; priced in the cheapest lane it "
            f"could occupy, so this is a floor rather than a worst case."
        ),
    )


def _description_length(path: Path) -> int:
    """Characters of the skill's frontmatter description — the only always-resident part.

    Raises rather than guessing if the skill is not where it should be: a footprint figure
    invented from a missing file would be worse than no figure.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"cannot measure claude-cost-tracker's own footprint: {path} is missing, and the skill "
            f"description is the thing being measured."
        )
    text = path.read_text(encoding="utf-8")
    if "description:" not in text:
        raise ValueError(f"{path} has no frontmatter description to measure")
    after = text.split("description:", 1)[1]
    return len(after.split("\n", 1)[0].strip())
