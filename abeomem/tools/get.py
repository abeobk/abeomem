"""memory_get tool (design.md §1.3.3).

Fetches a memo by id. Bumps access_count/last_accessed_at. Returns superseded
and archived memos normally — search filters them, get does not.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from abeomem.events import write_event
from abeomem.tools import error


_SELECT_COLS = (
    "id", "scope", "kind", "title",
    "symptom", "cause", "solution", "rule", "rationale", "notes",
    "tags", "topics",
    "superseded_by", "archived_at",
    "useful_count", "access_count", "last_accessed_at",
    "created_at", "updated_at",
)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d: dict[str, Any] = {c: row[c] for c in _SELECT_COLS}
    d["tags"] = json.loads(d["tags"]) if d["tags"] else []
    d["topics"] = json.loads(d["topics"]) if d["topics"] else []
    return d


def memory_get(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    id: int,
) -> dict[str, Any]:
    """Fetch full memo by id. Returns the memo dict (with derived `supersedes`)
    or an error dict."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {', '.join(_SELECT_COLS)} FROM memo WHERE id = ?", (id,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return error("not_found", f"memo {id} does not exist", {"id": id})

        # Bump access before emitting event so the row returned is consistent
        conn.execute(
            "UPDATE memo SET access_count = access_count + 1, "
            "last_accessed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (id,),
        )

        # Build event payload flags
        payload: dict[str, Any] | None = {}
        if row["superseded_by"] is not None:
            payload["superseded"] = True
        if row["archived_at"] is not None:
            payload["archived"] = True
        if not payload:
            payload = None

        write_event(
            conn,
            action="get",
            session_id=session_id,
            memo_id=id,
            payload=payload,
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    result = _row_to_dict(row)
    # Derive `supersedes` (ancestor) at read time — not stored
    ancestor = conn.execute(
        "SELECT id FROM memo WHERE superseded_by = ? LIMIT 1", (id,)
    ).fetchone()
    result["supersedes"] = ancestor[0] if ancestor is not None else None
    return result
