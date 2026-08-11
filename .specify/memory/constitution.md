# clauditor Constitution

<!--
Sync Impact Report — 2026-08-11
Version: — → 1.0.0 (initial ratification)
Adapted from the NextMngr constitution v1.3.1, generalized away from C/C++ and from
that project's build system. Carried over: Fail-Fast, Power of Simplicity, Stability
First & Zero-Bug, Developer-Led Quality, Test Discipline (incl. the unit-tests-as-
contract mandate for AI-authored code), Supportability & Debuggability, Organizational
Memory, Effort Optimization & Flow, Central Configuration, Scripting Standards,
Governance, and the two-level defect classification.
Added: Principle X (Honest Numbers) — domain-specific, non-negotiable for a tool whose
entire output is cost figures people make decisions on.
Dropped: C/C++-specific testing levels and mechanics; the acceptance-test level (no
customer-facing release gate for a local tool).
Keep in sync: CLAUDE.md, .claude/rules/python.md, .claude/rules/git-conventions.md,
docs/research/prior-art.md (the cost-component vocabulary).
-->

The durable engineering standards for this project — its DNA. Kept concise and
high-signal on purpose: this file is imported into agent context every session.
Language-specific *mechanics* live in `.claude/rules/*.md` (Python style, git) and MUST
stay consistent with this file; on conflict, this file wins.

## Core Principles

### I. Fail-Fast

- **Make bugs shout.** On a broken invariant, stop immediately rather than limp on in an
  undefined state — a limping process silently corrupts more state. When in doubt, raise.
- **Raise on genuine failures; never disguise one as a returned value.** Return a value
  only for a *normal outcome* the caller branches on — not-found, empty, would-block.
- **Never silently swallow an exception.** Catch only where you can genuinely recover, and
  narrow the `except`.
- **Every failure carries enough to triage:** the offending input, the file and record
  that produced it, and what invariant broke.
- *Why: this tool's job is to produce numbers people make decisions on. Data that is
  quietly wrong is far worse than a run that stops and says why.*

### II. Power of Simplicity (KISS)

- **Simplest path that works** — don't build structure the code doesn't yet need.
- **Standardize:** reuse an existing primitive or pattern before writing a new one; no
  second way to do the same thing. Actively look for existing code to reuse before
  deciding to write new code.
- **Small, focused files & clean separation:** keep files small and single-purpose with
  good folder separation — but do **not** create files or directories you do not need.
  Prefer fewer, well-placed files over many thin ones.
- **Fewest types that work:** do **not** introduce a class or abstraction the code does
  not yet need. Prefer the fewest well-named types over a zoo of thin wrappers or
  near-duplicate classes; collapse duplicates into one. A new indirection must earn its
  place.
- **Local-first, no infrastructure.** This tool runs on one machine with one command. No
  daemons, no docker-compose, no services to keep running, no external stores. A
  dependency that needs a server to be useful is the wrong dependency.
- **No speculative generality, and no cleverness a reader must decode.**

### III. Stability First & Zero-Bug

- **Stability outranks new features.**
- **Zero-bug:** fix known bugs before new work — bugs are blockers, not backlog.
- **Design first:** settle component boundaries, data flow, and interfaces before coding.
  In this repo that means the spec (`specs/<feature>/spec.md`) precedes the code.
- **Write once, then increment** on a tested baseline — no throw-away code; build
  bottom-up on already-tested layers.

### IV. Developer-Led Quality

- **You are the first line of defense — not CI, not a reviewer.** Fix fragile or
  over-complex code the moment you notice it.
- **Self-review your diff** end-to-end before pushing.
- **Validate before every push** (relevant tests + lint + type-check) — that is the
  Definition of Done.
- **Log deferred tech debt** in `HANDOFF.md`; keep the list lean.

### V. Test Discipline

- **Component tests are primary:** exercise a real pipeline stage end-to-end over fixture
  data, with minimal stubs and injected config.
