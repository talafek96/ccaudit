# The ccaudit Claude Code plugin

A thin wrapper over the CLI. It adds no second code path, and — the constraint that shaped
everything here — **no persistent resident content**.

## The footprint contract

| Surface | What it costs when you are not using it |
|---|---|
| `/ccaudit:audit` slash command | **Nothing.** Not in context until typed. |
| `ccaudit` skill | Its description only. The body loads when invoked. |
| `SessionEnd` hook | **Nothing.** Runs outside the conversation. |
| MCP server | **Not used, by design.** |

### Why there is no MCP server

An MCP server's tool descriptions sit in the resident context of **every** session,
permanently. The research behind this tool established that always-resident tool descriptions
are the single largest block of resident context — roughly **50×** a project's instruction
file.

A cost-observability tool that permanently inflated that block would corrupt the baseline it
exists to measure, and would show up in its own reports. So the integration consumes context
only when actively invoked (FR-055), and the tool measures and discloses its own footprint
rather than asserting it is negligible (FR-056, SC-017: under 0.5% of session cost).

## The `SessionEnd` hook — opt in, and it never analyses inline

The hook runs `ccaudit _enqueue`, which appends a queue entry, spawns a detached worker, and
exits in milliseconds.

It cannot do the analysis itself, for a documented reason: `SessionEnd` handlers share an
**overall budget capped at 60 seconds**, and a handler supplied by an installed plugin
**cannot raise that budget for itself**. A 30-second analysis would be silently cancelled on
someone else's machine.

The queue is the correctness guarantee; the detached worker is the latency optimization. If the
spawn fails, or the worker dies, or the platform cannot detach, the queue entry is still there
and the next invocation does the work. Nothing is lost.

Two further limits are worth knowing, and both are why automatic capture is a **convenience,
never the system of record**:

- `SessionEnd` **does not fire on compaction.** Compaction is an event *within* a continuing
  session, so a session that compacts five times still fires `SessionEnd` once. Compaction is
  reconstructed from the session's own records at analysis time, not observed via hooks.
- It also does not fire on a crash, a kill, or a closed terminal.

Every session remains fully analysable from its records afterwards regardless.

Failures are logged to `$CCAUDIT_HOME/ccaudit.log` and never surfaced into the user's session
(FR-054).

## Installing

```
/plugin marketplace add talafek96/ccaudit
/plugin install ccaudit
```

For local development, point Claude Code at this directory with `--plugin-dir`.

Removing the plugin leaves no trace in later sessions, and previously recorded results are
retained (FR-057).
