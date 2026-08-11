"""System test — the optional marimo notebook surface.

The notebook is an *additional* local surface, never a replacement for the shareable report:
that has to be one self-contained file that opens offline with no tooling (FR-032), and a
notebook is not that. These tests pin the two properties that keep it honest — that it costs
this project no dependency, and that it computes no figure of its own.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from ccaudit.cli import EXIT_OK, main
from ccaudit.notebook import NOTEBOOK_SOURCE, write_notebook

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
        assert '"ccaudit", *args, "--json"' in notebook

    def test_it_labels_its_figures_as_estimates(self, notebook: str) -> None:
        assert "API-equivalent" in notebook
        assert "not billed amounts" in notebook

    def test_it_shows_the_unattributed_remainder(self, notebook: str) -> None:
        """The remainder is not optional on any surface (FR-012)."""
        assert "couldn't attribute" in notebook
        assert "unattributed_micros" in notebook

    def test_it_reproduces_the_limitations(self, notebook: str) -> None:
        assert "limitations" in notebook


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

    def test_the_written_file_matches_the_source(self, tmp_path: Path) -> None:
        assert write_notebook(tmp_path / "nb.py").read_text(encoding="utf-8") == NOTEBOOK_SOURCE
