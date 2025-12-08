"""
Docker lifecycle hooks for task status transitions.

Start containers when starting work, stop when completing.
"""

from __future__ import annotations

import logging

from rich.console import Console

from ..config import get_config
from ..docker_ops import DockerError, compose_down, compose_ps, compose_up
from ..task_store import TaskStatus
from . import HookResult, TransitionContext

logger = logging.getLogger('worktree_manager.hooks')
console = Console()


class DockerStartHook:
    """Start Docker containers when starting work on a task."""

    name = 'docker_start'
    description = 'Start Docker containers for worktree'

    def should_run(self, ctx: TransitionContext) -> bool:
        """Run when moving to IN_PROGRESS from TODO or DONE."""
        if ctx.new_status != TaskStatus.IN_PROGRESS:
            return False
        if ctx.old_status not in (TaskStatus.TODO, TaskStatus.DONE):
            return False
        # Only run if we have worktree info
        return ctx.worktree_path is not None and ctx.project_name is not None

    def pre_check(self, ctx: TransitionContext) -> HookResult | None:
        """No pre-check needed."""
        return None

    def execute(self, ctx: TransitionContext) -> HookResult:
        """Start Docker containers for the worktree."""
        if not ctx.worktree_path or not ctx.project_name:
            return HookResult(
                success=False,
                message='No worktree path or project name available',
            )

        # Check if containers are already running
        try:
            containers = compose_ps(str(ctx.worktree_path), ctx.project_name)
            running = [c for c in containers if c.status == 'running']
            if running:
                return HookResult(
                    success=True,
                    message=f'Containers already running ({len(running)} services)',
                    action_taken='already_running',
                )
        except Exception:
            pass  # Continue to start

        console.print(f'[blue]Starting containers for {ctx.project_name}...[/blue]')

        # Check if streaming output is enabled
        config = get_config()
        stream_output = config.get_docker_setting('stream_output', True) and ctx.interactive

        try:
            if stream_output:
                # Stream output with rich formatting
                def on_output(line: str) -> None:
                    # Dim the output to distinguish from main messages
                    console.print(f'[dim]{line}[/dim]')

                compose_up(
                    str(ctx.worktree_path),
                    ctx.project_name,
                    detach=True,
                    stream_output=True,
                    on_output=on_output,
                )
            else:
                compose_up(str(ctx.worktree_path), ctx.project_name, detach=True)

            return HookResult(
                success=True,
                message=f'Started containers for {ctx.project_name}',
                action_taken='containers_started',
            )
        except DockerError as e:
            logger.error(f'Failed to start containers: {e}')
            return HookResult(
                success=False,
                message=f'Failed to start containers: {e}',
            )


class DockerStopHook:
    """Stop Docker containers when completing a task."""

    name = 'docker_stop'
    description = 'Stop Docker containers for worktree'

    def should_run(self, ctx: TransitionContext) -> bool:
        """Run when moving to DONE from IN_PROGRESS."""
        if ctx.new_status != TaskStatus.DONE:
            return False
        if ctx.old_status != TaskStatus.IN_PROGRESS:
            return False
        # Only run if we have worktree info
        return ctx.worktree_path is not None and ctx.project_name is not None

    def pre_check(self, ctx: TransitionContext) -> HookResult | None:
        """No pre-check needed."""
        return None

    def execute(self, ctx: TransitionContext) -> HookResult:
        """Stop Docker containers for the worktree."""
        if not ctx.worktree_path or not ctx.project_name:
            return HookResult(
                success=False,
                message='No worktree path or project name available',
            )

        # Get config for remove_volumes setting
        from worktree_manager.hooks.config import HookConfig

        config = HookConfig.load()
        remove_volumes = config.get_hook_setting('docker_stop', 'remove_volumes', False)

        console.print(f'[yellow]Stopping containers for {ctx.project_name}...[/yellow]')

        try:
            compose_down(str(ctx.worktree_path), ctx.project_name, volumes=remove_volumes)
            action = 'containers_stopped_with_volumes' if remove_volumes else 'containers_stopped'
            return HookResult(
                success=True,
                message=f'Stopped containers for {ctx.project_name}',
                action_taken=action,
            )
        except DockerError as e:
            logger.error(f'Failed to stop containers: {e}')
            return HookResult(
                success=False,
                message=f'Failed to stop containers: {e}',
            )
