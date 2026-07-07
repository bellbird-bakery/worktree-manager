"""
uv dependency-sync hook for worktree lifecycle events.

After a worktree is created, runs ``uv sync`` in it so the new checkout has an
up-to-date virtualenv with its dev dependencies installed. Only runs for uv
projects (a ``pyproject.toml`` and ``uv.lock`` are present) and when ``uv`` is
on PATH.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from . import HookResult, WorktreeLifecycleContext
from .config import HookConfig

logger = logging.getLogger('worktree_manager.hooks')


class UvSyncHook:
    """Run ``uv sync`` (including the dev group) in a newly created worktree."""

    name = 'uv_sync'
    description = 'Run uv sync to install dependencies after worktree creation'

    def should_run(self, ctx: WorktreeLifecycleContext) -> bool:
        """Run on worktree creation for uv projects with uv available."""
        if ctx.event != 'create':
            return False
        if not (ctx.worktree_path / 'pyproject.toml').exists():
            return False
        if not (ctx.worktree_path / 'uv.lock').exists():
            return False
        return shutil.which('uv') is not None

    def execute(self, ctx: WorktreeLifecycleContext) -> HookResult:
        """Invoke ``uv sync`` in the worktree directory."""
        config = HookConfig.load()
        # Extra flags let a user opt into e.g. --all-extras without code changes.
        extra_args = config.get_hook_setting(self.name, 'extra_args', []) or []

        cmd = ['uv', 'sync', *extra_args]
        try:
            subprocess.run(
                cmd,
                cwd=str(ctx.worktree_path),
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return HookResult(success=False, message='uv not found on PATH')
        except subprocess.CalledProcessError as e:
            logger.warning(f'uv sync failed: {e.stderr}')
            stderr = (e.stderr or '').strip().splitlines()
            detail = stderr[-1] if stderr else f'exit code {e.returncode}'
            return HookResult(success=False, message=f'uv sync failed: {detail}')

        return HookResult(
            success=True,
            message='Installed dependencies with uv sync',
            action_taken='uv_synced',
        )
