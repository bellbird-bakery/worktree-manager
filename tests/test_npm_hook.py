"""Tests for the npm dependency-install lifecycle hook."""

from __future__ import annotations

import subprocess
from pathlib import Path

from worktree_manager.hooks import WorktreeLifecycleContext
from worktree_manager.hooks import npm as m
from worktree_manager.hooks.config import HookConfig
from worktree_manager.hooks.npm import NpmInstallHook


def make_ctx(event: str = 'create', worktree_path: Path = Path('/tmp/worktrees/auth-fix')) -> WorktreeLifecycleContext:
    return WorktreeLifecycleContext(
        event=event,
        worktree_path=worktree_path,
        feature_name='auth-fix',
        main_repo_path=Path('/tmp/main'),
        branch_name='feature/auth-fix',
    )


def make_npm_project(tmp_path: Path, *, lockfile: bool = True) -> Path:
    (tmp_path / 'package.json').write_text('{"name": "x"}')
    if lockfile:
        (tmp_path / 'package-lock.json').write_text('{}')
    return tmp_path


def test_should_run_true_for_npm_project(tmp_path, monkeypatch):
    make_npm_project(tmp_path)
    monkeypatch.setattr(m.shutil, 'which', lambda _: '/usr/bin/npm')
    assert NpmInstallHook().should_run(make_ctx(worktree_path=tmp_path)) is True


def test_should_run_only_on_create(tmp_path, monkeypatch):
    make_npm_project(tmp_path)
    monkeypatch.setattr(m.shutil, 'which', lambda _: '/usr/bin/npm')
    assert NpmInstallHook().should_run(make_ctx(event='close', worktree_path=tmp_path)) is False


def test_should_run_false_without_package_json(tmp_path, monkeypatch):
    monkeypatch.setattr(m.shutil, 'which', lambda _: '/usr/bin/npm')
    assert NpmInstallHook().should_run(make_ctx(worktree_path=tmp_path)) is False


def test_should_run_false_without_lockfile(tmp_path, monkeypatch):
    make_npm_project(tmp_path, lockfile=False)
    monkeypatch.setattr(m.shutil, 'which', lambda _: '/usr/bin/npm')
    assert NpmInstallHook().should_run(make_ctx(worktree_path=tmp_path)) is False


def test_should_run_false_when_npm_missing(tmp_path, monkeypatch):
    make_npm_project(tmp_path)
    monkeypatch.setattr(m.shutil, 'which', lambda _: None)
    assert NpmInstallHook().should_run(make_ctx(worktree_path=tmp_path)) is False


def test_execute_runs_npm_ci(monkeypatch):
    monkeypatch.setattr(HookConfig, 'load', classmethod(lambda cls: HookConfig()))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return type('R', (), {'returncode': 0})()

    monkeypatch.setattr(m.subprocess, 'run', fake_run)

    result = NpmInstallHook().execute(make_ctx())

    assert result.success is True
    assert result.action_taken == 'npm_installed'
    assert calls[0][0] == ['npm', 'ci']
    assert calls[0][1]['cwd'] == '/tmp/worktrees/auth-fix'


def test_execute_passes_extra_args(monkeypatch):
    monkeypatch.setattr(
        HookConfig, 'load', classmethod(lambda cls: HookConfig(hooks={'npm_install': {'extra_args': ['--no-audit']}}))
    )
    calls = []
    monkeypatch.setattr(m.subprocess, 'run', lambda cmd, **kw: calls.append(cmd) or type('R', (), {'returncode': 0})())

    NpmInstallHook().execute(make_ctx())

    assert calls[0] == ['npm', 'ci', '--no-audit']


def test_execute_reports_failure(monkeypatch):
    monkeypatch.setattr(HookConfig, 'load', classmethod(lambda cls: HookConfig()))

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr='npm error: lockfile out of sync')

    monkeypatch.setattr(m.subprocess, 'run', fake_run)

    result = NpmInstallHook().execute(make_ctx())

    assert result.success is False
    assert 'npm ci failed' in result.message
