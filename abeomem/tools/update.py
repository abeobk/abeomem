"""memory_update tool (design.md §1.3.5).

Implements fix #1 (two-attempt CAS with re-fetch + re-merge on retry) and
fix #3 (append_notes concatenation).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from abeomem.events import write_event
from abeomem.hashing import MemoFields, content_hash
from abeomem.tools import KIND_REQUIRED, _nonempty, error, invalid
from abeomem.topics import normalize_topics

UPDATABLE_STR_FIELDS = (
    "title", "symptom", "cause", "solution", "rule", "rationale", "notes"
)
UPDATABLE_LIST_FIELDS = ("tags", "topics")
ALL_UPDATABLE = UPDATABLE_STR_FIELDS + UPDATABLE_LIST_FIELDS + ("append_notes",)


def _row_to_content_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = {f: row[f] for f in UPDATABLE_STR_FIELDS}
    d["tags"] = json.loads(row["tags"]) if row["tags"] else []
    d["topics"] = json.loads(row["topics"]) if row["topics"] else []
    d["kind"] = row["kind"]
    return d


def _apply_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Per §1.3.5: absent or None = unchanged; "" or [] = cleared; else replace."""
    merged = dict(current)
    for key in UPDATABLE_STR_FIELDS:
        if key not in patch:
            continue
        v = patch[key]
        if v is None:
            continue  # treat null as absent
        if not isinstance(v, str):
            raise ValueError(f"{key} must be a string")
        merged[key] = v  # "" clears
    for key in UPDATABLE_LIST_FIELDS:
        if key not in patch:
            continue
        v = patch[key]
        if v is None:
            continue
        if not isinstance(v, list):
            raise ValueError(f"{key} must be a list")
        merged[key] = list(v)  # [] clears

    # append_notes rule (fix #3)
    if "append_notes" in patch and patch["append_notes"] is not None:
        app = patch["append_notes"]
        if not isinstance(app, str):
            raise ValueError("append_notes must be a string")
        old = merged.get("notes") or ""
        if old.strip() == "":
            merged["notes"] = app
        else:
            merged["notes"] = old.rstrip() + "\n\n" + app.lstrip()

    # Normalize topics after patch
    merged["topics"] = normalize_topics(merged["topics"])
    return merged


def _validate_merged_kind(merged: dict[str, Any]) -> dict[str, Any] | None:
    """Post-merge kind contract check — required fields for kind must remain non-empty."""
    for f in KIND_REQUIRED[merged["kind"]]:
        if not _nonempty(merged.get(f)):
            return invalid(f, f"required for kind={merged['kind']} after merge")
    return None


def _compute_changed_fields(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    changed = []
    for f in UPDATABLE_STR_FIELDS:
        if (old.get(f) or "") != (new.get(f) or ""):
            changed.append(f)
    for f in UPDATABLE_LIST_FIELDS:
        if list(old.get(f) or []) != list(new.get(f) or []):
            changed.append(f)
    return changed


def _attempt_update(
    conn: sqlite3.Connection,
    *,
    memo_id: int,
    patch: dict[str, Any],
    session_id: str,
    source: str,
) -> dict[str, Any] | str:
    """Single CAS attempt. Returns result dict on success/noop/error; the
    literal string 'retry' if we lost the race (caller should try once more).
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT id, kind, title, symptom, cause, solution, rule, rationale,
                   notes, tags, topics, superseded_by, archived_at, content_hash,
                   updated_at
              FROM memo WHERE id = ?
            """,
            (memo_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return error("not_found", f"memo {memo_id} does not exist", {"id": memo_id})

        if row["superseded_by"] is not None:
            # Walk to tip
            curr = row["superseded_by"]
            while True:
                nxt = conn.execute(
                    "SELECT superseded_by FROM memo WHERE id = ?", (curr,)
                ).fetchone()
                if nxt is None or nxt["superseded_by"] is None:
                    break
                curr = nxt["superseded_by"]
            conn.execute("ROLLBACK")
            return error(
                "superseded_target",
                f"memo {memo_id} is superseded; current tip is {curr}",
                {"tip_id": curr},
            )

        if row["archived_at"] is not None:
            conn.execute("ROLLBACK")
            return error(
                "invalid_input",
                "target is archived; unarchive via DB edit",
                {"field": "target", "reason": "target is archived; unarchive via DB edit"},
            )

        old = _row_to_content_dict(row)
        old_hash = row["content_hash"]

        try:
            merged = _apply_patch(old, patch)
        except ValueError as e:
            conn.execute("ROLLBACK")
            return invalid("patch", str(e))

        kind_err = _validate_merged_kind(merged)
        if kind_err is not None:
            conn.execute("ROLLBACK")
            return kind_err

        new_hash = content_hash(MemoFields(
            kind=merged["kind"], title=merged["title"],
            symptom=merged.get("symptom"), cause=merged.get("cause"),
            solution=merged.get("solution"), rule=merged.get("rule"),
            rationale=merged.get("rationale"), notes=merged.get("notes"),
            topics=merged["topics"], tags=merged["tags"],
        ))

        if new_hash == old_hash:
            write_event(
                conn, action="update", session_id=session_id, memo_id=memo_id,
                payload={"noop": True, "source": source},
            )
            conn.execute("COMMIT")
            return {"id": memo_id, "updated_at": row["updated_at"]}

        cur = conn.execute(
            """
            UPDATE memo
               SET title = ?, symptom = ?, cause = ?, solution = ?, rule = ?,
                   rationale = ?, notes = ?, tags = ?, topics = ?,
                   content_hash = ?, updated_at = CURRENT_TIMESTAMP
             WHERE id = ? AND content_hash = ?
            """,
            (
                merged["title"], merged["symptom"], merged["cause"],
                merged["solution"], merged["rule"], merged["rationale"],
                merged["notes"],
                json.dumps(merged["tags"]), json.dumps(merged["topics"]),
                new_hash, memo_id, old_hash,
            ),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            return "retry"

        changed_fields = _compute_changed_fields(old, merged)
        write_event(
            conn, action="update", session_id=session_id, memo_id=memo_id,
            payload={"fields": changed_fields, "source": source},
        )
        new_row = conn.execute(
            "SELECT updated_at FROM memo WHERE id = ?", (memo_id,)
        ).fetchone()
        conn.execute("COMMIT")
        return {"id": memo_id, "updated_at": new_row["updated_at"]}
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def memory_update(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    id: int,
    patch: dict[str, Any],
    source: str = "tool",
) -> dict[str, Any]:
    """Update an active memo. PATCH semantics + two-attempt CAS (fix #1)."""
    if not isinstance(id, int):
        return invalid("id", "must be int")

    present_fields = [k for k in patch.keys() if k in ALL_UPDATABLE]
    if not present_fields:
        return invalid("patch", "no fields to update")

    if "notes" in patch and "append_notes" in patch:
        n_is_set = patch.get("notes") is not None
        an_is_set = patch.get("append_notes") is not None
        if n_is_set and an_is_set:
            return invalid("append_notes", "mutually exclusive with notes")

    result = _attempt_update(
        conn, memo_id=id, patch=patch, session_id=session_id, source=source,
    )
    if result != "retry":
        return result  # type: ignore[return-value]

    # One retry — re-fetch and re-merge against fresh state.
    result = _attempt_update(
        conn, memo_id=id, patch=patch, session_id=session_id, source=source,
    )
    if result == "retry":
        return error(
            "internal_error",
            "memory_update lost the CAS race twice; sustained write contention is "
            "unexpected for a single-user tool.",
        )
    return result  # type: ignore[return-value]
