import json
import threading

import pytest

from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.tools.save import memory_save
from abeomem.tools.update import memory_update


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


def test_simple_field_update(db):
    s = _save(db)
    r = memory_update(
        db, session_id="sess", id=s["id"],
        patch={"title": "updated title"},
    )
    assert r["id"] == s["id"]
    row = db.execute("SELECT title FROM memo WHERE id = ?", (s["id"],)).fetchone()
    assert row["title"] == "updated title"

    evt = db.execute(
        "SELECT payload FROM memo_event WHERE action='update' AND memo_id=?",
        (s["id"],),
    ).fetchone()
    assert "title" in json.loads(evt["payload"])["fields"]


def test_null_means_unchanged(db):
    s = _save(db, title="original", symptom="original sym")
    r = memory_update(
        db, session_id="sess", id=s["id"],
        patch={"title": None, "notes": "added"},
    )
    assert "error" not in r
    row = db.execute("SELECT title, notes FROM memo WHERE id=?", (s["id"],)).fetchone()
    assert row["title"] == "original"
    assert row["notes"] == "added"


def test_empty_string_clears_optional(db):
    s = _save(db, notes="something")
    r = memory_update(
        db, session_id="sess", id=s["id"],
        patch={"notes": ""},
    )
    assert "error" not in r
    row = db.execute("SELECT notes FROM memo WHERE id=?", (s["id"],)).fetchone()
    assert row["notes"] == ""


def test_empty_string_for_required_field_invalid(db):
    s = _save(db)  # fix — cause is required
    r = memory_update(
        db, session_id="sess", id=s["id"],
        patch={"cause": ""},
    )
    assert r["error"]["code"] == "invalid_input"
    assert r["error"]["details"]["field"] == "cause"


def test_append_notes_on_empty(db):
    s = _save(db)
    memory_update(db, session_id="sess", id=s["id"], patch={"append_notes": "extra"})
    row = db.execute("SELECT notes FROM memo WHERE id=?", (s["id"],)).fetchone()
    assert row["notes"] == "extra"


def test_append_notes_on_existing(db):
    s = _save(db, notes="first paragraph   ")
    memory_update(
        db, session_id="sess", id=s["id"],
        patch={"append_notes": "   second paragraph"},
    )
    row = db.execute("SELECT notes FROM memo WHERE id=?", (s["id"],)).fetchone()
    assert row["notes"] == "first paragraph\n\nsecond paragraph"


def test_noop_update(db):
    s = _save(db, title="x")
    r1 = memory_update(db, session_id="sess", id=s["id"], patch={"title": "y"})
    # Second update with same content → noop
    r2 = memory_update(db, session_id="sess", id=s["id"], patch={"title": "y"})
    assert r2["updated_at"] == r1["updated_at"]
    evt = db.execute(
        "SELECT payload FROM memo_event WHERE action='update' AND memo_id=? "
        "ORDER BY id DESC LIMIT 1",
        (s["id"],),
    ).fetchone()
    assert json.loads(evt["payload"]).get("noop") is True


def test_archived_target_refused(db):
    s = _save(db)
    db.execute("UPDATE memo SET archived_at = CURRENT_TIMESTAMP WHERE id = ?", (s["id"],))
    r = memory_update(db, session_id="sess", id=s["id"], patch={"title": "new"})
    assert r["error"]["code"] == "invalid_input"
    assert "archived" in r["error"]["details"]["reason"]


def test_superseded_target_returns_superseded_target(db):
    a = _save(db, title="a")
    b = _save(db, title="b", supersedes=a["id"])
    r = memory_update(db, session_id="sess", id=a["id"], patch={"title": "new"})
    assert r["error"]["code"] == "superseded_target"
    assert r["error"]["details"]["tip_id"] == b["id"]


def test_no_fields_invalid(db):
    s = _save(db)
    r = memory_update(db, session_id="sess", id=s["id"], patch={})
    assert r["error"]["code"] == "invalid_input"


def test_notes_and_append_notes_mutex(db):
    s = _save(db)
    r = memory_update(
        db, session_id="sess", id=s["id"],
        patch={"notes": "a", "append_notes": "b"},
    )
    assert r["error"]["code"] == "invalid_input"


def test_concurrent_updates_both_succeed(tmp_path):
    """Two concurrent updates to different fields — retry path ensures both
    patches land."""
    db_path = tmp_path / "kb.db"
    bootstrap = get_connection(db_path)
    run_migrations(bootstrap, packaged_migrations_dir())
    s = memory_save(
        bootstrap, session_id="s", scope="global",
        data={
            "kind": "fix", "title": "original", "symptom": "sy",
            "cause": "ca", "solution": "so", "tags": [], "topics": [],
        },
    )
    bootstrap.close()

    barrier = threading.Barrier(2)
    results = []

    def worker(patch: dict):
        c = get_connection(db_path)
        try:
            barrier.wait()
            results.append(memory_update(
                c, session_id="sess", id=s["id"], patch=patch,
            ))
        finally:
            c.close()

    t1 = threading.Thread(target=worker, args=({"title": "new title"},))
    t2 = threading.Thread(target=worker, args=({"notes": "new notes"},))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert all("error" not in r for r in results), f"unexpected: {results}"
    final_conn = get_connection(db_path)
    row = final_conn.execute(
        "SELECT title, notes FROM memo WHERE id=?", (s["id"],)
    ).fetchone()
    final_conn.close()
    assert row["title"] == "new title"
    assert row["notes"] == "new notes"


def test_topic_normalization_on_update(db):
    s = _save(db)
    memory_update(
        db, session_id="sess", id=s["id"],
        patch={"topics": ["Python", "AsyncIO"]},
    )
    row = db.execute("SELECT topics FROM memo WHERE id=?", (s["id"],)).fetchone()
    assert json.loads(row["topics"]) == ["python", "asyncio"]
