"""Tests for the shared dev image lifecycle hook."""

from __future__ import annotations

import subprocess
from pathlib import Path

import worktree_manager.hooks as hooks_pkg
from worktree_manager.config import Config, ProjectConfig
from worktree_manager.hooks import WorktreeLifecycleContext, get_lifecycle_hook_manager
from worktree_manager.hooks import shared_image as m
from worktree_manager.hooks.shared_image import SharedImageHook


def make_ctx(event: str = 'create') -> WorktreeLifecycleContext:
    return WorktreeLifecycleContext(
        event=event,
        worktree_path=Path('/tmp/worktrees/auth-fix'),
        feature_name='auth-fix',
        main_repo_path=Path('/tmp/main'),
        branch_name='feature/auth-fix',
    )


def fake_project(dev_image: str | None, compose_file: str = 'docker-compose.local.yml') -> ProjectConfig:
    return ProjectConfig(project_name='dispatch-guru', dev_image=dev_image, compose_file=compose_file)


def patch_should_run_env(
    monkeypatch,
    *,
    dev_image: str | None = 'dispatch-guru-dev:latest',
    auto_build: bool = True,
    docker_running: bool = True,
    image_exists: bool = False,
) -> None:
    monkeypatch.setattr(m.ProjectConfig, 'load', classmethod(lambda cls, repo_path=None: fake_project(dev_image)))
    monkeypatch.setattr(m.Config, 'load', classmethod(lambda cls: Config(docker={'auto_build': auto_build})))
    monkeypatch.setattr(m, 'is_docker_running', lambda: docker_running)
    monkeypatch.setattr(SharedImageHook, '_image_exists', lambda self, image: image_exists)


def test_should_run_true_when_all_conditions_met(monkeypatch):
    patch_should_run_env(monkeypatch)
    assert SharedImageHook().should_run(make_ctx()) is True


def test_should_run_only_on_create(monkeypatch):
    patch_should_run_env(monkeypatch)
    assert SharedImageHook().should_run(make_ctx(event='close')) is False


def test_should_run_false_when_dev_image_unset(monkeypatch):
    patch_should_run_env(monkeypatch, dev_image=None)
    assert SharedImageHook().should_run(make_ctx()) is False


def test_should_run_false_when_auto_build_off(monkeypatch):
    patch_should_run_env(monkeypatch, auto_build=False)
    assert SharedImageHook().should_run(make_ctx()) is False


def test_should_run_false_when_docker_not_running(monkeypatch):
    patch_should_run_env(monkeypatch, docker_running=False)
    assert SharedImageHook().should_run(make_ctx()) is False


def test_should_run_false_when_image_present(monkeypatch):
    patch_should_run_env(monkeypatch, image_exists=True)
    assert SharedImageHook().should_run(make_ctx()) is False


def test_execute_runs_compose_build(monkeypatch):
    monkeypatch.setattr(
        m.ProjectConfig, 'load', classmethod(lambda cls, repo_path=None: fake_project('dispatch-guru-dev:latest'))
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return type('R', (), {'returncode': 0})()

    monkeypatch.setattr(m.subprocess, 'run', fake_run)

    result = SharedImageHook().execute(make_ctx())

    assert result.success is True
    assert result.action_taken == 'shared_image_built'
    assert calls[0][0] == ['docker', 'compose', '-f', 'docker-compose.local.yml', 'build']
    assert calls[0][1]['cwd'] == '/tmp/main'


def test_execute_build_failure_returns_unsuccessful(monkeypatch):
    monkeypatch.setattr(
        m.ProjectConfig, 'load', classmethod(lambda cls, repo_path=None: fake_project('dispatch-guru-dev:latest'))
    )

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(m.subprocess, 'run', fake_run)

    result = SharedImageHook().execute(make_ctx())
    assert result.success is False
    assert 'failed' in result.message.lower()


def test_execute_docker_missing(monkeypatch):
    monkeypatch.setattr(
        m.ProjectConfig, 'load', classmethod(lambda cls, repo_path=None: fake_project('dispatch-guru-dev:latest'))
    )

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(m.subprocess, 'run', fake_run)

    result = SharedImageHook().execute(make_ctx())
    assert result.success is False
    assert 'docker not found' in result.message.lower()


def test_hook_registered_by_default(monkeypatch):
    monkeypatch.setattr(hooks_pkg, '_lifecycle_hook_manager', None)
    manager = get_lifecycle_hook_manager()
    names = [hook.name for hook in manager._hooks]
    assert 'shared_image' in names
