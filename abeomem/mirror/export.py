"""Markdown export (design.md §1.6).

Writes one file per memo under memos_dir with atomic temp-rename. On failure
the DB commit has already succeeded; we log a warning and let the next startup
reconciliation fix up.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from abeomem.slug import slugify

logger = logging.getLogger(__name__)

ARCHIVED_BANNER = (
    "> **⚠ This memo is archived.** Search never returns it. Kept on disk for "
    "audit and potential undelete via DB edit.\n"
)


def scope_dir_name(scope: str) -> str:
    """Translate DB scope id to a filesystem-safe directory name.

    Colons break on Windows and some sync services, so `repo:` → `repo-` and
    `repo:path:` → `repo-path-`. `global` is unchanged.
    """
    return scope.replace(":", "-")


def _row_to_frontmatter(row: sqlite3.Row, *, include_archived: bool = False) -> dict[str, Any]:
    fm: dict[str, Any] = {
        "id": row["id"],
        "scope": row["scope"],
        "kind": row["kind"],
        "topics": json.loads(row["topics"]) if row["topics"] else [],
        "tags": json.loads(row["tags"]) if row["tags"] else [],
        "useful_count": row["useful_count"],
        "created": row["created_at"],
        "updated": row["updated_at"],
    }
    if include_archived:
        fm["archived_at"] = row["archived_at"]
        # archived_reason is sourced from the archive event payload; exporters
        # that know it can pass via the extra_frontmatter arg.
    return fm


def _render_body(row: sqlite3.Row) -> str:
    parts: list[str] = [f"# {row['title']}\n"]
    kind = row["kind"]

    def add_field(label: str, value: str | None) -> None:
        if value is not None and value.strip() != "":
            parts.append(f"**{label}:** {value}\n")

    if kind in ("fix", "gotcha"):
        add_field("Symptom", row["symptom"])
        add_field("Cause", row["cause"])
        add_field("Solution", row["solution"])
    elif kind in ("convention", "decision"):
        add_field("Rule", row["rule"])
        add_field("Rationale", row["rationale"])

    if row["notes"] and row["notes"].strip() != "":
        parts.append("## Notes\n")
        parts.append(row["notes"].rstrip() + "\n")

    return "\n".join(parts)


def _find_existing_filename(target_dir: Path, memo_id: int) -> str | None:
    """If memo_id's file already exists, return its filename. Filename slugs
    never change (§1.6 filename stability)."""
    if not target_dir.exists():
        return None
    prefix = f"{memo_id}-"
    for p in target_dir.iterdir():
        if p.is_file() and p.name.startswith(prefix) and p.name.endswith(".md"):
            return p.name
    return None


def memo_file_path(
    memos_dir: Path,
    row: sqlite3.Row,
) -> Path:
    """Compute the .md file path for a memo. Preserves an existing filename's
    slug if already on disk."""
    scope_dir = memos_dir / scope_dir_name(row["scope"]) / row["kind"]
    existing = _find_existing_filename(scope_dir, row["id"])
    if existing is not None:
        return scope_dir / existing
    return scope_dir / f"{row['id']}-{slugify(row['title'])}.md"


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via temp-file + os.replace (§1.6)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f"{path.name}.tmp-", dir=str(path.parent), text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # Clean up leftover tmp file on failure
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def export_memo(
    row: sqlite3.Row,
    memos_dir: Path,
    *,
    archived_reason: str | None = None,
) -> bool:
    """Render memo row to its .md path. Returns True on success, False on
    non-fatal error (disk full, permission denied). Errors are logged to stderr;
    DB commit is already done when export is called (§1.6)."""
    try:
        path = memo_file_path(memos_dir, row)
        is_archived = row["archived_at"] is not None
        fm = _row_to_frontmatter(row, include_archived=is_archived)
        if is_archived and archived_reason is not None:
            fm["archived_reason"] = archived_reason

        fm_text = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False).rstrip()
        body = _render_body(row)
        if is_archived:
            content = f"---\n{fm_text}\n---\n\n{ARCHIVED_BANNER}\n{body}"
        else:
            content = f"---\n{fm_text}\n---\n\n{body}"

        _atomic_write(path, content)
        return True
    except OSError as e:
        print(
            f"abeomem: export failed for memo {row['id']}: {e}; "
            f"startup reconciliation will re-export.",
            file=sys.stderr,
        )
        return False
