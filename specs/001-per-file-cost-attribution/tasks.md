# Tasks: Per-File Cost Attribution with Carry Cost

**Input**: Design documents from `/specs/001-per-file-cost-attribution/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`, `quickstart.md`

**Tests**: **Included and mandatory.** Not by default — by constitution. Principle V requires unit
tests as the contract on AI-authored code, and *"attribution arithmetic gets golden-file tests"*.
Test tasks below are therefore first-class deliverables, not an optional lane.

**Organization**: Grouped by user story. v1 scope is **Stories 1–7** (spec → Assumptions → Scope).
Stories 8 and 9 are recorded at the end as deferred, not scheduled.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on incomplete work)
- **[Story]**: `[US1]`…`[US7]` — Setup / Foundational / Polish tasks carry no story label
- Every task names its exact file path

## Path Conventions

Single Python package per `plan.md`: `src/claude_cost_tracker/` with layers `config → ingest → model → render`
(`store` beside `model`); tests under `tests/{unit,component,golden,system,fixtures}/`.

**Definition of Done for every task** (constitution IV, `quickstart.md`):

```sh
uv run ruff format && uv run ruff check && uv run mypy && uv run pytest
```

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Make the package layout and the test tree real. `pyproject.toml`, `uv.lock`, CI, ruff,
and mypy already exist — this phase extends them, it does not recreate them.

- [X] T001 Add `rich` as the single runtime dependency and declare the `ccost` console entry point in `pyproject.toml`
- [X] T002 [P] Create the package skeleton — `src/claude_cost_tracker/__main__.py` plus empty `__init__.py` for `src/claude_cost_tracker/config/`, `src/claude_cost_tracker/ingest/`, `src/claude_cost_tracker/model/`, `src/claude_cost_tracker/store/`, `src/claude_cost_tracker/render/`
- [X] T003 [P] Create the test tree — `tests/unit/`, `tests/component/`, `tests/golden/`, `tests/system/`, `tests/fixtures/`, with a shared `tests/conftest.py` providing an isolated `CCOST_HOME` tmp fixture
- [X] T004 [P] Register `golden` and `system` pytest markers and include the new packages in `[tool.mypy]`/`[tool.ruff]` scope in `pyproject.toml`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The central config registry, the money primitive, the store, and the ingest layer that
turns transcripts into facts. Every user story consumes these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Central configuration (Principle IX)

- [X] T005 [P] Create `src/claude_cost_tracker/config/pricing.toml` — per-model input/output/cache rates, TTL write multipliers (1.25× at 5m, 2× at 1h), and the **non-monotonic** cacheability minimums (Opus 5 = 512, Opus 4.8/Sonnet 5/Sonnet 4.6 = 1024, Opus 4.7 = 2048, Opus 4.6/4.5/Haiku 4.5 = 4096)
- [X] T006 [P] Create `src/claude_cost_tracker/config/components.py` — the four cost components as the single authoritative registry: `id`, `technical_name`, mandated `plain_name` ("loading into context" / "keeping context loaded" / "your new typing" / "what Claude wrote back"), and `description` (FR-016)
- [X] T007 [P] Create `src/claude_cost_tracker/config/categories.py` — file-category rules mapping a path to `docs | source | spec | skill | schema | other`
- [X] T008 Create `src/claude_cost_tracker/config/__init__.py` — the loader: parse the table once, resolve it `$CCOST_PRICING` → `$CCOST_HOME/pricing.toml` → bundled seed (FR-099), and **raise** on an unknown model or a missing threshold rather than defaulting (Principle I, research §7)
- [X] T008a Create `src/claude_cost_tracker/config/refresh.py` — `ccost pricing refresh`: fetch a public rate table, **merge** it onto the current one, and write to `$CCOST_HOME` so the result survives upgrades (FR-099, FR-100). Preserves hand-verified thresholds, keeps models the source omits, and reports multiplier divergence rather than applying it (FR-101, FR-102)
- [X] T008b Implement the `pricing` command in `src/claude_cost_tracker/cli.py` — `pricing show` (which table, which rates, how old) and `pricing refresh [--source-url URL | --from FILE] [--dry-run]`, printing the full change report
- [X] T009 [P] Unit-test the config registry in `tests/unit/test_config.py` — unknown model raises, thresholds are read per model and never derived from ordering, plain names have exactly one definition site
- [X] T009a [P] Unit-test the refresh in `tests/unit/test_pricing_refresh.py` — a new model arrives without a threshold and raises on use; existing thresholds and omitted models survive; an empty source refuses to overwrite a working table. No test in this file touches the network
- [X] T010 Create `src/claude_cost_tracker/money.py` — integer micro-dollars: `cost_micros(tokens, rate, multiplier)`, largest-remainder allocation across a resident set, and confidence-driven significant-figure formatting for the presentation edge (research §8, FR-095)
- [X] T011 [P] Unit-test money primitives in `tests/unit/test_money.py` — largest-remainder slices sum **exactly** to the pool for adversarial weight vectors; no float appears in a stored or compared value

