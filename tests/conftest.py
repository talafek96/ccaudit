"""Shared fixtures.

Every test that touches persistent state runs against an isolated ``CCAUDIT_HOME`` so a test
run never reads or writes the developer's real state directory, and never reads the real
``~/.claude/`` transcripts (constitution: fixtures are committed and reproducible).
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def ccaudit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``CCAUDIT_HOME`` at a per-test temporary directory."""
    home = tmp_path / "ccaudit-home"
    monkeypatch.setenv("CCAUDIT_HOME", str(home))
    yield home


@pytest.fixture(autouse=True)
def _never_touch_real_claude_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fence the whole suite away from the developer's real transcripts.

    A test that wants transcripts points ``CLAUDE_CONFIG_DIR`` at a fixture tree itself; the
    default is an empty directory so an accidental discovery call finds nothing rather than
    the developer's actual sessions.
    """
    if "CCAUDIT_ALLOW_REAL_CLAUDE_HOME" in os.environ:
        return
    empty = tmp_path / "claude-config"
    empty.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty))
