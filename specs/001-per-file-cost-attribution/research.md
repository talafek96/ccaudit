# Phase 0 — Research & Decisions

Every Technical Context unknown resolved. Format: **Decision / Rationale / Alternatives rejected.**

---

## 1. Charting: hand-written SVG, no charting library

**Decision.** Generate SVG directly from Python for all five chart forms (icicle, treemap,
residency timeline, stacked/delta bars, sparkline). No d3, Vega-Lite, ECharts, or Plotly. Vanilla
JS for interaction, inlined.

**Rationale.** The self-contained-file constraint (FR-032) means every byte of a library ships in
every report, and we would use a small fraction of any of them — these five forms are rectangle
geometry and one path generator. More decisively, the `dataviz` rules the spec adopts
(fixed-order categorical hues, single-hue sequential ramps for magnitude, no dual axis,
**unattributed always visible as a slice**, light and dark themes) are constraints we would spend
effort *imposing on* a library's defaults. Writing the marks directly makes those rules the only
way to draw. Rendering server-side in Python also keeps the terminal, HTML, and interactive
surfaces sharing one layout implementation.

**Alternatives rejected.** *d3* (~280 KB for ~5% usage, and its defaults fight the palette rules).
*Vega-Lite* (declarative fit is good, but the runtime is larger still and grammar-level overrides
for the unattributed slice are awkward). *Matplotlib server-side to PNG* — loses interaction,
loses text selection and accessibility, and raster charts in a shareable report age badly.

---

## 2. Interactive UI: one renderer, two shells

**Decision.** The interactive browser UI and the shareable HTML report are **the same renderer**
over the same data contract. The report inlines its data as a JSON literal; the interactive UI
fetches the same JSON from an ephemeral loopback server. Server is stdlib `http.server` bound to
`127.0.0.1` on an OS-assigned port, single-purpose, read-only over SQLite, shut down on request.

**Rationale.** Satisfies FR-072/073/075 and FR-074 with one implementation rather than two, and
guarantees the two surfaces cannot drift. Keeping it stdlib preserves the no-daemon,
no-infrastructure constraint (Principle II) — it is a command that happens to render in a browser.

**Alternatives rejected.** *FastAPI/uvicorn* — a web framework and an ASGI server for a read-only
JSON endpoint; adds two dependencies and invites the thing to become a service. *Textual TUI as
the rich surface* — a genuinely good fit, but it would be a third renderer with no shared code, and
the browser is where the "10× nicer" ask actually lives. *Static report only* — fails the
exploration requirement.

---

## 3. Freshness: content fingerprint, not mtime

**Decision.** A session's coverage fingerprint is `(record_count, last_record_uuid, byte_size)`,
computed without a full parse. Every stored result records the fingerprint it covered. A result
whose fingerprint differs from the file's current fingerprint is **stale by detection**, and
FR-084 forbids serving it as current.

**Rationale.** This is what makes in-progress sessions work correctly (FR-087, SC-031): a live
session's fingerprint changes every turn, so staleness is observed rather than assumed, and the
tool either recomputes or states its coverage ("turns 1–40; session is now at 62"). Cheap enough
to run on every invocation across the whole corpus.

**Alternatives rejected.** *mtime* — coarse granularity, and a touch or a restore changes it
without changing content. *Full content hash* — correct but requires reading every byte of every
session on every invocation, defeating SC-021. *Session-ended flag* — the design this replaces;
it made currency depend on the user exiting, which is exactly the behaviour the user rejected.

---

## 4. Concurrency: claim-with-expiry over a pure function

**Decision.** A `claim` row per (session, fingerprint) with `state`, `expires_at`, `pid`, `host`.
Claims are taken with a single atomic statement that also reclaims expired ones. Readers seeing a
live claim wait a bounded interval, then compute the result themselves. Results are written in one
transaction, upserted idempotently on (session, fingerprint). WAL mode.

