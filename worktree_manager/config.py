"""
Global and project configuration management for worktree manager.

Configuration hierarchy (highest to lowest priority):
1. CLI flags
2. Environment variables (WORKTREE_*)
3. Project config: .worktree-manager.json (repo root)
4. Global config: ~/.config/worktree-manager/config.json
5. Built-in defaults
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import CONFIG_DIR_NAME, LEGACY_CONFIG_DIR, PROJECT_CONFIG_FILE

logger = logging.getLogger('worktree_manager')

CONFIG_DIR = Path.home() / '.config' / CONFIG_DIR_NAME
CONFIG_FILE = CONFIG_DIR / 'config.json'

# Legacy config for migration
LEGACY_CONFIG_PATH = Path(os.path.expanduser(LEGACY_CONFIG_DIR))

# Default global configuration
DEFAULT_CONFIG: dict[str, Any] = {
    'version': 1,
    # Branch patterns to ignore when listing or creating worktrees
    'ignore_patterns': [
        'dependabot/*',
        'renovate/*',
        'snyk-*',
    ],
    # Setup wizard completed flag
    'setup_completed': False,
    # Default base branch for new worktrees
    'base_branch': 'develop',
    # Docker settings
    'docker': {
        'compose_command': 'docker compose',
        'stream_output': True,
        'auto_build': True,
        'timeout_seconds': 300,
    },
    # Default port configuration
    'default_ports': {
        'web_base': 58000,
        'db_base': 5432,
        'range_size': 100,
    },
    # Notification settings
    'notifications': {
        'enabled': True,
        'command': 'notify-send',
    },
    # CI mode (non-interactive)
    'ci_mode': False,
}

# Default project configuration
DEFAULT_PROJECT_CONFIG: dict[str, Any] = {
    'version': 1,
    'project_name': None,  # Auto-detect
    'base_branch': 'develop',
    'compose_file': 'docker-compose.local.yml',
    'services': {
        'web': {
            'name': 'web',
            'port_env_var': 'WEB_PORT',
        },
        'db': {
            'name': 'db-postgres',
            'port_env_var': 'DB_PORT',
            'type': 'postgres',
        },
    },
    'database': {
        'type': 'postgres',
        'name': None,  # Auto-detect or require in config
        'user': None,
        'password_env_var': 'POSTGRES_PASSWORD',
    },
    'ports': {
        'web_base': 58000,
        'db_base': 5432,
        'range_size': 100,
    },
    'env_template': '.env.example',
    'env_vars_required': [],
    'ignore_patterns': [],
    'hooks': {
        'post_create': None,
        'pre_close': None,
    },
    'branch_name_pattern': r'^[a-zA-Z0-9][a-zA-Z0-9._/-]*$',
}

# Branch name validation
BRANCH_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._/-]*$')
MAX_BRANCH_LENGTH = 100


@dataclass
class Config:
    """Global configuration for worktree manager."""

    ignore_patterns: list[str] = field(default_factory=list)
    setup_completed: bool = False
    base_branch: str = 'develop'
    docker: dict = field(default_factory=dict)
    version: int = 1

    @classmethod
    def load(cls) -> Config:
        """Load configuration from file or return defaults."""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    data = json.load(f)
                return cls(
                    ignore_patterns=data.get('ignore_patterns', DEFAULT_CONFIG['ignore_patterns']),
                    setup_completed=data.get('setup_completed', False),
                    base_branch=data.get('base_branch', 'develop'),
                    docker=data.get('docker', DEFAULT_CONFIG['docker']),
                    version=data.get('version', 1),
                )
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f'Failed to load config: {e}, using defaults')

        return cls(
            ignore_patterns=DEFAULT_CONFIG['ignore_patterns'].copy(),
            setup_completed=DEFAULT_CONFIG['setup_completed'],
            base_branch=DEFAULT_CONFIG['base_branch'],
            docker=DEFAULT_CONFIG['docker'].copy(),
            version=DEFAULT_CONFIG['version'],
        )

    def save(self) -> None:
        """Save configuration to file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        data = {
            'version': self.version,
            'ignore_patterns': self.ignore_patterns,
            'setup_completed': self.setup_completed,
            'base_branch': self.base_branch,
            'docker': self.docker,
        }

        with open(CONFIG_FILE, 'w') as f:
            json.dump(data, f, indent=2)

        logger.info(f'Saved config to {CONFIG_FILE}')

    def should_ignore_branch(self, branch_name: str) -> bool:
        """
        Check if a branch should be ignored based on patterns.

        Args:
            branch_name: Branch name to check (e.g., 'dependabot/npm/lodash-4.17.21')

        Returns:
            True if branch matches any ignore pattern.
        """
        # Strip refs/remotes/origin/ prefix if present
        if branch_name.startswith('refs/remotes/'):
            branch_name = branch_name.split('/', 3)[-1]
        if branch_name.startswith('origin/'):
            branch_name = branch_name[7:]

        for pattern in self.ignore_patterns:
            if fnmatch.fnmatch(branch_name, pattern):
                return True
        return False

    def add_ignore_pattern(self, pattern: str) -> None:
        """Add a new ignore pattern."""
        if pattern not in self.ignore_patterns:
            self.ignore_patterns.append(pattern)

    def remove_ignore_pattern(self, pattern: str) -> None:
        """Remove an ignore pattern."""
        if pattern in self.ignore_patterns:
            self.ignore_patterns.remove(pattern)

    def get_docker_setting(self, key: str, default=None):
        """Get a Docker-related setting."""
        return self.docker.get(key, default)

    def set_docker_setting(self, key: str, value) -> None:
        """Set a Docker-related setting."""
        self.docker[key] = value


