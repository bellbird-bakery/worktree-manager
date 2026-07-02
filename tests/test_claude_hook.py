"""Tests for the Claude session lifecycle hook."""

from __future__ import annotations

from pathlib import Path

import worktree_manager.hooks as hooks_pkg
from worktree_manager.hooks import HookConfig, WorktreeLifecycleContext, get_lifecycle_hook_manager
from worktree_manager.hooks.claude import ClaudeLaunchHook


def make_ctx(event: str = 'create', interactive: bool = True) -> WorktreeLifecycleContext:
    return WorktreeLifecycleContext(
        event=event,
        worktree_path=Path('/tmp/worktrees/auth-fix'),
        feature_name='auth-fix',
        main_repo_path=Path('/tmp/main'),
        branch_name='feature/auth-fix',
        interactive=interactive,
    )


def config_with(auto_launch: bool) -> HookConfig:
    return HookConfig(hooks={'claude_launch': {'enabled': True, 'auto_launch': auto_launch}})


def test_should_run_only_on_create():
    hook = ClaudeLaunchHook()
    assert hook.should_run(make_ctx(event='create')) is True
    assert hook.should_run(make_ctx(event='close')) is False


def test_execute_prints_launch_hint(monkeypatch):
    from worktree_manager.hooks import claude as claude_hook_module

    monkeypatch.setattr(claude_hook_module.HookConfig, 'load', classmethod(lambda cls: config_with(False)))
    hook = ClaudeLaunchHook()
    result = hook.execute(make_ctx())
    assert result.success is True
    assert 'wt claude auth-fix' in result.message


def test_execute_auto_launch_uses_tmux_window(monkeypatch):
    from worktree_manager.hooks import claude as claude_hook_module

    monkeypatch.setattr(claude_hook_module.HookConfig, 'load', classmethod(lambda cls: config_with(True)))
    monkeypatch.setenv('TMUX', '/tmp/tmux-1000/default,1234,0')

    calls = []
    monkeypatch.setattr(
        claude_hook_module.subprocess,
        'run',
        lambda cmd, **kwargs: calls.append(cmd) or type('R', (), {'returncode': 0})(),
    )

    hook = ClaudeLaunchHook()
    result = hook.execute(make_ctx())

    assert result.success is True
    assert result.action_taken == 'claude_launched'
    assert calls and calls[0][:2] == ['tmux', 'new-window']
    assert '/tmp/worktrees/auth-fix' in calls[0]


def test_execute_auto_launch_without_tmux_falls_back_to_hint(monkeypatch):
    from worktree_manager.hooks import claude as claude_hook_module

    monkeypatch.setattr(claude_hook_module.HookConfig, 'load', classmethod(lambda cls: config_with(True)))
    monkeypatch.delenv('TMUX', raising=False)

    hook = ClaudeLaunchHook()
    result = hook.execute(make_ctx())

    assert result.success is True
    assert 'wt claude auth-fix' in result.message


def test_hook_registered_by_default(monkeypatch):
    monkeypatch.setattr(hooks_pkg, '_lifecycle_hook_manager', None)
    manager = get_lifecycle_hook_manager()
    names = [hook.name for hook in manager._hooks]
    assert 'claude_launch' in names
