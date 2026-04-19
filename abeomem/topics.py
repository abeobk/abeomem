"""Topic normalization (design.md §1.2.9).

Rule of thumb: topics affect ranking; tags don't. Topics get normalized to
a compact vocabulary (lowercase, hyphen-separated, singular). Tags are
stored verbatim.
"""

from __future__ import annotations

from collections.abc import Iterable


def normalize_topic(t: str) -> str:
    """Lowercase, strip, spaces → hyphens. Idempotent."""
    return "-".join(t.strip().lower().split())


def normalize_topics(topics: Iterable[str]) -> list[str]:
    """Normalize each topic and deduplicate, preserving first-seen order."""
    seen: dict[str, None] = {}
    for t in topics:
        n = normalize_topic(t)
        if n and n not in seen:
            seen[n] = None
    return list(seen.keys())
