"""memory_save tool (design.md §1.3.4).

T4.1 minimal implementation: no supersede, no dedup. T4.4 will add supersede
CAS; T4.5 will add dedup via RapidFuzz and hash short-circuit.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from abeomem.events import write_event
from abeomem.hashing import MemoFields, content_hash
from abeomem.tools import KIND_REQUIRED, VALID_KINDS, _nonempty, invalid
from abeomem.topics import normalize_topics


def _validate_save_input(data: dict[str, Any]) -> dict[str, Any] | None:
    kind = data.get("kind")
    if kind not in VALID_KINDS:
        return invalid("kind", f"must be one of {sorted(VALID_KINDS)}")
    if not _nonempty(data.get("title")):
        return invalid("title", "required, non-empty")
    if len(data["title"].split()) >= 16:
        return invalid("title", "<16 words")
    for f in KIND_REQUIRED[kind]:
        if not _nonempty(data.get(f)):
            return invalid(f, f"required for kind={kind}")
    return None


def memory_save(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    scope: str,
    data: dict[str, Any],
    source: str = "tool",
) -> dict[str, Any]:
    """Save a new memo and emit a save event. Returns {id, status}."""
    err = _validate_save_input(data)
    if err is not None:
        return err

    kind = data["kind"]
    title = data["title"].strip()
    topics = normalize_topics(data.get("topics") or [])
    tags = list(data.get("tags") or [])

    fields = MemoFields(
        kind=kind,
        title=title,
        symptom=data.get("symptom"),
        cause=data.get("cause"),
        solution=data.get("solution"),
        rule=data.get("rule"),
        rationale=data.get("rationale"),
        notes=data.get("notes"),
        topics=topics,
        tags=tags,
    )
    ch = content_hash(fields)

    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            INSERT INTO memo (scope, kind, title, symptom, cause, solution,
                              rule, rationale, notes, tags, topics, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope, kind, title,
                data.get("symptom"), data.get("cause"), data.get("solution"),
                data.get("rule"), data.get("rationale"), data.get("notes"),
                json.dumps(tags), json.dumps(topics), ch,
            ),
        )
        new_id = cur.lastrowid
        write_event(
            conn,
            action="save",
            session_id=session_id,
            memo_id=new_id,
            topics=topics,
            payload={"status": "created", "source": source},
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    return {"id": new_id, "status": "created"}
