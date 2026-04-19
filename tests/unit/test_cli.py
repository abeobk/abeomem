"""CLI smoke tests (T6.3). Unit tests call typer.testing.CliRunner on the
app; acceptance tests spawn subprocess `abeomem` for real stdio isolation."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect all abeomem paths into tmp_path by writing a config file."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cfg_dir = home / ".config" / "abeomem"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(f"""
[db]
path = "{tmp_path}/kb.db"

[memos]
dir = "{tmp_path}/memos"
fsnotify = false

[backup]
dir = "{tmp_path}/backups"
""")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def cli():
    # Import inside fixture so HOME env var applies to abeomem.config imports.
    from abeomem.cli import app
    return CliRunner(), app


def _save(env, **kw):
    """Save a memo via direct tool call (CLI save subcommand doesn't exist)."""
    from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
    from abeomem.tools.save import memory_save
    conn = get_connection(env / "kb.db")
    run_migrations(conn, packaged_migrations_dir())
    data = {
        "kind": "fix", "title": "t", "symptom": "s", "cause": "c",
        "solution": "sol", "tags": [], "topics": [],
    }
    data.update(kw)
    r = memory_save(conn, session_id="setup", scope="global", data=data)
    conn.close()
    return r


def test_scope_in_non_git(env, cli):
    runner, app = cli
    result = runner.invoke(app, ["scope"])
    assert result.exit_code == 0
    assert "global" in result.stdout


def test_ls_empty(env, cli):
    runner, app = cli
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0


def test_ls_json_output(env, cli):
    _save(env, title="first memo", topics=["python"])
    runner, app = cli
    result = runner.invoke(app, ["ls", "--scope", "global", "--json"])
    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["title"] == "first memo"
    assert parsed["topics"] == ["python"]


def test_topics_aggregate(env, cli):
    _save(env, title="a", topics=["python"])
    _save(env, title="b another unique", symptom="different xyz", topics=["python", "asyncio"])
    runner, app = cli
    result = runner.invoke(app, ["topics"])
    assert result.exit_code == 0
    assert "python" in result.stdout
    assert "asyncio" in result.stdout


def test_show_prints_markdown(env, cli):
    s = _save(env, title="visible memo")
    runner, app = cli
    result = runner.invoke(app, ["show", str(s["id"])])
    assert result.exit_code == 0
    assert "visible memo" in result.stdout
    assert "---" in result.stdout  # frontmatter


def test_show_nonexistent(env, cli):
    runner, app = cli
    result = runner.invoke(app, ["show", "9999"])
    assert result.exit_code == 1


def test_chain_root_to_tip(env, cli):
    a = _save(env, title="a")
    from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
    from abeomem.tools.save import memory_save
    conn = get_connection(env / "kb.db")
    run_migrations(conn, packaged_migrations_dir())
    b = memory_save(conn, session_id="s", scope="global", data={
        "kind": "fix", "title": "b", "symptom": "ss", "cause": "cc",
        "solution": "solution-b", "supersedes": a["id"], "tags": [], "topics": [],
    })
    conn.close()
    runner, app = cli
    result = runner.invoke(app, ["chain", str(b["id"])])
    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 2
    assert str(a["id"]) in lines[0]
    assert str(b["id"]) in lines[1]
    assert "tip" in lines[1]


def test_archive_excludes_from_ls(env, cli):
    s = _save(env, title="dying")
    runner, app = cli
    result = runner.invoke(app, ["archive", str(s["id"]), "--reason", "dup of X"])
    assert result.exit_code == 0

    # ls without --include-archived should not show it
    result = runner.invoke(app, ["ls", "--json"])
    ids = [json.loads(line)["id"] for line in result.stdout.splitlines() if line.strip()]
    assert s["id"] not in ids

    # File should have the archived banner now
    p = Path(env / "memos" / "global" / "fix" / f"{s['id']}-dying.md")
    assert p.exists()
    assert "⚠ This memo is archived" in p.read_text()


def test_sync_import_new_ingests_orphan(env, cli):
    """Manually create an orphan .md and verify sync --import-new ingests it."""
    memos = env / "memos" / "global" / "fix"
    memos.mkdir(parents=True)
    orphan = memos / "9999-hand-written.md"
    orphan.write_text(
        "---\nid: 9999\nscope: global\nkind: fix\n"
        "topics: []\ntags: []\n---\n\n"
        "# hand written memo\n\n"
        "**Symptom:** typed in obsidian\n\n"
        "**Cause:** direct editor use\n\n"
        "**Solution:** abeomem sync --import-new\n"
    )
    runner, app = cli
    result = runner.invoke(app, ["sync", "--import-new"])
    assert result.exit_code == 0
    # Should now be in DB with a real id
    from abeomem.db import get_connection
    conn = get_connection(env / "kb.db")
    rows = conn.execute("SELECT id, title FROM memo").fetchall()
    conn.close()
    titles = [r[1] for r in rows]
    assert "hand written memo" in titles
