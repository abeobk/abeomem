"""Markdown parse (design.md §1.6).

Parses a memo .md file (frontmatter + body) into a dict suitable for
memory_update. Filename id is authoritative — if frontmatter id disagrees,
filename wins (and we log a warning).
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from abeomem.mirror.export import ARCHIVED_BANNER

logger = logging.getLogger(__name__)

FILENAME_RE = re.compile(r"^(\d+)-.*\.md$")


def _id_from_filename(path: Path) -> int | None:
    m = FILENAME_RE.match(path.name)
    return int(m.group(1)) if m else None


def parse_memo_file(path: Path) -> dict[str, Any] | None:
    """Return a dict with parsed fields, or None on malformed input.

    The returned dict is suitable for passing to memory_update as a patch plus
    metadata (id, kind, scope, archived_at).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"abeomem: cannot read {path}: {e}", file=sys.stderr)
        return None

    # Expect: ---\n<yaml>\n---\n\n<body>
    if not text.startswith("---\n"):
        print(f"abeomem: {path.name} missing frontmatter fence", file=sys.stderr)
        return None
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        print(f"abeomem: {path.name} malformed frontmatter", file=sys.stderr)
        return None
    _, fm_text, body = parts

    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        print(f"abeomem: {path.name} YAML error: {e}", file=sys.stderr)
        return None

    if not isinstance(fm, dict):
        print(f"abeomem: {path.name} frontmatter is not a mapping", file=sys.stderr)
        return None

    # Filename id authoritative
    fname_id = _id_from_filename(path)
    if fname_id is None:
        print(f"abeomem: {path.name} filename has no id prefix", file=sys.stderr)
        return None
    if "id" in fm and fm["id"] != fname_id:
        print(
            f"abeomem: {path.name} id mismatch (filename={fname_id}, "
            f"frontmatter={fm['id']}) — filename wins",
            file=sys.stderr,
        )
    fm["id"] = fname_id

    body = body.lstrip()
    # Strip archived banner if present (body-level, survives frontmatter read)
    banner_line = ARCHIVED_BANNER.strip()
    if body.startswith(banner_line):
        body = body[len(banner_line):].lstrip()

    parsed_body = _parse_body(body, fm.get("kind"))
    fm.update(parsed_body)
    return fm


def _parse_body(body: str, kind: str | None) -> dict[str, Any]:
    """Extract title (# heading), inline fields (**Label:** ...), and Notes."""
    result: dict[str, Any] = {}

    lines = body.splitlines()
    # Title from first non-empty line starting with '# '
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i < len(lines) and lines[i].startswith("# "):
        result["title"] = lines[i][2:].strip()
        i += 1
    else:
        result["title"] = None

    # Collect inline fields and notes
    current_field: str | None = None
    field_bufs: dict[str, list[str]] = {}
    notes_buf: list[str] = []
    in_notes = False

    for line in lines[i:]:
        stripped = line.strip()
        if stripped == "## Notes":
            in_notes = True
            current_field = None
            continue
        if in_notes:
            notes_buf.append(line)
            continue
        m = re.match(r"^\*\*(Symptom|Cause|Solution|Rule|Rationale):\*\*\s*(.*)$",
                     stripped)
        if m:
            current_field = m.group(1).lower()
            field_bufs.setdefault(current_field, []).append(m.group(2))
        elif current_field is not None and stripped:
            field_bufs[current_field].append(stripped)
        elif stripped == "":
            current_field = None

    for f, buf in field_bufs.items():
        result[f] = " ".join(s for s in buf if s).strip() or None

    notes = "\n".join(notes_buf).strip()
    result["notes"] = notes if notes else None
    return result
