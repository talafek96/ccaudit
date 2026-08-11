-- ccaudit store schema, version 1.
--
-- Derived from specs/001-per-file-cost-attribution/data-model.md. The spine of the model is
-- the split between Charge (observed, never adjusted) and Attribution (derived, must sum back
-- to the observed total). Constraints below encode the data-model invariants by their names
-- (C1, L1, A1..A3, F1/F2, K1..K3) so a violation is a database error, not a wrong number.
--
-- Conventions:
--   * Money is INTEGER micro-dollars. Never a float in a stored or compared column.
--   * Token counts are INTEGER and non-negative.
--   * Timestamps are TEXT, ISO-8601 UTC ('2026-08-11T12:34:56Z'), so they sort lexically.
--   * Booleans are INTEGER 0/1 (SQLite has no boolean type).
--   * Enumerations are TEXT with a CHECK list; the authoritative Python-side registries are
--     ccaudit.config.components and ccaudit.config.categories (Principle IX). A value added
--     there must be added here in the same change.

-- One recorded conversation. `is_complete` drives the provisional label (FR-067).
CREATE TABLE IF NOT EXISTS session (
    session_id        TEXT PRIMARY KEY,
    project_path      TEXT,
    transcript_path   TEXT,
    started_at        TEXT,
    ended_at          TEXT,
    producing_version TEXT,
    is_complete       INTEGER NOT NULL DEFAULT 0 CHECK (is_complete IN (0, 1))
);

-- One exchange; the unit at which cost is observed and residency is evaluated.
-- Carries the dedup invariant: transcripts repeat the same assistant message across resume,
-- compact, and fork, so `(message_id, request_id)` is UNIQUE (FR-021, PITFALLS).
CREATE TABLE IF NOT EXISTS turn (
    turn_id        TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES session (session_id) ON DELETE CASCADE,
    ordinal        INTEGER NOT NULL CHECK (ordinal >= 0),
    message_id     TEXT,
    request_id     TEXT,
    model          TEXT NOT NULL,
    cache_ttl      TEXT CHECK (cache_ttl IS NULL OR cache_ttl IN ('5m', '1h', 'unknown')),
    is_sidechain   INTEGER NOT NULL DEFAULT 0 CHECK (is_sidechain IN (0, 1)),
    parent_turn_id TEXT REFERENCES turn (turn_id) ON DELETE SET NULL,
    -- SQLite treats NULLs as distinct in a UNIQUE index, so this constraint only fences turns
    -- that actually carry both identifiers. Records missing either one must be deduplicated in
    -- application code before insert (ingest owns that; see db.py notes).
    UNIQUE (message_id, request_id)
);

CREATE INDEX IF NOT EXISTS turn_by_session ON turn (session_id, ordinal);
CREATE INDEX IF NOT EXISTS turn_by_parent ON turn (parent_turn_id);

-- What the API billed, one row per component per turn, read straight from `usage`.
-- Invariant C1: the three input components sum to the turn's total prompt size; `fresh_input`
-- alone is never the prompt size (FR-083).
CREATE TABLE IF NOT EXISTS charge (
    turn_id      TEXT NOT NULL REFERENCES turn (turn_id) ON DELETE CASCADE,
    component    TEXT NOT NULL
                 CHECK (component IN ('fresh_input', 'cache_write', 'cache_read', 'output')),
    tokens       INTEGER NOT NULL CHECK (tokens >= 0),
    cost_micros  INTEGER NOT NULL CHECK (cost_micros >= 0),
    PRIMARY KEY (turn_id, component)
);

-- Anything occupying context space and therefore incurring cost. Files are one kind.
-- `project_path` namespaces identity so the same path in two projects is two items.
CREATE TABLE IF NOT EXISTS context_item (
    item_id      TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN (
                     'file', 'instruction_file', 'skill', 'tool_schema',
                     'mcp_schema', 'system_prompt', 'conversation')),
    identity     TEXT NOT NULL,
    project_path TEXT,
    category     TEXT NOT NULL
                 CHECK (category IN ('docs', 'source', 'spec', 'skill', 'schema', 'other')),
    size_tokens  INTEGER NOT NULL CHECK (size_tokens >= 0)
);

