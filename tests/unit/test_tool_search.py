import pytest

from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.tools.save import memory_save
from abeomem.tools.search import memory_search


@pytest.fixture
def db(tmp_path):
    conn = get_connection(tmp_path / "kb.db")
    run_migrations(conn, packaged_migrations_dir())
    yield conn
    conn.close()


def _save(db, scope="global", **kw):
    data = {
        "kind": "fix", "title": "t", "symptom": "s", "cause": "c", "solution": "sol",
        "tags": [], "topics": [],
    }
    data.update(kw)
    return memory_save(db, session_id="s", scope=scope, data=data)


def test_basic_term_match(db):
    a = _save(db, title="pnpm build fails", symptom="pnpm store corrupt")
    _save(db, title="ssl handshake error", symptom="ssl cert expired")
    r = memory_search(db, session_id="s", server_scope="global", query="pnpm")
    assert "error" not in r
    ids = [row["id"] for row in r["results"]]
    assert a["id"] in ids
    assert r["results"][0]["id"] == a["id"]


def test_useful_count_boost(db):
    a = _save(db, title="pnpm a")
    b = _save(db, title="pnpm b")
    db.execute("UPDATE memo SET useful_count = 5 WHERE id = ?", (b["id"],))
    r = memory_search(db, session_id="s", server_scope="global", query="pnpm")
    # b ranks first despite being saved later (identical FTS, useful wins)
    assert r["results"][0]["id"] == b["id"]


def test_superseded_not_returned(db):
    a = _save(db, title="old pnpm")
    b = _save(db, title="new pnpm")
    db.execute("UPDATE memo SET superseded_by = ? WHERE id = ?", (b["id"], a["id"]))
    r = memory_search(db, session_id="s", server_scope="global", query="pnpm")
    ids = [row["id"] for row in r["results"]]
    assert a["id"] not in ids
    assert b["id"] in ids


def test_archived_not_returned(db):
    a = _save(db, title="archived pnpm")
    db.execute("UPDATE memo SET archived_at = CURRENT_TIMESTAMP WHERE id = ?", (a["id"],))
    r = memory_search(db, session_id="s", server_scope="global", query="pnpm")
    assert all(row["id"] != a["id"] for row in r["results"])


def test_scope_repo_in_global_scope_returns_empty_with_warning(db):
    _save(db, title="global memo")
    r = memory_search(
        db, session_id="s", server_scope="global", query="global", scope="repo"
    )
    assert r["results"] == []
    assert "_warning" in r


def test_scope_repo_filters(db):
    _save(db, scope="global", title="global x")
    _save(db, scope="repo:abcdef0123456789", title="repo x")
    r = memory_search(
        db, session_id="s", server_scope="repo:abcdef0123456789",
        query="x", scope="repo",
    )
    kinds_of_scope = {
        db.execute("SELECT scope FROM memo WHERE id = ?", (row["id"],)).fetchone()[0]
        for row in r["results"]
    }
    assert kinds_of_scope == {"repo:abcdef0123456789"}


def test_scope_both_in_global_scope_degenerates(db):
    _save(db, scope="global", title="global y")
    r = memory_search(
        db, session_id="s", server_scope="global", query="y", scope="both"
    )
    assert len(r["results"]) == 1


def test_k_limits_results(db):
    for i in range(5):
        _save(db, title=f"pnpm {i}")
    r = memory_search(
        db, session_id="s", server_scope="global", query="pnpm", k=3
    )
    assert len(r["results"]) == 3


def test_invalid_kind(db):
    r = memory_search(db, session_id="s", server_scope="global", query="q", kind="note")
    assert r["error"]["details"]["field"] == "kind"


def test_empty_query_invalid(db):
    r = memory_search(db, session_id="s", server_scope="global", query="")
    assert r["error"]["details"]["field"] == "query"


def test_search_emits_event(db):
    _save(db, title="searchable")
    memory_search(db, session_id="sess-xyz", server_scope="global", query="searchable")
    row = db.execute(
        "SELECT action, session_id, query, payload FROM memo_event "
        "WHERE action = 'search' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["session_id"] == "sess-xyz"
    assert row["query"] == "searchable"
    import json
    payload = json.loads(row["payload"])
    assert payload["k"] == 8
    assert payload["returned"] >= 1
    assert payload["took_ms"] >= 0
