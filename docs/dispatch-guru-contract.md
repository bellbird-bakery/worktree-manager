# Worktree Manager ⇄ dispatch-guru Contract

This document defines the working contract between **worktree-manager** (the `wt`
CLI) and the **dispatch-guru** project — the only project this tool is used with.
It states what the tool guarantees to provide a dispatch-guru developer, what
dispatch-guru must supply in return for those guarantees to hold, and the caveats
specific to this pairing.

Scope: only what a developer working *on dispatch-guru* needs to know. It does not
cover developing worktree-manager itself.

- **Tool version described:** worktree-manager `1.6.0`
- **Config/registry location:** `~/.config/worktree-manager/` (the legacy
  `~/.config/dispatch-guru/` directory is auto-migrated on first run)

---

## 1. What worktree-manager provides (guarantees)

For dispatch-guru, the tool guarantees the following:

### Isolated worktrees
- `wt create <name>` creates a git worktree at `../worktrees/<name>` (sibling of
  the main repo) on a new branch.
- Branch naming: `feature/<name>` by default, `fix/<name>` with `-t fix`, or the
  raw name with `--raw`. New branches fork from `develop`.
- `wt list` / `wt status` report all registered worktrees and the current one.

### Per-worktree port allocation
- On create, the tool copies `.env.example` → `.env` in the new worktree and
  rewrites `WEB_PORT`, `DB_PORT`, and `REDIS_PORT` to values unique to that
  worktree's index. It also appends `UID`/`GID` for non-root containers.
- Allocation is by worktree index `N`:

  | Index | `WEB_PORT` | `DB_PORT` | `REDIS_PORT` |
  |-------|-----------|-----------|--------------|
  | 0 (main) | 58000 | 5432 | 6379 |
  | 1 | 58001 | 5433 | 6380 |
  | 2 | 58002 | 5434 | 6381 |
  | N | 58000 + N | 5432 + N | 6379 + N |

  Allocation happens atomically under a registry lock, so concurrent `wt create`
  calls will not hand out the same port.

### Shell integration (auto-`cd`)
- With `eval "$(wt shell-init)"` in your shell rc, `wt create` drops you into the
  new worktree and `wt close` returns you to the main repo.

### Close-time safety checks
- `wt close` refuses to remove a worktree that has (1) uncommitted changes,
  (2) unpushed commits, or (3) a branch not yet merged to `develop`, unless you
  pass `--force`. It also stops that worktree's Docker containers and fixes file
  permissions before removal.

### Database cloning
- `wt clone-db <index>` seeds the current worktree's database by running
  dispatch-guru's `database_tools/update_local_restore.sh`. It requires the
  worktree's Postgres container to be running first.

### Housekeeping
- `wt cleanup-orphans` removes Docker resources for worktrees no longer in the
  registry.
- `wt backfill-ports` assigns a `REDIS_PORT` to older worktrees created before
  Redis allocation existed (`--dry-run` to preview).

### Claude Code sessions
- `wt claude [name]` launches a Claude Code session scoped to a worktree; `-c`
  resumes the most recent session; args after the name pass through to `claude`.

### Lifecycle hooks
- `uv sync` and `claude_launch` hooks can run automatically on worktree create.
  Configured in `~/.config/worktree-manager/hooks.json`.

---

## 2. What dispatch-guru must provide (obligations)

These guarantees hold **only because** dispatch-guru's layout matches what the tool
expects. If any of the following changes, update this contract and/or add an
explicit `.worktree-manager.json` (see §4).

| Obligation | Current dispatch-guru value | Consumed by |
|---|---|---|
| Base branch is `develop` | `develop` | create (fork point) + close (merge check) |
| A compose file at the expected path | `docker-compose.local.yml` | all Docker operations |
| An env template carrying the three port keys | `.env.example` with `WEB_PORT` / `DB_PORT` / `REDIS_PORT` | `.env` generation on create |
| Web service named `web` | `web` | Docker ops |
| DB service named `db-postgres` | `db-postgres` | Docker ops, `clone-db` container detection |
| A DB restore script | `database_tools/update_local_restore.sh` | `clone-db` |
| Compose file reads `${WEB_PORT}` / `${DB_PORT}` / `${REDIS_PORT}` | it does | port isolation |

