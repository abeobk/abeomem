import json

import pytest

from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.events import write_event


@pytest.fixture
def db(tmp_path):
    conn = get_connection(tmp_path / "kb.db")
    run_migrations(conn, packaged_migrations_dir())
    yield conn
    conn.close()


def test_search_event_valid(db):
    eid = write_event(
        db,
        action="search",
        session_id="abc",
        query="pnpm",
        topics=["python"],
        payload={"k": 8, "returned": 3, "took_ms": 12},
    )
    row = db.execute("SELECT * FROM memo_event WHERE id = ?", (eid,)).fetchone()
    assert row["action"] == "search"
    assert row["session_id"] == "abc"
    assert row["query"] == "pnpm"
    assert json.loads(row["topics"]) == ["python"]
    assert json.loads(row["payload"]) == {"k": 8, "returned": 3, "took_ms": 12}


def test_unknown_action_raises(db):
    with pytest.raises(ValueError, match="unknown action"):
        write_event(db, action="explode", session_id="s")


def test_save_missing_status_raises(db):
    with pytest.raises(ValueError, match="status"):
        write_event(db, action="save", session_id="s", memo_id=1, payload={"source": "tool"})


def test_search_k_wrong_type_raises(db):
    with pytest.raises(ValueError, match="k"):
        write_event(
            db,
            action="search",
            session_id="s",
            payload={"k": "8", "returned": 3, "took_ms": 1},
        )


def test_get_payload_can_be_none(db):
    write_event(db, action="get", session_id="s", memo_id=1, payload=None)


def test_get_payload_archived_flag(db):
    write_event(db, action="get", session_id="s", memo_id=1, payload={"archived": True})


def test_get_payload_rejects_extra_key(db):
    with pytest.raises(ValueError, match="unexpected"):
        write_event(db, action="get", session_id="s", memo_id=1, payload={"foo": True})


def test_save_duplicate_status(db):
    write_event(
        db,
        action="save",
        session_id="s",
        memo_id=1,
        payload={"status": "duplicate", "source": "tool"},
    )


def test_save_supersedes_int(db):
    write_event(
        db,
        action="save",
        session_id="s",
        memo_id=2,
        payload={"status": "created", "source": "tool", "supersedes": 1},
    )


def test_update_noop_payload(db):
    write_event(
        db,
        action="update",
        session_id="s",
        memo_id=1,
        payload={"noop": True, "source": "tool"},
    )


def test_update_fields_payload(db):
    write_event(
        db,
        action="update",
        session_id="s",
        memo_id=1,
        payload={"fields": ["title", "notes"], "source": "watchdog"},
    )


def test_update_both_fields_and_noop_raises(db):
    with pytest.raises(ValueError, match="exactly one"):
        write_event(
            db,
            action="update",
            session_id="s",
            memo_id=1,
            payload={"fields": ["title"], "noop": True, "source": "tool"},
        )


def test_update_wrong_source_raises(db):
    with pytest.raises(ValueError, match="source"):
        write_event(
            db,
            action="update",
            session_id="s",
            memo_id=1,
            payload={"fields": ["title"], "source": "cli"},  # update is tool/watchdog only
        )


def test_useful_payload_must_be_none(db):
    write_event(db, action="useful", session_id="s", memo_id=1)
    with pytest.raises(ValueError, match="None"):
        write_event(db, action="useful", session_id="s", memo_id=1, payload={})


def test_archive_source_must_be_cli(db):
    write_event(
        db,
        action="archive",
        session_id="cli",
        memo_id=1,
        payload={"reason": "dup of 42", "source": "cli"},
    )
    with pytest.raises(ValueError):
        write_event(
            db,
            action="archive",
            session_id="s",
            memo_id=1,
            payload={"source": "tool"},
        )


def test_session_id_stored_verbatim(db):
    for sid in ("cli", "watchdog", "0a1b2c3d-abcd-ef01-2345-6789abcdef01"):
        write_event(db, action="useful", session_id=sid, memo_id=1)
    sids = {r[0] for r in db.execute("SELECT session_id FROM memo_event")}
    assert {"cli", "watchdog", "0a1b2c3d-abcd-ef01-2345-6789abcdef01"}.issubset(sids)
