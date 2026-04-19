"""MCP tool implementations. Each tool is a pure function of (conn, session_id,
scope, **inputs) returning a JSON-serializable dict. Registration with FastMCP
happens in abeomem.server."""

from __future__ import annotations

from typing import Any

KIND_REQUIRED: dict[str, tuple[str, ...]] = {
    "fix": ("symptom", "cause", "solution"),
    "gotcha": ("symptom",),
    "convention": ("rule",),
    "decision": ("rule", "rationale"),
}

VALID_KINDS = frozenset(KIND_REQUIRED)


def error(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the standard tool error response shape from §1.3.1."""
    err: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return {"error": err}


def invalid(field: str, reason: str) -> dict[str, Any]:
    return error("invalid_input", f"{field}: {reason}", {"field": field, "reason": reason})


def _nonempty(v: Any) -> bool:
    """True if v is a string with at least one non-whitespace character."""
    return isinstance(v, str) and v.strip() != ""
