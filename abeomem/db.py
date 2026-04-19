"""SQLite connection helpers and migration runner.

Design refs: §1.2.1 bootstrap pragmas, §1.2.3 migration runner.
"""

from __future__ import annotations

import re
import sqlite3
from importlib import resources
from pathlib import Path

BANNED_MIGRATION_KEYWORDS = ("VACUUM", "REINDEX", "PRAGMA", "ATTACH", "DETACH")


def packaged_migrations_dir() -> Path:
    """Return the filesystem path to the abeomem.migrations package directory.

    Uses importlib.resources so it works both in editable installs and wheels.
    """
    return Path(str(resources.files("abeomem.migrations")))


class MigrationError(Exception):
    """Raised when a migration cannot be applied."""


def get_connection(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with the four bootstrap pragmas from §1.2.1.

    WAL is per-database; the other three are per-connection. All four must be
    applied on every open. Returns a connection with isolation_level=None so
    callers manage BEGIN/COMMIT explicitly (required for BEGIN IMMEDIATE).
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _strip_sql_comments(sql: str) -> str:
    """Remove /* ... */ block comments and -- line comments.

    Used for banned-keyword scanning so a PRAGMA mentioned inside a comment
    does not trip the restriction.
    """
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", "", sql)
    return sql


def _validate_migration_sql(sql: str, filename: str) -> None:
    """Reject migration SQL containing non-transactional statements (§1.2.3 fix #5).

    Applies only to .sql files, NOT to .post.sql files (those are the escape
    hatch and are explicitly allowed to use banned keywords).
    """
    cleaned = _strip_sql_comments(sql)
    for keyword in BANNED_MIGRATION_KEYWORDS:
        if re.search(rf"\b{keyword}\b", cleaned, flags=re.IGNORECASE):
            raise MigrationError(
                f"migration {filename} contains banned keyword {keyword!r}: "
                f"non-transactional statements break the all-or-nothing guarantee. "
                f"Use a .post.sql companion instead."
            )


def _discover_migrations(migrations_dir: Path) -> list[tuple[int, Path, Path | None]]:
    """Return a list of (version, sql_path, post_path_or_None) sorted by version.

    Matches files named NNN_<name>.sql or NNN_<name>.post.sql. A version with
    only a .post.sql but no .sql is skipped (its DDL file is missing).
    """
    files: dict[int, list[Path | None]] = {}
    for p in sorted(migrations_dir.glob("*.sql")):
        m = re.match(r"^(\d+)_", p.name)
        if not m:
            continue
        version = int(m.group(1))
        entry = files.setdefault(version, [None, None])
        if p.name.endswith(".post.sql"):
            entry[1] = p
        else:
            entry[0] = p
    return [
        (v, entry[0], entry[1])
        for v, entry in sorted(files.items())
        if entry[0] is not None
    ]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def run_migrations(conn: sqlite3.Connection, migrations_dir: str | Path) -> int:
    """Apply pending migrations from migrations_dir. Returns the final version.

    §1.2.3 contract:
      1. Ensure schema_version exists (runner-owned).
      2. Read current version. If > max known, raise (downgrade unsupported).
      3. For each pending N: BEGIN, apply DDL, UPDATE schema_version, COMMIT.
         Rollback on failure.
      4. Scan each .sql for banned keywords before applying.
      5. For each version ≤ current_version whose .post.sql exists but whose
         sentinel is missing from migration_post_done: run it, insert sentinel.

    migration_post_done is created by migration 001 (a regular table, not a
    meta table the runner owns).
    """
    migrations_dir = Path(migrations_dir)

    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    # schema_version holds exactly one row. Plain INSERT OR IGNORE on VALUES(0)
    # is wrong — once UPDATE has bumped the row to e.g. 1, re-inserting 0 adds
    # a second row (different primary key value, no conflict).
    conn.execute(
        "INSERT INTO schema_version SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM schema_version)"
    )

    current = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    migrations = _discover_migrations(migrations_dir)
    max_known = max((v for v, _, _ in migrations), default=0)

    if current > max_known:
        raise MigrationError(
            f"DB is at schema version {current} but this build only knows up to {max_known}. "
            f"Downgrade is not supported."
        )

    # Apply pending transactional migrations
    for version, sql_path, _post_path in migrations:
        if version <= current:
            continue
        sql = sql_path.read_text()
        _validate_migration_sql(sql, sql_path.name)
        # executescript() commits any pending transaction on entry, so BEGIN
        # must live inside the script string, not before it.
        script = f"BEGIN;\n{sql}\nUPDATE schema_version SET version = {int(version)};\nCOMMIT;"
        try:
            conn.executescript(script)
            current = version
        except Exception as e:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(
                f"migration {sql_path.name} failed: {e}"
            ) from e

    # Run pending .post.sql scripts (§1.2.3 fix #5 escape hatch)
    if _has_table(conn, "migration_post_done"):
        for version, _sql_path, post_path in migrations:
            if post_path is None or version > current:
                continue
            done = conn.execute(
                "SELECT 1 FROM migration_post_done WHERE version = ?", (version,)
            ).fetchone()
            if done is not None:
                continue
            post_sql = post_path.read_text()
            # .post.sql intentionally allowed to contain banned keywords
            conn.executescript(post_sql)
            conn.execute("INSERT INTO migration_post_done VALUES (?)", (version,))

    return current
