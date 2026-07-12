# Worktree Manager development commands

# Show available commands
default:
    @just --list

# Show current version
version:
    uv version

# Bump patch version and install (1.1.1 -> 1.1.2)
bump-patch:
    uv version --bump patch
    uv tool install . --force

# Bump minor version and install (1.1.1 -> 1.2.0)
bump-minor:
    uv version --bump minor
    uv tool install . --force

# Bump major version and install (1.1.1 -> 2.0.0)
bump-major:
    uv version --bump major
    uv tool install . --force

# Install without version bump (--reinstall implies --refresh, forcing a fresh
# build even at the same version — no global `uv cache clean` lock contention)
install:
    uv tool install . --force --reinstall

# Run linter
lint:
    ruff check .

# Run linter with auto-fix
fix:
    ruff check . --fix

# Format code
fmt:
    ruff format .

# Run tests
test:
    uv run pytest

# Run all checks (lint + test)
check: lint test

# Sync dependencies
sync:
    uv sync
