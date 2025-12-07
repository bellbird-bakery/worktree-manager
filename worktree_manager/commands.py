"""
Command implementations for worktree manager.

This module provides the main command handlers used by the CLI.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from rich.console import Console

logger = logging.getLogger('worktree_manager')
from rich.panel import Panel
from rich.table import Table

from .docker_ops import (
    DockerError,
    compose_down,
    compose_ps,
    fix_permissions,
    is_docker_running,
    list_dispatch_guru_containers,
    list_dispatch_guru_networks,
    list_dispatch_guru_volumes,
    remove_container,
    remove_network,
    remove_volume,
)
from .git_ops import (
    GitError,
    commit_all,
    create_worktree,
    get_current_branch,
    get_main_repo_root,
    get_repo_root,
    has_uncommitted_changes,
    is_worktree,
    list_worktrees,
    remove_worktree,
    validate_feature_name,
)
from .ports import calculate_ports_for_index, validate_ports
from .registry import WorktreeEntry, locked_registry, read_registry
from .validator import validate_worktree

console = Console()


def create_worktree_cmd(feature_name: str) -> int:
    """
    Create a new worktree with automatic port assignment.

    Args:
        feature_name: Name for the feature branch.

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
        console.print(f'Ports: WEB=[cyan]{ports.web}[/cyan], DB=[cyan]{ports.db}[/cyan]')
        console.print()

        # Check for port conflicts
        conflicts = validate_ports(ports.web, ports.db)
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
            worktree_path, branch_name = create_worktree(feature_name, repo_path=str(main_repo))
            console.print(f'[green]Created: {worktree_path}[/green]')
        except GitError as e:
            console.print(f'[red]Error creating worktree: {e}[/red]')
            return 1

        # Create .env file
        console.print('Configuring environment...')
        env_template = worktree_path / '.env.local'
        env_file = worktree_path / '.env'

        if env_template.exists():
            # Copy template
            shutil.copy(env_template, env_file)

            # Update ports in .env
            with open(env_file) as f:
                content = f.read()

            # Replace or add port settings
            import re

            if 'WEB_PORT=' in content:
                content = re.sub(r'WEB_PORT=\d+', f'WEB_PORT={ports.web}', content)
            else:
                content += f'\nWEB_PORT={ports.web}\n'

            if 'DB_PORT=' in content:
                content = re.sub(r'DB_PORT=\d+', f'DB_PORT={ports.db}', content)
            else:
                content += f'DB_PORT={ports.db}\n'

            with open(env_file, 'w') as f:
                f.write(content)

            console.print(f'[green]Created .env with ports WEB={ports.web}, DB={ports.db}[/green]')
        else:
            console.print('[yellow]Warning: .env.local not found. Create .env manually.[/yellow]')

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

    # Create task for the worktree
    console.print('Creating task...')
    create_task_for_worktree(feature_name, str(worktree_path))
    console.print('[green]Task created in Kanban board[/green]')

    console.print()
    console.print(Panel.fit('[bold green]Worktree Created Successfully![/bold green]', border_style='green'))
    console.print()
    console.print('Next steps:')
    console.print(f'  1. cd [cyan]{worktree_path}[/cyan]')
    console.print('  2. [cyan]just build && just up[/cyan]')
    console.print()
    console.print('View tasks: [cyan]worktree-manager web[/cyan]')
    console.print()

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


def close_worktree(message: str) -> int:
    """
    Close the current worktree.

    This will:
    1. Commit any uncommitted changes
    2. Stop Docker containers
    3. Remove the git worktree
    4. Remove from registry

    Args:
        message: Commit message for any uncommitted changes.

    Returns:
        Exit code (0 for success).
    """
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

    console.print(f'Path: [cyan]{current_path}[/cyan]')
    console.print(f'Branch: [cyan]{get_current_branch()}[/cyan]')
    console.print()

    # Confirm
    console.print('[yellow]This will:[/yellow]')
    console.print('  - Commit any uncommitted changes')
    console.print('  - Stop Docker containers')
    console.print('  - Remove the worktree directory')
    console.print('  - Remove from registry')
    console.print()

    if not console.input('Continue? (yes/no): ').lower() == 'yes':
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
            compose_down(current_path, entry.compose_project_name, volumes=False)
            console.print('[green]Containers stopped[/green]')
        except DockerError as e:
            console.print(f'[yellow]Warning: {e}[/yellow]')

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

    # Step 6: Mark task as done
    if entry:
        console.print('Marking task as done...')
        complete_task_for_worktree(entry.feature_name)
        console.print('[green]Task marked as done[/green]')

    console.print()
    console.print(Panel.fit('[bold green]Worktree Closed Successfully![/bold green]', border_style='green'))
    console.print()
    console.print(f'You are now in: [cyan]{main_repo}[/cyan]')
    console.print()

    return 0


