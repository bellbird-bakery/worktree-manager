"""
Docker operations for worktree management.

Handles Docker Compose operations and resource cleanup.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class DockerError(Exception):
    """Error from Docker operations."""

    pass


@dataclass
class DockerContainer:
    """Represents a Docker container."""

    name: str
    status: str
    project_name: str | None = None


@dataclass
class DockerVolume:
    """Represents a Docker volume."""

    name: str
    project_name: str | None = None


@dataclass
class DockerNetwork:
    """Represents a Docker network."""

    name: str
    project_name: str | None = None


def is_docker_running() -> bool:
    """Check if Docker daemon is running."""
    try:
        result = subprocess.run(
            ['docker', 'info'],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def get_compose_project_name(worktree_path: str) -> str:
    """
    Get the Docker Compose project name for a worktree.

    This matches the naming convention used in docker-compose.local.yml.
    """
    dirname = Path(worktree_path).name
    # Docker Compose normalizes names to lowercase and removes special chars
    return dirname.lower().replace(' ', '-').replace('_', '-')


def compose_up(
    worktree_path: str,
    project_name: str,
    detach: bool = True,
    stream_output: bool = False,
    on_output: callable = None,
) -> None:
    """
    Start Docker Compose services for a worktree.

    Args:
        worktree_path: Path to the worktree.
        project_name: Docker Compose project name.
        detach: Run in detached mode.
        stream_output: If True, stream output line-by-line instead of buffering.
        on_output: Callback function for each line of output (only used with stream_output).
    """
    cmd = ['docker', 'compose', '-f', 'docker-compose.local.yml']
    cmd.extend(['up', '--build'])
    if detach:
        cmd.append('-d')

    env = os.environ.copy()
    env['COMPOSE_PROJECT_NAME'] = project_name

    try:
        if stream_output:
            # Stream output in real-time
            process = subprocess.Popen(
                cmd,
                cwd=worktree_path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # Line buffered
            )

            for line in iter(process.stdout.readline, ''):
                if on_output:
                    on_output(line.rstrip())
                else:
                    print(line, end='')

            process.wait()
            if process.returncode != 0:
                raise DockerError(f'Docker Compose failed with exit code {process.returncode}')
        else:
            # Original behavior - buffer output
            subprocess.run(
                cmd,
                check=True,
                cwd=worktree_path,
                env=env,
            )
    except subprocess.CalledProcessError as e:
        raise DockerError(f'Failed to start containers: {e}') from e


def compose_down(worktree_path: str, project_name: str, volumes: bool = False) -> None:
    """
    Stop Docker Compose services for a worktree.

    Args:
        worktree_path: Path to the worktree.
        project_name: Docker Compose project name.
        volumes: Also remove volumes.
    """
    cmd = ['docker', 'compose', '-f', 'docker-compose.local.yml']
    cmd.append('down')
    if volumes:
        cmd.append('-v')

    env = os.environ.copy()
    env['COMPOSE_PROJECT_NAME'] = project_name

    try:
        subprocess.run(
            cmd,
            check=True,
            cwd=worktree_path,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        raise DockerError(f'Failed to stop containers: {e}') from e


def fix_permissions(worktree_path: str, project_name: str) -> bool:
    """
    Fix file permissions in worktree by running chown inside the container.

    Docker containers often run as root, creating files owned by root.
    This runs chown inside the web container to fix ownership before closing.

    Args:
        worktree_path: Path to the worktree.
        project_name: Docker Compose project name.

    Returns:
        True if successful, False otherwise.
    """
    import pwd

    # Get current user's UID and GID
    uid = os.getuid()
    gid = os.getgid()

    cmd = [
        'docker',
        'compose',
        '-f',
        'docker-compose.local.yml',
        'exec',
        '-T',
        'web',
        'chown',
        '-R',
        f'{uid}:{gid}',
        '/app',
    ]

    env = os.environ.copy()
    env['COMPOSE_PROJECT_NAME'] = project_name

    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=worktree_path,
            env=env,
            timeout=60,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def compose_ps(worktree_path: str, project_name: str) -> list[DockerContainer]:
    """
    List running containers for a worktree.

    Args:
        worktree_path: Path to the worktree.
        project_name: Docker Compose project name.

    Returns:
        List of running containers.
    """
    cmd = ['docker', 'compose', '-f', 'docker-compose.local.yml', 'ps', '--format', 'json']

    env = os.environ.copy()
    env['COMPOSE_PROJECT_NAME'] = project_name

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=worktree_path,
            env=env,
        )
        if result.returncode != 0:
            return []

        import json

        containers = []
        # Output is newline-delimited JSON
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            try:
                data = json.loads(line)
                containers.append(
                    DockerContainer(
                        name=data.get('Name', ''),
                        status=data.get('State', 'unknown'),
                        project_name=project_name,
                    )
                )
            except json.JSONDecodeError:
                continue

        return containers
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def list_project_volumes(project_name: str | None = None) -> list[DockerVolume]:
    """
    List Docker volumes for the project.

    Args:
        project_name: Project name to filter by. If None, auto-detects from config.

    Returns:
        List of matching Docker volumes.
    """
    if project_name is None:
        from .config import get_project_config

        project_name = get_project_config().project_name

    try:
        result = subprocess.run(
            ['docker', 'volume', 'ls', '--format', '{{.Name}}'],
            capture_output=True,
            text=True,
            check=True,
        )
        volumes = []
        for name in result.stdout.strip().split('\n'):
            if not name:
                continue
            if project_name.lower() in name.lower():
                # Extract project name from volume name
                # Format: project-name_volume-suffix
                parts = name.split('_')
                extracted_project = parts[0] if parts else None
                volumes.append(DockerVolume(name=name, project_name=extracted_project))
        return volumes
    except subprocess.CalledProcessError:
        return []


def list_project_containers(project_name: str | None = None) -> list[DockerContainer]:
    """
    List Docker containers for the project.

    Args:
        project_name: Project name to filter by. If None, auto-detects from config.

    Returns:
        List of matching Docker containers.
    """
    if project_name is None:
        from .config import get_project_config

        project_name = get_project_config().project_name

    try:
        result = subprocess.run(
            ['docker', 'ps', '-a', '--format', '{{.Names}}\t{{.Status}}'],
            capture_output=True,
            text=True,
            check=True,
        )
        containers = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split('\t')
            name = parts[0]
            status = parts[1] if len(parts) > 1 else 'unknown'

            if project_name.lower() in name.lower():
                # Extract project name from container name
                # Format: project-name-service-1
                extracted_project = None
                for suffix in ['-web-', '-db-', '-redis-', '-celery-', '-worker-', '-beat-']:
                    if suffix in name:
                        extracted_project = name.split(suffix)[0]
                        break
                containers.append(DockerContainer(name=name, status=status, project_name=extracted_project))
        return containers
    except subprocess.CalledProcessError:
        return []


def list_project_networks(project_name: str | None = None) -> list[DockerNetwork]:
    """
    List Docker networks for the project.

    Args:
        project_name: Project name to filter by. If None, auto-detects from config.

    Returns:
        List of matching Docker networks.
    """
    if project_name is None:
        from .config import get_project_config

        project_name = get_project_config().project_name

    try:
        result = subprocess.run(
            ['docker', 'network', 'ls', '--format', '{{.Name}}'],
            capture_output=True,
            text=True,
            check=True,
        )
        networks = []
        for name in result.stdout.strip().split('\n'):
            if not name:
                continue
            if project_name.lower() in name.lower():
                # Extract project name from network name
                # Format: project-name_network-suffix
                parts = name.split('_')
                extracted_project = parts[0] if len(parts) > 1 else None
                networks.append(DockerNetwork(name=name, project_name=extracted_project))
        return networks
    except subprocess.CalledProcessError:
        return []


# Legacy aliases for backwards compatibility
def list_dispatch_guru_volumes() -> list[DockerVolume]:
    """Legacy alias for list_project_volumes. Use list_project_volumes instead."""
    return list_project_volumes('dispatch-guru')


def list_dispatch_guru_containers() -> list[DockerContainer]:
    """Legacy alias for list_project_containers. Use list_project_containers instead."""
    return list_project_containers('dispatch-guru')


def list_dispatch_guru_networks() -> list[DockerNetwork]:
    """Legacy alias for list_project_networks. Use list_project_networks instead."""
    return list_project_networks('dispatch-guru')


def remove_container(name: str, force: bool = True) -> bool:
    """Remove a Docker container."""
    cmd = ['docker', 'rm']
    if force:
        cmd.append('-f')
    cmd.append(name)

    try:
        subprocess.run(cmd, capture_output=True, check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def remove_volume(name: str) -> bool:
    """Remove a Docker volume."""
    try:
        subprocess.run(['docker', 'volume', 'rm', name], capture_output=True, check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def remove_network(name: str) -> bool:
    """Remove a Docker network."""
    try:
        subprocess.run(['docker', 'network', 'rm', name], capture_output=True, check=True)
        return True
    except subprocess.CalledProcessError:
        return False
