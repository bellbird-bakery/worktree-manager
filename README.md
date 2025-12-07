# Worktree Manager

A Git worktree manager with Docker Compose isolation and Kanban task tracking.

## Overview

Worktree Manager helps you work on multiple features simultaneously by:

- **Git Worktrees**: Create isolated working directories for each feature branch
- **Docker Isolation**: Automatic port allocation prevents conflicts between worktrees
- **Task Tracking**: Built-in Kanban board to track feature progress
- **Database Cloning**: Clone databases between worktrees for testing
- **Safety Checks**: Prevents closing worktrees with uncommitted/unpushed changes

## Installation

### With uv (recommended)

```bash
# Install globally as a tool
uv tool install worktree-manager

# Or from GitHub
uv tool install git+https://github.com/yourusername/worktree-manager

# Upgrade
uv tool upgrade worktree-manager
```

### With pip

```bash
pip install worktree-manager
```

### From source

```bash
git clone https://github.com/yourusername/worktree-manager
cd worktree-manager
uv tool install .
```

## Quick Start

### 1. Initialize your project

```bash
cd your-project
worktree-manager init
```

This creates a `.worktree-manager.json` configuration file.

### 2. Create a new worktree

```bash
worktree-manager create my-feature
# or use the short alias
wt create my-feature
```

This will:
1. Create a git worktree at `../worktrees/my-feature`
2. Create the feature branch `feature/my-feature`
3. Allocate unique ports (web, database)
4. Create a `.env` file with the allocated ports
5. Add a task to the Kanban board

### 3. Work on your feature

```bash
cd ../worktrees/my-feature
docker compose up -d
# ... make changes ...
```

### 4. Track progress with the Kanban board

```bash
worktree-manager web
# Open http://localhost:8000
```

### 5. Close the worktree when done

```bash
worktree-manager close
```

This will:
1. Check for uncommitted changes and unpushed commits
2. Verify the branch is merged to develop
3. Stop Docker containers
4. Remove the worktree directory
5. Mark the task as done

## Commands

| Command | Alias | Description |
|---------|-------|-------------|
| `worktree-manager create <name>` | `wt create` | Create a new worktree |
| `worktree-manager list` | `wt list` | List all worktrees |
| `worktree-manager status` | `wt status` | Show current worktree status |
| `worktree-manager close` | `wt close` | Close current worktree |
| `worktree-manager clone-db <index>` | | Clone database from another worktree |
| `worktree-manager cleanup-orphans` | | Clean up orphaned Docker resources |
| `worktree-manager web [--port PORT]` | | Start the Kanban web interface |
| `worktree-manager sync` | | Sync tasks between JSON and SQLite |
| `worktree-manager setup` | | Run interactive setup wizard |
| `worktree-manager config` | | Show or modify configuration |
| `worktree-manager init` | | Initialize project configuration |

### Global Options

| Option | Description |
|--------|-------------|
| `-y, --yes` | Skip all confirmation prompts |
| `-q, --quiet` | Minimal output |
| `--non-interactive` | Fail instead of prompting (for CI/CD) |

## Web Interface

The Kanban web interface provides:

- **Board View**: Drag-and-drop tasks between Todo, In Progress, and Done columns
- **Worktrees View**: See all worktrees with their status, ports, and actions
- **Close Worktree**: Close worktrees with safety checks (uncommitted changes, unpushed commits, unmerged branches)
- **Clone Database**: Clone databases between worktrees
- **Sync Status**: View and resolve sync conflicts between local and git-tracked tasks

Start the web interface:

```bash
worktree-manager web --port 8000
```

## Port Allocation

Worktrees are assigned ports based on their index to prevent conflicts:

| Index | Web Port | DB Port |
|-------|----------|---------|
| 0 (main) | 8000 | 5432 |
| 1 | 8010 | 5442 |
| 2 | 8020 | 5452 |
| 3 | 8030 | 5462 |
| N | 8000 + N×10 | 5432 + N×10 |

Ports are stored in the `.env` file as `WEB_PORT` and `DB_PORT`.

## Task Synchronization

