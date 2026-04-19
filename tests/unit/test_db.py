import sqlite3

from abeomem.db import get_connection


def test_pragmas_applied(tmp_path):
    db = tmp_path / "test.db"
    conn = get_connection(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_two_connections_concurrent_read(tmp_path):
    db = tmp_path / "test.db"
    c1 = get_connection(db)
    c1.execute("CREATE TABLE t (v INTEGER)")
    c1.execute("INSERT INTO t VALUES (1)")

    c2 = get_connection(db)
    # Both readers should see the row without contention.
    assert c1.execute("SELECT v FROM t").fetchone()[0] == 1
    assert c2.execute("SELECT v FROM t").fetchone()[0] == 1
    c1.close()
    c2.close()


def test_row_factory_is_Row(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute("CREATE TABLE t (name TEXT)")
    conn.execute("INSERT INTO t VALUES ('x')")
    row = conn.execute("SELECT name FROM t").fetchone()
    assert isinstance(row, sqlite3.Row)
    assert row["name"] == "x"
    conn.close()


def test_isolation_level_none(tmp_path):
    """isolation_level=None means autocommit; we run BEGIN IMMEDIATE manually."""
    conn = get_connection(tmp_path / "test.db")
    assert conn.isolation_level is None
    conn.close()
