"""
Command implementations for worktree manager.

This module provides the main command handlers used by the CLI.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .docker_ops import (
    DockerError,
    compose_down,
    compose_ps,
    fix_permissions,
    get_compose_project_name,
    is_container_running,
    is_docker_running,
    list_project_containers,
    list_project_networks,
    list_project_volumes,
    remove_container,
    remove_network,
    remove_volume,
)
from .git_ops import (
    WORKTREE_PREFIX,
    GitError,
    commit_all,
    commit_all_in_path,
    create_worktree,
    get_branch_for_path,
    get_current_branch,
    get_main_repo_root,
    get_repo_root,
    has_uncommitted_changes,
    has_uncommitted_changes_in_path,
    has_unpushed_commits,
    is_branch_merged,
    is_worktree,
    list_worktrees,
    remove_worktree,
    validate_feature_name,
)
from .ports import calculate_ports_for_index, redis_dbs_for_index, validate_ports
from .registry import Registry, WorktreeEntry, locked_registry, read_registry
from .validator import validate_worktree

logger = logging.getLogger('worktree_manager')
console = Console()

# Environment variable set by the `wt` shell function (see `wt shell-init`).
# When present, it names a file the CLI writes a target directory to so the
# wrapping shell function can `cd` the parent shell after create/close.
CD_TARGET_ENV = 'WT_CD_FILE'


def _emit_cd_target(path: Path | str) -> None:
    """Write a directory for the shell wrapper to cd into, if integration is active.

    Does nothing when the ``WT_CD_FILE`` env var is unset (i.e. `wt` is being run
    directly rather than through the `wt shell-init` shell function).
    """
    cd_file = os.environ.get(CD_TARGET_ENV)
    if not cd_file:
        return
    try:
        Path(cd_file).write_text(f'{path}\n')
    except OSError as e:
        logger.warning(f'Failed to write cd target to {cd_file}: {e}')


def create_worktree_cmd(feature_name: str, branch_type: str | None = 'feature') -> int:
    """
    Create a new worktree with automatic port assignment.

    Args:
        feature_name: Name for the feature branch.
        branch_type: Branch prefix type ('feature' or 'fix'). None for raw (no prefix).

    Returns:
        Exit code (0 for success, non-zero for failure).
    """
    # Validate feature name
    is_valid, error_msg = validate_feature_name(feature_name)
    if not is_valid:
        console.print(f'[red]Error: {error_msg}[/red]')
        return 1

    console.print()
    console.print(Panel.fit('[bold]Creating New Worktree[/bold]', border_style='blue'))
    console.print()

    try:
        main_repo = get_main_repo_root()
    except GitError as e:
        console.print(f'[red]Error: {e}[/red]')
        return 1

    # Check if already in a worktree
    if is_worktree():
        console.print('[yellow]Warning: Currently in a worktree. Creating from main repository.[/yellow]')

    from .config import ProjectConfig

    project_config = ProjectConfig.load(repo_path=main_repo)

    # Lock registry and allocate ports
    with locked_registry(str(main_repo)) as registry:
        # Check if feature already exists
        existing = registry.find_by_feature(feature_name)
        if existing:
            console.print(f'[red]Error: Worktree for feature "{feature_name}" already exists at {existing.path}[/red]')
            return 1

        # Get next available index and ports
        next_index = registry.get_next_index()
        ports = calculate_ports_for_index(next_index)

        console.print(f'Feature: [cyan]{feature_name}[/cyan]')
        console.print(f'Index: [cyan]{next_index}[/cyan]')
        if project_config.has_shared_redis():
            broker_db, cache_db = redis_dbs_for_index(next_index)
            redis_display = f'shared DB {broker_db}/{cache_db}'
        else:
            redis_display = str(ports.redis)
        console.print(
            f'Ports: WEB=[cyan]{ports.web}[/cyan], DB=[cyan]{ports.db}[/cyan], REDIS=[cyan]{redis_display}[/cyan]'
        )
        console.print()

        # Check for port conflicts. For shared-DB projects the per-worktree DB port is
        # vestigial, so skip it (checking it would false-positive against the shared
        # server on 5432 — see HANDOVER-dispatch-guru-shared-db.md, item 2).
        db_port_to_check = None if project_config.has_shared_db() else ports.db
        # For shared-Redis projects the per-worktree REDIS_PORT is retired (worktrees
        # are isolated by logical DB number), so skip the redis-port conflict check —
        # the shared server's single port would otherwise false-positive across worktrees.
        redis_port_to_check = None if project_config.has_shared_redis() else ports.redis
        conflicts = validate_ports(ports.web, db_port_to_check, redis_port_to_check)
        if conflicts:
            console.print('[red]Port conflicts detected:[/red]')
            for conflict in conflicts:
                console.print(f'  - Port {conflict.port} ({conflict.port_type}): {conflict.process_name or "in use"}')
            console.print()
            console.print('[red]Cannot create worktree with conflicting ports.[/red]')
            return 1

        # Create git worktree
        console.print('Creating git worktree...')
        try:
            worktree_path, branch_name = create_worktree(
                feature_name, repo_path=str(main_repo), branch_type=branch_type
            )
            console.print(f'[green]Created: {worktree_path}[/green]')
        except GitError as e:
            console.print(f'[red]Error creating worktree: {e}[/red]')
            return 1

        # Create .env file
        console.print('Configuring environment...')
        env_template = worktree_path / project_config.env_template
        env_file = worktree_path / '.env'

        if env_template.exists():
            # Copy template
            shutil.copy(env_template, env_file)

            # Update ports in .env
            with open(env_file) as f:
                content = f.read()

            content, port_summary = _configure_env_ports(
                content,
                ports,
                project_config.has_shared_db(),
                has_shared_redis=project_config.has_shared_redis(),
                index=next_index,
            )

            # Add UID/GID for non-root Docker containers
            uid = os.getuid()
            gid = os.getgid()
            if 'UID=' not in content:
                content += f'UID={uid}\n'
            if 'GID=' not in content:
                content += f'GID={gid}\n'

            with open(env_file, 'w') as f:
                f.write(content)

            console.print(f'[green]Created .env with ports {port_summary}[/green]')
        else:
            console.print(f'[yellow]Warning: {project_config.env_template} not found. Create .env manually.[/yellow]')

        # Derive compose project name
        compose_project_name = worktree_path.name.lower().replace(' ', '-').replace('_', '-')

        # Add to registry
        entry = WorktreeEntry(
            path=str(worktree_path),
            compose_project_name=compose_project_name,
            ports=ports,
            feature_name=feature_name,
            index=next_index,
        )
        registry.add_worktree(entry)

    # Run lifecycle hooks for worktree creation
    from worktree_manager.hooks import WorktreeLifecycleContext, get_lifecycle_hook_manager

    lifecycle_ctx = WorktreeLifecycleContext(
        event='create',
        worktree_path=worktree_path,
        feature_name=feature_name,
        main_repo_path=main_repo,
        branch_name=branch_name,
    )
    hook_results = get_lifecycle_hook_manager().execute(lifecycle_ctx)

    for result in hook_results:
        if result.success:
            console.print(f'[green]{result.hook_name}: {result.message}[/green]')
        else:
            console.print(f'[yellow]{result.hook_name}: {result.message}[/yellow]')

    # Shared dev-DB fast path: clone this worktree's database from the template so
    # second-and-later worktrees come up in seconds. No-op unless the project configures
    # a shared_db block; best-effort so create still succeeds if it can't provision.
    _maybe_ensure_shared_db(worktree_path, compose_project_name, project_config)

    console.print()
    console.print(Panel.fit('[bold green]Worktree Created Successfully![/bold green]', border_style='green'))
    console.print()
    console.print('Next steps:')
    if os.environ.get(CD_TARGET_ENV):
        console.print('  1. [cyan]just build && just up[/cyan]')
    else:
        console.print(f'  1. cd [cyan]{worktree_path}[/cyan]')
        console.print('  2. [cyan]just build && just up[/cyan]')
    console.print()

    # Ask the shell wrapper (if any) to cd into the new worktree.
    _emit_cd_target(worktree_path)

    return 0


def _set_env_var(content: str, key: str, value: object) -> str:
    """Set or replace ``KEY=value`` on its own line in .env file *content*.

    Matches only a full-line assignment (``^KEY=...$`` in multiline mode) so a key
    that is a suffix of another is never clobbered — e.g. rewriting ``DB_PORT`` must
    not touch ``SHARED_DB_PORT`` (see HANDOVER-dispatch-guru-shared-db.md, item 1).
    """
    pattern = re.compile(rf'^{re.escape(key)}=.*$', re.MULTILINE)
    if pattern.search(content):
        return pattern.sub(lambda _m: f'{key}={value}', content)
    if content and not content.endswith('\n'):
        content += '\n'
    return content + f'{key}={value}\n'


def _configure_env_ports(
    content: str,
    ports,
    has_shared_db: bool,
    has_shared_redis: bool = False,
    index: int | None = None,
) -> tuple[str, str]:
    """Rewrite the per-worktree port assignments in a worktree's .env *content*.

    Returns ``(new_content, summary)`` where ``summary`` is the human-readable
    ``WEB=.., DB=.., REDIS=..`` line shown on create.

    Assignments are line-anchored (see :func:`_set_env_var`) so DB_PORT never
    clobbers SHARED_DB_PORT. For shared-DB projects the per-worktree DB port is
    vestigial: DB_PORT is left exactly as the template had it (never bumped to a
    bogus per-index value) and the summary reports the DB as shared rather than a
    fabricated port (see HANDOVER-dispatch-guru-shared-db.md, items 1/2).

    For shared-Redis projects the per-worktree REDIS_PORT is likewise retired:
    the worktree is isolated by two logical Redis DB numbers derived from its
    ``index`` (``REDIS_BROKER_DB``/``REDIS_CACHE_DB``) written in place of
    REDIS_PORT.
    """
    content = _set_env_var(content, 'WEB_PORT', ports.web)
    if has_shared_db:
        db_summary = 'shared (SHARED_DB_PORT)'
    else:
        content = _set_env_var(content, 'DB_PORT', ports.db)
        db_summary = str(ports.db)

    if has_shared_redis:
        broker_db, cache_db = redis_dbs_for_index(index if index is not None else 0)
        content = _remove_env_var(content, 'REDIS_PORT')
        content = _set_env_var(content, 'REDIS_BROKER_DB', broker_db)
        content = _set_env_var(content, 'REDIS_CACHE_DB', cache_db)
        content = _comment_out_stale_redis_urls(content)
        redis_summary = f'shared (DB {broker_db}/{cache_db})'
    else:
        content = _set_env_var(content, 'REDIS_PORT', ports.redis)
        redis_summary = str(ports.redis)

    summary = f'WEB={ports.web}, DB={db_summary}, REDIS={redis_summary}'
    return content, summary


def _remove_env_var(content: str, key: str) -> str:
    """Delete a full-line ``KEY=value`` assignment (and its newline) from .env *content*.

    Line-anchored like :func:`_set_env_var` so ``REDIS_PORT`` never removes a line
    that merely contains that substring (e.g. ``SHARED_REDIS_PORT``).
    """
    pattern = re.compile(rf'^{re.escape(key)}=.*\n?', re.MULTILINE)
    return pattern.sub('', content)


def _comment_out_stale_redis_urls(content: str) -> str:
    """Comment out full-URL Redis overrides that point at the CI-only ``redis`` host.

    ``CELERY_BROKER_URL``/``REDIS_URL`` full-URL overrides WIN over the composed
    ``REDIS_HOST``/``*_DB`` parts, so an active ``redis://redis:...`` line left in a
    migrated worktree silently breaks celery (the ``redis`` service is now CI-only).
    Only ACTIVE (uncommented) lines whose value uses the ``redis`` hostname are
    neutralised; overrides on any other host (``localhost``, ``host.docker.internal``)
    are preserved. ``CELERY_RESULT_BACKEND`` is dead (settings hardcode ``django-db``)
    and is left untouched.
    """
    pattern = re.compile(r'^(CELERY_BROKER_URL|REDIS_URL)=redis://redis:.*$', re.MULTILINE)
    return pattern.sub(lambda m: f'# {m.group(0)}', content)


def shared_db_name(compose_project_name: str) -> str:
    """Database name for a worktree on the shared dev server.

    Mirrors Dispatch Guru's derivation: the ``COMPOSE_PROJECT_NAME`` lowercased with
    hyphens turned into underscores (e.g. ``wt-foo-bar`` -> ``wt_foo_bar``).
    """
    return compose_project_name.lower().replace('-', '_')


def _run_worktree_command(worktree_path: Path | str, project_name: str, command: str) -> tuple[bool, str | None]:
    """Run a shell-style command (e.g. ``just db-ensure``) inside a worktree.

    Sets ``COMPOSE_PROJECT_NAME`` and scrubs port env vars so the worktree's ``.env``
    stays authoritative, matching how Compose is invoked elsewhere. Output streams to
    the terminal. Returns ``(success, error_message)``.
    """
    import shlex

    from .docker_ops import PORT_ENV_SCRUB

    env = os.environ.copy()
    for var in PORT_ENV_SCRUB:
        env.pop(var, None)
    env['COMPOSE_PROJECT_NAME'] = project_name

    try:
        result = subprocess.run(shlex.split(command), cwd=str(worktree_path), env=env)
    except (FileNotFoundError, OSError) as e:
        return False, str(e)
    if result.returncode == 0:
        return True, None
    return False, f'`{command}` exited with status {result.returncode}'


def _maybe_ensure_shared_db(worktree_path: Path, project_name: str, project_config) -> None:
    """Clone this worktree's DB from the shared server's template (item 4 fast path).

    Best-effort and gated behind docker ``auto_build``: on success a fresh worktree's
    database is ready in ~1s (no container boot, no restore). A no-op unless the project
    configures a ``shared_db`` block with an ``ensure_command``. Never runs the one-time
    host seed (``just seed-shared``) — that stays a manual host-setup step.
    """
    from .config import get_config

    if not project_config.has_shared_db():
        return
    ensure_cmd = project_config.get_shared_db_ensure_command()
    if not ensure_cmd:
        return
    if not get_config().get_docker_setting('auto_build', True):
        console.print('[dim]Skipping shared-DB clone (docker auto_build disabled). Run `just up` later.[/dim]')
        return
    if not is_docker_running():
        console.print(
            '[yellow]Docker not running; skipping shared-DB clone. Run `just up` in the worktree later.[/yellow]'
        )
        return

    console.print(f'Provisioning worktree database ([cyan]{ensure_cmd}[/cyan])...')
    ok, err = _run_worktree_command(worktree_path, project_name, ensure_cmd)
    if ok:
        console.print(f'[green]Worktree database ready: {shared_db_name(project_name)}[/green]')
    else:
        console.print(f'[yellow]Could not provision shared database: {err}[/yellow]')
        console.print(
            '[dim]If the template is missing, run `just seed-shared` once on this host, then `just up`.[/dim]'
        )


def _maybe_drop_shared_db(worktree_path: Path | str, entry, project_config, keep_volumes: bool) -> None:
    """Drop this worktree's database from the shared server on close (item 6).

    Unlike a per-worktree Docker volume, the shared-server DB isn't removed by
    ``compose down -v``, so it would be orphaned. A no-op unless the project configures
    a ``shared_db`` block with a ``drop_command``, or when ``keep_volumes`` is set.
    """
    if keep_volumes or not project_config.has_shared_db():
        return
    drop_cmd = project_config.get_shared_db_drop_command()
    if not drop_cmd:
        return

    db_name = shared_db_name(entry.compose_project_name)
    if not is_docker_running():
        console.print(
            f'[yellow]Docker not running; skipping shared-DB drop. Database {db_name} may be orphaned.[/yellow]'
        )
        return

    console.print(f'Dropping shared database [cyan]{db_name}[/cyan] ([cyan]{drop_cmd}[/cyan])...')
    ok, err = _run_worktree_command(worktree_path, entry.compose_project_name, drop_cmd)
    if ok:
        console.print('[green]Shared database dropped[/green]')
    else:
        console.print(f'[yellow]Could not drop shared database ({db_name}): {err}[/yellow]')


def _maybe_flush_shared_redis(worktree_path: Path | str, entry, project_config) -> None:
    """Flush this worktree's logical Redis DBs on the shared server (prune only).

    Symmetric with :func:`_maybe_drop_shared_db`: the shared-Redis server's logical
    DBs aren't cleared by ``compose down -v``, so they'd retain this worktree's data.
    A no-op unless the project configures a ``shared_redis`` block with a
    ``flush_command``.
    """
    flush_cmd = project_config.get_shared_redis_flush_command()
    if not flush_cmd:
        return
    if not is_docker_running():
        console.print('[yellow]Docker not running; skipping shared-Redis flush.[/yellow]')
        return
    console.print(f'Flushing shared Redis ([cyan]{flush_cmd}[/cyan])...')
    ok, err = _run_worktree_command(worktree_path, entry.compose_project_name, flush_cmd)
    if ok:
        console.print('[green]Shared Redis flushed[/green]')
    else:
        console.print(f'[yellow]Could not flush shared Redis: {err}[/yellow]')


@dataclass
class TeardownResult:
    """Outcome of tearing down one worktree."""

    feature_name: str
    ok: bool
    error: str | None = None


def _teardown_worktree(entry, project_config, *, prune: bool, message: str) -> TeardownResult:
    """Perform the destructive teardown of a single worktree.

    Shared by ``close`` and ``close-all`` so both run one code path:

    1. Auto-commit if the worktree is dirty (reachable in bulk only under ``--force``).
    2. Fix permissions, then ``compose down`` — removing volumes only when ``prune``.
    3. When ``prune``: drop this worktree's shared DB and flush its shared-Redis DBs.
    4. Remove the git worktree.

    Registry removal is left to the caller (batched under one lock). Best-effort: any
    error is captured in the returned :class:`TeardownResult` rather than raised.
    """
    path = entry.path
    try:
        if has_uncommitted_changes_in_path(path):
            commit_all_in_path(path, message)

        if is_docker_running():
            fix_permissions(path, entry.compose_project_name)
            compose_down(path, entry.compose_project_name, volumes=prune)

        if prune:
            _maybe_drop_shared_db(path, entry, project_config, keep_volumes=False)
            _maybe_flush_shared_redis(path, entry, project_config)

        remove_worktree(path, force=True)
    except (GitError, DockerError, OSError) as e:
        return TeardownResult(feature_name=entry.feature_name, ok=False, error=str(e))
    return TeardownResult(feature_name=entry.feature_name, ok=True)


def _migrate_env_redis(content: str, broker_db: int, cache_db: int) -> str:
    """Migrate a worktree's .env *content* from a per-worktree REDIS_PORT to logical DBs.

    Idempotent: removes any ``REDIS_PORT`` line, writes ``REDIS_BROKER_DB``/
    ``REDIS_CACHE_DB``, and comments out any active ``redis://redis:...`` full-URL
    override (which would otherwise WIN over the composed parts and point celery at
    the now-CI-only ``redis`` host). Overrides on other hosts are preserved.
    """
    content = _remove_env_var(content, 'REDIS_PORT')
    content = _set_env_var(content, 'REDIS_BROKER_DB', broker_db)
    content = _set_env_var(content, 'REDIS_CACHE_DB', cache_db)
    content = _comment_out_stale_redis_urls(content)
    return content


def backfill_redis_ports_cmd(dry_run: bool = False) -> int:
    """
    Assign Redis logical DB indices to existing worktrees (replaces REDIS_PORT).

    For each registered worktree, derive its two logical Redis DBs from its
    monotonic ``index`` (``REDIS_BROKER_DB=2*index``, ``REDIS_CACHE_DB=2*index+1``),
    write them into its .env, remove the retired ``REDIS_PORT`` line, and comment out
    any active ``redis://redis:...`` full-URL override that would silently break
    celery against the now-CI-only ``redis`` host.
    """
    console.print()
    console.print(Panel.fit('[bold]Migrating worktrees to Redis logical DB indices[/bold]', border_style='blue'))
    console.print()

    try:
        main_repo = get_main_repo_root()
    except GitError as e:
        console.print(f'[red]Error: {e}[/red]')
        return 1

    with locked_registry(str(main_repo)) as registry:
        worktrees = list(registry.worktrees)
        if not worktrees:
            console.print('[green]No registered worktrees. Nothing to do.[/green]')
            return 0

        for wt in worktrees:
            broker_db, cache_db = redis_dbs_for_index(wt.index)
            env_file = Path(wt.path) / '.env'
            console.print(
                f'  [cyan]{wt.feature_name}[/cyan] (index {wt.index}) -> '
                f'REDIS_BROKER_DB={broker_db}, REDIS_CACHE_DB={cache_db} (REDIS_PORT removed)'
            )

            if dry_run:
                continue

            try:
                content = env_file.read_text() if env_file.exists() else ''
                env_file.write_text(_migrate_env_redis(content, broker_db, cache_db))
            except OSError as e:
                console.print(f'    [yellow]Warning: could not update {env_file}: {e}[/yellow]')

    console.print()
    if dry_run:
        console.print('[yellow]Dry run - no changes written.[/yellow]')
        console.print(f'[bold]{len(worktrees)} worktree(s) would be updated.[/bold]')
    else:
        console.print(f'[bold green]Migrated {len(worktrees)} worktree(s) to Redis logical DB indices.[/bold green]')
    return 0


def resolve_worktree(
    registry: Registry, feature_name: str | None = None, cwd: Path | None = None
) -> WorktreeEntry | None:
    """
    Resolve a worktree entry by feature name, or by walking up from cwd.

    Args:
        registry: The worktree registry.
        feature_name: Feature name to look up. Takes precedence over cwd.
        cwd: Directory to resolve from (may be a subdirectory of a worktree).

    Returns:
        The matching entry, or None.
    """
    if feature_name:
        return registry.find_by_feature(feature_name)

    if cwd is not None:
        for candidate in [Path(cwd).resolve(), *Path(cwd).resolve().parents]:
            entry = registry.find_by_path(str(candidate))
            if entry:
                return entry

    return None


def build_claude_argv(continue_session: bool = False, extra_args: list[str] | None = None) -> list[str]:
    """Build the argv for launching a Claude Code session."""
    argv = ['claude']
    if continue_session:
        argv.append('--continue')
    if extra_args:
        argv.extend(extra_args)
    return argv


def claude_cmd(
    feature_name: str | None = None,
    continue_session: bool = False,
    extra_args: list[str] | None = None,
) -> int:
    """
    Launch a Claude Code session in a worktree.

    Resolves the worktree by feature name, or from the current directory
    when no name is given, then replaces this process with `claude` running
    in the worktree directory.

    Args:
        feature_name: Feature name of the target worktree.
        continue_session: Pass --continue to resume the most recent session.
        extra_args: Additional arguments passed through to claude.

    Returns:
        Exit code (only on failure; on success the process is replaced).
    """
    registry = read_registry()
    if not registry or not registry.worktrees:
        console.print('[red]No worktrees registered.[/red]')
        console.print('Create one with: [cyan]worktree-manager create <feature-name>[/cyan]')
        return 1

    entry = resolve_worktree(registry, feature_name=feature_name, cwd=Path.cwd())
    if not entry:
        if feature_name:
            console.print(f'[red]No worktree found for feature: {feature_name}[/red]')
        else:
            console.print('[red]Not inside a registered worktree. Pass a feature name.[/red]')
        console.print()
        console.print('Registered worktrees:')
        for wt in registry.worktrees:
            console.print(f'  [cyan]{wt.feature_name}[/cyan] ({wt.path})')
        return 1

    worktree_path = Path(entry.path)
    if not worktree_path.is_dir():
        console.print(f'[red]Worktree directory missing: {worktree_path}[/red]')
        console.print('Run [cyan]worktree-manager prune[/cyan] to clean up the registry.')
        return 1

    if not shutil.which('claude'):
        console.print('[red]claude not found on PATH.[/red]')
        console.print('Install Claude Code: [cyan]https://code.claude.com/docs/en/setup[/cyan]')
        return 1

    argv = build_claude_argv(continue_session=continue_session, extra_args=extra_args)
    console.print(f'Launching Claude Code in [cyan]{worktree_path}[/cyan]')
    os.chdir(worktree_path)
    os.execvp(argv[0], argv)
    return 0


def list_worktrees_cmd() -> int:
    """
    List all registered worktrees.

    Returns:
        Exit code (0 for success).
    """
    registry = read_registry()

    console.print()
    console.print(Panel.fit('[bold]Worktree Registry[/bold]', border_style='blue'))
    console.print()

    if not registry or not registry.worktrees:
        console.print('[yellow]No worktrees registered.[/yellow]')
        console.print()
        console.print('Create a new worktree with:')
        console.print('  [cyan]worktree-manager create <feature-name>[/cyan]')
        return 0

    table = Table(show_header=True, header_style='bold')
    table.add_column('#', style='dim')
    table.add_column('Feature')
    table.add_column('Path')
    table.add_column('WEB Port')
    table.add_column('DB Port')
    table.add_column('Status')

    for wt in registry.worktrees:
        # Check if path exists
        path_exists = Path(wt.path).exists()
        status = '[green]OK[/green]' if path_exists else '[red]Missing[/red]'

        # Check if containers are running
        if path_exists:
            containers = compose_ps(wt.path, wt.compose_project_name)
            if containers:
                running = sum(1 for c in containers if 'running' in c.status.lower())
                status = f'[green]{running} running[/green]'

        table.add_row(
            str(wt.index),
            wt.feature_name,
            wt.path,
            str(wt.ports.web),
            str(wt.ports.db),
            status,
        )

    console.print(table)
    console.print()
    console.print(f'Main repo: [cyan]{registry.main_repo_path}[/cyan]')
    console.print()

    return 0


def show_status() -> int:
    """
    Show status of the current worktree.

    Returns:
        Exit code (0 for success).
    """
    console.print()
    console.print(Panel.fit('[bold]Worktree Status[/bold]', border_style='blue'))
    console.print()

    try:
        repo_root = get_repo_root()
    except GitError:
        console.print('[red]Error: Not in a git repository[/red]')
        return 1

    current_path = str(repo_root)
    branch = get_current_branch()
    is_wt = is_worktree()

    console.print(f'Path: [cyan]{current_path}[/cyan]')
    console.print(f'Branch: [cyan]{branch}[/cyan]')
    console.print(f'Type: {"[cyan]Worktree[/cyan]" if is_wt else "[cyan]Main Repository[/cyan]"}')
    console.print()

    # Check registry
    registry = read_registry()
    if registry:
        entry = registry.find_by_path(current_path)
        if entry:
            console.print('[green]Registered in worktree registry[/green]')
            console.print(f'  Index: [cyan]{entry.index}[/cyan]')
            console.print(f'  Feature: [cyan]{entry.feature_name}[/cyan]')
            console.print(f'  WEB Port: [cyan]{entry.ports.web}[/cyan]')
            console.print(f'  DB Port: [cyan]{entry.ports.db}[/cyan]')
            console.print(f'  Project Name: [cyan]{entry.compose_project_name}[/cyan]')
        else:
            console.print('[yellow]Not registered in worktree registry[/yellow]')

    console.print()

    # Validate environment
    console.print('[bold]Environment Validation:[/bold]')
    report = validate_worktree(current_path, strict_ports=True)

    for result in report.results:
        if result.passed:
            console.print(f'  [green]{result.name}[/green]: {result.message}')
        elif result.is_error:
            console.print(f'  [red]{result.name}[/red]: {result.message}')
        else:
            console.print(f'  [yellow]{result.name}[/yellow]: {result.message}')

    console.print()

    if report.has_errors:
        console.print('[red]Validation failed. Fix errors before starting containers.[/red]')
        return 1

    return 0


def _classify_worktrees(worktrees, base_branch: str, force: bool) -> tuple[list, list[tuple]]:
    """Partition worktrees into (to_close, skipped) by the merged/clean/pushed gate.

    A worktree is safe to close when its branch is merged to ``base_branch`` and it
    has no uncommitted changes and no unpushed commits. Any worktree failing a
    predicate is skipped with an ordered list of reason strings
    (``unmerged``/``dirty``/``unpushed``). ``force`` closes everything, no reasons.

    Returns ``(to_close, skipped)`` where ``skipped`` is a list of
    ``(entry, reasons)`` tuples.
    """
    to_close = []
    skipped = []
    for wt in worktrees:
        if force:
            to_close.append(wt)
            continue
        branch = get_branch_for_path(wt.path)
        reasons = []
        if not is_branch_merged(branch, base_branch, repo_path=wt.path):
            reasons.append('unmerged')
        if has_uncommitted_changes_in_path(wt.path):
            reasons.append('dirty')
        if has_unpushed_commits(wt.path):
            reasons.append('unpushed')
        if reasons:
            skipped.append((wt, reasons))
        else:
            to_close.append(wt)
    return to_close, skipped


def close_worktree(message: str, keep_volumes: bool = False) -> int:
    """
    Close the current worktree.

    This will:
    1. Commit any uncommitted changes
    2. Stop Docker containers and remove their volumes
    3. Remove the git worktree
    4. Remove from registry

    Args:
        message: Commit message for any uncommitted changes.
        keep_volumes: Keep the worktree's Docker volumes instead of removing them.

    Returns:
        Exit code (0 for success).
    """
    from .cli import should_prompt

    console.print()
    console.print(Panel.fit('[bold]Closing Worktree[/bold]', border_style='yellow'))
    console.print()

    try:
        repo_root = get_repo_root()
    except GitError:
        console.print('[red]Error: Not in a git repository[/red]')
        return 1

    current_path = str(repo_root)

    if not is_worktree():
        console.print('[red]Error: Not in a worktree. Cannot close main repository.[/red]')
        return 1

    # Get registry entry
    registry = read_registry()
    entry = registry.find_by_path(current_path) if registry else None

    if not entry:
        console.print('[yellow]Warning: Worktree not found in registry.[/yellow]')

    from .config import get_project_config

    project_config = get_project_config(current_path)
    drops_shared_db = (
        entry is not None
        and not keep_volumes
        and project_config.has_shared_db()
        and bool(project_config.get_shared_db_drop_command())
    )

    console.print(f'Path: [cyan]{current_path}[/cyan]')
    console.print(f'Branch: [cyan]{get_current_branch()}[/cyan]')
    console.print()

    # Confirm
    console.print('[yellow]This will:[/yellow]')
    console.print('  - Commit any uncommitted changes')
    if keep_volumes:
        console.print('  - Stop Docker containers (keeping volumes)')
    else:
        console.print('  - Stop Docker containers and remove their volumes')
    if drops_shared_db:
        console.print(
            f"  - Drop this worktree's database ({shared_db_name(entry.compose_project_name)}) on the shared server"
        )
    console.print('  - Remove the worktree directory')
    console.print('  - Remove from registry')
    console.print()

    if should_prompt() and not console.input('Continue? (yes/no): ').lower() == 'yes':
        console.print('Cancelled.')
        return 0

    # Step 1: Commit changes
    if has_uncommitted_changes():
        console.print('Committing changes...')
        try:
            commit_all(message)
            console.print('[green]Changes committed[/green]')
        except subprocess.CalledProcessError as e:
            console.print(f'[red]Error committing: {e}[/red]')
            return 1
    else:
        console.print('[dim]No changes to commit[/dim]')

    # Step 2: Fix permissions and stop containers
    if entry and is_docker_running():
        # Fix permissions before stopping (Docker creates files as root)
        console.print('Fixing file permissions...')
        if fix_permissions(current_path, entry.compose_project_name):
            console.print('[green]Permissions fixed[/green]')
        else:
            console.print('[yellow]Could not fix permissions (container may not be running)[/yellow]')

        console.print('Stopping containers...')
        try:
            compose_down(current_path, entry.compose_project_name, volumes=not keep_volumes)
            if keep_volumes:
                console.print('[green]Containers stopped (volumes kept)[/green]')
                leftover = list_project_volumes(entry.compose_project_name)
                if leftover:
                    names = ' '.join(v.name for v in leftover)
                    console.print('[yellow]Kept volumes:[/yellow]')
                    for v in leftover:
                        console.print(f'  - {v.name}')
                    console.print(f'[dim]Remove them later with: docker volume rm {names}[/dim]')
            else:
                console.print('[green]Containers stopped and volumes removed[/green]')
        except DockerError as e:
            console.print(f'[yellow]Warning: {e}[/yellow]')

    # Step 2b: Drop this worktree's database and flush its Redis DBs on the shared dev
    # server (if any). Must run while the worktree (and its justfile) still exists,
    # before removal below.
    if entry:
        _maybe_drop_shared_db(current_path, entry, project_config, keep_volumes)
        if not keep_volumes:
            _maybe_flush_shared_redis(current_path, entry, project_config)

    # Step 3: Change to main repo (can't remove worktree while in it)
    main_repo = get_main_repo_root()
    os.chdir(main_repo)

    # Step 4: Remove git worktree
    console.print('Removing git worktree...')
    try:
        remove_worktree(current_path, force=True)
        console.print('[green]Worktree removed[/green]')
    except GitError as e:
        console.print(f'[red]Error: {e}[/red]')
        return 1

    # Step 5: Remove from registry
    if registry and entry:
        with locked_registry(str(main_repo)) as reg:
            reg.remove_worktree(current_path)
        console.print('[green]Removed from registry[/green]')

    console.print()
    console.print(Panel.fit('[bold green]Worktree Closed Successfully![/bold green]', border_style='green'))
    console.print()
    console.print(f'You are now in: [cyan]{main_repo}[/cyan]')
    console.print()

    # Ask the shell wrapper (if any) to cd back to the main repo.
    _emit_cd_target(main_repo)

    return 0


def close_all_cmd(
    prune: bool = False, force: bool = False, message: str | None = None, assume_yes: bool = False
) -> int:
    """Close every registered worktree in one pass.

    Run from the main repo. By default only worktrees whose branch is merged to the
    base branch and that have no uncommitted changes / unpushed commits are closed;
    the rest are skipped and reported. ``force`` closes them all. ``prune`` also
    removes Docker volumes and drops each worktree's shared DB / flushes its
    shared-Redis DBs (mirrors ``close``'s volume removal, which ``close-all`` keeps
    off by default for safety).
    """
    from .cli import should_prompt
    from .config import get_project_config

    console.print()
    console.print(Panel.fit('[bold]Closing All Worktrees[/bold]', border_style='yellow'))
    console.print()

    # Must run from the main repo (can't delete a worktree we're standing in). Compare
    # repo roots rather than trusting is_worktree(), which misreports when
    # `git rev-parse --git-common-dir` returns a relative path.
    try:
        current_root = get_repo_root().resolve()
        main_repo = get_main_repo_root().resolve()
    except GitError:
        console.print('[red]Error: Not in a git repository.[/red]')
        return 1
    if current_root != main_repo:
        console.print('[red]Error: Run close-all from the main repository, not inside a worktree.[/red]')
        console.print(f'[dim]cd to {main_repo} and try again.[/dim]')
        return 1

    registry = read_registry()
    worktrees = list(registry.worktrees) if registry else []
    if not worktrees:
        console.print('[green]No registered worktrees. Nothing to do.[/green]')
        return 0

    base_branch = get_project_config(str(main_repo)).base_branch
    to_close, skipped = _classify_worktrees(worktrees, base_branch, force)

    # Plan
    table = Table(title='Close-all plan', show_header=True, header_style='bold')
    table.add_column('Worktree')
    table.add_column('Index', justify='right')
    table.add_column('Disposition')
    for wt in to_close:
        table.add_row(wt.feature_name, str(wt.index), '[green]close[/green]')
    for wt, reasons in skipped:
        table.add_row(wt.feature_name, str(wt.index), f'[yellow]skip: {", ".join(reasons)}[/yellow]')
    console.print(table)
    extras = 'remove volumes + drop DB/Redis' if prune else 'keep volumes + data'
    console.print(f'[dim]Mode: {"force" if force else "safe"} · prune: {extras}[/dim]')
    console.print()

    if not to_close:
        console.print('[yellow]No worktrees eligible to close.[/yellow]')
        if skipped and not force:
            console.print('[dim]Use --force to close unmerged/dirty/unpushed worktrees anyway.[/dim]')
        return 0

    if not assume_yes and should_prompt() and console.input('Continue? (yes/no): ').lower() != 'yes':
        console.print('Cancelled.')
        return 0

    message = message or 'wip: close-all'
    results = []
    for wt in to_close:
        console.print(f'Closing [cyan]{wt.feature_name}[/cyan] (index {wt.index})...')
        project_config = get_project_config(wt.path)
        results.append(_teardown_worktree(wt, project_config, prune=prune, message=message))

    # Batched registry removal: only worktrees that tore down cleanly.
    ok_paths = [wt.path for wt, result in zip(to_close, results, strict=True) if result.ok]
    if ok_paths:
        with locked_registry(str(main_repo)) as reg:
            for path in ok_paths:
                reg.remove_worktree(path)

    n_ok = sum(1 for r in results if r.ok)
    n_err = len(results) - n_ok
    console.print()
    summary = f'[bold green]Closed {n_ok} worktree(s)[/bold green]'
    if skipped:
        summary += f', [yellow]skipped {len(skipped)}[/yellow]'
    if n_err:
        summary += f', [red]{n_err} errored[/red]'
    console.print(Panel.fit(summary, border_style='green' if not n_err else 'yellow'))
    for wt, result in zip(to_close, results, strict=True):
        if not result.ok:
            console.print(f'  [red]{wt.feature_name}: {result.error}[/red]')
    console.print()

    return 0


_POSIX_SHELL_INIT = """\
# worktree-manager shell integration.
# Wraps `wt` so `wt create` / `wt close` change the current shell's directory.
# Add to your shell rc file:  eval "$(wt shell-init)"
wt() {
    if [ "$1" = "shell-init" ]; then
        command wt "$@"
        return $?
    fi
    local _wt_cd_file
    _wt_cd_file="$(mktemp "${TMPDIR:-/tmp}/wt-cd.XXXXXX")" || { command wt "$@"; return $?; }
    WT_CD_FILE="$_wt_cd_file" command wt "$@"
    local _wt_ret=$?
    if [ -s "$_wt_cd_file" ]; then
        cd "$(cat "$_wt_cd_file")" || true
    fi
    rm -f "$_wt_cd_file"
    return $_wt_ret
}
"""

_FISH_SHELL_INIT = """\
# worktree-manager shell integration.
# Wraps `wt` so `wt create` / `wt close` change the current shell's directory.
# Add to your config.fish:  wt shell-init --shell fish | source
function wt
    if test "$argv[1]" = shell-init
        command wt $argv
        return $status
    end
    set -l _wt_cd_file (mktemp)
    env WT_CD_FILE=$_wt_cd_file command wt $argv
    set -l _wt_ret $status
    if test -s "$_wt_cd_file"
        cd (cat "$_wt_cd_file")
    end
    rm -f "$_wt_cd_file"
    return $_wt_ret
