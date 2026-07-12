# Handover: Dispatch Guru moved to a shared dev Postgres server

**To:** worktree-manager maintainer
**From:** Dispatch Guru side (Claude Code)
**Date:** 2026-07-12
**Dispatch Guru refs:** PR #862 (shared dev DB), issue #861, ADR‑0027 · sibling PR #859 (issue #858, shared dev *image*)

## TL;DR

Dispatch Guru no longer runs a **per‑worktree `db-postgres` container**. It now runs **one shared Postgres server** (`dg-shared-postgres`, its own Compose project `dg-shared`, published on host port `SHARED_DB_PORT`, default `5432`). Each worktree/checkout gets its own **database** on that server, created instantly by cloning a template:

```
CREATE DATABASE <worktree_db> TEMPLATE dg_seed
```

`dg_seed` is a small, PII‑free demo dataset built once per host by `just seed-shared`. The worktree DB name is the worktree's `COMPOSE_PROJECT_NAME` with hyphens→underscores (e.g. dir `wt-foo-bar` → db `wt_foo_bar`).

This changes several assumptions worktree-manager currently bakes in (per‑worktree `DB_PORT`, the `db-postgres` service, the restore‑script clone path). None of it breaks worktree-manager immediately — Dispatch Guru's `just` recipes are self‑sufficient — but a few things worktree-manager does are now **redundant, subtly wrong, or a missed opportunity**. Details below.

> **Not urgent / non‑breaking:** `wt create` still works — `just up` inside the new worktree provisions the DB itself. The items below make worktree-manager *correct and optimal* for the new model, and let `wt` own the fast path.

---

## What the new Dispatch Guru dev‑DB model looks like

