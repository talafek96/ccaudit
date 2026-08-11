# Contract — Report data

The single data shape produced by `--json`, inlined into the self-contained HTML report, and
served to the interactive UI. **One contract, three consumers** — this is what keeps the surfaces
from drifting and makes FR-074 (every figure obtainable from the terminal) structurally true
rather than a promise.

Versioned by `schema_version`; consumers reject an unknown major.

## Envelope

```jsonc
{
  "schema_version": "1.0",
  "generated_at": "…",
  "tool_version": "…",
  "cost_basis": "api_equivalent_estimate",   // never "billed" — FR-010
  "currency": "USD",
  "policy": "proportional",                   // carry-splitting policy in effect
  "redacted": false,

  "scope": {
    "sessions_included": ["…"],
    "sessions_excluded_count": 0,             // FR-063 — exclusion is part of the result
    "covered_through_turn": 62,
    "provisional": false,                     // true ⇒ session still in progress (FR-067)
    "producing_versions": ["…"]               // >1 ⇒ result spans versions (FR-028)
  },

  "totals": { … },
  "components": [ … ],
  "items": [ … ],
  "tree": { … },
  "turns": [ … ],
  "residency": [ … ],
  "invalidations": [ … ],
  "comparison": { … },
  "diagnostics": { … }
}
```

## `totals`

```jsonc
{
  "cost_micros": 1649000000,
  "attributed_micros": 1500000000,
  "unattributed_micros": 149000000,
  "unattributed_share": 0.0904,               // always present, never hidden — FR-012/013
  "tokens": { "fresh_input": 0, "cache_write": 0, "cache_read": 0, "output": 0 }
}
```

> **Consumer invariant.** `attributed + unattributed == cost_micros`, exact integer equality. A
> consumer that receives a payload failing this must refuse to render it rather than display
> numbers that do not add up.

## `components`

The four cost components, each carrying its mandated plain-language name **from the config that
defines it** — never re-typed by a renderer (Principle IX, FR-016).

```jsonc
[{
  "id": "cache_read",
  "technical_name": "cache_read_input_tokens",
  "plain_name": "Keeping context loaded",
  "description": "Charged every turn for re-showing everything already in the conversation.",
  "tokens": 589716189,
  "cost_micros": 883000000,
  "share": 0.536
}]
```

## `items`

One row per context item. Powers the leaderboard, the direct-vs-carry bars, and drill-down.

```jsonc
[{
  "item_id": "…",
  "kind": "instruction_file",                 // file | instruction_file | skill | tool_schema | mcp_schema | system_prompt | conversation
  "identity": "CLAUDE.md",
  "display": "CLAUDE.md",                     // redaction-aware
  "category": "docs",
  "size_tokens": 984,

  "direct_micros": 0,
  "carry_micros": 0,
  "total_micros": 0,
  "share": 0.0,

  "reads": 1,
  "turns_resident": 59,

  "lanes": {                                  // cost-model §5.2 — the honest cache story
    "cached_micros": 0,
    "uncached_micros": 0,                     // >0 ⇒ below the model's minimum, full rate every turn
    "loading_micros": 0
  },
  "never_cacheable_on": ["claude-opus-4-6"],  // FR-078 — surfaced, not buried

  "basis": "measured",
  "confidence": "medium",
  "per_session": [ { "session_id": "…", "total_micros": 0 } ]   // FR-064
}]
```

`never_cacheable_on` is a first-class field rather than a footnote because it can be a 10× per-turn
difference on the same file across models, and it is the kind of finding a reader must not have to
go looking for.

## `tree`

Folder hierarchy for the icicle/treemap. Each node carries **both** measures so the flat/total
toggle is a client-side switch, not a refetch.

```jsonc
{
  "name": "/", "path": "/",
  "flat_micros": 0,      // this node's own cost
  "total_micros": 0,     // including everything below it
  "share": 1.0,
  "children": [ … ]
}
```

An `unattributed` node is present at the root whenever the remainder is non-zero — the part-to-whole
views must show it (FR-040).

## `turns`

Per-turn accumulation, with compaction marked (FR-039).

```jsonc
[{
  "ordinal": 42,
  "model": "claude-opus-5",
  "cost_micros": 0,
  "components": { "fresh_input": 0, "cache_write": 0, "cache_read": 0, "output": 0 },
  "prompt_tokens": 0,                         // sum of all three input measures — FR-083
  "compaction": { "occurred": true, "pre_tokens": 0, "post_tokens": 0, "dropped_tokens": 0 }
}]
```

## `residency`

The timeline: one bar per span.

```jsonc
[{
  "item_id": "…",
  "display": "CLAUDE.md",
  "first_turn": 1,
  "last_turn": null,                          // null ⇒ still resident at session end
  "weight_tokens": 984,
  "end_reason": null,                         // evicted | invalidated | session_end | unknown
  "lane_by_turn": ["loading", "cached", "cached", … ]
}]
```

## `invalidations`

Forced reloads, charged to their cause rather than to the content re-loaded (FR-081). This is the
array that answers "what did that change cost me?"

```jsonc
[{
  "turn": 30,
  "tier": "tools",                            // tools | system | messages
  "trigger": "tool_set_changed",
  "detail": "MCP server 'playwright' added",
  "forced_reload_micros": 0,
  "items_reloaded": 47
}]
```

## `comparison`

The side-by-side instruction-vs-reads view, on a common scale (FR-037). Two series, one axis —
never two pie charts.

```jsonc
{
  "resident_instruction": [ { "label": "Tool + MCP schemas", "tokens": 51100, "cost_micros": 0, "share": 0.582 } ],
  "work_driven_reads":    [ { "label": "docs", "tokens": 24300, "cost_micros": 0, "share": 0.277 } ],
  "note": "Resident content is charged every turn; file reads are charged when read plus while resident."
}
```

## `diagnostics`

```jsonc
{
  "unparseable_records": 0,                   // FR-027 — counted, never silently skipped
  "anchor_reconciliation": [ { "anchor": "context_table", "delta_tokens": 0, "within_tolerance": true } ],
  "limitations": [
    "Injected instruction content is stripped before the transcript is written; some resident cost is provably absent from the source data."
  ],
  "estimated_figures": 0
}
```

`limitations` is required output, not optional garnish (FR-018): the JSONL under-reports exactly
the content under dispute, and a reader must be told that where it affects the figures.

## Redaction

With `--redact`, `display` is replaced by a stable pseudonym and `identity` is omitted. Costs,
shares, structure, and the tree shape are preserved so the report remains readable and the
argument still checkable (FR-043).