**Rationale.** The correctness argument is short because the analysis is a **pure function of the
transcript**: same input, same output. A race therefore wastes CPU but cannot produce a wrong
answer, which reduces the problem from distributed locking to three obligations — never serve
stale as fresh (fingerprint), never write a partial result (single transaction), never wait
forever (bounded wait then self-compute). Expiry gives crash recovery for free: a worker that dies
holding a claim blocks nothing past the lease (FR-092).

**Alternatives rejected.** *File locks* (`flock`) — stale locks after a kill need manual cleanup,
the exact failure FR-092 forbids. *A coordinating daemon* — violates Principle II outright.
*No concurrency control* — risks a half-written result being read as complete (FR-093).

---

## 5. Background pre-computation: enqueue **and** detach

**Decision.** The `SessionEnd` hook appends a queue entry and spawns a detached worker
(`start_new_session=True`, stdio redirected away from the parent), then exits in milliseconds. The
queue is the correctness guarantee; the detached run is the latency optimization.

**Rationale.** `SessionEnd` shares an overall budget capped at 60 seconds, and — decisively — a
hook supplied by an installed plugin **cannot raise that budget for itself**. A 30-second analysis
(SC-005) therefore cannot run inline. Belt-and-braces is nearly free: if the spawn fails, or the
worker dies, or the platform is Windows, the queue entry is still there and the next invocation
does the work. Nothing is lost.

**Alternatives rejected.** *Analyse inline in the hook* — silently cancelled on someone else's
machine. *Detach only* — a failed spawn silently drops the session. *Queue only* — correct but
forfeits the "already done by the time you look" benefit the user asked for.

---

## 6. Token resolution: exact, then measured, then declared

**Decision.** A three-tier ladder per quantity, with the tier recorded as the figure's `basis`:

1. **Exact** — a count recorded in the transcript (`usage` fields; telemetry `result_tokens` where
   an optional capture exists).
2. **Measured** — computed by a documented rule from data we hold: image tokens from the decoded
   header's pixel dimensions via the published area formula, capped at the model's per-image
   maximum.
3. **Declared** — where neither is available, the figure is withheld or marked low-confidence
   (FR-019). `chars // 4` is **never** used for images and is marked estimated wherever used at
   all.

**Rationale.** Base64 image payloads were ~95% of tool-result token volume in the observed corpus,
and `chars // 4` is wrong on them by roughly 100× — an error large enough to invert the tool's
central conclusion. Image dimensions are readable from PNG/JPEG/WebP headers without a full decode
or an image library. The `/context` records and session totals give an independent oracle to
validate the rule against (FR-026).

**Alternatives rejected.** *`chars // 4` everywhere* — the pass-2 headline defect. *Pillow* — a
runtime dependency to read four integers out of a header. *Skipping images* — they are a real and
dominant cost; excluding them silently would understate totals.

**Open, resolved by validation not by research:** the exact per-image formula constant is
confirmed against the `/context` anchors during golden-fixture construction; if the residual
exceeds tolerance the figure drops to low-confidence rather than shipping a plausible wrong number.

---

## 7. Pricing and thresholds: one config, per model, versioned

**Decision.** A single `config/pricing.toml` keyed by model ID, holding per-token rates, the
**TTL-dependent cache-write multipliers** (1.25× at 5-minute, 2× at 1-hour), and the
**cacheability minimum** (512 / 1024 / 2048 / 4096 depending on model). Loaded once; every rate
lookup goes through it. The producing-tool version is recorded per ingested row so
version-spanning comparisons are identifiable (FR-028).

**Rationale.** Principle IX makes this a one-place edit, and the cost model makes it necessary:
the cacheability minimum is **not monotonic across model generations**, so it cannot be derived
from a version ordering — it must be a table. Same for the write multiplier, which doubles with
TTL and must not be applied as a single session-wide constant.

**Alternatives rejected.** *Fetching prices at runtime* — violates the no-network constraint.
*Hardcoded constants at call sites* — the drift bug Principle IX exists to prevent. *Deriving the
threshold from model ordering* — factually wrong; Opus 4.7 requires 2048 while the newer Opus 5
requires 512.

