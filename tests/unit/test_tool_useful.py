import pytest

from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.tools.save import memory_save
from abeomem.tools.useful import memory_useful


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


def test_increments_useful_count(db):
    s = _save(db)
    r = memory_useful(db, session_id="sess", id=s["id"])
    assert r["useful_count"] == 1
    r = memory_useful(db, session_id="sess", id=s["id"])
    assert r["useful_count"] == 2


def test_allowed_on_superseded(db):
    a = _save(db)
    b = _save(db, title="replacement", supersedes=a["id"])
    r = memory_useful(db, session_id="sess", id=a["id"])
    assert r["useful_count"] == 1
    # The superseded memo now has useful_count=1 but search still filters it
    assert b["id"] != a["id"]


def test_nonexistent(db):
    r = memory_useful(db, session_id="sess", id=9999)
    assert r["error"]["code"] == "not_found"


def test_emits_useful_event(db):
    s = _save(db)
    memory_useful(db, session_id="sess-xyz", id=s["id"])
    row = db.execute(
        "SELECT session_id, action, payload FROM memo_event "
        "WHERE action='useful' AND memo_id=?",
        (s["id"],),
    ).fetchone()
    assert row["session_id"] == "sess-xyz"
    assert row["action"] == "useful"
    assert row["payload"] is None
