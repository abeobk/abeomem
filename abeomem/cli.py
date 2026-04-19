"""abeomem CLI (design.md §1.8.1).

All write commands use session_id="cli". All output goes to stdout (piping),
errors to stderr. `serve` is the only command that owns stdout for JSON-RPC.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from abeomem.config import load_config
from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.events import write_event
from abeomem.mirror.export import export_memo, memo_file_path
from abeomem.mirror.parse import parse_memo_file
from abeomem.scope import resolve_scope
from abeomem.slug import slugify

app = typer.Typer(
    name="abeomem",
    help="MCP server for tips, tricks, and bug fixes Claude Code already paid for once.",
    no_args_is_help=True,
)

err = Console(file=sys.stderr)


def _open_db():
    cfg = load_config()
    cfg.db.path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(cfg.db.path)
    run_migrations(conn, packaged_migrations_dir())
    return cfg, conn


def _active_only() -> str:
    return "superseded_by IS NULL AND archived_at IS NULL"


@app.command()
def init(
    global_: Annotated[bool, typer.Option("--global", help="Install ~/.claude/CLAUDE.md")] = False,
) -> None:
    """Setup (injects CLAUDE.md block with markers)."""
    from abeomem.claude_md import run_init
    run_init(is_global=global_)


@app.command()
def serve(
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Run MCP server (stdio)."""
    from abeomem.server import run_server
    run_server(verbose=verbose)


@app.command()
def sync(
    import_new: Annotated[bool, typer.Option("--import-new")] = False,
) -> None:
    """Rescan memos dir; reimport changed files.

    Without --import-new, runs the reconciliation step only (same as startup).
    """
    cfg, conn = _open_db()
    from abeomem.mirror.reconcile import reconcile
    reconcile(conn, cfg.memos.dir)
    if import_new:
        _import_new(cfg, conn)
    conn.close()