---

## 8. Money representation: integer micro-dollars

**Decision.** All stored, summed, and compared monetary values are integers in micro-dollars.
Floats appear only at the presentation edge, where rounding happens exactly once. Remainders from
proportional splits are allocated explicitly (largest-remainder) rather than left to rounding.

**Rationale — conservation, not precision.** This is *not* about float accuracy. Float64 error
across even ten thousand additions is on the order of `1e-10` dollars: irrelevant next to the real
uncertainty in these figures. The reason is that SC-001 requires the invariant to be a literal
equality:

```
Σ per-item + unattributed == session total
```

With floats that comparison can be `False` because of a `1e-16` ordering artifact on a perfectly
correct breakdown. The fix would be a tolerance — and **the moment an epsilon exists, it becomes
the place real errors hide**: a genuine $0.003 misattribution slips under a threshold chosen to
absorb float noise. Integers make the check exact and need no epsilon.

The same argument covers splitting: when carry cost divides across dozens of resident items, the
slices must sum to the pool exactly. Integer largest-remainder gives that by construction. Floats
would need explicit remainder allocation anyway — the same code, plus a tolerance.

**Explicitly not a claim about accuracy.** Exact arithmetic on imputed inputs is still imputed.
FR-095 to FR-098 exist precisely so this internal exactness is never presented as precision the
figures do not have.

**Alternatives rejected.** *`Decimal`* — also exact and equality-checkable, but slower and still
needs the same remainder policy; integers are the simpler primitive for identical benefit.
*Floats with a tolerance* — weakens SC-001 into "approximately adds up", which is the failure
mode the tool exists to avoid.

---

## 9. Carry-cost splitting: proportional default, exclusive alternative

**Decision.** Default policy splits a turn's carry cost across the resident set **proportional to
each item's token weight**. A second policy implements dominator-tree-style exclusive attribution
(Eclipse MAT's model). Policy is selected by configuration (FR-006).

**Rationale.** Proportional is explainable in one sentence and a disputant can recompute it by
hand — decisive for a tool whose purpose is settling arguments. The exclusive alternative exists
because OpenCost, facing the same problem, deliberately declines to mandate one method, and a
hardcoded choice gets re-litigated the first time someone disputes a number.

**Alternatives rejected.** *Shapley value* — theoretically principled, O(2ⁿ) over the resident set,
and impossible to defend in a meeting. *Uniform split* — ignores size, which is the dominant term.

---

## 10. Terminal rendering: `rich`, degrading to plain

**Decision.** `rich` for tables, proportion bars, and colour; detect non-TTY and emit plain
parseable text (FR-071). One runtime dependency, justified.

**Rationale.** Hand-rolling ANSI tables, width negotiation, and colour capability detection is
meaningful work for no differentiation, and `rich` is mature, widely adopted, and dependency-light.
The pipe-detection requirement keeps the tool scriptable.

**Alternatives rejected.** *Plain `print`* — fails the "rich terminal presentation" requirement.
*Textual* — a full TUI framework; more than needed, and the interactive surface is the browser.

---

## 11. Distribution: `uvx` first, plugin second

**Decision.** Primary entry is `uvx ccaudit` (no install) with `uv tool install ccaudit` for
repeat use. The Claude Code plugin is a thin wrapper: `.claude-plugin/plugin.json`, a slash
command, a model-invocable skill, and an optional `SessionEnd` hook — all invoking the same CLI.

**Rationale.** Zero-install is what makes SC-015 (never-heard-of-it to a breakdown in under two
minutes) achievable. Keeping the plugin a wrapper means it adds no second code path, and it keeps
the tool's own resident footprint to a skill description that is loaded only on invocation
(FR-055).

**Alternatives rejected.** *Plugin-only* — forces an install before any value. *MCP server* —
ruled out in the spec: its tool descriptions would be permanently resident in every session, which
is the exact cost this tool exists to measure.
