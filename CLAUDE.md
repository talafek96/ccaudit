# clauditor

Local-first cost observability for Claude Code sessions. Answers "where did the money go?"
down to the individual file, tool call, and prompt — including the cost of keeping context
resident across turns.

## Project constitution — ALWAYS IN EFFECT

The project constitution is imported below. **Read it, keep it in context, and comply with
every principle in it for every task in this repository** — code, specs, plans, docs, and
commits alike.

@.specify/memory/constitution.md

Rules for working with the constitution:

- The constitution **supersedes** any other convention, habit, or default you would otherwise apply.
- If a request conflicts with a constitutional principle, say so explicitly before proceeding,
  and propose the compliant alternative.
- Never silently violate a principle for convenience. If a violation is genuinely warranted,
  it must be called out and justified in the plan or PR description.
- Do not edit `.specify/memory/constitution.md` as a side effect of other work. Amendments are a
  deliberate act — use `/speckit-constitution`, bump the version, and update the amendment date.

## Spec-driven development

This repo uses [spec-kit](https://github.com/github/spec-kit). Feature work flows through:

`/speckit-constitution` → `/speckit-specify` → `/speckit-clarify` → `/speckit-plan` →
`/speckit-tasks` → `/speckit-analyze` → `/speckit-implement`

Specs live in `specs/<NNN-feature-name>/`. Do not start implementing a feature that has no spec.

## Domain vocabulary

Cost figures in this project are **API-equivalent cost**, never billed cost. See
`docs/research/prior-art.md` for the full rationale and the four cost components
(cache reads, cache writes, fresh input, output) with their plain-language names.
