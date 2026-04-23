"""
Hook configuration management.

Configuration is stored in ~/.config/dispatch-guru/hooks.json
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger('worktree_manager.hooks')

CONFIG_DIR = Path.home() / '.config' / 'dispatch-guru'
CONFIG_FILE = CONFIG_DIR / 'hooks.json'

# Default configuration
DEFAULT_CONFIG: dict[str, Any] = {
    'version': 1,
    'hooks': {
        'docker_start': {
            'enabled': True,
            'auto_build': True,
        },
        'docker_stop': {
            'enabled': True,
            'remove_volumes': False,
        },
        'git_commit': {
            'enabled': True,
            'default_message': 'Complete {feature_name}',
        },
        'console_notify': {
            'enabled': True,
        },
        'system_notify': {
            'enabled': True,
        },
    },
}


@dataclass
class HookConfig:
    """Configuration for hooks."""

    hooks: dict[str, dict] = field(default_factory=dict)
    version: int = 1

    @classmethod
    def load(cls) -> HookConfig:
        """Load configuration from file or return defaults."""
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
