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


def create_worktree_cmd(feature_name: str, branch_type: str = 'feature') -> int:
    """
    Create a new worktree with automatic port assignment.

    Args:
        feature_name: Name for the feature branch.
        branch_type: Branch prefix type ('feature' or 'fix').

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
            worktree_path, branch_name = create_worktree(
                feature_name, repo_path=str(main_repo), branch_type=branch_type
            )
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

            # Add UID/GID for non-root Docker containers
            uid = os.getuid()
            gid = os.getgid()
            if 'UID=' not in content:
                content += f'UID={uid}\n'
            if 'GID=' not in content:
                content += f'GID={gid}\n'

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
    console.print(
        Panel.fit(
            f'[bold green]Sync Complete![/bold green]\n\nCreated: {created_count}\nUpdated: {updated_count}',
            border_style='green',
        )
    )
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


# Path to production restore script
PROD_RESTORE_SCRIPT = Path('/home/jeremy/projects/dispatch-guru/database_tools/update_local_restore.sh')


def load_db_cmd(
    feature_name: str | None = None,
    skip_dump: bool = False,
    skip_backup: bool = False,
) -> int:
    """
    Load production database into a worktree.

    Args:
        feature_name: Target worktree feature name. If None, uses current directory.
        skip_dump: Use cached production dump instead of fetching fresh.
        skip_backup: Skip backing up current local database.

    Returns:
        Exit code (0 for success).
    """
    from .cli import should_prompt

    console.print()
    console.print(Panel.fit('[bold]Load Database[/bold]', border_style='blue'))
    console.print()

    # Get registry
    registry = read_registry()
    if not registry:
        console.print('[red]Error: No worktree registry found[/red]')
        return 1

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

    console.print(f'Target: [cyan]{target.feature_name}[/cyan] (port {target.ports.db})')
    console.print(f'Path: [dim]{target.path}[/dim]')
    console.print()

    # Check prerequisites
    if not is_docker_running():
        console.print('[red]Error: Docker is not running[/red]')
        return 1

    if not PROD_RESTORE_SCRIPT.exists():
        console.print(f'[red]Error: Restore script not found at {PROD_RESTORE_SCRIPT}[/red]')
        return 1

    # Build flags
    flags = []
    if skip_dump:
        flags.append('--skip-dump')
        console.print('[dim]Using cached production dump[/dim]')
    if skip_backup:
        flags.append('--skip-local-backup')
        console.print('[dim]Skipping local database backup[/dim]')

    # Confirm
    if should_prompt():
        console.print()
        console.print('[yellow]Warning: This will DROP the target database and replace it with production data![/yellow]')
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
    env['LOCAL_DB_PORT'] = str(target.ports.db)

    # Run script
    try:
        result = subprocess.run(
            [str(PROD_RESTORE_SCRIPT)] + flags,
            env=env,
            cwd=PROD_RESTORE_SCRIPT.parent,
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
        if reg is None:
            console.print('[red]Error: Could not lock registry[/red]')
            return 1

        removed = 0
        for entry in missing:
            # Find and remove by path
            reg.worktrees = [wt for wt in reg.worktrees if wt.path != entry.path]
            removed += 1

    console.print()
    console.print(f'[green]Removed {removed} entries from registry.[/green]')
    return 0
