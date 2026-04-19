"""memory_search tool (design.md §1.3.2, §1.4).

BM25 with field weights × useful_count boost × topic overlap boost.
`_hint` response field appears when results are non-empty and no useful
event has fired in the current session.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from typing import Any

from abeomem.events import write_event
from abeomem.tools import VALID_KINDS, invalid
from abeomem.topics import normalize_topics

HINT_MESSAGE = (
    "After solving the problem, ask the user if a memo above helped. "
    "If yes, call memory_useful(id)."
)


def _escape_fts_query(q: str) -> str:
    """Wrap the user query as an FTS5 phrase string, escaping embedded quotes.

    We deliberately do not expose FTS5 operators (AND/OR/NEAR, column filters)
    to the caller; the query is treated as free text.
    """
    return '"' + q.replace('"', '""') + '"'


def _scope_clause(
    scope_filter: str,
    server_scope: str,
) -> tuple[str, list[Any], str | None]:
    """Return (SQL predicate, params, warning_or_None)."""
    if scope_filter == "global":
        return ("memo.scope = ?", ["global"], None)
    if scope_filter == "repo":
        if server_scope == "global":
            return ("1=0", [], "server is in global scope; scope=repo returns empty")
        return ("memo.scope = ?", [server_scope], None)
    if scope_filter == "both":
        if server_scope == "global":
            return ("memo.scope = ?", ["global"], None)
        return ("memo.scope IN (?, ?)", [server_scope, "global"], None)
    return ("", [], None)  # caller must validate before calling


def memory_search(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    server_scope: str,
    query: str,
    kind: str = "any",
    scope: str = "both",
    topics: list[str] | None = None,  # reserved for T4.8; unused here
    k: int = 8,
) -> dict[str, Any]:
    """Search active memos, ranked by BM25 × useful_count factor.

    Returns {"results": [...], optional "_warning": str}.
    """
    # --- Validate inputs ---
    if not isinstance(query, str) or query.strip() == "":
        return invalid("query", "required, non-empty")
    if kind not in VALID_KINDS and kind != "any":
        return invalid("kind", f"must be one of {sorted(VALID_KINDS)} or 'any'")
    if scope not in ("global", "repo", "both"):
        return invalid("scope", "must be global|repo|both")
    if not isinstance(k, int) or k <= 0:
        return invalid("k", "must be positive int")

    scope_sql, scope_params, warning = _scope_clause(scope, server_scope)

    where_parts = [
        "memo.superseded_by IS NULL",
        "memo.archived_at IS NULL",
        scope_sql,
    ]
    params: list[Any] = list(scope_params)

    if kind != "any":
        where_parts.append("memo.kind = ?")
        params.append(kind)

    match_expr = _escape_fts_query(query)
    where_parts.append("memo_fts MATCH ?")
    params.append(match_expr)

    sql = f"""
    SELECT memo.id, memo.kind, memo.title, memo.useful_count, memo.topics,
           -bm25(memo_fts, 3, 2, 2, 2, 1) AS raw_score,
           snippet(memo_fts, 0, '', '', '…', 10) AS snippet_line
      FROM memo_fts
      JOIN memo ON memo.id = memo_fts.rowid
     WHERE {' AND '.join(where_parts)}
    """

    # Normalize query topics once for ranking.
    q_topics = set(normalize_topics(topics or []))

    t0 = time.perf_counter()
    rows = list(conn.execute(sql, params))
    took_ms = int((time.perf_counter() - t0) * 1000)

    def boost(row: sqlite3.Row) -> float:
        s = float(row["raw_score"]) * (1.0 + math.log(1.0 + row["useful_count"]))
        if q_topics:
            memo_topics = set(json.loads(row["topics"]) if row["topics"] else [])
            overlap = len(q_topics & memo_topics) / len(q_topics)  # asymmetric: §1.4
            s *= 1.0 + 0.5 * overlap
        return s

    scored = sorted(rows, key=boost, reverse=True)[:k]

    results = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "title": r["title"],
            "snippet_line": r["snippet_line"] or "",
            "score": round(boost(r), 4),
        }
        for r in scored
    ]

    write_event(
        conn,
        action="search",
        session_id=session_id,
        query=query,
        topics=list(q_topics) if q_topics else None,
        payload={"k": k, "returned": len(results), "took_ms": took_ms},
    )

    response: dict[str, Any] = {"results": results}
    if warning is not None:
        response["_warning"] = warning

    # `_hint` appears iff results non-empty AND no useful event fired in this
    # session. (§1.3.2)
    if results:
        useful_in_session = conn.execute(
            "SELECT 1 FROM memo_event "
            "WHERE session_id = ? AND action = 'useful' LIMIT 1",
            (session_id,),
        ).fetchone()
        if useful_in_session is None:
            response["_hint"] = HINT_MESSAGE

    return response
