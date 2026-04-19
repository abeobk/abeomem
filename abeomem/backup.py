"""Backup (design.md §1.8.1, fix #4).

Startup check is the real guarantor — CC sessions cycle and the in-process
timer rarely fires. Backup uses a dedicated connection opened at backup-time
and closed after; sharing the request connection would deadlock with in-flight
BEGIN IMMEDIATE transactions.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from abeomem.config import BackupConfig, Config
from abeomem.db import get_connection

logger = logging.getLogger(__name__)


def _timestamp_name() -> str:
    # Microsecond precision so rapid successive backups (e.g. in tests or
    # during migration windows) don't collide on a VACUUM INTO target.
    return datetime.now(UTC).strftime("kb-%Y%m%d-%H%M%S-%f.db")


def _rotate(backup_dir: Path, keep_count: int) -> None:
    files = sorted(
        (p for p in backup_dir.iterdir() if p.is_file() and p.suffix == ".db"),
        key=lambda p: p.stat().st_mtime,
    )
    while len(files) > keep_count:
        oldest = files.pop(0)
        try:
            oldest.unlink()
        except OSError as e:
            print(f"abeomem: could not delete old backup {oldest}: {e}", file=sys.stderr)


def run_backup(
    db_path: Path,
    cfg: Config,
    *,
    explicit_target: Path | None = None,
) -> Path | None:
    """Checkpoint WAL + VACUUM INTO a timestamped file. Returns the path.

    Opens a fresh connection; the default one may be held by an in-flight
    request. Checkpoint is mandatory before VACUUM INTO — without it, pages
    in the WAL are omitted from the backup.
    """
    backup_dir = cfg.backup.dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = explicit_target if explicit_target is not None else backup_dir / _timestamp_name()

    conn = get_connection(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()

    _rotate(backup_dir, cfg.backup.keep_count)
    return target


def newest_backup_age_seconds(backup_dir: Path) -> float | None:
    if not backup_dir.exists():
        return None
    files = [p for p in backup_dir.iterdir() if p.is_file() and p.suffix == ".db"]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return time.time() - newest.stat().st_mtime


def backup_needed(cfg: BackupConfig) -> bool:
    """True if the newest backup is older than interval_days (or none exists)."""
    age = newest_backup_age_seconds(cfg.dir)
    if age is None:
        return True
    return age > cfg.interval_days * 86400


def startup_backup_if_due(db_path: Path, cfg: Config) -> Path | None:
    """Synchronous guardian — run at `serve` startup before accepting tool calls.

    This is the real interval guarantor (fix #4). Named distinctly from
    run_backup() so the intent is obvious at call sites.
    """
    if not cfg.backup.enabled:
        return None
    if not backup_needed(cfg.backup):
        return None
    return run_backup(db_path, cfg)


async def backup_loop(db_path: Path, cfg: Config) -> None:
    """Background timer (fix #4). Sleeps interval_days × 86400, then runs
    backup via asyncio.to_thread so the blocking SQLite calls don't starve
    stdio.  Caller creates this as an asyncio.Task."""
    interval_s = cfg.backup.interval_days * 86400
    while True:
        await asyncio.sleep(interval_s)
        if not cfg.backup.enabled:
            continue
        try:
            await asyncio.to_thread(run_backup, db_path, cfg)
        except Exception as e:
            logger.warning("scheduled backup failed: %s", e)
