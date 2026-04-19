import json
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


def _save(db, **overrides):
    data = {
        "kind": "fix",
        "title": "test memo",
        "symptom": "s",
        "cause": "c",
        "solution": "sol",
        "tags": [],
        "topics": [],
    }
    data.update(overrides)
    return memory_save(db, session_id="s1", scope="global", data=data)


def test_minimal_fix_saves(db):
    result = _save(db)
    assert "error" not in result
    assert result["status"] == "created"
    assert isinstance(result["id"], int)
    row = db.execute("SELECT * FROM memo WHERE id = ?", (result["id"],)).fetchone()
    assert row["kind"] == "fix"
    assert row["title"] == "test memo"
    assert row["scope"] == "global"


def test_fix_missing_cause_invalid(db):
    result = _save(db, cause=None)
    assert "error" in result
    assert result["error"]["code"] == "invalid_input"
    assert result["error"]["details"]["field"] == "cause"


def test_gotcha_needs_symptom(db):
    # gotcha with only symptom → OK
    r = _save(db, kind="gotcha", cause=None, solution=None, rule=None)
    assert r["status"] == "created"
    # gotcha missing symptom → invalid
    r = _save(db, kind="gotcha", symptom=None, cause=None, solution=None)
    assert r["error"]["details"]["field"] == "symptom"


def test_convention_needs_rule(db):
    r = _save(db, kind="convention", rule="use pnpm", symptom=None, cause=None, solution=None)
    assert r["status"] == "created"


def test_decision_needs_rule_and_rationale(db):
    r = _save(
        db,
        kind="decision",
        rule="use cockroach",
        rationale="compliance",
        symptom=None, cause=None, solution=None,
    )
    assert r["status"] == "created"
    r = _save(
        db,
        kind="decision",
        rule="use cockroach",
        rationale=None,
        symptom=None, cause=None, solution=None,
    )
    assert r["error"]["details"]["field"] == "rationale"


def test_invalid_kind(db):
    assert _save(db, kind="note")["error"]["details"]["field"] == "kind"


def test_title_over_15_words_invalid(db):
    long = " ".join(["w"] * 20)
    r = _save(db, title=long)
    assert r["error"]["details"]["field"] == "title"


def test_topics_normalized_on_save(db):
    r = _save(db, topics=["Python", "AsyncIO", "python"])
    row = db.execute("SELECT topics FROM memo WHERE id = ?", (r["id"],)).fetchone()
    assert json.loads(row["topics"]) == ["python", "asyncio"]


def test_save_emits_event(db):
    r = _save(db)
    evt = db.execute(
        "SELECT action, session_id, memo_id, payload FROM memo_event WHERE memo_id = ?",
        (r["id"],),
    ).fetchone()
    assert evt["action"] == "save"
    assert evt["session_id"] == "s1"
    payload = json.loads(evt["payload"])
    assert payload["status"] == "created"
    assert payload["source"] == "tool"


def test_concurrent_inserts_both_succeed(tmp_path):
    db_path = tmp_path / "kb.db"
    conn = get_connection(db_path)
    run_migrations(conn, packaged_migrations_dir())
    conn.close()

    results = []
    errors = []
    barrier = threading.Barrier(2)

    def worker(tag: str):
        c = get_connection(db_path)
        try:
            barrier.wait()
            r = memory_save(
                c,
                session_id=f"s-{tag}",
                scope="global",
                data={
                    "kind": "fix",
                    "title": f"memo {tag}",
                    "symptom": f"sym-{tag}",
                    "cause": f"c-{tag}",
                    "solution": f"sol-{tag}",
                    "tags": [],
                    "topics": [],
                },
            )
            results.append(r)
        except Exception as e:
            errors.append(e)
        finally:
            c.close()

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"unexpected errors: {errors}"
    assert len(results) == 2
    assert all(r["status"] == "created" for r in results)
    assert results[0]["id"] != results[1]["id"]
