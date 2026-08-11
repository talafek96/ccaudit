# Contract — Claude Code plugin

A thin wrapper over the CLI. It adds no second code path and, critically, **no persistent resident
content** (FR-055).

## Layout

```text
src/ccaudit/plugin/
├── .claude-plugin/plugin.json     # name, description, version
├── commands/audit.md              # /ccaudit:audit — costs nothing until typed
├── skills/ccaudit/SKILL.md        # model-invocable; description resident, body on invocation
└── hooks/hooks.json               # optional SessionEnd capture
```

Verified against the current plugin documentation: directories live at the plugin root (not inside
`.claude-plugin/`), and skills are namespaced `/plugin-name:skill-name`.

## The footprint contract

| Surface | Resident cost when unused |
|---|---|
| Slash command | **Zero** — nothing until typed |
| Skill | Description only; body loads on invocation |
| `SessionEnd` hook | **Zero** — runs outside the conversation |
| MCP server | *Not used, by design* |

An MCP server's tool descriptions would sit in the resident context of **every** session,
permanently. Since resident tool descriptions are the single largest block of resident context —
roughly 50× a project's instruction file — a cost-observability tool that added to that block
would corrupt the baseline it exists to measure and appear in its own reports. Hence FR-055, and
hence FR-056: the tool must **measure and disclose its own footprint** rather than assert it is
negligible (SC-017: under 0.5% of session cost).

## Slash command — `/ccaudit:audit`

Analyses the **current** session, including while in progress (FR-051). Passes the session
identifier through from the invocation context; requires no arguments. Output is the terminal
summary, labelled provisional when the session is still running.

## Skill

Model-invocable so a natural-language question about session cost is answered from measured data
rather than estimated (FR-052). The skill shells out to the CLI; it does not reimplement anything.

## `SessionEnd` hook — opt-in

**Contract: enqueue and return. Never analyse inline.**

```jsonc
{ "hooks": { "SessionEnd": [{ "hooks": [{ "type": "command", "command": "ccaudit _enqueue" }] }] } }
```

`_enqueue` is internal: it appends a queue entry, spawns a detached worker, and exits — target
under 50 ms, hard ceiling well under the budget.

Three documented constraints make this the only viable shape:

1. `SessionEnd` handlers share an **overall budget capped at 60 seconds**, and a handler supplied
   by an installed plugin **cannot raise that budget for itself**. A 30-second analysis (SC-005)
   would be silently cancelled on someone else's machine.
2. `SessionEnd` **does not fire on compaction** — compaction is an event *within* a continuing
   session (`PreCompact`/`PostCompact`). A session that compacts five times still fires
   `SessionEnd` once. Compaction is reconstructed from `compactMetadata` at analysis time, not
   observed via hooks.
3. It also does not fire on a crash, a kill, or a closed terminal. Automatic capture is therefore
   a **convenience, never the system of record** — every session remains fully analysable from its
   records afterwards (FR-087).

Failures are logged, never surfaced into the user's session (FR-054).

### Race handling

The detached worker takes a claim keyed on `(session, fingerprint)` with an expiry. Because
analysis is a **pure function of the transcript**, a duplicate run wastes CPU and cannot produce a
different answer — so the obligations reduce to: never serve stale as fresh, never write a partial
result, never wait forever. See `data-model.md` invariants K1–K3.

## Distribution

```
/plugin marketplace add <owner>/ccaudit
/plugin install ccaudit
```

Local development: `--plugin-dir`. Removal leaves no trace in subsequent sessions while retaining
previously recorded results (FR-057, SC-019).
