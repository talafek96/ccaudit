# PITFALLS

Traps hit in this project and the rule that avoids them. Format: **The trap** — what it
looks like; **The rule** — what to do instead. Append whenever a bug is found and fixed.

Entries below marked *(anticipated)* come from the research pass in
[`docs/research/prior-art.md`](docs/research/prior-art.md) rather than from being burned
here — they are documented traps in the ecosystem that this project will hit.

---

## Transcript records double-count on resume, compact, and fork *(anticipated)*

**The trap:** sessions get resumed, compacted, and forked, so the same assistant message
appears in more than one JSONL file or more than once in the same file. A naive scan sums
the same tokens repeatedly and inflates every figure downstream.

**The rule:** dedup on `(message.id, requestId)` at ingest. This is what ccusage and
token-dashboard both do, and token-dashboard ships dedup tests specifically for rescan and
resume. Non-optional; ingest must be idempotent (constitution, Scripting Standards).

## `chars // 4` is not a token count *(anticipated)*

**The trap:** estimating tool-result tokens by dividing character count by four. It is the
common shortcut (token-dashboard's scanner does it) and it is wrong by a wide, non-constant
margin.

**The rule:** prefer the exact count where the data provides one — OpenTelemetry tool spans
carry Claude Code's own `result_tokens`. Where only an estimate is possible, mark the
figure's basis as estimated and say so in the output (Principle X).

## Cache-write tokens do not match tool output size, per turn *(anticipated)*

**The trap:** assuming the next turn's `cache_creation_input_tokens` equals the size of the
tool result that was just produced, and building per-turn attribution on that join.
Measured on real transcripts: median ratio 3.31, and one 61,526-character Read produced
only 1,212 cache-creation tokens on the following turn. Cache-breakpoint placement
decouples the two.

**The rule:** reconcile at **session** level — total direct plus total carry must equal the
session total — and never assert turn-level exactness. Surface the unattributed remainder
rather than forcing the join.

## Attribute semantics shift between Claude Code versions *(anticipated)*

**The trap:** aggregating a telemetry attribute across versions where its meaning changed.
`mcp_server.name` changed meaning in v2.1.222 and Anthropic's own docs warn that dashboards
aggregating it "show a step down after you upgrade."

**The rule:** record the Claude Code version on every ingested row and version the
attribution logic alongside it. A trend line that crosses a version boundary must be able
to say so.

## Streaming double-count below Claude Code v2.1.214 *(anticipated)*

**The trap:** multi-frame usage streams inflated cost and token metrics by roughly one
extra full request per extra frame, in versions before 2.1.214.

**The rule:** record the version, and treat data from below 2.1.214 as suspect rather than
silently including it in totals.

## Pricing tables go stale and are wrong in specific ways *(anticipated)*

**The trap:** hardcoding model prices. Multiple ccusage issues track cache-write 1.25×
multipliers and systematic underestimates — the failure mode is a plausible number that is
quietly off.

**The rule:** pricing lives in one central, editable config (Principle IX). Prefer an
upstream-computed figure (`cost_usd_micros` from telemetry) where available, which moves
the drift problem out of this codebase.

## Enabling tool-detail telemetry exports more than expected *(anticipated)*

**The trap:** `OTEL_LOG_TOOL_DETAILS=1` exports file paths and full shell command strings,
tagged with the user's email under SSO. `OTEL_LOG_TOOL_CONTENT=1` exports file *bodies*.
On an enterprise account this is every developer's activity leaving the machine.

**The rule:** never enable content export. Local-first design keeps this moot by default
(constitution, Privacy); any team-wide rollout needs explicit sign-off, and any shared
export needs a path-redacting mode.

## `specify init` no longer takes `--ai`

**The trap:** `specify init --here --ai claude` fails with "No such option: --ai". The flag
was renamed and much of the documentation and muscle memory still uses the old form.

**The rule:** use `--integration claude`. Check `specify init --help` before assuming a
flag; `specify check` lists available integrations.
