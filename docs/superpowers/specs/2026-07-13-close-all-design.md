# Design: `wt close-all [--prune] [--force]`

Date: 2026-07-13
Status: Approved (pending spec review)

## Summary

A bulk teardown command that closes every registered worktree in one pass:
stops their containers, removes the git worktrees, and clears them from the
registry. By default it only closes worktrees that are safe to close (branch
merged to base, no uncommitted changes, no unpushed commits). Two flags widen
the blast radius:

- `--force` — close every worktree regardless of the merged/clean/pushed gate.
- `--prune` — additionally remove Docker volumes and drop each worktree's shared
  database (and flush its shared-Redis logical DBs, when configured).

## Motivation

After a sprint of parallel feature work a developer accumulates many worktrees,
each with its own containers, volumes, and a slice of the shared dev-database /
shared-Redis server. Closing them one at a time is tedious. `close-all` collapses
that into a single, auditable operation while defaulting to the conservative
choice (never silently destroy unmerged or unpushed work, never drop data unless
explicitly asked).

## Command surface

New subcommand `close-all` (a dedicated subparser, **not** `close all`). A separate
subcommand is chosen over a reserved `all` positional on `close` to avoid an
argparse footgun and any collision with a real feature named `all`.

```
wt close-all [--prune] [--force] [--message MESSAGE] [--yes]
```

- `--prune` — remove volumes + drop shared DB + flush shared-Redis DBs (the
  destructive extras). Without it, volumes and databases are kept.
- `--force` — override the merged/clean/pushed gate; close every worktree
  (auto-committing dirty ones, as single `close` does).
- `--message MESSAGE` — commit message used for any auto-commits (only reachable
  under `--force`). Defaults to a generated message (e.g. `wip: close-all`).
- `--yes` / non-interactive handling — consistent with the rest of the CLI via
  `should_prompt()`; a single confirmation prompt gates the whole batch.

### Location constraint

`close-all` must be run **from the main repo**. It cannot delete a worktree it is
standing in, and mixing "the current worktree" into a bulk op is surprising. If
run from inside a worktree, it errors with a hint to `cd` to the main repo.

### Naming note (`--prune` vs the `prune` command)

A `prune` *command* already exists — it removes registry entries whose directory
no longer exists on disk. The `--prune` *flag* here is unrelated (it selects the
destructive teardown extras). This overlap is intentional per the request but is
called out in `--help` text for both to avoid confusion:
- `prune` command help: "Remove registry entries for worktrees that no longer
  exist on disk (does not touch containers or databases)."
- `close-all --prune` help: "Also remove Docker volumes and drop each worktree's
  shared database / Redis DBs."

## Selection gate

For each registered worktree, compute three predicates using existing helpers:

- **merged** — `git_ops.is_branch_merged(branch, base_branch)`
- **clean** — `not git_ops.has_uncommitted_changes_in_path(path)`
- **pushed** — `not git_ops.has_unpushed_commits(path)`

`base_branch` comes from the project/user config (`config.base_branch`, default
`develop`).

Decision:

| Mode        | Closes                                   |
|-------------|------------------------------------------|
| default     | worktrees where merged AND clean AND pushed |
| `--force`   | all worktrees                            |

Any worktree that fails a predicate under the default mode is **skipped** and
listed in the summary with its reason(s): `unmerged`, `dirty`, `unpushed`.

A pure function computes this so it is unit-testable in isolation:

```
def _classify_worktrees(worktrees, base_branch, force) -> tuple[list[Selected], list[Skipped]]
```

where each `Skipped` carries the worktree and a list of reason strings. Git
predicate calls are injected/mockable (module-level functions patched in tests,
following the `tests/test_redis_dbs.py` monkeypatch style).

## Per-worktree teardown

The teardown steps are factored out of the existing `close_worktree` into a
shared helper so `close`, and `close-all`, run one code path:

```
def _teardown_worktree(entry, project_config, *, prune: bool, message: str) -> TeardownResult
```

Steps (mirroring current `close_worktree`, lines ~790–850):

1. Auto-commit if dirty (only reachable under `--force`; uses `message`).
2. `fix_permissions(path, compose_project_name)` then
   `compose_down(path, compose_project_name, volumes=prune)`.
   - Volumes are **kept unless `--prune`**. (Note this inverts the single-`close`
     default, where volumes are removed unless `--keep-volumes`. For a bulk op the
     safe default is to keep; `--prune` is the explicit opt-in.)
3. If `prune`:
   - `_maybe_drop_shared_db(path, entry, project_config, keep_volumes=False)` —
     drops this worktree's DB on the shared server (no-op unless a `shared_db`
     block with a `drop_command` is configured).
   - If a shared-Redis `flush_command` is configured
     (`project_config.get_shared_redis_flush_command()`), run it in the worktree
     to flush this worktree's logical Redis DBs. Symmetric with the DB drop and
     ties into the per-worktree Redis DB allocation added in the last commit.
4. `remove_worktree(path, force=True)`.
5. Registry removal — collected and applied under a **single**
   `locked_registry(main_repo)` block for the whole batch (not one lock per
   worktree).

`refactor: extract _teardown_worktree` — `close_worktree` is updated to call the
shared helper (with `prune=not keep_volumes` to preserve its current semantics),
so behavior for single close is unchanged and covered by existing tests.

Best-effort semantics: a worktree that errors mid-teardown is recorded in the
result and the batch **continues** to the next — matching how single close already
swallows Docker warnings. Errors are surfaced in the final summary.

## Output & confirmation

1. **Plan** (before doing anything): a Rich table listing every worktree and its
   disposition — `close`, or `skip: <reasons>` — plus a one-line banner stating
   whether volumes/DBs will be pruned and whether `--force` is active.
2. **Confirmation**: one prompt gated by `should_prompt()` (bypassed by `--yes` /
   non-interactive). Declining is a clean no-op exit 0.
3. **Summary** (after): `N closed, M skipped, E errored`, with the skipped/errored
   detail. Exit code 0 on success, including "nothing to do" and best-effort
   teardown warnings. Non-zero is reserved for command-level failures:
   not-in-main-repo and registry-lock acquisition failure.

## Testing

Following `tests/test_redis_dbs.py` style (pytest + monkeypatch, no real git/docker):

- `_classify_worktrees`: full merged × clean × pushed matrix → correct
  close/skip partition; `--force` closes everything and reports no skips.
- Skip-reason strings are correct and combine (e.g. dirty + unpushed).
- `_teardown_worktree`: `prune=False` calls `compose_down(volumes=False)` and does
  NOT call the drop/flush commands; `prune=True` calls `compose_down(volumes=True)`,
  `_maybe_drop_shared_db`, and the Redis `flush_command` when configured (and skips
  flush when not configured).
- `close_worktree` still delegates to `_teardown_worktree` with the volume
  semantics preserved (regression guard for the refactor).
- Location guard: running `close-all` from inside a worktree errors without
  touching anything.

## Out of scope

- Parallel teardown (worktrees are closed sequentially; keeps output legible and
  avoids Docker/registry contention).
- Selecting a subset by pattern/age (`close-all` is all-or-nothing by design;
  granular selection stays with per-worktree `close`).
- Changing the existing `prune` command or single `close` defaults beyond the
  internal `_teardown_worktree` extraction.
