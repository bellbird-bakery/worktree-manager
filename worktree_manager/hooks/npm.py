"""
npm dependency-install hook for worktree lifecycle events.

After a worktree is created, runs ``npm ci`` in it so the new checkout has its
Node dependencies (esbuild etc.) installed. ``node_modules`` is gitignored and
per-worktree, so a fresh worktree has none until this runs. Only fires for npm
projects (a ``package.json`` and ``package-lock.json`` are present) and when
``npm`` is on PATH.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from . import HookResult, WorktreeLifecycleContext
from .config import HookConfig


class NpmInstallHook:
    """Run ``npm ci`` in a newly created worktree to install Node dependencies."""

    name = 'npm_install'
    description = 'Run npm ci to install Node dependencies after worktree creation'

    def should_run(self, ctx: WorktreeLifecycleContext) -> bool:
        """Run on worktree creation for npm projects (with a lockfile) when npm is available."""
        if ctx.event != 'create':
            return False
        if not (ctx.worktree_path / 'package.json').exists():
            return False
        # npm ci requires a lockfile; without one, skip rather than fail the create.
        if not (ctx.worktree_path / 'package-lock.json').exists():
            return False
        return shutil.which('npm') is not None

    def execute(self, ctx: WorktreeLifecycleContext) -> HookResult:
        """Invoke ``npm ci`` in the worktree directory."""
        config = HookConfig.load()
        # Extra flags let a user opt into e.g. --no-audit without code changes.
        extra_args = config.get_hook_setting(self.name, 'extra_args', []) or []

        cmd = ['npm', 'ci', *extra_args]
        try:
            subprocess.run(
                cmd,
                cwd=str(ctx.worktree_path),
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return HookResult(success=False, message='npm not found on PATH')
        except subprocess.CalledProcessError as e:
            logging.getLogger('worktree_manager.hooks').warning(f'npm ci failed: {e.stderr}')
            stderr = (e.stderr or '').strip().splitlines()
            detail = stderr[-1] if stderr else f'exit code {e.returncode}'
            return HookResult(success=False, message=f'npm ci failed: {detail}')

        return HookResult(
            success=True,
            message='Installed Node dependencies with npm ci',
            action_taken='npm_installed',
        )