# Singleton instance
_config: Config | None = None


def get_config() -> Config:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def reload_config() -> Config:
    """Reload configuration from disk."""
    global _config
    _config = Config.load()
    return _config


@dataclass
class ProjectConfig:
    """
    Project-specific configuration.

    Loaded from .worktree-manager.json in the repository root.
    Falls back to defaults and auto-detection if not present.
    """

    project_name: str
    base_branch: str = 'develop'
    compose_file: str = 'docker-compose.local.yml'
    services: dict = field(default_factory=dict)
    database: dict = field(default_factory=dict)
    ports: dict = field(default_factory=dict)
    env_template: str = '.env.example'
    env_vars_required: list[str] = field(default_factory=list)
    ignore_patterns: list[str] = field(default_factory=list)
    hooks: dict = field(default_factory=dict)
    branch_name_pattern: str = r'^[a-zA-Z0-9][a-zA-Z0-9._/-]*$'
    version: int = 1

    @classmethod
    def load(cls, repo_path: Path | str | None = None) -> ProjectConfig:
        """
        Load project configuration from .worktree-manager.json.

        Args:
            repo_path: Path to repository root. If None, auto-detect.

        Returns:
            ProjectConfig with values from file or defaults.
        """
        if repo_path is None:
            repo_path = find_repo_root()

        repo_path = Path(repo_path) if repo_path else None
        config_file = repo_path / PROJECT_CONFIG_FILE if repo_path else None

        # Start with defaults
        data = DEFAULT_PROJECT_CONFIG.copy()

        # Load from file if exists
        if config_file and config_file.exists():
            try:
                with open(config_file) as f:
                    file_data = json.load(f)
                # Merge file data over defaults
                data = _deep_merge(data, file_data)
                logger.info(f'Loaded project config from {config_file}')
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f'Failed to load project config: {e}')

        # Auto-detect project name if not set
        if not data.get('project_name'):
            data['project_name'] = detect_project_name(repo_path)

        return cls(
            project_name=data['project_name'],
            base_branch=data.get('base_branch', 'develop'),
            compose_file=data.get('compose_file', 'docker-compose.local.yml'),
            services=data.get('services', DEFAULT_PROJECT_CONFIG['services']),
            database=data.get('database', DEFAULT_PROJECT_CONFIG['database']),
            ports=data.get('ports', DEFAULT_PROJECT_CONFIG['ports']),
            env_template=data.get('env_template', '.env.example'),
            env_vars_required=data.get('env_vars_required', []),
            ignore_patterns=data.get('ignore_patterns', []),
            hooks=data.get('hooks', {}),
            branch_name_pattern=data.get('branch_name_pattern', r'^[a-zA-Z0-9][a-zA-Z0-9._/-]*$'),
            version=data.get('version', 1),
        )

    def save(self, repo_path: Path | str) -> None:
        """Save project configuration to .worktree-manager.json."""
        repo_path = Path(repo_path)
        config_file = repo_path / PROJECT_CONFIG_FILE

        data = {
            'version': self.version,
            'project_name': self.project_name,
            'base_branch': self.base_branch,
            'compose_file': self.compose_file,
            'services': self.services,
            'database': self.database,
            'ports': self.ports,
            'env_template': self.env_template,
            'env_vars_required': self.env_vars_required,
            'ignore_patterns': self.ignore_patterns,
            'hooks': self.hooks,
            'branch_name_pattern': self.branch_name_pattern,
        }

        with open(config_file, 'w') as f:
            json.dump(data, f, indent=2)

        logger.info(f'Saved project config to {config_file}')

    def get_db_service_name(self) -> str:
        """Get the database service name."""
        return self.services.get('db', {}).get('name', 'db-postgres')

    def get_web_service_name(self) -> str:
        """Get the web service name."""
        return self.services.get('web', {}).get('name', 'web')

    def get_db_type(self) -> str:
        """Get the database type."""
        return self.database.get('type', 'postgres')

    def get_db_name(self) -> str | None:
        """Get the database name."""
        return self.database.get('name')

    def get_db_user(self) -> str | None:
        """Get the database user."""
        return self.database.get('user')

    def get_web_port_base(self) -> int:
        """Get the base web port."""
        return self.ports.get('web_base', 58000)

    def get_db_port_base(self) -> int:
        """Get the base database port."""
        return self.ports.get('db_base', 5432)


