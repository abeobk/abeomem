import pytest

from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.mirror.export import export_memo, memo_file_path
from abeomem.mirror.reconcile import reconcile
from abeomem.tools.save import memory_save


@pytest.fixture
def db_dir(tmp_path):
    conn = get_connection(tmp_path / "kb.db")
    run_migrations(conn, packaged_migrations_dir())
    yield conn, tmp_path / "memos"
    conn.close()


def _save(db, **kw):
    data = {
        "kind": "fix", "title": "t", "symptom": "s", "cause": "c", "solution": "sol",
        "tags": [], "topics": [],
    }
    data.update(kw)
    return memory_save(db, session_id="s", scope="global", data=data)


def _row(db, id):
    return db.execute("SELECT * FROM memo WHERE id = ?", (id,)).fetchone()


def test_offline_edit_picked_up_on_startup(db_dir):
    db, memos_dir = db_dir
    s = _save(db, title="before")
    export_memo(_row(db, s["id"]), memos_dir)

    # Edit the file while the "server" is off
    p = memo_file_path(memos_dir, _row(db, s["id"]))
    p.write_text(p.read_text().replace("# before", "# after-offline"))

    reconcile(db, memos_dir)
    row = db.execute("SELECT title FROM memo WHERE id = ?", (s["id"],)).fetchone()
    assert row["title"] == "after-offline"


def test_missing_md_reexported(db_dir):
    db, memos_dir = db_dir
    s = _save(db)
    # Do not export; there is no .md
    reconcile(db, memos_dir)
    p = memo_file_path(memos_dir, _row(db, s["id"]))
    assert p.exists()


def test_leftover_tmp_cleaned(db_dir):
    db, memos_dir = db_dir
    s = _save(db)
    export_memo(_row(db, s["id"]), memos_dir)
    fix_dir = memos_dir / "global" / "fix"
    (fix_dir / "junk.md.tmp-12345").write_text("partial")
    reconcile(db, memos_dir)
    # Tmp file gone, real file still there
    assert not (fix_dir / "junk.md.tmp-12345").exists()
    assert any(p.suffix == ".md" for p in fix_dir.iterdir())


def test_archived_memo_with_active_md_gets_banner(db_dir):
    db, memos_dir = db_dir
    s = _save(db, title="dead")
    export_memo(_row(db, s["id"]), memos_dir)
    # Now archive in DB (simulating archive CLI) without re-export
    db.execute(
        "UPDATE memo SET archived_at = CURRENT_TIMESTAMP WHERE id = ?",
        (s["id"],),
    )
    # File still has active frontmatter; reconcile should re-export with banner
    reconcile(db, memos_dir)
    content = memo_file_path(memos_dir, _row(db, s["id"])).read_text()
    assert "⚠ This memo is archived" in content


def test_orphan_md_left_alone(db_dir, capsys):
    db, memos_dir = db_dir
    fix_dir = memos_dir / "global" / "fix"
    fix_dir.mkdir(parents=True)
    (fix_dir / "9999-orphan.md").write_text(
        "---\nid: 9999\nscope: global\nkind: fix\n---\n\n# orphan\n"
    )
    reconcile(db, memos_dir)
    # Still there, no DB row
    assert (fix_dir / "9999-orphan.md").exists()
    assert db.execute("SELECT COUNT(*) FROM memo WHERE id = 9999").fetchone()[0] == 0
    err = capsys.readouterr().err
    assert "orphan" in err
