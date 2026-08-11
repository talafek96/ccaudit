# Prior art & problem framing

Research pass completed 2026-08-11. This document is input to the product spec.
Everything here is evidence-backed; sources are linked inline.

---

## 1. What the dollar figure actually is

Claude Code stopped writing `costUSD` into its JSONL transcripts in v1.0.9
([ccusage#1281](https://github.com/ccusage/ccusage/pull/1281)). Every dollar figure produced by
every local tool today — including this one — is **imputed**: token counts × published list price.

On a Teams/Enterprise seat, Anthropic's own docs are explicit that usage inside the seat
allowance "isn't metered in dollars," and that `/usage`'s session cost figure "isn't relevant for
billing purposes" for subscribers ([Manage costs](https://code.claude.com/docs/en/costs)). Local
data cannot even distinguish billing modes: `service_tier` reads `"standard"` for both API-key and
subscription accounts across 44k+ observed JSONL entries.

**Product decision.** clauditor reports **API-equivalent cost** and labels it as such, everywhere,
without exception. Enterprise pricing is understood to track API list pricing closely, which makes
the figure a useful proxy — but it is a *shadow price* for internal chargeback and optimization,
not an invoice. Alongside every absolute dollar figure, clauditor reports **share of total spend**
(the percentage), because the share is robust to pricing drift and to the gap between list price
and negotiated enterprise rates. The share is the number that survives being wrong about the
dollars.

The schema shape to adopt is the one proposed in
[ccusage#1503](https://github.com/ccusage/ccusage/issues/1503): carry `estimated_cost_usd`,
a nullable `actual_billed_cost_usd`, and an explicit `billing_basis`.

---

## 2. Where the money actually goes

Measured across 22 local sessions on this machine:

| Component | Tokens | Share of imputed spend |
|---|---:|---:|
| `cache_read_input_tokens` | 589,716,189 | **53.6%** |
| `output_tokens` | 5,241,999 | 23.8% |
| `cache_creation_input_tokens` | 19,683,429 | 22.4% |
| `input_tokens` (fresh) | 168,963 | 0.2% |

Total imputed at Opus list rates: ~$1,649.

**This is the central finding.** A file's content enters the bill **once** as a cache write, and
then **re-enters on every subsequent turn** as a cache read for as long as it stays resident in
context. Cache reads are 96.7% of all input-side tokens and over half of all spend.

Consequence for the product: **any per-file attribution that charges a file only for the turn it
was read explains less than a quarter of the money.** A read-count heatmap is not an answer to
"which file cost the most." The residency term dominates by roughly 30:1 in tokens.

---

## 3. Plain-language names for the four cost components

Users (reasonably) do not know what "cache read" means. Every surface must show a plain-language
name; the technical term is retained as a secondary label for people who want it.

| Technical term | Plain-language name | What it actually is |
|---|---|---|
| `cache_creation_input_tokens` | **Loading into context** | The one-time charge for putting a chunk of content (a file you read, a skill, CLAUDE.md) into the conversation so the model can see it. Paid once per chunk. |
| `cache_read_input_tokens` | **Keeping context loaded** | Charged *every single turn* for re-showing the model everything already in the conversation. You pay this again on every message for as long as the content stays loaded. This is the big one. |
| `input_tokens` | **Your new typing** | The genuinely new text in your message that isn't already in context. Almost always negligible. |
| `output_tokens` | **What Claude wrote back** | Everything the model generated — replies, code, tool call arguments, thinking. |

A useful one-line framing for the manager-facing view: *"Loading" is rent you pay once. "Keeping
loaded" is rent you pay every turn until it's evicted.*

---

## 4. The question this tool exists to answer

The motivating hypothesis, held by the user's manager:

> A large share of our Claude Code spend comes from reading and re-reading `.md` files —
> `CLAUDE.md`, skills, specs, and similar.

clauditor must be able to **confirm or refute this with numbers over real sessions.** That makes
per-file attribution — and specifically per-file attribution *including residency cost* — the
core feature, not a nice-to-have. Grouping by file, by file type/extension, by directory, and by
category (project docs vs. source vs. skill vs. spec) are all first-class.

Note that the hypothesis is genuinely plausible under the residency model *and* genuinely
plausible to be wrong: a small `.md` file loaded at turn 1 and never evicted accrues carry cost on
every one of 60 turns, while a large source file read at turn 58 accrues almost none. Read counts
and file sizes both mislead here. Only the residency-weighted model settles it — which is exactly
why the tool is needed rather than a spreadsheet.

---

## 5. Attribution model

Borrowed from the [OpenCost Specification](https://www.opencost.io/docs/specification) (CNCF),
which solves the structurally identical problem of allocating a shared, continuously-billed
resource to tenants who only intermittently cause it. Its decomposition —
`workload + idle + overhead` — ports directly:

| OpenCost concept | clauditor analogue | Measured from | Share |
|---|---|---|---|
| Workload cost | **Direct cost** — file X's content entering context | cache-creation tokens attributable to the Read/Edit that injected it | ~22% |
| Idle cost | **Carry cost** — re-paid every turn X stays resident | the turn's cache-read tokens, minus overhead, split across the resident set | ~54% |
| Overhead cost | **Overhead** — system prompt, CLAUDE.md, tool/MCP schemas | measured at session start; subtracted before splitting | (within carry) |
| *(no analogue)* | **Output** — caused by the turn, never by a file | output tokens | ~24% |

The mechanism: maintain a per-turn **residency set** — which content is live in context at turn
*t*, and each item's token weight. For each turn, subtract overhead from the turn's cache-read
tokens and split the remainder across the residency set proportional to token weight. A file's
total cost is its one-time direct charge plus the sum of its carry slices.

Three rules, each earned from the research:

1. **The splitting policy is a config knob**, not a hardcoded choice. OpenCost names three methods
   (uniform, proportional-to-consumption, custom metric) and deliberately declines to pick one;
   a spec that hardcodes one will be re-litigated the first time someone disputes a number.
   Default: proportional by residency tokens — explainable in one sentence and auditable, unlike
   Shapley value, which is principled but O(2ⁿ) over the residency set and impossible to defend
   in a meeting.
2. **Carry cost is computed as a residual, and the unallocated remainder is always shown.**
   Per-file dollars must reconcile to the session total. Never let attribution silently fail to
   sum.
3. **Every figure carries a confidence label and a basis.** Pattern from
   [agentacct](https://github.com/mikehasa/agentacct): *"missing attribution beats wrong
   attribution."* Direct cost from telemetry `result_tokens` is exact; carry cost from a
   proportional split is medium; the dollar conversion is always an estimate.

Open modelling question: **eviction.** No prior art models when content *leaves* context. Simplest
defensible v1 treats `PreCompact`/`PostCompact` as residency-set reset boundaries, and accounts for
cache TTL (1h on subscription, 5m on usage credits).

---

## 6. Data sources

### OpenTelemetry traces — richest, and subscription-native

With `CLAUDE_CODE_ENABLE_TELEMETRY=1`, `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`,
`OTEL_TRACES_EXPORTER=otlp`, and `OTEL_LOG_TOOL_DETAILS=1`, Claude Code emits:

```
claude_code.interaction              (one per user prompt)
├── claude_code.llm_request          → input/output/cache_read/cache_creation tokens, model
└── claude_code.tool                 → tool_name, result_tokens, tool_use_id,
                                       file_path, full_command, skill_name,
                                       subagent_type, agent_id, parent_agent_id
```

Two properties JSONL cannot match: `result_tokens` is *Claude Code's own* token count for the tool
result (versus a `chars // 4` estimate), and `file_path` sits as a sibling of the token counts
under one interaction span — so the file→cost join is **structural, not inferred**.
`claude_code.api_request` also carries `cost_usd_micros`, moving pricing-table maintenance
upstream. `tool_use_id` is a shared join key across spans, events, and hook payloads.

**Auth note (the constraint is inverted from what one would expect):** OAuth/SSO is the
*privileged* case. Anthropic's docs state that with a direct API key or via Bedrock/Vertex/Foundry
"there is no Claude account in the session," and only `user.id`/`session.id` are populated —
whereas SSO sessions get `user.email`, `user.account_uuid`, and `organization.id` stamped
automatically. **Nothing in the OTel path requires an API key; nothing in it breaks under SSO.**

Caveat to verify before depending on it: enterprise managed settings can lock or strip
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` at startup.

### JSONL transcripts — the historical record

`~/.claude/projects/**/*.jsonl` is the only path to **past** sessions (telemetry is prospective
only). Same extraction as [token-dashboard](https://github.com/nateherkai/token-dashboard)'s
scanner, with `chars // 4` for result token estimates. Backfilled data should be marked lower
confidence.

### Hooks — fallback

`PostToolUse` yields `tool_name`, `tool_input.file_path`, `tool_use_id`, `agent_id` — enough to
reconstruct the residency set, which is what [token-manage](https://github.com/aestheteRON/token-manage)
does. Documented trap: the transcript is written asynchronously and lags the in-memory
conversation, so a hook cannot read the current turn's token counts. Hooks capture *residency
events*; token counts join in later on `tool_use_id`.

---

## 7. Prior art shortlist

Granularity ladder: `session → turn/prompt → tool-call → file → file-with-carry-cost`.
**Nothing in the ecosystem reaches the last rung.**

| Project | What it does | Granularity | Gap |
|---|---|---|---|
| [nateherkai/token-dashboard](https://github.com/nateherkai/token-dashboard) (654★) | JSONL → SQLite → local web dashboard; per-prompt cost, tool heatmaps, subagent + cache analytics | tool-call, **per-file counts** | Closest existing work, and exactly where it stops: `tool_calls.target` holds the file path, but cost is grouped by `tool_name` only. Counts, never dollars. No carry model. |
| [androidZzT/context-window-inspector](https://github.com/androidZzT/context-window-inspector) | "Where did the tokens go" by bucket (tool results / MCP / skills / summaries) with cache-aware cost | turn + bucket | Best cache-aware cost model found. Buckets, not files. |
| [mikehasa/agentacct](https://github.com/mikehasa/agentacct) (583★) | TUI; per-session work-step ledger; confidence-labelled attribution; % of weekly plan consumed | session + work-step | No file/tool dollars. Its *epistemics* are the thing to copy. |
| [ccusage/ccusage](https://github.com/ccusage/ccusage) (17.8k★) | De-facto imputation engine; daily/weekly/session rollups | session/day | No tool/file granularity. |
| [aestheteRON/token-manage](https://github.com/aestheteRON/token-manage) (0★, v0.0.1) | VS Code sidebar; `PostToolUse` hook → file-level token attribution, "context bill" | **file (tokens)** | The only project attempting file-level attribution at all. Proves the hook path works; attributes tokens, not dollars. |
| [anthropics/claude-code-monitoring-guide](https://github.com/anthropics/claude-code-monitoring-guide) (360★) | Official OTel Collector → Prometheus → Grafana reference | session/user/model | 8 panels, metrics only. No tool, no file. |
| [ColeMurray/claude-code-otel](https://github.com/ColeMurray/claude-code-otel) (486★) | Richer OTel dashboards: cost, DAU/WAU/MAU, tool usage, latency, errors | session + tool_name | Tool *counts*, not tool *dollars*. |
| [chiphuyen/sniffly](https://github.com/chiphuyen/sniffly) (1.3k★) | Usage stats + error breakdown, shareable dashboard | session | Stale (~1yr); predates current schema. |

**Disqualified on auth grounds** (this org authenticates via Google SSO → claude.ai seats, not API
keys): the Anthropic Console usage API, the Claude Code Analytics API lane, and LiteLLM-style
gateway spend tracking — the last of which would also mean routing a subscription OAuth token
through a third-party proxy, a ToS risk. Do not.

**Enterprise admin ceiling, confirmed:** the spend-report CSV and Analytics API reach
per-user × per-model × per-day, and cover only usage-credit spend past the seat allowance. No
session, no tool, no file. Anthropic's own docs redirect to OpenTelemetry for per-user token and
cost data.

---

## 8. Architecture implication: local-first, no Prometheus

Requirement: this must run locally and easily. That rules out the Prometheus + Grafana + collector
stack that the official guide and `claude-code-otel` both assume — appropriate for an org-wide
rollout, far too heavy for a single developer answering a single question.

It also happens to be the technically correct call. Per-file attribution is a **stateful sequential
computation over a session's turns** — reconstructing residency sets, splitting residuals — and is
not expressible as a Prometheus recording rule. Prometheus is a time-series metrics store; this is
an analytical query workload over event data. The docs are also explicit that `file_path` is
trace/event-only, deliberately excluded from metrics because it "would cause unbounded
cardinality" — so the very dimension the product is built around cannot live in Prometheus at all.

Direction: JSONL (and optionally local OTLP capture) → embedded analytical store
(SQLite or DuckDB) → static HTML report + local dashboard. No daemon required, no docker-compose,
no services to keep running. Precedent for the DuckDB-over-cost-data pattern:
[cost-goblin](https://github.com/etiennechabert/cost-goblin).

---

## 9. Known traps to design around

| Trap | Evidence | Mitigation |
|---|---|---|
| Transcript double-counting on resume / compact / fork | token-dashboard ships dedup tests; ccusage dedups on `messageId`+`requestId` | Dedup on `(message.id, requestId)`. Non-optional. |
| Streaming double-count | Before Claude Code v2.1.214, multi-frame usage streams inflated cost/token metrics by ~one extra full request per extra frame | Require ≥2.1.214 |
| Attribute semantics shift between versions | `mcp_server.name` changed meaning in v2.1.222; docs warn dashboards "show a step down after you upgrade" | Record `app.version` on every row; version the attribution logic |
| `chars // 4` is not a token count | token-dashboard's scanner estimates this way | Prefer telemetry `result_tokens`; mark estimated rows as such |
| Cache writes ≠ tool output size, per turn | Measured: median next-turn cache-creation ÷ estimated result tokens = 3.31; one 61,526-char Read produced only 1,212 cache-creation tokens the following turn — cache-breakpoint placement decouples them | Reconcile at *session* level; never assert turn-level exactness |
| Stale pricing tables | Multiple ccusage issues on cache-write 1.25× multipliers and underestimates | Editable pricing config; prefer upstream `cost_usd_micros` where available |
| Privacy blast radius | `OTEL_LOG_TOOL_DETAILS=1` exports file paths and shell command strings tagged with user email; `OTEL_LOG_TOOL_CONTENT=1` exports file *bodies* | Never enable content export. Local-first design keeps this moot by default; any team rollout needs sign-off. |
