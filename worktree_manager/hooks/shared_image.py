"""
Shared dev image hook for worktree lifecycle events.

When a project configures a shared dev image tag (``dev_image`` in
``.worktree-manager.json``), this hook builds that image once at worktree-create
time if it is missing, so every worktree can start its containers without a
redundant per-worktree image build. It is a no-op unless ``dev_image`` is set.
"""

from __future__ import annotations

import logging
import subprocess

from ..config import Config, ProjectConfig
from ..docker_ops import is_docker_running
from . import HookResult, WorktreeLifecycleContext

logger = logging.getLogger('worktree_manager.hooks')


class SharedImageHook:
    """Build the shared dev Docker image on worktree create if it is missing."""

    name = 'shared_image'
    description = 'Build the shared dev Docker image if it is missing'

    def should_run(self, ctx: WorktreeLifecycleContext) -> bool:
        """Run on create when a dev_image is configured, auto_build is on, and it's missing."""
        if ctx.event != 'create':
            return False

        project = ProjectConfig.load(repo_path=ctx.main_repo_path)
        if not project.dev_image:
            return False

        if not Config.load().get_docker_setting('auto_build', True):
            return False

        if not is_docker_running():
            return False

        return not self._image_exists(project.dev_image)

    def execute(self, ctx: WorktreeLifecycleContext) -> HookResult:
        """Build the shared image from the main repo, streaming output to the terminal."""
        project = ProjectConfig.load(repo_path=ctx.main_repo_path)
        image = project.dev_image

        print(f'Building shared dev image {image} (first time; subsequent worktrees will reuse it)...')

        cmd = ['docker', 'compose', '-f', project.compose_file, 'build']
        try:
            # No capture: stream docker build output so a multi-minute build never looks hung.
            subprocess.run(cmd, cwd=str(ctx.main_repo_path), check=True)
        except FileNotFoundError:
            return HookResult(success=False, message='docker not found on PATH')
        except subprocess.CalledProcessError as e:
            logger.warning(f'shared image build failed: exit {e.returncode}')
            return HookResult(success=False, message=f'Shared image build failed (exit {e.returncode})')

        return HookResult(
            success=True,
            message=f'Built shared dev image {image}',
            action_taken='shared_image_built',
        )

    def _image_exists(self, image: str) -> bool:
        """Return True if the given Docker image tag is present locally."""
        try:
            result = subprocess.run(
                ['docker', 'image', 'inspect', image],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        except FileNotFoundError:
            return False
