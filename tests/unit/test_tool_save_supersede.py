"""T4.4: supersede via CAS, including race between two concurrent saves."""

import threading

import pytest

from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
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


def test_normal_supersede(db):
    a = _save(db, title="old")
    b = _save(db, title="new", supersedes=a["id"])
    assert b["status"] == "created"
    assert b["supersedes"] == a["id"]
    row_a = db.execute("SELECT superseded_by FROM memo WHERE id = ?", (a["id"],)).fetchone()
    assert row_a["superseded_by"] == b["id"]


def test_supersede_nonexistent(db):
    r = _save(db, title="x", supersedes=9999)
    assert r["error"]["code"] == "not_found"


def test_supersede_already_superseded(db):
    a = _save(db)
    b = _save(db, title="b", supersedes=a["id"])
    r = _save(db, title="c", supersedes=a["id"])
    assert r["error"]["code"] == "superseded_target"
    assert r["error"]["details"]["tip_id"] == b["id"]


def test_supersede_archived_returns_superseded_target(db):
    a = _save(db)
    db.execute("UPDATE memo SET archived_at = CURRENT_TIMESTAMP WHERE id = ?", (a["id"],))
    r = _save(db, title="b", supersedes=a["id"])
    assert r["error"]["code"] == "superseded_target"


def test_chain_walks_to_current_tip(db):
    a = _save(db)
    b = _save(db, title="b", supersedes=a["id"])
    c = _save(db, title="c", supersedes=b["id"])
    # Trying to supersede the root should report the tip = c
    r = _save(db, title="d", supersedes=a["id"])
    assert r["error"]["details"]["tip_id"] == c["id"]


def test_supersede_race_only_one_wins(tmp_path):
    """Acceptance #4: two concurrent supersede calls — exactly one wins,
    loser gets superseded_target with winner's id."""
    db_path = tmp_path / "kb.db"
    bootstrap = get_connection(db_path)
    run_migrations(bootstrap, packaged_migrations_dir())
    base = memory_save(
        bootstrap, session_id="s", scope="global",
        data={
            "kind": "fix", "title": "base", "symptom": "s", "cause": "c",
            "solution": "sol", "tags": [], "topics": [],
        },
    )
    bootstrap.close()

    results: list[dict] = []
    barrier = threading.Barrier(2)

    def worker(tag: str):
        c = get_connection(db_path)
        try:
            barrier.wait()
            r = memory_save(
                c, session_id=f"s-{tag}", scope="global",
                data={
                    "kind": "fix", "title": f"v-{tag}",
                    "symptom": "s", "cause": "c", "solution": "sol",
                    "tags": [], "topics": [],
                    "supersedes": base["id"],
                },
            )
            results.append(r)
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    winners = [r for r in results if "error" not in r]
    losers = [r for r in results if "error" in r]
    assert len(winners) == 1, f"expected one winner; got {results}"
    assert len(losers) == 1
    assert losers[0]["error"]["code"] == "superseded_target"
    assert losers[0]["error"]["details"]["tip_id"] == winners[0]["id"]
