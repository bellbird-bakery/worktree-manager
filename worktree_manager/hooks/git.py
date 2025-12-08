"""
Git hooks for task status transitions.

Prompts for commit when completing a task.
"""

from __future__ import annotations

import logging
import subprocess

from rich.console import Console
from rich.prompt import Confirm, Prompt

from ..task_store import TaskStatus
from . import HookResult, TransitionContext

logger = logging.getLogger('worktree_manager.hooks')
console = Console()


def get_git_diff_stat(worktree_path: str | None = None) -> str:
    """Get git diff --stat output."""
    try:
        result = subprocess.run(
            ['git', 'diff', '--stat', 'HEAD'],
            capture_output=True,
            text=True,
            cwd=worktree_path,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ''


def get_git_status_short(worktree_path: str | None = None) -> str:
    """Get git status --short output."""
    try:
        result = subprocess.run(
            ['git', 'status', '--short'],
            capture_output=True,
            text=True,
            cwd=worktree_path,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return ''


class GitCommitHook:
    """Prompt for commit when completing a task."""

    name = 'git_commit'
    description = 'Commit changes when completing task'

    def should_run(self, ctx: TransitionContext) -> bool:
        """Run when moving to DONE from IN_PROGRESS."""
        if ctx.new_status != TaskStatus.DONE:
            return False
        if ctx.old_status != TaskStatus.IN_PROGRESS:
            return False
        # Only run if we have worktree path
        return ctx.worktree_path is not None

    def pre_check(self, ctx: TransitionContext) -> HookResult | None:
        """
        Check if there are uncommitted changes that need user confirmation.

        In webapp context (non-interactive), returns requires_confirmation=True
        with diff data for the modal.
        In CLI context (interactive), returns None and handles in execute().
        """
        if not ctx.worktree_path:
            return None

        # Check for uncommitted changes
        worktree_str = str(ctx.worktree_path)

        try:
            # Run git status in the worktree directory
            result = subprocess.run(
                ['git', 'status', '--porcelain'],
                capture_output=True,
                text=True,
                cwd=worktree_str,
            )
            has_changes = bool(result.stdout.strip())
        except subprocess.CalledProcessError:
            has_changes = False

        if not has_changes:
            return None  # No confirmation needed

        # In interactive (CLI) mode, we handle prompts in execute()
        if ctx.interactive:
            return None

        # In non-interactive (webapp) mode, return confirmation data
        diff_stat = get_git_diff_stat(worktree_str)
        status_short = get_git_status_short(worktree_str)

        from worktree_manager.hooks.config import HookConfig

        config = HookConfig.load()
        default_message = config.get_hook_setting('git_commit', 'default_message', 'Complete {feature_name}')
        default_message = default_message.format(feature_name=ctx.task.feature_name)

        return HookResult(
            success=True,
            message='Uncommitted changes found',
            requires_confirmation=True,
            confirmation_type='git_commit',
            confirmation_data={
                'diff_stat': diff_stat,
                'status_short': status_short,
                'default_message': default_message,
                'feature_name': ctx.task.feature_name,
            },
        )

    def execute(self, ctx: TransitionContext) -> HookResult:
        """
        Commit changes if user confirms.

        In CLI mode, prompts interactively.
        In webapp mode, uses user_data from confirmation modal.
        """
        if not ctx.worktree_path:
            return HookResult(
                success=True,
                message='No worktree path, skipping git commit',
            )

        worktree_str = str(ctx.worktree_path)

        # Check for uncommitted changes
        try:
            result = subprocess.run(
                ['git', 'status', '--porcelain'],
                capture_output=True,
                text=True,
                cwd=worktree_str,
            )
            has_changes = bool(result.stdout.strip())
        except subprocess.CalledProcessError:
            has_changes = False

        if not has_changes:
            return HookResult(
                success=True,
                message='No uncommitted changes',
            )

        # Get config
        from worktree_manager.hooks.config import HookConfig

        config = HookConfig.load()
        default_message = config.get_hook_setting('git_commit', 'default_message', 'Complete {feature_name}')
        default_message = default_message.format(feature_name=ctx.task.feature_name)

        if ctx.interactive:
            # CLI mode: prompt user
            return self._execute_interactive(ctx, worktree_str, default_message)
        else:
            # Webapp mode: check if user confirmed via modal
            return self._execute_from_confirmation(ctx, worktree_str, default_message)

    def _execute_interactive(self, ctx: TransitionContext, worktree_str: str, default_message: str) -> HookResult:
        """Execute in interactive CLI mode with prompts."""
        console.print('\n[bold yellow]Uncommitted changes detected:[/bold yellow]')

        # Show status
        status = get_git_status_short(worktree_str)
        if status:
            console.print(status)

        # Show diff stat
        diff_stat = get_git_diff_stat(worktree_str)
        if diff_stat:
            console.print(f'\n{diff_stat}')

        # Prompt for confirmation
        if not Confirm.ask('\nCommit these changes?', default=True):
            return HookResult(
                success=True,
                message='User skipped commit',
                action_taken='commit_skipped',
            )

        # Get commit message
        message = Prompt.ask('Commit message', default=default_message)

        # Do the commit
        try:
            # Stage all and commit
            subprocess.run(['git', 'add', '-A'], check=True, cwd=worktree_str)
            subprocess.run(['git', 'commit', '-m', message], check=True, cwd=worktree_str)

            console.print(f'[green]Committed: {message}[/green]')
            return HookResult(
                success=True,
                message=f'Committed: {message}',
                action_taken='committed',
            )
        except subprocess.CalledProcessError as e:
            logger.error(f'Git commit failed: {e}')
            return HookResult(
                success=False,
                message=f'Git commit failed: {e}',
            )

    def _execute_from_confirmation(self, ctx: TransitionContext, worktree_str: str, default_message: str) -> HookResult:
        """Execute using confirmation data from webapp modal."""
        if not ctx.user_confirmed:
            # User didn't confirm (e.g., clicked skip in modal)
            return HookResult(
                success=True,
                message='User skipped commit',
                action_taken='commit_skipped',
            )

        # Get commit message from user data
        message = ctx.user_data.get('commit_message', default_message)

        try:
            subprocess.run(['git', 'add', '-A'], check=True, cwd=worktree_str)
            subprocess.run(['git', 'commit', '-m', message], check=True, cwd=worktree_str)

            return HookResult(
                success=True,
                message=f'Committed: {message}',
                action_taken='committed',
            )
        except subprocess.CalledProcessError as e:
            logger.error(f'Git commit failed: {e}')
            return HookResult(
                success=False,
                message=f'Git commit failed: {e}',
            )