- **Automated first, manual minimal;** grow coverage within reason.
- **Defect-driven:** nearly every bug adds a test. **No flaky tests.**
- **A failing test, lint, or type-check is a red alert — not a nuisance to silence.**
  Never assume the check is wrong and edit it to pass. Tests encode invariants; changing
  or deleting one signals a change of spec or design that may be unintended. Any test
  change REQUIRES explicit human verification, with a short written explanation of why
  the invariant changed.
- **Unit tests are the contract on AI-authored code.** Code produced by an AI agent MUST
  ship with unit tests that fence its intended behavior; those tests ARE the durable
  contract for that code. A unit test that later fails means a previously agreed contract
  was breached — the agent MUST realign the code to the contract, and MUST NOT weaken,
  rewrite, or delete the test to make it pass without explicit human intervention (per the
  rule above). This is contract-fencing, not test-first or a coverage quota.
- **Attribution arithmetic gets golden-file tests.** Any change to how cost is split
  across files, turns, or components must be pinned by a fixture session with a known,
  hand-checked expected breakdown. This is the highest-risk code in the project: it is
  silently wrong, not loudly wrong.
- Full per-level policy in *Testing Discipline* below.

### VI. Supportability & Debuggability

- **Debuggable from output, not a live debugger.** Every derived number must be traceable
  back to the source records that produced it.
- **Instrumentation is part of "done":** a `--explain`-style path that shows the
  derivation of a figure is a feature, not a debug aid.
- **Act on observed state, never on sleeps or timing.**

### VII. Organizational Memory

- **Repo is the single source of truth** — docs live in it, and stay minimal.
- **Code decisions live inline, not in prose docs;** docs hold only the slow-changing
  concepts.
- **Two files carry session-to-session memory:** `HANDOFF.md` (project state, next steps)
  and `PITFALLS.md` (traps hit and the rule that avoids them). Keep both current.

### VIII. Effort Optimization & Flow

- **Blockers first** — bugs are blockers.
- **Long horizon:** pivot to other work when blocked; keep long-term dependencies in view.
- **Localize & batch:** fix adjacent issues in place; group work by functional area.
- **Lean:** less process, not more.

### IX. Central Configuration

- **One source per registry.** Every set of related configuration or registry entries —
  model pricing, cost-component definitions and their plain-language names, file-category
  rules, attribution policies — has a single authoritative location. Adding or changing an
  entry is a **one-place edit**.
- **Derive, don't duplicate.** Generate dependent artifacts (enums, display labels,
  report columns, chart legends) from that one source so they cannot drift apart. The
  plain-language name of a cost component is defined once and rendered everywhere from
  that definition.
- **No scattered magic constants.** Tunables (pricing, thresholds, limits) live in the
  central config, not sprinkled across call sites.
- *Why: a scattered registry breeds "changed X, forgot Y" drift bugs. This is DRY applied
  to configuration — distinct from VII, which governs docs and knowledge.*

### X. Honest Numbers

The product is a number-producing tool used to settle arguments. A confidently wrong
figure does more damage than no figure at all. Therefore:

- **Never present an estimate as a measurement.** Every cost figure is *API-equivalent
  cost* — imputed from token counts and list prices — and MUST be labeled as such at every
  surface it appears on. It is not a bill, and must never be worded as one.
- **Pair every absolute with a share.** Report the percentage of total alongside the
  dollar figure; the share survives being wrong about pricing, and the dollars do not.
- **Attribution must reconcile.** Per-file, per-tool, and per-category breakdowns MUST sum
  to the session total. Any remainder is shown explicitly as unattributed — never silently
  dropped, never quietly absorbed into the nearest bucket.
- **Label confidence and basis.** Each figure carries how it was derived and how much to
  trust it. Missing attribution beats wrong attribution: when the data cannot support a
  number, say so instead of estimating one.
- **Name things as they are.** Every technical token category carries a plain-language
  name that a non-expert reads correctly, with the technical term retained as a secondary
  label. Jargon that only the author understands is a defect.
- **A finding must be reproducible by the reader.** Any number in a report must be
  traceable to the session records that produced it, without rerunning the tool.
- *Why: the tool exists to test claims about where money goes, including claims the
  author would prefer to be true. It is only worth anything if its numbers hold up when
  someone who disagrees checks them.*