CREATE INDEX IF NOT EXISTS context_item_by_identity ON context_item (identity, project_path);
CREATE INDEX IF NOT EXISTS context_item_by_category ON context_item (category);

-- One event placing an item into the conversation; the origin of *direct* cost.
-- A file read, edited, and read again produces distinct injections, not one continuing span.
CREATE TABLE IF NOT EXISTS injection (
    injection_id TEXT PRIMARY KEY,
    turn_id      TEXT NOT NULL REFERENCES turn (turn_id) ON DELETE CASCADE,
    item_id      TEXT NOT NULL REFERENCES context_item (item_id) ON DELETE CASCADE,
    cause        TEXT NOT NULL CHECK (cause IN (
                     'tool_result', 'attachment', 'skill_listing', 'deferred_tools_delta',
                     'at_mention', 'session_start', 'compact_reinjection')),
    tool_use_id  TEXT,
    size_tokens  INTEGER NOT NULL CHECK (size_tokens >= 0)
);

CREATE INDEX IF NOT EXISTS injection_by_turn ON injection (turn_id);
CREATE INDEX IF NOT EXISTS injection_by_item ON injection (item_id);
CREATE INDEX IF NOT EXISTS injection_by_tool_use ON injection (tool_use_id);

-- The interval an item remained available; the origin of *carry* cost.
-- `last_turn` is NULL while the item is still resident.
CREATE TABLE IF NOT EXISTS residency_span (
    span_id      TEXT PRIMARY KEY,
    injection_id TEXT NOT NULL REFERENCES injection (injection_id) ON DELETE CASCADE,
    item_id      TEXT NOT NULL REFERENCES context_item (item_id) ON DELETE CASCADE,
    first_turn   INTEGER NOT NULL CHECK (first_turn >= 0),
    last_turn    INTEGER CHECK (last_turn IS NULL OR last_turn >= first_turn),
    end_reason   TEXT CHECK (end_reason IS NULL OR end_reason IN (
                     'evicted', 'invalidated', 'session_end', 'unknown')),
    -- A span that has ended says why; an open span has neither a last turn nor a reason.
    CHECK ((last_turn IS NULL) = (end_reason IS NULL))
);

CREATE INDEX IF NOT EXISTS residency_span_by_item ON residency_span (item_id, first_turn);
CREATE INDEX IF NOT EXISTS residency_span_by_injection ON residency_span (injection_id);

-- Which pricing lane a resident item sat in, per turn. The cost-model bridge that stops
-- sub-threshold content from being mispriced.
-- Invariant L1: `uncached` requires size_tokens < threshold(turn.model), read from config —
-- never inferred from model ordering, which is not monotonic. Enforced in application code.
CREATE TABLE IF NOT EXISTS cache_lane (
    turn_id     TEXT NOT NULL REFERENCES turn (turn_id) ON DELETE CASCADE,
    item_id     TEXT NOT NULL REFERENCES context_item (item_id) ON DELETE CASCADE,
    lane        TEXT NOT NULL CHECK (lane IN ('cached', 'uncached', 'loading')),
    lane_reason TEXT NOT NULL CHECK (lane_reason IN (
                    'cacheable', 'below_minimum', 'first_load', 'reload_forced')),
    PRIMARY KEY (turn_id, item_id)
);

CREATE INDEX IF NOT EXISTS cache_lane_by_item ON cache_lane (item_id, lane);

-- A prefix-tier change that forced content to be re-loaded. First-class so the reload is
-- charged to the change that caused it, not to the content re-written (FR-081).
CREATE TABLE IF NOT EXISTS invalidation_event (
    event_id             TEXT PRIMARY KEY,
    turn_id              TEXT NOT NULL REFERENCES turn (turn_id) ON DELETE CASCADE,
    tier                 TEXT NOT NULL CHECK (tier IN ('tools', 'system', 'messages')),
    -- Quoted: TRIGGER is a reserved word in SQL. The data-model field name is kept as-is
    -- rather than renamed, so the column reads the same as the spec.
    "trigger"            TEXT NOT NULL CHECK ("trigger" IN (
                             'tool_set_changed', 'model_switched', 'instruction_changed')),
    detail               TEXT NOT NULL,
    forced_reload_micros INTEGER NOT NULL CHECK (forced_reload_micros >= 0)
);