### Store

- [X] T012 Create `src/claude_cost_tracker/store/schema.sql` — tables for Session, Turn, Charge, ContextItem, Injection, ResidencySpan, CacheLane, InvalidationEvent, Attribution, AnalysisResult, Claim, IngestDiagnostic, with `UNIQUE(message_id, request_id)` and `UNIQUE(session_id, fingerprint, policy)` per data-model invariants F2/K1
- [X] T013 Create `src/claude_cost_tracker/store/db.py` — connection with WAL, schema creation on demand under `CCOST_HOME`, migration hook, and a single-transaction write context (invariant K2)
- [X] T014 [P] Unit-test the store in `tests/unit/test_store_db.py` — state directory created on first use with no setup step (FR-050), dedup and result uniqueness constraints enforced, a failed transaction leaves nothing readable

### Ingest — facts, never conclusions

- [X] T015 Create `src/claude_cost_tracker/ingest/records.py` — transcript record types and parsing, the `usage` block (all three input measures, FR-083), per-turn `model` and `cache_ttl`, `producing_version` stamp (FR-028), `is_sidechain`/`parent_turn_id`, and `compactMetadata` (FR-025); unparseable records are counted and carried, never skipped silently (FR-027)
- [X] T016 [P] Create `src/claude_cost_tracker/ingest/dedup.py` — deduplicate on `(message.id, requestId)` across resume, fork, and compaction before any arithmetic (FR-021, PITFALLS)
- [X] T017 [P] Create `src/claude_cost_tracker/ingest/discover.py` — locate sessions under `~/.claude/` (honouring `CLAUDE_CONFIG_DIR`) read-only, and compute the coverage fingerprint `(record_count, last_record_uuid, byte_size)` without a full parse (research §3, FR-085)
- [X] T018 [P] Create `src/claude_cost_tracker/ingest/tokens.py` — the exact → measured → declared ladder, recording the tier as `basis`; image tokens from decoded PNG/JPEG/WebP header dimensions via the published area formula capped at the per-image maximum. **`chars // 4` is never applied to images** and is marked estimated wherever used at all (research §6)
- [X] T019 [P] Create `src/claude_cost_tracker/ingest/anchors.py` — parse `/context` ground-truth tables and reconcile computed totals against them, reporting disagreement rather than adjusting either side (FR-026)
- [X] T020 Persist and surface `IngestDiagnostic` rows from `src/claude_cost_tracker/ingest/records.py` through `src/claude_cost_tracker/store/db.py` — unparseable counts, unrecognised versions, anchor mismatches, each with a sample record identifier
- [X] T021 [P] Unit-test record parsing in `tests/unit/test_records.py` — all three input measures summed for prompt size, version stamp captured, malformed record counted not dropped
- [X] T022 [P] Unit-test dedup in `tests/unit/test_dedup.py` — a resumed and a forked session counted exactly once; re-ingest does not double any figure
- [X] T023 [P] Unit-test discovery and fingerprinting in `tests/unit/test_discover.py` — fingerprint changes when the session advances, is stable when it does not, and no write ever touches `~/.claude/` (FR-020)
- [X] T024 [P] Unit-test token resolution in `tests/unit/test_tokens.py` — an image sized from its header, never from character count; the basis tier recorded on every quantity
- [X] T025 [P] Unit-test anchor reconciliation in `tests/unit/test_anchors.py` — a deliberate mismatch is reported, not silently absorbed

### Fixtures and CLI skeleton

- [X] T026 Create `tests/fixtures/builder.py` — a synthetic transcript builder (turns, usage blocks, tool results, injections, models, TTLs, compaction, sidechains). **Synthetic only; never real user transcripts** (git-conventions)
- [X] T027 Create the baseline fixture session under `tests/fixtures/sessions/baseline/` — a small multi-turn session with file reads, resident instruction content, and a known token profile
- [X] T028 Component-test the ingest stage end-to-end over fixtures in `tests/component/test_ingest_pipeline.py` — transcript in, deduplicated facts and diagnostics out, idempotent across repeat runs (FR-094)
- [X] T029 Create `src/claude_cost_tracker/cli.py` and wire `src/claude_cost_tracker/__main__.py` — argument parsing for the command table in `contracts/cli.md`, and the exit-code contract (`0` success, `1` usage, `2` no sessions, `3` breakdown does not add up, `4` data error, `130` interrupted)
- [X] T030 Configure logging in `src/claude_cost_tracker/cli.py` — the constitution's four levels, with a file target under `CCOST_HOME` so hook-path failures log rather than surface into a user's session (FR-054)
- [X] T031 [P] Unit-test the CLI surface in `tests/unit/test_cli_exit_codes.py` — each exit code reachable and distinct; exit `3` is never reachable from an ordinary warning path

