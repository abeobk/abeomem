import pytest

from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.mirror.export import (
    export_memo,
    memo_file_path,
    scope_dir_name,
)
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


def _row(db, id):
    return db.execute("SELECT * FROM memo WHERE id = ?", (id,)).fetchone()


def test_scope_dir_translation():
    assert scope_dir_name("global") == "global"
    assert scope_dir_name("repo:abc123") == "repo-abc123"
    assert scope_dir_name("repo:path:abc123") == "repo-path-abc123"


def test_export_creates_file(db, tmp_path):
    s = _save(db, title="asyncio CancelledError")
    ok = export_memo(_row(db, s["id"]), tmp_path / "memos")
    assert ok
    p = tmp_path / "memos" / "global" / "fix" / f"{s['id']}-asyncio-cancellederror.md"
    assert p.exists()
    content = p.read_text()
    assert "---\n" in content
    assert "id: " + str(s["id"]) in content
    assert "# asyncio CancelledError" in content
    assert "**Symptom:**" in content


def test_frontmatter_roundtrip_via_yaml(db, tmp_path):
    import yaml

    s = _save(db, title="title", topics=["python", "asyncio"], tags=["urgent"])
    export_memo(_row(db, s["id"]), tmp_path / "memos")
    p = memo_file_path(tmp_path / "memos", _row(db, s["id"]))
    content = p.read_text()
    fm_block = content.split("---\n", 2)[1]
    fm = yaml.safe_load(fm_block)
    assert fm["id"] == s["id"]
    assert fm["kind"] == "fix"
    assert fm["scope"] == "global"
    assert fm["topics"] == ["python", "asyncio"]
    assert fm["tags"] == ["urgent"]


def test_filename_stability_on_rename(db, tmp_path):
    s = _save(db, title="original title")
    export_memo(_row(db, s["id"]), tmp_path / "memos")

    # Change the title
    db.execute("UPDATE memo SET title = 'completely different title' WHERE id = ?",
               (s["id"],))
    export_memo(_row(db, s["id"]), tmp_path / "memos")

    fix_dir = tmp_path / "memos" / "global" / "fix"
    files = list(fix_dir.iterdir())
    # Only one file — the original slug is preserved
    assert len(files) == 1
    assert "original-title" in files[0].name

    # But body reflects new title
    assert "completely different title" in files[0].read_text()


def test_archived_memo_has_banner(db, tmp_path):
    s = _save(db, title="dead memo")
    db.execute("UPDATE memo SET archived_at = CURRENT_TIMESTAMP WHERE id = ?",
               (s["id"],))
    export_memo(_row(db, s["id"]), tmp_path / "memos", archived_reason="dup of 89")
    p = memo_file_path(tmp_path / "memos", _row(db, s["id"]))
    content = p.read_text()
    assert "archived_at:" in content
    assert "archived_reason: dup of 89" in content
    assert "⚠ This memo is archived" in content


def test_export_failure_returns_false(db, tmp_path, monkeypatch):
    """If disk write fails, export returns False and doesn't raise."""
    s = _save(db)

    def boom(*a, **kw):
        raise OSError("disk full")

    # Patch os.fsync to simulate mid-write failure
    monkeypatch.setattr("abeomem.mirror.export.os.fsync", boom)
    ok = export_memo(_row(db, s["id"]), tmp_path / "memos")
    assert ok is False
    # No leftover .tmp file
    fix_dir = tmp_path / "memos" / "global" / "fix"
    if fix_dir.exists():
        leftovers = [p for p in fix_dir.iterdir() if ".tmp-" in p.name]
        assert leftovers == []


def test_export_atomic_writes_to_temp_first(db, tmp_path):
    """The tmp file goes to the same directory as the target so os.replace
    stays within a single filesystem."""
    s = _save(db)
    export_memo(_row(db, s["id"]), tmp_path / "memos")
    fix_dir = tmp_path / "memos" / "global" / "fix"
    # Only the final file, no lingering tmp
    names = [p.name for p in fix_dir.iterdir()]
    assert len(names) == 1
    assert names[0].endswith(".md")


def test_archived_roundtrip_then_unarchive(db, tmp_path):
    s = _save(db)
    # Archive
    db.execute("UPDATE memo SET archived_at = CURRENT_TIMESTAMP WHERE id = ?",
               (s["id"],))
    export_memo(_row(db, s["id"]), tmp_path / "memos", archived_reason="x")
    p = memo_file_path(tmp_path / "memos", _row(db, s["id"]))
    assert "archived_at:" in p.read_text()

    # Unarchive
    db.execute("UPDATE memo SET archived_at = NULL WHERE id = ?", (s["id"],))
    export_memo(_row(db, s["id"]), tmp_path / "memos")
    content = p.read_text()
    assert "⚠ This memo is archived" not in content
