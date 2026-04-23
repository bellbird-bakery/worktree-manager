# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

Worktree Manager is a standalone Python CLI tool for managing git worktrees with Docker Compose isolation and Kanban task tracking. It's designed for developers working on multiple features simultaneously in projects that use Docker for local development.

## Tech Stack

- **Python 3.11+** - Core language
- **Starlette** - Lightweight web framework for the Kanban UI
- **Jinja2** - Template engine (note: NOT Django templates)
- **Rich** - Terminal output formatting
- **uvicorn** - ASGI server for the web interface
- **filelock** - Thread-safe file locking for registry
- **uv** - Package manager (recommended)

## Project Structure

```
worktree-manager/
├── pyproject.toml          # Package configuration and dependencies
├── README.md               # User documentation
├── CLAUDE.md               # This file
├── LICENSE                 # MIT license
└── worktree_manager/       # Main package
    ├── __init__.py
    ├── __main__.py         # Entry point for `python -m worktree_manager`
    ├── cli.py              # Argument parsing (argparse)
    ├── commands.py         # CLI command implementations
    ├── config.py           # Configuration management
    ├── docker_ops.py       # Docker/Compose operations
    ├── git_ops.py          # Git operations (worktree, branch, commit)
    ├── ports.py            # Port allocation logic
    ├── registry.py         # Worktree registry (JSON file storage)
    ├── setup_wizard.py     # Interactive setup
    ├── task_store.py       # Task storage (SQLite)
    ├── validator.py        # Environment validation
    ├── hooks/              # Lifecycle hooks
    │   ├── __init__.py
    │   ├── config.py       # Hook configuration
    │   ├── docker.py       # Docker-related hooks
    │   ├── git.py          # Git-related hooks
    │   └── notify.py       # Notification hooks
    ├── web/                # Web interface (Starlette)
    │   ├── __init__.py
    │   ├── app.py          # Starlette app factory
    │   ├── api.py          # JSON API endpoints
    │   └── routes.py       # HTML page routes
    ├── templates/          # Jinja2 templates
    │   ├── base.html
    │   ├── kanban.html
    │   ├── worktrees.html
    │   └── ...
    └── static/             # CSS and JS assets
        ├── kanban.css
        └── kanban.js
```

## Key Commands

```bash
# Development
uv sync                          # Install dependencies
uv run worktree-manager --help   # Run CLI
uv run worktree-manager web      # Start web UI

# Code quality
uv run ruff format .             # Format code
uv run ruff check . --fix        # Lint and fix

# Package management
uv build                         # Build wheel
uv tool install .                # Install globally
```

## Important Patterns

### Template Syntax (Jinja2, NOT Django)

This project uses Jinja2 templates, not Django templates. Key differences:

```jinja2
{# CORRECT - Jinja2 #}
{% for item in items %}
    {{ item.name }}
{% else %}
    No items
{% endfor %}

{{ value|date("M j, H:i") }}

{# WRONG - Django syntax #}
{% empty %}                    {# Use {% else %} instead #}
{{ value|date:"M j, H:i" }}    {# Use parentheses, not colon #}
```

### Async Routes

Web routes are async functions using Starlette:

```python
async def my_route(request: Request) -> Response:
    templates = get_templates(request)
    return templates.TemplateResponse(request, 'template.html', {'key': 'value'})
```

### Data Storage

- **Registry** (`~/.config/dispatch-guru/registry.json`): Worktree metadata, ports, paths
- **Tasks** (`~/.config/dispatch-guru/tasks.db`): SQLite database for Kanban task tracking

### Port Allocation

Ports are allocated by worktree index:
- Index 0: Web 8000, DB 5432
- Index 1: Web 8010, DB 5442
- Index N: Web 8000+N*10, DB 5432+N*10

### Safety Checks (Close Worktree)

Before closing a worktree, the system checks:
1. Uncommitted changes
2. Unpushed commits
3. Branch merged to develop

If any check fails, user must use `--force` or "Force Close" in the UI.

## Configuration Files

### User Config (`~/.config/dispatch-guru/config.json`)

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

### Project Config (`.worktree-manager.json`)

Created per-project with `worktree-manager init`.

## Code Style

- **Formatter**: Ruff
- **Quote style**: Single quotes
- **Line length**: 120 characters
- **Type hints**: Encouraged but not strictly enforced

## Testing

No test suite exists yet. `pytest` and `pytest-asyncio` are declared as optional dev dependencies, but `tests/` is empty — add tests under that path when introducing coverage.

## Common Tasks

### Adding a New CLI Command

1. Add command function in `commands.py`
2. Add argument parser in `cli.py`
3. Wire up in `cli.py` main function

### Adding a New Web Route

1. Add route function in `web/routes.py`
2. Add to routes list in `web/app.py`
3. Create template in `templates/`

### Adding a New Hook

1. Create hook class in `hooks/`
2. Register in `hooks/__init__.py`

## Dependencies

Core (required):
- `rich>=13.0.0` - Terminal formatting
- `starlette>=0.40.0` - Web framework
- `uvicorn>=0.30.0` - ASGI server
- `jinja2>=3.1.0` - Templates
- `filelock>=3.0.0` - File locking

Dev (optional):
- `pytest>=8.0.0` - Testing
- `ruff>=0.8.0` - Linting/formatting
- `mypy>=1.13.0` - Type checking

## on template changes
- uv tool install /home/jeremy/projects/worktree-manager --force
