import pytest

from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.mirror.export import export_memo, memo_file_path
from abeomem.mirror.parse import parse_memo_file
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


def test_roundtrip_basic(db, tmp_path):
    s = _save(
        db,
        title="asyncio CancelledError",
        symptom="silently swallowed",
        cause="by spec",
        solution="wrap in try/except",
        notes="extra detail\nmore detail",
    )
    memos = tmp_path / "memos"
    export_memo(_row(db, s["id"]), memos)
    p = memo_file_path(memos, _row(db, s["id"]))
    parsed = parse_memo_file(p)
    assert parsed is not None
    assert parsed["id"] == s["id"]
    assert parsed["kind"] == "fix"
    assert parsed["title"] == "asyncio CancelledError"
    assert parsed["symptom"] == "silently swallowed"
    assert parsed["cause"] == "by spec"
    assert parsed["solution"] == "wrap in try/except"
    assert "extra detail" in parsed["notes"]


def test_title_edit_in_body_reflected(db, tmp_path):
    s = _save(db, title="original")
    memos = tmp_path / "memos"
    export_memo(_row(db, s["id"]), memos)
    p = memo_file_path(memos, _row(db, s["id"]))

    content = p.read_text()
    edited = content.replace("# original", "# edited in editor")
    p.write_text(edited)

    parsed = parse_memo_file(p)
    assert parsed["title"] == "edited in editor"


def test_id_mismatch_filename_wins(db, tmp_path, capsys):
    s = _save(db)
    memos = tmp_path / "memos"
    export_memo(_row(db, s["id"]), memos)
    p = memo_file_path(memos, _row(db, s["id"]))

    content = p.read_text()
    # Corrupt the frontmatter id
    bad = content.replace(f"id: {s['id']}", "id: 9999")
    p.write_text(bad)

    parsed = parse_memo_file(p)
    assert parsed["id"] == s["id"]
    err = capsys.readouterr().err
    assert "id mismatch" in err


def test_malformed_frontmatter_returns_none(db, tmp_path, capsys):
    p = tmp_path / "42-bad.md"
    p.write_text("no fences here")
    assert parse_memo_file(p) is None
    assert "frontmatter" in capsys.readouterr().err


def test_archived_banner_stripped_from_body(db, tmp_path):
    s = _save(db, title="dead")
    memos = tmp_path / "memos"
    db.execute("UPDATE memo SET archived_at = CURRENT_TIMESTAMP WHERE id = ?",
               (s["id"],))
    export_memo(_row(db, s["id"]), memos, archived_reason="dup")
    p = memo_file_path(memos, _row(db, s["id"]))
    parsed = parse_memo_file(p)
    assert parsed is not None
    assert parsed["title"] == "dead"
    # The banner text should not leak into notes or any content field
    assert "⚠" not in (parsed.get("notes") or "")
    assert parsed.get("archived_at") is not None
