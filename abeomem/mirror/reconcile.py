"""Startup reconciliation (design.md §1.6).

Runs after migrations+backup and before watchdog starts. Brings the markdown
mirror into agreement with the DB:
  - .md present, DB row exists, hash differs → route through memory_update
    (source='watchdog', session_id='watchdog'; external edits while the
    server was down are conceptually editor-driven).
  - DB row exists, .md missing → re-export (no event: repair, not user action).
  - Archived DB row, .md shows active content → re-export with banner.
  - Orphan .tmp-* files left by a crashed earlier run → remove.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from abeomem.hashing import MemoFields, content_hash
from abeomem.mirror.export import export_memo, memo_file_path, scope_dir_name
from abeomem.mirror.parse import parse_memo_file
from abeomem.tools.update import memory_update


def _clean_tmp_files(memos_dir: Path) -> None:
    if not memos_dir.exists():
        return
    for p in memos_dir.rglob("*.tmp-*"):
        try:
            p.unlink()
        except OSError as e:
            print(f"abeomem: could not remove stale tmp file {p}: {e}", file=sys.stderr)


def _find_md_files(memos_dir: Path) -> list[Path]:
    if not memos_dir.exists():
        return []
    return [p for p in memos_dir.rglob("*.md") if p.is_file()]


def _md_path_for_row(memos_dir: Path, row: sqlite3.Row) -> Path:
    return memo_file_path(memos_dir, row)


def reconcile(conn: sqlite3.Connection, memos_dir: Path) -> None:
    """Bring the mirror into agreement with the DB.

    Must run BEFORE the watchdog starts, else watchdog re-imports the
    reimports (§1.6 strict startup order).
    """
    memos_dir = Path(memos_dir)
    memos_dir.mkdir(parents=True, exist_ok=True)

    _clean_tmp_files(memos_dir)

    md_files = _find_md_files(memos_dir)
    # 1. Reconcile each .md against its DB row
    for path in md_files:
        parsed = parse_memo_file(path)
        if parsed is None:
            continue
        memo_id = parsed["id"]
        row = conn.execute("SELECT * FROM memo WHERE id = ?", (memo_id,)).fetchone()
        if row is None:
            # Orphan — leave alone, warn once
            print(
                f"abeomem: orphan .md {path.name} during reconciliation; "
                f"run `abeomem sync --import-new` to ingest",
                file=sys.stderr,
            )
            continue

        parsed_fields = MemoFields(
            kind=row["kind"],
            title=parsed.get("title") or "",
            symptom=parsed.get("symptom"),
            cause=parsed.get("cause"),
            solution=parsed.get("solution"),
            rule=parsed.get("rule"),
            rationale=parsed.get("rationale"),
            notes=parsed.get("notes"),
            topics=json.loads(row["topics"]) if row["topics"] else [],
            tags=json.loads(row["tags"]) if row["tags"] else [],
        )
        if content_hash(parsed_fields) == row["content_hash"]:
            continue  # already in sync

        patch = {
            k: parsed.get(k)
            for k in ("title", "symptom", "cause", "solution",
                      "rule", "rationale", "notes")
            if parsed.get(k) is not None
        }
        if not patch:
            continue
        result = memory_update(
            conn, session_id="watchdog", id=memo_id, patch=patch, source="watchdog",
        )
        if "error" in result:
            print(
                f"abeomem reconcile: update for {path.name} failed: "
                f"{result['error']}",
                file=sys.stderr,
            )

    # 2. Re-export DB rows that have no corresponding .md (repair, no event).
    all_rows = conn.execute(
        "SELECT * FROM memo"
    ).fetchall()
    for row in all_rows:
        target = _md_path_for_row(memos_dir, row)
        if target.exists():
            # If archived and the existing file has no banner, re-export.
            if row["archived_at"] is not None:
                content = target.read_text(encoding="utf-8", errors="ignore")
                if "⚠ This memo is archived" not in content:
                    export_memo(row, memos_dir)
            continue
        # Missing — repair
        export_memo(row, memos_dir)


__all__ = ["reconcile", "scope_dir_name"]
