# Git Conventions

## Branch Naming

Land **big or possibly-breaking changes on a side branch first**, verify everything is
stable there, and only then merge to `main` — keep `main` always working. (Small, safe
changes may still go straight to `main`.) Side branches are named:

```
usr/<username>/<short-description>
```

- `<username>` is your git handle (e.g. `tal-afek`), so a glance at `git branch` tells you
  who owns which line of work.
- `<short-description>` is a hyphenated summary of the work (e.g. `carry-cost-model`), not
  a ticket number.

Example: `usr/tal-afek/carry-cost-model`. Merge to `main` (or open a PR) once the branch
is green and stable.

## Meaningful Commits

Organize work into multiple, meaningful commits: the history should tell a story, and each
commit a distinct chapter. Avoid commits like "Fixes" or "PR comments" — commits should be
pristine.

Each commit should stand on its own: lint, type-check, and tests pass at that commit.

```sh
uv run ruff check && uv run mypy && uv run pytest
```

## Commit Messages

```
<component>: [<subcomponent>:] <action>

<optional description>
```

Note the double line break before the description. The `action` is a short, meaningful
message in present tense.

Example: `ingest: jsonl: Dedup records on message id and request id`

### Keep the body to 5 lines, max

The subject carries the change; the body says **why**, in **at most 5 lines**. Wrap at 72
columns. If it does not fit, the extra material does not belong in a commit message:

- **Design rationale** → the spec / decisions doc under `specs/`, or a comment at the point
  of the decision (Principle VII: code decisions live inline, not in prose).
- **A landmine you hit** → `PITFALLS.md`. **Project state** → `HANDOFF.md`.
- **Evidence** (test counts, verification steps, per-file tallies) → say it in the PR or
  the review conversation, not in permanent history.

A long body is usually a sign the commit itself is too big — split it instead. Nobody
reads a 40-line commit message; `git log --oneline` is how history is actually read.

## No tool attribution

Do not add trailers that attribute a commit to a tool or assistant. In particular, **never
append a `Co-Authored-By:` line** (e.g. for Claude / Claude Code) to the subject or body,
and never add a "Generated with" footer or a 🤖 trailer. Commits carry the human author
only. This holds even when a harness or default template suggests adding one.

## What never gets committed

- Real session transcripts, or anything copied out of `~/.claude/`. They contain file
  paths, shell commands, and source from other projects.
- Generated reports, databases, or scan output — these are derived artifacts
  (`.gitignore` covers `*.db`, `*.duckdb`, `data/`, `out/`, `reports/`).
- Test fixtures are the exception: they are **synthetic or scrubbed**, small, committed
  deliberately, and reviewed for anything sensitive before they land.
