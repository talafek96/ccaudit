# Implementation Plan: Per-File Cost Attribution with Carry Cost

**Branch**: `001-per-file-cost-attribution` | **Date**: 2026-08-11 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-per-file-cost-attribution/spec.md`

## Summary

**ccaudit** reads Claude Code's local session transcripts, reconstructs what content was resident
in the conversation at every turn, and attributes each turn's actual token charges to the files
and other context items that caused them — reconciling to the session total with any remainder
shown explicitly.

The technical approach turns on one decision, stated in [`docs/cost-model.md`](../../docs/cost-model.md)
and load-bearing for everything below: **observe, don't predict.** The transcript records what was
actually charged per turn (`input_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`, `output_tokens`, and the model). Cache mechanics — the
model-dependent cacheability minimum, the TTL-dependent write multiplier, the three-tier prefix
invalidation — are used to *explain* an observed number, never to *derive* one. This is what makes
the cache complications tractable rather than fatal.

Everything runs locally from one command: Python via `uv`, SQLite for persistence, a `rich`
terminal surface, and a self-contained HTML report rendered by hand-written SVG so it opens
offline with no network and no bundled charting library.

## Technical Context

**Language/Version**: Python 3.11+ (matches PEP 723 script metadata already used by the vendored
skills; `uv` manages the interpreter — never the OS Python, per `.claude/rules/python.md`)

**Primary Dependencies**: `rich` (terminal rendering) as the only required runtime dependency.
Everything else is standard library: `sqlite3`, `json`, `http.server`, `pathlib`, `base64`,
`tomllib`. Dev-only: `ruff`, `mypy`, `pytest`.

**Storage**: SQLite via stdlib `sqlite3`, WAL mode, single file under a per-user state directory.
Chosen by the user for familiarity; recorded in `HANDOFF.md`. Consequence designed around: the
attribution pass is a stateful sequential walk over a session's turns in application code, with
SQLite as the store and aggregation layer, not as an analytical engine.

**Testing**: `pytest`. Four levels per the constitution: unit (parsers, cost primitives, each
attribution primitive in isolation), component (a pipeline stage end-to-end over fixture data),
golden (fixture sessions with hand-verified expected breakdowns), system (the real CLI over a
transcript corpus, asserting reconciliation and well-formedness).

**Target Platform**: POSIX (macOS primary, Linux supported). The background-spawn path uses
`start_new_session=True`; on Windows the CLI and queue still work, but detached pre-computation
degrades to compute-on-next-invocation.

**Project Type**: Local CLI tool with three presentation surfaces (terminal, self-contained HTML
report, ephemeral local browser UI) plus a Claude Code plugin wrapper.

**Performance Goals**: Single typical session analysed in under 30s (SC-005); full ~25-session
corpus under 5 minutes (SC-006); include/exclude recomputation over 100 stored sessions under 2s
(SC-021); browser UI up in under 5s (SC-025).

**Constraints**: No daemon, no service, no container, no network, no credential (FR-029, FR-030).
User transcripts are read-only (FR-020). The tool's own resident footprint in a Claude Code
session must be under 0.5% of session cost and self-measured (FR-055, FR-056, SC-017). Every
breakdown reconciles to its total with zero tolerance (SC-001).

**Scale/Scope**: Single machine, single user. Observed corpus: ~23 sessions, ~615M input-side
tokens, sessions up to a few thousand records. Design for 10× that without re-architecture.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Gate | Status | Evidence |
|---|---|:--:|---|
| **I. Fail-Fast** | Broken invariants raise; failures carry the offending record | ✅ PASS | Reconciliation mismatch is a raised error, not a silent adjustment. Unparseable records are counted and surfaced (FR-027), never skipped silently. |
| **II. KISS / local-first** | No daemon, no infrastructure; fewest types; no speculative generality | ✅ PASS | One command, stdlib + `rich`. Browser UI is a command that exits, not a service. One charting approach for both HTML surfaces. |
| **III. Design first** | Spec precedes code | ✅ PASS | `spec.md` at 94 FRs / 35 SCs precedes this plan; no implementation exists. |
| **IV. Developer-led quality** | Lint + type-check + tests are the Definition of Done | ✅ PASS | `ruff format`, `ruff check`, `mypy`, `pytest` gate every commit. |
| **V. Test discipline** | Component primary; unit tests fence AI-authored code; **attribution arithmetic gets golden-file tests** | ✅ PASS | Golden fixtures with hand-checked breakdowns are a Phase 1 deliverable, not an afterthought. |
| **VI. Supportability** | Every derived number traceable; `--explain` is a feature | ✅ PASS | `--explain` is in the CLI contract, backed by a provenance record per attribution (FR-015). |
| **VII. Organizational memory** | Repo is the source of truth; decisions inline | ✅ PASS | `docs/cost-model.md` is tracked precisely because `PITFALLS.md` and `docs/research/` are not. |
| **VIII. Effort / flow** | Blockers first; batch by area | ✅ PASS | Phasing puts ingest correctness (the blocker for every number) before presentation. |
| **IX. Central configuration** | One source per registry; derive, don't duplicate | ✅ PASS | `config/` holds pricing, cost-component definitions with their plain-language names, cacheability thresholds, and file-category rules. Labels, columns, and legends are derived from those definitions. |
| **X. Honest numbers** | API-equivalent labelling, share alongside absolute, reconciliation, confidence + basis, plain-language names, reproducibility | ✅ PASS | Enforced structurally: money is integer micro-dollars, every attribution row carries `basis` + `confidence`, and the reconciliation invariant is asserted in code and pinned by golden tests. |
| **Privacy** | Local by default; exports reviewable and redactable; no credentials | ✅ PASS | Redaction mode (FR-043); no network calls anywhere in the runtime. |
| **Scripting standards** | Fail-fast, validate first, idempotent, read-only against user data | ✅ PASS | Ingest is idempotent by fingerprint + dedup key (FR-094); `~/.claude/` opened read-only. |

**No violations. Complexity Tracking section omitted.**

## Project Structure

### Documentation (this feature)

```text
specs/001-per-file-cost-attribution/
├── plan.md              # This file
├── research.md          # Phase 0 output — decisions with rationale and alternatives
├── data-model.md        # Phase 1 output — entities, schema, invariants
├── quickstart.md        # Phase 1 output — runnable validation scenarios
├── contracts/
│   ├── cli.md           # Command surface, exit codes, output modes
│   ├── report-data.md   # The data contract both HTML surfaces render from
│   └── plugin.md        # Claude Code plugin layout and hook contract
├── checklists/
│   └── requirements.md  # Spec quality checklist (already passing)
└── tasks.md             # Phase 2 output — NOT created by /speckit-plan
```

### Source Code (repository root)

```text
src/ccaudit/
├── __main__.py             # `python -m ccaudit`
├── cli.py                  # Argument parsing, zero-argument default, exit codes
├── analyse.py              # The pipeline: parse → dedup → size → timeline → attribute → reconcile
├── money.py                # Integer micro-dollars: rates → cost, largest-remainder, sig figs
├── config/
│   ├── __init__.py         # Loader; the single authoritative registry (Principle IX)
│   ├── pricing.toml        # Per-model: token rates, TTL write multipliers, cacheability minimum
│   ├── components.py       # The four cost components + mandated plain-language names
│   └── categories.py       # File-category rules (docs / source / spec / skill / schema / other)
├── ingest/
│   ├── discover.py         # Locate sessions; compute coverage fingerprints cheaply
│   ├── records.py          # Transcript record types and parsing
│   ├── dedup.py            # (message.id, requestId) deduplication across resume/fork/compact
│   ├── tokens.py           # Token resolution: exact where recorded, image sizing, estimates
│   └── anchors.py          # /context ground-truth tables; reconciliation against them
├── model/
│   ├── residency.py        # Per-turn resident set: injections, spans, evictions
│   ├── lanes.py            # Classify each resident item per turn: cached / uncached / loading
│   ├── attribute.py        # Direct, carry, overhead, output split
│   ├── policy.py           # Carry-splitting policies (proportional default; exclusive alternative)
│   ├── invalidation.py     # Detect prefix-tier changes; charge forced reloads to the cause
│   └── reconcile.py        # Enforce the sum invariant; emit the unattributed remainder
├── store/
│   ├── schema.sql          # Tables, indices, invariant constraints
│   ├── db.py               # Connection, WAL, migrations, transactions
│   └── claims.py           # Freshness fingerprints, claim/lease, crash recovery
├── render/
│   ├── terminal.py         # rich tables, proportion bars; plain text when not a TTY
│   ├── explain.py          # --explain derivation traces
│   ├── report.py           # Self-contained HTML (data inlined)
│   ├── serve.py            # Ephemeral loopback server for the interactive UI
│   ├── assets/             # CSS + vanilla JS, inlined at render time
│   └── charts/             # Hand-written SVG: icicle, treemap, timeline, bars, sparkline
└── plugin/                 # Shipped Claude Code plugin: manifest, command, skill, hooks

