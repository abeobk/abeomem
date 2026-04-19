"""Scope resolution (design.md §1.2.5).

Three rules, no project-marker heuristics:
  1. Git repo with remote → repo:<sha256(normalized_remote)[:16]>
  2. Git repo without remote → repo:path:<sha256(toplevel_path)[:16]>
  3. Else → global, anchor = CWD
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ScopeResult:
    scope_id: str
    anchor_path: Path


def normalize_remote_url(url: str) -> str:
    """Five-step normalization from §1.2.5.

    git@host:path → https://host/path; lowercase; strip trailing .git;
    strip trailing /; ://www. → ://.
    """
    s = url.strip()
    # Step 1: git@host:path → https://host/path
    m = re.match(r"^git@([^:]+):(.+)$", s)
    if m:
        s = f"https://{m.group(1)}/{m.group(2)}"
    # Step 2: lowercase
    s = s.lower()
    # Step 3: strip trailing .git
    if s.endswith(".git"):
        s = s[:-4]
    # Step 4: strip trailing /
    if s.endswith("/"):
        s = s[:-1]
    # Step 5: ://www. → ://
    s = re.sub(r"://www\.", "://", s)
    return s


def _hex16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _git(cwd: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out or None


def resolve_scope(cwd: str | Path) -> ScopeResult:
    """Resolve the scope for a working directory per §1.2.5."""
    cwd = Path(cwd)
    toplevel = _git(cwd, "rev-parse", "--show-toplevel")
    if toplevel is not None:
        remote = _git(cwd, "remote", "get-url", "origin")
        if remote is not None:
            scope_id = f"repo:{_hex16(normalize_remote_url(remote))}"
            return ScopeResult(scope_id=scope_id, anchor_path=Path(toplevel))
        # Git repo without remote
        scope_id = f"repo:path:{_hex16(toplevel)}"
        return ScopeResult(scope_id=scope_id, anchor_path=Path(toplevel))
    # Not a git repo
    return ScopeResult(scope_id="global", anchor_path=cwd.resolve())
