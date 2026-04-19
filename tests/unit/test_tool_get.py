import json

import pytest

from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.tools.get import memory_get
from abeomem.tools.save import memory_save


@pytest.fixture
def db(tmp_path):
    conn = get_connection(tmp_path / "kb.db")
    run_migrations(conn, packaged_migrations_dir())
    yield conn
    conn.close()


def _save(db, **kw):
    data = {
        "kind": "fix", "title": "t", "symptom": "s", "cause": "c", "solution": "sol",
        "tags": [], "topics": [],
    }
    data.update(kw)
    return memory_save(db, session_id="s", scope="global", data=data)


def test_get_active_memo(db):
    saved = _save(db)
    got = memory_get(db, session_id="s", id=saved["id"])
    assert got["id"] == saved["id"]
    assert got["kind"] == "fix"
    assert got["title"] == "t"
    assert got["superseded_by"] is None
    assert got["supersedes"] is None
    assert got["archived_at"] is None


def test_get_bumps_access_count(db):
    saved = _save(db)
    got = memory_get(db, session_id="s", id=saved["id"])
    assert got["access_count"] == 0  # returned row is pre-bump snapshot
    # Confirm DB now has 1
    row = db.execute(
        "SELECT access_count, last_accessed_at FROM memo WHERE id = ?", (saved["id"],)
    ).fetchone()
    assert row["access_count"] == 1
    assert row["last_accessed_at"] is not None


def test_get_nonexistent(db):
    r = memory_get(db, session_id="s", id=9999)
    assert r["error"]["code"] == "not_found"
    assert r["error"]["details"]["id"] == 9999


def test_get_superseded_returns_row_and_flag(db):
    a = _save(db, title="original")
    b = _save(db, title="replacement")
    # Manually supersede (T4.4 adds proper tool path)
    db.execute("UPDATE memo SET superseded_by = ? WHERE id = ?", (b["id"], a["id"]))
    got = memory_get(db, session_id="s", id=a["id"])
    assert got["superseded_by"] == b["id"]
    # `supersedes` is for descendants (ancestor of `got`) — here the descendant is b
    # so `got.supersedes` should still be None (a has no ancestor)
    assert got["supersedes"] is None

    got_b = memory_get(db, session_id="s", id=b["id"])
    assert got_b["supersedes"] == a["id"]

    # Event payload for superseded id
    evt = db.execute(
        "SELECT payload FROM memo_event WHERE memo_id = ? AND action = 'get' "
        "ORDER BY id DESC LIMIT 1",
        (a["id"],),
    ).fetchone()
    assert json.loads(evt["payload"]) == {"superseded": True}


def test_get_archived_returns_row_and_flag(db):
    saved = _save(db)
    db.execute(
        "UPDATE memo SET archived_at = CURRENT_TIMESTAMP WHERE id = ?", (saved["id"],)
    )
    got = memory_get(db, session_id="s", id=saved["id"])
    assert got["archived_at"] is not None
    evt = db.execute(
        "SELECT payload FROM memo_event WHERE memo_id = ? AND action = 'get' "
        "ORDER BY id DESC LIMIT 1",
        (saved["id"],),
    ).fetchone()
    assert json.loads(evt["payload"]) == {"archived": True}
