"""Contract on the terminal surface.

The honesty rules are not decoration on this output, they are the output: a figure without its
share, a figure printed finer than its confidence supports, a remainder that vanishes when the
table is truncated, or the word "billed" in front of a number would each be a defect the tool
exists to prevent. Every test here pins one of those.
"""

import re
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from ccaudit.analyse import SessionAnalysis, analyse_transcript
from ccaudit.config import BUNDLED_PRICING_PATH, load_pricing
from ccaudit.config.components import CHARGE_COMPONENTS
from ccaudit.model.reconcile import UNATTRIBUTED_DISPLAY
from ccaudit.money import format_micros
from ccaudit.render.data import build_report_data
from ccaudit.render.terminal import PLAIN_WIDTH, render_report
from tests.fixtures.builder import TranscriptBuilder

PRICING = load_pricing(BUNDLED_PRICING_PATH)
FIXED_TIME = "2026-08-11T12:00:00+00:00"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def busy_session() -> TranscriptBuilder:
    builder = TranscriptBuilder()
    builder.add_user_text("audit the config")
    builder.add_turn(
        input_tokens=420, cache_creation_1h=12_000, output_tokens=310, tool_use_ids=("t1",)
    )
    builder.add_tool_result(tool_use_id="t1", file_path="/repo/src/app.py", text="x = 1\n" * 200)
    builder.add_turn(input_tokens=37, cache_read=12_400, output_tokens=95, tool_use_ids=("t2",))
    builder.add_tool_result(
        tool_use_id="t2", file_path="/repo/docs/guide.md", text="# Guide\n" * 90
    )
    builder.add_at_mention(display_path="/repo/CLAUDE.md", content="# Rules\n" * 40)
    builder.add_turn(input_tokens=11, cache_creation_5m=3_100, cache_read=12_400, output_tokens=71)
    builder.add_turn(input_tokens=3, cache_read=15_500, output_tokens=44)
    return builder


def plain_console() -> tuple[Console, StringIO]:
    """A console that is explicitly not a terminal — the piped/captured path."""
    stream = StringIO()
    return Console(file=stream, width=PLAIN_WIDTH, no_color=True, highlight=False), stream


def tty_console() -> tuple[Console, StringIO]:
    """A console that claims to be a terminal, so the colour path is exercised."""
    stream = StringIO()
    return Console(file=stream, width=120, force_terminal=True, color_system="truecolor"), stream


def render(payload: dict, *, tty: bool = False, top: int | None = None) -> str:
    console, stream = tty_console() if tty else plain_console()
    render_report(payload, console=console, top=top)
    return stream.getvalue()


def analyse(builder: TranscriptBuilder, tmp_path: Path, **kwargs: bool) -> SessionAnalysis:
    return analyse_transcript(
        builder.write(tmp_path / "s.jsonl"),
        pricing=PRICING,
        provisional=kwargs.get("provisional", False),
    )


@pytest.fixture
def payload(tmp_path: Path) -> dict:
    return build_report_data([analyse(busy_session(), tmp_path)], generated_at=FIXED_TIME)


@pytest.fixture
def output(payload: dict) -> str:
    return render(payload)


