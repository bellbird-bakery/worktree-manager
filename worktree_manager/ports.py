"""
Port management for worktrees.

Handles automatic port assignment and conflict detection.

Port allocation is performed atomically inside the registry lock to prevent
race conditions when multiple processes try to allocate ports simultaneously.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import time
from dataclasses import dataclass

from . import BASE_DB_PORT, BASE_REDIS_PORT, BASE_WEB_PORT
from .registry import (
    PORT_RETRY_ATTEMPTS,
    PORT_RETRY_DELAY,
    Registry,
    WorktreePorts,
    locked_registry,
    read_registry,
)

logger = logging.getLogger('worktree_manager')


@dataclass
class PortConflict:
    """Information about a port conflict."""

    port: int
    port_type: str  # 'web', 'db', or 'redis'
    process_name: str | None = None
    process_pid: int | None = None


def is_port_in_use(port: int) -> tuple[bool, str | None, int | None]:
    """
    Check if a port is currently in use.

    Returns:
        Tuple of (is_in_use, process_name, process_pid)
    """
    # First try socket check (quick)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        result = s.connect_ex(('127.0.0.1', port))
        if result != 0:
            return (False, None, None)

    # Port is in use, try to get process info
    process_name = None
    process_pid = None

    # Try lsof
    try:
        result = subprocess.run(
            ['lsof', '-i', f':{port}', '-sTCP:LISTEN', '-t'],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            process_pid = int(result.stdout.strip().split('\n')[0])
            # Get process name
            ps_result = subprocess.run(['ps', '-p', str(process_pid), '-o', 'comm='], capture_output=True, text=True)
            if ps_result.returncode == 0:
                process_name = ps_result.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        # lsof not available or error, try ss
        try:
            result = subprocess.run(['ss', '-tlnp', f'sport = :{port}'], capture_output=True, text=True)
            if result.returncode == 0 and len(result.stdout.strip().split('\n')) > 1:
                # Parse ss output
                pass  # ss output is harder to parse, skip for now
        except (FileNotFoundError, subprocess.SubprocessError):
            pass

    return (True, process_name, process_pid)


def check_port_conflicts(web_port: int, db_port: int | None, redis_port: int | None = None) -> list[PortConflict]:
    """
    Check for port conflicts with the given ports.

    Pass ``db_port=None`` to skip the database-port check entirely. For projects
    on a shared dev-database server the per-worktree DB port is vestigial and its
    "conflict" would be a false positive against the shared server (see
    HANDOVER-dispatch-guru-shared-db.md, item 2).

    Returns:
        List of PortConflict objects for any conflicts found.
    """
    conflicts = []

    # Check web port
    in_use, proc_name, proc_pid = is_port_in_use(web_port)
    if in_use:
        conflicts.append(PortConflict(port=web_port, port_type='web', process_name=proc_name, process_pid=proc_pid))

    # Check db port
    if db_port is not None:
        in_use, proc_name, proc_pid = is_port_in_use(db_port)
        if in_use:
            conflicts.append(PortConflict(port=db_port, port_type='db', process_name=proc_name, process_pid=proc_pid))

    # Check redis port
    if redis_port is not None:
        in_use, proc_name, proc_pid = is_port_in_use(redis_port)
        if in_use:
            conflicts.append(
                PortConflict(port=redis_port, port_type='redis', process_name=proc_name, process_pid=proc_pid)
            )

    return conflicts


def check_registry_conflicts(
    web_port: int, db_port: int | None, redis_port: int | None = None, exclude_path: str | None = None
) -> list[PortConflict]:
    """
    Check if ports conflict with other registered worktrees.

    Args:
        web_port: Web port to check
        db_port: Database port to check
        redis_port: Redis port to check (None to skip)
        exclude_path: Optional path to exclude from conflict check (for current worktree)

    Returns:
        List of PortConflict objects for any conflicts found.
    """
    conflicts = []
    registry = read_registry()

    if not registry:
        return conflicts

    for wt in registry.worktrees:
        if exclude_path and wt.path == exclude_path:
            continue

        if wt.ports.web == web_port:
            conflicts.append(
                PortConflict(
                    port=web_port,
                    port_type='web',
                    process_name=f'worktree:{wt.feature_name}',
                )
            )

        # db_port is None for shared-DB projects (per-worktree DB port is vestigial).
        if db_port is not None and wt.ports.db == db_port:
            conflicts.append(
                PortConflict(
                    port=db_port,
                    port_type='db',
                    process_name=f'worktree:{wt.feature_name}',
                )
            )

        # wt.ports.redis is 0 for legacy entries created before REDIS_PORT allocation; skip those.
        if redis_port is not None and wt.ports.redis and wt.ports.redis == redis_port:
            conflicts.append(
                PortConflict(
                    port=redis_port,
                    port_type='redis',
                    process_name=f'worktree:{wt.feature_name}',
                )
            )

    return conflicts


def calculate_ports_for_index(index: int) -> WorktreePorts:
    """
    Calculate ports for a given worktree index.

    Index 0 is reserved for the main worktree (uses default ports).
    Index 1+ get offset ports.
    """
    if index == 0:
        return WorktreePorts(web=BASE_WEB_PORT, db=BASE_DB_PORT, redis=BASE_REDIS_PORT)

    return WorktreePorts(
        web=BASE_WEB_PORT + index,
        db=BASE_DB_PORT + index,
        redis=BASE_REDIS_PORT + index,
    )


def get_available_ports(registry: Registry) -> WorktreePorts:
    """
    Get the next available ports based on the registry.

    This finds the next available index and calculates ports for it.

    WARNING: This function does NOT hold the registry lock. For atomic
    port allocation, use allocate_ports_atomic() instead.
    """
    next_index = registry.get_next_index()
    return calculate_ports_for_index(next_index)


def validate_ports(
    web_port: int, db_port: int | None, redis_port: int | None = None, exclude_path: str | None = None
) -> list[PortConflict]:
    """
    Validate that ports are available (not in use and not conflicting with registry).

    Args:
        web_port: Web port to validate
        db_port: Database port to validate (None to skip, e.g. shared-DB projects)
        redis_port: Redis port to validate (None to skip)
        exclude_path: Optional path to exclude from registry conflict check

    Returns:
        Combined list of all conflicts found.
    """
    conflicts = []
    conflicts.extend(check_port_conflicts(web_port, db_port, redis_port))
    conflicts.extend(check_registry_conflicts(web_port, db_port, redis_port, exclude_path))
    return conflicts


class PortAllocationError(Exception):
    """Failed to allocate ports."""

    pass


def allocate_ports_atomic(
    main_repo_path: str,
    max_retries: int = PORT_RETRY_ATTEMPTS,
) -> tuple[WorktreePorts, int]:
    """
    Atomically allocate ports for a new worktree.

    This function acquires the registry lock BEFORE checking port availability,
    preventing race conditions where two processes might allocate the same ports.

    Args:
        main_repo_path: Path to the main repository.
        max_retries: Maximum number of retry attempts if ports are in use.

    Returns:
        Tuple of (allocated ports, worktree index).

    Raises:
        PortAllocationError: If ports cannot be allocated after max retries.
    """
    last_error = None

    for attempt in range(max_retries):
        try:
            with locked_registry(main_repo_path) as registry:
                # Get next available index
                next_index = registry.get_next_index()
                ports = calculate_ports_for_index(next_index)

                # Check if ports are actually available (inside lock!)
                conflicts = check_port_conflicts(ports.web, ports.db, ports.redis)

                if conflicts:
                    # Ports in use by system - try next index
                    logger.warning(f'Ports {ports.web}/{ports.db}/{ports.redis} in use, trying next index')
                    # Find an index with free ports
                    for offset in range(1, 100):
                        test_index = next_index + offset
                        test_ports = calculate_ports_for_index(test_index)
                        test_conflicts = check_port_conflicts(test_ports.web, test_ports.db, test_ports.redis)
                        if not test_conflicts:
                            ports = test_ports
                            next_index = test_index
                            break
                    else:
                        raise PortAllocationError('Could not find available ports in range')

                # Double-check registry conflicts (should not happen if locking works)
                registry_conflicts = check_registry_conflicts(ports.web, ports.db, ports.redis, exclude_path=None)
                if registry_conflicts:
                    # This shouldn't happen if locking is working correctly
                    logger.error(f'Registry conflict detected despite holding lock: {registry_conflicts}')
                    raise PortAllocationError(f'Registry conflict for ports {ports.web}/{ports.db}/{ports.redis}')

                # Ports are available - allocation successful
                # Note: The actual worktree entry is added by the caller
                logger.info(
                    f'Allocated ports web={ports.web}, db={ports.db}, redis={ports.redis} at index {next_index}'
                )
                return ports, next_index

        except PortAllocationError as e:
            last_error = e
            if attempt < max_retries - 1:
                logger.warning(f'Port allocation attempt {attempt + 1} failed, retrying...')
                time.sleep(PORT_RETRY_DELAY * (attempt + 1))
            continue

    raise PortAllocationError(f'Failed to allocate ports after {max_retries} attempts: {last_error}')


def find_available_ports_in_range(
    start_index: int,
    max_range: int = 100,
) -> tuple[WorktreePorts, int] | None:
    """
    Find available ports starting from a given index.

    This is a helper function that does NOT hold any locks.
    For atomic allocation, use allocate_ports_atomic() instead.

    Args:
        start_index: Starting worktree index to try.
        max_range: Maximum number of indices to try.

    Returns:
        Tuple of (ports, index) if found, None otherwise.
    """
    for offset in range(max_range):
        index = start_index + offset
        ports = calculate_ports_for_index(index)

        # Check system port usage
        system_conflicts = check_port_conflicts(ports.web, ports.db, ports.redis)
        if system_conflicts:
            continue

        # Check registry conflicts
        registry_conflicts = check_registry_conflicts(ports.web, ports.db, ports.redis)
        if registry_conflicts:
            continue

        return ports, index

    return None
