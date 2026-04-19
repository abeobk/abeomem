"""`abeomem init` — installs the CLAUDE.md memory block and initializes the DB.

§1.7: block is bounded by <!-- BEGIN abeomem --> / <!-- END abeomem --> markers.
Reinstall replaces only content between markers. Per-repo install requires a
git toplevel; global install goes to ~/.claude/CLAUDE.md.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from abeomem.config import load_config
from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.scope import resolve_scope

BEGIN_MARKER = "<!-- BEGIN abeomem -->"
END_MARKER = "<!-- END abeomem -->"

TEMPLATE = """<!-- BEGIN abeomem -->
## Memory (abeomem)

Before non-trivial work, check prior lessons:
- Debugging an error: `memory_search(query=<symptom>, kind="fix", topics=<stack>)`
- Choosing an approach: `memory_search(query=<task>, kind="convention|decision", topics=<stack>)`
- When you see an error you haven't seen this session: always search
- Topics: pass what you're working on — `["python","asyncio"]`, `["nginx","ssl"]`. Reuse existing topics (`abeomem topics`) before inventing new ones.

After non-trivial work, record what you learned:
- Bug took >5 min to diagnose → `memory_save(kind="fix", topics=[...], ...)`
- Project rule not in docs → `memory_save(kind="convention", topics=[...], ...)`
- Architectural choice worth remembering → `memory_save(kind="decision", topics=[...], ...)`
- Always include topics.

When a retrieved memo needs refinement → `memory_update(id, ...)`.
When a retrieved memo is now wrong (project changed) → `memory_save(supersedes=<id>)`.
When a retrieved memo actually helped → ASK THE USER: "Did memo #<id> help?" If yes, call `memory_useful(id)`. Never call based on your own judgment — the user decides.
<!-- END abeomem -->
"""


def _is_git_tracked(path: Path) -> bool:
    """True if `path` is tracked by git."""
    try:
        r = subprocess.run(
            ["git", "-C", str(path.parent), "ls-files", "--error-unmatch", path.name],
            capture_output=True, text=True, check=False,
        )
    except (OSError, FileNotFoundError):
        return False
    return r.returncode == 0


def _has_markers(text: str) -> bool:
    return BEGIN_MARKER in text and END_MARKER in text


def _replace_block(text: str, new_block: str) -> str:
    """Replace the region (inclusive of markers) with new_block.
    new_block must include both markers."""
    start = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER)
    if start == -1 or end == -1:
        raise ValueError("markers not present")
    end_after = end + len(END_MARKER)
    return text[:start] + new_block.rstrip() + "\n" + text[end_after:].lstrip("\n")


def install_claude_md(
    target: Path,
    *,
    confirm_append: bool = True,
    confirm_shared_repo: bool = True,
) -> str:
    """Install or refresh the abeomem block in target. Returns what action
    was taken: 'created', 'updated', 'appended', or 'skipped'."""
    block = TEMPLATE.rstrip() + "\n"

    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(block)
        return "created"

    existing = target.read_text()
    if _has_markers(existing):
        target.write_text(_replace_block(existing, block))
        return "updated"

    # No markers, file exists with other content.
    if _is_git_tracked(target) and confirm_shared_repo:
        resp = input(
            f"{target} is tracked by git; installing here commits memory "
            f"instructions to repo history. Continue? [y/N] "
        ).strip().lower()
        if resp != "y":
            print("skipped; no changes written", file=sys.stderr)
            return "skipped"

    if confirm_append:
        resp = input(
            f"{target} exists without abeomem markers. Append the block? [y/N] "
        ).strip().lower()
        if resp != "y":
            print("skipped; no changes written", file=sys.stderr)
            return "skipped"

    with open(target, "a") as f:
        if not existing.endswith("\n"):
            f.write("\n")
        f.write("\n")
        f.write(block)
    return "appended"


def run_init(is_global: bool = False) -> None:
    """CLI entrypoint for `abeomem init [--global]`."""
    cfg = load_config()

    if is_global:
        target = Path.home() / ".claude" / "CLAUDE.md"
    else:
        # Must be in a git repo (scope rule 1 or 2)
        r = resolve_scope(Path.cwd())
        if r.scope_id == "global":
            print(
                "abeomem init: not in a git repo. Use `abeomem init --global` for "
                "global install, or run `git init` first for per-project install.",
                file=sys.stderr,
            )
            sys.exit(2)
        target = r.anchor_path / "CLAUDE.md"

    # Initialize DB and memos dir
    cfg.db.path.parent.mkdir(parents=True, exist_ok=True)
    cfg.memos.dir.mkdir(parents=True, exist_ok=True)
    cfg.backup.dir.mkdir(parents=True, exist_ok=True)
    conn = get_connection(cfg.db.path)
    try:
        run_migrations(conn, packaged_migrations_dir())
    finally:
        conn.close()

    action = install_claude_md(target)
    print(f"abeomem init: CLAUDE.md {action} at {target}", file=sys.stderr)
    print(f"abeomem init: DB initialized at {cfg.db.path}", file=sys.stderr)
