---
paths:
  - "**/*.py"
---

# Python Conventions

Conventions for the code assistant when writing or modifying Python in this project.
These are the language-specific *how*; the durable *why* — fail-fast, test discipline,
scripting standards, honest numbers — is the
[constitution](../../.specify/memory/constitution.md). On conflict, the constitution wins.

## Tooling — always through `uv`

- **Never invoke the OS Python.** Every Python action goes through `uv`, which manages its
  own interpreter and environment: `uv run python …`, `uv run ruff …`, `uv run mypy`,
  `uv run pytest`. Do not call a bare `python`, `python3`, or `pip`.
- Configuration lives in [`pyproject.toml`](../../pyproject.toml).
- **Keep the runtime dependency set small and boring.** This tool must stay easy to run on
  someone else's machine with one command (constitution, Principle II — local-first). A
  dependency that pulls in a service, a daemon, or a heavyweight toolchain is the wrong
  dependency.
- **Do not introduce a new library when an existing dependency (or the standard library)
  already covers the need** — no second way to do the same thing (Principle II). When a
  genuinely new capability is required, research the most popular, well-maintained options
  and pick the best **long-term** choice (adoption, maintenance, license, fit), not the
  first hit.
- Standalone scripts use [PEP 723](https://peps.python.org/pep-0723/) inline metadata so
  they run via `uv run` with no project install.

## Formatting & linting — `ruff`

- `ruff` is both formatter and linter. Format with `uv run ruff format`; lint with
  `uv run ruff check` (add `--fix` to auto-apply safe fixes).
- **Line length 100.**
- Code MUST be clean under `ruff check` before a change is done.

## Type checking — `mypy` (non-strict)

- **Type hints are required** on every function/method signature (parameters and return)
  and on module-level constants where the type is not obvious.
- `uv run mypy` MUST pass. It runs **non-strict** by decision — do not enable `--strict`
  or `strict = true`.

## Suppressions need human approval

- Adding a `# noqa`, a `# type: ignore`, or any typing escape hatch (`cast`, `Any` to
  dodge a checker) is a **deliberate deviation** and requires **explicit human approval
  before it is added**. Fix the underlying issue first; suppress only when there is no
  correct alternative, and justify it inline at the point of use.

## Naming

- **Functions, variables, module names:** `snake_case`.
- **Classes:** `PascalCase`. **Constants:** `UPPER_SNAKE_CASE`.
- **Internal helpers:** prefix with a single underscore (`_helper`).
- Names are descriptive, not compressed — `cache_read_tokens`, not `crtok`.
- **Domain terms match the central vocabulary.** Cost components, attribution policies,
  and file categories use the names defined in the single authoritative config
  (Principle IX) — never a local synonym invented at the call site.

## Structure & style

- Prefer small, single-purpose functions; validate arguments early and return early.
- Prefer standard-library primitives and existing helpers before writing new ones
  (KISS / reuse — Principle II). No second way to do the same thing.
- Use `pathlib` over manual string path joining; use f-strings over `%` / `.format`.
- Group imports stdlib → third-party → local; `ruff`'s import sorting enforces this.
- **All imports and module-level globals/constants at the top of the file** — imports right
  after the module docstring, then module constants and shared state (before any `def` /
  `class` that uses them). **No imports inside functions or other scopes** (no lazy/local
  imports); if a heavy or circular import tempts you into a function, fix the dependency
  instead. Mutable module state is declared once, at the top, and mutated via `global`.
- **Avoid unnecessary module-level globals.** Give state and constants the narrowest scope
  that works; a value used by a single function belongs inside it. Mutable module state
  must be justified.

## Numeric handling

- **Money is never a float in a stored or compared value.** Use integer minor units
  (micro-dollars) or `Decimal` for anything persisted, summed, or asserted on; floats are
  for display only. Rounding happens at the presentation edge, once.
- **Sums must reconcile** (Principle X). When splitting a total across buckets, allocate
  the remainder explicitly rather than letting rounding silently lose or invent value; a
  breakdown that does not add up to its total is a show-stopper defect.
- Token counts are integers. Never estimate one where an exact count is available in the
  source data.

## Fail-fast

- Mirror the constitution's Scripting Standards: **exit immediately on error with a clear
  message**; validate required tools/paths *before* mutating any state; keep scripts
  **idempotent** (safe to re-run after a mid-run failure).
- Raise on broken invariants rather than limping on — do not silently swallow exceptions.
  Catch only where you can genuinely recover, and narrow the `except`.
- **Malformed input records are named, not skipped in silence.** A transcript record that
  cannot be parsed is counted and surfaced in the run summary.

## Docstrings & comments

- Public functions/classes get a one-line docstring stating contract (inputs, outputs,
  side effects) when not obvious from the signature.
- Comment **why**, not **what**. Keep comments current with the code.
- **Attribution arithmetic carries its rationale inline** — the formula, the policy it
  implements, and what the number means. This is the code a reader will most need to
  audit (Principle X).

## Tests

- Definition of done for Python: `uv run ruff format`, `uv run ruff check`, `uv run mypy`,
  and `uv run pytest` all clean.
- **Prefer `pytest` fixtures for reusable setup/teardown** — any setup, resource, or
  helper state that can and should be shared belongs in a fixture, not copy-pasted across
  tests. Always choose the **correct scope** for each fixture (`function`, `class`,
  `module`, `session`): the narrowest scope that still shares the work, balancing
  isolation against setup cost.
- Fixture transcripts live in the test tree and are committed. Never point a test at the
  developer's real `~/.claude/` data — it is not reproducible and it is not shareable.
