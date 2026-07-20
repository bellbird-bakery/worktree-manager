# Optional Services Gate

Let a project declare which services it uses, so worktree-manager stops assuming
every project has a database, a Redis instance, and Docker.

## Problem

`wt create` allocates and conflict-checks a web port, a DB port and a Redis port
for every worktree, unconditionally. On a project that has none of those — a
plain uv-managed app with a web server — this causes three problems:

1. **`wt create` can refuse to run.** `commands.py:145-150` aborts with exit 1 if
   anything is already listening on the allocated DB or Redis port. A machine
   with a local Postgres on 5432 blocks worktree index 0 outright, and there is
   no flag to skip the check.
2. **`.env` gets keys the project does not use.** `_configure_env_ports`
   (`commands.py:274`) always writes `DB_PORT` and `REDIS_PORT`.
3. **`wt status` always fails.** `validator.py:292-306` registers "Docker is not
   running" and "docker-compose.local.yml not found" as `is_error=True`, and
   `show_status` returns exit 1 on `report.has_errors` (`commands.py:779`).

Everything else already degrades gracefully: Docker calls on the create and
close paths are gated behind `is_docker_running()` or behind config keys
(`shared_db`, `shared_redis`, `dev_image`) that default to empty.

## Approach

A project declares its services by listing them in the `services` block of
`.worktree-manager.json`. Omitting a service means the project does not use it.

```json
{
  "base_branch": "develop",
  "services": {"web": {"name": "web", "port_env_var": "WEB_PORT"}},
  "env_template": ".env.example"
}
```

This reuses the existing `services` block rather than introducing a parallel set
of `uses_db` / `uses_redis` booleans, so there is one source of truth.

## Design

### 1. Config semantics

`redis` joins `web` and `db` in `DEFAULT_PROJECT_CONFIG['services']`
(`config.py:75`), which currently defines only the first two.

`services` becomes **name-authoritative**: when `.worktree-manager.json` supplies
a `services` block, its keys are the complete set of services for that project.

This requires special-casing `services` in `ProjectConfig.load`, because
`_deep_merge` (`config.py:603`) currently merges the default `db` entry back in
over a file that omits it, making "omitted" indistinguishable from "defaulted".
The rule:

- File has no `services` key → use the defaults verbatim. This is today's
  behaviour, so every existing project is unaffected.
- File has a `services` key → for each service name in the file, deep-merge its
  value against that service's default (so per-key defaults still fill in).
  Default services the file does not name are dropped.

Every other config key keeps using `_deep_merge` unchanged.

Two accessors follow:

```python
def has_db(self) -> bool:
    return 'db' in self.services

def has_redis(self) -> bool:
    return 'redis' in self.services
```

These are deliberately distinct from the existing `has_shared_db()` /
`has_shared_redis()`, which answer *where* a service lives rather than *whether*
it exists. A project may have neither, either, or both.

**Consequence to be aware of:** adding `redis` to the defaults means a project
with no `.worktree-manager.json` now has a Redis service where the schema
previously listed none. This matches current runtime behaviour — a Redis port is
already allocated and written for every worktree — so it is a schema change, not
a behaviour change.

### 2. Allocation and registry

`WorktreePorts.db` and `WorktreePorts.redis` become `int | None`. There is a
single definition of this dataclass, in `registry.py:44`, imported by
`ports.py:19`.

```python
def calculate_ports_for_index(index, *, has_db=True, has_redis=True) -> WorktreePorts:
    return WorktreePorts(
        web=BASE_WEB_PORT + index,
        db=(BASE_DB_PORT + index) if has_db else None,
        redis=(BASE_REDIS_PORT + index) if has_redis else None,
    )
```

Allocation stays purely index-derived, so disabling a service never shifts
another worktree's ports. No renumbering and no registry migration.

**Serialisation keeps two distinct absent-values.** `WorktreePorts.from_dict`
currently reads `data.get('redis', 0)`, where `0` means "legacy entry created
before Redis allocation existed". That fallback stays as-is. An explicit JSON
`null` is the new "service not used". `0` and `None` must not be collapsed into
each other.

The create-time conflict check then converges the shared-service case and the
no-service case on the same value:

```python
db_port_to_check = ports.db if not project_config.has_shared_db() else None
redis_port_to_check = ports.redis if not project_config.has_shared_redis() else None
conflicts = validate_ports(ports.web, db_port_to_check, redis_port_to_check)
```

`validate_ports` already skips `None` arguments, so it needs no change.

Display code (`wt list`, the create summary) renders `None` as `—` rather than a
port number.

### 3. `.env` generation

`_configure_env_ports` (`commands.py:274`) takes the new flags. When a service is
absent it writes no port key for that service, and the summary line omits that
segment — `WEB=58000` rather than `WEB=58000, DB=…, REDIS=…`.

The absent-service check comes first and short-circuits, so the existing
shared-DB and shared-Redis branches (including `REDIS_BROKER_DB` /
`REDIS_CACHE_DB` and `_comment_out_stale_redis_urls`) are reached only when the
service exists, and are otherwise unchanged.

### 4. `wt status`

The two hard errors at `validator.py:292-306` become conditional on the project
needing Docker at all. A project needs Docker when any service other than `web`
is declared, or when `dev_image`, `shared_db`, or `shared_redis` is set.

When Docker is not needed, both checks are **skipped entirely** rather than
recorded as passing, so the output does not imply a check ran that did not.

The compose-file check also starts reading `project_config.compose_file` instead
of the hardcoded `path / 'docker-compose.local.yml'` at `validator.py:305`.

**Out of scope:** `docker_ops.py` hardcodes `-f docker-compose.local.yml` at
lines 110, 165 and 218, ignoring `project_config.compose_file`. That is a real
inconsistency but it does not affect a project that runs no Docker, and fixing it
belongs in its own change.

## Testing

The config merge carries the most risk and gets direct unit tests:

- No `services` key in the file → all three defaults present.
- `services` with only `web` → `has_db()` and `has_redis()` both false, and
  `web` still has its default `port_env_var` filled in.
- `services` with `web` and `db` → `has_db()` true, `has_redis()` false.
- `services` with a partial entry (e.g. `{"db": {"name": "mydb"}}`) → unspecified
  keys fall back to their defaults.

Registry round-tripping:

- An entry with `redis: 0` loads as `0`, not `None` (legacy compatibility).
- An entry with `redis: null` loads as `None`.
- `to_dict` / `from_dict` round-trips a `None` port.

Behaviour:

- `calculate_ports_for_index` returns `None` for disabled services, and the same
  web port regardless of which other services are enabled.
- `_configure_env_ports` writes no `DB_PORT` when there is no db service, and
  still writes `REDIS_BROKER_DB` when `shared_redis` is configured.
- `wt status` on a project declaring only `web` produces no Docker errors and
  exits 0.

## Not doing

- A `--no-db` / `--skip-port-check` CLI flag. The gate is a property of the
  project, not of a single invocation.
- Inferring services from `.env.example` contents, which would be implicit and
  would break when no template exists.
- Removing the `hooks.post_create` / `hooks.pre_close` project keys, which are
  vestigial and never read. Unrelated cleanup.
