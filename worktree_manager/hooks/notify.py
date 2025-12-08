"""
Notification hooks for task status transitions.

Provides console output and system notifications (notify-send).
"""

from __future__ import annotations

import logging
import subprocess

from rich.console import Console

from ..task_store import TaskStatus
from . import HookResult, TransitionContext

logger = logging.getLogger('worktree_manager.hooks')
console = Console()


# Status display names and colors
STATUS_DISPLAY = {
    TaskStatus.TODO: ('To Do', 'yellow'),
    TaskStatus.IN_PROGRESS: ('In Progress', 'blue'),
    TaskStatus.DONE: ('Done', 'green'),
}


class ConsoleNotifyHook:
    """Print status change notifications to console."""

    name = 'console_notify'
    description = 'Print status change to console'

    def should_run(self, ctx: TransitionContext) -> bool:
        """Always run for any transition."""
        return True

    def pre_check(self, ctx: TransitionContext) -> HookResult | None:
        """No pre-check needed."""
        return None

    def execute(self, ctx: TransitionContext) -> HookResult:
        """Print the status change with rich formatting."""
        old_name, _ = STATUS_DISPLAY.get(ctx.old_status, (ctx.old_status, 'white'))
        new_name, new_color = STATUS_DISPLAY.get(ctx.new_status, (ctx.new_status, 'white'))

        console.print(f'[bold]{ctx.task.title}[/bold]: {old_name} -> [{new_color}]{new_name}[/{new_color}]')

        return HookResult(
            success=True,
            message=f'Printed notification for {ctx.task.feature_name}',
        )


class SystemNotifyHook:
    """Send system notification via notify-send."""

    name = 'system_notify'
    description = 'Send desktop notification'

    def should_run(self, ctx: TransitionContext) -> bool:
        """Only run in interactive contexts."""
        return ctx.interactive

    def pre_check(self, ctx: TransitionContext) -> HookResult | None:
        """No pre-check needed."""
        return None

    def execute(self, ctx: TransitionContext) -> HookResult:
        """Send notification via notify-send."""
        old_name, _ = STATUS_DISPLAY.get(ctx.old_status, (ctx.old_status, 'white'))
        new_name, _ = STATUS_DISPLAY.get(ctx.new_status, (ctx.new_status, 'white'))

        title = f'Task: {ctx.task.title}'
        body = f'{old_name} -> {new_name}'

        try:
            subprocess.run(
                ['notify-send', title, body],
                check=True,
                capture_output=True,
            )
            return HookResult(
                success=True,
                message='Sent system notification',
            )
        except FileNotFoundError:
            logger.debug('notify-send not available')
            return HookResult(
                success=True,  # Not a failure, just not available
                message='notify-send not available',
            )
        except subprocess.CalledProcessError as e:
            logger.warning(f'notify-send failed: {e}')
            return HookResult(
                success=False,
                message=f'notify-send failed: {e}',
            )
