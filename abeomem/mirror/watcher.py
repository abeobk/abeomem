"""Watchdog + debouncer (design.md §1.6, fix #2).

External .md edits are parsed, hashed, and routed through memory_update with
session_id='watchdog' so metrics can distinguish editor-driven curation from
MCP and CLI activity. Debounced per path at configurable ms to collapse editor
write-rename-write storms into one update.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from abeomem.db import get_connection
from abeomem.hashing import MemoFields, content_hash
from abeomem.mirror.parse import parse_memo_file
from abeomem.tools.update import memory_update

logger = logging.getLogger(__name__)

WATCHDOG_SESSION_ID = "watchdog"


class MemosWatcher:
    """Observe memos_dir and forward changes to memory_update.

    Start with .start(); stop with .stop(). One SQLite connection is opened
    per event (cheap and thread-safe).
    """

    def __init__(
        self,
        memos_dir: Path,
        db_path: Path,
        *,
        debounce_ms: int = 500,
    ) -> None:
        self.memos_dir = Path(memos_dir)
        self.db_path = Path(db_path)
        self.debounce_s = debounce_ms / 1000.0
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        self._observer: Observer | None = None

    def start(self) -> None:
        self.memos_dir.mkdir(parents=True, exist_ok=True)
        handler = _Handler(self)
        observer = Observer()
        observer.schedule(handler, str(self.memos_dir), recursive=True)
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()

    def schedule_handle(self, path: str) -> None:
        """Debounce: cancel any pending timer for this path and start a new one."""
        with self._lock:
            existing = self._timers.get(path)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(self.debounce_s, self._fire, args=(path,))
            timer.daemon = True
            self._timers[path] = timer
            timer.start()

    def _fire(self, path: str) -> None:
        with self._lock:
            self._timers.pop(path, None)
        try:
            self._handle_changed(Path(path))
        except Exception as e:  # pragma: no cover — defensive
            print(f"abeomem watchdog: unhandled error for {path}: {e}", file=sys.stderr)

    def _handle_changed(self, path: Path) -> None:
        if not path.exists():
            # File was removed between event and debounce fire
            print(f"abeomem: deleted .md {path.name} — DB unchanged "
                  f"(use `abeomem archive` for removal)", file=sys.stderr)
            return
        if path.name.startswith(".") or ".tmp-" in path.name:
            return  # ignore editor swap files and our own atomic temp files
        if not path.suffix == ".md":
            return

        parsed = parse_memo_file(path)
        if parsed is None:
            return  # error already logged

        memo_id = parsed["id"]
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT content_hash, kind, tags, topics, useful_count "
                "FROM memo WHERE id = ?",
                (memo_id,),
            ).fetchone()
            if row is None:
                print(
                    f"abeomem: orphan .md {path.name} (no matching DB row); "
                    f"use `abeomem sync --import-new` to opt in",
                    file=sys.stderr,
                )
                return

            # Hash-before-update: if parsed content matches the DB's current
            # hash, this is a self-triggered event (we exported it) — skip.
            import json as _json

            fields = MemoFields(
                kind=row["kind"],
                title=parsed.get("title") or "",
                symptom=parsed.get("symptom"),
                cause=parsed.get("cause"),
                solution=parsed.get("solution"),
                rule=parsed.get("rule"),
                rationale=parsed.get("rationale"),
                notes=parsed.get("notes"),
                topics=_json.loads(row["topics"]) if row["topics"] else [],
                tags=_json.loads(row["tags"]) if row["tags"] else [],
            )
            if content_hash(fields) == row["content_hash"]:
                return  # no-op

            patch = {
                k: parsed.get(k)
                for k in ("title", "symptom", "cause", "solution",
                          "rule", "rationale", "notes")
                if parsed.get(k) is not None
            }
            if not patch:
                return  # nothing to update

            result = memory_update(
                conn,
                session_id=WATCHDOG_SESSION_ID,
                id=memo_id,
                patch=patch,
                source="watchdog",
            )
            if "error" in result:
                print(
                    f"abeomem watchdog: update for {path.name} failed: "
                    f"{result['error']}",
                    file=sys.stderr,
                )
        finally:
            conn.close()


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: MemosWatcher) -> None:
        self._w = watcher

    def on_modified(self, event) -> None:  # type: ignore[no-untyped-def]
        if not event.is_directory:
            self._w.schedule_handle(event.src_path)

    def on_created(self, event) -> None:  # type: ignore[no-untyped-def]
        if not event.is_directory:
            self._w.schedule_handle(event.src_path)

    def on_moved(self, event) -> None:  # type: ignore[no-untyped-def]
        if not event.is_directory:
            self._w.schedule_handle(event.dest_path)
