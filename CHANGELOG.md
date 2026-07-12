# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- **Shared-Redis support (`shared_redis` project block)** - Isolate worktrees on one
  shared Redis server by logical DB number instead of a per-worktree `REDIS_PORT`
  - New `redis_dbs_for_index(index)` helper: worktree `index` owns DBs `{2*index,
    2*index+1}` = (broker, cache/channels); the main checkout (index 0) gets `(0, 1)`
  - When a non-empty `shared_redis` block is configured, `create` writes
    `REDIS_BROKER_DB`/`REDIS_CACHE_DB` to `.env` (no `REDIS_PORT`), skips the
    per-worktree redis-port conflict check, and the validator checks
    `SHARED_REDIS_PORT` in place of `REDIS_PORT`. A no-op when the block is unset.
- **`close-all` command** - Close every registered worktree in one pass from the main repo
  - Safe by default: only closes worktrees whose branch is merged to the base branch
    with no uncommitted changes and no unpushed commits; the rest are skipped and
    reported in a plan table with their reason (`unmerged`/`dirty`/`unpushed`)
  - `--force` closes them all (auto-committing dirty ones); `--prune` additionally
    removes Docker volumes, drops each worktree's shared database, and flushes its
    shared-Redis DBs (a no-op unless those blocks are configured)
  - Shared teardown helper `_teardown_worktree`; single `close` now also flushes the
    shared-Redis DBs (when configured) for parity with `close-all --prune`
- **`--raw` flag for `create` command** - Use branch names as-is without `feature/` or `fix/` prefix
  - `worktree-manager create 003-my-branch --raw` creates branch `003-my-branch` directly
- **Shared dev image hook** - New `shared_image` lifecycle hook builds a shared dev
  Docker image once at `create` time if it is missing, so worktrees reuse one image
  instead of rebuilding per worktree
  - Enabled by setting `dev_image` (e.g. `"dispatch-guru-dev:latest"`) in
    `.worktree-manager.json`; a no-op when unset
  - Gated by the `auto_build` docker setting and skipped if the image already exists
    or Docker is not running

### Changed
- **`backfill-ports` repurposed** - Now migrates every registered worktree to Redis
  logical DB indices: writes `REDIS_BROKER_DB`/`REDIS_CACHE_DB` from the worktree
  index, removes the retired `REDIS_PORT`, and comments out any active
  `redis://redis:...` full-URL override (which would otherwise win over the composed
  parts and point celery at the now-CI-only `redis` host)

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
