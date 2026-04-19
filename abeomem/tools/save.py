"""memory_save tool (design.md §1.3.4).

Covers: T4.1 core insert, T4.4 supersede CAS, T4.5 dedup.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from rapidfuzz import fuzz

from abeomem.events import write_event
from abeomem.hashing import MemoFields, content_hash
from abeomem.tools import KIND_REQUIRED, VALID_KINDS, _nonempty, error, invalid
from abeomem.topics import normalize_topics

DEFAULT_DEDUP_THRESHOLD = 85


def _primary_field(kind: str, data: dict[str, Any]) -> str:
    return (data.get("symptom") if kind in ("fix", "gotcha") else data.get("rule")) or ""


def _primary_field_row(row: sqlite3.Row) -> str:
    return (row["symptom"] if row["kind"] in ("fix", "gotcha") else row["rule"]) or ""


def _find_fuzzy_dup(
    conn: sqlite3.Connection,
    *,
    scope: str,
    title: str,
    primary: str,
    threshold: int,
) -> int | None:
    candidate = f"{title} {primary}".strip()
    rows = conn.execute(
        """
        SELECT id, kind, title, symptom, rule
          FROM memo
         WHERE scope = ?
           AND superseded_by IS NULL
           AND archived_at IS NULL
        """,
        (scope,),
    ).fetchall()
    for row in rows:
        existing = f"{row['title']} {_primary_field_row(row)}".strip()
        if fuzz.token_set_ratio(candidate, existing) >= threshold:
            return row["id"]
    return None


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


def _current_tip(conn: sqlite3.Connection, start_id: int, *, limit: int = 100) -> int:
    """Walk superseded_by chain to the current tip. CAS guarantees no cycles,
    but bound the walk anyway."""
    curr = start_id
    for _ in range(limit):
        row = conn.execute(
            "SELECT superseded_by FROM memo WHERE id = ?", (curr,)
        ).fetchone()
        if row is None or row["superseded_by"] is None:
            return curr
        curr = row["superseded_by"]
    raise RuntimeError(f"supersede chain from {start_id} exceeded {limit} hops")


def _emit_duplicate_event(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    existing_id: int,
    topics: list[str],
    source: str,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    write_event(
        conn,
        action="save",
        session_id=session_id,
        memo_id=existing_id,
        topics=topics,
        payload={"status": "duplicate", "source": source},
    )
    conn.execute("COMMIT")


def memory_save(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    scope: str,
    data: dict[str, Any],
    source: str = "tool",
    dedup_threshold: int = DEFAULT_DEDUP_THRESHOLD,
) -> dict[str, Any]:
    """Save a new memo (with optional supersede and dedup, §1.3.4).

    Returns {id, status='created'|'duplicate', supersedes?: int} or an error dict.
    """
    err = _validate_save_input(data)
    if err is not None:
        return err

    supersedes = data.get("supersedes")
    if supersedes is not None and not isinstance(supersedes, int):
        return invalid("supersedes", "must be int if present")

    if supersedes is not None:
        target = conn.execute(
            "SELECT id, superseded_by, archived_at FROM memo WHERE id = ?",
            (supersedes,),
        ).fetchone()
        if target is None:
            return error("not_found", f"memo {supersedes} does not exist",
                         {"id": supersedes})
        if target["superseded_by"] is not None or target["archived_at"] is not None:
            tip = _current_tip(conn, supersedes)
            return error(
                "superseded_target",
                f"memo {supersedes} is not a tip; current tip is {tip}",
                {"tip_id": tip},
            )

    kind = data["kind"]
    title = data["title"].strip()
    topics = normalize_topics(data.get("topics") or [])
    tags = list(data.get("tags") or [])

    # Dedup check — skipped when supersedes is set (explicit override).
    if supersedes is None:
        dup_id = _find_fuzzy_dup(
            conn,
            scope=scope,
            title=title,
            primary=_primary_field(kind, data),
            threshold=dedup_threshold,
        )
        if dup_id is not None:
            _emit_duplicate_event(
                conn, session_id=session_id, existing_id=dup_id,
                topics=topics, source=source,
            )
            return {"id": dup_id, "status": "duplicate"}

    fields = MemoFields(
        kind=kind, title=title,
        symptom=data.get("symptom"), cause=data.get("cause"),
        solution=data.get("solution"), rule=data.get("rule"),
        rationale=data.get("rationale"), notes=data.get("notes"),
        topics=topics, tags=tags,
    )
    ch = content_hash(fields)

    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
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
        except sqlite3.IntegrityError:
            # Either (scope, content_hash) UNIQUE or scope CHECK. Distinguish
            # by re-querying: if the exact row exists, it's dedup.
            conn.execute("ROLLBACK")
            existing = conn.execute(
                "SELECT id FROM memo WHERE scope = ? AND content_hash = ?",
                (scope, ch),
            ).fetchone()
            if existing is None:
                raise  # not a dedup — propagate the original IntegrityError
            _emit_duplicate_event(
                conn, session_id=session_id, existing_id=existing["id"],
                topics=topics, source=source,
            )
            return {"id": existing["id"], "status": "duplicate"}

        if supersedes is not None:
            cur = conn.execute(
                "UPDATE memo SET superseded_by = ? "
                "WHERE id = ? AND superseded_by IS NULL AND archived_at IS NULL",
                (new_id, supersedes),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                tip = _current_tip(conn, supersedes)
                return error(
                    "superseded_target",
                    f"memo {supersedes} was superseded by a concurrent save; "
                    f"current tip is {tip}",
                    {"tip_id": tip},
                )

        payload: dict[str, Any] = {"status": "created", "source": source}
        if supersedes is not None:
            payload["supersedes"] = supersedes
        write_event(
            conn,
            action="save",
            session_id=session_id,
            memo_id=new_id,
            topics=topics,
            payload=payload,
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    result: dict[str, Any] = {"id": new_id, "status": "created"}
    if supersedes is not None:
        result["supersedes"] = supersedes
    return result
