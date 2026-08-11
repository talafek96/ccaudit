---
name: ccaudit
description: Answer questions about what a Claude Code session cost — which files, folders, or categories consumed the spend, why a file is expensive, whether docs or instruction files dominate, or how much context is costing per turn. Use whenever the user asks where their tokens or money went, what a session or file cost, why context is expensive, or whether reading .md files is driving spend. Produces measured figures from local session records rather than estimates.
---

# ccaudit — where the money went

Answers cost questions from the user's **local session records**, not from guesswork. When a
user asks what something cost, run the tool; do not estimate from token counts you have seen in
the conversation.

## Running it

```sh
ccaudit                              # most recent session for this project
ccaudit --session <id>               # a specific session
ccaudit --by category                # docs vs source vs specs vs schemas
ccaudit --by folder                  # cost rolled up the directory tree
ccaudit --sort carry --top 10        # what is expensive because it is being *carried*
ccaudit --json                       # machine-readable, same figures
ccaudit explain <figure-id>          # how one number was derived, down to the records
```

Prefix with `uvx` if it is not installed: `uvx ccaudit`.

## Reporting the answer — non-negotiable

**Every figure is an estimate of API-equivalent cost.** It is imputed from token counts and
published list prices. It is *not* a bill and must never be worded as one — not "you spent",
not "you were charged". "About $X at API rates" is the register.

**Pair every dollar figure with its share of the total.** The share survives being wrong about
pricing; the absolute does not.

**Report the unattributed remainder.** It is a real part of the answer. Never divide it among
the files to make the table tidy, and never omit it because it is large.

**Do not print more precision than the tool did.** A figure resting on a splitting policy is
not known to the cent, and the tool already renders each number at the precision its confidence
supports. Quote what it printed.

**Exit code 3 means the breakdown did not add up.** Report that and nothing else — no figures.
It is a defect in the tool, not in the user's session.

## The two reasons a file is expensive

They look identical in a total and have opposite remedies, so always say which one it is:

| Cause | What it looks like | What fixes it |
|---|---|---|
| **Loading into context** | High read count, cost concentrated in `direct` | Read it once; read a slice, not the whole file |
| **Keeping context loaded** | Read once, resident for many turns, cost concentrated in `carry` | Size matters more than read count — trim it, or let it leave context |

Carry is roughly half of all spend, so a file read once early and carried to the end can easily
outrank one read forty times.

## Two things worth checking before blaming documentation

- **Tool and MCP schemas are usually the largest resident block** — measured at roughly 50× a
  project's instruction file. `ccaudit --by category` shows `schema` separately for this reason.
- **A small instruction file may not be cached at all.** Below a model-dependent minimum,
  content is billed at full rate on *every* turn instead of a tenth. The tool reports this per
  item; it is a finding, not a footnote.

## What it will not do

It will not report a figure it cannot support. Where the records cannot carry a number — an
image whose dimensions are unreadable, content cleared before a compaction — it shows the gap
instead of estimating. If the tool declines to give a number, say that; do not fill it in.
