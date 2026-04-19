"""TOML config loader (design.md §1.8.2).

Defaults match §1.8.2 exactly. Expand ~ in every path field. Missing file →
all defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tomllib


def _expand(p: str) -> Path:
    return Path(p).expanduser()


@dataclass
class DBConfig:
    path: Path = field(default_factory=lambda: _expand("~/.abeomem/kb.db"))


@dataclass
class MemosConfig:
    dir: Path = field(default_factory=lambda: _expand("~/.abeomem/memos"))
    fsnotify: bool = True
    debounce_ms: int = 500


@dataclass
class BackupConfig:
    enabled: bool = True
    dir: Path = field(default_factory=lambda: _expand("~/.abeomem/backups"))
    keep_count: int = 8
    interval_days: int = 7


@dataclass
class ScopeConfig:
    default_search: str = "both"  # repo | global | both


@dataclass
class RetrievalConfig:
    default_k: int = 8
    dedup_threshold: int = 85


@dataclass
class LoggingConfig:
    level: str = "info"  # debug | info | warn


@dataclass
class Config:
    db: DBConfig = field(default_factory=DBConfig)
    memos: MemosConfig = field(default_factory=MemosConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


DEFAULT_CONFIG_PATH = _expand("~/.config/abeomem/config.toml")


def _overlay(section_cls: type, data: dict) -> object:
    """Merge dict keys onto a dataclass section, converting path fields."""
    if not isinstance(data, dict):
        raise ValueError(f"config section {section_cls.__name__} must be a table")
    kwargs: dict = {}
    for f in section_cls.__dataclass_fields__.values():
        if f.name not in data:
            continue
        v = data[f.name]
        if f.type is Path or f.name in ("path", "dir"):
            v = _expand(v)
        kwargs[f.name] = v
    base = section_cls()
    for k, v in kwargs.items():
        setattr(base, k, v)
    return base


def load_config(path: Path | None = None) -> Config:
    """Load a Config from `path` (default: ~/.config/abeomem/config.toml).
    Missing file → Config() with all defaults."""
    p = path if path is not None else DEFAULT_CONFIG_PATH
    if not Path(p).exists():
        return Config()
    with open(p, "rb") as f:
        data = tomllib.load(f)

    cfg = Config()
    if "db" in data:
        cfg.db = _overlay(DBConfig, data["db"])  # type: ignore[assignment]
    if "memos" in data:
        cfg.memos = _overlay(MemosConfig, data["memos"])  # type: ignore[assignment]
    if "backup" in data:
        cfg.backup = _overlay(BackupConfig, data["backup"])  # type: ignore[assignment]
    if "scope" in data:
        cfg.scope = _overlay(ScopeConfig, data["scope"])  # type: ignore[assignment]
    if "retrieval" in data:
        cfg.retrieval = _overlay(RetrievalConfig, data["retrieval"])  # type: ignore[assignment]
    if "logging" in data:
        cfg.logging = _overlay(LoggingConfig, data["logging"])  # type: ignore[assignment]
    return cfg
