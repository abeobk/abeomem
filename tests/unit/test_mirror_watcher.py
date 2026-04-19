import json
import time

import pytest

from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.mirror.export import export_memo, memo_file_path
from abeomem.mirror.watcher import MemosWatcher
from abeomem.tools.save import memory_save


@pytest.fixture
def db_and_dir(tmp_path):
    db_path = tmp_path / "kb.db"
    memos_dir = tmp_path / "memos"
    conn = get_connection(db_path)
    run_migrations(conn, packaged_migrations_dir())
    yield conn, db_path, memos_dir
    conn.close()


def _save(db, **kw):
    data = {
        "kind": "fix", "title": "t", "symptom": "s", "cause": "c", "solution": "sol",
        "tags": [], "topics": [],
    }
    data.update(kw)
    return memory_save(db, session_id="s", scope="global", data=data)


def _wait_for_event(db, memo_id, action="update", timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = db.execute(
            "SELECT COUNT(*) FROM memo_event WHERE memo_id = ? AND action = ?",
            (memo_id, action),
        ).fetchone()
        if row[0] > 0:
            return True
        time.sleep(0.05)
    return False


def test_external_edit_routed_through_update(db_and_dir):
    db, db_path, memos_dir = db_and_dir
    s = _save(db, title="hello world", symptom="original symptom")
    row = db.execute("SELECT * FROM memo WHERE id = ?", (s["id"],)).fetchone()
    export_memo(row, memos_dir)
    p = memo_file_path(memos_dir, row)

    watcher = MemosWatcher(memos_dir, db_path, debounce_ms=100)
    watcher.start()
    try:
        content = p.read_text()
        edited = content.replace("hello world", "goodbye world")
        p.write_text(edited)

        assert _wait_for_event(db, s["id"], action="update")
        row2 = db.execute("SELECT title FROM memo WHERE id = ?", (s["id"],)).fetchone()
        assert row2["title"] == "goodbye world"

        evt = db.execute(
            "SELECT session_id, payload FROM memo_event "
            "WHERE memo_id = ? AND action = 'update' ORDER BY id DESC LIMIT 1",
            (s["id"],),
        ).fetchone()
        assert evt["session_id"] == "watchdog"
        assert json.loads(evt["payload"])["source"] == "watchdog"
    finally:
        watcher.stop()


def test_debounces_rapid_writes(db_and_dir):
    db, db_path, memos_dir = db_and_dir
    s = _save(db, title="x")
    row = db.execute("SELECT * FROM memo WHERE id = ?", (s["id"],)).fetchone()
    export_memo(row, memos_dir)
    p = memo_file_path(memos_dir, row)

    baseline = db.execute(
        "SELECT COUNT(*) FROM memo_event WHERE action='update' AND memo_id=?",
        (s["id"],),
    ).fetchone()[0]

    watcher = MemosWatcher(memos_dir, db_path, debounce_ms=150)
    watcher.start()
    try:
        content = p.read_text()
        for i in range(5):
            p.write_text(content.replace("# x", f"# x{i}"))
            time.sleep(0.02)
        # Wait past debounce + processing
        time.sleep(0.8)
        count = db.execute(
            "SELECT COUNT(*) FROM memo_event WHERE action='update' AND memo_id=?",
            (s["id"],),
        ).fetchone()[0]
        # At most one new update (debounce collapsed the storm)
        assert count - baseline <= 1
    finally:
        watcher.stop()


def test_orphan_md_logged_not_written(db_and_dir, capsys):
    db, db_path, memos_dir = db_and_dir
    watcher = MemosWatcher(memos_dir, db_path, debounce_ms=50)
    watcher.start()
    try:
        fix_dir = memos_dir / "global" / "fix"
        fix_dir.mkdir(parents=True)
        orphan = fix_dir / "9999-bogus.md"
        orphan.write_text(
            "---\nid: 9999\nscope: global\nkind: fix\n---\n\n# bogus\n"
        )
        time.sleep(0.5)
        err = capsys.readouterr().err
        assert "orphan" in err
        # No new memo row
        count = db.execute("SELECT COUNT(*) FROM memo WHERE id = 9999").fetchone()[0]
        assert count == 0
    finally:
        watcher.stop()


def test_self_exported_file_no_update(db_and_dir):
    """Writing the same content back via export (our own write) must not
    trigger a noisy update event."""
    db, db_path, memos_dir = db_and_dir
    s = _save(db, title="noiseless")
    row = db.execute("SELECT * FROM memo WHERE id = ?", (s["id"],)).fetchone()
    export_memo(row, memos_dir)

    baseline_updates = db.execute(
        "SELECT COUNT(*) FROM memo_event WHERE action='update' AND memo_id=?",
        (s["id"],),
    ).fetchone()[0]

    watcher = MemosWatcher(memos_dir, db_path, debounce_ms=100)
    watcher.start()
    try:
        # Simulate a re-export writing identical content
        export_memo(row, memos_dir)
        time.sleep(0.5)
        count = db.execute(
            "SELECT COUNT(*) FROM memo_event WHERE action='update' AND memo_id=?",
            (s["id"],),
        ).fetchone()[0]
        assert count == baseline_updates
    finally:
        watcher.stop()
