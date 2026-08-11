"""System test — the optional marimo notebook surface.

The notebook is an *additional* local surface, never a replacement for the shareable report:
that has to be one self-contained file that opens offline with no tooling (FR-032), and a
notebook is not that. These tests pin the two properties that keep it honest — that it costs
this project no dependency, and that it computes no figure of its own.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ccaudit.cli import EXIT_INTERRUPTED, EXIT_OK, EXIT_USAGE, main, marimo_command
from ccaudit.notebook import COMMAND_PLACEHOLDER, NOTEBOOK_SOURCE, ccaudit_command, write_notebook

pytestmark = pytest.mark.system


@pytest.fixture
def notebook(tmp_path: Path) -> str:
    return write_notebook(tmp_path / "nb.py").read_text(encoding="utf-8")


class TestItIsAValidNotebook:
    def test_it_is_valid_python(self, notebook: str) -> None:
        """A marimo notebook *is* a Python file; if it does not parse, nothing else matters."""
        ast.parse(notebook)

    def test_it_declares_its_own_environment(self, notebook: str) -> None:
        """PEP 723 inline metadata is what lets `marimo edit --sandbox` install for this file
        alone, so nothing lands on the machine and nothing lands in this project."""
        assert notebook.startswith("# /// script")
        assert "dependencies = " in notebook

    def test_it_has_cells_and_an_entry_point(self, notebook: str) -> None:
        assert "@app.cell" in notebook
        assert "app = marimo.App(" in notebook
        assert 'if __name__ == "__main__":' in notebook


class TestItCostsThisProjectNoDependency:
    def test_ccaudit_never_imports_marimo(self) -> None:
        """The runtime dependency set stays `rich` and nothing else (Principle II)."""
        source = Path("src/ccaudit").rglob("*.py")
        for module in source:
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    assert not name.startswith(("marimo", "altair", "pandas")), (
                        f"{module} imports {name}: the notebook's dependencies must stay in the "
                        f"notebook"
                    )

    def test_writing_one_needs_nothing_installed(self, tmp_path: Path) -> None:
        """It runs in this environment, which has no marimo."""
        result = subprocess.run(
            [sys.executable, "-c", "import marimo"], capture_output=True, check=False
        )
        assert result.returncode != 0, "this test is vacuous if marimo is installed"
        assert write_notebook(tmp_path / "nb.py").exists()


class TestItComputesNoFigure:
    def test_every_figure_comes_from_the_cli(self, notebook: str) -> None:
        """FR-074 by construction: the notebook renders what ccaudit returns.

        A notebook that summed per-session figures itself would be a second implementation of
        the arithmetic, free to disagree with the first.
        """
        assert "[*CCAUDIT, *args]" in notebook
        assert '"--json"' in notebook

    def test_it_is_told_where_to_find_ccaudit(self, notebook: str) -> None:
        """`ccaudit` is not on PATH under `uvx`, and the notebook runs under a different
        interpreter besides — so the command is resolved at write time and written in as an
        absolute path. Assuming the bare name failed with a bare FileNotFoundError."""
        assert COMMAND_PLACEHOLDER not in notebook
        command = json.loads(notebook.split("CCAUDIT = ", 1)[1].split("\n", 1)[0])
        assert Path(command[0]).is_absolute()

    def test_a_missing_ccaudit_says_what_to_do(self, notebook: str) -> None:
        """The failure it replaces gave the reader nothing to act on."""
        assert "cannot run ccaudit at" in notebook
        assert "uv tool install" in notebook

    def test_it_labels_its_figures_as_estimates(self, notebook: str) -> None:
        assert "API-equivalent" in notebook
        assert "not billed amounts" in notebook

    def test_it_shows_the_unattributed_remainder(self, notebook: str) -> None:
        """The remainder is not optional on any surface (FR-012)."""
        assert "couldn't attribute" in notebook
        assert "unattributed_micros" in notebook

    def test_it_reproduces_the_limitations(self, notebook: str) -> None:
        assert "limitations" in notebook


class TestTheThrowawayNotebook:
    """The default is the same bargain as `ccaudit ui`: one command, nothing left behind."""

    @pytest.fixture
    def launched(self, monkeypatch: pytest.MonkeyPatch) -> list:
        """Capture what would have been launched, without launching marimo."""
        calls: list = []

        def fake_run(command, **kwargs):
            calls.append([str(part) for part in command])
            # The notebook has to exist *while* marimo is running, or there is nothing to open.
            calls.append(Path(command[-1]).exists())
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        return calls

    def test_it_opens_the_notebook_in_a_sandbox(
        self, launched: list, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["notebook"]) == EXIT_OK
        command = launched[0]
        assert command[-3:-1] == ["edit", "--sandbox"]
        assert "marimo" in command

    def test_the_notebook_exists_while_marimo_runs(self, launched: list) -> None:
        main(["notebook"])
        assert launched[1] is True

    def test_nothing_is_left_on_disk_afterwards(self, launched: list) -> None:
        """The whole point of the default: it litters nothing, not even marimo's own state."""
        main(["notebook"])
        assert not Path(launched[0][-1]).exists()
        assert not Path(launched[0][-1]).parent.exists()

    def test_it_says_the_notebook_is_temporary(
        self, launched: list, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["notebook"])
        printed = " ".join(capsys.readouterr().out.split())
        assert "temporary" in printed
        assert "Nothing was left on disk" in printed

    def test_it_says_how_to_keep_one_instead(
        self, launched: list, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["notebook"])
        assert "--out" in " ".join(capsys.readouterr().out.split())

    def test_interrupting_it_still_cleans_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ctrl-C is the exit that actually happens, so it is the one that must not litter."""
        seen: list[Path] = []

        def interrupt(command, **kwargs):
            seen.append(Path(command[-1]))
            raise KeyboardInterrupt

        monkeypatch.setattr(subprocess, "run", interrupt)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        assert main(["notebook"]) == EXIT_INTERRUPTED
        assert seen and not seen[0].exists() and not seen[0].parent.exists()

    def test_without_uv_it_says_what_to_install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Naming the fallback matters: `--out` works with any marimo already on the machine."""
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert main(["notebook"]) == EXIT_USAGE


