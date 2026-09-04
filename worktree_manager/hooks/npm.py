"""
npm dependency-install hook for worktree lifecycle events.

After a worktree is created, runs ``npm ci`` in it so the new checkout has its
Node dependencies (esbuild etc.) installed, then ``npm run build`` so the
generated bundle exists too. ``node_modules`` and build output are gitignored
and per-worktree, so a fresh worktree has neither until this runs. Only fires
for npm projects (a ``package.json`` and ``package-lock.json`` are present) and
when ``npm`` is on PATH.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

from . import HookResult, WorktreeLifecycleContext
from .config import HookConfig


class NpmInstallHook:
    """Run ``npm ci`` (then ``npm run build``) in a newly created worktree."""

    name = 'npm_install'
    description = 'Run npm ci and npm run build after worktree creation'

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
        """Invoke ``npm ci``, then the project's build script, in the worktree directory."""
        config = HookConfig.load()
        # Extra flags let a user opt into e.g. --no-audit without code changes.
        extra_args = config.get_hook_setting(self.name, 'extra_args', []) or []

        failure = self._run(ctx, ['npm', 'ci', *extra_args], 'npm ci')
        if failure is not None:
            return failure

        if not config.get_hook_setting(self.name, 'run_build', True):
            return HookResult(
                success=True,
                message='Installed Node dependencies with npm ci',
                action_taken='npm_installed',
            )

        script = config.get_hook_setting(self.name, 'build_script', 'build') or 'build'
        if not self._has_script(ctx, script):
            return HookResult(
                success=True,
                message='Installed Node dependencies with npm ci',
                action_taken='npm_installed',
            )

        label = f'npm run {script}'
        failure = self._run(ctx, ['npm', 'run', script], label)
        if failure is not None:
            # npm ci already succeeded, so report the partial result rather than
            # implying the worktree has no dependencies at all.
            failure.message = f'Installed Node dependencies, but {failure.message}'
            failure.action_taken = 'npm_installed'
            return failure

        return HookResult(
            success=True,
            message=f'Installed Node dependencies and built assets with {label}',
            action_taken='npm_installed_and_built',
        )

    def _has_script(self, ctx: WorktreeLifecycleContext, script: str) -> bool:
        """Whether the worktree's package.json defines the given npm script."""
        try:
            data = json.loads((ctx.worktree_path / 'package.json').read_text())
        except (OSError, json.JSONDecodeError) as e:
            logging.getLogger('worktree_manager.hooks').warning(f'Could not read package.json: {e}')
            return False
        scripts = data.get('scripts')
        return isinstance(scripts, dict) and script in scripts

    def _run(self, ctx: WorktreeLifecycleContext, cmd: list[str], label: str) -> HookResult | None:
        """Run a command in the worktree; return a failure HookResult, or None on success."""
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
            logging.getLogger('worktree_manager.hooks').warning(f'{label} failed: {e.stderr}')
            stderr = (e.stderr or '').strip().splitlines()
            detail = stderr[-1] if stderr else f'exit code {e.returncode}'
            return HookResult(success=False, message=f'{label} failed: {detail}')
        return None
