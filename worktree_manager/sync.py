"""
Synchronization between JSON task store and SQLite database.

Provides bidirectional sync for multi-system task tracking:
- JSON file (.worktree-tasks.json) is the canonical source, version controlled in git
- SQLite database (tasks.db) is the local cache for fast web UI queries
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from .task_store import (
    TASK_FILE_NAME,
    TaskStatus,
    ensure_db,
    find_repo_root,
    get_task_file,
    read_tasks,
    save_task,
    tasks,
    write_tasks,
)


class SyncResult(NamedTuple):
    """Result of a sync operation."""

    created: int
    updated: int
    unchanged: int
    errors: list[str]


class Conflict(NamedTuple):
    """A conflict between JSON and SQLite versions of a task."""

    feature_name: str
    json_data: dict
    sqlite_status: str
    sqlite_updated_at: datetime
    json_updated_at: datetime


def _parse_timestamp(ts_str: str | None) -> datetime:
    """Parse an ISO timestamp string to datetime."""
    if not ts_str:
        return datetime(1970, 1, 1, tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, AttributeError):
        return datetime(1970, 1, 1, tzinfo=UTC)


def sync_json_to_sqlite(repo_root: Path | str | None = None) -> SyncResult:
    """
    Sync tasks from JSON file to SQLite database.

    This is a one-way sync, typically run on webapp startup.
    Tasks in JSON that don't exist in SQLite are created.
    Tasks in JSON that are newer than SQLite are updated.

    Returns a SyncResult with counts of created/updated/unchanged tasks.
    """
    ensure_db()

    if repo_root is not None:
        repo_root = Path(repo_root)

    store = read_tasks(repo_root)
    created = 0
    updated = 0
    unchanged = 0
    errors: list[str] = []

    for feature_name, data in store.get('tasks', {}).items():
        try:
            # Get existing task
            existing_task = tasks.get_by_feature(feature_name)

            task_data = {
                'title': data.get('title', feature_name.replace('-', ' ').replace('_', ' ').title()),
                'description': data.get('description', ''),
                'status': data.get('status', TaskStatus.TODO.value),
                'priority': data.get('priority', 0),
                'notes': data.get('notes', ''),
                'worktree_path': data.get('worktree_path', ''),
            }

            if existing_task is None:
                # Create new task
                task, _ = tasks.get_or_create(
                    feature_name=feature_name,
                    defaults=task_data,
                )
                created += 1
            else:
                # Check if JSON is newer
                json_updated = _parse_timestamp(data.get('updated_at'))
                db_updated = existing_task.updated_at
                if db_updated.tzinfo is None:
                    db_updated = db_updated.replace(tzinfo=UTC)

                if json_updated > db_updated:
                    # Update from JSON
                    for key, value in task_data.items():
                        setattr(existing_task, key, value)
                    save_task(existing_task)
                    updated += 1
                else:
                    unchanged += 1

        except Exception as e:
            errors.append(f'Error syncing {feature_name}: {e}')

    return SyncResult(created=created, updated=updated, unchanged=unchanged, errors=errors)


def sync_sqlite_to_json(repo_root: Path | str | None = None) -> SyncResult:
    """
    Sync tasks from SQLite database to JSON file.

    This is typically run after changes in the web UI.
    Uses last-write-wins: only updates JSON if SQLite task is newer.

    Returns a SyncResult with counts of created/updated/unchanged tasks.
    """
    ensure_db()

    if repo_root is not None:
        repo_root = Path(repo_root)

    store = read_tasks(repo_root)
    created = 0
    updated = 0
    unchanged = 0
    errors: list[str] = []

    for task in tasks.all():
        try:
            task_data = {
                'title': task.title,
                'description': task.description,
                'status': task.status,
                'priority': task.priority,
                'notes': task.notes,
                'worktree_path': task.worktree_path,
                'created_at': task.created_at.isoformat(),
                'updated_at': task.updated_at.isoformat(),
            }

            if task.feature_name in store.get('tasks', {}):
                # Compare timestamps - update only if SQLite is newer
                existing = store['tasks'][task.feature_name]
                existing_updated = _parse_timestamp(existing.get('updated_at'))

                db_updated = task.updated_at
                if db_updated.tzinfo is None:
                    db_updated = db_updated.replace(tzinfo=UTC)

                if db_updated > existing_updated:
                    store['tasks'][task.feature_name] = task_data
                    updated += 1
                else:
                    unchanged += 1
            else:
                if 'tasks' not in store:
                    store['tasks'] = {}
                store['tasks'][task.feature_name] = task_data
                created += 1

        except Exception as e:
            errors.append(f'Error syncing {task.feature_name}: {e}')

    if write_tasks(store, repo_root):
        return SyncResult(created=created, updated=updated, unchanged=unchanged, errors=errors)
    else:
        errors.append('Failed to write JSON file')
        return SyncResult(created=0, updated=0, unchanged=0, errors=errors)


def full_sync(repo_root: Path | str | None = None) -> dict:
    """
    Perform bidirectional sync between JSON and SQLite.

    Process:
    1. Read both sources
    2. Merge using last-write-wins by updated_at
    3. Write back to both

    Returns a dict with sync statistics.
    """
    ensure_db()

    if repo_root is not None:
        repo_root = Path(repo_root)

    store = read_tasks(repo_root)
    stats = {
        'json_to_sqlite': {'created': 0, 'updated': 0},
        'sqlite_to_json': {'created': 0, 'updated': 0},
        'errors': [],
    }

    # Build merged task set
    merged_tasks: dict[str, tuple[dict, str]] = {}  # feature_name -> (data, source)

    # Start with JSON tasks
    for feature_name, data in store.get('tasks', {}).items():
        merged_tasks[feature_name] = (data, 'json')

    # Merge in SQLite tasks
    for task in tasks.all():
        db_data = {
            'title': task.title,
            'description': task.description,
            'status': task.status,
            'priority': task.priority,
            'notes': task.notes,
            'worktree_path': task.worktree_path,
            'created_at': task.created_at.isoformat(),
            'updated_at': task.updated_at.isoformat(),
        }

        if task.feature_name in merged_tasks:
            # Compare timestamps
            json_data, _ = merged_tasks[task.feature_name]
            json_updated = _parse_timestamp(json_data.get('updated_at'))

            db_updated = task.updated_at
            if db_updated.tzinfo is None:
                db_updated = db_updated.replace(tzinfo=UTC)

            if db_updated > json_updated:
                merged_tasks[task.feature_name] = (db_data, 'sqlite')
        else:
            merged_tasks[task.feature_name] = (db_data, 'sqlite')

    # Apply merged state to JSON
    new_store = {'version': store.get('version', 1), 'tasks': {}}
    for feature_name, (data, source) in merged_tasks.items():
        new_store['tasks'][feature_name] = data
        if source == 'sqlite' and feature_name not in store.get('tasks', {}):
            stats['sqlite_to_json']['created'] += 1
        elif source == 'sqlite':
            stats['sqlite_to_json']['updated'] += 1

    if not write_tasks(new_store, repo_root):
        stats['errors'].append('Failed to write JSON file')

    # Apply merged state to SQLite
    for feature_name, (data, source) in merged_tasks.items():
        try:
            defaults = {
                'title': data.get('title', feature_name),
                'description': data.get('description', ''),
                'status': data.get('status', TaskStatus.TODO.value),
                'priority': data.get('priority', 0),
                'notes': data.get('notes', ''),
                'worktree_path': data.get('worktree_path', ''),
            }

            task, was_created = tasks.get_or_create(
                feature_name=feature_name,
                defaults=defaults,
            )

            if was_created:
                stats['json_to_sqlite']['created'] += 1
            elif source == 'json':
                # Update existing task
                for key, value in defaults.items():
                    setattr(task, key, value)
                save_task(task)
                stats['json_to_sqlite']['updated'] += 1

        except Exception as e:
            stats['errors'].append(f'Error syncing {feature_name} to SQLite: {e}')

    return stats


def auto_commit_tasks(message: str = 'Update task status', repo_root: Path | str | None = None) -> bool:
    """
    Automatically commit changes to .worktree-tasks.json.

    Returns True if commit was made, False otherwise.
    """
    import subprocess

    if repo_root is not None:
        repo_root = Path(repo_root)

    repo = repo_root or find_repo_root()
    if not repo:
        return False

    task_file = repo / TASK_FILE_NAME
    if not task_file.exists():
        return False

    try:
        # Check if file has changes
        result = subprocess.run(
            ['git', 'status', '--porcelain', TASK_FILE_NAME],
            cwd=repo,
            capture_output=True,
            text=True,
        )

        if not result.stdout.strip():
            # No changes to commit
            return False

        # Stage the file
        subprocess.run(
            ['git', 'add', TASK_FILE_NAME],
            cwd=repo,
            check=True,
        )

        # Commit
        subprocess.run(
            ['git', 'commit', '-m', message],
            cwd=repo,
            check=True,
        )

        return True

    except subprocess.CalledProcessError:
        return False


def detect_conflicts(repo_root: Path | str | None = None) -> list[Conflict]:
    """
    Detect conflicts between JSON and SQLite task data.

    A conflict exists when:
    1. Task exists in both JSON and SQLite
    2. Both have been modified since last sync (last_synced_at)
    3. They have different status values

    Returns a list of Conflict objects.
    """
    ensure_db()

    if repo_root is not None:
        repo_root = Path(repo_root)

    store = read_tasks(repo_root)
    conflicts: list[Conflict] = []

    for task in tasks.all():
        if task.feature_name not in store.get('tasks', {}):
            # Task only in SQLite, not a conflict (will be added to JSON on sync)
            continue

        json_data = store['tasks'][task.feature_name]
        json_updated = _parse_timestamp(json_data.get('updated_at'))

        db_updated = task.updated_at
        if db_updated.tzinfo is None:
            db_updated = db_updated.replace(tzinfo=UTC)

        # Check if both have been modified since last sync
        if task.last_synced_at:
            last_sync = task.last_synced_at
            if last_sync.tzinfo is None:
                last_sync = last_sync.replace(tzinfo=UTC)

            json_modified_since_sync = json_updated > last_sync
            sqlite_modified_since_sync = db_updated > last_sync

            # Conflict if both modified and have different values
            if json_modified_since_sync and sqlite_modified_since_sync and task.status != json_data.get('status'):
                conflicts.append(
                    Conflict(
                        feature_name=task.feature_name,
                        json_data=json_data,
                        sqlite_status=task.status,
                        sqlite_updated_at=db_updated,
                        json_updated_at=json_updated,
                    )
                )
        else:
            # No last_synced_at means never synced - check if different
            if task.status != json_data.get('status'):
                conflicts.append(
                    Conflict(
                        feature_name=task.feature_name,
                        json_data=json_data,
                        sqlite_status=task.status,
                        sqlite_updated_at=db_updated,
                        json_updated_at=json_updated,
                    )
                )

    return conflicts


def resolve_conflict(
    feature_name: str,
    use_json: bool,
    repo_root: Path | str | None = None,
) -> bool:
    """
    Resolve a conflict by choosing either JSON or SQLite version.

    Args:
        feature_name: The task's feature name
        use_json: If True, use JSON version. If False, use SQLite version.
        repo_root: Optional path to repo root

    Returns:
        True if resolution succeeded, False otherwise.
    """
    ensure_db()

    if repo_root is not None:
        repo_root = Path(repo_root)

    task = tasks.get_by_feature(feature_name)
    if not task:
        return False

    store = read_tasks(repo_root)
    if feature_name not in store.get('tasks', {}):
        return False

    json_data = store['tasks'][feature_name]
    now = datetime.now(UTC)

    if use_json:
        # Update SQLite from JSON
        task.status = json_data.get('status', TaskStatus.TODO.value)
        task.title = json_data.get('title', task.title)
        task.description = json_data.get('description', '')
        task.priority = json_data.get('priority', 0)
        task.notes = json_data.get('notes', '')
        task.last_synced_at = now
        save_task(task)
    else:
        # Update JSON from SQLite
        store['tasks'][feature_name] = {
            'title': task.title,
            'description': task.description,
            'status': task.status,
            'priority': task.priority,
            'notes': task.notes,
            'worktree_path': task.worktree_path,
            'created_at': task.created_at.isoformat(),
            'updated_at': now.isoformat(),
        }
        if not write_tasks(store, repo_root):
            return False
        task.last_synced_at = now
        save_task(task)

    return True


def mark_synced(feature_name: str) -> bool:
    """
    Mark a task as synced (update last_synced_at to now).

    Returns True if successful, False if task not found.
    """
    ensure_db()

    task = tasks.get_by_feature(feature_name)
    if not task:
        return False

    task.last_synced_at = datetime.now(UTC)
    save_task(task)
    return True


def cleanup_orphaned_tasks(repo_root: Path | str | None = None, dry_run: bool = False) -> dict:
    """
    Remove tasks from JSON and SQLite that have no corresponding local worktree.

    An orphaned task is one where:
    1. Task exists in JSON file or SQLite database
    2. No worktree exists locally with that feature_name
    3. Task is not in the worktree registry

    This is useful for cleaning up tasks after worktrees have been manually deleted
    or closed on another machine.

    Args:
        repo_root: Optional path to repo root
        dry_run: If True, just return what would be removed without deleting

    Returns:
        dict with:
        - removed: list of feature names that were removed (or would be)
        - kept: list of feature names that were kept
        - errors: list of error messages
    """
    from .git_ops import get_main_repo_root, list_worktrees
    from .registry import read_registry

    ensure_db()

    if repo_root is not None:
        repo_root = Path(repo_root)

    store = read_tasks(repo_root)
    result = {
        'removed': [],
        'kept': [],
        'errors': [],
    }

    # Collect all feature names from both JSON and SQLite
    all_features = set(store.get('tasks', {}).keys())
    for task in tasks.all():
        all_features.add(task.feature_name)

    if not all_features:
        return result

    # Get registered worktrees
    registry = read_registry()
    registered_features = set()
    if registry:
        registered_features = {wt.feature_name for wt in registry.worktrees}

    # Get actual git worktrees
    try:
        main_repo = get_main_repo_root()
        git_worktrees = list_worktrees(str(main_repo))
        git_worktree_branches = set()
        for wt in git_worktrees:
            # Extract feature name from branch (e.g., "feature/foo" -> "foo")
            branch = wt.branch
            for prefix in ('feature/', 'bugfix/', 'chore/', 'hotfix/'):
                if branch.startswith(prefix):
                    branch = branch[len(prefix) :]
                    break
            git_worktree_branches.add(branch)
    except Exception as e:
        result['errors'].append(f'Could not list git worktrees: {e}')
        git_worktree_branches = set()

    # Determine which tasks to keep
    tasks_to_remove = []
    for feature_name in all_features:
        # Keep if in registry
        if feature_name in registered_features:
            result['kept'].append(feature_name)
            continue

        # Keep if has active git worktree
        if feature_name in git_worktree_branches:
            result['kept'].append(feature_name)
            continue

        # Orphaned - mark for removal
        tasks_to_remove.append(feature_name)

    # Remove orphaned tasks (unless dry run)
    if not dry_run:
        for feature_name in tasks_to_remove:
            # Remove from JSON
            if feature_name in store.get('tasks', {}):
                del store['tasks'][feature_name]

            # Remove from SQLite
            tasks.delete_by_feature(feature_name)

            result['removed'].append(feature_name)

        if tasks_to_remove:
            if not write_tasks(store, repo_root):
                result['errors'].append('Failed to write JSON file')
    else:
        result['removed'] = tasks_to_remove

    return result


def get_sync_status(repo_root: Path | str | None = None) -> dict:
    """
    Get overall sync status between JSON and SQLite.

    Returns a dict with:
    - json_only: tasks only in JSON (not yet in SQLite)
    - sqlite_only: tasks only in SQLite (not yet in JSON)
    - synced: tasks that are in sync
    - conflicts: tasks with conflicts
    - last_json_modified: last modification time of JSON file
    """
    ensure_db()

    if repo_root is not None:
        repo_root = Path(repo_root)

    store = read_tasks(repo_root)
    json_features = set(store.get('tasks', {}).keys())
    sqlite_features = set(t.feature_name for t in tasks.all())

    conflicts = detect_conflicts(repo_root)
    conflict_features = {c.feature_name for c in conflicts}

    # Get JSON file modification time
    task_file = get_task_file(repo_root)
    last_json_modified = None
    if task_file and task_file.exists():
        last_json_modified = datetime.fromtimestamp(
            task_file.stat().st_mtime,
            tz=UTC,
        )

    return {
        'json_only': list(json_features - sqlite_features),
        'sqlite_only': list(sqlite_features - json_features),
        'synced': list((json_features & sqlite_features) - conflict_features),
        'conflicts': conflicts,
        'last_json_modified': last_json_modified,
        'json_task_count': len(json_features),
        'sqlite_task_count': len(sqlite_features),
    }
