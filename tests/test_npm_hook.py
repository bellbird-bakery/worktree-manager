"""Tests for the npm dependency-install lifecycle hook."""

from __future__ import annotations

import json
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


def make_npm_project_with_build(tmp_path: Path, script: str = 'build') -> Path:
    (tmp_path / 'package.json').write_text(json.dumps({'name': 'x', 'scripts': {script: 'node esbuild.config.js'}}))
    (tmp_path / 'package-lock.json').write_text('{}')
    return tmp_path


def record_runs(monkeypatch, fail_on: list[str] | None = None) -> list[list[str]]:
    """Capture subprocess commands; optionally raise CalledProcessError for one command."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if fail_on is not None and cmd == fail_on:
            raise subprocess.CalledProcessError(1, cmd, stderr='esbuild: build failed')
        return type('R', (), {'returncode': 0})()

    monkeypatch.setattr(m.subprocess, 'run', fake_run)
    return calls


def test_execute_runs_build_after_install(tmp_path, monkeypatch):
    make_npm_project_with_build(tmp_path)
    monkeypatch.setattr(HookConfig, 'load', classmethod(lambda cls: HookConfig()))
    calls = record_runs(monkeypatch)

    result = NpmInstallHook().execute(make_ctx(worktree_path=tmp_path))

    assert calls == [['npm', 'ci'], ['npm', 'run', 'build']]
    assert result.success is True
    assert result.action_taken == 'npm_installed_and_built'


def test_execute_skips_build_without_build_script(tmp_path, monkeypatch):
    make_npm_project(tmp_path)  # package.json with no "scripts"
    monkeypatch.setattr(HookConfig, 'load', classmethod(lambda cls: HookConfig()))
    calls = record_runs(monkeypatch)

    result = NpmInstallHook().execute(make_ctx(worktree_path=tmp_path))

    assert calls == [['npm', 'ci']]
    assert result.success is True
    assert result.action_taken == 'npm_installed'


def test_execute_honours_run_build_false(tmp_path, monkeypatch):
    make_npm_project_with_build(tmp_path)
    monkeypatch.setattr(
        HookConfig, 'load', classmethod(lambda cls: HookConfig(hooks={'npm_install': {'run_build': False}}))
    )
    calls = record_runs(monkeypatch)

    NpmInstallHook().execute(make_ctx(worktree_path=tmp_path))

    assert calls == [['npm', 'ci']]


def test_execute_uses_configured_build_script(tmp_path, monkeypatch):
    make_npm_project_with_build(tmp_path, script='build:prod')
    monkeypatch.setattr(
        HookConfig, 'load', classmethod(lambda cls: HookConfig(hooks={'npm_install': {'build_script': 'build:prod'}}))
    )
    calls = record_runs(monkeypatch)

    NpmInstallHook().execute(make_ctx(worktree_path=tmp_path))

    assert calls == [['npm', 'ci'], ['npm', 'run', 'build:prod']]


def test_execute_reports_build_failure_as_partial(tmp_path, monkeypatch):
    make_npm_project_with_build(tmp_path)
    monkeypatch.setattr(HookConfig, 'load', classmethod(lambda cls: HookConfig()))
    record_runs(monkeypatch, fail_on=['npm', 'run', 'build'])

    result = NpmInstallHook().execute(make_ctx(worktree_path=tmp_path))

    assert result.success is False
    assert 'Installed Node dependencies' in result.message
    assert 'npm run build failed' in result.message
    assert result.action_taken == 'npm_installed'


def test_execute_skips_build_when_install_fails(tmp_path, monkeypatch):
    make_npm_project_with_build(tmp_path)
    monkeypatch.setattr(HookConfig, 'load', classmethod(lambda cls: HookConfig()))
    calls = record_runs(monkeypatch, fail_on=['npm', 'ci'])

    result = NpmInstallHook().execute(make_ctx(worktree_path=tmp_path))

    assert calls == [['npm', 'ci']]
    assert result.success is False
    assert 'npm ci failed' in result.message