def clone_database(source_index: int) -> int:
    """
    Clone database from another worktree.

    Args:
        source_index: Index of the source worktree.

    Returns:
        Exit code (0 for success).
    """
    console.print()
    console.print(Panel.fit('[bold]Database Cloning[/bold]', border_style='blue'))
    console.print()

    try:
        repo_root = get_repo_root()
    except GitError:
        console.print('[red]Error: Not in a git repository[/red]')
        return 1

    current_path = str(repo_root)

    # Get registry
    registry = read_registry()
    if not registry:
        console.print('[red]Error: No worktree registry found[/red]')
        return 1

    # Find source worktree
    source = registry.find_by_index(source_index)
    if not source:
        console.print(f'[red]Error: No worktree found with index {source_index}[/red]')
        console.print()
        console.print('Available worktrees:')
        for wt in registry.worktrees:
            console.print(f'  [{wt.index}] {wt.feature_name} - {wt.path}')
        return 1

    # Find current worktree
    target = registry.find_by_path(current_path)
    if not target:
        console.print('[red]Error: Current directory is not a registered worktree[/red]')
        return 1

    if source.path == current_path:
        console.print('[red]Error: Cannot clone from self[/red]')
        return 1

    console.print(f'Source: [cyan]{source.feature_name}[/cyan] (index {source.index})')
    console.print(f'Target: [cyan]{target.feature_name}[/cyan] (index {target.index})')
    console.print()

    # Use the bash script for now (it has rollback logic)
    script_path = Path(__file__).parent.parent / 'scripts' / 'worktree-db-clone.sh'
    if not script_path.exists():
        # Try from main repo
        script_path = get_main_repo_root() / 'scripts' / 'worktree-db-clone.sh'

    if script_path.exists():
        try:
            result = subprocess.run(
                [str(script_path), source.path, target.path],
                check=True,
            )
            return result.returncode
        except subprocess.CalledProcessError as e:
            return e.returncode
    else:
        console.print('[red]Error: Database clone script not found[/red]')
        return 1


def cleanup_orphans() -> int:
    """
    Clean up orphaned Docker resources and registry entries.

    Returns:
        Exit code (0 for success).
    """
    console.print()
    console.print(Panel.fit('[bold]Orphan Cleanup[/bold]', border_style='yellow'))
    console.print()

    registry = read_registry()
    if not registry:
        console.print('[yellow]No registry found. Nothing to clean up.[/yellow]')
        return 0

    registered_projects = {wt.compose_project_name for wt in registry.worktrees}

    # Find orphaned resources
    orphaned_volumes = []
    orphaned_containers = []
    orphaned_networks = []
    orphaned_entries = []

    console.print('Scanning for orphaned resources...')
    console.print()

    # Check volumes
    for volume in list_dispatch_guru_volumes():
        if volume.project_name and volume.project_name not in registered_projects:
            orphaned_volumes.append(volume)

    # Check containers
    for container in list_dispatch_guru_containers():
        if container.project_name and container.project_name not in registered_projects:
            orphaned_containers.append(container)

    # Check networks
    for network in list_dispatch_guru_networks():
        if network.project_name and network.project_name not in registered_projects:
            orphaned_networks.append(network)

    # Check registry entries (paths that don't exist)
    git_worktrees = {wt.path for wt in list_worktrees(registry.main_repo_path)}
    for entry in registry.worktrees:
        if entry.path not in git_worktrees:
            orphaned_entries.append(entry)

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

    # Confirm
    console.print('[red]WARNING: This will permanently delete these resources![/red]')
    console.print()
    if not console.input('Continue with cleanup? (yes/no): ').lower() == 'yes':
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


