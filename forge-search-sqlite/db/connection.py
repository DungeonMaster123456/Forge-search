"""
Shared SQLite connection helper for Forge-Search.

No server to install or run — the whole database is a single file,
created automatically on first use.

Location is controlled by the DB_PATH environment variable, defaulting
to forge_search.db in the project root.
"""
import os
import sqlite3

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "forge_search.db"),
)

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def get_connection():
    """
    Returns a sqlite3 connection with dict-like row access, and makes sure
    the schema exists (safe to call every time — uses IF NOT EXISTS).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    with open(_SCHEMA_PATH, "r") as f:
        conn.executescript(f.read())

    return conn


def get_dict_cursor(conn):
    """
    Kept for API parity with the old psycopg2 helper. sqlite3.Row already
    gives dict-like access (row["col"]), so this just returns a normal cursor.
    """
    return conn.cursor()
