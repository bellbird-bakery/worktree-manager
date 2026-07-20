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
- No `.worktree-manager.json` at all → infer from disk. See §2.

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
that reaches the full defaults now has a Redis service where the schema
previously listed none. This matches current runtime behaviour — a Redis port is
already allocated and written for every worktree — so for those projects it is a
schema change, not a behaviour change.

A project with a `.worktree-manager.json` that has no `services` key is
therefore unaffected. A project with *no* config file may now resolve to `web`
only, which **is** a behaviour change — but only when it has no compose file,
i.e. only when the DB and Redis ports were never backed by anything. See §2.

### 2. Zero-config detection

A project with no `.worktree-manager.json` should not have to write one just to
say "I am not a Docker project". When the config file is absent, services are
inferred from disk instead of taken from the defaults:

- A compose file is present → use the full defaults (`web`, `db`, `redis`).
  This is today's behaviour for every existing unconfigured project.
- No compose file → `web` only.

Detection probes a fixed candidate list, in order, taking the first that exists:

```python
COMPOSE_FILE_CANDIDATES = (
    'docker-compose.local.yml',
    'docker-compose.yml',
    'compose.yml',
    'compose.yaml',
)
```

Probing more than this tool's `docker-compose.local.yml` default matters: a
project using the standard `docker-compose.yml` with no wt config would
otherwise be detected as non-Docker and silently lose its DB and Redis ports.
When a candidate is found its name also becomes the effective `compose_file`.

This runs **only** when `.worktree-manager.json` is absent. Once the file
exists, `services` is authoritative and no inference happens — so a project can
always override a wrong guess by writing the config file.

Known limitation: inference reads the working tree, so `wt` behaves differently
in two checkouts of the same repo if one has its compose file gitignored.
Writing a `.worktree-manager.json` is the fix, and the `wt status` output names
which compose file (if any) was detected.

### 3. Allocation and registry

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

### 4. `.env` generation

`_configure_env_ports` (`commands.py:274`) takes the new flags. When a service is
absent it writes no port key for that service, and the summary line omits that
segment — `WEB=58000` rather than `WEB=58000, DB=…, REDIS=…`.

The absent-service check comes first and short-circuits, so the existing
shared-DB and shared-Redis branches (including `REDIS_BROKER_DB` /
`REDIS_CACHE_DB` and `_comment_out_stale_redis_urls`) are reached only when the
service exists, and are otherwise unchanged.

### 5. `wt status`

The two hard errors at `validator.py:292-306` become conditional on the project
needing Docker at all. **A project needs Docker when a compose file is
resolvable on disk, or when `dev_image`, `shared_db` or `shared_redis` is set.**

Note this keys off the compose file, *not* the service list. The two answer
different questions and must not be conflated:

- The **service list** drives port allocation and `.env` keys — what ports this
  worktree needs reserved.
- **Compose-file presence** drives the Docker checks — whether Docker is how
  those services actually run.

A project with a local system Postgres declares a `db` service, so it still gets
its DB port allocated and conflict-checked, but has no compose file, so
`wt status` correctly skips the Docker checks rather than failing.

"Resolvable on disk" means `project_config.compose_file` when
`.worktree-manager.json` is present, or the §2 candidate probe when it is not.
Either way the check replaces the hardcoded `path / 'docker-compose.local.yml'`
at `validator.py:305`.

When Docker is not needed, both checks are **skipped entirely** rather than
recorded as passing, so the output does not imply a check ran that did not.

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

Zero-config detection (§2), all with no `.worktree-manager.json` present:

- `docker-compose.yml` on disk → full defaults, and `compose_file` resolves to
  `docker-compose.yml`.
- `docker-compose.local.yml` on disk → full defaults, and it wins over
  `docker-compose.yml` when both exist.
- No compose file → `web` only, `has_db()` and `has_redis()` false.
- A `.worktree-manager.json` declaring only `web` **alongside** a compose file →
  `web` only. The config file suppresses inference entirely.

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
- `wt status` on a project declaring `db` but with no compose file skips the
  Docker checks (the local-Postgres case), while still allocating a DB port.
- `wt status` on a project with `shared_db` set but no compose file still runs
  the Docker checks.

## Not doing

- A `--no-db` / `--skip-port-check` CLI flag. The gate is a property of the
  project, not of a single invocation.
- Inferring services from `.env.example` contents. §2 does infer from disk, but
  only from compose-file presence, which is a direct statement about how
  services run; env-var names are a weak proxy, and no template file exists on
  many projects. Inference is also confined to the no-config case — writing
  `.worktree-manager.json` always wins.
- Removing the `hooks.post_create` / `hooks.pre_close` project keys, which are
  vestigial and never read. Unrelated cleanup.