def start_web(host: str = '127.0.0.1', port: int = 8000) -> int:
    """
    Start the Kanban web interface.

    Args:
        host: Host to bind to.
        port: Port to run on.

    Returns:
        Exit code (0 for success).
    """
    console.print()
    console.print(Panel.fit('[bold]Worktree Task Tracker[/bold]', border_style='blue'))
    console.print()

    # Initialize database
    console.print('Initializing database...')
    try:
        from .task_store import ensure_db

        ensure_db()
        console.print('[green]Database ready[/green]')
    except Exception as e:
        console.print(f'[red]Error initializing database: {e}[/red]')
        return 1

    # Start uvicorn with Starlette app
    console.print()
    console.print(f'Starting server at [cyan]http://{host}:{port}[/cyan]')
    console.print('Press [cyan]Ctrl+C[/cyan] to stop')
    console.print()

    try:
        import uvicorn

        uvicorn.run(
            'worktree_manager.web:create_app',
            host=host,
            port=port,
            reload=False,
            factory=True,
            log_level='info',
        )
        return 0
    except ImportError:
        console.print('[red]Error: uvicorn not installed[/red]')
        console.print()
        console.print('Install with:')
        console.print('  [cyan]uv add uvicorn[/cyan]')
        return 1
    except KeyboardInterrupt:
        console.print()
        console.print('[yellow]Server stopped[/yellow]')
        return 0


def sync_tasks_cmd() -> int:
    """
    Synchronize tasks with the worktree registry.

    Returns:
        Exit code (0 for success).
    """
    console.print()
    console.print(Panel.fit('[bold]Syncing Tasks[/bold]', border_style='blue'))
    console.print()

    # Ensure database exists
    from .task_store import TaskStatus, ensure_db, save_task, tasks

    ensure_db()

    # Get registry
    registry = read_registry()
    if not registry:
        console.print('[yellow]No worktree registry found.[/yellow]')
        return 0

    console.print(f'Found [cyan]{len(registry.worktrees)}[/cyan] registered worktrees')
    console.print()

    created_count = 0
    updated_count = 0

    for worktree in registry.worktrees:
        task, created = tasks.get_or_create(
            feature_name=worktree.feature_name,
            defaults={
                'title': worktree.feature_name.replace('-', ' ').replace('_', ' ').title(),
                'worktree_path': worktree.path,
                'status': TaskStatus.TODO.value,
            },
        )

        if created:
            console.print(f'  [green]Created:[/green] {task.title}')
            created_count += 1
        elif task.worktree_path != worktree.path:
            task.worktree_path = worktree.path
            save_task(task)
            console.print(f'  [yellow]Updated:[/yellow] {task.title}')
            updated_count += 1
        else:
            console.print(f'  [dim]Exists:[/dim] {task.title}')

    console.print()
    console.print(Panel.fit(
        f'[bold green]Sync Complete![/bold green]\n\n'
        f'Created: {created_count}\n'
        f'Updated: {updated_count}',
        border_style='green',
    ))
    console.print()

    return 0


def create_task_for_worktree(feature_name: str, worktree_path: str) -> None:
    """
    Create a task for a new worktree.

    Args:
        feature_name: Name of the feature branch.
        worktree_path: Path to the worktree directory.
    """
    try:
        from .task_store import TaskStatus, ensure_db, tasks

        ensure_db()

        tasks.get_or_create(
            feature_name=feature_name,
            defaults={
                'title': feature_name.replace('-', ' ').replace('_', ' ').title(),
                'worktree_path': worktree_path,
                'status': TaskStatus.TODO.value,
            },
        )

        # Sync to JSON for git sync
        try:
            from .sync import sync_sqlite_to_json

            sync_sqlite_to_json()
        except Exception as e:
            logger.warning(f'JSON sync failed: {e}')
    except Exception as e:
        logger.warning(f'Task creation failed for {feature_name}: {e}')
        console.print(f'[yellow]Warning: Task creation failed: {e}[/yellow]')


