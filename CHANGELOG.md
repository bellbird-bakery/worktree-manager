# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- **`--raw` flag for `create` command** - Use branch names as-is without `feature/` or `fix/` prefix
  - `worktree-manager create 003-my-branch --raw` creates branch `003-my-branch` directly
- **Shared dev image hook** - New `shared_image` lifecycle hook builds a shared dev
  Docker image once at `create` time if it is missing, so worktrees reuse one image
  instead of rebuilding per worktree
  - Enabled by setting `dev_image` (e.g. `"dispatch-guru-dev:latest"`) in
    `.worktree-manager.json`; a no-op when unset
  - Gated by the `auto_build` docker setting and skipped if the image already exists
    or Docker is not running

### Removed
- **JSON task storage** - Removed `.worktree-tasks.json` file and all JSON synchronization features
  - Deleted `sync.py` module entirely
  - Removed `sync` and `cleanup-tasks` CLI commands
  - Removed sync status page, conflict resolution UI, and related templates
  - Tasks are now stored only in SQLite (`~/.config/dispatch-guru/tasks.db`)
  - The `sync-tasks` command remains but only syncs tasks with the worktree registry (no JSON)

### Changed
- Simplified task storage to SQLite-only (no more git-tracked JSON files cluttering repos)
- Removed `last_synced_at` field from Task model (no longer needed without JSON sync)
- Web UI no longer shows sync status or conflict warnings

### Fixed
- **Hooks config location** - `hooks.json` now lives under `~/.config/worktree-manager/`
  (aligned with the renamed config dir) and is migrated from the legacy
  `~/.config/dispatch-guru/hooks.json` on first load