**Checkpoint**: Facts can be extracted from a transcript, stored, and re-extracted idempotently.

---

## Phase 3: User Story 1 — Find out where a session's money went (Priority: P1) 🎯 MVP

**Goal**: A ranked per-item breakdown of one session in which every dollar is accounted for,
including an explicit unattributed line, reconciling to the session total by exact integer equality.

**Independent Test**: Run against a single recorded session; verify per-file, per-folder, and
per-category breakdowns each sum to the session total, the unattributed remainder is shown
explicitly, and repeat runs are byte-identical (SC-001, SC-002, SC-009).

- [X] T032 [US1] Create `src/claude_cost_tracker/model/residency.py` — the per-turn resident set: injections in, spans out, eviction and compaction survival applied so carry stops when content leaves (FR-003, FR-004)
- [X] T033 [P] [US1] Create `src/claude_cost_tracker/model/policy.py` — the proportional (default) and exclusive carry-splitting policies over integer weights, using largest-remainder allocation from `src/claude_cost_tracker/money.py` (FR-006, invariant A3)
- [X] T034 [US1] Create `src/claude_cost_tracker/model/attribute.py` — split each turn's observed charges into direct, carry, overhead, and output; output targets the exchange and **never** an item (invariant A2, FR-005)
- [X] T035 [US1] Roll subagent turns up to the parent exchange exactly once in `src/claude_cost_tracker/model/attribute.py`, asserting rather than warning on a double count (FR-009, data-model validation rule 5)
- [X] T036 [US1] Create `src/claude_cost_tracker/model/reconcile.py` — enforce `Σ attributions + unattributed == session total` by integer equality, emit the remainder as its own explicit entry, and raise on violation (invariant A1, FR-012, FR-013)
- [X] T037 [US1] Persist `AnalysisResult` and `Attribution` rows in one transaction via `src/claude_cost_tracker/store/db.py`, keyed `(session_id, fingerprint, policy)` so a repeat run creates no second entry (FR-047, FR-094)
- [X] T038 [US1] Create `src/claude_cost_tracker/render/data.py` — the report-data envelope from `contracts/report-data.md`: `scope`, `totals` (with `uncertainty_notes`), `components` sourced from `config/components.py`, and `items`. One contract, three consumers
- [X] T039 [US1] Create `src/claude_cost_tracker/render/terminal.py` — ranked table with proportion bars, every absolute paired with its share, every figure labelled an **API-equivalent cost estimate**, the unattributed line always present, and plain-text degradation when not a TTY (FR-010, FR-011, FR-033, FR-070, FR-071)
- [X] T040 [US1] Implement `analyse --session` and `--json` in `src/claude_cost_tracker/cli.py`, returning exit `3` when reconciliation fails rather than printing the numbers
- [X] T041 [P] [US1] Unit-test residency in `tests/unit/test_residency.py` — a span ends on eviction, on invalidation, and at session end; carry accrues only while resident
- [X] T042 [P] [US1] Unit-test splitting policies in `tests/unit/test_policy.py` — both policies conserve the pool exactly; policy choice changes per-item figures and never the total
- [X] T043 [P] [US1] Unit-test attribution in `tests/unit/test_attribute.py` — output never targets an item; subagent work counted once
- [X] T044 [P] [US1] Unit-test reconciliation in `tests/unit/test_reconcile.py` — a deliberately corrupted attribution raises; the remainder is never distributed across items
- [X] T045 [P] [US1] Unit-test the data contract in `tests/unit/test_report_data.py` — `attributed + unattributed == cost_micros` exactly; `unattributed_share` and `uncertainty_notes` are always present
- [X] T046 [US1] Create the golden fixture and its **hand-verified** expected breakdown under `tests/golden/fixtures/session_basic/` — every figure checked by hand and the derivation recorded alongside it
- [X] T047 [US1] Golden-test the attribution arithmetic in `tests/golden/test_attribution_basic.py` — a diff here is a red alert, never a rebaseline (constitution V)
- [X] T048 [US1] Component-test the analyse stage in `tests/component/test_analyse_pipeline.py` — fixture transcript to reconciled breakdown in one process, per-file, per-folder, and per-category each summing to the total
- [X] T049 [P] [US1] Determinism test in `tests/component/test_determinism.py` — the same input yields byte-identical figures across repeated runs (FR-017, SC-009)

