"""memory_useful tool (design.md §1.3.6).

The user is the rater; CC is the messenger. This tool is called only after
the user has confirmed a memo helped.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from abeomem.events import write_event
from abeomem.tools import error, invalid


def memory_useful(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    id: int,
) -> dict[str, Any]:
    if not isinstance(id, int):
        return invalid("id", "must be int")

    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT useful_count FROM memo WHERE id = ?", (id,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return error("not_found", f"memo {id} does not exist", {"id": id})
        conn.execute(
            "UPDATE memo SET useful_count = useful_count + 1 WHERE id = ?", (id,)
        )
        new_count = conn.execute(
            "SELECT useful_count FROM memo WHERE id = ?", (id,)
        ).fetchone()[0]
        write_event(conn, action="useful", session_id=session_id, memo_id=id)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"useful_count": new_count}
