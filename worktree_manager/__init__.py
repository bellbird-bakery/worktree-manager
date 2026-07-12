"""
Git Worktree Manager

A bulletproof git worktree management system with automatic port assignment
and a persistent registry.

This tool manages isolated Docker Compose environments for each git worktree,
with automatic port allocation and database cloning capabilities.
"""

# Base ports for worktrees (can be overridden in project config)
BASE_WEB_PORT = 58000
BASE_DB_PORT = 5432
BASE_REDIS_PORT = 6379

# Environment variable naming the shared dev Postgres server's published port.
# Its VALUE lives only in the project's .env / .env.example and must be identical
# across every worktree (one shared server). worktree-manager never allocates it
# per-index. See HANDOVER-dispatch-guru-shared-db.md.
SHARED_DB_PORT_ENV = 'SHARED_DB_PORT'

# Default project name - None means auto-detect from git/directory
DEFAULT_PROJECT_NAME = None

# Config directory name (under ~/.config/)
CONFIG_DIR_NAME = 'worktree-manager'

# Registry file location
REGISTRY_FILE = f'~/.config/{CONFIG_DIR_NAME}/registry.json'
CONFIG_DIR = f'~/.config/{CONFIG_DIR_NAME}'

# Project config filename (at repo root)
PROJECT_CONFIG_FILE = '.worktree-manager.json'

# Legacy config directory (for migration)
LEGACY_CONFIG_DIR = '~/.config/dispatch-guru'