**Checkpoint**: US1 is independently demonstrable — one session in, a breakdown that adds up out.

---

## Phase 4: User Story 2 — Understand *why* a file is expensive (Priority: P1)

**Goal**: Separate "read forty times" from "read once and carried fifty-eight turns" — two problems
with opposite remedies — and make any figure traceable to the records that produced it.

**Independent Test**: Two fixture sessions with equal total cost for a file but opposite cause
profiles; verify the direct/carry splits differ measurably and the derivation of each is printable
without rerunning the analysis (SC-008, SC-010).

- [X] T050 [US2] Extend `src/claude_cost_tracker/model/residency.py` with per-item cause metrics — load count, turns resident, and `end_reason` per span (FR-008, FR-035)
- [X] T051 [US2] Add `source_refs` provenance to every attribution row in `src/claude_cost_tracker/model/attribute.py` — the record identifiers, the formula, and the inputs behind the figure (FR-015, Principle VI)
- [X] T052 [US2] Create `src/claude_cost_tracker/render/explain.py` — the derivation trace: component, formula, inputs, policy in effect, `basis`, `confidence`, and source record identifiers
- [X] T053 [US2] Implement the `explain` command in `src/claude_cost_tracker/cli.py` (`explain <figure-id>`, `--explain FIGURE`)
- [X] T054 [US2] Add grouping aggregation queries to `src/claude_cost_tracker/store/db.py` — by file, folder (at every level of the hierarchy), extension, category, and item, each reconciling to the total (FR-007)
- [X] T055 [US2] Implement `--by`, `--sort`, and `--top` in `src/claude_cost_tracker/cli.py` and `src/claude_cost_tracker/render/terminal.py`, with the omitted remainder still shown as its own line
- [X] T056 [US2] Populate `reads`, `turns_resident`, and the direct/carry split per item in `src/claude_cost_tracker/render/data.py`
- [X] T057 [US2] Create the cause-profile golden fixture under `tests/golden/fixtures/session_cause_profiles/` — two files, equal total, opposite profiles, expected breakdown hand-verified
- [X] T058 [US2] Golden-test cause attribution in `tests/golden/test_cause_profiles.py` — the two files' direct/carry splits differ measurably
- [X] T059 [P] [US2] Unit-test grouping in `tests/unit/test_grouping.py` — every grouping level sums to the session total; no bucket silently absorbs the remainder
- [X] T060 [P] [US2] Unit-test explain output in `tests/unit/test_explain.py` — a skeptic can recompute the figure from the trace alone
- [X] T061 [US2] System-test the falsifiable premise in `tests/system/test_carry_reorders_ranking.py` — ranking by total attributed cost differs materially from ranking by read count on at least one real session (SC-010, quickstart Scenario 3). **If this fails, the product thesis is wrong and we stop and say so**

**Checkpoint**: US1 and US2 both work independently. The premise has been tested, not assumed.

---

## Phase 5: User Story 3 — Run it without ceremony (Priority: P1)

**Goal**: One command, no arguments, no setup, no account. Then the same answer from inside Claude
Code, and optional automatic capture that never blocks the user.

**Independent Test**: On a machine where the tool has never run, execute it with no arguments in a
project directory and get a correct breakdown of the most recent session — no configuration, no
credential, no prior step (SC-011, SC-015).

