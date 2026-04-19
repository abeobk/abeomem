import pytest

from abeomem.db import MigrationError, get_connection, run_migrations


def _write(path, name, content):
    (path / name).write_text(content)


def test_fresh_db_applies_migrations(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(migrations, "001_initial.sql", "CREATE TABLE t (v INTEGER);")
    _write(migrations, "002_add.sql", "CREATE TABLE u (v INTEGER);")

    conn = get_connection(tmp_path / "db.sqlite")
    final = run_migrations(conn, migrations)
    assert final == 2

    # Both tables exist
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"t", "u"}.issubset(names)

    # Idempotent — second run is a no-op
    assert run_migrations(conn, migrations) == 2


def test_banned_vacuum_rejected(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(migrations, "001_bad.sql", "CREATE TABLE t (v INTEGER);\nVACUUM;")

    conn = get_connection(tmp_path / "db.sqlite")
    with pytest.raises(MigrationError, match="VACUUM"):
        run_migrations(conn, migrations)
    # Table should not exist — migration was rejected before apply
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='t'"
    ).fetchone()
    assert row is None


def test_banned_in_comment_allowed(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(
        migrations,
        "001_ok.sql",
        "-- this migration does not VACUUM\nCREATE TABLE t (v INTEGER);\n"
        "/* block comment mentioning PRAGMA */\n",
    )
    conn = get_connection(tmp_path / "db.sqlite")
    assert run_migrations(conn, migrations) == 1


def test_crash_mid_ddl_rolls_back(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    # Second statement is invalid SQL — first should roll back too
    _write(
        migrations,
        "001_bad.sql",
        "CREATE TABLE t (v INTEGER);\nCREATE TABLE t (v INTEGER);",  # duplicate fails
    )
    conn = get_connection(tmp_path / "db.sqlite")
    with pytest.raises(MigrationError):
        run_migrations(conn, migrations)
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 0


def test_downgrade_refused(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(migrations, "001_initial.sql", "CREATE TABLE t (v INTEGER);")
    conn = get_connection(tmp_path / "db.sqlite")
    run_migrations(conn, migrations)

    # Simulate: build now only knows up to version 0 (no migration files)
    migrations2 = tmp_path / "migrations_old"
    migrations2.mkdir()
    with pytest.raises(MigrationError, match="[Dd]owngrade"):
        run_migrations(conn, migrations2)


def test_post_sql_runs_after_ddl(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    # 001 creates migration_post_done so the runner can track it
    _write(
        migrations,
        "001_initial.sql",
        "CREATE TABLE t (v INTEGER);\n"
        "CREATE TABLE migration_post_done (version INTEGER PRIMARY KEY);",
    )
    # 002 needs a post step — use INSERT (not banned) as a stand-in for anything idempotent
    _write(migrations, "002_followup.sql", "CREATE TABLE u (v INTEGER PRIMARY KEY);")
    _write(
        migrations,
        "002_followup.post.sql",
        "INSERT OR IGNORE INTO u VALUES (1);",  # idempotent
    )

    conn = get_connection(tmp_path / "db.sqlite")
    run_migrations(conn, migrations)

    # Post ran
    assert conn.execute("SELECT COUNT(*) FROM u").fetchone()[0] == 1
    # Sentinel set
    assert conn.execute(
        "SELECT 1 FROM migration_post_done WHERE version = 2"
    ).fetchone() is not None

    # Second run: post does not re-execute
    run_migrations(conn, migrations)
    assert conn.execute("SELECT COUNT(*) FROM u").fetchone()[0] == 1


def test_post_sql_reruns_after_crash(tmp_path):
    """Simulate crash between DDL commit and post.sql: sentinel is absent, so
    next startup re-runs post. Idempotency is the author's responsibility."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(
        migrations,
        "001_initial.sql",
        "CREATE TABLE t (v INTEGER);\n"
        "CREATE TABLE migration_post_done (version INTEGER PRIMARY KEY);",
    )
    _write(migrations, "002_followup.sql", "CREATE TABLE u (v INTEGER PRIMARY KEY);")
    _write(
        migrations,
        "002_followup.post.sql",
        "INSERT OR IGNORE INTO u VALUES (1);",
    )

    conn = get_connection(tmp_path / "db.sqlite")
    run_migrations(conn, migrations)

    # Simulate crash: delete the sentinel
    conn.execute("DELETE FROM migration_post_done WHERE version = 2")

    run_migrations(conn, migrations)
    # Post was re-run and is idempotent (still one row)
    assert conn.execute("SELECT COUNT(*) FROM u").fetchone()[0] == 1
    # Sentinel restored
    assert conn.execute(
        "SELECT 1 FROM migration_post_done WHERE version = 2"
    ).fetchone() is not None
