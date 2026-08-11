# ccaudit

Local-first cost observability for Claude Code sessions. It answers *"where did the money
go?"* down to the individual file — including the cost of **keeping** content loaded across
turns, which is roughly half of all spend and which read-count-based accounting misses
entirely.

Every figure it reports is **API-equivalent cost**: imputed from token counts and published
list prices, always paired with a share of the session total. It is not a bill.

## Run it

```sh
uvx ccaudit                       # the most recent session for this project
```

No install, no config file, no account, no credential, no network. Zero arguments is a
complete invocation.

```sh
ccaudit --by category             # docs vs source vs specs vs tool schemas
ccaudit --by folder --top 15      # cost rolled up per directory
ccaudit sessions                  # what is available to analyse
ccaudit --json                    # machine-readable, same figures
ccaudit --redact                  # obscure paths, keep the cost structure
ccaudit explain <figure>          # how one number was derived, down to the records
```

## What it tells you that a token counter does not

Content is paid for **twice**: once when it is loaded, and again on *every subsequent turn*
for as long as it stays loaded. On a real 23-session corpus that second charge — the **carry
cost** — was about 54% of all spend, against 22% for the initial load.

So the two questions a ranking has to separate are:

| Cause | Looks like | What actually fixes it |
|---|---|---|
| **Loading into context** | read many times | read it once, or read a slice |
| **Keeping context loaded** | read once, resident for 200 turns | its *size* matters, not its read count |

They can produce identical totals and have opposite remedies. `ccaudit` reports them as
separate columns, and `explain` shows the derivation of either.

## Honest numbers

The tool exists to settle arguments, including ones its author would prefer to win. So:

- **Every breakdown adds up.** Per-item figures plus an explicit *couldn't attribute* line
  equal the session total, by exact integer equality with zero tolerance. If they ever do not,
  the tool exits **3** and refuses to print — a plausible-looking report full of wrong numbers
  is worse than no report.
- **Nothing is silently spread around.** Cost that cannot be tied to an item stays on its own
  visible line, at every grouping level, even when rows are truncated.
- **Precision follows confidence.** A figure resting on a splitting policy is never printed to
  the cent. Internal exactness is not accuracy, and the display says so.
- **Every figure is traceable.** `ccaudit explain` prints the component, the formula, the
  inputs, the policy in effect, the basis, the confidence, and the source records — enough for
  a skeptic to redo the arithmetic without rerunning the tool.
- **It declines rather than guessing.** Where the records cannot support a number — an image
  whose dimensions are unreadable, content cleared before a compaction — it shows the gap.

## Rates are yours, not the release's

Prices, cache multipliers, and cacheability thresholds drift. Baking them into the release
would go stale on every installed copy.

```sh
ccaudit pricing show              # which table is in effect, and how old its rates are
ccaudit pricing refresh           # update it, without upgrading ccaudit
ccaudit pricing refresh --from rates.json   # air-gapped
```

The refreshed table lives under `$CCAUDIT_HOME` and survives upgrades. **`refresh` is the only
command that touches the network**; analysis never does, needs no credential, and works fully
offline.

## In Claude Code

```
/plugin marketplace add talafek96/ccaudit
/plugin install ccaudit
```

Adds a `/ccaudit:audit` slash command, a model-invocable skill, and an optional `SessionEnd`
hook that queues the analysis and returns in milliseconds.

Deliberately **no MCP server**. An MCP server's tool descriptions sit in the resident context
of every session, permanently — and always-resident tool descriptions are the largest single
block of resident context, roughly 50× a project's instruction file. A cost tool that inflated
that block would corrupt the baseline it exists to measure and show up in its own reports.

## Development

Everything runs through [`uv`](https://docs.astral.sh/uv/):

```sh
uv sync --group dev
uv run ruff format && uv run ruff check && uv run mypy && uv run pytest
```

All four clean is the definition of done for every commit. The specification lives in
[`specs/001-per-file-cost-attribution/`](specs/001-per-file-cost-attribution/); the cost
mechanics everything is built on are in [`docs/cost-model.md`](docs/cost-model.md) — read that
before touching anything that produces a number. Engineering standards are the
[constitution](.specify/memory/constitution.md).

## Privacy

Session transcripts contain file paths, shell commands, and source code. Everything stays on
your machine: nothing is transmitted, `~/.claude/` is treated as strictly read-only, and
`--redact` exists for the case where a report leaves the team.