- [X] T062 [US3] Implement the zero-argument default in `src/claude_cost_tracker/cli.py` and `src/claude_cost_tracker/ingest/discover.py` — resolve the current working directory's project and its most recent session (FR-048)
- [X] T063 [US3] Create the state directory on demand under `CCOST_HOME` in `src/claude_cost_tracker/store/db.py` — no config file, no first-run wizard (FR-050)
- [X] T064 [US3] Create `src/claude_cost_tracker/store/claims.py` — claim per `(session_id, fingerprint)` with `state`, `expires_at`, `pid`, `host`; taken by a single atomic statement that also reclaims expired claims (invariants K1–K3, FR-089, FR-092)
- [X] T065 [US3] Implement the internal `_enqueue` command in `src/claude_cost_tracker/cli.py` — append a queue entry and return, targeting under 50 ms, never analysing inline (PITFALLS: `SessionEnd` cannot raise its own budget)
- [X] T066 [US3] Spawn the detached worker from `_enqueue` in `src/claude_cost_tracker/cli.py` with `start_new_session=True` and stdio redirected away from the parent; a failed spawn degrades to the queue entry, losing nothing (research §5, FR-088)
- [X] T067 [US3] Implement `--wait SECONDS` in `src/claude_cost_tracker/cli.py` — bound the wait on a live claim, then compute the result locally rather than block (FR-091, SC-035)
- [X] T068 [P] [US3] Create `src/claude_cost_tracker/plugin/.claude-plugin/plugin.json` — name, description, version
- [X] T069 [P] [US3] Create `src/claude_cost_tracker/plugin/commands/audit.md` — `/ccost:audit`, analysing the current session including while in progress; costs nothing until typed (FR-051, FR-055)
- [X] T070 [P] [US3] Create `src/claude_cost_tracker/plugin/skills/ccost/SKILL.md` — model-invocable so a natural-language cost question is answered from measured data; shells out to the CLI and reimplements nothing (FR-052)
- [X] T071 [P] [US3] Create `src/claude_cost_tracker/plugin/hooks/hooks.json` — opt-in `SessionEnd` hook invoking `ccost _enqueue` (FR-053, FR-054)
- [X] T072 [US3] Implement self-footprint measurement in `src/claude_cost_tracker/model/attribute.py` and `src/claude_cost_tracker/render/terminal.py` — report the tool's own always-resident contribution as its own figure (FR-056, SC-017)
- [X] T073 [P] [US3] Unit-test claims in `tests/unit/test_claims.py` — an expired claim is reclaimable with no manual cleanup; a partial result is never readable as complete
- [X] T074 [US3] Component-test the capture path in `tests/component/test_enqueue_worker.py` — `_enqueue` returns fast, the worker completes the analysis, two simultaneous analyses leave exactly one stored result with identical figures (SC-033, SC-034)
- [X] T075 [US3] System-test first-run ceremony in `tests/system/test_zero_argument_run.py` — clean state directory, no arguments, correct breakdown, no configuration step

**Checkpoint**: v1's three P1 stories are complete. The tool is usable and trustworthy.

---

## Phase 6: User Story 4 — Settle the "are our docs expensive?" question (Priority: P2)

**Goal**: Report each always-resident item separately, price it in the lane it was actually charged
in, blame forced reloads on the change that caused them, and put instruction content and
work-driven reads on one common scale.

**Independent Test**: A fixture whose instruction file sits below the cacheability minimum on one
model and above it on another; verify the same file is classified differently per turn's model and
reported as full-rate-every-turn where it does not cache (SC-026, SC-028).

- [X] T076 [US4] Create `src/claude_cost_tracker/model/lanes.py` — classify each (turn, resident item) as `cached` / `uncached` / `loading`, with the threshold read from `config/pricing.toml` for **that turn's model** and never inferred from model ordering (invariant L1, FR-077, FR-078, FR-079)
- [X] T077 [US4] Create `src/claude_cost_tracker/model/invalidation.py` — detect `tools`/`system`/`messages` tier changes, name the trigger, and charge the forced reload to the change rather than to the content re-loaded (FR-081, SC-027)
- [X] T078 [US4] Price carry per lane in `src/claude_cost_tracker/model/attribute.py` — reduced reuse rate, full rate, and the TTL-dependent write multiplier read per request (FR-080)
- [X] T079 [US4] Distinguish the four cache-miss reasons in `src/claude_cost_tracker/model/lanes.py` — left the conversation, prefix change, never eligible, lookback window exceeded — without collapsing them (FR-082, PITFALLS)
- [X] T080 [US4] Report each always-resident item individually in `src/claude_cost_tracker/render/data.py` — instruction files, skills, base instructions, and each group of tool descriptions, never one combined bucket (FR-076)
- [X] T081 [US4] Add `lanes` and `never_cacheable_on` to items in `src/claude_cost_tracker/render/data.py` and surface them in `src/claude_cost_tracker/render/terminal.py` as findings, not footnotes
- [X] T082 [US4] Build the `comparison` series in `src/claude_cost_tracker/render/data.py` — resident instruction content against work-driven reads on one common scale, two series and one axis (FR-037)
- [X] T083 [US4] Implement confidence-driven presentation in `src/claude_cost_tracker/render/data.py` and `src/claude_cost_tracker/render/terminal.py` — `display_sig_figs` from confidence, an `uncertainty` range with its dominant driver, and totals-level uncertainty notes (FR-095, FR-096, FR-097, FR-098)
- [X] T084 [US4] Populate `diagnostics.limitations` in `src/claude_cost_tracker/render/data.py` — including that injected instruction content is stripped before the transcript is written, stated alongside the figures it affects (FR-018, FR-019)
- [X] T085 [P] [US4] Unit-test lane assignment in `tests/unit/test_lanes.py` — the same 984-token file classifies as cached on Opus 5 and uncached on Opus 4.6 within one session
- [X] T086 [P] [US4] Unit-test invalidation tiers in `tests/unit/test_invalidation.py` — a tool-set change re-writes everything; a system-prompt edit re-writes system and messages but not tools
- [X] T087 [US4] Create the threshold-spanning golden fixture under `tests/golden/fixtures/session_threshold_span/` and pin it in `tests/golden/test_lane_pricing.py`
- [X] T088 [US4] Create the mid-session MCP-addition golden fixture under `tests/golden/fixtures/session_invalidation/` and pin it in `tests/golden/test_blame_the_change.py` — instruction files must **not** absorb the reload cost

