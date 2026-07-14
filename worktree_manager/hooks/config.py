"""
Hook configuration management.

Configuration is stored in ~/.config/worktree-manager/hooks.json (migrated from the
legacy ~/.config/dispatch-guru/hooks.json on first load).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import CONFIG_DIR_NAME, LEGACY_CONFIG_DIR

logger = logging.getLogger('worktree_manager.hooks')

CONFIG_DIR = Path.home() / '.config' / CONFIG_DIR_NAME
CONFIG_FILE = CONFIG_DIR / 'hooks.json'
LEGACY_CONFIG_FILE = Path(os.path.expanduser(LEGACY_CONFIG_DIR)) / 'hooks.json'

# Default configuration
DEFAULT_CONFIG: dict[str, Any] = {
    'version': 1,
    'hooks': {
        'claude_launch': {
            'enabled': True,
            'auto_launch': False,
        },
        'uv_sync': {
            'enabled': True,
            'extra_args': [],
        },
        'npm_install': {
            'enabled': True,
            'extra_args': [],
        },
        'shared_image': {
            'enabled': True,
        },
    },
}


def _migrate_legacy_hooks_config() -> None:
    """Copy hooks.json from the legacy dispatch-guru config dir if not yet migrated."""
    if CONFIG_FILE.exists() or not LEGACY_CONFIG_FILE.exists():
        return
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LEGACY_CONFIG_FILE, CONFIG_FILE)
        logger.info(f'Migrated hooks config {LEGACY_CONFIG_FILE} -> {CONFIG_FILE}')
    except OSError as e:
        logger.warning(f'Failed to migrate legacy hooks config: {e}')


@dataclass
class HookConfig:
    """Configuration for hooks."""

    hooks: dict[str, dict] = field(default_factory=dict)
    version: int = 1

    @classmethod
    def load(cls) -> HookConfig:
        """Load configuration from file or return defaults."""
        _migrate_legacy_hooks_config()
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    data = json.load(f)
                return cls(
                    hooks=data.get('hooks', DEFAULT_CONFIG['hooks']),
                    version=data.get('version', 1),
                )
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f'Failed to load hook config: {e}, using defaults')

        return cls(
            hooks=DEFAULT_CONFIG['hooks'].copy(),
            version=DEFAULT_CONFIG['version'],
        )

    def save(self) -> None:
        """Save configuration to file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        data = {
            'version': self.version,
            'hooks': self.hooks,
        }

        with open(CONFIG_FILE, 'w') as f:
            json.dump(data, f, indent=2)

        logger.info(f'Saved hook config to {CONFIG_FILE}')

    def is_enabled(self, hook_name: str) -> bool:
        """Check if a hook is enabled."""
        hook_config = self.hooks.get(hook_name, {})
        return hook_config.get('enabled', True)

    def get_hook_setting(self, hook_name: str, setting: str, default=None):
        """Get a specific setting for a hook."""
        hook_config = self.hooks.get(hook_name, {})
        return hook_config.get(setting, default)

    def set_hook_enabled(self, hook_name: str, enabled: bool) -> None:
        """Enable or disable a hook."""
        if hook_name not in self.hooks:
            self.hooks[hook_name] = {}
        self.hooks[hook_name]['enabled'] = enabled

    def set_hook_setting(self, hook_name: str, setting: str, value) -> None:
        """Set a specific setting for a hook."""
        if hook_name not in self.hooks:
            self.hooks[hook_name] = {'enabled': True}
        self.hooks[hook_name][setting] = value