DB credentials (`POSTGRES_NAME=bellbird`, `POSTGRES_USER=bellbird`) live in
dispatch-guru's `.env` and are read by the restore script — worktree-manager does
**not** need them in its own config.

---

## 3. Shared responsibility: Docker isolation

**Important.** Isolation between concurrent worktrees is split across two tools, and
neither one owns all of it:

- **Ports** come from **worktree-manager**, written into each worktree's `.env`.
- **The Compose project name** — which namespaces networks, volumes, and containers,
  i.e. the *actual* isolation between environments — comes from **dispatch-guru's own
  `justfile`**, which sets `COMPOSE_PROJECT_NAME` on every `docker compose` call.
  worktree-manager does **not** write `COMPOSE_PROJECT_NAME` into `.env`.

Consequences a dispatch-guru dev must know:

1. **Always start/stop containers via `just`** (`just up`, `just down`, etc.).
   Running `docker compose up` directly, without `COMPOSE_PROJECT_NAME` set, falls
   back to the default project name `dispatch-guru` and will collide with other
   worktrees' networks and volumes.

2. **Avoid underscores in worktree names.** The two tools derive the project name
   independently:
   - dispatch-guru's justfile: `basename "$(git rev-parse --show-toplevel)"`,
     lowercased (underscores kept).
   - worktree-manager: the worktree directory name, lowercased, with `_` → `-`.

   For a name like `my-feature` they agree. For `my_feature` they **diverge**:
   `just up` creates `my_feature_*` resources while `wt close` /
   `wt cleanup-orphans` look for `my-feature` — leaving orphaned containers and
   volumes behind. Stick to letters, digits, and hyphens.

---

## 4. Optional: pinning the contract with `.worktree-manager.json`

dispatch-guru currently has **no** `.worktree-manager.json`; the tool runs entirely
on its built-in defaults, which happen to match dispatch-guru exactly (see §2).
Nothing is broken by its absence — including `clone-db`.

Adding the file is **optional**. Its only benefit is to *pin* these values so a
future change to the tool's defaults can't silently alter behavior, and to make the
coupling explicit for new developers. If you want that, drop this at the repo root:

```json
{
  "version": 1,
  "project_name": "dispatch-guru",
  "base_branch": "develop",
  "compose_file": "docker-compose.local.yml",
  "services": {
    "web": {"name": "web"},
    "db": {"name": "db-postgres", "type": "postgres"}
  },
  "database": {
    "type": "postgres",
    "name": "bellbird",
    "user": "bellbird"
  },
  "db_restore_script": "database_tools/update_local_restore.sh",
  "env_template": ".env.example"
}
```

You can also override the port bases here if 58000/5432/6379 ever conflict:

```json
"ports": { "web_base": 58000, "db_base": 5432 }
```

Note: `wt init` writes a `worktree_dir` key, but the tool ignores it — worktree
location is always `<repo-parent>/<name>`.

---

## 5. Known gaps and caveats

- **Traccar ports are not managed.** `TRACCAR_WEB_PORT` (default `8082`) and
  `TRACCAR_DEVICE_PORT` (default `5027`) are **not** allocated per worktree. The
  `traccar` service is gated behind the `--profile traccar` flag (off by default),
  so this only matters if you run that profile in two worktrees simultaneously —
  they will collide. Set these manually in the worktree's `.env` if you need it.
- **The README port table** in worktree-manager was historically wrong (it showed a
  base of 8000 with ×10 steps). The source of truth is §1 above and the corrected
  README table: base 58000, step of 1 per index.

---

## 6. Versioning

This contract describes worktree-manager `1.6.0`. If you upgrade the tool and any
guarantee in §1, obligation in §2, or default in §4 changes, revise this document.