CREATE INDEX IF NOT EXISTS invalidation_event_by_turn ON invalidation_event (turn_id);

-- What we concluded, with its own provenance. Every figure says how it was derived and how
-- much to trust it — `basis` and `confidence` are NOT NULL with no default (FR-014).
-- Invariant A1: per session, SUM(cost_micros) equals the session total by exact integer
-- equality, with `unattributed` absorbing the residual. Cross-row, so application-enforced.
-- Invariant A2 (no output charged to an item) is enforced here as a CHECK.
CREATE TABLE IF NOT EXISTS attribution (
    attribution_id TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES session (session_id) ON DELETE CASCADE,
    turn_id        TEXT REFERENCES turn (turn_id) ON DELETE CASCADE,
    target_kind    TEXT NOT NULL
                   CHECK (target_kind IN ('item', 'invalidation_event', 'prompt',
                                          'unattributed')),
    target_id      TEXT,
    component      TEXT NOT NULL
                   CHECK (component IN ('direct', 'carry', 'overhead', 'output')),
    cost_micros    INTEGER NOT NULL,
    basis          TEXT NOT NULL CHECK (basis IN ('exact', 'measured', 'estimated')),
    confidence     TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    source_refs    TEXT NOT NULL,
    -- A target is named unless the row *is* the unattributed remainder.
    CHECK ((target_kind = 'unattributed') = (target_id IS NULL)),
    -- Invariant A2: output is never charged to a context item (FR-005).
    CHECK (NOT (component = 'output' AND target_kind = 'item'))
);

CREATE INDEX IF NOT EXISTS attribution_by_session ON attribution (session_id, component);
CREATE INDEX IF NOT EXISTS attribution_by_target ON attribution (target_kind, target_id);
CREATE INDEX IF NOT EXISTS attribution_by_turn ON attribution (turn_id);

-- A completed analysis of a session's records under one splitting policy.
-- Invariant F1: current only while `fingerprint` matches the transcript's present fingerprint.
-- Invariant F2: UNIQUE(session_id, fingerprint, policy) — re-running over unchanged records
-- creates no second entry (FR-094).
CREATE TABLE IF NOT EXISTS analysis_result (
    result_id            TEXT PRIMARY KEY,
    session_id           TEXT NOT NULL REFERENCES session (session_id) ON DELETE CASCADE,
    fingerprint          TEXT NOT NULL,
    covered_through_turn INTEGER NOT NULL CHECK (covered_through_turn >= 0),
    policy               TEXT NOT NULL,
    producing_version    TEXT,
    tool_version         TEXT,
    computed_at          TEXT NOT NULL,
    UNIQUE (session_id, fingerprint, policy)
);

CREATE INDEX IF NOT EXISTS analysis_result_by_fingerprint ON analysis_result (fingerprint);
CREATE INDEX IF NOT EXISTS analysis_result_by_session ON analysis_result (session_id, computed_at);

-- Who is analysing a given (session, fingerprint), so concurrent runs do not duplicate work.
-- Invariant K1: a claim past `expires_at` is reclaimable by anyone, with no manual cleanup —
-- expiry is the crash-recovery mechanism, which is why `expires_at` is NOT NULL and indexed.
CREATE TABLE IF NOT EXISTS claim (
    session_id  TEXT NOT NULL REFERENCES session (session_id) ON DELETE CASCADE,
    fingerprint TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('queued', 'running', 'done')),
    claimed_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    pid         TEXT NOT NULL,
    host        TEXT NOT NULL,
    -- A lease that expires before it is taken is not a lease.
    CHECK (expires_at > claimed_at),
    PRIMARY KEY (session_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS claim_by_expiry ON claim (expires_at);

-- Records that could not be used, counted and sampled rather than silently dropped
-- (FR-026, FR-027). Surfaced in the run summary.
CREATE TABLE IF NOT EXISTS ingest_diagnostic (
    session_id TEXT NOT NULL REFERENCES session (session_id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN (
                   'unparseable', 'unrecognised_version', 'anchor_mismatch')),
    count      INTEGER NOT NULL CHECK (count >= 0),
    sample     TEXT,
    PRIMARY KEY (session_id, kind)
);
