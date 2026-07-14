"""
Lifecycle hooks for worktree events.

This module provides a hook system that triggers actions when worktrees
are created or closed.

Supported hooks:
- uv sync (install dependencies)
- Claude Code launch
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger('worktree_manager.hooks')


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


# Global lifecycle hook manager instance
_lifecycle_hook_manager: LifecycleHookManager | None = None


def get_lifecycle_hook_manager() -> LifecycleHookManager:
    """Get the global lifecycle hook manager, initializing hooks if needed."""
    global _lifecycle_hook_manager

    if _lifecycle_hook_manager is None:
        _lifecycle_hook_manager = LifecycleHookManager()
        _register_default_lifecycle_hooks(_lifecycle_hook_manager)

    return _lifecycle_hook_manager


def _register_default_lifecycle_hooks(manager: LifecycleHookManager) -> None:
    """Register all default lifecycle hooks."""
    from worktree_manager.hooks.claude import ClaudeLaunchHook
    from worktree_manager.hooks.npm import NpmInstallHook
    from worktree_manager.hooks.shared_image import SharedImageHook
    from worktree_manager.hooks.uv import UvSyncHook

    # uv sync runs first so dependencies are ready before a Claude session launches.
    manager.register(UvSyncHook())
    # npm ci installs Node deps (esbuild etc.) into the fresh, otherwise-empty node_modules.
    manager.register(NpmInstallHook())
    # Build the shared dev image (if configured/missing) before offering a Claude session.
    manager.register(SharedImageHook())
    manager.register(ClaudeLaunchHook())


# Re-export for convenience - noqa required as this is intentionally at bottom for re-export
from .config import HookConfig  # noqa: E402

__all__ = [
    'WorktreeLifecycleContext',
    'HookResult',
    'LifecycleHook',
    'LifecycleHookManager',
    'HookConfig',
    'get_lifecycle_hook_manager',
]
