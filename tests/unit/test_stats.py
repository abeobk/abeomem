import json

import pytest

from abeomem.db import get_connection, packaged_migrations_dir, run_migrations
from abeomem.events import write_event
from abeomem.stats import (
    compute_metrics,
    count_recent_backups,
    format_table,
)


@pytest.fixture
def db(tmp_path):
    conn = get_connection(tmp_path / "kb.db")
    run_migrations(conn, packaged_migrations_dir())
    yield conn
    conn.close()


def test_empty_db_all_none(db):
    m = compute_metrics(db)
    # topic_coverage is None (no active memos = no denominator)
    assert m["retrievals_per_day"] is None
    assert m["useful_retrieved"] is None
    assert m["save_dedup_rate"] is None
    assert m["supersede_rate"] is None
    assert m["topic_coverage"] is None
    # fsnotify is a rate over a fixed window; 0 is valid
    assert m["fsnotify_reimports_per_week"] == 0.0


def test_useful_retrieved_ratio(db):
    # 1 save, 3 gets, 1 useful → ratio = 1/3
    db.execute(
        "INSERT INTO memo (scope, kind, title, content_hash) "
        "VALUES ('global', 'fix', 't', ?)",
        (b"\x00" * 32,),
    )
    mid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for sid in ("s1", "s2", "s3"):
        write_event(db, action="get", session_id=sid, memo_id=mid)
    write_event(db, action="useful", session_id="s1", memo_id=mid)
    m = compute_metrics(db)
    assert m["useful_retrieved"] == pytest.approx(1 / 3)


def test_dedup_rate(db):
    db.execute(
        "INSERT INTO memo (scope, kind, title, content_hash) "
        "VALUES ('global', 'fix', 't', ?)",
        (b"\x00" * 32,),
    )
    mid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    write_event(db, action="save", session_id="s", memo_id=mid,
                payload={"status": "created", "source": "tool"})
    write_event(db, action="save", session_id="s", memo_id=mid,
                payload={"status": "duplicate", "source": "tool"})
    write_event(db, action="save", session_id="s", memo_id=mid,
                payload={"status": "duplicate", "source": "tool"})
    m = compute_metrics(db)
    assert m["save_dedup_rate"] == pytest.approx(2 / 3)


def test_supersede_rate(db):
    db.execute(
        "INSERT INTO memo (scope, kind, title, content_hash) "
        "VALUES ('global', 'fix', 't', ?)",
        (b"\x00" * 32,),
    )
    mid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    write_event(db, action="save", session_id="s", memo_id=mid,
                payload={"status": "created", "source": "tool"})
    write_event(db, action="save", session_id="s", memo_id=mid,
                payload={"status": "created", "source": "tool", "supersedes": 1})
    m = compute_metrics(db)
    assert m["supersede_rate"] == pytest.approx(1 / 2)


def test_topic_coverage(db):
    db.execute(
        "INSERT INTO memo (scope, kind, title, topics, content_hash) "
        "VALUES ('global', 'fix', 'a', '[\"python\"]', ?)",
        (b"\x01" * 32,),
    )
    db.execute(
        "INSERT INTO memo (scope, kind, title, topics, content_hash) "
        "VALUES ('global', 'fix', 'b', '[]', ?)",
        (b"\x02" * 32,),
    )
    m = compute_metrics(db)
    assert m["topic_coverage"] == pytest.approx(0.5)


def test_fsnotify_reimports_counts_only_non_noop(db):
    db.execute(
        "INSERT INTO memo (scope, kind, title, content_hash) "
        "VALUES ('global', 'fix', 't', ?)",
        (b"\x00" * 32,),
    )
    mid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    # One watchdog update (non-noop)
    write_event(db, action="update", session_id="watchdog", memo_id=mid,
                payload={"fields": ["title"], "source": "watchdog"})
    # One watchdog noop (should not count)
    write_event(db, action="update", session_id="watchdog", memo_id=mid,
                payload={"noop": True, "source": "watchdog"})
    # One tool update (wrong source)
    write_event(db, action="update", session_id="s", memo_id=mid,
                payload={"fields": ["notes"], "source": "tool"})
    m = compute_metrics(db)
    # 1 non-noop watchdog update over 30 days = 1/(30/7) ≈ 0.233
    assert m["fsnotify_reimports_per_week"] == pytest.approx(1 / (30 / 7))


def test_format_table_shows_em_dash_on_zero_denom():
    m = {
        "retrievals_per_day": None,
        "useful_retrieved": None,
        "save_dedup_rate": None,
        "supersede_rate": None,
        "topic_coverage": None,
        "fsnotify_reimports_per_week": 0.0,
    }
    out = format_table(m, 0)
    assert "—" in out
    assert "[ALERT]" in out  # backups_last_30_days < 4


def test_count_recent_backups(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    (d / "kb-20260101-000000-000000.db").write_text("x")
    (d / "kb-20260102-000000-000000.db").write_text("y")
    assert count_recent_backups(d) == 2


def test_stats_json_output(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cfg_dir = home / ".config" / "abeomem"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(f"""
[db]
path = "{tmp_path}/kb.db"

[memos]
dir = "{tmp_path}/memos"

[backup]
dir = "{tmp_path}/backups"
""")
    from abeomem.stats import run_stats
    run_stats(json_output=True)
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "backups_last_30_days" in parsed
    assert "useful_retrieved" in parsed