def complete_task_for_worktree(feature_name: str) -> None:
    """
    Mark a task as done when closing a worktree.

    Args:
        feature_name: Name of the feature branch.
    """
    try:
        from .task_store import TaskStatus, ensure_db, save_task, tasks

        ensure_db()

        task = tasks.get_by_feature(feature_name)
        if task:
            task.status = TaskStatus.DONE.value
            save_task(task)

        # Sync to JSON for git sync
        try:
            from .sync import sync_sqlite_to_json

            sync_sqlite_to_json()
        except Exception as e:
            logger.warning(f'JSON sync failed: {e}')
    except Exception as e:
        logger.warning(f'Task completion failed for {feature_name}: {e}')
        console.print(f'[yellow]Warning: Task completion failed: {e}[/yellow]')


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
    show: bool = False,
    add_ignore: str | None = None,
    remove_ignore: str | None = None,
) -> int:
    """
    Show or modify configuration.

    Args:
        show: Display current configuration.
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


def sync_cmd(pull_only: bool = False, push_only: bool = False, auto_commit: bool = False) -> int:
    """
    Synchronize tasks between JSON file and SQLite database.

    Args:
        pull_only: Only sync from JSON to SQLite.
        push_only: Only sync from SQLite to JSON.
        auto_commit: Automatically commit changes to git.

    Returns:
        Exit code (0 for success).
    """
    console.print()
    console.print(Panel.fit('[bold]Task Sync[/bold]', border_style='blue'))
    console.print()

    # Check if we're in a git repo
    from .task_store import ensure_db, find_repo_root, get_task_file

    repo_root = find_repo_root()
    if not repo_root:
        console.print('[red]Error: Not in a git repository[/red]')
        return 1

    task_file = get_task_file(repo_root)
    console.print(f'Task file: [cyan]{task_file}[/cyan]')
    console.print()

    # Ensure database exists
    ensure_db()

    # Perform sync
    from .sync import auto_commit_tasks, full_sync, sync_json_to_sqlite, sync_sqlite_to_json

    if pull_only:
        console.print('Syncing JSON → SQLite...')
        result = sync_json_to_sqlite(repo_root)
        console.print(f'  Created: [green]{result.created}[/green]')
        console.print(f'  Updated: [yellow]{result.updated}[/yellow]')
        console.print(f'  Unchanged: [dim]{result.unchanged}[/dim]')
        if result.errors:
            for error in result.errors:
                console.print(f'  [red]Error: {error}[/red]')

    elif push_only:
        console.print('Syncing SQLite → JSON...')
        result = sync_sqlite_to_json(repo_root)
        console.print(f'  Created: [green]{result.created}[/green]')
        console.print(f'  Updated: [yellow]{result.updated}[/yellow]')
        console.print(f'  Unchanged: [dim]{result.unchanged}[/dim]')
        if result.errors:
            for error in result.errors:
                console.print(f'  [red]Error: {error}[/red]')

    else:
        console.print('Performing bidirectional sync...')
        console.print()
        stats = full_sync(repo_root)

        console.print('JSON → SQLite:')
        console.print(f'  Created: [green]{stats["json_to_sqlite"]["created"]}[/green]')
        console.print(f'  Updated: [yellow]{stats["json_to_sqlite"]["updated"]}[/yellow]')
        console.print()
        console.print('SQLite → JSON:')
        console.print(f'  Created: [green]{stats["sqlite_to_json"]["created"]}[/green]')
        console.print(f'  Updated: [yellow]{stats["sqlite_to_json"]["updated"]}[/yellow]')

        if stats.get('errors'):
            console.print()
            for error in stats['errors']:
                console.print(f'[red]Error: {error}[/red]')

    # Auto-commit if requested
    if auto_commit:
        console.print()
        console.print('Committing changes...')
        if auto_commit_tasks('Update task status via worktree-manager sync', repo_root):
            console.print('[green]Changes committed[/green]')
        else:
            console.print('[dim]No changes to commit[/dim]')

    console.print()
    console.print(Panel.fit('[bold green]Sync Complete![/bold green]', border_style='green'))
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
