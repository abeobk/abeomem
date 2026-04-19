"""Single entry point for writing memo_event rows (design.md §1.2.7).

All writes go through write_event() so payload shapes are validated centrally.
Malformed payload raises ValueError — this is programmer error (bug in a tool
handler), and failing loud here prevents metrics from silently breaking.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

ACTIONS = ("search", "get", "save", "update", "useful", "archive")

_ALLOWED_SOURCES_SAVE = {"tool", "watchdog", "sync-import-new"}
_ALLOWED_SOURCES_UPDATE = {"tool", "watchdog"}


def _require(payload: dict, field: str, types: type | tuple[type, ...]) -> None:
    if field not in payload:
        raise ValueError(f"payload missing required field {field!r}")
    if not isinstance(payload[field], types):
        raise ValueError(
            f"payload field {field!r} has wrong type: "
            f"expected {types}, got {type(payload[field]).__name__}"
        )


def _validate_payload(action: str, payload: dict[str, Any] | None) -> None:
    if action == "search":
        if not isinstance(payload, dict):
            raise ValueError("search payload must be dict with k/returned/took_ms")
        _require(payload, "k", int)
        _require(payload, "returned", int)
        _require(payload, "took_ms", int)
    elif action == "get":
        if payload is None:
            return
        if not isinstance(payload, dict):
            raise ValueError("get payload must be None or dict")
        allowed = {"superseded", "archived"}
        extras = set(payload) - allowed
        if extras:
            raise ValueError(f"get payload has unexpected keys: {extras}")
        for k in payload:
            if not isinstance(payload[k], bool):
                raise ValueError(f"get payload field {k!r} must be bool")
    elif action == "save":
        if not isinstance(payload, dict):
            raise ValueError("save payload must be dict")
        _require(payload, "status", str)
        if payload["status"] not in ("created", "duplicate"):
            raise ValueError(f"save.status must be created|duplicate, got {payload['status']!r}")
        _require(payload, "source", str)
        if payload["source"] not in _ALLOWED_SOURCES_SAVE:
            raise ValueError(f"save.source must be one of {_ALLOWED_SOURCES_SAVE}")
        if "supersedes" in payload and not isinstance(payload["supersedes"], int):
            raise ValueError("save.supersedes must be int if present")
    elif action == "update":
        if not isinstance(payload, dict):
            raise ValueError("update payload must be dict")
        _require(payload, "source", str)
        if payload["source"] not in _ALLOWED_SOURCES_UPDATE:
            raise ValueError(f"update.source must be one of {_ALLOWED_SOURCES_UPDATE}")
        has_fields = "fields" in payload
        has_noop = payload.get("noop") is True
        if has_fields == has_noop:
            raise ValueError("update payload must have exactly one of 'fields' or noop=true")
        if has_fields:
            if not isinstance(payload["fields"], list):
                raise ValueError("update.fields must be list[str]")
            for f in payload["fields"]:
                if not isinstance(f, str):
                    raise ValueError("update.fields entries must be str")
    elif action == "useful":
        if payload is not None:
            raise ValueError("useful payload must be None")
    elif action == "archive":
        if not isinstance(payload, dict):
            raise ValueError("archive payload must be dict")
        _require(payload, "source", str)
        if payload["source"] != "cli":
            raise ValueError("archive.source must be 'cli'")
        if "reason" in payload and payload["reason"] is not None:
            if not isinstance(payload["reason"], str):
                raise ValueError("archive.reason must be str if present")
    else:
        raise ValueError(f"unknown action {action!r}; expected one of {ACTIONS}")


def write_event(
    conn: sqlite3.Connection,
    *,
    action: str,
    session_id: str,
    memo_id: int | None = None,
    query: str | None = None,
    topics: Iterable[str] | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    """Insert a validated memo_event row. Returns the new event id.

    Raises ValueError on malformed payload shape (bug in calling handler).
    """
    _validate_payload(action, payload)
    topics_json = json.dumps(list(topics)) if topics is not None else None
    payload_json = json.dumps(payload) if payload is not None else None
    cur = conn.execute(
        """
        INSERT INTO memo_event (session_id, action, memo_id, query, topics, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (session_id, action, memo_id, query, topics_json, payload_json),
    )
    return cur.lastrowid
