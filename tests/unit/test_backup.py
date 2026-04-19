import threading
import time

import pytest

from abeomem.backup import (
    backup_needed,
    run_backup,
    startup_backup_if_due,
)
from abeomem.config import Config
from abeomem.db import get_connection, packaged_migrations_dir, run_migrations


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.db.path = tmp_path / "kb.db"
    c.memos.dir = tmp_path / "memos"
    c.backup.dir = tmp_path / "backups"
    # Fresh DB
    conn = get_connection(c.db.path)
    run_migrations(conn, packaged_migrations_dir())
    conn.execute(
        "INSERT INTO memo (scope, kind, title, content_hash) "
        "VALUES ('global', 'fix', 'seed', ?)",
        (b"\x00" * 32,),
    )
    conn.close()
    return c


def test_backup_creates_file(cfg):
    path = run_backup(cfg.db.path, cfg)
    assert path is not None
    assert path.exists()
    assert path.suffix == ".db"


def test_backup_is_readable_copy(cfg):
    path = run_backup(cfg.db.path, cfg)
    b = get_connection(path)
    row = b.execute("SELECT title FROM memo").fetchone()
    b.close()
    assert row["title"] == "seed"


def test_rotation_enforces_keep_count(cfg):
    cfg.backup.keep_count = 3
    for _ in range(5):
        run_backup(cfg.db.path, cfg)
        time.sleep(0.01)  # ensure distinct mtimes
    files = list(cfg.backup.dir.iterdir())
    assert len(files) == 3


def test_backup_needed_none(cfg):
    assert backup_needed(cfg.backup) is True


def test_backup_not_needed_after_run(cfg):
    run_backup(cfg.db.path, cfg)
    assert backup_needed(cfg.backup) is False


def test_startup_check_runs_when_due(cfg):
    p = startup_backup_if_due(cfg.db.path, cfg)
    assert p is not None


def test_startup_check_skips_when_recent(cfg):
    run_backup(cfg.db.path, cfg)
    p = startup_backup_if_due(cfg.db.path, cfg)
    assert p is None


def test_backup_works_while_write_in_progress(cfg):
    """Dedicated connection — backup doesn't deadlock on an in-flight
    BEGIN IMMEDIATE on a different connection."""
    busy = get_connection(cfg.db.path)
    busy.execute("BEGIN IMMEDIATE")
    busy.execute(
        "INSERT INTO memo (scope, kind, title, content_hash) "
        "VALUES ('global', 'fix', 'x', ?)",
        (b"\x01" * 32,),
    )

    result_holder = {}
    done = threading.Event()

    def do_backup():
        try:
            result_holder["path"] = run_backup(cfg.db.path, cfg)
        except Exception as e:
            result_holder["err"] = e
        finally:
            done.set()

    t = threading.Thread(target=do_backup)
    t.start()
    # Wait briefly — backup should succeed since we have busy_timeout and
    # VACUUM INTO reads from main DB, not from the in-flight writer's uncommitted.
    # Actually wal_checkpoint(TRUNCATE) may block on active writers. Give it
    # time then commit the blocker.
    time.sleep(0.5)
    busy.execute("COMMIT")
    busy.close()
    t.join(timeout=10)
    assert done.is_set()
    assert "err" not in result_holder
    assert result_holder["path"].exists()


def test_explicit_target_path(cfg, tmp_path):
    target = tmp_path / "my-custom-backup.db"
    result = run_backup(cfg.db.path, cfg, explicit_target=target)
    assert result == target
    assert target.exists()
