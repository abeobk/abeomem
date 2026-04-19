import sqlite3

import pytest

from abeomem.db import get_connection, packaged_migrations_dir, run_migrations


@pytest.fixture
def db(tmp_path):
    conn = get_connection(tmp_path / "kb.db")
    run_migrations(conn, packaged_migrations_dir())
    yield conn
    conn.close()


def _insert_memo(conn, **overrides):
    """Insert a minimal valid memo; return the new rowid."""
    row = {
        "scope": "global",
        "kind": "fix",
        "title": "t",
        "symptom": "s",
        "cause": "c",
        "solution": "sol",
        "rule": None,
        "rationale": None,
        "notes": None,
        "content_hash": b"\x00" * 32,
    }
    row.update(overrides)
    cur = conn.execute(
        """
        INSERT INTO memo (scope, kind, title, symptom, cause, solution, rule,
                          rationale, notes, content_hash)
        VALUES (:scope, :kind, :title, :symptom, :cause, :solution, :rule,
                :rationale, :notes, :content_hash)
        """,
        row,
    )
    return cur.lastrowid


def test_expected_tables_exist(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"memo", "memo_event", "schema_version", "migration_post_done"}.issubset(names)
    # memo_fts creates several shadow tables; main is memo_fts itself
    fts_tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memo_fts%'"
    )}
    assert "memo_fts" in {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE name='memo_fts'"
    )} or "memo_fts" in fts_tables


def test_reject_invalid_kind(db):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_memo(db, kind="note")


def test_reject_invalid_scope(db):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_memo(db, scope="foo")


def test_accept_valid_scope_forms(db):
    # global
    _insert_memo(db, scope="global", content_hash=b"\x01" * 32)
    # repo:<hex>
    _insert_memo(db, scope="repo:abcdef0123456789", content_hash=b"\x02" * 32)
    # repo:path:<hex>
    _insert_memo(db, scope="repo:path:1234abcdef567890", content_hash=b"\x03" * 32)


def test_fts_trigger_on_insert(db):
    mid = _insert_memo(db, title="asyncio CancelledError", solution="wrap in try")
    rows = list(db.execute(
        "SELECT rowid FROM memo_fts WHERE memo_fts MATCH 'asyncio'"
    ))
    assert any(r[0] == mid for r in rows)


def test_fts_trigger_on_update(db):
    mid = _insert_memo(db, title="old title")
    db.execute("UPDATE memo SET title = 'new title' WHERE id = ?", (mid,))
    # old term not found
    old = list(db.execute("SELECT rowid FROM memo_fts WHERE memo_fts MATCH 'old'"))
    assert not old
    new = list(db.execute("SELECT rowid FROM memo_fts WHERE memo_fts MATCH 'new'"))
    assert any(r[0] == mid for r in new)


def test_fts_trigger_on_delete(db):
    mid = _insert_memo(db, title="deleteme")
    db.execute("DELETE FROM memo WHERE id = ?", (mid,))
    rows = list(db.execute("SELECT rowid FROM memo_fts WHERE memo_fts MATCH 'deleteme'"))
    assert not rows


def test_unique_scope_content_hash(db):
    h = b"\xaa" * 32
    _insert_memo(db, content_hash=h)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_memo(db, content_hash=h)


def test_partial_index_on_active(db):
    # Just assert the index exists; the planner will use it automatically.
    rows = list(db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='memo_active'"
    ))
    assert len(rows) == 1
