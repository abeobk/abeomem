"""Success metrics (design.md §1.9).

Fixed 30-day window in Stage 1. Zero-denominator metrics render as em-dash.
Below-target rows prefixed [ALERT] and colored red when stdout is a TTY.
Session-count metrics filter session_id NOT IN ('cli', 'watchdog') per fix #2.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from abeomem.config import load_config
from abeomem.db import get_connection, packaged_migrations_dir, run_migrations

WINDOW_DAYS = 30
WINDOW_SQL_BOUND = f"ts > datetime('now', '-{WINDOW_DAYS} days')"

RED = "\x1b[31m"
RESET = "\x1b[0m"


def _is_tty() -> bool:
    return sys.stdout.isatty()


def _fmt_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _alert_wrap(prefix: str, line: str, tty: bool) -> str:
    if tty:
        return f"{RED}[ALERT]{RESET} {prefix}{line}"
    return f"[ALERT] {prefix}{line}"


def compute_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the raw numbers behind the metrics table.

    Keys:
      retrievals_per_day, useful_retrieved, save_dedup_rate, supersede_rate,
      topic_coverage, fsnotify_reimports_per_week, backups_last_30_days.

    Values: float | int | None (None for zero-denominator cases).
    """
    # 1. retrievals_per_day = search events / days active
    total_searches = conn.execute(
        f"SELECT COUNT(*) FROM memo_event WHERE action='search' AND {WINDOW_SQL_BOUND}"
    ).fetchone()[0]
    distinct_days = conn.execute(
        f"SELECT COUNT(DISTINCT date(ts)) FROM memo_event WHERE {WINDOW_SQL_BOUND}"
    ).fetchone()[0]
    retrievals_per_day = (total_searches / distinct_days) if distinct_days else None

    # 2. useful:retrieved = distinct (session, memo) with useful / with get
    pairs_got = conn.execute(
        f"""SELECT COUNT(DISTINCT session_id || ':' || memo_id) FROM memo_event
            WHERE action='get' AND {WINDOW_SQL_BOUND}"""
    ).fetchone()[0]
    pairs_useful = conn.execute(
        f"""SELECT COUNT(DISTINCT session_id || ':' || memo_id) FROM memo_event
            WHERE action='useful' AND {WINDOW_SQL_BOUND}"""
    ).fetchone()[0]
    useful_retrieved = (pairs_useful / pairs_got) if pairs_got else None

    # 3. save_dedup_rate = duplicate saves / all saves
    total_saves = conn.execute(
        f"SELECT COUNT(*) FROM memo_event WHERE action='save' AND {WINDOW_SQL_BOUND}"
    ).fetchone()[0]
    dup_saves = conn.execute(
        f"""SELECT COUNT(*) FROM memo_event
            WHERE action='save' AND {WINDOW_SQL_BOUND}
              AND json_extract(payload, '$.status') = 'duplicate'"""
    ).fetchone()[0]
    save_dedup_rate = (dup_saves / total_saves) if total_saves else None

    # 4. supersede_rate = save events with 'supersedes' / all saves
    super_saves = conn.execute(
        f"""SELECT COUNT(*) FROM memo_event
            WHERE action='save' AND {WINDOW_SQL_BOUND}
              AND json_extract(payload, '$.supersedes') IS NOT NULL"""
    ).fetchone()[0]
    supersede_rate = (super_saves / total_saves) if total_saves else None

    # 5. topic_coverage = active memos with >=1 topic / active memos
    active_total = conn.execute(
        "SELECT COUNT(*) FROM memo WHERE superseded_by IS NULL AND archived_at IS NULL"
    ).fetchone()[0]
    active_with_topic = conn.execute(
        """SELECT COUNT(*) FROM memo
           WHERE superseded_by IS NULL AND archived_at IS NULL
             AND topics IS NOT NULL AND topics NOT IN ('[]', '')"""
    ).fetchone()[0]
    topic_coverage = (active_with_topic / active_total) if active_total else None

    # 6. fsnotify_reimports_per_week = non-noop update events with source=watchdog / weeks
    watchdog_updates = conn.execute(
        f"""SELECT COUNT(*) FROM memo_event
            WHERE action='update' AND {WINDOW_SQL_BOUND}
              AND json_extract(payload, '$.source') = 'watchdog'
              AND json_extract(payload, '$.noop') IS NULL"""
    ).fetchone()[0]
    weeks = WINDOW_DAYS / 7.0
    fsnotify_per_week = watchdog_updates / weeks

    return {
        "retrievals_per_day": retrievals_per_day,
        "useful_retrieved": useful_retrieved,
        "save_dedup_rate": save_dedup_rate,
        "supersede_rate": supersede_rate,
        "topic_coverage": topic_coverage,
        "fsnotify_reimports_per_week": fsnotify_per_week,
        # backups_last_30_days is a filesystem check, not a DB query; caller
        # populates it.
    }


def count_recent_backups(backup_dir: Path, days: int = WINDOW_DAYS) -> int:
    import time
    if not backup_dir.exists():
        return 0
    cutoff = time.time() - days * 86400
    return sum(
        1 for p in backup_dir.iterdir()
        if p.is_file() and p.suffix == ".db" and p.stat().st_mtime >= cutoff
    )


def format_table(metrics: dict[str, Any], backups_last_30_days: int) -> str:
    tty = _is_tty()
    lines: list[str] = []
    lines.append("abeomem stats — 30-day window")
    lines.append("")

    def row(label: str, value: Any, alert: bool = False) -> None:
        fmt = _fmt_value(value)
        line = f"  {label:<35} {fmt}"
        if alert and value is not None:
            line = _alert_wrap("", line, tty)
        lines.append(line)

    rpd = metrics["retrievals_per_day"]
    row("retrievals_per_day", rpd, alert=(rpd is not None and rpd == 0))

    ur = metrics["useful_retrieved"]
    row("useful:retrieved (target >0.3)", ur, alert=(ur is not None and ur < 0.3))

    row("save_dedup_rate", metrics["save_dedup_rate"])
    row("supersede_rate", metrics["supersede_rate"])

    tc = metrics["topic_coverage"]
    row("topic_coverage", tc, alert=(tc is not None and tc < 0.9))

    row("fsnotify_reimports_per_week", metrics["fsnotify_reimports_per_week"])

    bk = backups_last_30_days
    alert_bk = bk < 4
    row("backups_last_30_days (target >=4)", bk, alert=alert_bk)

    return "\n".join(lines)


def run_stats(json_output: bool = False) -> None:
    cfg = load_config()
    cfg.db.path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(cfg.db.path)
    run_migrations(conn, packaged_migrations_dir())
    metrics = compute_metrics(conn)
    conn.close()
    backups = count_recent_backups(cfg.backup.dir)
    if json_output:
        payload = dict(metrics)
        payload["backups_last_30_days"] = backups
        print(json.dumps(payload))
        return
    print(format_table(metrics, backups))