end
"""


def shell_init_cmd(shell: str | None = None) -> int:
    """Print shell integration code for `eval`/`source` in a shell rc file.

    Args:
        shell: 'bash', 'zsh', 'fish', or None to auto-detect from $SHELL.

    Returns:
        Exit code (0 for success).
    """
    if not shell:
        shell = os.path.basename(os.environ.get('SHELL', '')).lower()

    if shell == 'fish':
        script = _FISH_SHELL_INIT
    elif shell in ('bash', 'zsh', 'sh', 'ksh', ''):
        script = _POSIX_SHELL_INIT
    else:
        console.print(f'[red]Error: unsupported shell {shell!r} (use bash, zsh, or fish)[/red]')
        return 1

    # Print raw (no Rich markup) so the output is safe to eval/source.
    print(script, end='')
    return 0


def cleanup_orphans(dry_run: bool = False) -> int:
    """
    Clean up orphaned Docker resources and registry entries.

    Args:
        dry_run: Only report orphans, never delete anything.

    Returns:
        Exit code (0 for success).
    """
    from .cli import should_prompt
    from .config import get_project_config

    console.print()
    console.print(Panel.fit('[bold]Orphan Cleanup[/bold]', border_style='yellow'))
    console.print()

    registry = read_registry()
    if not registry:
        console.print('[yellow]No registry found. Nothing to clean up.[/yellow]')
        return 0

    project_name = get_project_config(registry.main_repo_path).project_name
    git_worktrees = {wt.path for wt in list_worktrees(registry.main_repo_path)}

    # Resources are protected if they belong to the main checkout, a registered
    # worktree, or any git worktree still on disk (even if missing from the
    # registry) — never offer those for deletion, regardless of registry state.
    protected_projects = {wt.compose_project_name for wt in registry.worktrees}
    protected_projects.update({get_compose_project_name(registry.main_repo_path), project_name})
    protected_projects.update(get_compose_project_name(path) for path in git_worktrees)

    def scan_orphans(list_resources):
        # Worktree resources are named wt-<feature>_* and don't contain the
        # project name, so scan both patterns and dedupe by resource name.
        by_name = {}
        for resource in [*list_resources(project_name), *list_resources(WORKTREE_PREFIX)]:
            by_name.setdefault(resource.name, resource)
        return [
            r
            for r in by_name.values()
            if r.project_name
            and r.project_name not in protected_projects
            and (r.project_name.startswith(WORKTREE_PREFIX) or project_name.lower() in r.project_name.lower())
        ]

    console.print('Scanning for orphaned resources...')
    console.print()

    orphaned_volumes = scan_orphans(list_project_volumes)
    orphaned_containers = scan_orphans(list_project_containers)
    orphaned_networks = scan_orphans(list_project_networks)
    # Check registry entries (paths that don't exist)
    orphaned_entries = [entry for entry in registry.worktrees if entry.path not in git_worktrees]

    total = len(orphaned_volumes) + len(orphaned_containers) + len(orphaned_networks) + len(orphaned_entries)

    if total == 0:
        console.print('[green]No orphaned resources found![/green]')
        return 0

    console.print(f'[yellow]Found {total} orphaned resources:[/yellow]')
    console.print()

    if orphaned_volumes:
        console.print(f'[yellow]Orphaned Volumes ({len(orphaned_volumes)}):[/yellow]')
        for v in orphaned_volumes:
            console.print(f'  - {v.name}')
        console.print()

    if orphaned_containers:
        console.print(f'[yellow]Orphaned Containers ({len(orphaned_containers)}):[/yellow]')
        for c in orphaned_containers:
            console.print(f'  - {c.name} (status: {c.status})')
        console.print()

    if orphaned_networks:
        console.print(f'[yellow]Orphaned Networks ({len(orphaned_networks)}):[/yellow]')
        for n in orphaned_networks:
            console.print(f'  - {n.name}')
        console.print()

    if orphaned_entries:
        console.print(f'[yellow]Orphaned Registry Entries ({len(orphaned_entries)}):[/yellow]')
        for e in orphaned_entries:
            console.print(f'  - {e.compose_project_name} (path: {e.path})')
        console.print()

    if dry_run:
        console.print('[dim]Dry run: nothing was deleted. Re-run without --dry-run to clean up.[/dim]')
        console.print()
        return 0

    # Confirm
    console.print('[red]WARNING: This will permanently delete these resources![/red]')
    console.print()
    if should_prompt() and not console.input('Continue with cleanup? (yes/no): ').lower() == 'yes':
        console.print('Cancelled.')
        return 0

    console.print()
    console.print('Starting cleanup...')
    console.print()

    # Remove containers first
    for c in orphaned_containers:
        console.print(f'  Removing container: {c.name}')
        remove_container(c.name)

    # Remove networks
    for n in orphaned_networks:
        console.print(f'  Removing network: {n.name}')
        remove_network(n.name)

    # Remove volumes
    for v in orphaned_volumes:
        console.print(f'  Removing volume: {v.name}')
        remove_volume(v.name)

    # Remove registry entries
    if orphaned_entries:
        with locked_registry(registry.main_repo_path) as reg:
            for e in orphaned_entries:
                console.print(f'  Removing registry entry: {e.compose_project_name}')
                reg.remove_worktree(e.path)

    console.print()
    console.print(Panel.fit('[bold green]Cleanup Complete![/bold green]', border_style='green'))
    console.print()
    console.print('Removed:')
    console.print(f'  - {len(orphaned_containers)} containers')
    console.print(f'  - {len(orphaned_networks)} networks')
    console.print(f'  - {len(orphaned_volumes)} volumes')
    console.print(f'  - {len(orphaned_entries)} registry entries')
    console.print()

    return 0


def update_last_accessed() -> int:
    """
    Update the last_accessed timestamp for the current worktree.

    Returns:
        Exit code (0 for success).
    """
    try:
        repo_root = get_repo_root()
    except GitError:
        return 0  # Silently fail if not in a git repo

    current_path = str(repo_root)

    registry = read_registry()
    if not registry:
        return 0

    entry = registry.find_by_path(current_path)
    if not entry:
        return 0

    with locked_registry(registry.main_repo_path) as reg:
        for wt in reg.worktrees:
            if wt.path == current_path:
                wt.last_accessed = datetime.now().isoformat()
                break

    return 0


def setup_cmd() -> int:
    """
    Run the interactive setup wizard.

    Returns:
        Exit code (0 for success).
    """
    from .setup_wizard import run_setup_wizard

    if run_setup_wizard():
        return 0
    return 1


def config_cmd(
    add_ignore: str | None = None,
    remove_ignore: str | None = None,
) -> int:
    """
    Show or modify configuration.

    Args:
        add_ignore: Pattern to add to ignore list.
        remove_ignore: Pattern to remove from ignore list.

    Returns:
        Exit code (0 for success).
    """
    from .config import get_config

    config = get_config()

    if add_ignore:
        config.add_ignore_pattern(add_ignore)
        config.save()
        console.print(f'[green]Added ignore pattern: {add_ignore}[/green]')
        return 0

    if remove_ignore:
        if remove_ignore in config.ignore_patterns:
            config.remove_ignore_pattern(remove_ignore)
            config.save()
            console.print(f'[yellow]Removed ignore pattern: {remove_ignore}[/yellow]')
        else:
            console.print(f'[red]Pattern not found: {remove_ignore}[/red]')
            return 1
        return 0

    # Default: show configuration
    console.print()
    console.print(Panel.fit('[bold]Worktree Manager Configuration[/bold]', border_style='blue'))
    console.print()

    console.print(f'[bold]Base Branch:[/bold] [cyan]{config.base_branch}[/cyan]')
    console.print()

    console.print('[bold]Ignore Patterns:[/bold]')
    if config.ignore_patterns:
        for pattern in config.ignore_patterns:
            console.print(f'  • {pattern}')
    else:
        console.print('  [dim](none)[/dim]')
    console.print()

    console.print('[bold]Docker Settings:[/bold]')
    console.print(f'  Stream output: [cyan]{config.get_docker_setting("stream_output", True)}[/cyan]')
    console.print(f'  Auto build: [cyan]{config.get_docker_setting("auto_build", True)}[/cyan]')
    console.print()

    console.print(f'[bold]Setup completed:[/bold] [cyan]{config.setup_completed}[/cyan]')
    console.print()

    console.print('[dim]Config file: ~/.config/worktree-manager/config.json[/dim]')
    console.print('[dim]Run "worktree-manager setup" to reconfigure[/dim]')
    console.print()

    return 0


def init_cmd(from_legacy: bool = False) -> int:
    """
    Initialize a project for worktree-manager.

    Creates a .worktree-manager.json configuration file in the project root.

    Args:
        from_legacy: Migrate from legacy dispatch-guru config.

    Returns:
        Exit code (0 for success).
    """
    import json

    console.print()
    console.print(Panel.fit('[bold]Initialize Project[/bold]', border_style='blue'))
    console.print()

    # Find the git root
    try:
        repo_root = get_repo_root()
    except GitError:
        console.print('[red]Error: Not in a git repository[/red]')
        return 1

    config_file = repo_root / '.worktree-manager.json'

    # Migrate from legacy config if requested
    if from_legacy:
        from .config import migrate_legacy_config

        console.print('Migrating from legacy configuration...')
        if migrate_legacy_config():
            console.print('[green]Legacy configuration migrated successfully![/green]')
        else:
            console.print('[yellow]No legacy configuration found to migrate.[/yellow]')
        console.print()

    if config_file.exists():
        console.print(f'[green]Configuration file exists: {config_file}[/green]')
        console.print()

        # Show current configuration
        try:
            with open(config_file) as f:
                config = json.load(f)
            console.print('Current configuration:')
            console.print(f'  Project: [cyan]{config.get("project_name", "N/A")}[/cyan]')
            console.print(f'  Base branch: [cyan]{config.get("base_branch", "develop")}[/cyan]')
            console.print(f'  Compose file: [cyan]{config.get("compose_file", "N/A")}[/cyan]')
            db = config.get('database', {})
            console.print(f'  Database: [cyan]{db.get("type", "N/A")}[/cyan]')
            console.print()
        except Exception as e:
            console.print(f'[yellow]Could not read config: {e}[/yellow]')

        return 0

    # Detect project name from directory name
    project_name = repo_root.name.lower().replace(' ', '-').replace('_', '-')

    # Create default configuration
    config = {
        'version': 1,
        'project_name': project_name,
        'base_branch': 'develop',
        'compose_file': 'docker-compose.local.yml',
        'services': {
            'web': {
                'name': 'web',
            },
            'db': {
                'name': 'db-postgres',
                'type': 'postgres',
            },
        },
        'database': {
            'type': 'postgres',
            'name': project_name.replace('-', '_'),
            'user': project_name.replace('-', '_'),
        },
        'worktree_dir': '../worktrees',
    }

    # Write configuration
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)
        f.write('\n')

    console.print(f'[green]Created configuration file: {config_file}[/green]')
    console.print()
    console.print('Configuration:')
    console.print(f'  Project: [cyan]{project_name}[/cyan]')
    console.print(f'  Base branch: [cyan]{config["base_branch"]}[/cyan]')
    console.print(f'  Compose file: [cyan]{config["compose_file"]}[/cyan]')
    console.print(f'  Database: [cyan]{config["database"]["type"]}[/cyan]')
    console.print()
    console.print('[dim]Edit .worktree-manager.json to customize settings.[/dim]')
    console.print()

    return 0


def load_db_cmd(
    feature_name: str | None = None,
    do_dump: bool = False,
    do_backup: bool = False,
) -> int:
    """
    Load production database into a worktree.

    Args:
        feature_name: Target worktree feature name. If None, uses current directory.
        do_dump: Fetch fresh production dump (default: use cached).
        do_backup: Backup current local database before loading (default: skip).

    Returns:
        Exit code (0 for success).
    """
    from .cli import should_prompt
    from .config import get_project_config

    console.print()
    console.print(Panel.fit('[bold]Load Database[/bold]', border_style='blue'))
    console.print()

    # Get registry
    registry = read_registry()
    if not registry:
        console.print('[red]Error: No worktree registry found[/red]')
        return 1

    project_config = get_project_config(registry.main_repo_path)
    restore_script = project_config.get_db_restore_script(registry.main_repo_path)

    # Find target worktree
    if feature_name:
        target = registry.find_by_feature(feature_name)
        if not target:
            console.print(f'[red]Error: Worktree "{feature_name}" not found in registry[/red]')
            return 1
    else:
        # Use current directory
        try:
            current_path = str(get_repo_root())
        except GitError:
            console.print('[red]Error: Not in a git repository[/red]')
            return 1

        target = registry.find_by_path(current_path)
        if not target:
            console.print('[red]Error: Current directory is not a registered worktree[/red]')
            return 1

    if project_config.has_shared_db():
        console.print(f'Target: [cyan]{target.feature_name}[/cyan] (db {shared_db_name(target.compose_project_name)})')
    else:
        console.print(f'Target: [cyan]{target.feature_name}[/cyan] (port {target.ports.db})')
    console.print(f'Path: [dim]{target.path}[/dim]')
    console.print()

    # Check prerequisites
    if not is_docker_running():
        console.print('[red]Error: Docker is not running[/red]')
        return 1

    if not restore_script.exists():
        console.print(f'[red]Error: Restore script not found at {restore_script}[/red]')
        console.print('[dim]Set "db_restore_script" in .worktree-manager.json to point at your restore script.[/dim]')
        return 1

    # Check the database is reachable. For shared-DB projects the dev database lives on
    # the external shared server (e.g. dg-shared-postgres), not a per-worktree container.
    if project_config.has_shared_db():
        container = project_config.get_shared_db_container()
        if container and not is_container_running(container):
            console.print(f'[red]Error: shared database container {container!r} is not running[/red]')
            console.print('[dim]Start it first: `just db-shared-up` (or `just up` in the worktree).[/dim]')
            return 1
    else:
        containers = compose_ps(target.path, target.compose_project_name)
        db_containers = [c for c in containers if 'db' in c.name.lower() or 'postgres' in c.name.lower()]
        db_running = any(c.status.lower() == 'running' for c in db_containers)
        if not db_running:
            console.print('[red]Error: Database container is not running[/red]')
            console.print(f'[dim]Start the worktree containers first: cd {target.path} && just up[/dim]')
            return 1

    # Build flags - by default we skip dump and backup for speed
    flags = []
    if not do_dump:
        flags.append('--skip-dump')
        console.print('[dim]Using cached production dump (use --dump to fetch fresh)[/dim]')
    else:
        console.print('[cyan]Fetching fresh production dump...[/cyan]')
    if not do_backup:
        flags.append('--skip-local-backup')
        console.print('[dim]Skipping local database backup (use --backup to backup first)[/dim]')
    else:
        console.print('[cyan]Backing up local database first...[/cyan]')

    # Confirm
    if should_prompt():
        console.print()
        console.print(
            '[yellow]Warning: This will DROP the target database and replace it with production data![/yellow]'
        )
        response = input('Continue? (y/N): ')
        if response.lower() != 'y':
            console.print('Cancelled.')
            return 0

    console.print()
    console.print('Loading database...')
    console.print()

    # Build environment
    env = os.environ.copy()
    env['DG_PATH'] = target.path
    if project_config.has_shared_db():
        # The restore script targets the shared container and derives the DB from the
        # worktree name; it reads the shared port (not a per-worktree DB_PORT). Pass the
        # global SHARED_DB_PORT from this worktree's .env; let the script default it if
        # absent (see HANDOVER-dispatch-guru-shared-db.md, item 3).
        from .validator import load_env_file

        port_env = project_config.get_shared_db_port_env()
        shared_port = load_env_file(target.path).get(port_env)
        if shared_port:
            env[port_env] = shared_port
    else:
        env['LOCAL_DB_PORT'] = str(target.ports.db)

    # Run script
    try:
        result = subprocess.run(
            [str(restore_script)] + flags,
            env=env,
            cwd=restore_script.parent,
            input='y\n',  # Auto-confirm the script's prompt
            capture_output=False,  # Stream output directly
            text=True,
            timeout=600,  # 10 minutes
        )

        if result.returncode == 0:
            console.print()
            console.print(Panel.fit('[bold green]Database Loaded Successfully![/bold green]', border_style='green'))
            return 0
        else:
            console.print()
            console.print('[red]Database load failed[/red]')
            return 1

    except subprocess.TimeoutExpired:
        console.print('[red]Error: Database load timed out after 10 minutes[/red]')
        return 1
    except Exception as e:
        console.print(f'[red]Error: {e}[/red]')
        return 1


def prune_missing_worktrees() -> int:
    """
    Remove worktrees from registry that no longer exist on disk.

    Returns:
        Exit code (0 for success).
    """
    from .cli import should_prompt

    console.print()
    console.print(Panel.fit('[bold]Prune Missing Worktrees[/bold]', border_style='blue'))
    console.print()

    # Get registry
    registry = read_registry()
    if not registry:
        console.print('[red]Error: No worktree registry found[/red]')
        return 1

    # Find missing worktrees
    missing = []
    for entry in registry.worktrees:
        if not Path(entry.path).exists():
            missing.append(entry)

    if not missing:
        console.print('[green]No missing worktrees found. Registry is clean.[/green]')
        return 0

    # Show what will be removed
    console.print(f'Found [yellow]{len(missing)}[/yellow] worktree(s) that no longer exist on disk:')
    console.print()

    table = Table(show_header=True)
    table.add_column('#', style='dim')
    table.add_column('Feature')
    table.add_column('Path')
    table.add_column('Ports')

    for entry in missing:
        table.add_row(
            str(entry.index),
            entry.feature_name,
            entry.path,
            f'web:{entry.ports.web} db:{entry.ports.db}',
        )

    console.print(table)
    console.print()

    # Confirm
    if should_prompt():
        response = input(f'Remove {len(missing)} entries from registry? (y/N): ')
        if response.lower() != 'y':
            console.print('Cancelled.')
            return 0

    # Remove from registry
    with locked_registry() as reg:
        removed = 0
        for entry in missing:
            # Find and remove by path
            reg.worktrees = [wt for wt in reg.worktrees if wt.path != entry.path]
            removed += 1

    console.print()
    console.print(f'[green]Removed {removed} entries from registry.[/green]')
    return 0
