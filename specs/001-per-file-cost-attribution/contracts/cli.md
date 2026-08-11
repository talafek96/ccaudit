# Contract — Command-line surface

The CLI is the primary and only mandatory interface (FR-048, FR-074). Every figure available
anywhere is obtainable here.

## Invocation

```
uvx ccaudit [COMMAND] [OPTIONS]        # zero-install
ccaudit     [COMMAND] [OPTIONS]        # after `uv tool install ccaudit`
```

**Zero arguments is a complete invocation** (FR-048): analyse the most recent session of the
project in the current working directory and print the summary. No config file, no account, no
setup step (FR-050).

## Commands

| Command | Purpose |
|---|---|
| *(none)* | Analyse the most recent session for the current project |
| `sessions` | List analysable sessions with enough detail to identify them (FR-060) |
| `analyse` | Analyse an explicit selection |
| `report` | Write the self-contained HTML report |
| `ui` | Start the ephemeral local browser interface |
| `explain` | Show the derivation of a single figure |
| `diff` | Compare two analysed sessions *(deferred — Story 8)* |
| `savings` | Counterfactual lever panel *(deferred — Story 9)* |

## Selection options

Shared by `analyse`, `report`, `ui`. Combining them is an intersection.

| Option | Effect |
|---|---|
| `--session ID…` | Explicit session identifiers |
| `--project PATH` | All sessions for a project (default: cwd's project) |
| `--since DATE` / `--until DATE` | Date range |
| `--all` | Every session in the local corpus |
| `--last N` | The N most recent in the resolved set |
| `--exclude ID…` | Drop sessions from an otherwise-matching set (FR-062) |

> **Honesty constraint.** Any multi-session output states which sessions are included and how many
> were excluded (FR-063). The exclude flag must never become a silent cherry-picking tool — the
> exclusion is part of the result, not a hidden input.

## Grouping and output options

| Option | Effect |
|---|---|
| `--by file\|folder\|ext\|category\|item` | Grouping dimension (default `file`) |
| `--policy proportional\|exclusive` | Carry-splitting policy (FR-006; default proportional) |
| `--sort cost\|carry\|direct\|reads\|share` | Ranking measure |
| `--top N` | Limit rows; the omitted remainder is still shown as a line |
| `--json` | Machine-readable output; implies plain rendering |
| `--redact` | Obscure file paths, preserve cost structure (FR-043) |
| `--open` | Open the produced report/UI |
| `--refresh` | Recompute even if a current stored result exists |
| `--watch` | Re-analyse and redraw as the session progresses, without re-invoking (FR-068). Polls the coverage fingerprint; redraws only when it changes. Exits on interrupt, or automatically once the session ends |
| `--wait SECONDS` | Bound the wait on an in-progress analysis (FR-091) |
| `--explain FIGURE` | Emit the derivation trace for a figure |

## Output modes

**Interactive terminal (TTY).** `rich` tables, proportion bars, colour. Answers "what was most
expensive and why" without leaving the terminal (FR-033, FR-070).

**Non-TTY.** Plain, parseable, stable-column text — piping and capture must work (FR-071).

**`--json`.** The report-data contract (see `report-data.md`), unchanged in shape from what the
HTML surfaces consume.

Every mode, without exception:

- labels figures as **API-equivalent cost estimates**, never as billed amounts (FR-010);
- pairs every absolute with its share of total (FR-011);
- shows the **unattributed** remainder as its own visible line (FR-012, FR-013);
- marks an in-progress session's result **provisional**, noting that the most recent activity may
  not yet be included (FR-067).

## `explain`

Given any figure identifier, print its derivation: the component, the formula, the inputs, the
policy in effect, the `basis` and `confidence`, and the source record identifiers that produced it
(FR-015, Principle VI). No re-run required. This is the surface a skeptic uses, and it is a
feature rather than a debug aid.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Usage error — bad arguments or selection |
| `2` | No analysable sessions found for the selection |
| `3` | **The breakdown does not add up** — the per-item figures plus the unattributed remainder did not equal the total. A show-stopper defect (Principle I, X); the tool refuses to present the numbers |
| `4` | Data error — records unreadable in a way that prevents a result; the diagnostic names the file and record |
| `130` | Interrupted |

Exit code `3` deserves its own code precisely because it must never be mistaken for a warning.
Every other failure is visible — a crash, a missing file, an error. This one produces a complete,
plausible-looking report full of wrong numbers, which is worse than no report because someone will
act on it.

## Environment

| Variable | Purpose |
|---|---|
| `CCAUDIT_HOME` | State directory (default: platform user-state path) |
| `CCAUDIT_PRICING` | Override the pricing/threshold config path |
| `CLAUDE_CONFIG_DIR` | Honour Claude Code's own override when locating transcripts |

No variable is required. There is no credential, no key, and no network access (FR-029, FR-030).

## Guarantees

- **Read-only** against `~/.claude/` (FR-020).
- **Idempotent**: re-running over unchanged records produces identical figures and no duplicate
  stored entry (FR-017, FR-094, SC-009).
- **No daemon**: `ui` exits when asked and leaves nothing running (FR-073).
