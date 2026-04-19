import subprocess

import pytest

from abeomem.claude_md import (
    BEGIN_MARKER,
    END_MARKER,
    TEMPLATE,
    install_claude_md,
)


def test_fresh_install(tmp_path):
    target = tmp_path / "CLAUDE.md"
    action = install_claude_md(target, confirm_append=False, confirm_shared_repo=False)
    assert action == "created"
    content = target.read_text()
    assert BEGIN_MARKER in content
    assert END_MARKER in content


def test_idempotent_reinstall(tmp_path):
    target = tmp_path / "CLAUDE.md"
    install_claude_md(target, confirm_append=False, confirm_shared_repo=False)
    # Second install updates (not appends)
    action = install_claude_md(target, confirm_append=False, confirm_shared_repo=False)
    assert action == "updated"
    # The block appears exactly once
    assert target.read_text().count(BEGIN_MARKER) == 1


def test_preserves_other_content(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text("# Existing project context\n\n"
                      "Some rules here.\n\n"
                      f"{TEMPLATE}"
                      "More rules after block.\n")
    # Modify the block content to verify replacement (not duplication)
    install_claude_md(target, confirm_append=False, confirm_shared_repo=False)
    content = target.read_text()
    assert "# Existing project context" in content
    assert "More rules after block" in content
    assert content.count(BEGIN_MARKER) == 1


def test_append_to_existing_without_markers(tmp_path, monkeypatch):
    target = tmp_path / "CLAUDE.md"
    target.write_text("# Other rules\n")
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    action = install_claude_md(target, confirm_shared_repo=False)
    assert action == "appended"
    content = target.read_text()
    assert content.startswith("# Other rules")
    assert BEGIN_MARKER in content


def test_refuse_append_on_existing(tmp_path, monkeypatch):
    target = tmp_path / "CLAUDE.md"
    target.write_text("# Other rules\n")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    action = install_claude_md(target, confirm_shared_repo=False)
    assert action == "skipped"
    assert target.read_text() == "# Other rules\n"


def test_warn_on_git_tracked_file(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    target = tmp_path / "CLAUDE.md"
    target.write_text("# existing content\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "CLAUDE.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)

    answers = iter(["n"])  # refuse the shared-repo confirmation
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    action = install_claude_md(target)
    assert action == "skipped"


def test_global_install_outside_git(tmp_path, monkeypatch):
    """`abeomem init` (no flag) outside git refuses; --global works."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg_dir = tmp_path / "home" / ".config" / "abeomem"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(f"""
[db]
path = "{tmp_path}/kb.db"

[memos]
dir = "{tmp_path}/memos"

[backup]
dir = "{tmp_path}/backups"
""")

    from abeomem.claude_md import run_init

    # Non-git-non-global: should exit non-zero
    with pytest.raises(SystemExit) as exc:
        run_init(is_global=False)
    assert exc.value.code == 2

    # Global install succeeds
    run_init(is_global=True)
    target = tmp_path / "home" / ".claude" / "CLAUDE.md"
    assert target.exists()
    assert BEGIN_MARKER in target.read_text()
