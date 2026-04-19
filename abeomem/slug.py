"""Deterministic slug generation (design.md §1.6)."""

from __future__ import annotations

import re
import unicodedata


def slugify(title: str) -> str:
    """Return a filename-safe slug for a memo title.

    NFKD + ASCII strip → cross-platform consistency (café → cafe).
    Lowercase, drop non-alphanumeric (keep hyphen/space), collapse whitespace
    and underscores to hyphens, trim, cap at 60 chars. Empty result → 'untitled'.
    """
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    s = s.strip("-")[:60]
    s = s.rstrip("-")  # re-trim after slicing to 60 may leave a trailing hyphen
    return s or "untitled"
