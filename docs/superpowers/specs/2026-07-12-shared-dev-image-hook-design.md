# Shared dev image hook — design

## Problem

When a dispatch-guru developer runs `wt create`, the new worktree eventually builds
its **own** Docker image (`<COMPOSE_PROJECT_NAME>-web`, etc.) even though the dev
image is functionally identical across worktrees — the only per-worktree thing
(source) is bind-mounted at runtime, not baked in. This causes a redundant image
build per worktree.

dispatch-guru#858 addresses the compose side by giving the dev services a single
shared `image:` tag (e.g. `dispatch-guru-dev:latest`) so worktrees reference one
image instead of building their own. This spec covers the **worktree-manager** side:
ensure that shared image exists at `wt create` time, so the first worktree builds it
once and every subsequent worktree starts instantly.

## Goals

- At `wt create`, build the shared dev image once if it is missing, so `just up` in
  any worktree finds it and skips the build.
- Ship safely **before** dispatch-guru#858 lands: a no-op until a `dev_image` tag is
  configured.
- Fully gated and predictable; no change to existing behavior when unconfigured.

## Non-goals

- Changing dispatch-guru's compose or Dockerfile (tracked in dispatch-guru#858).
- Per-worktree image tagging / `docker tag` band-aids (obsoleted by the shared tag).
- Rebuilding the image when dependencies change — that stays the user's `just build`.

## Design

### 1. New project-config field: `dev_image`

Add `dev_image: str | None = None` to `ProjectConfig` (and `DEFAULT_PROJECT_CONFIG`,
`save()`, and the `load()` mapping) in `config.py`.

- Names the shared image tag to ensure, e.g. `"dispatch-guru-dev:latest"`.
- **Default `None` → the hook does nothing.** This is what makes shipping before
  dispatch-guru#858 safe. It activates only when dispatch-guru adds a
  `.worktree-manager.json` with `"dev_image": "..."` (ties to contract §4).

### 2. `SharedImageHook` (new lifecycle hook)

New file `hooks/shared_image.py`, registered in `hooks/__init__.py`.

- `name = 'shared_image'`
- `description = 'Build the shared dev Docker image if it is missing'`

**`should_run(ctx)`** returns true only when **all** hold:
- `ctx.event == 'create'`
- The project config (loaded from `ctx.main_repo_path`) has a non-empty `dev_image`.
- Docker is running (`docker_ops.is_docker_running()`).
- The global `auto_build` docker setting is on (`Config.get_docker_setting('auto_build', True)`).
- The image is **absent**: `docker image inspect <dev_image>` exits non-zero.

**`execute(ctx)`**:
- Runs `docker compose -f <compose_file> build` in `ctx.main_repo_path` (builds and
  tags the shared image "from develop").
- Streams output to the terminal (no `capture_output`) — this is a rare, first-time,
  potentially multi-minute operation; silent capture would look hung.
- Prints a short notice first (e.g. "Building shared dev image `<tag>` (first
  time)…").
- Returns `HookResult(success=..., message=..., action_taken='shared_image_built')`.
  A build failure returns `success=False` but must **not** abort `wt create` (the
  hook manager already isolates hook exceptions; a failed build just means the user
  builds via `just build` later).

**Registration order** in `_register_default_lifecycle_hooks`: `uv_sync`,
`shared_image`, `claude_launch` (image ready before a Claude session is offered;
claude launch stays last).

**Hook config default**: add `'shared_image': {'enabled': True}` to
`hooks/config.py`'s `DEFAULT_CONFIG['hooks']`.

### 3. Fix: align the hooks config path (folds in a regression)

`hooks/config.py` hardcodes `~/.config/dispatch-guru/hooks.json` — it was missed in
the config-dir rename that moved `config.json`/`registry.json` to
`~/.config/worktree-manager/`. This also made a recent README edit
(`hooks.json` under `worktree-manager/`) disagree with the code.

Fix:
- Point `CONFIG_DIR` at `Path.home() / '.config' / CONFIG_DIR_NAME` (import the
  shared constant), matching `config.py`.
- Add a legacy path (`~/.config/dispatch-guru/hooks.json`) and migrate it to the new
  location on load if the new file is absent — mirroring `config.py`'s migration
  approach, so existing hook settings are preserved.

## Behavior summary

| State | `wt create` result |
|---|---|
| `dev_image` unset | Hook no-op (unchanged behavior). |
| `dev_image` set, image missing, docker up, auto_build on | Builds shared image once, streamed. |
| `dev_image` set, image already present | Skips (instant). |
| Docker down / auto_build off | Skips silently. |
| Build fails | `wt create` still succeeds; user builds via `just build`. |

## Testing (TDD, following `test_claude_hook.py`)

- `should_run` truth table: `dev_image` unset vs set × image present vs absent ×
  docker up vs down × auto_build on vs off — with `docker image inspect` and
  `is_docker_running` mocked.
- `execute`: invokes `docker compose -f <compose_file> build` with `cwd` =
  main repo, subprocess mocked; asserts the command, cwd, and `HookResult`.
- `hooks/config.py`: loads from the new dir; migrates a legacy
  `dispatch-guru/hooks.json` when the new file is absent.

## Risks / dependencies

- **Depends on dispatch-guru#858** for real-world value (the shared `image:` tag must
  exist so the build produces one tag and worktrees reference it). Until then the
  hook is dormant via the `dev_image` default.
- **Build time**: synchronous build blocks the first `wt create` for minutes. Chosen
  deliberately (predictable, one-time); output is streamed so it never looks hung.
- **Concurrency**: two simultaneous `wt create` with a missing image could both
  build. Docker handles this (last tag wins); not worth locking.

## Out of scope

- dispatch-guru compose/Dockerfile changes (#858).
- Auto-refresh on dependency changes.
