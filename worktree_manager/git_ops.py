"""
Git operations for worktree management.

Handles git worktree creation, deletion, and listing.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Directory (and Docker Compose project) name prefix for managed worktrees
WORKTREE_PREFIX = 'wt-'


class GitError(Exception):
    """Error from git operations."""

    pass


@dataclass
class GitWorktree:
    """Represents a git worktree."""

    path: str
    branch: str
    commit: str
    is_bare: bool = False
    is_detached: bool = False


def get_repo_root(path: str | None = None) -> Path:
    """Get the root directory of the git repository."""
    cmd = ['git', 'rev-parse', '--show-toplevel']
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=path,
        )
        return Path(result.stdout.strip())
    except subprocess.CalledProcessError as e:
        raise GitError(f'Not a git repository: {e.stderr}') from e


def get_main_repo_root() -> Path:
    """
    Get the root of the main repository (not the worktree).

    For worktrees, this returns the path to the main repo.
    """
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--git-common-dir'],
            capture_output=True,
            text=True,
            check=True,
        )
        git_common_dir = Path(result.stdout.strip()).resolve()
        # git-common-dir returns the .git directory, get parent
        if git_common_dir.name == '.git':
            return git_common_dir.parent
        # For worktrees, it might be .git/worktrees/xxx, go up
        return git_common_dir.parent.parent.parent
    except subprocess.CalledProcessError:
        return get_repo_root()


def is_worktree() -> bool:
    """Check if the current directory is a worktree (not the main repo)."""
    try:
        toplevel = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            capture_output=True,
            text=True,
            check=True,
        )
        common_dir = subprocess.run(
            ['git', 'rev-parse', '--git-common-dir'],
            capture_output=True,
            text=True,
            check=True,
        )
        toplevel_path = Path(toplevel.stdout.strip())
        common_path = Path(common_dir.stdout.strip())

        # If common dir is not in toplevel, this is a worktree
        return not common_path.is_relative_to(toplevel_path)
    except subprocess.CalledProcessError:
        return False


def list_worktrees(repo_path: str | None = None) -> list[GitWorktree]:
    """
    List all git worktrees.

    Args:
        repo_path: Optional path to the repository.

    Returns:
        List of GitWorktree objects.
    """
    cmd = ['git', 'worktree', 'list', '--porcelain']
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=repo_path,
        )
    except subprocess.CalledProcessError as e:
        raise GitError(f'Failed to list worktrees: {e.stderr}') from e

    worktrees = []
    current_wt: dict = {}

    for line in result.stdout.strip().split('\n'):
        if not line:
            if current_wt:
                worktrees.append(
                    GitWorktree(
                        path=current_wt.get('worktree', ''),
                        branch=current_wt.get('branch', ''),
                        commit=current_wt.get('HEAD', ''),
                        is_bare=current_wt.get('bare', False),
                        is_detached=current_wt.get('detached', False),
                    )
                )
                current_wt = {}
            continue

        if line.startswith('worktree '):
            current_wt['worktree'] = line[9:]
        elif line.startswith('HEAD '):
            current_wt['HEAD'] = line[5:]
        elif line.startswith('branch '):
            current_wt['branch'] = line[7:]
        elif line == 'bare':
            current_wt['bare'] = True
        elif line == 'detached':
            current_wt['detached'] = True

    # Don't forget the last worktree
    if current_wt:
        worktrees.append(
            GitWorktree(
                path=current_wt.get('worktree', ''),
                branch=current_wt.get('branch', ''),
                commit=current_wt.get('HEAD', ''),
                is_bare=current_wt.get('bare', False),
                is_detached=current_wt.get('detached', False),
            )
        )

    return worktrees


def create_worktree(
    feature_name: str,
    base_branch: str = 'develop',
    repo_path: str | None = None,
    branch_type: str | None = 'feature',
) -> tuple[Path, str]:
    """
    Create a new git worktree.

    Args:
        feature_name: Name of the feature (used for branch and directory name).
        base_branch: Base branch to create the feature branch from.
        repo_path: Optional path to the main repository.
        branch_type: Branch prefix type ('feature' or 'fix'). None for raw (no prefix).

    Returns:
        Tuple of (worktree_path, branch_name)

    Raises:
        GitError: If worktree creation fails.
    """
    main_repo = Path(repo_path) if repo_path else get_main_repo_root()
    branch_name = f'{branch_type}/{feature_name}' if branch_type else feature_name
    worktree_dir = f'{WORKTREE_PREFIX}{feature_name}'
    worktree_path = main_repo.parent / worktree_dir

    # Check if worktree directory already exists
    if worktree_path.exists():
        raise GitError(f'Worktree directory already exists: {worktree_path}')

    # Check if branch already exists
    result = subprocess.run(
        ['git', 'branch', '--list', branch_name],
        capture_output=True,
        text=True,
        cwd=main_repo,
    )
    branch_exists = bool(result.stdout.strip())

    # Create the worktree
    if branch_exists:
        # Use existing branch
        cmd = ['git', 'worktree', 'add', str(worktree_path), branch_name]
    else:
        # Create new branch from base
        cmd = ['git', 'worktree', 'add', '-b', branch_name, str(worktree_path), base_branch]

    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=main_repo,
        )
    except subprocess.CalledProcessError as e:
        raise GitError(f'Failed to create worktree: {e.stderr}') from e

    return (worktree_path, branch_name)


def remove_worktree(worktree_path: str, force: bool = False) -> None:
    """
    Remove a git worktree.

    Args:
        worktree_path: Path to the worktree to remove.
        force: If True, remove even if there are uncommitted changes.

    Raises:
        GitError: If worktree removal fails.
    """
    cmd = ['git', 'worktree', 'remove']
    if force:
        cmd.append('--force')
    cmd.append(worktree_path)

    try:
        # Need to run from main repo or another worktree
        main_repo = get_main_repo_root()
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=main_repo,
        )
    except subprocess.CalledProcessError as e:
        raise GitError(f'Failed to remove worktree: {e.stderr}') from e


def get_current_branch() -> str:
    """Get the current branch name."""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return 'unknown'


def has_uncommitted_changes() -> bool:
    """Check if there are uncommitted changes."""
    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            capture_output=True,
            text=True,
            check=True,
        )
        return bool(result.stdout.strip())
    except subprocess.CalledProcessError:
        return False


def commit_all(message: str) -> bool:
    """
    Stage all changes and commit.

    Returns:
        True if commit was made, False if nothing to commit.
    """
    # Check if there are changes
    if not has_uncommitted_changes():
        return False

    # Stage all changes
    subprocess.run(['git', 'add', '-A'], check=True)

    # Commit
    subprocess.run(['git', 'commit', '-m', message], check=True)
    return True


def validate_feature_name(feature_name: str) -> tuple[bool, str]:
    """
    Validate a feature name for use in worktree/branch names.

    Args:
        feature_name: The feature name to validate.

    Returns:
        Tuple of (is_valid, error_message)
    """
    # Only allow alphanumeric, hyphens, underscores
    if not re.match(r'^[a-zA-Z0-9_-]+$', feature_name):
        return (False, 'Feature name can only contain letters, numbers, hyphens, and underscores')

    # Prevent directory traversal
    if '..' in feature_name:
        return (False, "Feature name cannot contain '..'")

    # Prevent starting with hyphen (git branch naming)
    if feature_name.startswith('-'):
        return (False, 'Feature name cannot start with a hyphen')

    # Length check
    if len(feature_name) > 100:
        return (False, 'Feature name must be 100 characters or less')

    return (True, '')


def is_branch_merged(branch_name: str, target_branch: str = 'develop', repo_path: str | None = None) -> bool:
    """
    Check if a branch has been merged into the target branch.

    Args:
        branch_name: The branch to check.
        target_branch: The branch to check against (default: develop).
        repo_path: Path to the repository.

    Returns:
        True if the branch is merged, False otherwise.
    """
    try:
        # Get branches that contain all commits from branch_name
        result = subprocess.run(
            ['git', 'branch', '--merged', target_branch],
            capture_output=True,
            text=True,
            check=True,
            cwd=repo_path,
        )
        # Check if our branch is in the merged list
        merged_branches = [b.strip().lstrip('* ') for b in result.stdout.strip().split('\n')]
        return branch_name in merged_branches or f'feature/{branch_name}' in merged_branches
    except subprocess.CalledProcessError:
        return False


def has_uncommitted_changes_in_path(path: str) -> bool:
    """Check if there are uncommitted changes in a specific path."""
    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            capture_output=True,
            text=True,
            check=True,
            cwd=path,
        )
        return bool(result.stdout.strip())
    except subprocess.CalledProcessError:
        return False


def has_unpushed_commits(path: str) -> bool:
    """Check if there are commits that haven't been pushed to the remote."""
    try:
        # Get the current branch
        branch_result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
            cwd=path,
        )
        branch = branch_result.stdout.strip()

        # Check if remote tracking branch exists
        remote_check = subprocess.run(
            ['git', 'rev-parse', '--verify', f'origin/{branch}'],
            capture_output=True,
            text=True,
            cwd=path,
        )
        if remote_check.returncode != 0:
            # No remote tracking branch - all commits are unpushed
            return True

        # Count commits ahead of remote
        result = subprocess.run(
            ['git', 'rev-list', '--count', f'origin/{branch}..HEAD'],
            capture_output=True,
            text=True,
            check=True,
            cwd=path,
        )
        return int(result.stdout.strip()) > 0
    except (subprocess.CalledProcessError, ValueError):
        return False