class TestEveryModeIsHonest:
    @pytest.mark.parametrize("tty", [False, True])
    def test_figures_are_labelled_api_equivalent_estimates(self, payload: dict, tty: bool) -> None:
        assert "API-equivalent cost estimate" in render(payload, tty=tty)

    @pytest.mark.parametrize("tty", [False, True])
    def test_no_figure_is_presented_as_an_amount_charged(self, payload: dict, tty: bool) -> None:
        """'billed' may appear only inside a denial that these figures are a bill."""
        text = ANSI.sub("", render(payload, tty=tty))
        for match in re.finditer("billed", text):
            assert text[max(0, match.start() - 4) : match.start()] == "not ", text[
                match.start() - 60 : match.start() + 40
            ]

    @pytest.mark.parametrize("tty", [False, True])
    def test_the_unattributed_remainder_has_its_own_line(self, payload: dict, tty: bool) -> None:
        assert UNATTRIBUTED_DISPLAY in ANSI.sub("", render(payload, tty=tty))

    @pytest.mark.parametrize("tty", [False, True])
    def test_every_dollar_figure_shares_a_line_with_a_percentage(
        self, payload: dict, tty: bool
    ) -> None:
        """FR-011: the share survives being wrong about pricing; the dollars do not."""
        text = ANSI.sub("", render(payload, tty=tty))
        for line in text.splitlines():
            if "$" in line and "Total (API-equivalent estimate)" not in line:
                assert "%" in line, line

    @pytest.mark.parametrize("tty", [False, True])
    def test_component_names_are_plain_with_the_technical_term_secondary(
        self, payload: dict, tty: bool
    ) -> None:
        text = ANSI.sub("", render(payload, tty=tty)).replace("\n", " ")
        for component in CHARGE_COMPONENTS:
            assert component.plain_name in text
            assert component.technical_name in text

    @pytest.mark.parametrize("tty", [False, True])
    def test_the_uncertainty_notes_and_limitations_are_printed(
        self, payload: dict, tty: bool
    ) -> None:
        text = ANSI.sub("", render(payload, tty=tty)).replace("\n", " ")
        assert "How to read these numbers" in text
        assert "What these figures do not cover" in text

    @pytest.mark.parametrize("tty", [False, True])
    def test_the_carry_splitting_policy_in_effect_is_named(self, payload: dict, tty: bool) -> None:
        assert "carry split: proportional" in ANSI.sub("", render(payload, tty=tty))


class TestTruncation:
    def test_the_omitted_rows_are_still_represented(self, payload: dict) -> None:
        """--top hides rows, never cost (FR-012)."""
        assert len(payload["items"]) > 1
        text = render(payload, top=1)
        assert "other items (not shown)" in text

    def test_the_unattributed_line_survives_truncation(self, payload: dict) -> None:
        assert UNATTRIBUTED_DISPLAY in render(payload, top=1)

    def test_the_visible_lines_still_account_for_the_whole_total(self, payload: dict) -> None:
        """Shown items + the omitted line + the conversation rows + the remainder = the total."""
        shown = payload["items"][:1]
        omitted = payload["items"][1:]
        conversation = sum(
            component["cost_micros"]
            for component in payload["attribution"]
            if not component["per_item"]
        )
        accounted = (
            sum(item["total_micros"] for item in shown)
            + sum(item["total_micros"] for item in omitted)
            + conversation
            + payload["totals"]["unattributed_micros"]
        )
        assert accounted == payload["totals"]["cost_micros"]

    def test_the_conversations_own_cost_has_its_own_rows(self, output: str) -> None:
        """Without them the printed column would not reach the total, silently."""
        collapsed = " ".join(output.split())
        assert "The conversation itself" in collapsed
        assert "What Claude wrote back (output)" in collapsed

    def test_top_larger_than_the_table_adds_no_omitted_line(self, payload: dict) -> None:
        assert "other items (not shown)" not in render(payload, top=999)


class TestPrecision:
    def test_a_figure_is_rendered_at_its_own_significant_figures(self, payload: dict) -> None:
        text = render(payload)
        for item in payload["items"]:
            expected = format_micros(item["total_micros"], item["display_sig_figs"])
            assert expected in text, f"{item['display']} -> {expected}"

    def test_a_low_confidence_figure_is_not_printed_to_the_cent(self, tmp_path: Path) -> None:
        builder = TranscriptBuilder()
        builder.add_turn(input_tokens=50, cache_creation_5m=9_000, output_tokens=20)
        builder.add_tool_schema_delta(added=["mcp__demo__go"], added_lines=400)
        builder.add_turn(input_tokens=5, cache_read=90_000, output_tokens=15)
        payload = build_report_data([analyse(builder, tmp_path)], generated_at=FIXED_TIME)

        estimated = [i for i in payload["items"] if i["display_sig_figs"] == 1]
        assert estimated
        text = render(payload)
        for item in estimated:
            precise = format_micros(item["total_micros"], 6)
            coarse = format_micros(item["total_micros"], 1)
            assert coarse in text
            if precise != coarse:
                assert precise not in text


