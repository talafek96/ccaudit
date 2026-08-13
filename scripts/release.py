#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Cut a release: tag a commit on `rel/stable` and push, which is what triggers publishing.

    uv run scripts/release.py patch          # 0.2.0 -> 0.2.1
    uv run scripts/release.py minor          # 0.2.1 -> 0.3.0
    uv run scripts/release.py major          # 0.3.0 -> 1.0.0
    uv run scripts/release.py 1.4.2          # an exact version
    uv run scripts/release.py minor --dry-run

**There is no version to edit.** `hatch-vcs` derives it from the tag at build time, so the
tag *is* the release. That is the whole reason this script is short: it has one artifact to
create, not two to keep in agreement.

Everything is checked before anything is pushed, and the branch and the tag go together in
one atomic push — a `rel/stable` that moved without its tag would be a release from nowhere,
and a tag without the branch would be one the CD workflow refuses.

**Pushing does not publish.** Publishing is triggered by a GitHub *Release*, so this leaves a
**draft** one for you to read and press the button on. That is the deliberate act, and it is
also where the notes get written. Without `gh` installed the script prints the URL to create
it by hand; nothing about the release depends on the tool being present.
"""

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

RELEASE_BRANCH = "rel/stable"
SOURCE_BRANCH = "main"
REMOTE = "origin"

# Releases are plain `MAJOR.MINOR.PATCH`. Anything else — a dev build, a local version — is
# something hatch-vcs derives for an untagged commit and is never a thing anyone tags.
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
TAG = re.compile(r"^v(\d+\.\d+\.\d+)$")


class Refused(RuntimeError):
    """A precondition failed. Carries what to do about it."""


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> "Version":
        found = VERSION.match(text)
        if not found:
            raise Refused(f"{text!r} is not a MAJOR.MINOR.PATCH version")
        return cls(*(int(part) for part in found.groups()))

    def bump(self, level: str) -> "Version":
        if level == "major":
            return Version(self.major + 1, 0, 0)
        if level == "minor":
            return Version(self.major, self.minor + 1, 0)
        return Version(self.major, self.minor, self.patch + 1)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False, cwd=repo_root()
    )
    if check and result.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {result.stderr.strip() or 'no detail'}")
    return result.stdout.strip()


def repo_root() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise Refused("not inside a git repository")
    return result.stdout.strip()


def latest_version() -> Version | None:
    """The highest released version, from the tags themselves.

    Read from the tags rather than from a file, because the tags are what the build reads.
    Sorted numerically — `git tag` sorts lexically, which puts v0.10.0 before v0.9.0.
    """
    versions = [
        Version.parse(found.group(1))
        for line in git("tag", "--list", "v*").splitlines()
        if (found := TAG.match(line.strip()))
    ]
    return max(versions, key=lambda v: (v.major, v.minor, v.patch), default=None)


def check_preconditions() -> str:
    """Refuse before doing anything, and return the commit the release will be cut from."""
    if git("status", "--porcelain"):
        raise Refused(
            "the working tree has uncommitted changes. A release is built from a commit, so "
            "what is on disk right now would not be what ships. Commit or stash first."
        )

    git("fetch", "--quiet", REMOTE, "--tags")

    local = git("rev-parse", SOURCE_BRANCH)
    remote = git("rev-parse", f"{REMOTE}/{SOURCE_BRANCH}")
    if local != remote:
        raise Refused(
            f"{SOURCE_BRANCH} and {REMOTE}/{SOURCE_BRANCH} point at different commits. Release "
            f"from what is pushed, not from what is local: push or pull {SOURCE_BRANCH} first."
        )

    # `rel/stable` only ever fast-forwards from `main`. Anything else means someone committed
    # to the release branch directly, and the release would contain code that never sat on
    # main or passed its CI.
    if git("rev-parse", "--verify", "--quiet", f"{REMOTE}/{RELEASE_BRANCH}", check=False):
        behind = git("rev-list", "--count", f"{local}..{REMOTE}/{RELEASE_BRANCH}")
        if behind != "0":
            raise Refused(
                f"{REMOTE}/{RELEASE_BRANCH} has {behind} commit(s) that {SOURCE_BRANCH} does "
                f"not. It must only ever fast-forward from {SOURCE_BRANCH}; reconcile them "
                f"before releasing."
            )
    return local


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="release.py",
        description="Tag a release on rel/stable and push it, which starts the publish.",
    )
    parser.add_argument(
        "level",
        help="major, minor, patch, or an exact MAJOR.MINOR.PATCH version.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Say exactly what would happen and change nothing, locally or remotely.",
    )
    args = parser.parse_args()

    try:
        commit = check_preconditions()
        current = latest_version()
        if args.level in ("major", "minor", "patch"):
            following = (current or Version(0, 0, 0)).bump(args.level)
        else:
            following = Version.parse(args.level)
            if current and (following.major, following.minor, following.patch) <= (
                current.major,
                current.minor,
                current.patch,
            ):
                raise Refused(f"{following} is not after the current release {current}")

        tag = f"v{following}"
        if git("tag", "--list", tag):
            raise Refused(f"{tag} already exists. A published version is never re-cut.")

        subject = git("log", "-1", "--format=%s", commit)
        print(f"  from      {SOURCE_BRANCH} at {commit[:9]} — {subject}")
        print(f"  version   {current or 'none yet'} -> {following}")
        print(f"  tag       {tag}")
        print(f"  push      {REMOTE} {RELEASE_BRANCH} + {tag}  (atomic)")

        if args.dry_run:
            print("\n  --dry-run: nothing was changed.")
            return 0

        # Local first, so a failure leaves nothing published to undo.
        git("branch", "--force", RELEASE_BRANCH, commit)
        git("tag", "--annotate", tag, "--message", f"ccaudit {following}", commit)
        try:
            # Atomic: the branch and its tag land together or not at all. Separately, a
            # half-push leaves either a release the workflow refuses or a branch claiming to
            # be stable that nothing verified.
            git(
                "push",
                "--atomic",
                REMOTE,
                f"{RELEASE_BRANCH}:{RELEASE_BRANCH}",
                f"refs/tags/{tag}",
            )
        except Refused:
            # Roll the local tag back so a retry is not blocked by wreckage from this attempt.
            git("tag", "--delete", tag, check=False)
            raise

        print(f"\n  pushed. {_draft_release(tag, following)}")
        return 0
    except Refused as refusal:
        print(f"release refused: {refusal}", file=sys.stderr)
        return 1


def _slug() -> str:
    remote = git("remote", "get-url", REMOTE, check=False)
    return re.sub(r"^(git@github\.com:|https://github\.com/)|\.git$", "", remote)


def _draft_release(tag: str, version: Version) -> str:
    """Leave a draft GitHub Release, which is what actually starts the publish when opened.

    A draft on purpose. Creating it published would make this script the thing that ships to
    PyPI, and the whole point of the Release trigger is that shipping is a separate, visible
    decision — one a person makes after reading what is in it.
    """
    slug = _slug()
    if not shutil.which("gh"):
        target = f"https://github.com/{slug}/releases/new?tag={tag}" if slug else "GitHub"
        return f"Publish a Release for {tag} to ship it: {target}"

    drafted = subprocess.run(
        [
            "gh",
            "release",
            "create",
            tag,
            "--draft",
            "--title",
            f"ccaudit {version}",
            # Notes from the commits since the last release, so the draft opens with something
            # to edit rather than an empty box.
            "--generate-notes",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root(),
    )
    if drafted.returncode != 0:
        detail = drafted.stderr.strip() or "no detail"
        return f"tag pushed, but drafting the Release failed ({detail}). Create it by hand."
    return f"Draft Release ready — review it and press Publish to ship:\n  {drafted.stdout.strip()}"


if __name__ == "__main__":
    raise SystemExit(main())