class TestLaunchingMarimo:
    """`uvx` and `uv tool run` take different arguments, so choosing wrongly runs something else.

    It did. The check was `which("uvx").endswith("uvx")`, which is false for Windows'
    `uvx.exe`, so the command became `uvx tool run marimo` — and uvx read "tool" as a package
    name, went to PyPI, installed a project called `tool`, and reported "Package `tool` does
    not provide any executables". No traceback, no clue.
    """

    def test_uvx_is_invoked_directly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        assert marimo_command() == ["/usr/bin/uvx", "marimo"]

    def test_uvx_exe_is_still_uvx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The bug, in one line: a name is a stem, not the whole file name."""
        monkeypatch.setattr("shutil.which", lambda name: f"C:\\Users\\dev\\.local\\bin\\{name}.exe")
        command = marimo_command()
        assert command is not None
        assert command[1:] == ["marimo"], command

    def test_uv_alone_uses_tool_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: None if name == "uvx" else "/usr/bin/uv")
        assert marimo_command() == ["/usr/bin/uv", "tool", "run", "marimo"]

    def test_no_uv_at_all_is_no_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert marimo_command() is None

    def test_something_that_is_not_uvx_is_not_used_as_uvx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running an unrelated program with marimo's arguments is how this failed the first
        time; falling back to `uv` is the only safe answer."""
        monkeypatch.setattr(
            "shutil.which",
            lambda name: "/usr/bin/not-uvx-at-all" if name == "uvx" else "/usr/bin/uv",
        )
        assert marimo_command() == ["/usr/bin/uv", "tool", "run", "marimo"]

    def test_the_launched_command_never_says_tool_run_after_uvx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact shape that went wrong, asserted end to end."""
        seen: list[list[str]] = []

        def capture(command, **kwargs):
            seen.append([str(part) for part in command])
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(subprocess, "run", capture)
        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}.exe")
        main(["notebook"])
        assert seen
        assert "tool" not in seen[0], seen[0]
        assert seen[0][1] == "marimo", seen[0]


class TestTheCommand:
    def test_it_writes_the_file_and_says_how_to_run_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "deep" / "nb.py"
        assert main(["notebook", "--out", str(out)]) == EXIT_OK
        printed = " ".join(capsys.readouterr().out.split())
        assert out.exists()
        assert "marimo edit --sandbox" in printed

    def test_it_analyses_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Writing a notebook must not need a session, a store, or a rate table."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
        assert main(["notebook", "--out", str(tmp_path / "nb.py")]) == EXIT_OK

    def test_the_written_file_is_the_source_with_the_command_filled_in(
        self, tmp_path: Path
    ) -> None:
        written = write_notebook(tmp_path / "nb.py", command=["/opt/ccaudit"]).read_text(
            encoding="utf-8"
        )
        assert written == NOTEBOOK_SOURCE.replace(COMMAND_PLACEHOLDER, '["/opt/ccaudit"]')


class TestFindingCcauditAgain:
    """`ccaudit` on PATH is the assumption that broke this on a real machine.

    Installed with `uvx --from git+... ccaudit`, the executable lives in a uv cache directory
    and never reaches PATH; the notebook's first cell died with `FileNotFoundError [WinError 2]`
    and nothing to act on. So the command is resolved when the notebook is written, by the
    process that knows the answer.
    """

    def test_it_prefers_the_executable_that_is_running(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        launcher = tmp_path / "ccaudit"
        launcher.write_text("#!/bin/sh\n")
        monkeypatch.setattr("sys.argv", [str(launcher)])
        assert ccaudit_command() == [str(launcher.resolve())]

    def test_it_falls_back_to_one_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["pytest"])
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/ccaudit")
        assert ccaudit_command() == ["/usr/local/bin/ccaudit"]

    def test_it_falls_back_to_this_interpreter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`python -m ccaudit` works wherever the package is importable, which is here."""
        monkeypatch.setattr("sys.argv", ["pytest"])
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert ccaudit_command()[1:] == ["-m", "ccaudit"]

    def test_every_form_is_absolute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The notebook runs from a different directory, under a different interpreter."""
        monkeypatch.setattr("sys.argv", ["pytest"])
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert Path(ccaudit_command()[0]).is_absolute()
