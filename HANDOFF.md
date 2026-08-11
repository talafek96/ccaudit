# HANDOFF

Project state and next steps. Kept current; the next session reads this first.

## What this project is

**clauditor** — local-first cost observability for Claude Code sessions. It answers "where
did the money go?" per file, per tool call, per prompt, including the cost of keeping
content resident in context across turns.

Motivating question it must settle with numbers: *does a large share of Claude Code spend
actually come from reading and re-reading `.md` files (CLAUDE.md, skills, specs)?* The tool
must be able to confirm **or refute** that, credibly enough to show a manager.

## Current state — 2026-08-11

Scaffolding only. No product code yet.

- Git repo initialized on `main`.
- **spec-kit** installed (`specify init --here --integration claude`, note the CLI uses
  `--integration`, not the older `--ai`). Skills under `.claude/skills/speckit-*`.
- **Constitution** ratified at v1.0.0 in `.specify/memory/constitution.md`, adapted from
  the NextMngr constitution v1.3.1 and generalized off C/C++. Principle X (Honest Numbers)
  is new and domain-specific.
- **`CLAUDE.md`** imports the constitution via `@` so it is always in context.
- **`.claude/rules/`** — `python.md` and `git-conventions.md`, both generalized from
  NextMngr.
- **`.claude/commands/`** — `shift-handoff.md` (verbatim), `end-session.md` (adapted: the
  `goal-orchestrator` skill it referenced does not exist here, so step 2 is inline
  decomposition).
- **Portable skills** vendored from NextMngr: `duckduckgo-search`, `web-scraper`,
  `prior-art`, `claude-md-standards`, `explain-with-trees`, `reconcile-docs`,
  `resume-remote-handoff`.
- **`docs/research/prior-art.md`** — the research pass: what the dollar figure actually
  is, where the money goes, the attribution model, data sources, prior-art shortlist,
  known traps. This is spec input; read it before writing anything that produces a number.

## Decisions locked — do not re-litigate

- **API-equivalent cost, always labeled.** Claude Code dropped `costUSD` from transcripts
  in v1.0.9; enterprise seat usage is not metered in dollars. Every figure is imputed.
  Enterprise pricing is understood to track API list pricing closely, which is what makes
  the proxy useful — but it is a shadow price, not a bill.
- **Always pair absolute with share of total.** The percentage survives being wrong about
  pricing; the dollars do not. This is also the pie-chart view.
- **Per-file attribution must include carry cost.** Cache reads are ~54% of spend and
  cache writes ~22%; charging a file only for the turn it was read explains under a
  quarter of the money.
- **Plain-language names are mandatory**, technical terms secondary. Defined once,
  centrally (Principle IX), rendered everywhere from that definition.
- **No Prometheus, no docker-compose, no daemon.** Local-first, one command. Also the
  technically correct call: `file_path` is deliberately excluded from Claude Code's
  metrics for cardinality reasons, and residency attribution is a stateful sequential
  computation, not a recording rule.
- **Attribution splitting policy is a config knob**, not hardcoded. Default: proportional
  by residency tokens. Not Shapley — principled but O(2ⁿ) and unauditable in a meeting.

## Next steps

1. **Write the product spec** — `/speckit-specify`. Inputs: this file and
   `docs/research/prior-art.md`. Done-check: a `specs/001-*/spec.md` that states the
   questions the tool answers, the required output surfaces, and the honesty constraints
   from Principle X as testable requirements.
2. **`/speckit-clarify`** the spec — the known-ambiguous areas are eviction modelling,
   which cost components are attributable to a file at all, and the exact scope of the
   first release (single session vs. cross-session aggregation).
3. **`/speckit-plan`** — pick the storage (SQLite vs. DuckDB) and the presentation surface
   (static HTML report vs. local dashboard vs. both), grounded in the local-first
   constraint.
4. **Verify the telemetry path is actually open** before designing around it: check
   whether enterprise managed settings lock or strip `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`.
   If locked, the JSONL + hooks path becomes primary rather than the backfill path.

## Open questions

- Eviction: no prior art models when content *leaves* context. Working v1 assumption is
  `PreCompact`/`PostCompact` as residency-set reset boundaries, plus cache TTL (1h on
  subscription, 5m on usage credits). Needs validation against real transcripts.
- Python packaging/runtime not yet chosen — no `pyproject.toml` exists yet, so the
  Definition of Done commands in `CLAUDE.md` are aspirational until it lands.
