# claude-cost-tracker

**Find out how much a Claude Code session cost you — and which files the money went to.**

`claude-cost-tracker` reads the session transcripts Claude Code already writes on your machine and
turns them into a per-file cost breakdown: what each file, skill, and tool schema cost in a
session, split into the price of *loading* it into context and the price of *keeping* it there.
Local-first, no account, no API key, no network.

```sh
uvx claude-cost-tracker
```

That's it — no install, no config, no account, no network, no plugin. It reads your local
Claude Code session records and prints a ranked breakdown of where the money went. The command
is **`ccost`** once installed.

```
Total (API-equivalent estimate): $140.15
  accounted for:      $135.03  (96.3%)
  couldn't attribute:   $5.12   (3.7%)

Item                          Loading into context   Keeping context loaded   Total   Share
specs/001-.../spec.md                         $0.2                      $20     $20   18.5%
Skill listing                                 $0.4                       $4      $4    5.0%
specs/001-.../plan.md                        $0.08                       $4      $4    4.6%
```

Install it properly and the command is short:

```sh
uv tool install claude-cost-tracker    # then just: ccost
pip install claude-cost-tracker
```

Working in a clone? `uv run ccost`.

---

## Why a Claude Code session costs more than you think

**Content in a Claude Code session is paid for twice**: once when it is loaded, and again on
*every later turn* it stays loaded. That second charge — **carry cost** — was ~54% of spend
across a real 23-session corpus, against 22% for the initial load.

This is the thing a token counter cannot tell you. Two files can cost the same for opposite
reasons, and the fixes are opposite too:

| Cause | Looks like | What fixes it |
|---|---|---|
| **Loading into context** | read many times | read it once, or read a slice |
| **Keeping context loaded** | read once, resident for 200 turns | its *size* matters, not its read count |

On this project's own session, ranking by cost and ranking by read count produced
**completely disjoint** top-5 lists. A read counter would have named five different files.

## What you can ask it

```sh
ccost                     # this project's sessions, ranked by cost
ccost --latest            # just the session you finished
ccost --all               # every Claude Code session on the machine
ccost --by category       # docs vs source vs specs vs tool schemas
ccost --sort carry        # what's expensive because it's being carried
ccost sessions --facts    # every session with cost, rounds, reads, .md files
ccost explain <figure>    # how one number was derived, down to the records
ccost report --redact     # one self-contained HTML file, safe to share
ccost ui                  # explore in a browser; leaves nothing running
ccost notebook            # a throwaway marimo notebook; deleted when you stop it
ccost --watch             # live, while the session is still going
```

## Tracking Claude Code costs over time

Analysis is cached per session, so re-running is cheap and the history accumulates on its own.
`ccost sessions --facts` ranks every session you have ever run by cost, rounds, reads, and how
many markdown files it pulled in — which is how you notice that the expensive sessions all share
one document.

### Optional: the Claude Code plugin

**Nothing above needs this.** The plugin adds `/ccost:audit`, a skill your assistant can
call, and a session-end hook that analyses each finished session in the background:

```
/plugin marketplace add talafek96/claude-cost-tracker
/plugin install claude-cost-tracker
```

The hook runs the installed `ccost` if there is one and falls back to `uvx` if there
isn't — no install required either way. It queues the session and returns in about a
second; the analysis runs detached, after the session is gone.

What that buys is modest, and worth stating plainly rather than overselling: `ccost`
caches every completed session it analyses anyway, so a second run is already faster than
the first with no plugin involved. The hook only moves that first analysis earlier — to
session end, instead of the next time you ask. On this project's own corpus (26 sessions,
46 MB) a cold run took 2.0s against 1.7s warm.

## Numbers you can argue with

Every figure is **API-equivalent cost** — imputed from token counts and published list
prices, always paired with a share of the total. It is not a bill, and no invoice is consulted
to produce it.

- **Every breakdown adds up.** Per-item figures plus an explicit *couldn't attribute* line
  equal the session total, exactly. If they ever don't, the tool exits **3** and refuses to
  print rather than show plausible wrong numbers.
- **Nothing is quietly spread around.** Unattributable cost stays on its own visible line.
- **Precision follows confidence.** A figure resting on a splitting policy is never printed
  to the cent.
- **Every figure is traceable.** `ccost explain` gives the formula, the inputs, the policy,
  and the source records — enough to check by hand without rerunning the tool.
- **It declines rather than guessing.** Where the records can't support a number, it shows
  the gap.

Rates aren't baked into the release: `ccost pricing refresh` updates them in place, and the
tool tells you when they're getting old. That refresh is the *only* command that touches the
network — analysis never does.

## Privacy

Claude Code session records contain file paths, shell commands, and source. Everything stays on
your machine: nothing is transmitted, `~/.claude/` is treated as read-only, and `--redact`
replaces paths with stable pseudonyms while keeping the cost structure checkable.

## Keeping it current

```sh
uv tool upgrade claude-cost-tracker    # installed
uvx --refresh claude-cost-tracker      # uvx: skip the cached resolution
```

## Development

```sh
uv sync --group dev
uv run ruff format && uv run ruff check && uv run mypy && uv run pytest
```

All four clean is the definition of done. The spec lives in
[`specs/001-per-file-cost-attribution/`](specs/001-per-file-cost-attribution/); read
[`docs/cost-model.md`](docs/cost-model.md) before touching anything that produces a number, and
[`docs/releasing.md`](docs/releasing.md) to cut a release. Engineering standards are the
[constitution](.specify/memory/constitution.md).

## License

[Apache 2.0](LICENSE).
