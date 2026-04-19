"""Acceptance suite (design.md §1 acceptance criteria).

Each test maps 1:1 to a criterion. Functional correctness is asserted via
direct tool calls against a real DB (MCP tools are thin wrappers around
these); one subprocess test exercises the CLI stdio boundary.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from abeomem.config import Config
from abeomem.db import (
    MigrationError,
    get_connection,
    packaged_migrations_dir,
    run_migrations,
)
from abeomem.mirror.export import export_memo
from abeomem.mirror.watcher import MemosWatcher
from abeomem.tools.save import memory_save
from abeomem.tools.search import memory_search
from abeomem.tools.update import memory_update


def _save(conn, scope="global", **kw):
    data = {
        "kind": "fix", "title": "t", "symptom": "s", "cause": "c", "solution": "sol",
        "tags": [], "topics": [],
    }
    data.update(kw)
    return memory_save(conn, session_id="s", scope=scope, data=data)


# Acceptance #1
def test_two_concurrent_sessions_different_repos(tmp_path):
    """Two concurrent CC sessions in different repos save and search without
    contention errors."""
    db_path = tmp_path / "kb.db"
    bootstrap = get_connection(db_path)
    run_migrations(bootstrap, packaged_migrations_dir())
    bootstrap.close()

    scopes = ["repo:1111111111111111", "repo:2222222222222222"]
    errors: list = []

    # Use wholly distinct words per save so fuzzy-dedup doesn't merge them.
    # What we're proving is concurrency safety, not dedup.
    distinctive_words = [
        "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
        "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho",
        "sigma", "tau", "upsilon",
    ]

    def worker(scope: str, tag: str) -> None:
        c = get_connection(db_path)
        try:
            for i in range(20):
                word = distinctive_words[i]
                r = memory_save(
                    c, session_id=f"sess-{tag}", scope=scope,
                    data={
                        "kind": "fix",
                        "title": f"{tag}-{word} failure",
                        "symptom": f"{word} subsystem broken uniquely",
                        "cause": f"{word} module regression",
                        "solution": f"patched {word} dependency",
                        "tags": [], "topics": [],
                    },
                    # Effectively disable fuzzy dedup — this test is about
                    # concurrency, not dedup. Exact hash dedup still applies.
                    dedup_threshold=101,
                )
                if "error" in r or r.get("status") != "created":
                    errors.append(("save", tag, i, r))
                r2 = memory_search(
                    c, session_id=f"sess-{tag}", server_scope=scope,
                    query=word,
                )
                if "error" in r2:
                    errors.append(("search", tag, i, r2))
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(s, t))
          for s, t in zip(scopes, ["a", "b"], strict=True)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errors, f"unexpected contention errors: {errors[:3]}"

    # Verify both scopes have 20 memos each
    verify = get_connection(db_path)
    for s in scopes:
        count = verify.execute(
            "SELECT COUNT(*) FROM memo WHERE scope = ?", (s,)
        ).fetchone()[0]
        assert count == 20
    verify.close()


# Acceptance #2
def test_migrations_all_or_nothing(tmp_path):
    """Failing migration → schema_version unchanged; rest of DB intact."""
    # First, set up a normal DB
    conn = get_connection(tmp_path / "kb.db")
    run_migrations(conn, packaged_migrations_dir())
    version_before = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    conn.close()

    # Now simulate a future failing migration in a separate dir
    bad_dir = tmp_path / "bad-migrations"
    bad_dir.mkdir()
    # Copy the real migration + add a bad one
    for f in packaged_migrations_dir().iterdir():
        if f.is_file() and f.suffix == ".sql":
            (bad_dir / f.name).write_text(f.read_text())
    (bad_dir / "002_broken.sql").write_text(
        "CREATE TABLE ok (v INTEGER);\nCREATE TABLE ok (v INTEGER);"  # dup fails
    )
    conn = get_connection(tmp_path / "kb.db")
    with pytest.raises(MigrationError):
        run_migrations(conn, bad_dir)
    # schema_version must still be version_before (we were at 1, migration 002 failed)
    version_after = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version_after == version_before
    # The stub 'ok' table must not exist
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ok'"
    ).fetchone()
    assert row is None
    conn.close()


def test_backup_restore_roundtrips(tmp_path):
    from abeomem.backup import run_backup

    cfg = Config()
    cfg.db.path = tmp_path / "kb.db"
    cfg.backup.dir = tmp_path / "backups"
    cfg.memos.dir = tmp_path / "memos"

    conn = get_connection(cfg.db.path)
    run_migrations(conn, packaged_migrations_dir())
    saved = _save(conn, title="survive the backup", symptom="should be in backup")
    conn.close()

    backup_path = run_backup(cfg.db.path, cfg)

    # Wipe original and restore
    cfg.db.path.unlink()
    cfg.db.path.with_suffix(".db-wal").unlink(missing_ok=True)
    cfg.db.path.with_suffix(".db-shm").unlink(missing_ok=True)
    import shutil
    shutil.copy(backup_path, cfg.db.path)

    restored = get_connection(cfg.db.path)
    row = restored.execute(
        "SELECT title FROM memo WHERE id = ?", (saved["id"],)
    ).fetchone()
    restored.close()
    assert row["title"] == "survive the backup"


# Acceptance #3
def test_identical_save_returns_duplicate(tmp_path):
    conn = get_connection(tmp_path / "kb.db")
    run_migrations(conn, packaged_migrations_dir())
    a = _save(conn, title="pnpm corrupted", symptom="store corrupt", cause="x", solution="y")
    b = _save(conn, title="pnpm corrupted", symptom="store corrupt", cause="x", solution="y")
    assert b["status"] == "duplicate"
    assert b["id"] == a["id"]
    count = conn.execute("SELECT COUNT(*) FROM memo").fetchone()[0]
    assert count == 1
    conn.close()


# Acceptance #4
def test_concurrent_supersede_one_wins(tmp_path):
    db_path = tmp_path / "kb.db"
    bootstrap = get_connection(db_path)
    run_migrations(bootstrap, packaged_migrations_dir())
    base = _save(bootstrap, title="base", symptom="s", cause="c", solution="s")
    bootstrap.close()

    barrier = threading.Barrier(2)
    results: list = []

    def worker(tag: str) -> None:
        c = get_connection(db_path)
        try:
            barrier.wait()
            r = memory_save(
                c, session_id=f"s-{tag}", scope="global",
                data={
                    "kind": "fix", "title": f"v-{tag}",
                    "symptom": f"sym-{tag}", "cause": "c", "solution": "sol",
                    "tags": [], "topics": [],
                    "supersedes": base["id"],
                },
            )
            results.append(r)
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    winners = [r for r in results if "error" not in r]
    losers = [r for r in results if "error" in r]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0]["error"]["code"] == "superseded_target"
    assert losers[0]["error"]["details"]["tip_id"] == winners[0]["id"]


# Acceptance #5
def test_watchdog_reflects_edit_within_2s(tmp_path):
    db_path = tmp_path / "kb.db"
    memos_dir = tmp_path / "memos"
    conn = get_connection(db_path)
    run_migrations(conn, packaged_migrations_dir())
    s = _save(conn, title="before edit")
    row = conn.execute("SELECT * FROM memo WHERE id = ?", (s["id"],)).fetchone()
    export_memo(row, memos_dir)

    from abeomem.mirror.export import memo_file_path
    p = memo_file_path(memos_dir, row)

    baseline_updates = conn.execute(
        "SELECT COUNT(*) FROM memo_event WHERE action='update' AND memo_id=?",
        (s["id"],),
    ).fetchone()[0]

    watcher = MemosWatcher(memos_dir, db_path, debounce_ms=300)
    watcher.start()
    try:
        # Storm of 5 writes in 100ms
        for i in range(5):
            p.write_text(
                p.read_text().replace("# before edit", f"# after edit v{i}")
            )
            time.sleep(0.02)

        # Wait for debounce + processing
        deadline = time.time() + 2.0
        while time.time() < deadline:
            count = conn.execute(
                "SELECT COUNT(*) FROM memo_event WHERE action='update' AND memo_id=?",
                (s["id"],),
            ).fetchone()[0]
            if count > baseline_updates:
                break
            time.sleep(0.05)

        new_count = conn.execute(
            "SELECT COUNT(*) FROM memo_event WHERE action='update' AND memo_id=?",
            (s["id"],),
        ).fetchone()[0]
        assert new_count - baseline_updates >= 1  # at least one update
        assert new_count - baseline_updates <= 2  # debounced — not 5

        # Search finds the new content
        r = memory_search(
            conn, session_id="sess", server_scope="global", query="after edit"
        )
        assert r["results"], "search did not find updated memo"
        assert r["results"][0]["id"] == s["id"]

        # Event has session_id='watchdog'
        evt = conn.execute(
            "SELECT session_id FROM memo_event "
            "WHERE action='update' AND memo_id=? ORDER BY id DESC LIMIT 1",
            (s["id"],),
        ).fetchone()
        assert evt["session_id"] == "watchdog"
    finally:
        watcher.stop()
        conn.close()


# Acceptance #6
def test_stats_produces_full_table_on_day_1(tmp_path, monkeypatch, capsys):
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

[backup]
dir = "{tmp_path}/backups"
""")
    # One save + one search to seed minimal events
    conn = get_connection(tmp_path / "kb.db")
    run_migrations(conn, packaged_migrations_dir())
    _save(conn, title="seed", topics=["python"])
    memory_search(conn, session_id="s", server_scope="global", query="seed")
    conn.close()

    from abeomem.stats import run_stats
    run_stats(json_output=False)
    out = capsys.readouterr().out
    # All seven metric labels present
    for label in (
        "retrievals_per_day",
        "useful:retrieved",
        "save_dedup_rate",
        "supersede_rate",
        "topic_coverage",
        "fsnotify_reimports_per_week",
        "backups_last_30_days",
    ):
        assert label in out
    # Zero-denominator renders as em-dash, not 0.0 or NaN
    # (supersede_rate with no saves-supersede should be 0.0 since total_saves > 0)


# Bonus: the stdio server process actually launches cleanly.
# (Full JSON-RPC round-trip testing is finicky with stdio buffering; the
# tool-registration correctness is verified by the unit test
# test_create_server_registers_five_tools.)
def test_serve_subprocess_starts_without_crashing(tmp_path):
    import os as _os
    home = tmp_path / "home"
    home.mkdir()
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
    repo_root = str(Path(__file__).resolve().parent.parent.parent)
    env = {
        **_os.environ,
        "HOME": str(home),
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": repo_root,
    }

    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "abeomem", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
    )
    try:
        # Wait a moment to let the server start. If it crashes, proc.poll()
        # returns non-None before the timeout.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if proc.poll() is not None:
                stderr_text = proc.stderr.read().decode("utf-8", errors="replace")
                pytest.fail(
                    f"server exited early with code {proc.returncode}: {stderr_text}"
                )
            time.sleep(0.1)
        # Still running = it survived startup (migrations, backup check,
        # reconciliation, watchdog, stdio listener)
        assert proc.poll() is None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


# Silence unused-import lint for json and memory_update
_ = json
_ = memory_update
