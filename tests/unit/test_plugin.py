"""Contract on the shipped Claude Code plugin.

The plugin's whole design constraint is that **the tool must not become the thing it
measures**. Always-resident tool descriptions are the largest single block of resident context
— roughly 50x a project's instruction file — so an integration that added to that block would
corrupt the baseline this tool exists to measure and appear in its own reports.

These tests fence that constraint structurally, because it is the kind of thing that erodes
one convenient addition at a time.
"""

import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "src" / "ccaudit" / "plugin"


class TestLayout:
    def test_the_manifest_is_where_claude_code_looks_for_it(self) -> None:
        assert (PLUGIN / ".claude-plugin" / "plugin.json").is_file()

    def test_the_content_directories_are_at_the_plugin_root(self) -> None:
        """Commands, skills, and hooks live at the root — not inside .claude-plugin/."""
        assert (PLUGIN / "commands" / "audit.md").is_file()
        assert (PLUGIN / "skills" / "ccaudit" / "SKILL.md").is_file()
        assert (PLUGIN / "hooks" / "hooks.json").is_file()

    def test_the_manifest_is_valid_json_with_a_name_and_version(self) -> None:
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        assert manifest["name"] == "ccaudit"
        assert manifest["version"]
        assert manifest["description"]


class TestFootprint:
    def test_no_mcp_server_is_declared_anywhere(self) -> None:
        """The one thing this plugin must never ship (FR-055).

        An MCP server's tool descriptions are resident in every session, permanently.
        """
        for path in PLUGIN.rglob("*"):
            if not path.is_file() or path.suffix not in (".json", ".md"):
                continue
            text = path.read_text(encoding="utf-8").lower()
            assert "mcpservers" not in text.replace("_", "").replace(" ", "")
            if path.suffix == ".json":
                assert "mcp" not in text, f"{path.name} mentions MCP"

    def test_the_skill_description_is_short_enough_to_be_resident(self) -> None:
        """The description is the only part that is always loaded. It has to earn its size."""
        frontmatter = (PLUGIN / "skills" / "ccaudit" / "SKILL.md").read_text(encoding="utf-8")
        description = frontmatter.split("description:", 1)[1].split("\n", 1)[0]
        assert 0 < len(description) < 600


class TestSessionEndHook:
    def test_the_hook_enqueues_and_does_not_analyse(self) -> None:
        """SessionEnd shares a 60s budget a plugin cannot raise for itself.

        A 30-second analysis run inline would be silently cancelled on someone else's machine.
        """
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        commands = [
            entry["command"] for group in hooks["hooks"]["SessionEnd"] for entry in group["hooks"]
        ]
        assert commands == ["ccaudit _enqueue"]
        assert not any("analyse" in command or "report" in command for command in commands)

    def test_the_hook_timeout_is_well_inside_the_budget(self) -> None:
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        timeout = hooks["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"]
        assert timeout <= 30, "must return far sooner than the 60s overall SessionEnd budget"


class TestHonestyInstructions:
    @pytest.mark.parametrize(
        "relative",
        ["commands/audit.md", "skills/ccaudit/SKILL.md"],
    )
    def test_every_invocable_surface_says_the_figures_are_not_a_bill(self, relative: str) -> None:
        """FR-010 reaches the model-facing text too — that is where it gets paraphrased away."""
        text = (PLUGIN / relative).read_text(encoding="utf-8")
        assert "API-equivalent" in text
        assert "not a bill" in text or "not a bill." in text or "never be worded as one" in text

    @pytest.mark.parametrize("relative", ["commands/audit.md", "skills/ccaudit/SKILL.md"])
    def test_every_invocable_surface_requires_the_share_alongside_the_figure(
        self, relative: str
    ) -> None:
        text = (PLUGIN / relative).read_text(encoding="utf-8")
        assert "share" in text.lower()

    @pytest.mark.parametrize("relative", ["commands/audit.md", "skills/ccaudit/SKILL.md"])
    def test_every_invocable_surface_protects_the_unattributed_line(self, relative: str) -> None:
        """It is the first thing that gets dropped for looking untidy (FR-012, FR-013)."""
        text = (PLUGIN / relative).read_text(encoding="utf-8")
        assert "unattributed" in text.lower()

    @pytest.mark.parametrize("relative", ["commands/audit.md", "skills/ccaudit/SKILL.md"])
    def test_every_invocable_surface_handles_exit_code_three(self, relative: str) -> None:
        """A breakdown that does not add up must not be reported as figures."""
        text = (PLUGIN / relative).read_text(encoding="utf-8")
        assert "3" in text
        assert "add up" in text

    def test_the_skill_explains_the_two_causes_with_opposite_fixes(self) -> None:
        """A ranking without a cause is a report, not a tool (US2)."""
        text = (PLUGIN / "skills" / "ccaudit" / "SKILL.md").read_text(encoding="utf-8")
        assert "Loading into context" in text
        assert "Keeping context loaded" in text