Tasks are stored in two locations for flexibility:

- **SQLite** (`~/.config/dispatch-guru/tasks.db`): Fast local queries, used by the web UI
- **JSON** (`.worktree-tasks.json`): Version-controlled, shareable across machines via git

Use `worktree-manager sync` to synchronize between them:

```bash
# Full bidirectional sync
worktree-manager sync

# Pull from JSON to SQLite only
worktree-manager sync --pull

# Push from SQLite to JSON only
worktree-manager sync --push

# Auto-commit changes to git
worktree-manager sync --commit
```

## Configuration

### Project Configuration (`.worktree-manager.json`)

Created per-project with `worktree-manager init`:

```json
{
  "version": 1,
  "project_name": "my-project",
  "base_branch": "develop",
  "compose_file": "docker-compose.local.yml",
  "services": {
    "web": {"name": "web"},
    "db": {"name": "db-postgres", "type": "postgres"}
  },
  "database": {
    "type": "postgres",
    "name": "mydb",
    "user": "myuser"
  },
  "worktree_dir": "../worktrees"
}
```

### Global Configuration (`~/.config/dispatch-guru/config.json`)

User-specific settings:

```json
{
  "base_branch": "develop",
  "setup_completed": true,
  "ignore_patterns": [],
  "docker_settings": {
    "stream_output": true,
    "auto_build": true
  }
}
```

### Registry (`~/.config/dispatch-guru/registry.json`)

Tracks all worktrees across projects:

```json
{
  "main_repo_path": "/path/to/project",
  "worktrees": [
    {
      "path": "/path/to/worktrees/my-feature",
      "feature_name": "my-feature",
      "index": 1,
      "ports": {"web": 8010, "db": 5442},
      "compose_project_name": "my-feature"
    }
  ]
}
```

## Safety Features

### Close Worktree Checks

Before closing a worktree, the tool checks:

1. **Uncommitted changes**: Files modified but not committed
2. **Unpushed commits**: Commits not pushed to remote
3. **Unmerged branch**: Branch not merged to the base branch (usually `develop`)

If any check fails:
- **CLI**: Prompts for confirmation or use `--force`
- **Web UI**: Shows warning and requires "Force Close" button

### Database Cloning

Clone databases safely between worktrees:

```bash
# From CLI (in target worktree)
worktree-manager clone-db 0  # Clone from main repo (index 0)
worktree-manager clone-db 1  # Clone from worktree index 1

# From Web UI
# Go to Worktrees > Clone DB button
```

## CI/CD Integration

For automated environments:

```bash
# Skip all prompts
worktree-manager --yes create my-feature

# Quiet mode (minimal output)
worktree-manager --quiet --yes close

# Non-interactive mode (fails instead of prompting)
worktree-manager --non-interactive sync

# Combine options
worktree-manager -yq create ci-test
```

## Hooks

Worktree Manager supports lifecycle hooks for custom automation:

- **Git hooks**: Auto-commit on status change
- **Docker hooks**: Auto-start/stop containers
- **Notification hooks**: Desktop notifications

Configure hooks in `~/.config/dispatch-guru/hooks.json`.

## Troubleshooting

### Port already in use

```bash
# Check which worktree is using a port
worktree-manager list

# Clean up orphaned resources
worktree-manager cleanup-orphans
```

### Worktree not in registry

```bash
# Re-sync the registry
worktree-manager sync-tasks
```

### Permission issues (Docker)

The tool automatically fixes permissions when closing worktrees, but if needed:

```bash
# Fix permissions manually
docker compose run --rm web chown -R $(id -u):$(id -g) .
```

## Requirements

- Python 3.11+
- Git 2.20+
- Docker and Docker Compose
- Linux or macOS

## Development

```bash
# Clone the repo
git clone https://github.com/yourusername/worktree-manager
cd worktree-manager

# Install dependencies
uv sync

# Run locally
uv run worktree-manager --help

# Run tests
uv run pytest

# Format and lint
uv run ruff format .
uv run ruff check . --fix
```

## License

MIT License - see [LICENSE](LICENSE) for details.
