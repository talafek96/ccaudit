# ccaudit

**Which files cost you the most in a Claude Code session, and why.**

```sh
uvx --from git+https://github.com/talafek96/ccaudit ccaudit
```

That's it — no install, no config, no account, no network. It reads your local session
records and prints a ranked breakdown of where the money went.

(Not on PyPI yet, hence the `--from`. Working in a clone? `uv run ccaudit`.)

```
Total (API-equivalent estimate): $140.15
  accounted for:      $135.03  (96.3%)
  couldn't attribute:   $5.12   (3.7%)

Item                          Loading into context   Keeping context loaded   Total   Share
specs/001-.../spec.md                         $0.2                      $20     $20   18.5%
skill_listing                                 $0.4                       $4      $4    5.0%
specs/001-.../plan.md                        $0.08                       $4      $4    4.6%
```

---

## The one thing a token counter can't tell you

Content is paid for **twice**: once when it is loaded, and again on *every later turn* it
stays loaded. That second charge — **carry cost** — was ~54% of spend across a real
23-session corpus, against 22% for the initial load.

So two files can cost the same for opposite reasons, and the fixes are opposite too:

| Cause | Looks like | What fixes it |
|---|---|---|
| **Loading into context** | read many times | read it once, or read a slice |
| **Keeping context loaded** | read once, resident for 200 turns | its *size* matters, not its read count |

On this project's own session, ranking by cost and ranking by read count produced
**completely disjoint** top-5 lists. A read counter would have named five different files.

## More

```sh
ccaudit --by category      # docs vs source vs specs vs tool schemas
ccaudit --sort carry       # what's expensive because it's being carried
ccaudit explain <figure>   # how one number was derived, down to the records
ccaudit report --redact    # one self-contained HTML file, safe to share
ccaudit ui                 # explore in a browser; leaves nothing running
ccaudit notebook           # open a throwaway marimo notebook; deleted when you stop it
ccaudit --watch            # live, while the session is still going
```

Inside Claude Code — adds `/ccaudit:audit` and a skill your assistant can call:

```
/plugin marketplace add talafek96/ccaudit
/plugin install ccaudit
```

## Numbers you can argue with

Every figure is **API-equivalent cost** — imputed from token counts and published list
prices, always paired with a share of the total. It is not a bill.

- **Every breakdown adds up.** Per-item figures plus an explicit *couldn't attribute* line
  equal the session total, exactly. If they ever don't, the tool exits **3** and refuses to
  print rather than show plausible wrong numbers.
- **Nothing is quietly spread around.** Unattributable cost stays on its own visible line.
- **Precision follows confidence.** A figure resting on a splitting policy is never printed
  to the cent.
- **Every figure is traceable.** `ccaudit explain` gives the formula, the inputs, the policy,
  and the source records — enough to check by hand without rerunning the tool.
- **It declines rather than guessing.** Where the records can't support a number, it shows
  the gap.

Rates aren't baked into the release: `ccaudit pricing refresh` updates them in place, and the
tool tells you when they're getting old. That refresh is the *only* command that touches the
network — analysis never does.

## Privacy

Session records contain file paths, shell commands, and source. Everything stays on your
machine: nothing is transmitted, `~/.claude/` is read-only, and `--redact` replaces paths with
stable pseudonyms while keeping the cost structure checkable.

## Development

```sh
uv sync --group dev
uv run ruff format && uv run ruff check && uv run mypy && uv run pytest
```

All four clean is the definition of done. The spec lives in
[`specs/001-per-file-cost-attribution/`](specs/001-per-file-cost-attribution/); read
[`docs/cost-model.md`](docs/cost-model.md) before touching anything that produces a number.
Engineering standards are the [constitution](.specify/memory/constitution.md).
