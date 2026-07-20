"""Tests that `wt create` branches from the project's configured base branch."""

from __future__ import annotations

import json
import subprocess

import pytest

from worktree_manager import commands
from worktree_manager import registry as registry_mod


def _git(cwd, *args) -> str:
    result = subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def main_only_repo(tmp_path, monkeypatch):
    """A repo whose only branch is `main`, configured with base_branch=main.

    Mirrors a project that never had a `develop` branch — branching from the
    hardcoded default fails outright with "invalid reference: develop".
    """
    repo = tmp_path / 'proj'
    repo.mkdir()
    _git(repo, 'init', '-b', 'main')
    _git(repo, 'config', 'user.email', 't@t.co')
    _git(repo, 'config', 'user.name', 't')
    (repo / 'README.md').write_text('hi\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'init')

    (repo / '.worktree-manager.json').write_text(json.dumps({'base_branch': 'main'}))

    # Redirect the registry off the user's real config directory.
    config_dir = tmp_path / 'config'
    monkeypatch.setattr(registry_mod, 'CONFIG_DIR', str(config_dir))
    monkeypatch.setattr(registry_mod, 'REGISTRY_FILE', str(config_dir / 'registry.json'))
    monkeypatch.chdir(repo)
    return repo


def test_create_uses_project_base_branch(main_only_repo):
    """create_worktree_cmd branches from base_branch in .worktree-manager.json."""
    assert commands.create_worktree_cmd('smoke', branch_type='feature') == 0

    branches = _git(main_only_repo, 'branch', '--format=%(refname:short)').split('\n')
    assert 'feature/smoke' in branches

    # The new branch must point at main's tip, not some other base.
    assert _git(main_only_repo, 'rev-parse', 'feature/smoke') == _git(main_only_repo, 'rev-parse', 'main')
