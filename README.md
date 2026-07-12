# Worktree Manager

A Git worktree manager with Docker Compose isolation.

## Overview

Worktree Manager helps you work on multiple features simultaneously by:

- **Git Worktrees**: Create isolated working directories for each feature branch
- **Docker Isolation**: Automatic port allocation prevents conflicts between worktrees
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

#### Branch type options

```bash
# Default: creates feature/my-feature
worktree-manager create my-feature

# Fix branch: creates fix/my-bugfix
worktree-manager create my-bugfix -t fix

# Raw branch name (no prefix): creates branch as-is
worktree-manager create 003-customers-orders-divorce-2 --raw
```

### 3. Work on your feature

```bash
cd ../worktrees/my-feature
docker compose up -d
# ... make changes ...
```

### 4. Close the worktree when done

```bash
worktree-manager close
```

This will:
1. Check for uncommitted changes and unpushed commits
2. Verify the branch is merged to develop
3. Stop Docker containers
4. Remove the worktree directory

## Commands

| Command | Alias | Description |
|---------|-------|-------------|
| `worktree-manager create <name> [-t TYPE] [--raw]` | `wt create` | Create a new worktree |
| `worktree-manager list` | `wt list` | List all worktrees |
| `worktree-manager status` | `wt status` | Show current worktree status |
| `worktree-manager close` | `wt close` | Close current worktree |
| `worktree-manager claude [name] [-c]` | `wt claude` | Launch a Claude Code session in a worktree |
| `worktree-manager clone-db <index>` | | Clone database from another worktree |
| `worktree-manager cleanup-orphans` | | Clean up orphaned Docker resources |
| `worktree-manager setup` | | Run interactive setup wizard |
| `worktree-manager config` | | Show or modify configuration |
| `worktree-manager init` | | Initialize project configuration |
| `worktree-manager shell-init [--shell SHELL]` | `wt shell-init` | Print shell integration for auto-`cd` on create/close |

### Shell Integration (auto-`cd`)

By default `wt create` and `wt close` can't change your shell's working directory
(a subprocess can't `cd` its parent shell). Enable the shell wrapper so that:

- `wt create foo` drops you into the new worktree directory, and
- `wt close` returns you to the main repository directory.

Add one line to your shell's rc file:

```bash
# bash (~/.bashrc) or zsh (~/.zshrc)
eval "$(wt shell-init)"
```

```fish
# fish (~/.config/fish/config.fish)
wt shell-init --shell fish | source
```

Reload your shell (or `source` the rc file) and the `cd` happens automatically.
Without the wrapper, `wt` still works and just prints the directory to `cd` into.

### Claude Code Sessions

`wt claude` opens a [Claude Code](https://code.claude.com) session in a worktree, so each feature environment gets its own isolated session:

```bash
wt claude my-feature          # Launch claude in the my-feature worktree
wt claude                     # Launch in the worktree containing the current directory
wt claude -c my-feature       # Resume the worktree's most recent session
wt claude my-feature -p "run the tests"   # Args after the name are passed through to claude
```

After `wt create`, the `claude_launch` hook prints the matching `wt claude` command. Set `auto_launch: true` for `claude_launch` in `~/.config/worktree-manager/hooks.json` to instead open the session automatically in a new tmux window (requires an active tmux session).

### Global Options

| Option | Description |
|--------|-------------|
| `-y, --yes` | Skip all confirmation prompts |
| `-q, --quiet` | Minimal output |
| `-V, --version` | Show version and exit |
| `--non-interactive` | Fail instead of prompting (for CI/CD) |

## Port Allocation

Worktrees are assigned ports based on their index to prevent conflicts:

| Index | Web Port | DB Port | Redis Port |
|-------|----------|---------|------------|
| 0 (main) | 58000 | 5432 | 6379 |
| 1 | 58001 | 5433 | 6380 |
| 2 | 58002 | 5434 | 6381 |
| 3 | 58003 | 5435 | 6382 |
| N | 58000 + N | 5432 + N | 6379 + N |

Ports are stored in the `.env` file as `WEB_PORT`, `DB_PORT`, and `REDIS_PORT`.

Worktrees created before Redis port allocation can be backfilled with a unique
`REDIS_PORT` via `worktree-manager backfill-ports` (add `--dry-run` to preview).

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
  "dev_image": "my-project-dev:latest",
  "worktree_dir": "../worktrees"
}
```

### Global Configuration (`~/.config/worktree-manager/config.json`)

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

### Registry (`~/.config/worktree-manager/registry.json`)

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

If any check fails, the CLI prompts for confirmation or you can use `--force`.

### Database Cloning

Clone databases safely between worktrees:

```bash
# From CLI (in target worktree)
worktree-manager clone-db 0  # Clone from main repo (index 0)
worktree-manager clone-db 1  # Clone from worktree index 1
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

Worktree Manager supports lifecycle hooks that run when a worktree is created or closed:

- **uv sync hook**: Install dependencies in the new worktree
- **Claude launch hook**: Launch Claude Code in the new worktree
- **Shared dev image hook**: Build a shared dev Docker image once, on create, if it's
  missing — so worktrees reuse a single image instead of rebuilding per worktree.
  Off unless `dev_image` is set in `.worktree-manager.json` (see below); gated by the
  `auto_build` docker setting and skipped when the image already exists.

Configure hooks in `~/.config/worktree-manager/hooks.json`.

## Troubleshooting

### Port already in use

```bash
# Check which worktree is using a port
worktree-manager list

# Clean up orphaned resources
worktree-manager cleanup-orphans
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

### Just Commands

The project includes a `justfile` for common development tasks:

```bash
just              # Show available commands
just version      # Show current version
just bump-patch   # Bump patch version and install (1.1.0 -> 1.1.1)
just bump-minor   # Bump minor version and install (1.1.0 -> 1.2.0)
just bump-major   # Bump major version and install (1.1.0 -> 2.0.0)
just install      # Install without version bump
just lint         # Run linter
just fix          # Run linter with auto-fix
just fmt          # Format code
just test         # Run tests
just check        # Run all checks (lint + test)
just sync         # Sync dependencies
```

## License

MIT License - see [LICENSE](LICENSE) for details.
