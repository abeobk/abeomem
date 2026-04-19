"""T4.5: dedup via RapidFuzz fuzzy match + content_hash short-circuit."""

import json

import pytest

from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.tools.save import memory_save


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


def test_identical_save_returns_duplicate(db):
    a = _save(db, title="pnpm build fails", symptom="store corrupt")
    b = _save(db, title="pnpm build fails", symptom="store corrupt")
    assert b["id"] == a["id"]
    assert b["status"] == "duplicate"
    # Only one memo row exists
    count = db.execute("SELECT COUNT(*) FROM memo").fetchone()[0]
    assert count == 1


def test_near_duplicate_returns_duplicate(db):
    a = _save(
        db,
        title="asyncio CancelledError swallowed",
        symptom="asyncio CancelledError silently swallowed",
    )
    # Minor rewording — token_set_ratio should exceed 85
    b = _save(
        db,
        title="CancelledError swallowed in asyncio",
        symptom="CancelledError silently swallowed in asyncio",
    )
    assert b["status"] == "duplicate"
    assert b["id"] == a["id"]


def test_different_scope_accepted(db):
    a = _save(db, scope="global", title="pnpm fails", symptom="store corrupt")
    b = _save(db, scope="repo:abcdef0123456789", title="pnpm fails", symptom="store corrupt")
    assert b["status"] == "created"
    assert b["id"] != a["id"]


def test_dedup_ignores_superseded(db):
    a = _save(db, title="pnpm fails", symptom="store")
    c = _save(db, title="pnpm works", symptom="repaired")
    # Mark a as superseded by c so it is no longer "active"
    db.execute("UPDATE memo SET superseded_by = ? WHERE id = ?", (c["id"], a["id"]))
    # A re-save matching the now-superseded a must NOT dedup — a is inactive
    new = _save(db, title="pnpm fails", symptom="store different word here unique")
    # Spec: dedup checks only active memos. Confirm accepted as created.
    # (If token_set_ratio happens to match c, then duplicate is correct — we
    # constructed this test so the text differs enough from c.)
    # We just assert it is not a duplicate of a.
    if new["status"] == "duplicate":
        assert new["id"] != a["id"]
    else:
        assert new["status"] == "created"


def test_dedup_skipped_when_supersedes_set(db):
    # A fuzzy-near save WITHOUT supersede would return duplicate; WITH supersede
    # it is accepted as a new row (dedup is an explicit override).
    a = _save(db, title="asyncio CancelledError swallowed", symptom="silently swallowed")

    # Sanity: without supersede, this gets deduped
    dup = _save(
        db,
        title="CancelledError swallowed in asyncio",
        symptom="silently swallowed",
    )
    assert dup["status"] == "duplicate"
    assert dup["id"] == a["id"]

    # With supersede + content that's just different enough to not hit the
    # UNIQUE (scope, content_hash) invariant, the new row is accepted.
    b = _save(
        db,
        title="asyncio CancelledError bubbled up correctly",
        symptom="bubbled correctly with try/except",
        supersedes=a["id"],
    )
    assert b["status"] == "created"
    assert b["id"] != a["id"]


def test_duplicate_emits_event(db):
    a = _save(db)
    _save(db)  # duplicate
    rows = db.execute(
        "SELECT payload FROM memo_event WHERE action = 'save' AND memo_id = ? "
        "ORDER BY id",
        (a["id"],),
    ).fetchall()
    statuses = [json.loads(r["payload"])["status"] for r in rows]
    assert statuses == ["created", "duplicate"]