## Testing Discipline

Per-level policy. "Used" means it is an accepted, expected part of the workflow.

| Level               | Used | Notes                                                                                                     |
|---------------------|------|-----------------------------------------------------------------------------------------------------------|
| Static analysis     | Yes  | `ruff check` + `mypy` clean before done. Run proactively, not just at the end.                              |
| Unit                | Yes (required for AI-authored code) | The fence on agent-written code: each unit test is a durable contract; a break requires human intervention to change (Principle V). Write the minimum that pins the contract — not TDD, not a coverage quota. Also the level for parsers, the cost model, and each attribution primitive in isolation. |
| Component / module  | Yes  | The primary method (Principle V). A pipeline stage over fixture data, end-to-end, as one process. For a stage built of sub-parts, this level tests the **composition**; each sub-part **in isolation** is the unit level. |
| Golden / regression | Yes  | Fixture sessions with hand-verified expected breakdowns, pinning the attribution arithmetic. A diff here is a red alert, never a rebaseline-by-default. |
| Integration         | No   | Mid-layer cross-component testing is not worth the time as its own level.                                  |
| System              | Yes  | The **merge gate**: run the real CLI over a real transcript corpus and assert the output reconciles and is well-formed. MUST be stable and predictable. |

- Explicitly avoided: **TDD**, **code-coverage targets** — too costly for the return. The
  unit-test mandate above is contract-fencing (pin intended behavior after the fact),
  which is neither test-first nor a coverage quota.

## Logging & Defect Classification

- **Log levels:** *Critical* (failure, usually raise + exit); *Control* (state-changing
  flow; default); *Info* (operational, low noise); *Debug* (verbose diagnostics).
- *Critical* + *Control* MUST trace the entire workflow; *Info*/*Debug* MAY merge; too
  many levels create a mess.
- **Defects — two levels only** (avoids priority overhead): *Show-stopper* (fix
  immediately — anything producing a wrong number is by definition a show-stopper); *all
  others* (fix soon).

## Scripting Standards

- **Fail-fast:** exit immediately on error with a clear, informative message.
- **Validate first:** confirm required tools and env vars *before* changing any data.
- **Idempotent:** safe to re-run, even after a mid-run failure. Re-running an ingest over
  the same transcripts MUST NOT double-count.
- **Static analysis:** run linting, type checking, and any other available static analysis
  proactively to verify correctness and best-practice adherence.
- **Modular & portable:** split long scripts into small units; no hard-coded absolute
  paths.
- **Read-only against user data.** Anything under `~/.claude/` is treated as read-only
  input. The tool never writes to, moves, or prunes a user's transcripts.

## Privacy

- **Local by default.** Session transcripts contain file paths, shell commands, and source
  code. Nothing leaves the machine unless the user explicitly exports it.
- **Exports are opt-in and reviewable.** Any shareable artifact (a report for a manager)
  must be inspectable before it is shared, and must offer a path-redacting mode.
- **Never require credentials.** The tool works from local data and the user's existing
  Claude Code install. It does not ask for, store, or transmit an API key.

## Governance

- This constitution supersedes ad-hoc practice; when code, a decision, or a review
  conflicts with a principle here, the principle wins.
- **`.claude/rules/` relationship:** this file holds the durable *why* and the
  non-negotiables; `.claude/rules/*.md` hold the language-specific *how*
  ([`python.md`](../../.claude/rules/python.md) for Python,
  [`git-conventions.md`](../../.claude/rules/git-conventions.md) for git). Rules MUST NOT
  restate this file and MUST stay consistent with it.
- **Amendments:** a documented change, reviewed like code; on merge, bump the version
  below and update the Sync Impact Report comment.
- **Versioning (semver):** MAJOR = incompatible principle removal/redefinition; MINOR =
  new or materially expanded principle; PATCH = clarification/wording.
- **Compliance:** design and review verify adherence; deliberate deviations MUST be
  justified in writing at the point of deviation.

**Version**: 1.0.0 | **Ratified**: 2026-08-11 | **Last Amended**: 2026-08-11
