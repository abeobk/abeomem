"""memory_search tool (design.md §1.3.2, §1.4).

T4.3 base implementation: BM25 with field weights + useful_count boost,
scope + kind filters. T4.8 will layer topic boost and the _hint response
field on top.
"""

from __future__ import annotations

import math
import sqlite3
import time
from typing import Any

from abeomem.events import write_event
from abeomem.tools import VALID_KINDS, invalid


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

    # FTS match clause
    match_expr = _escape_fts_query(query)
    where_parts.append("memo_fts MATCH ?")
    params.append(match_expr)

    # BM25 returns a negative score (more negative = better). Negate so higher =
    # better, then multiply by useful_count factor. Keep the raw BM25 for
    # column-1 snippet generation.
    sql = f"""
    SELECT memo.id, memo.kind, memo.title, memo.useful_count,
           -bm25(memo_fts, 3, 2, 2, 2, 1) AS raw_score,
           snippet(memo_fts, 0, '', '', '…', 10) AS snippet_line
      FROM memo_fts
      JOIN memo ON memo.id = memo_fts.rowid
     WHERE {' AND '.join(where_parts)}
    """

    t0 = time.perf_counter()
    rows = list(conn.execute(sql, params))
    took_ms = int((time.perf_counter() - t0) * 1000)

    # Apply useful_count boost in Python (cheaper than SQL for small k), sort, trim
    def boost(row: sqlite3.Row) -> float:
        return float(row["raw_score"]) * (1.0 + math.log(1.0 + row["useful_count"]))

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
        topics=topics,
        payload={"k": k, "returned": len(results), "took_ms": took_ms},
    )

    response: dict[str, Any] = {"results": results}
    if warning is not None:
        response["_warning"] = warning
    return response
