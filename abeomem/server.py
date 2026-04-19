"""FastMCP server bootstrap (design.md §1.6 strict startup order).

Startup sequence:
  1. Open DB, apply bootstrap pragmas.
  2. Run migrations.
  3. Auto-backup if interval elapsed (§1.8.1).
  4. Reconciliation (§1.6).
  5. Start watchdog.
  6. Start MCP listener.

session_id is one UUID4 per server process (= one stdio connection = one CC
session, §1.2.8). Stdio owns stdout; all logging goes to stderr.
"""

from __future__ import annotations

import logging
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from abeomem.config import Config, load_config
from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.mirror.reconcile import reconcile
from abeomem.mirror.watcher import MemosWatcher
from abeomem.scope import resolve_scope
from abeomem.tools.get import memory_get as _memory_get
from abeomem.tools.save import memory_save as _memory_save
from abeomem.tools.search import memory_search as _memory_search
from abeomem.tools.update import memory_update as _memory_update
from abeomem.tools.useful import memory_useful as _memory_useful

logger = logging.getLogger(__name__)


@dataclass
class ServerContext:
    config: Config
    session_id: str
    scope: str
    db_path: Path
    memos_dir: Path


def bootstrap(config: Config | None = None) -> ServerContext:
    """Run the non-MCP parts of startup: pragmas, migrations, reconcile.

    Returns a ServerContext. Callers that want the full server (watchdog +
    stdio listener) should call create_server() instead.
    """
    cfg = config or load_config()
    cfg.db.path.parent.mkdir(parents=True, exist_ok=True)
    cfg.memos.dir.mkdir(parents=True, exist_ok=True)
    cfg.backup.dir.mkdir(parents=True, exist_ok=True)

    conn = get_connection(cfg.db.path)
    try:
        run_migrations(conn, packaged_migrations_dir())
        reconcile(conn, cfg.memos.dir)
    finally:
        conn.close()

    scope = resolve_scope(Path.cwd()).scope_id
    return ServerContext(
        config=cfg,
        session_id=str(uuid.uuid4()),
        scope=scope,
        db_path=cfg.db.path,
        memos_dir=cfg.memos.dir,
    )


def _register_tools(mcp: FastMCP, ctx: ServerContext) -> None:
    """Register the five memory_* tools with FastMCP."""

    @mcp.tool
    def memory_search(
        query: str,
        kind: str = "any",
        scope: str = "both",
        topics: list[str] | None = None,
        k: int = 8,
    ) -> dict[str, Any]:
        """Search for lessons from prior sessions.

        Call this BEFORE debugging, BEFORE making non-obvious choices, and
        whenever you see an error you haven't seen this session.
        """
        conn = get_connection(ctx.db_path)
        try:
            return _memory_search(
                conn,
                session_id=ctx.session_id,
                server_scope=ctx.scope,
                query=query,
                kind=kind,
                scope=scope,
                topics=topics,
                k=k,
            )
        finally:
            conn.close()

    @mcp.tool
    def memory_get(id: int) -> dict[str, Any]:
        """Fetch full memo by id. Bumps access_count and updates last_accessed_at."""
        conn = get_connection(ctx.db_path)
        try:
            return _memory_get(conn, session_id=ctx.session_id, id=id)
        finally:
            conn.close()

    @mcp.tool
    def memory_save(
        kind: str,
        title: str,
        symptom: str | None = None,
        cause: str | None = None,
        solution: str | None = None,
        rule: str | None = None,
        rationale: str | None = None,
        notes: str | None = None,
        tags: list[str] | None = None,
        topics: list[str] | None = None,
        supersedes: int | None = None,
    ) -> dict[str, Any]:
        """Save a hard-earned lesson.

        Save when a bug took >5 min to diagnose, you discovered a non-obvious
        project convention, or you made a decision you'd forget. Do NOT save
        secrets, scratchpad, anything already in repo docs, or the user's
        current thought.
        """
        conn = get_connection(ctx.db_path)
        try:
            return _memory_save(
                conn,
                session_id=ctx.session_id,
                scope=ctx.scope,
                data={
                    "kind": kind, "title": title,
                    "symptom": symptom, "cause": cause, "solution": solution,
                    "rule": rule, "rationale": rationale, "notes": notes,
                    "tags": tags or [], "topics": topics or [],
                    "supersedes": supersedes,
                },
                dedup_threshold=ctx.config.retrieval.dedup_threshold,
            )
        finally:
            conn.close()

    @mcp.tool
    def memory_update(
        id: int,
        title: str | None = None,
        symptom: str | None = None,
        cause: str | None = None,
        solution: str | None = None,
        rule: str | None = None,
        rationale: str | None = None,
        notes: str | None = None,
        append_notes: str | None = None,
        tags: list[str] | None = None,
        topics: list[str] | None = None,
    ) -> dict[str, Any]:
        """Refine an existing memo: typo, clarification, extra notes, added tag.

        Preserves id, created_at, useful_count, access_count. If the memo's
        claim is now wrong, use memory_save(supersedes=<id>) instead.
        """
        patch = {
            k: v for k, v in {
                "title": title, "symptom": symptom, "cause": cause,
                "solution": solution, "rule": rule, "rationale": rationale,
                "notes": notes, "append_notes": append_notes,
                "tags": tags, "topics": topics,
            }.items() if v is not None
        }
        conn = get_connection(ctx.db_path)
        try:
            return _memory_update(
                conn, session_id=ctx.session_id, id=id, patch=patch,
            )
        finally:
            conn.close()

    @mcp.tool
    def memory_useful(id: int) -> dict[str, Any]:
        """Call this ONLY after the user has explicitly confirmed a memo helped.

        Ask first: "Did memo #<id> help?" — if yes, call. Do NOT call based on
        your own judgment.
        """
        conn = get_connection(ctx.db_path)
        try:
            return _memory_useful(conn, session_id=ctx.session_id, id=id)
        finally:
            conn.close()


def create_server(config: Config | None = None) -> tuple[FastMCP, ServerContext, MemosWatcher]:
    """Build a fully-wired FastMCP server: startup sequence + tools + watchdog.

    Returns (mcp, ctx, watcher). Caller must watcher.stop() when done.
    """
    ctx = bootstrap(config)
    mcp = FastMCP(name="abeomem", version="0.1.0")
    _register_tools(mcp, ctx)

    watcher = MemosWatcher(
        ctx.memos_dir, ctx.db_path,
        debounce_ms=ctx.config.memos.debounce_ms,
    )
    if ctx.config.memos.fsnotify:
        watcher.start()

    return mcp, ctx, watcher


def _setup_stderr_logging(level: str) -> None:
    """All logs go to stderr — stdout is owned by MCP stdio transport."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric)


def run_server(verbose: bool = False) -> None:
    """Entrypoint for `abeomem serve`. Never returns (until stdio closes)."""
    cfg = load_config()
    _setup_stderr_logging("debug" if verbose else cfg.logging.level)

    mcp, ctx, watcher = create_server(cfg)
    print(
        f"abeomem: serving scope={ctx.scope} db={ctx.db_path} session={ctx.session_id}",
        file=sys.stderr,
    )
    try:
        mcp.run(transport="stdio", show_banner=False)
    finally:
        watcher.stop()
