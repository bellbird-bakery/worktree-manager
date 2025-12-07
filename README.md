# Worktree Manager

A Git worktree manager with Docker Compose isolation and Kanban task tracking.

## Overview

Worktree Manager helps you work on multiple features simultaneously by:

- **Git Worktrees**: Create isolated working directories for each feature branch
- **Docker Isolation**: Automatic port allocation prevents conflicts between worktrees
- **Task Tracking**: Built-in Kanban board to track feature progress
- **Database Cloning**: Clone databases between worktrees for testing

## Installation

```bash
# Install with pip
pip install worktree-manager

# Or with uv
uv tool install worktree-manager
```

## Quick Start

### Initialize a project

```bash
cd your-project
worktree-manager init
```

This creates a `.worktree-manager.json` configuration file.

### Create a new worktree

```bash
worktree-manager create my-feature
```

This will:
1. Create a git worktree at `../worktrees/my-feature`
2. Allocate unique ports (web, database)
3. Create a `.env` file with the ports
4. Add a task to the Kanban board

### List worktrees

```bash
worktree-manager list
```

### Start the Kanban web interface

```bash
worktree-manager web
```

Open http://localhost:8000 to view and manage tasks.

### Close a worktree

```bash
cd ../worktrees/my-feature
worktree-manager close -m "Feature complete"
```

This will:
1. Commit any uncommitted changes
2. Stop Docker containers
3. Remove the worktree
4. Mark the task as done

## Configuration

### Project Configuration (`.worktree-manager.json`)

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

### Global Configuration

User-specific settings are stored in `~/.config/worktree-manager/config.json`.

## Commands

| Command | Description |
|---------|-------------|
| `init` | Initialize project configuration |
| `create <name>` | Create a new worktree |
| `list` | List all worktrees |
| `status` | Show current worktree status |
| `close` | Close current worktree |
| `clone-db <index>` | Clone database from another worktree |
| `cleanup-orphans` | Clean up orphaned Docker resources |
| `web` | Start the Kanban web interface |
| `sync` | Sync tasks between JSON and SQLite |
| `sync-tasks` | Sync tasks with worktree registry |
| `setup` | Run interactive setup wizard |
| `config` | Show or modify configuration |

## Port Allocation

Worktrees are assigned ports based on their index:

| Index | Web Port | DB Port |
|-------|----------|---------|
| 0 | 8000 | 5432 |
| 1 | 8010 | 5442 |
| 2 | 8020 | 5452 |
| ... | ... | ... |

## Task Synchronization

Tasks are stored in two locations:
- **SQLite** (`~/.config/worktree-manager/tasks.db`): Fast local queries
- **JSON** (`.worktree-tasks.json`): Version-controlled, shareable

Use `worktree-manager sync` to synchronize between them.

## CI/CD Integration

For non-interactive environments:

```bash
# Skip all prompts
worktree-manager --yes create my-feature

# Quiet mode (minimal output)
worktree-manager --quiet --yes close -m "Done"

# Non-interactive mode
worktree-manager --non-interactive sync
```

## Requirements

- Python 3.11+
- Git
- Docker and Docker Compose
- Linux or macOS

## License

MIT
