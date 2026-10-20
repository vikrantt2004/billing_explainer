"""SQLite connection handling and schema setup.

Everything above this layer talks to a `sqlite3.Connection`; only this module knows
where the file lives. Swapping to Supabase/Postgres later means replacing this module
and repository.py, not the parsers or the UI.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 2  # 2: documents.cloud_provider


def connect(db_path: "str | Path") -> sqlite3.Connection:
    """Open a connection with the settings this app depends on."""
    # isolation_level=None -> we control BEGIN/COMMIT ourselves (see transaction()).
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Foreign-key enforcement is OFF by default in SQLite and is per-connection,
    # so it must be switched on for every connection we open.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(db_path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if missing. Safe to run repeatedly."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade databases created by older versions. CREATE TABLE IF NOT EXISTS never
    alters an existing table, so new columns must be added explicitly. Each step checks
    first, so running this repeatedly (or on a fresh database) is harmless."""
    doc_columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
    if "cloud_provider" not in doc_columns:  # v1 -> v2
        conn.execute("ALTER TABLE documents ADD COLUMN cloud_provider TEXT")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All-or-nothing block. Any exception rolls back every write inside it.

    If a transaction is already open (a caller wrapped several repository calls),
    we join it instead of nesting: the outermost block decides commit/rollback.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")  # take the write lock up front
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


if __name__ == "__main__":  # python -m billing_explainer.database
    from billing_explainer.config import load_settings

    settings = load_settings()
    connection = connect(settings.db_path)
    init_db(connection)
    connection.close()
    print(f"Database ready at {settings.db_path}")
