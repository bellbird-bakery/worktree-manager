"""
Git Worktree Manager

A bulletproof git worktree management system with automatic port assignment,
persistent registry, and web interface.

This tool manages isolated Docker Compose environments for each git worktree,
with automatic port allocation and database cloning capabilities.
"""

# Base ports for worktrees (can be overridden in project config)
BASE_WEB_PORT = 58000
BASE_DB_PORT = 5432

# Default project name - None means auto-detect from git/directory
DEFAULT_PROJECT_NAME = None

# Config directory name (under ~/.config/)
CONFIG_DIR_NAME = 'worktree-manager'

# Registry file location
REGISTRY_FILE = f'~/.config/{CONFIG_DIR_NAME}/registry.json'
CONFIG_DIR = f'~/.config/{CONFIG_DIR_NAME}'

# Task database location
TASK_DB_PATH = f'~/.config/{CONFIG_DIR_NAME}/tasks.db'

# Project config filename (at repo root)
PROJECT_CONFIG_FILE = '.worktree-manager.json'

# Legacy config directory (for migration)
LEGACY_CONFIG_DIR = '~/.config/dispatch-guru'
