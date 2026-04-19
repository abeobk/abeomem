"""Deterministic content hash for dedup and CAS (design.md §1.2.4)."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass
class MemoFields:
    """The content fields that participate in content_hash.

    Scope is NOT included — dedup is scope-local and the (scope, content_hash)
    UNIQUE constraint handles cross-scope disambiguation.
    """

    kind: str
    title: str
    symptom: str | None = None
    cause: str | None = None
    solution: str | None = None
    rule: str | None = None
    rationale: str | None = None
    notes: str | None = None
    topics: Iterable[str] = ()
    tags: Iterable[str] = ()


def _nfc(s: str | None) -> str:
    return unicodedata.normalize("NFC", s or "")


def content_hash(m: MemoFields) -> bytes:
    """Return 32-byte sha256 of the canonical serialization of m.

    NFC is applied to every string field AND to every topic/tag before sorting.
    Without NFC-in-sort, `café` (NFC) and `café` (NFD) would sort to different
    positions despite rendering identically.

    Fields are joined with \\x1f (unit separator) so e.g. title='ab',symptom=''
    hashes distinctly from title='a',symptom='b'.
    """
    topics = sorted(unicodedata.normalize("NFC", t) for t in m.topics)
    tags = sorted(unicodedata.normalize("NFC", t) for t in m.tags)
    parts = [
        _nfc(m.kind),
        _nfc(m.title),
        _nfc(m.symptom),
        _nfc(m.cause),
        _nfc(m.solution),
        _nfc(m.rule),
        _nfc(m.rationale),
        _nfc(m.notes),
        "|".join(topics),
        "|".join(tags),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
