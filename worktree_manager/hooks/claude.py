"""
Claude Code session hook for worktree lifecycle events.

After a worktree is created, prints the command to start a Claude Code
session in it. With auto_launch enabled and a tmux session active, opens
the session in a new tmux window instead.
"""

from __future__ import annotations

import logging
import os
import subprocess

from . import HookResult, WorktreeLifecycleContext
from .config import HookConfig

logger = logging.getLogger('worktree_manager.hooks')


class ClaudeLaunchHook:
    """Suggest or launch a Claude Code session after worktree creation."""

    name = 'claude_launch'
    description = 'Show or launch a Claude Code session after worktree creation'

    def should_run(self, ctx: WorktreeLifecycleContext) -> bool:
        """Only run when a worktree is created."""
        return ctx.event == 'create'

    def execute(self, ctx: WorktreeLifecycleContext) -> HookResult:
        """Print the launch command, or open Claude in a new tmux window."""
        config = HookConfig.load()
        auto_launch = config.get_hook_setting(self.name, 'auto_launch', False)

        if auto_launch and ctx.interactive and os.environ.get('TMUX'):
            try:
                subprocess.run(
                    ['tmux', 'new-window', '-c', str(ctx.worktree_path), '-n', ctx.feature_name, 'claude'],
                    check=True,
                    capture_output=True,
                )
                return HookResult(
                    success=True,
                    message=f'Opened Claude Code in a new tmux window for {ctx.feature_name}',
                    action_taken='claude_launched',
                )
            except (FileNotFoundError, subprocess.CalledProcessError) as e:
                logger.warning(f'tmux launch failed: {e}')
                return HookResult(
                    success=False,
                    message=f'Could not open tmux window: {e}',
                )

        return HookResult(
            success=True,
            message=f'Start a Claude session: wt claude {ctx.feature_name}',
        )
