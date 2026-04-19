"""Server bootstrap tests. MCP stdio integration is tested in acceptance."""

from pathlib import Path

import pytest

from abeomem.config import Config
from abeomem.server import bootstrap, create_server


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.db.path = tmp_path / "kb.db"
    c.memos.dir = tmp_path / "memos"
    c.backup.dir = tmp_path / "backups"
    c.memos.fsnotify = False  # disable watchdog for unit tests
    return c


def test_bootstrap_runs_migrations_and_reconcile(cfg):
    ctx = bootstrap(cfg)
    assert ctx.db_path == cfg.db.path
    assert cfg.db.path.exists()
    assert cfg.memos.dir.exists()
    # schema_version advanced
    import sqlite3
    conn = sqlite3.connect(cfg.db.path)
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    conn.close()
    assert version >= 1


def test_bootstrap_scope_from_cwd(cfg, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # non-git dir
    ctx = bootstrap(cfg)
    assert ctx.scope == "global"


def test_create_server_registers_five_tools(cfg):
    cfg.memos.fsnotify = False
    mcp, ctx, watcher = create_server(cfg)
    try:
        import asyncio
        tools = asyncio.run(mcp.list_tools())
        names = {t.name for t in tools}
        assert {
            "memory_search", "memory_get", "memory_save",
            "memory_update", "memory_useful",
        }.issubset(names)
    finally:
        watcher.stop()


def test_bootstrap_idempotent(cfg):
    bootstrap(cfg)
    ctx2 = bootstrap(cfg)  # second call must not error
    assert Path(ctx2.db_path).exists()