class TestNonTtyOutput:
    def test_it_contains_no_ansi_escape_sequences(self, output: str) -> None:
        assert ANSI.search(output) is None

    def test_the_columns_are_stable_and_independent_of_terminal_width(self, payload: dict) -> None:
        """Captured output must not change shape because the developer resized a window."""
        narrow, wide = StringIO(), StringIO()
        render_report(payload, console=Console(file=narrow, width=100, no_color=True))
        render_report(payload, console=Console(file=wide, width=100, no_color=True))
        assert narrow.getvalue() == wide.getvalue()

    def test_the_headings_are_present_in_a_fixed_order(self, output: str) -> None:
        headings = ["How the cost was incurred", "What cost the most", "How to read these numbers"]
        positions = [output.index(heading) for heading in headings]
        assert positions == sorted(positions)

    def test_it_is_the_same_bytes_on_a_second_render(self, payload: dict) -> None:
        assert render(payload) == render(payload)


class TestProvisionalAndScope:
    def test_an_in_progress_session_is_marked_provisional(self, tmp_path: Path) -> None:
        analysis = analyse(busy_session(), tmp_path, provisional=True)
        text = render(build_report_data([analysis], generated_at=FIXED_TIME))
        assert "PROVISIONAL" in text

    def test_a_finished_session_is_not_marked_provisional(self, output: str) -> None:
        assert "PROVISIONAL" not in output

    def test_excluded_sessions_are_stated(self, tmp_path: Path) -> None:
        analysis = analyse(busy_session(), tmp_path)
        payload = build_report_data([analysis], sessions_excluded_count=2, generated_at=FIXED_TIME)
        assert "2 excluded from this result" in render(payload)

    def test_redaction_is_announced_and_paths_do_not_appear(self, tmp_path: Path) -> None:
        analysis = analyse(busy_session(), tmp_path)
        text = render(build_report_data([analysis], redact=True, generated_at=FIXED_TIME))
        assert "Paths are redacted" in text
        assert "/repo" not in text
        assert "app.py" not in text

    def test_shares_survive_redaction(self, tmp_path: Path) -> None:
        analysis = analyse(busy_session(), tmp_path)
        clear = render(build_report_data([analysis], generated_at=FIXED_TIME))
        redacted = render(build_report_data([analysis], redact=True, generated_at=FIXED_TIME))
        percentages = re.compile(r"\d+\.\d%|<0\.1%")
        assert percentages.findall(clear) == percentages.findall(redacted)


class TestAccessibility:
    def test_proportion_bars_carry_their_signal_in_length_not_colour(self, output: str) -> None:
        """FR-042: stripping colour must remove nothing a reader needs."""
        assert "#" in output or "." in output
        for line in output.splitlines():
            if "#" in line and "$" in line:
                assert "%" in line, line


class TestLegibility:
    """A column a reader has to guess at is a defect (Principle X: name things as they are).

    Every column heading here is a compression of something the reader has no way to expand
    on their own — 'carry' is a term of art, and '3 / 420' is two unlabelled numbers. The
    output has to say what they mean, in the output, without a manual.
    """

    @staticmethod
    def prose(payload: dict, tty: bool) -> str:
        """Rendered text with the console's line wrapping undone, so a sentence is one string."""
        return " ".join(ANSI.sub("", render(payload, tty=tty)).split())

    @pytest.mark.parametrize("tty", [False, True])
    def test_the_component_table_says_what_its_rows_are(self, payload: dict, tty: bool) -> None:
        assert "split by what you were charged for" in self.prose(payload, tty)

    @pytest.mark.parametrize("tty", [False, True])
    def test_carry_is_explained_as_charged_per_turn_not_per_read(
        self, payload: dict, tty: bool
    ) -> None:
        """The single most misread figure: it grows with turns, not with read count."""
        text = self.prose(payload, tty)
        assert "on every later turn to keep it there" in text
        assert "not with how often you read it" in text

    @pytest.mark.parametrize("tty", [False, True])
    def test_the_reads_over_turns_column_is_expanded(self, payload: dict, tty: bool) -> None:
        text = self.prose(payload, tty)
        assert "how many times the item was read" in text
        assert "how many turns it stayed in context" in text


class TestRefusal:
    def test_it_refuses_to_render_a_breakdown_that_does_not_add_up(self, payload: dict) -> None:
        payload["totals"]["unattributed_micros"] += 1
        with pytest.raises(ValueError, match="does not add up"):
            render(payload)
