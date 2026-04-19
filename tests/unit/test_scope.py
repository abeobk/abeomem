import subprocess

import pytest

from abeomem.scope import normalize_remote_url, resolve_scope


@pytest.mark.parametrize(
    "raw",
    [
        "git@github.com:Abeo/project.git",
        "https://github.com/abeo/project.git",
        "https://github.com/abeo/project/",
        "https://www.github.com/Abeo/project",
        "https://github.com/abeo/project",
    ],
)
def test_all_spec_forms_normalize_same(raw):
    expected = "https://github.com/abeo/project"
    assert normalize_remote_url(raw) == expected


def test_non_git_dir_is_global(tmp_path):
    result = resolve_scope(tmp_path)
    assert result.scope_id == "global"
    assert result.anchor_path == tmp_path.resolve()


def _init_repo(path, *, with_remote=False):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    if with_remote:
        subprocess.run(
            ["git", "-C", str(path), "remote", "add", "origin",
             "git@github.com:abeo/project.git"],
            check=True,
        )


def test_git_with_remote_is_repo_hash(tmp_path):
    _init_repo(tmp_path, with_remote=True)
    result = resolve_scope(tmp_path)
    assert result.scope_id.startswith("repo:")
    assert not result.scope_id.startswith("repo:path:")
    # 16 lowercase hex chars after "repo:"
    tail = result.scope_id[len("repo:"):]
    assert len(tail) == 16
    assert all(c in "0123456789abcdef" for c in tail)
    assert result.anchor_path == tmp_path.resolve()


def test_git_without_remote_is_repo_path(tmp_path):
    _init_repo(tmp_path, with_remote=False)
    result = resolve_scope(tmp_path)
    assert result.scope_id.startswith("repo:path:")
    tail = result.scope_id[len("repo:path:"):]
    assert len(tail) == 16
    assert all(c in "0123456789abcdef" for c in tail)


def test_same_remote_different_clones_same_scope(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _init_repo(a, with_remote=True)
    _init_repo(b, with_remote=True)
    assert resolve_scope(a).scope_id == resolve_scope(b).scope_id
