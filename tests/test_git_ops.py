"""Tests for git_ops helpers that need a real git repository."""

from __future__ import annotations

import subprocess

import pytest

from worktree_manager import git_ops


def _git(cwd, *args):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo_with_worktree(tmp_path):
    """A main repo on `main` plus one linked worktree on `feature/x`."""
    main = tmp_path / 'main'
    main.mkdir()
    _git(main, 'init', '-b', 'main')
    _git(main, 'config', 'user.email', 't@t.co')
    _git(main, 'config', 'user.name', 't')
    (main / 'README.md').write_text('hi\n')
    _git(main, 'add', '-A')
    _git(main, 'commit', '-m', 'init')

    wt = tmp_path / 'wt-x'
    _git(main, 'worktree', 'add', '-b', 'feature/x', str(wt), 'main')
    return main, wt


def test_is_worktree_false_in_main_repo(repo_with_worktree, monkeypatch):
    main, _ = repo_with_worktree
    monkeypatch.chdir(main)
    assert git_ops.is_worktree() is False


def test_is_worktree_true_in_linked_worktree(repo_with_worktree, monkeypatch):
    _, wt = repo_with_worktree
    monkeypatch.chdir(wt)
    assert git_ops.is_worktree() is True


def test_is_worktree_false_in_subdir_of_main(repo_with_worktree, monkeypatch):
    main, _ = repo_with_worktree
    subdir = main / 'pkg'
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    # A subdirectory of the main checkout is still not a worktree.
    assert git_ops.is_worktree() is False