**Checkpoint**: The motivating dispute can be settled with numbers that survive a skeptic.

---

## Phase 7: User Story 5 — Share the finding (Priority: P2)

**Goal**: A single self-contained HTML file that opens offline on a machine with nothing installed,
with every figure labelled, every share shown, and the unattributed slice visible in every
part-to-whole view.

**Independent Test**: Produce a report, move it to a machine with no tooling, disconnect the
network, open it, and confirm every figure and visual renders (SC-012).

- [X] T089 [US5] Create `src/claude_cost_tracker/render/charts/__init__.py` — shared SVG geometry, the fixed-order categorical palette, single-hue sequential ramps, and light/dark theming (research §1, FR-041)
- [X] T090 [P] [US5] Implement the icicle and treemap in `src/claude_cost_tracker/render/charts/hierarchy.py` — drill-down over the folder tree with a flat/total toggle (FR-034)
- [X] T091 [P] [US5] Implement the residency timeline in `src/claude_cost_tracker/render/charts/timeline.py` — one bar per span, so prolonged residency is visible at a glance (FR-036)
- [X] T092 [P] [US5] Implement stacked/delta bars and the cumulative sparkline in `src/claude_cost_tracker/render/charts/bars.py` — direct-versus-carry per item, and cost accumulating over the session with compaction events marked (FR-035, FR-039)
- [X] T093 [US5] Build the `tree` and `turns` sections in `src/claude_cost_tracker/render/data.py`, with an `unattributed` node present at the root whenever the remainder is non-zero (FR-040)
- [X] T094 [US5] Create `src/claude_cost_tracker/render/assets/` — CSS and vanilla JS for sorting, filtering, and the flat/total toggle, inlined at render time; every distinction conveyed by more than colour (FR-042)
- [X] T095 [US5] Create `src/claude_cost_tracker/render/report.py` — a single self-contained HTML file with the data inlined as a JSON literal and zero external requests (FR-032, FR-075)
- [X] T096 [US5] Implement `report --out PATH` and `--open` in `src/claude_cost_tracker/cli.py`
- [X] T097 [US5] Implement `--redact` across `src/claude_cost_tracker/render/data.py` — stable pseudonyms in `display`, `identity` omitted, cost structure and tree shape preserved (FR-043)
- [X] T098 [P] [US5] Unit-test chart geometry in `tests/unit/test_charts.py` — rectangles partition their parent exactly; the unattributed slice is emitted whenever non-zero
- [X] T099 [US5] System-test the report in `tests/system/test_report_offline.py` — no external URL of any kind in the output, every figure labelled API-equivalent and paired with a share, redaction preserves totals

**Checkpoint**: The evidence can leave the machine that produced it.

---

## Phase 8: User Story 6 — Analyse many sessions at once (Priority: P2)

**Goal**: The accumulated picture across a project, a date range, or everything — with include and
exclude recomputed from stored data, and the exclusion stated as part of the result.

**Independent Test**: Combine several sessions and verify per-item totals equal the sum of the
per-session figures; exclude one and verify the totals drop by exactly its contribution (SC-020).

