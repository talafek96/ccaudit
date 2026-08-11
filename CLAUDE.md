# CLAUDE.md

Guidance for Claude Code in this repository. The engineering standards live in the
constitution, imported below — read it as part of this file. Keep this wrapper thin.

**clauditor** is a local-first cost observability tool for Claude Code sessions. It answers
"where did the money go?" down to the individual file, tool call, and prompt — including
the cost of keeping context resident across turns.

## Global operation rules

You don't have memory. These two files at repo root do: `HANDOFF.md`, `PITFALLS.md`.
`git log` is history.

You and also subagents shall read `HANDOFF.md` + `PITFALLS.md` on spawn. Regularly update
both at key moments — update `HANDOFF.md` when reaching milestones during your work, and
constantly update `PITFALLS.md` whenever you encounter bugs and resolve them.

**Unit tests fence your code (constitution Principle V):** code you author MUST ship with
unit tests that pin its intended behavior. A unit test that fails is a *breached prior
contract* — realign the code to it; never weaken, rewrite, or delete the test to make it
pass without explicit human sign-off.

**Numbers are the product (constitution Principle X):** every cost figure is
API-equivalent, labeled as such, paired with a share of total, and reconciles to the
session total. A breakdown that does not add up is a show-stopper defect, not a rounding
detail.

## Core Guidelines & Standards — the constitution

The project's engineering standards — fail-fast, simplicity, test discipline, honest
numbers, and the rest — are the constitution, imported here so they are **always in
context**:

@.specify/memory/constitution.md

Rules for working with it:

- The constitution **supersedes** any other convention, habit, or default you would
  otherwise apply.
- If a request conflicts with a principle, say so explicitly before proceeding, and
  propose the compliant alternative.
- Never silently violate a principle for convenience. A genuinely warranted deviation must
  be called out and justified in writing at the point of deviation.
- Do not edit `.specify/memory/constitution.md` as a side effect of other work. Amendments
  are deliberate — use `/speckit-constitution`, bump the version, and update the Sync
  Impact Report.

## Subagents

Delegate only when the subagent gives you something hard to get yourself: parallel work,
isolated context, specialized tools, fresh eyes. A two-file edit is not a delegation
candidate.

Brief every subagent with **HOW** (the steps or constraints), **WHAT** (the deliverable
shape), **WHY** (the reason this matters). The subagent has no session context.

## Spec-driven development

This repo uses [spec-kit](https://github.com/github/spec-kit). Feature work flows through:

`/speckit-constitution` → `/speckit-specify` → `/speckit-clarify` → `/speckit-plan` →
`/speckit-tasks` → `/speckit-analyze` → `/speckit-implement`

Specs live in `specs/<NNN-feature-name>/`. Do not start implementing a feature that has no
spec (constitution Principle III: design first).

## Domain vocabulary — read before touching cost code

Cost figures are **API-equivalent cost**, never billed cost. The four cost components each
have a mandated plain-language name (loading into context / keeping context loaded / your
new typing / what Claude wrote back), and the attribution model splits cost into direct,
carry, overhead, and output. All of it, with the evidence behind it, is in
[`docs/research/prior-art.md`](docs/research/prior-art.md). Read it before writing or
reviewing anything that produces a number.

## Definition of done

```sh
uv run ruff format          # format what you touched
uv run ruff check           # must be clean
uv run mypy                 # must pass (non-strict)
uv run pytest               # must pass
```

Always go through **`uv`**, never the OS Python. Adding a `# noqa` or `# type: ignore`
requires explicit human approval.

## Deeper docs (read on demand)

- Language conventions — [`.claude/rules/python.md`](.claude/rules/python.md)
- Git & commit conventions — [`.claude/rules/git-conventions.md`](.claude/rules/git-conventions.md)
- Prior art, cost model, and known traps — [`docs/research/prior-art.md`](docs/research/prior-art.md)