tests/
├── unit/                   # Parsers, cost primitives, each attribution primitive in isolation
├── component/              # A pipeline stage end-to-end over fixtures
├── golden/                 # Fixture sessions + hand-verified expected breakdowns
├── system/                 # Real CLI over a corpus; reconciliation and well-formedness
└── fixtures/               # Synthetic transcripts — never real user data
```

**Structure Decision**: Single Python package, four layers in dependency order —
`config` → `ingest` → `model` → `render`, with `store` beside `model`. The layering is the point:
`ingest` produces facts, `model` produces attributions, `render` produces surfaces, and nothing
reaches backward.

Two modules sit outside the layering, at opposite ends of it, and both do so deliberately.
`money.py` is a **leaf**: no imports of its own, depended on by `config` (rates to cost), `model`
(largest-remainder splits), and `render` (significant figures at the presentation edge). Placing it
inside any one layer would force the other two to reach sideways or duplicate it — and duplicated
money arithmetic is precisely how Invariant A1 breaks. `analyse.py` is the **composition root**: it
runs parse → dedup → size → timeline → attribute → reconcile in order. Something has to join the
layers, and putting the join inside any one of them would invert a dependency. It is also the
seam the component tests exercise, since what they test is the composition rather than any single
stage. The three presentation surfaces share one data contract
(`contracts/report-data.md`) and the same SVG chart code, so the interactive UI and the shareable
report differ only in whether data is inlined or fetched — which is what keeps FR-074 (every
figure obtainable from the terminal) cheap to honour instead of a second implementation.

## Constitution Re-Check (post-design)

*Re-evaluated after Phase 1. Only rows where the design changed the assessment are discussed.*

| Principle | Status | What the design added |
|---|:--:|---|
| **I. Fail-Fast** | ✅ PASS | Reconciliation failure gets its **own exit code (3)**, distinct from a data error — it can never be mistaken for a warning. Unknown-model rate lookups raise rather than defaulting. |
| **II. KISS** | ✅ PASS | Design *removed* structure rather than adding it: one renderer serves both HTML surfaces, one data contract serves all three consumers, no charting library, no web framework. The plugin is a wrapper with no second code path. |
| **V. Test discipline** | ✅ PASS | Quickstart Scenario 3 is a **falsifiable test of the premise itself** — if carry cost never reorders the top-10, the product thesis is wrong and we learn it before building the rest. Golden diffs are explicitly red alerts, not rebaselines. |
| **VI. Supportability** | ✅ PASS | `--explain` is backed by a `source_refs` field on every attribution row, making traceability a data-model property rather than a rendering afterthought. |
| **IX. Central configuration** | ✅ PASS | Strengthened by necessity: the cacheability threshold **cannot** be derived from model ordering (it is not monotonic), so the config table is load-bearing rather than a convenience. Plain-language names flow from `config/components.py` into the JSON contract, so no renderer re-types them. |
| **X. Honest numbers** | ✅ PASS | Enforced structurally rather than by review: integer micro-dollars make Invariant A1 checkable by equality; `basis` and `confidence` are non-nullable columns; `unattributed` is a required field in the data contract; `limitations` is required output. |

**No new violations. Complexity Tracking remains empty.**

Two design choices are worth flagging as deliberate, since either could look like over-engineering:

- **`InvalidationEvent` as a first-class entity.** Without it, prefix invalidation misattributes a
  tool-set change's cost to the instruction files it forced to reload — a confidently wrong answer
  to the exact question the tool exists to settle.
- **`CacheLane` per (turn, item).** Three pricing lanes differing by 10–20× cannot be collapsed
  into one pool without mispricing precisely the small instruction files under dispute.

## Complexity Tracking

No constitution violations. Section intentionally empty.
