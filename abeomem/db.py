"""SQLite connection helpers and migration runner.

Design refs: §1.2.1 bootstrap pragmas, §1.2.3 migration runner.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def get_connection(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with the four bootstrap pragmas from §1.2.1.

    WAL is per-database; the other three are per-connection. All four must be
    applied on every open. Returns a connection with isolation_level=None so
    callers manage BEGIN/COMMIT explicitly (required for BEGIN IMMEDIATE).
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn
