# Quickstart — Validation guide

Runnable scenarios that prove the feature works end-to-end. Each maps to spec success criteria.
This is a validation guide, not an implementation guide — task-level detail belongs in `tasks.md`.

## Prerequisites

- `uv` on PATH (never the OS Python — `.claude/rules/python.md`)
- A Claude Code installation that has produced at least one session
- No account, credential, network access, or configuration file (SC-011)

```sh
uv run ruff format && uv run ruff check && uv run mypy && uv run pytest
```

All four clean is the Definition of Done for every commit.

---

## Scenario 1 — First run, zero arguments *(SC-015, SC-004, SC-011)*

```sh
cd <a project with session history>
uvx ccaudit
```

**Expect:** a ranked per-file breakdown of the most recent session, with no setup step.

**Verify:**
- Total, per-file rows, and an explicit **unattributed** line.
- Every dollar figure labelled an API-equivalent estimate and paired with a share (FR-010/011).
- Each of the four components shown with its plain-language name.
- The most expensive file *and the reason it is expensive* identifiable within 60 seconds.
- Completes in under 30 seconds (SC-005).

---

## Scenario 2 — Reconciliation *(SC-001, SC-002 — the core promise)*

```sh
uvx ccaudit --json > result.json
```

**Verify:**
- `attributed_micros + unattributed_micros == cost_micros`, **exact integer equality**. Not
  "within tolerance" — equal.
- The same holds per-folder, per-extension, and per-category.
- `unattributed_share` is present and displayed regardless of size.
- Corrupting an attribution row and re-running exits **3**, refusing to present numbers that do
  not add up.

---

## Scenario 3 — Why a file is expensive *(SC-010 — the falsifiable test)*

```sh
uvx ccaudit --sort cost --top 10
uvx ccaudit --sort reads --top 10
```

**Verify:** the two top-10 orderings differ materially on at least one real session.

This is the experiment that decides whether the tool measures something new. If carry cost never
changes the ranking, the premise is wrong and we want to know early — not after building the rest.

Also verify per-file `direct` vs `carry` split distinguishes *read 40 times* from *read once,
resident 58 turns* — two problems with opposite fixes.

---

## Scenario 4 — Cache-lane honesty *(SC-026, SC-028)*

Use a fixture whose instruction file sits below the cacheability minimum for its model.

**Verify:**
- The item reports `lanes.uncached_micros > 0` and is described as charged at **full rate every
  turn**, not at the reduced reuse rate.
- `never_cacheable_on` names the model.
- The threshold is read from config for **that turn's model** — a fixture spanning Opus 4.6 (4096)
  and Opus 5 (512) must classify the same 984-token file differently in each.

That last check is the one that catches a monotonic-ordering assumption, which would be silently
wrong rather than loudly wrong.

---

## Scenario 5 — Blame the change, not the content *(SC-027)*

Use a fixture where a tool/MCP server is added mid-session.

**Verify:**
- An `invalidations` entry names the change and carries `forced_reload_micros`.
- Instruction files do **not** absorb that cost.
- The narrative reads *"adding that server cost $X in forced re-writes"* — not *"CLAUDE.md got
  more expensive."*

---

## Scenario 6 — In-progress session *(SC-031, SC-023)*

With a Claude Code session **still running**:

```sh
uvx ccaudit
```

**Verify:**
- A result is produced with no action taken to end the session.
- It is labelled **provisional**, noting the most recent activity may not yet be included.
- Re-running after more turns yields a larger total; the final post-session result supersedes the
  provisional one rather than adding to it.
- No provisional figure later proves to have been an over-count.

---

## Scenario 7 — Freshness *(SC-030)*

```sh
uvx ccaudit --session S      # analyse
# ...continue the session...
uvx ccaudit --session S      # analyse again
```

**Verify:** the second run either recomputes, or reports the stored result **with explicit
coverage** ("covers turns 1–40; session is now at 62"). It never presents the older figures as
current.

---

## Scenario 8 — Concurrency and crash recovery *(SC-033, SC-034, SC-035)*

```sh
uvx ccaudit --session S & uvx ccaudit --session S & wait
```

**Verify:** both complete, figures identical, exactly one stored result.

Then kill a run mid-analysis:

**Verify:** no session is left permanently in-progress; the next invocation completes it with no
manual cleanup; a reader waiting on a live claim returns within `--wait` rather than blocking.

---

## Scenario 9 — Multi-session with exclusion *(SC-020, SC-021, SC-022)*

```sh
uvx ccaudit --all --by category
uvx ccaudit --all --exclude <one session> --by category
```

**Verify:**
- Combined per-item totals equal the sum of per-session figures.
- Excluding one session reduces totals by **exactly** that session's contribution.
- Recomputation completes under 2 seconds over ~100 sessions, without re-reading source records.
- Output states which sessions are included and how many were excluded.
- A months-old session analyses with no loss of detail.

---

## Scenario 10 — Shareable report *(SC-012, SC-024)*

```sh
uvx ccaudit report --out audit.html
```

Move `audit.html` to a machine with no tooling, disconnect the network, open it.

**Verify:**
- Renders completely offline — no external requests of any kind.
- Charts render in both light and dark themes; every distinction is conveyed by more than colour.
- The unattributed slice is visible in every part-to-whole view.
- Every figure present is also obtainable from the terminal.
- `--redact` obscures paths while preserving structure and cost.

---

## Scenario 11 — Interactive UI *(SC-025)*

```sh
uvx ccaudit ui
```

**Verify:** starts in under 5 seconds, binds loopback only, makes no external requests, supports
drill-down/sort/filter/session-selection, and **leaves nothing running** after exit.

---

## Scenario 12 — Traceability *(SC-008)*

```sh
uvx ccaudit explain <figure-id>
```

**Verify:** component, formula, inputs, policy, basis, confidence, and the source record
identifiers — sufficient for a skeptic to check the number without re-running the tool.

---

## Scenario 13 — Hostile session *(SC-014)*

One fixture containing images, a resume, subagents, and a compaction.

**Verify:** complete result, no crash, every affected limitation declared, image tokens measured
from pixel dimensions (never `chars // 4`), subagent work rolled up exactly once, unparseable
records counted in the summary.

---

## Scenario 14 — Plugin footprint *(SC-016, SC-017, SC-019)*

Install the plugin, run a session, then:

```sh
uvx ccaudit --by item --kind tool_schema
```

**Verify:** ccaudit's own resident contribution is reported and is **under 0.5%** of session cost.
Uninstall and confirm a later session contains no trace of it while stored results remain.

---

## Golden-file discipline

Fixture sessions with hand-verified expected breakdowns pin the attribution arithmetic
(constitution Principle V). A golden diff is a **red alert**, never a rebaseline-by-default: it
means either a real regression or a deliberate model change that requires written justification
and human sign-off. Fixtures are synthetic or scrubbed — never real user transcripts.