# Singleton for project config
_project_config: ProjectConfig | None = None


def get_project_config(repo_path: Path | str | None = None) -> ProjectConfig:
    """Get the project configuration instance."""
    global _project_config
    if _project_config is None or repo_path is not None:
        _project_config = ProjectConfig.load(repo_path)
    return _project_config


def reload_project_config(repo_path: Path | str | None = None) -> ProjectConfig:
    """Reload project configuration from disk."""
    global _project_config
    _project_config = ProjectConfig.load(repo_path)
    return _project_config


def find_repo_root(start_path: Path | str | None = None) -> Path | None:
    """
    Find the git repository root.

    Args:
        start_path: Starting path for search. Defaults to current directory.

    Returns:
        Path to repository root, or None if not in a git repo.
    """
    if start_path is None:
        start_path = Path.cwd()
    else:
        start_path = Path(start_path)

    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            capture_output=True,
            text=True,
            cwd=start_path,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    return None


def detect_project_name(repo_path: Path | str | None = None) -> str:
    """
    Auto-detect the project name.

    Detection order:
    1. .worktree-manager.json project_name field
    2. Git remote origin URL (extract repo name)
    3. Directory name

    Args:
        repo_path: Path to repository. If None, uses current directory.

    Returns:
        Detected project name.
    """
    if repo_path is None:
        repo_path = find_repo_root() or Path.cwd()
    else:
        repo_path = Path(repo_path)

    # Try git remote origin
    try:
        result = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            capture_output=True,
            text=True,
            cwd=repo_path,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            # Extract repo name from URL
            # Handles: git@github.com:user/repo.git, https://github.com/user/repo.git
            name = url.rstrip('/').rstrip('.git').split('/')[-1]
            if name:
                return name.lower()
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    # Fall back to directory name
    return repo_path.name.lower()


def validate_branch_name(name: str, pattern: str | None = None) -> None:
    """
    Validate branch name to prevent shell injection and ensure valid format.

    Args:
        name: Branch name to validate.
        pattern: Optional custom regex pattern. Uses default if not provided.

    Raises:
        ValueError: If branch name is invalid.
    """
    if not name:
        raise ValueError('Branch name cannot be empty')

    if len(name) > MAX_BRANCH_LENGTH:
        raise ValueError(f'Branch name too long (max {MAX_BRANCH_LENGTH} characters)')

    # Use custom pattern or default
    if pattern:
        compiled_pattern = re.compile(pattern)
    else:
        compiled_pattern = BRANCH_NAME_PATTERN

    if not compiled_pattern.match(name):
        raise ValueError(
            f'Invalid branch name: {name!r}. '
            'Must start with alphanumeric character and contain only '
            'alphanumerics, dots, underscores, hyphens, and slashes.'
        )

    # Additional security checks
    dangerous_patterns = ['..', '~', '^', ':', '\\', ' ', '\t', '\n']
    for dangerous in dangerous_patterns:
        if dangerous in name:
            raise ValueError(f'Branch name contains invalid character sequence: {dangerous!r}')


def validate_project_name(name: str) -> None:
    """
    Validate project name for use in Docker Compose.

    Args:
        name: Project name to validate.

    Raises:
        ValueError: If project name is invalid.
    """
    if not name:
        raise ValueError('Project name cannot be empty')

    if len(name) > 64:
        raise ValueError('Project name too long (max 64 characters)')

    # Docker Compose project names must be lowercase alphanumeric with hyphens
    pattern = re.compile(r'^[a-z0-9][a-z0-9-]*$')
    if not pattern.match(name):
        raise ValueError(f'Invalid project name: {name!r}. Must be lowercase alphanumeric with hyphens only.')


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Deep merge two dictionaries.

    Args:
        base: Base dictionary.
        override: Dictionary with values to override.

    Returns:
        Merged dictionary.
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def migrate_legacy_config() -> bool:
    """
    Migrate configuration from legacy dispatch-guru location.

    Returns:
        True if migration was performed, False if not needed.
    """
    if not LEGACY_CONFIG_PATH.exists():
        return False

    if CONFIG_DIR.exists():
        logger.info('New config directory already exists, skipping migration')
        return False

    logger.info(f'Migrating config from {LEGACY_CONFIG_PATH} to {CONFIG_DIR}')

    # Create new config directory
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Copy files
    files_to_migrate = [
        ('worktrees.json', 'registry.json'),  # Renamed
        ('config.json', 'config.json'),
        ('tasks.db', 'tasks.db'),
    ]

    for old_name, new_name in files_to_migrate:
        old_path = LEGACY_CONFIG_PATH / old_name
        new_path = CONFIG_DIR / new_name
        if old_path.exists():
            import shutil

            shutil.copy2(old_path, new_path)
            logger.info(f'Migrated {old_name} -> {new_name}')

    return True