Per‑worktree `.env` now needs (from worktree-manager's perspective):

| var | old | new |
|---|---|---|
| `WEB_PORT` | per‑worktree unique | **unchanged** — still per‑worktree unique |
| `REDIS_PORT` | per‑worktree unique | **unchanged** — Redis stays per‑worktree |
| `DB_PORT` | per‑worktree unique | **unused by the dev stack** — `db-postgres` is gone from the dev flow (see below). Harmless to keep, but it no longer maps to a running dev container. |
| `SHARED_DB_PORT` | — | **NEW. Must be identical across every worktree** (it is the one shared server's published port). Default `5432`. Do **not** allocate this per‑index. |

The relevant `just` recipes on the Dispatch Guru side:

- `just seed-shared` — **once per host.** Ensures the shared server is up and builds the `dg_seed` template (migrate + factory seed). Also creates the Traccar DB.
- `just db-shared-up` — ensures the shared container is running. **Idempotent and stable**: it no‑ops if already running, `docker start`s it if stopped, and only `compose up`s when genuinely absent. (This matters — see "Do not `compose up` the shared server yourself" below.)
- `just db-ensure` — clones this worktree's DB from `dg_seed` if it doesn't exist yet. Fast (~1s). Errors clearly if `dg_seed` is missing ("run `just seed-shared` first").
- `just up` — runs `db-shared-up` → `db-ensure` → starts `web`/`redis`/`celery` pointed at the shared server. So **a plain `just up` in a fresh worktree already provisions the DB.**
- `just prune` — drops **only** this worktree's DB (`DROP DATABASE … WITH (FORCE)`) and then `docker compose down -v`. `just down` (plain stop) never touches the DB.

CI is unaffected: `db-postgres` still exists in `docker-compose.local.yml` but behind `profiles: [ci]`, so a dev `just up` never starts it; only the self‑hosted CI runner opts in with `--profile ci`.

---

## Impact on worktree-manager — concrete items

### 1. Seed `SHARED_DB_PORT` into `.env` (and treat it as a global, not per‑worktree)
`commands.py:153‑180` copies `.env.example` and rewrites `WEB_PORT`/`DB_PORT`/`REDIS_PORT`. Add `SHARED_DB_PORT` handling, but with **the same value for every worktree** (read it from the project config or default `5432`) — never derive it from the worktree index. If two worktrees get different `SHARED_DB_PORT`s they'll point at different (or nonexistent) servers.

Dispatch Guru's `.env.example` now ships `SHARED_DB_PORT=5432` with a "keep identical across worktrees" comment, so a straight `.env.example` copy already carries the right value — just make sure your port‑rewriting pass doesn't clobber it.

### 2. `DB_PORT` allocation is now vestigial
`ports.py` / `__init__.py:13` (`BASE_DB_PORT = 5432`) / `config.py` `db_base` allocate a unique `DB_PORT` per worktree. Nothing in the dev stack consumes it anymore (the dev `db-postgres` service is gone). Options, in order of preference:
- Keep allocating it (harmless) but stop *port‑conflict‑checking* it (`ports.py:99‑101`) — otherwise you may report false conflicts against the shared server on 5432.
- Or drop DB_PORT allocation entirely and only allocate `WEB_PORT`/`REDIS_PORT`.
Either way, **add `SHARED_DB_PORT` to the conflict check once**, globally (it's one port shared by all).

### 3. The DB‑restore/clone path changed
`commands.py:1109‑1191` invokes `db_restore_script` (`database_tools/update_local_restore.sh`) and passes `env['LOCAL_DB_PORT'] = str(target.ports.db)` (`:1184`). Two things:
- **`LOCAL_DB_PORT = target.ports.db` is now wrong.** The restore script was updated (PR #862) to target the shared container `dg-shared-postgres` and derive the DB from the worktree name; it reads `SHARED_DB_PORT`, not a per‑worktree `DB_PORT`. Pass `SHARED_DB_PORT` (global) instead, or let the script default it.
- **For a *fresh* worktree you usually don't need the restore script at all** — `just db-ensure` (clone from `dg_seed`) is the instant path. Reserve `update_local_restore.sh` for when the user explicitly wants real/prod‑shaped data. Consider making "clone from template" the default `wt create` behavior and the full restore an opt‑in flag.

### 4. `wt create` fast path (the #858 + #862 opportunity, gated by `auto_build`)
This is the payoff. On `wt create`, after seeding `.env`, do (gated behind `docker_settings.auto_build` / a new setting):
1. **(from #858)** build the shared dev *image* once if absent — `dispatch-guru-dev:latest` is shared across worktrees, so only the first create pays the build.
2. ensure the shared *server* + template exist: if `dg_seed` is missing, run `just seed-shared` (one‑time, ~minutes); otherwise `just db-shared-up` (instant).
3. clone this worktree's DB: `just db-ensure` (or just run `just up`, which does 2+3 and starts the stack).

Net effect: **second and later worktrees come up in seconds** — no image build, no DB container boot, no restore. First‑ever worktree on a clean host pays the one‑time image build + `seed-shared`.

### 5. Do **not** `compose up` the shared server yourself from a worktree dir
If worktree-manager ever brings the DB up directly, use `just db-shared-up` (or `docker compose -f docker-compose.shared.yml -p dg-shared up -d` **only when the container is absent**). Running `compose up` for project `dg-shared` from different worktree directories makes Compose recreate the one shared container (config drift / the explicit `container_name`), briefly bouncing the DB for every *other* active worktree. Dispatch Guru's `db-shared-up` already guards against this (no‑op if running); mirror that if you re‑implement it.

### 6. `wt close` should drop the worktree's database
`close_worktree` (`commands.py:530`) tears down containers/volumes. The worktree's DB now lives on the **shared** server, not in a per‑worktree volume, so `keep_volumes` doesn't cover it and it will be **orphaned** on close. Add a step that drops it: run `just db-drop-self` (or `just prune`) in the worktree before removal, or `DROP DATABASE "<worktree_db>"` on `dg-shared-postgres` directly. Use the same name derivation as Dispatch Guru: `compose_project_name` lowercased, hyphens→underscores. (Respect a `--keep-db`/`keep_volumes` escape hatch if the user wants to preserve it.)

### 7. Config model: the "db" service is now external
`config.py:80‑83` / `get_db_service_name()` model the DB as a per‑worktree Compose service named `db-postgres`. That's now the *CI‑only* profiled service; the dev DB is the external `dg-shared-postgres`. If any worktree-manager logic waits on the `db-postgres` service to be healthy (health‑gating `wt create`), point it at the shared server instead (`docker inspect dg-shared-postgres` / `pg_isready` against `SHARED_DB_PORT`). Consider adding a `shared_db` block to the project config:
```json
"shared_db": {
  "compose_file": "docker-compose.shared.yml",
  "project_name": "dg-shared",
  "container_name": "dg-shared-postgres",
  "port_env_var": "SHARED_DB_PORT",
  "template_db": "dg_seed",
  "seed_command": "just seed-shared",
  "ensure_command": "just db-ensure",
  "drop_command": "just db-drop-self"
}
```
so this stays project‑configurable rather than hard‑coded.

### 8. `docker_ops.py` env scrubbing
`docker_ops.py:95/149/199` strip `DB_PORT`/`WEB_PORT`/`POSTGRES_PORT`/`REDIS_PORT` from the shell env before invoking Compose (so `.env` wins). If worktree-manager ever exports `SHARED_DB_PORT`, add it to that scrub list too — but really `SHARED_DB_PORT` should only ever come from `.env`, identically everywhere.

---

## Compatibility / migration notes

- **Existing worktrees** created under the old model still have a per‑worktree `db-postgres` in their (older) checkout of `docker-compose.local.yml`. Once they pull the branch with PR #862, their next `just up` switches them to the shared server and provisions a fresh DB from `dg_seed` (their old per‑worktree DB volume is simply no longer used; reclaim with `docker volume rm` if desired). No data migration is performed (matches the Dispatch Guru decision).
- **One‑time host setup:** `just seed-shared` must run once on the host before any worktree's `just up` can clone a DB. `wt create` is the natural place to trigger this lazily (item 4).
- **Port 5432:** while both models coexist on a host, the old main‑checkout `db-postgres` may still hold 5432. Dispatch Guru dev/test happened on `SHARED_DB_PORT=5500` to avoid that during rollout; the committed default is `5432` for once the old container is retired. If your conflict‑checker sees 5432 taken, that's expected during the transition.

---

## Suggested order of work

1. Item 1 (`SHARED_DB_PORT` in `.env`, global) + item 2 (stop conflict‑checking per‑worktree `DB_PORT`) — smallest, unblocks correctness.
2. Item 6 (`wt close` drops the DB) — prevents orphan‑DB accumulation on the shared server.
3. Item 4 (`wt create` fast path) — the headline UX win; do after 1 so `.env` is correct first.
4. Items 3, 7, 8 — cleanups / config‑model tidy.

## Questions for you / open decisions

- Do you want `wt create` to **default** to template‑clone (`db-ensure`) with full prod restore behind a flag, or keep restore as the default? (Dispatch Guru's expectation is clone‑by‑default; restore is the heavy, occasional path.)
- Should `wt` own running `just seed-shared` the first time, or leave that as a documented manual host‑setup step?
- Where should `SHARED_DB_PORT` live authoritatively — `.env.example` (already there) is enough, or do you want it in `.worktree-manager.json` too?

Ping the Dispatch Guru side (PR #862 / ADR‑0027) if you want the exact recipe internals; the `justfile`, `docker-compose.shared.yml`, and `scripts/shared_db.sh` on that branch are the source of truth.
