# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

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
