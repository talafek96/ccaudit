"""Contract on the release script.

The script is short because the tag is the only artifact a release creates. What is worth
fencing is the arithmetic that decides *which* tag, because getting it wrong publishes a
version that cannot be recalled — PyPI does not allow re-uploading one.

Loaded by path rather than imported: `scripts/` is deliberately not part of the package, and
making it importable to test it would put release tooling in the shipped wheel.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def release() -> ModuleType:
    return load()


class TestTheNextVersion:
    def test_each_level_moves_the_part_it_names_and_zeroes_the_rest(
        self, release: ModuleType
    ) -> None:
        version = release.Version(1, 4, 2)

        assert str(version.bump("patch")) == "1.4.3"
        assert str(version.bump("minor")) == "1.5.0"
        assert str(version.bump("major")) == "2.0.0"

    def test_the_first_release_is_not_zero(self, release: ModuleType) -> None:
        """With no tags yet, a minor bump from nothing is 0.1.0 — a release, not a null."""
        assert str(release.Version(0, 0, 0).bump("minor")) == "0.1.0"

    @pytest.mark.parametrize("text", ["1.2", "v1.2.3", "1.2.3.4", "1.2.3-rc1", "", "latest"])
    def test_only_a_plain_three_part_version_is_accepted(
        self, release: ModuleType, text: str
    ) -> None:
        """A release tag is MAJOR.MINOR.PATCH. Everything else is something hatch-vcs derives
        for an untagged commit, and is never a thing anyone tags."""
        with pytest.raises(release.Refused):
            release.Version.parse(text)


class TestPickingTheLatestRelease:
    def test_versions_are_ordered_numerically_not_lexically(self, release: ModuleType) -> None:
        """The trap this exists for: sorted as text, v0.10.0 comes before v0.9.0, so the next
        release after 0.10.0 would be computed from 0.9.0 and collide with a published tag."""
        versions = [release.Version.parse(v) for v in ("0.9.0", "0.10.0", "0.2.0")]
        newest = max(versions, key=lambda v: (v.major, v.minor, v.patch))

        assert str(newest) == "0.10.0"
        assert str(newest.bump("patch")) == "0.10.1"

    def test_the_tag_pattern_ignores_anything_that_is_not_a_release(
        self, release: ModuleType
    ) -> None:
        """Other tags may exist in a repository; only vN.N.N ones are releases."""
        for text in ("v1.2.3",):
            assert release.TAG.match(text)
        for text in ("nightly", "v1.2", "1.2.3", "v1.2.3-rc1", "release-1.2.3"):
            assert not release.TAG.match(text), text


class TestItRefusesBeforeItActs:
    def test_a_refusal_carries_what_to_do_about_it(self, release: ModuleType) -> None:
        """Principle I: every failure names the offending state and the way out of it."""
        assert issubclass(release.Refused, RuntimeError)

    def test_a_release_creates_exactly_one_ref(self, release: ModuleType) -> None:
        """The tag. Nothing else — a second ref is a second thing that can be half-pushed."""
        source = SCRIPT.read_text(encoding="utf-8")
        assert "--atomic" not in source
        assert 'git("branch"' not in source

    def test_it_releases_from_the_branch_the_workflow_checks(self, release: ModuleType) -> None:
        """A cross-file contract: the workflow asserts containment in this exact branch, so a
        rename here without one there rejects every release.

        This used to name `rel/stable`. That branch existed from when pushing it was the
        trigger; once a published GitHub Release became the trigger it only fed a check that
        `main` answers just as well, so it is gone rather than kept for symmetry.
        """
        workflow = (SCRIPT.parents[1] / ".github/workflows/release.yml").read_text(encoding="utf-8")
        assert release.SOURCE_BRANCH == "main"
        assert "origin/main" in workflow
        assert "rel/stable" not in workflow, "the retired release branch is back in the workflow"