def _import_new(cfg, conn) -> None:
    """Scan memos dir for orphan .md files and ingest them."""
    from abeomem.hashing import MemoFields, content_hash
    from abeomem.topics import normalize_topics

    memos_dir = cfg.memos.dir
    if not memos_dir.exists():
        return
    for path in memos_dir.rglob("*.md"):
        parsed = parse_memo_file(path)
        if parsed is None:
            continue
        # Is there a row with this id?
        if conn.execute("SELECT 1 FROM memo WHERE id = ?", (parsed["id"],)).fetchone():
            continue
        # Orphan — assign a new id via INSERT and rename the file
        topics = normalize_topics(parsed.get("topics") or [])
        tags = list(parsed.get("tags") or [])
        fields = MemoFields(
            kind=parsed.get("kind", "fix"),
            title=parsed.get("title") or "",
            symptom=parsed.get("symptom"), cause=parsed.get("cause"),
            solution=parsed.get("solution"), rule=parsed.get("rule"),
            rationale=parsed.get("rationale"), notes=parsed.get("notes"),
            topics=topics, tags=tags,
        )
        ch = content_hash(fields)
        scope = parsed.get("scope", "global")
        cur = conn.execute(
            """
            INSERT INTO memo (scope, kind, title, symptom, cause, solution,
                              rule, rationale, notes, tags, topics, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scope, fields.kind, fields.title,
                fields.symptom, fields.cause, fields.solution,
                fields.rule, fields.rationale, fields.notes,
                json.dumps(tags), json.dumps(topics), ch,
            ),
        )
        new_id = cur.lastrowid
        write_event(
            conn, action="save", session_id="cli", memo_id=new_id,
            payload={"status": "created", "source": "sync-import-new"},
        )
        # Rename file to reflect new id
        new_path = path.parent / f"{new_id}-{slugify(fields.title)}.md"
        if new_path != path:
            path.rename(new_path)
        print(f"imported {path.name} as memo {new_id}", file=sys.stderr)


@app.command()
def backup(
    out: Annotated[str | None, typer.Option("--out")] = None,
) -> None:
    """Checkpoint WAL + VACUUM INTO timestamped copy."""
    from abeomem.backup import run_backup
    cfg = load_config()
    target = Path(out) if out else None
    run_backup(cfg.db.path, cfg, explicit_target=target)


@app.command()
def ls(
    kind: Annotated[str | None, typer.Option("--kind")] = None,
    topic: Annotated[str | None, typer.Option("--topic")] = None,
    tag: Annotated[str | None, typer.Option("--tag")] = None,
    scope_: Annotated[str | None, typer.Option("--scope")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 50,
    json_: Annotated[bool, typer.Option("--json")] = False,
    include_archived: Annotated[bool, typer.Option("--include-archived")] = False,
) -> None:
    """List memos (defaults to current scope)."""
    cfg, conn = _open_db()
    current_scope = resolve_scope(Path.cwd()).scope_id

    where = []
    params: list = []
    if not include_archived:
        where.append("superseded_by IS NULL AND archived_at IS NULL")
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    if scope_ is None:
        where.append("scope = ?")
        params.append(current_scope)
    elif scope_ != "all":
        where.append("scope = ?")
        params.append(scope_)
    sql = "SELECT id, scope, kind, title, topics, tags, useful_count FROM memo"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()

    # Python-side filters for JSON columns
    def _topic_ok(row) -> bool:
        if topic is None:
            return True
        return topic in (json.loads(row["topics"]) if row["topics"] else [])

    def _tag_ok(row) -> bool:
        if tag is None:
            return True
        return tag in (json.loads(row["tags"]) if row["tags"] else [])

    filtered = [r for r in rows if _topic_ok(r) and _tag_ok(r)]

    if json_:
        for r in filtered:
            print(json.dumps({
                "id": r["id"], "scope": r["scope"], "kind": r["kind"],
                "title": r["title"],
                "topics": json.loads(r["topics"]) if r["topics"] else [],
                "tags": json.loads(r["tags"]) if r["tags"] else [],
                "useful_count": r["useful_count"],
            }))
        conn.close()
        return

    table = Table(show_header=True)
    for col in ("ID", "Kind", "Title", "Topics", "Useful"):
        table.add_column(col)
    for r in filtered:
        topics = ",".join(json.loads(r["topics"]) if r["topics"] else [])
        table.add_row(str(r["id"]), r["kind"], r["title"], topics, str(r["useful_count"]))
    Console().print(table)
    conn.close()


@app.command()
def show(id: int) -> None:
    """Print full memo as markdown to stdout."""
    cfg, conn = _open_db()
    row = conn.execute("SELECT * FROM memo WHERE id = ?", (id,)).fetchone()
    if row is None:
        err.print(f"[red]memo {id} does not exist[/red]")
        raise typer.Exit(code=1)
    p = memo_file_path(cfg.memos.dir, row)
    if not p.exists():
        export_memo(row, cfg.memos.dir)
    if p.exists():
        sys.stdout.write(p.read_text())
    conn.close()


@app.command()
def edit(id: int) -> None:
    """Open exported .md in $EDITOR (non-blocking)."""
    cfg, conn = _open_db()
    row = conn.execute("SELECT * FROM memo WHERE id = ?", (id,)).fetchone()
    if row is None:
        err.print(f"[red]memo {id} does not exist[/red]")
        raise typer.Exit(code=1)
    p = memo_file_path(cfg.memos.dir, row)
    if not p.exists():
        export_memo(row, cfg.memos.dir)
    editor = os.environ.get("EDITOR", "vi")
    subprocess.Popen([editor, str(p)])
    conn.close()


@app.command()
def chain(id: int) -> None:
    """Print supersede chain for id (root → tip)."""
    _, conn = _open_db()
    # Walk backwards to root
    root = id
    while True:
        prev = conn.execute(
            "SELECT id FROM memo WHERE superseded_by = ? LIMIT 1", (root,)
        ).fetchone()
        if prev is None:
            break
        root = prev["id"]
    # Walk forward
    curr = root
    while curr is not None:
        row = conn.execute("SELECT id, title, superseded_by, archived_at FROM memo WHERE id = ?",
                           (curr,)).fetchone()
        if row is None:
            break
        tag = "archived" if row["archived_at"] else ("tip" if row["superseded_by"] is None else "")
        print(f"{row['id']:>5}  {row['title']}  {tag}")
        curr = row["superseded_by"]
    conn.close()


@app.command()
def archive(
    id: int,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    """Soft-delete; excludes from search."""
    cfg, conn = _open_db()
    row = conn.execute("SELECT * FROM memo WHERE id = ?", (id,)).fetchone()
    if row is None:
        err.print(f"[red]memo {id} does not exist[/red]")
        raise typer.Exit(code=1)
    if row["archived_at"] is not None:
        err.print(f"[yellow]memo {id} is already archived[/yellow]")
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE memo SET archived_at = CURRENT_TIMESTAMP WHERE id = ?", (id,)
        )
        payload = {"source": "cli"}
        if reason is not None:
            payload["reason"] = reason
        write_event(conn, action="archive", session_id="cli", memo_id=id, payload=payload)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    # Re-export with banner
    row = conn.execute("SELECT * FROM memo WHERE id = ?", (id,)).fetchone()
    export_memo(row, cfg.memos.dir, archived_reason=reason)
    err.print(f"archived memo {id}")
    conn.close()


@app.command()
def topics(
    min_count: Annotated[int, typer.Option("--min-count")] = 1,
) -> None:
    """List topics by frequency (active memos only)."""
    _, conn = _open_db()
    rows = conn.execute(f"SELECT topics FROM memo WHERE {_active_only()}").fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        for t in json.loads(r["topics"]) if r["topics"] else []:
            counts[t] = counts.get(t, 0) + 1
    sorted_topics = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    table = Table(show_header=True)
    table.add_column("Topic")
    table.add_column("Count")
    for t, c in sorted_topics:
        if c < min_count:
            continue
        table.add_row(t, str(c))
    Console().print(table)
    conn.close()


@app.command()
def scope(
    show_remote: Annotated[bool, typer.Option("--show-remote")] = False,
) -> None:
    """Print current-directory scope."""
    r = resolve_scope(Path.cwd())
    print(r.scope_id)
    if show_remote and r.scope_id.startswith("repo:") and not r.scope_id.startswith("repo:path:"):
        try:
            result = subprocess.run(
                ["git", "-C", str(r.anchor_path), "remote", "get-url", "origin"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                from abeomem.scope import normalize_remote_url
                print(f"remote: {result.stdout.strip()}")
                print(f"normalized: {normalize_remote_url(result.stdout.strip())}")
        except (OSError, FileNotFoundError):
            pass


# Placeholder — real stats lives in T7.1
@app.command()
def stats(
    json_: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Success metrics (30-day window)."""
    from abeomem.stats import run_stats
    run_stats(json_output=json_)


# Shadow so ruff F401 doesn't kick in on shutil (reserved for future use)
_ = shutil