- [X] T100 [US6] Implement the selection options in `src/claude_cost_tracker/cli.py` — `--session`, `--project`, `--since`/`--until`, `--all`, `--last N`, `--exclude`, combining as an intersection
- [X] T101 [US6] Implement the `sessions` command in `src/claude_cost_tracker/cli.py` and `src/claude_cost_tracker/render/terminal.py` — a browsable list with enough detail to identify each session (FR-060)
- [X] T102 [US6] Implement cross-session aggregation in `src/claude_cost_tracker/store/db.py` — accumulate per-item attribution from stored results **without re-reading the original records** (FR-061, FR-062, SC-021)
- [X] T103 [US6] Populate `scope.sessions_included`, `scope.sessions_excluded_count`, and `scope.producing_versions` in `src/claude_cost_tracker/render/data.py`, and state them in every multi-session output (FR-063, FR-028)
- [X] T104 [US6] Add `per_session` decomposition to each item in `src/claude_cost_tracker/render/data.py` — accumulated total plus contribution per contributing session (FR-064, FR-065)
- [X] T105 [US6] Create `src/claude_cost_tracker/render/serve.py` — an ephemeral `http.server` bound to `127.0.0.1` on an OS-assigned port, read-only over SQLite, serving the same data contract to the same renderer, shutting down cleanly (FR-072, FR-073)
- [X] T106 [US6] Implement the `ui` command in `src/claude_cost_tracker/cli.py` with drill-down, sorting, filtering, and session selection, leaving nothing running on exit (SC-025)
- [X] T107 [P] [US6] Unit-test aggregation exactness in `tests/unit/test_aggregation.py` — combined totals equal the sum of per-session figures; excluding one session subtracts exactly its contribution
- [X] T108 [US6] System-test multi-session behaviour in `tests/system/test_multi_session.py` — include/exclude recomputation under 2 s over a 100-session corpus, and a months-old session analysed with no loss of detail (SC-021, SC-022)

**Checkpoint**: One session is an anecdote; the corpus is now an argument.

---

## Phase 9: User Story 7 — Watch a session as it runs (Priority: P2)

**Goal**: Analyse an in-progress session at any moment, labelled provisional, never serving stale
figures as current and never reporting a figure that later proves an over-count.

**Independent Test**: With a session still running, produce a breakdown of activity so far, clearly
labelled provisional; re-run after more turns and confirm the later result supersedes rather than
adds (SC-023, SC-031).

- [X] T109 [US7] Set `scope.provisional` and `scope.covered_through_turn` for in-progress sessions in `src/claude_cost_tracker/render/data.py`, and render the provisional label prominently in `src/claude_cost_tracker/render/terminal.py` (FR-066, FR-067)
- [X] T110 [US7] Enforce freshness on read in `src/claude_cost_tracker/store/db.py` — a stored result whose fingerprint differs from the transcript's current fingerprint is either recomputed or served with explicit coverage ("covers turns 1–40; session is now at 62"), never as current (FR-084, FR-086, SC-030)
- [X] T111 [US7] Implement `--refresh` and `--watch` in `src/claude_cost_tracker/cli.py` — `--watch` polls the coverage fingerprint, redraws only on change, and exits on interrupt or when the session ends (FR-068)
- [X] T112 [US7] Ensure a later result supersedes an earlier provisional one for the same session in `src/claude_cost_tracker/store/db.py`, rather than accumulating alongside it (FR-069)
- [X] T113 [P] [US7] Unit-test freshness in `tests/unit/test_freshness.py` — invariants F1 and F2: stale is never current, and an unchanged transcript creates no second entry
- [X] T114 [US7] Component-test in-progress analysis in `tests/component/test_in_progress.py` — a growing fixture transcript yields a monotonically increasing total with no provisional figure ever later proving an over-count

**Checkpoint**: All seven v1 stories are independently functional.

---

## Phase 10: Polish & Cross-Cutting Concerns

- [X] T115 Create the hostile golden fixture under `tests/golden/fixtures/session_hostile/` — images, a resume, subagents, and a compaction in one session — and pin it in `tests/golden/test_hostile_session.py` (SC-014, quickstart Scenario 13)
- [X] T116 System-test corpus reconciliation in `tests/system/test_corpus_reconciles.py` — the real CLI over the local corpus, asserting every breakdown adds up and every output is well-formed. **This is the merge gate** (constitution, Testing Discipline)
- [X] T117 [P] Validate the performance goals in `tests/system/test_performance.py` — a single session under 30 s, a ~25-session corpus under 5 minutes, the UI up in under 5 s (SC-005, SC-006, SC-025)
- [X] T118 [P] Audit precision and uncertainty across every surface in `tests/system/test_precision_audit.py` — no figure carries more significant digits than its confidence supports, and every totals surface names its dominant uncertainty (SC-036, SC-037)
- [X] T119 [P] Verify the no-network, no-credential guarantee in `tests/system/test_no_egress.py` — no outbound request from any code path, and the tool runs with no environment variable set (FR-029, FR-030, SC-011)
- [X] T120 Walk every scenario in `specs/001-per-file-cost-attribution/quickstart.md` end to end and record the result
- [X] T121 [P] Update `README.md` with real usage — zero-argument invocation, the command table, and the honesty framing (API-equivalent, never billed)
- [X] T122 [P] Reconcile `HANDOFF.md` and `PITFALLS.md` with what was actually built and what was actually hit — the two files that carry session-to-session memory (constitution VII)
- [X] T123 Self-review the full diff, then run `uv run ruff format && uv run ruff check && uv run mypy && uv run pytest` clean (constitution IV)

