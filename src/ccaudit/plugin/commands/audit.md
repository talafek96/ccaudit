---
description: Show where this session's money went — per file, including the cost of keeping content loaded.
---

# ccaudit

Analyse the **current** session and report where its cost went, per file and per category.

Works while the session is still running; the result is labelled provisional when it is.

## What to run

```sh
ccaudit --session "$CLAUDE_SESSION_ID"
```

If `ccaudit` is not installed, run it without installing:

```sh
uvx ccaudit --session "$CLAUDE_SESSION_ID"
```

If the session id is not available in the environment, fall back to `ccaudit`, which analyses
the most recent session for the project in the current directory.

## Reading the output

Report the breakdown as printed. Three things must survive into whatever you say about it:

- **Every figure is an estimate of API-equivalent cost, not a bill.** Never describe a number
  as what the user "was charged" or "spent" — it is imputed from token counts and published
  list prices.
- **Quote the share alongside the dollar figure.** The percentage survives being wrong about
  pricing; the dollars do not.
- **The unattributed line is part of the answer.** Report it. Do not drop it because it looks
  untidy, and do not divide it among the files.

If the command exits with code **3**, the breakdown did not add up. Say so plainly and do not
report any of the figures — that exit code exists precisely because a report full of
plausible-looking wrong numbers is worse than no report.

## Answering "why is this file expensive?"

The breakdown separates two causes that have opposite fixes:

- **Loading into context** — the file is being read over and over. Read it once, or read less
  of it.
- **Keeping context loaded** — the file was read once and then carried for the rest of the
  session. Its size matters far more than its read count.

Say which one dominates before suggesting anything.
