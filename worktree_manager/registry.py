"""
Registry management for worktrees.

Handles persistent storage of worktree configurations with file locking
to prevent race conditions.

Uses filelock for cross-platform locking (works on Linux, macOS, and Windows).
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

try:
    from filelock import FileLock
    from filelock import Timeout as FileLockTimeout
except ImportError:
    # Fallback to fcntl for backwards compatibility
    FileLock = None
    FileLockTimeout = None
    import fcntl

if TYPE_CHECKING:
    from collections.abc import Iterator

from . import CONFIG_DIR, REGISTRY_FILE

logger = logging.getLogger('worktree_manager')


@dataclass
class WorktreePorts:
    """Port configuration for a worktree."""

    web: int
    db: int

    def to_dict(self) -> dict:
        return {'web': self.web, 'db': self.db}

    @classmethod
    def from_dict(cls, data: dict) -> WorktreePorts:
        return cls(web=data['web'], db=data['db'])


@dataclass
class WorktreeEntry:
    """A registered worktree entry."""

    path: str
    compose_project_name: str
    ports: WorktreePorts
    feature_name: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_accessed: str = field(default_factory=lambda: datetime.now().isoformat())
    index: int = 0

    def to_dict(self) -> dict:
        return {
            'path': self.path,
            'compose_project_name': self.compose_project_name,
            'ports': self.ports.to_dict(),
            'feature_name': self.feature_name,
            'created_at': self.created_at,
            'last_accessed': self.last_accessed,
            'index': self.index,
        }

    @classmethod
    def from_dict(cls, data: dict) -> WorktreeEntry:
        return cls(
            path=data['path'],
            compose_project_name=data['compose_project_name'],
            ports=WorktreePorts.from_dict(data['ports']),
            feature_name=data['feature_name'],
            created_at=data.get('created_at', datetime.now().isoformat()),
            last_accessed=data.get('last_accessed', datetime.now().isoformat()),
            index=data.get('index', 0),
        )


@dataclass
class Registry:
    """The worktree registry."""

    main_repo_path: str
    worktrees: list[WorktreeEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'main_repo_path': self.main_repo_path,
            'worktrees': [wt.to_dict() for wt in self.worktrees],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Registry:
        worktrees = [WorktreeEntry.from_dict(wt) for wt in data.get('worktrees', [])]
        return cls(
            main_repo_path=data.get('main_repo_path', ''),
            worktrees=worktrees,
        )

    def find_by_path(self, path: str) -> WorktreeEntry | None:
        """Find a worktree by its path."""
        normalized_path = str(Path(path).resolve())
        for wt in self.worktrees:
            if str(Path(wt.path).resolve()) == normalized_path:
                return wt
        return None

    def find_by_index(self, index: int) -> WorktreeEntry | None:
        """Find a worktree by its index."""
        for wt in self.worktrees:
            if wt.index == index:
                return wt
        return None

    def find_by_feature(self, feature_name: str) -> WorktreeEntry | None:
        """Find a worktree by its feature name."""
        for wt in self.worktrees:
            if wt.feature_name == feature_name:
                return wt
        return None

    def get_next_index(self) -> int:
        """Get the next available index."""
        if not self.worktrees:
            return 1
        used_indices = {wt.index for wt in self.worktrees}
        # Find the first available index starting from 1
        index = 1
        while index in used_indices:
            index += 1
        return index

    def add_worktree(self, entry: WorktreeEntry) -> None:
        """Add a worktree to the registry."""
        self.worktrees.append(entry)

    def remove_worktree(self, path: str) -> bool:
        """Remove a worktree from the registry by path."""
        normalized_path = str(Path(path).resolve())
        for i, wt in enumerate(self.worktrees):
            if str(Path(wt.path).resolve()) == normalized_path:
                self.worktrees.pop(i)
                return True
        return False


def get_registry_path() -> Path:
    """Get the path to the registry file."""
    return Path(os.path.expanduser(REGISTRY_FILE))


def get_config_dir() -> Path:
    """Get the config directory path."""
    return Path(os.path.expanduser(CONFIG_DIR))


def ensure_config_dir() -> Path:
    """Ensure the config directory exists."""
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


# Lock timeout in seconds
LOCK_TIMEOUT = 30

# Retry settings for port allocation
PORT_RETRY_ATTEMPTS = 3
PORT_RETRY_DELAY = 0.5


class RegistryLockError(Exception):
    """Failed to acquire registry lock."""

    pass


@contextmanager
def locked_registry(main_repo_path: str | None = None, timeout: int = LOCK_TIMEOUT) -> Iterator[Registry]:
    """
    Context manager for safely reading and modifying the registry.

    Uses file locking to prevent race conditions when multiple processes
    try to modify the registry simultaneously.

    Args:
        main_repo_path: Path to the main repository. Required if registry doesn't exist.
        timeout: Lock timeout in seconds. Default is 30.

    Yields:
        Registry object that can be modified. Changes are saved on exit.

    Raises:
        RegistryLockError: If lock cannot be acquired within timeout.
        ValueError: If main_repo_path is required but not provided.
    """
    ensure_config_dir()
    registry_path = get_registry_path()
    lock_path = registry_path.with_suffix('.lock')

    # Create empty registry if it doesn't exist
    if not registry_path.exists():
        if not main_repo_path:
            raise ValueError('main_repo_path required when creating new registry')
        registry = Registry(main_repo_path=main_repo_path)
        registry_path.write_text(json.dumps(registry.to_dict(), indent=2))

    if FileLock is not None:
        # Use filelock (cross-platform)
        lock = FileLock(lock_path, timeout=timeout)
        try:
            with lock:
                # Read current registry
                with open(registry_path) as f:
                    data = json.load(f)
                registry = Registry.from_dict(data)

                # Update main_repo_path if provided
                if main_repo_path:
                    registry.main_repo_path = main_repo_path

                # Yield for modifications
                yield registry

                # Write back changes atomically
                temp_path = registry_path.with_suffix('.tmp')
                with open(temp_path, 'w') as f:
                    json.dump(registry.to_dict(), f, indent=2)
                temp_path.replace(registry_path)

        except FileLockTimeout:
            raise RegistryLockError(
                f'Could not acquire registry lock within {timeout} seconds. Another process may be holding the lock.'
            ) from None
    else:
        # Fallback to fcntl (Linux/macOS only)
        logger.warning('filelock not installed, using fcntl (Linux/macOS only)')
        with open(registry_path, 'r+') as f:
            # Acquire exclusive lock
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                # Read current registry
                f.seek(0)
                data = json.load(f)
                registry = Registry.from_dict(data)

                # Update main_repo_path if provided
                if main_repo_path:
                    registry.main_repo_path = main_repo_path

                # Yield for modifications
                yield registry

                # Write back changes
                f.seek(0)
                f.truncate()
                json.dump(registry.to_dict(), f, indent=2)
            finally:
                # Lock released automatically on close
                pass


def read_registry() -> Registry | None:
    """
    Read the registry without locking.

    Use this for read-only operations where you don't need to modify the registry.
    """
    registry_path = get_registry_path()
    if not registry_path.exists():
        return None

    with open(registry_path) as f:
        data = json.load(f)
        return Registry.from_dict(data)
