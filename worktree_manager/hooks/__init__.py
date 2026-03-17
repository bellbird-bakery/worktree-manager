"""
Status transition hooks for worktree task tracking.

This module provides a hook system that triggers actions when tasks
move between statuses (TODO, IN_PROGRESS, DONE).

Supported hooks:
- Git operations (commit on completion)
- Notifications (console + system)
- Docker lifecycle (start/stop containers)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from worktree_manager.task_store import Task, TaskStatus

logger = logging.getLogger('worktree_manager.hooks')


@dataclass
class TransitionContext:
    """Context for a task status transition."""

    task: Task
    old_status: TaskStatus
    new_status: TaskStatus
    source: str  # 'webapp', 'cli', 'sync'
    interactive: bool = False  # Can prompt user?
    worktree_path: Path | None = None
    project_name: str | None = None
    # User confirmation data (set after confirmation modal)
    user_confirmed: bool = False
    user_data: dict = field(default_factory=dict)

    @property
    def transition_key(self) -> str:
        """Return a key like 'todo_to_in_progress'."""
        return f'{self.old_status}_to_{self.new_status}'


@dataclass
class WorktreeLifecycleContext:
    """Context for worktree lifecycle events (create, close)."""

    event: str  # 'create', 'close'
    worktree_path: Path
    feature_name: str
    main_repo_path: Path
    branch_name: str
    interactive: bool = True


@dataclass
class HookResult:
    """Result of a hook execution."""

    success: bool
    message: str
    hook_name: str = ''
    action_taken: str | None = None  # 'committed', 'containers_started', etc.
    requires_confirmation: bool = False
    confirmation_type: str | None = None  # 'git_commit', etc.
    confirmation_data: dict = field(default_factory=dict)


class Hook(Protocol):
    """Protocol for hook implementations."""

    name: str
    description: str

    def should_run(self, ctx: TransitionContext) -> bool:
        """Check if this hook should run for the given transition."""
        ...

    def pre_check(self, ctx: TransitionContext) -> HookResult | None:
        """
        Pre-flight check before status change.

        Return HookResult with requires_confirmation=True if user
        confirmation is needed (e.g., git commit prompt).
        Return None if no pre-check needed.
        """
        ...

    def execute(self, ctx: TransitionContext) -> HookResult:
        """Execute the hook action."""
        ...


class LifecycleHook(Protocol):
    """Protocol for worktree lifecycle hook implementations."""

    name: str
    description: str

    def should_run(self, ctx: WorktreeLifecycleContext) -> bool:
        """Check if this hook should run for the given lifecycle event."""
        ...

    def execute(self, ctx: WorktreeLifecycleContext) -> HookResult:
        """Execute the hook action."""
        ...


class HookManager:
    """Manages hook registration and execution."""

    def __init__(self) -> None:
        self._hooks: list[Hook] = []
        self._config: HookConfig | None = None

    @property
    def config(self) -> HookConfig:
        """Lazy-load configuration."""
        if self._config is None:
            from worktree_manager.hooks.config import HookConfig

            self._config = HookConfig.load()
        return self._config

    def register(self, hook: Hook) -> None:
        """Register a hook."""
        self._hooks.append(hook)
        logger.debug(f'Registered hook: {hook.name}')

    def get_hooks_for_transition(self, ctx: TransitionContext) -> list[Hook]:
        """Get hooks that should run for a transition."""
        applicable = []
        for hook in self._hooks:
            if not self.config.is_enabled(hook.name):
                continue
            if hook.should_run(ctx):
                applicable.append(hook)
        return applicable

    def pre_check(self, ctx: TransitionContext) -> HookResult | None:
        """
        Check if any hooks need user confirmation before proceeding.

        Returns the first HookResult that requires confirmation, or None.
        """
        for hook in self.get_hooks_for_transition(ctx):
            result = hook.pre_check(ctx)
            if result and result.requires_confirmation:
                return result
        return None

    def execute(self, ctx: TransitionContext) -> list[HookResult]:
        """Execute all applicable hooks for the transition."""
        results = []

        for hook in self.get_hooks_for_transition(ctx):
            try:
                result = hook.execute(ctx)
                result.hook_name = hook.name
                results.append(result)

                if result.success:
                    logger.info(f'Hook {hook.name} succeeded: {result.message}')
                else:
                    logger.warning(f'Hook {hook.name} failed: {result.message}')

            except Exception as e:
                logger.exception(f'Hook {hook.name} raised exception')
                results.append(
                    HookResult(
                        success=False,
                        message=str(e),
                        hook_name=hook.name,
                    )
                )

        return results


class LifecycleHookManager:
    """Manages lifecycle hook registration and execution for worktree events."""

    def __init__(self) -> None:
        self._hooks: list[LifecycleHook] = []
        self._config: HookConfig | None = None

    @property
    def config(self) -> HookConfig:
        """Lazy-load configuration."""
        if self._config is None:
            from worktree_manager.hooks.config import HookConfig

            self._config = HookConfig.load()
        return self._config

    def register(self, hook: LifecycleHook) -> None:
        """Register a lifecycle hook."""
        self._hooks.append(hook)
        logger.debug(f'Registered lifecycle hook: {hook.name}')

    def get_hooks_for_event(self, ctx: WorktreeLifecycleContext) -> list[LifecycleHook]:
        """Get hooks that should run for a lifecycle event."""
        applicable = []
        for hook in self._hooks:
            if not self.config.is_enabled(hook.name):
                continue
            if hook.should_run(ctx):
                applicable.append(hook)
        return applicable

    def execute(self, ctx: WorktreeLifecycleContext) -> list[HookResult]:
        """Execute all applicable hooks for the lifecycle event."""
        results = []

        for hook in self.get_hooks_for_event(ctx):
            try:
                result = hook.execute(ctx)
                result.hook_name = hook.name
                results.append(result)

                if result.success:
                    logger.info(f'Lifecycle hook {hook.name} succeeded: {result.message}')
                else:
                    logger.warning(f'Lifecycle hook {hook.name} failed: {result.message}')

            except Exception as e:
                logger.exception(f'Lifecycle hook {hook.name} raised exception')
                results.append(
                    HookResult(
                        success=False,
                        message=str(e),
                        hook_name=hook.name,
                    )
                )

        return results


# Global hook manager instance
_hook_manager: HookManager | None = None


def get_hook_manager() -> HookManager:
    """Get the global hook manager, initializing hooks if needed."""
    global _hook_manager

    if _hook_manager is None:
        _hook_manager = HookManager()
        _register_default_hooks(_hook_manager)

    return _hook_manager


def _register_default_hooks(manager: HookManager) -> None:
    """Register all default hooks."""
    from worktree_manager.hooks.docker import DockerStartHook, DockerStopHook
    from worktree_manager.hooks.git import GitCommitHook
    from worktree_manager.hooks.notify import ConsoleNotifyHook, SystemNotifyHook

    # Order matters - notifications run last
    manager.register(DockerStartHook())
    manager.register(DockerStopHook())
    manager.register(GitCommitHook())
    manager.register(ConsoleNotifyHook())
    manager.register(SystemNotifyHook())


# Global lifecycle hook manager instance
_lifecycle_hook_manager: LifecycleHookManager | None = None


def get_lifecycle_hook_manager() -> LifecycleHookManager:
    """Get the global lifecycle hook manager, initializing hooks if needed."""
    global _lifecycle_hook_manager

    if _lifecycle_hook_manager is None:
        _lifecycle_hook_manager = LifecycleHookManager()
        _register_lifecycle_hooks(_lifecycle_hook_manager)

    return _lifecycle_hook_manager


def _register_lifecycle_hooks(manager: LifecycleHookManager) -> None:
    """Register all lifecycle hooks."""
    from worktree_manager.hooks.serena import SerenaSetupHook

    manager.register(SerenaSetupHook())


def build_context(
    task: Task,
    old_status: TaskStatus,
    new_status: TaskStatus,
    source: str = 'webapp',
    interactive: bool = False,
) -> TransitionContext:
    """
    Build a TransitionContext with worktree info looked up from registry.
    """
    from worktree_manager.registry import read_registry

    worktree_path = None
    project_name = None

    try:
        registry = read_registry()
        if registry:
            entry = registry.find_by_feature(task.feature_name)
            if entry:
                worktree_path = Path(entry.path)
                project_name = entry.compose_project_name
    except Exception as e:
        logger.warning(f'Failed to lookup worktree for {task.feature_name}: {e}')

    return TransitionContext(
        task=task,
        old_status=old_status,
        new_status=new_status,
        source=source,
        interactive=interactive,
        worktree_path=worktree_path,
        project_name=project_name,
    )


# Re-export for convenience - noqa required as this is intentionally at bottom for re-export
from .config import HookConfig  # noqa: E402

__all__ = [
    'TransitionContext',
    'WorktreeLifecycleContext',
    'HookResult',
    'Hook',
    'LifecycleHook',
    'HookManager',
    'LifecycleHookManager',
    'HookConfig',
    'get_hook_manager',
    'get_lifecycle_hook_manager',
    'build_context',
]
