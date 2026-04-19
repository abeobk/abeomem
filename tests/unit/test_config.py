from pathlib import Path

from abeomem.config import Config, load_config


def test_defaults_match_spec(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.memos.debounce_ms == 500
    assert cfg.backup.keep_count == 8
    assert cfg.backup.interval_days == 7
    assert cfg.retrieval.default_k == 8
    assert cfg.retrieval.dedup_threshold == 85
    assert cfg.scope.default_search == "both"
    assert cfg.logging.level == "info"


def test_override_memos_dir(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[memos]
dir = "~/custom-memos"
debounce_ms = 1000
""")
    cfg = load_config(cfg_file)
    assert cfg.memos.dir == Path("~/custom-memos").expanduser()
    assert cfg.memos.debounce_ms == 1000


def test_missing_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "does-not-exist.toml")
    assert isinstance(cfg, Config)
    assert cfg.memos.fsnotify is True


def test_partial_override_preserves_other_sections(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[retrieval]\ndefault_k = 16\n")
    cfg = load_config(cfg_file)
    assert cfg.retrieval.default_k == 16
    assert cfg.retrieval.dedup_threshold == 85  # preserved
    assert cfg.backup.keep_count == 8  # untouched section