---

## Deferred — not v1 scope

Recorded so nothing is lost; **not scheduled**. Spec → Assumptions → Scope defers both until the
underlying figures are demonstrably trustworthy, because a delta needs two comparable runs and a
counterfactual asserts what would have happened. A wrong counterfactual is worse than none.

- **User Story 8 (P3) — Know what a change cost**: `diff` command, per-item deltas, trend view
  distinguishing always-present overhead from work-driven cost (FR-046).
- **User Story 9 (P4) — See what could be saved**: `savings` command, items ranked by the saving
  from shortened residency, each labelled a counterfactual estimate rather than a measurement.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)** → no dependencies.
- **Foundational (Phase 2)** → depends on Setup. **Blocks every user story.**
- **US1 (Phase 3)** → depends on Foundational. The MVP.
- **US2 (Phase 4)** → depends on US1 (it explains US1's numbers).
- **US3 (Phase 5)** → depends on Foundational only; independently testable. May run in parallel
  with US2 by a second developer.
- **US4 (Phase 6)** → depends on US1 (refines how carry is priced per lane).
- **US5 (Phase 7)** → depends on US1 for data, US4 for the comparison series.
- **US6 (Phase 8)** → depends on US1 for stored results and US5 for the renderer the UI reuses.
- **US7 (Phase 9)** → depends on US1 and on Foundational fingerprinting; independent of US4–US6.
- **Polish (Phase 10)** → depends on all seven stories.

### Within each story

Model primitives → attribution → persistence → data contract → rendering → CLI → tests that pin the
contract. Golden fixtures are built alongside the arithmetic they pin, never after.

### Parallel opportunities

- Phase 1: T002, T003, T004 together.
- Phase 2: the three config files (T005–T007) together; then the ingest modules T016–T019 together
  once `records.py` lands; then all five ingest unit tests (T021–T025) together.
- Phase 3: unit tests T041–T045 together once their subjects exist.
- Phase 5: the four plugin files T068–T071 together.
- Phase 7: the three chart modules T090–T092 together once the shared geometry (T089) lands.
- Phase 10: T117–T119, T121, T122 together.
- Across stories: once Phase 2 is done, US3 can proceed alongside US2 and US4.

---

## Parallel Example: Phase 2 ingest

```bash
# After T015 (records.py) lands, four modules touch four different files:
Task: "Create src/claude_cost_tracker/ingest/dedup.py"
Task: "Create src/claude_cost_tracker/ingest/discover.py"
Task: "Create src/claude_cost_tracker/ingest/tokens.py"
Task: "Create src/claude_cost_tracker/ingest/anchors.py"

# Then their unit tests, also four different files:
Task: "Unit-test record parsing in tests/unit/test_records.py"
Task: "Unit-test dedup in tests/unit/test_dedup.py"
Task: "Unit-test discovery in tests/unit/test_discover.py"
Task: "Unit-test token resolution in tests/unit/test_tokens.py"
```

---

## Implementation Strategy

### MVP first

1. Phase 1 → Phase 2 → Phase 3.
2. **Stop and validate**: quickstart Scenarios 1 and 2. The breakdown must add up by exact integer
   equality, and a corrupted attribution must exit `3`.
3. That is a shippable tool: one session in, a trustworthy breakdown out.

### Then, in order of what de-risks the most

4. **US2 next, and specifically T061 early** — the falsifiable test of the premise (SC-010). If
   carry cost never reorders the top-10, the thesis is wrong, and finding that out after building
   the report renderer would be the expensive way to learn it.
5. US3 makes it habitual; US4 settles the motivating dispute; US5 lets the evidence leave the
   machine; US6 turns anecdote into argument; US7 puts it in the working loop.

### Non-negotiables carried through every phase

- **A failing golden test is a red alert, never a rebaseline.** A diff means a real regression or a
  deliberate model change requiring written justification and human sign-off (constitution V).
- **Exit code 3 is not a warning.** A breakdown that does not add up is a show-stopper defect; the
  tool refuses to present the numbers.
- **Observe, don't predict.** Cache mechanics explain an observed charge; they never derive one.
- **Fixtures are synthetic or scrubbed.** Never a real user transcript, in any commit.
