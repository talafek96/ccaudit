# Phase 1 — Data Model

Entities, their fields, and the invariants that must hold. Derived from spec Key Entities and
[`docs/cost-model.md`](../../docs/cost-model.md).

**Money is integer micro-dollars everywhere.** Tokens are integers. Floats exist only at the
presentation edge.

---

## Entity overview

```
Session ──1:N── Turn ──1:N── Charge          (what was billed — observed)
   │              │
   │              └──N:M── ResidencySpan ──N:1── ContextItem
   │                             │
   │                             └──1:1── Injection   (why it became resident)
   │
   ├──1:N── Attribution ──N:1── ContextItem  (what we concluded — derived)
   ├──1:N── InvalidationEvent                (why a reload was forced)
   └──1:1── AnalysisResult ──1:1── Claim     (freshness + concurrency)
```

The split between **Charge** (observed) and **Attribution** (derived) is the model's spine. Charges
are facts read from the transcript and never adjusted; attributions are conclusions that must sum
back to them.

---

## Core entities

### Session

One recorded conversation.

| Field | Type | Notes |
|---|---|---|
| `session_id` | text PK | From the transcript |
| `project_path` | text | Source project; distinguishes same-named files across projects (edge case) |
| `transcript_path` | text | Read-only source |
| `started_at`, `ended_at` | timestamp | `ended_at` null while in progress |
| `producing_version` | text | Claude Code version; FR-028 |
| `is_complete` | bool | Derived; drives the provisional label (FR-067) |

### Turn

One exchange. The unit at which cost is observed and residency is evaluated.

| Field | Type | Notes |
|---|---|---|
| `turn_id` | text PK | |
| `session_id` | text FK | |
| `ordinal` | int | Position in session; the timeline x-axis |
| `message_id`, `request_id` | text | **Dedup key** — `UNIQUE(message_id, request_id)` (FR-021) |
| `model` | text | Per-turn; resolves rates *and* the cacheability threshold |
| `cache_ttl` | text nullable | 5m / 1h / unknown → drives write multiplier; null ⇒ confidence downgrade |
| `is_sidechain` | bool | Subagent work; rolls up to parent (FR-009) |
| `parent_turn_id` | text nullable | Subagent rollup target |

### Charge — observed, never adjusted

One row per component per turn. Four components, defined once in `config/components.py`.

| Field | Type | Notes |
|---|---|---|
| `turn_id` | text FK | |
| `component` | enum | `fresh_input` / `cache_write` / `cache_read` / `output` |
| `tokens` | int | Straight from `usage` |
| `cost_micros` | int | tokens × rate × multiplier, from `config/pricing.toml` |

> **Invariant C1.** `Σ Charge.tokens` over a turn's three input components equals that turn's total
> prompt size. `fresh_input` alone is never treated as prompt size (FR-083).

### ContextItem

Anything occupying space and therefore incurring cost. Files are one kind.

| Field | Type | Notes |
|---|---|---|
| `item_id` | text PK | Stable across sessions for aggregation |
| `kind` | enum | `file` / `instruction_file` / `skill` / `tool_schema` / `mcp_schema` / `system_prompt` / `conversation` |
| `identity` | text | Path for files, name for skills/schemas |
| `project_path` | text nullable | Namespaces file identity (edge case: same path, different projects) |
| `category` | enum | docs / source / spec / skill / schema / other — from `config/categories.py` |
| `size_tokens` | int | Measured; basis recorded |

### Injection

One event placing an item into the conversation. Origin of **direct** cost.

| Field | Type | Notes |
|---|---|---|
| `injection_id` | text PK | |
| `turn_id`, `item_id` | FK | |
| `cause` | enum | `tool_result` / `attachment` / `skill_listing` / `deferred_tools_delta` / `at_mention` / `session_start` / `compact_reinjection` |
| `tool_use_id` | text nullable | The three-way join key across spans, events, hooks |
| `size_tokens` | int | |

> A file read, modified, and read again produces **distinct injections** with distinct spans
> (edge case). `cause` matters: `compact_reinjection` is what makes CLAUDE.md re-appear post-
> compaction as a *new* write, not a continuing span.

### ResidencySpan

The interval an item remained available. Origin of **carry** cost.

| Field | Type | Notes |
|---|---|---|
| `span_id` | text PK | |
| `injection_id`, `item_id` | FK | |
| `first_turn`, `last_turn` | int | `last_turn` null while still resident |
| `end_reason` | enum nullable | `evicted` / `invalidated` / `session_end` / `unknown` |

### CacheLane — the cost-model bridge

Per (turn, resident item): which pricing lane the item sat in that turn. This is §5.2 of the cost
model made concrete, and it is what stops sub-threshold content from being mispriced.

| Field | Type | Notes |
|---|---|---|
| `turn_id`, `item_id` | FK | |
| `lane` | enum | `cached` (0.1×) / `uncached` (1× every turn — below threshold) / `loading` (1.25× or 2×) |
| `lane_reason` | enum | `cacheable` / `below_minimum` / `first_load` / `reload_forced` |

