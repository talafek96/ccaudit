"""The SQLite store: connection, schema management, and the single-transaction write.

Three things live here and nothing else. Queries and aggregation belong with the code that
asks the question, not in a general-purpose data-access layer (Principle II — no repository
abstraction the code does not need).

**Why the transaction is a first-class primitive.** A result written in pieces can be read as
complete while it is still half-computed, and a half-computed breakdown does not add up — the
one failure mode this project treats as a show-stopper (SC-001, invariant K2). So results are
written inside :func:`transaction`, which commits once or rolls back entirely.

**No setup step.** The state directory and the database are created on demand under
:func:`~claude_cost_tracker.config.ccost_home` on first use (FR-050). There is no config file to write
and no first-run wizard.

**Dedup is a shared responsibility.** ``UNIQUE(message_id, request_id)`` on ``turn`` fences the
resume/compact/fork double-count (FR-021), but SQLite treats NULLs as distinct in a unique
index, so turns missing either identifier are *not* fenced by the database. Ingest must
deduplicate those itself before insert.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from claude_cost_tracker.config import ccost_home

__all__ = [
    "DB_FILENAME",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "SchemaVersionError",
    "connect",
    "database_path",
    "transaction",
]

DB_FILENAME = "ccost.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Bumped whenever schema.sql changes shape. Mirrored into `PRAGMA user_version`, which is the
# only version marker: a schema_version table would be a second source for the same fact.
SCHEMA_VERSION = 2

# from-version -> statements that carry the database to from-version + 1. Empty at v1: the
# mechanism exists so the first real migration is a data entry, not a design decision made
# under pressure. A gap in the chain is a hard error, never a silent skip.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    # v1 -> v2: the cache. `contribution` holds the round-trippable conclusion and
    # `pricing_fingerprint` puts the rates into the key that decides whether it may be served
    # (invariant F3, FR-106). Both are nullable/defaulted, so an existing row survives the
    # migration and simply misses the cache until it is next computed.
    1: (
        "ALTER TABLE analysis_result ADD COLUMN pricing_fingerprint TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE analysis_result ADD COLUMN contribution BLOB",
    ),
}


class SchemaVersionError(RuntimeError):
    """Raised when the database on disk cannot be brought to :data:`SCHEMA_VERSION`.

    Fatal by design (Principle I). Reading a newer or unmigratable database with today's
    queries would return figures whose meaning we cannot vouch for.
    """


def database_path() -> Path:
    """Where the store lives by default — under the per-user state directory."""
    return ccost_home() / DB_FILENAME


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the store, creating the directory, the file, and the schema if absent.

    WAL journalling and foreign-key enforcement are on: WAL so a reader is never blocked by
    the writer during an analysis, foreign keys so an orphaned child row is an error rather
    than a row that quietly drops out of a join and shrinks a total.

    Idempotent — connecting to an existing database migrates it if needed and leaves its
    contents untouched.
    """
    target = database_path() if path is None else path
    target.parent.mkdir(parents=True, exist_ok=True)

    # isolation_level=None disables the driver's implicit transaction handling so that
    # `transaction()` is the single, explicit place a write boundary is drawn.
    conn = sqlite3.connect(target, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        _migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a write as one all-or-nothing unit: commit on success, roll back on any exception.

    ``BEGIN IMMEDIATE`` takes the write lock up front, so two concurrent analyses collide at
    the start rather than after both have done their work.

    Nesting raises: SQLite has no nested transactions, so an inner block that "committed"
    would in fact still be undone by the outer rollback — a partial write readable as
    complete, which is exactly what this guards against (invariant K2).
    """
    if conn.in_transaction:
        raise RuntimeError(
            "transaction() was entered while a transaction is already open on this connection. "
            "SQLite has no nested transactions, so the inner block could not commit "
            "independently; restructure the caller to open one transaction for the whole write."
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring the database to :data:`SCHEMA_VERSION`, creating it from scratch if empty."""
    version = _user_version(conn)

    if version == 0 and not _has_tables(conn):
        # One script, one transaction. `executescript` would implicitly commit an already-open
        # transaction, so the BEGIN/COMMIT are part of the script rather than around it: either
        # every table and the version marker land, or none of them do.
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(
            f"BEGIN;\n{schema_sql}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
        )
        return

    if version == SCHEMA_VERSION:
        return

    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"{_database_file(conn)} is at schema version {version}, but this build of claude-cost-tracker "
            f"understands version {SCHEMA_VERSION}. Upgrade ccost rather than reading it with "
            f"an older one."
        )

    while version < SCHEMA_VERSION:
        statements = _MIGRATIONS.get(version)
        if statements is None:
            raise SchemaVersionError(
                f"{_database_file(conn)} is at schema version {version} and no migration to "
                f"version {version + 1} is registered in claude_cost_tracker.store.db._MIGRATIONS. Refusing "
                f"to read it: the tables may not mean what the queries assume."
            )
        with transaction(conn):
            for statement in statements:
                conn.execute(statement)
            _set_user_version(conn, version + 1)
        version += 1


def _user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA does not accept bound parameters, so the value is interpolated. It is an int
    # from this module's own constants, never user input.
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _has_tables(conn: sqlite3.Connection) -> bool:
    """Whether the database holds any user table — distinguishes 'new' from 'version 0'."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


def _database_file(conn: sqlite3.Connection) -> str:
    """The main database's file path, for a failure message that names the offending input."""
    for row in conn.execute("PRAGMA database_list"):
        if row["name"] == "main":
            return str(row["file"] or "<in-memory>")
    return "<unknown>"
