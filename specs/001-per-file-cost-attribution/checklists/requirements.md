# Specification Quality Checklist: Per-File Cost Attribution with Carry Cost

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-08-11
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

Validation performed 2026-08-11, single iteration, all items passing.

**Storage technology deliberately unnamed.** The source description proposed SQLite or DuckDB
and a self-contained HTML report; the spec states the observable properties instead (results
persist locally between runs, FR-044; the report is a single self-contained file that opens
without additional software or network access, FR-032). The choice belongs in `/speckit-plan`.
SQLite has since been chosen and is recorded in `HANDOFF.md` under locked decisions; the spec
is intentionally left unchanged, since nothing observable to a user depends on it.

**FR-055 rewritten for plain language** (2026-08-11). The original wording — "MUST NOT add
persistent resident content to the user's sessions" — failed the "written for non-technical
stakeholders" bar in practice: a reader asked what it meant. Restated in terms of the observable
effect ("installing the integration must not increase the size of the user's conversations")
rather than the mechanism. Worth noting as a pattern: a requirement a reader has to ask about is
a defective requirement, regardless of whether it is technically precise.

**Session selection, in-progress analysis, and presentation surfaces added**
(FR-059 to FR-075, SC-020 to SC-025, Stories 6 and 7). Re-validated: still zero clarification
markers. FR-074 is the load-bearing one among the presentation requirements — every figure in
the browser interface must also be reachable from the terminal, which prevents the richer
surface from quietly becoming mandatory.

**Invocation surfaces added after initial validation** (User Story 3, FR-048 to FR-058,
SC-015 to SC-019). Re-validated: still zero clarification markers, and the requirements remain
observable rather than prescriptive — they state that the tool must be invocable from within a
session and installable through the mechanism Claude Code provides, without naming the plugin
file layout, which belongs in the plan. FR-055 (no persistent resident content) and FR-056
(measure its own footprint) are the load-bearing ones: they encode the constraint that a
cost-observability tool must not inflate the context it measures.

**Zero [NEEDS CLARIFICATION] markers, by design not by omission.** Every open question in the
source description had a defensible default backed by the research in `docs/research/`, and each
is recorded in Assumptions with its rationale rather than deferred. The two that came closest to
warranting a marker:

1. *v1 scope.* Revised 2026-08-11 after user input: multi-session accumulation (Story 6) and
   in-progress analysis (Story 7) moved **into** v1, since the manager-facing argument needs
   accumulation and the working-loop value needs live insight. Stories 8 and 9 remain deferred,
   now on a sharper rationale — they are *interpretations* of the numbers (a delta, a
   counterfactual) rather than the numbers themselves.
2. *Path redaction default.* Resolved to off-by-default (FR-043 available on request), on the
   grounds that the dominant use is local analysis where paths are the most useful identifier.

Both are cheap to reverse and are better interrogated in `/speckit-clarify` against a complete
spec than guessed at in isolation.

**Domain vocabulary retained deliberately.** Terms such as *compaction*, *residency*, *subagent*,
and *carry cost* appear throughout. They name things the tool measures and cannot be paraphrased
away without losing precision. Each is defined in Key Entities or on first use, and FR-016
requires plain-language naming to be carried through to every user-facing surface — the
constraint applies to the product, and the spec holds it to that standard rather than pre-empting
it.