> **Invariant L1.** An item is assigned `uncached` only when `item.size_tokens <
> threshold(turn.model)`, with the threshold read from config — never inferred from model ordering
> (it is not monotonic).

### InvalidationEvent — first-class cause

A prefix-tier change that forced content to be re-loaded. Carries cost in its own right so the
reload is charged to the change, not the content (FR-081).

| Field | Type | Notes |
|---|---|---|
| `event_id` | text PK | |
| `turn_id` | FK | |
| `tier` | enum | `tools` / `system` / `messages` |
| `trigger` | enum | `tool_set_changed` / `model_switched` / `instruction_changed` |
| `detail` | text | e.g. the MCP server added — the user-facing explanation |
| `forced_reload_micros` | int | Excess cache-write cost attributed here |

### Attribution — derived, carries its own provenance

| Field | Type | Notes |
|---|---|---|
| `attribution_id` | text PK | |
| `session_id`, `turn_id` | FK | |
| `target_kind` | enum | `item` / `invalidation_event` / `prompt` / `unattributed` |
| `target_id` | text nullable | Null only for `unattributed` |
| `component` | enum | direct / carry / overhead / output |
| `cost_micros` | int | |
| `basis` | enum | `exact` / `measured` / `estimated` (FR-014, research §6) |
| `confidence` | enum | `high` / `medium` / `low` |
| `source_refs` | json | Record identifiers backing this figure — powers `--explain` (FR-015) |

> **Invariant A1 (the product's core promise, SC-001).** For every session:
> `Σ Attribution.cost_micros == Session total cost_micros`, **exact integer equality**, with
> `unattributed` absorbing the residual explicitly. Violation raises (Principle I) — it is a
> show-stopper defect, not a rounding detail.
>
> **Invariant A2.** No `output` component may target an item (FR-005).
>
> **Invariant A3.** Proportional-split remainders are allocated by largest-remainder, never
> dropped — otherwise A1 fails by construction.

---

## Freshness and concurrency

### AnalysisResult

| Field | Type | Notes |
|---|---|---|
| `session_id` | text PK | |
| `fingerprint` | text | `(record_count, last_record_uuid, byte_size)` — coverage identity |
| `covered_through_turn` | int | What the reader is told when the session has since advanced |
| `policy` | text | Splitting policy in effect — results are policy-scoped |
| `producing_version`, `tool_version` | text | Version-spanning comparisons identifiable |
| `computed_at` | timestamp | |

> **Invariant F1.** A result is current only if its `fingerprint` equals the transcript's present
> fingerprint. Otherwise it is recomputed or presented with explicit coverage — never served as
> current (FR-084).
>
> **Invariant F2.** `UNIQUE(session_id, fingerprint, policy)` — re-running over unchanged records
> creates no second entry (FR-094).

### Claim

| Field | Type | Notes |
|---|---|---|
| `session_id`, `fingerprint` | PK | |
| `state` | enum | `queued` / `running` / `done` |
| `claimed_at`, `expires_at` | timestamp | Expiry is the crash-recovery mechanism |
| `pid`, `host` | text | Diagnostics for "who is analysing this" (FR-090) |

> **Invariant K1.** A claim past `expires_at` is reclaimable by any actor without manual cleanup
> (FR-092). No session is ever permanently stuck.
>
> **Invariant K2.** Results are written in a single transaction; a partial computation is never
> readable as complete (FR-093).
>
> **Invariant K3.** A reader waits at most a bounded interval on a live claim, then computes the
> result itself (FR-091). Safe because analysis is a pure function — a duplicate run wastes CPU and
> cannot produce a different answer.

### IngestDiagnostic

| Field | Type | Notes |
|---|---|---|
| `session_id`, `kind`, `count`, `sample` | | Unparseable, unrecognised-version, or anchor-mismatch records — surfaced in the run summary, never silently dropped (FR-027, FR-026) |

---

## State transitions

**Residency:** `injected → resident → (evicted | invalidated | session_end)`. Carry accrues only
while resident (FR-004).

**Lane, per turn:** `loading → cached` (normal), `loading → loading` (forced reload — pair with an
InvalidationEvent), or `uncached → uncached` (below threshold; never transitions).

**Analysis:** `absent → queued → running → done`, with `running → queued` on lease expiry, and
`done → stale` whenever the fingerprint changes.

---

## Validation rules

1. Dedup on `(message_id, request_id)` before any arithmetic (FR-021).
2. Reject a rate lookup for an unknown model rather than defaulting — fail-fast (Principle I).
3. Every `Attribution` requires `basis` and `confidence`; no nullable defaults.
4. A session with zero file activity is valid and produces a result dominated by resident content
   (edge case) — not an error, not an empty state.
5. Subagent turns roll up to the parent exactly once; double-counting at both levels is an
   assertion failure, not a warning.
