"""
Task storage for worktree task tracking.

Provides both JSON-based storage (for git sync) and SQLite storage (for local web UI).
This module is Django-free and can be used standalone.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator, TypedDict

from . import CONFIG_DIR, TASK_DB_PATH

logger = logging.getLogger('worktree_manager')


# ==============================================================================
# Task Status Enum
# ==============================================================================


class TaskStatus(str, Enum):
    """Task status choices for Kanban board columns."""

    TODO = 'todo'
    IN_PROGRESS = 'in_progress'
    DONE = 'done'

    @classmethod
    def values(cls) -> list[str]:
        """Return all valid status values."""
        return [s.value for s in cls]

    @classmethod
    def display_name(cls, status: str) -> str:
        """Get display name for a status."""
        names = {
            cls.TODO.value: 'To Do',
            cls.IN_PROGRESS.value: 'In Progress',
            cls.DONE.value: 'Done',
        }
        return names.get(status, status)


# ==============================================================================
# Task Data Classes
# ==============================================================================


@dataclass
class Task:
    """
    A task associated with a git worktree/feature branch.

    Tasks are displayed on a Kanban board and track the progress
    of feature development across worktrees.
    """

    # Required fields
    feature_name: str
    title: str

    # Optional fields with defaults
    description: str = ''
    status: str = TaskStatus.TODO.value
    worktree_path: str = ''
    priority: int = 0
    notes: str = ''

    # Auto-managed timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_synced_at: datetime | None = None

    # Database ID (set when loaded from SQLite)
    id: int | None = None

    def __post_init__(self) -> None:
        """Ensure timestamps have timezone info."""
        if self.created_at.tzinfo is None:
            self.created_at = self.created_at.replace(tzinfo=timezone.utc)
        if self.updated_at.tzinfo is None:
            self.updated_at = self.updated_at.replace(tzinfo=timezone.utc)

    @property
    def status_display(self) -> str:
        """Get human-readable status name."""
        return TaskStatus.display_name(self.status)

    def get_status_display(self) -> str:
        """Django-compatible method for status display."""
        return self.status_display

    def start(self) -> None:
        """Move task to In Progress status."""
        self.status = TaskStatus.IN_PROGRESS.value
        self.updated_at = datetime.now(timezone.utc)

    def complete(self) -> None:
        """Move task to Done status."""
        self.status = TaskStatus.DONE.value
        self.updated_at = datetime.now(timezone.utc)

    def reopen(self) -> None:
        """Move task back to To Do status."""
        self.status = TaskStatus.TODO.value
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            'title': self.title,
            'description': self.description,
            'status': self.status,
            'priority': self.priority,
            'notes': self.notes,
            'worktree_path': self.worktree_path,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'last_synced_at': self.last_synced_at.isoformat() if self.last_synced_at else None,
        }

    @classmethod
    def from_dict(cls, feature_name: str, data: dict) -> Task:
        """Create Task from dictionary (e.g., from JSON)."""
        created_at = parse_timestamp(data.get('created_at', '')) if data.get('created_at') else datetime.now(
            timezone.utc
        )
        updated_at = parse_timestamp(data.get('updated_at', '')) if data.get('updated_at') else datetime.now(
            timezone.utc
        )
        last_synced_at = parse_timestamp(data['last_synced_at']) if data.get('last_synced_at') else None

        return cls(
            feature_name=feature_name,
            title=data.get('title', feature_name.replace('-', ' ').replace('_', ' ').title()),
            description=data.get('description', ''),
            status=data.get('status', TaskStatus.TODO.value),
            worktree_path=data.get('worktree_path', ''),
            priority=data.get('priority', 0),
            notes=data.get('notes', ''),
            created_at=created_at,
            updated_at=updated_at,
            last_synced_at=last_synced_at,
        )


# ==============================================================================
# JSON Storage (for git sync)
# ==============================================================================


class TaskData(TypedDict, total=False):
    """Task data structure for JSON storage."""

    title: str
    description: str
    status: str
    priority: int
    notes: str
    worktree_path: str
    created_at: str
    updated_at: str
    last_synced_at: str | None


class TaskStore(TypedDict):
    """Root structure of the task store JSON file."""

    version: int
    tasks: dict[str, TaskData]


TASK_FILE_NAME = '.worktree-tasks.json'
CURRENT_VERSION = 1


def find_repo_root(start_path: Path | None = None) -> Path | None:
    """
    Find git repository root from the given path or current directory.

    Walks up the directory tree looking for a .git directory.
    Returns None if not inside a git repository.
    """
    cwd = start_path or Path.cwd()

    # Handle worktrees: .git might be a file pointing to the main repo
    for parent in [cwd, *cwd.parents]:
        git_path = parent / '.git'
        if git_path.exists():
            return parent

    return None


def get_task_file(repo_root: Path | None = None) -> Path | None:
    """
    Get path to .worktree-tasks.json in repository root.

    Returns None if not inside a git repository.
    """
    repo = repo_root or find_repo_root()
    return repo / TASK_FILE_NAME if repo else None


def read_tasks(repo_root: Path | None = None) -> TaskStore:
    """
    Read tasks from JSON file.

    Returns an empty store if the file doesn't exist or we're not in a repo.
    """
    path = get_task_file(repo_root)
    if not path or not path.exists():
        return {'version': CURRENT_VERSION, 'tasks': {}}

    try:
        data = json.loads(path.read_text())
        # Ensure required structure
        if 'version' not in data:
            data['version'] = CURRENT_VERSION
        if 'tasks' not in data:
            data['tasks'] = {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f'Failed to read {path}: {e}')
        return {'version': CURRENT_VERSION, 'tasks': {}}


def write_tasks(store: TaskStore, repo_root: Path | None = None) -> bool:
    """
    Write tasks to JSON file.

    Returns True on success, False if not in a git repository or write fails.
    """
    path = get_task_file(repo_root)
    if not path:
        return False

    try:
        # Pretty-print with sorted keys for clean git diffs
        content = json.dumps(store, indent=2, sort_keys=True)
        path.write_text(content + '\n')  # Trailing newline for git
        return True
    except OSError as e:
        logger.warning(f'Failed to write {path}: {e}')
        return False


def task_to_json(
    feature_name: str,
    title: str,
    status: str,
    description: str = '',
    priority: int = 0,
    notes: str = '',
    worktree_path: str = '',
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> TaskData:
    """
    Create a TaskData dict from task fields.

    Timestamps are normalized to UTC ISO 8601 format.
    """
    now = datetime.now(timezone.utc)
    created = created_at or now
    updated = updated_at or now

    # Ensure UTC
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)

    return {
        'title': title,
        'description': description,
        'status': status,
        'priority': priority,
        'notes': notes,
        'worktree_path': worktree_path,
        'created_at': created.isoformat(),
        'updated_at': updated.isoformat(),
    }


def parse_timestamp(iso_string: str) -> datetime:
    """Parse an ISO 8601 timestamp string to datetime."""
    if not iso_string:
        return datetime.now(timezone.utc)
    # Handle both 'Z' suffix and '+00:00' formats
    if iso_string.endswith('Z'):
        iso_string = iso_string[:-1] + '+00:00'
    return datetime.fromisoformat(iso_string)


def add_task(
    feature_name: str,
    title: str,
    status: str = 'todo',
    repo_root: Path | None = None,
) -> bool:
    """
    Add a new task to the store.

    Returns True on success, False if task already exists or write fails.
    """
    store = read_tasks(repo_root)

    if feature_name in store['tasks']:
        return False

    store['tasks'][feature_name] = task_to_json(
        feature_name=feature_name,
        title=title,
        status=status,
    )

    return write_tasks(store, repo_root)


def update_task_status(
    feature_name: str,
    status: str,
    repo_root: Path | None = None,
) -> bool:
    """
    Update a task's status in the store.

    Returns True on success, False if task doesn't exist or write fails.
    """
    store = read_tasks(repo_root)

    if feature_name not in store['tasks']:
        return False

    task = store['tasks'][feature_name]
    task['status'] = status
    task['updated_at'] = datetime.now(timezone.utc).isoformat()

    return write_tasks(store, repo_root)


def get_task(feature_name: str, repo_root: Path | None = None) -> TaskData | None:
    """Get a task by feature name, or None if not found."""
    store = read_tasks(repo_root)
    return store['tasks'].get(feature_name)


# ==============================================================================
# SQLite Storage (for local web UI)
# ==============================================================================


def get_db_path() -> Path:
    """Get path to SQLite database."""
    path = Path(os.path.expanduser(TASK_DB_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_connection() -> sqlite3.Connection:
    """Get SQLite database connection with row factory."""
    db_path = get_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db_connection() -> Iterator[sqlite3.Connection]:
    """Context manager for database connections."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Initialize the SQLite database schema."""
    with db_connection() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feature_name TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'todo',
                worktree_path TEXT DEFAULT '',
                priority INTEGER DEFAULT 0,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_synced_at TEXT
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_feature ON tasks(feature_name)')


def _row_to_task(row: sqlite3.Row) -> Task:
    """Convert a database row to a Task object."""
    return Task(
        id=row['id'],
        feature_name=row['feature_name'],
        title=row['title'],
        description=row['description'] or '',
        status=row['status'],
        worktree_path=row['worktree_path'] or '',
        priority=row['priority'] or 0,
        notes=row['notes'] or '',
        created_at=parse_timestamp(row['created_at']),
        updated_at=parse_timestamp(row['updated_at']),
        last_synced_at=parse_timestamp(row['last_synced_at']) if row['last_synced_at'] else None,
    )


# ==============================================================================
# SQLite Task Operations (Django-like interface)
# ==============================================================================


class TaskManager:
    """
    Django-like manager for Task objects.

    Provides a familiar interface for querying tasks from SQLite.
    """

    def all(self) -> list[Task]:
        """Get all tasks."""
        init_db()
        with db_connection() as conn:
            cursor = conn.execute('SELECT * FROM tasks ORDER BY -priority, created_at DESC')
            return [_row_to_task(row) for row in cursor.fetchall()]

    def filter(self, **kwargs) -> list[Task]:
        """Filter tasks by field values."""
        init_db()
        conditions = []
        params = []

        for key, value in kwargs.items():
            conditions.append(f'{key} = ?')
            params.append(value)

        where_clause = ' AND '.join(conditions) if conditions else '1=1'

        with db_connection() as conn:
            cursor = conn.execute(
                f'SELECT * FROM tasks WHERE {where_clause} ORDER BY -priority, created_at DESC',
                params,
            )
            return [_row_to_task(row) for row in cursor.fetchall()]

    def get(self, **kwargs) -> Task | None:
        """Get a single task by field values."""
        results = self.filter(**kwargs)
        if len(results) == 1:
            return results[0]
        elif len(results) == 0:
            return None
        else:
            raise ValueError(f'Multiple tasks match criteria: {kwargs}')

    def get_by_pk(self, pk: int) -> Task | None:
        """Get task by primary key (id)."""
        return self.get(id=pk)

    def get_by_feature(self, feature_name: str) -> Task | None:
        """Get task by feature name."""
        return self.get(feature_name=feature_name)

    def create(self, **kwargs) -> Task:
        """Create a new task."""
        init_db()
        now = datetime.now(timezone.utc).isoformat()

        feature_name = kwargs['feature_name']
        title = kwargs.get('title', feature_name.replace('-', ' ').replace('_', ' ').title())

        with db_connection() as conn:
            cursor = conn.execute(
                '''
                INSERT INTO tasks (
                    feature_name, title, description, status, worktree_path,
                    priority, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    feature_name,
                    title,
                    kwargs.get('description', ''),
                    kwargs.get('status', TaskStatus.TODO.value),
                    kwargs.get('worktree_path', ''),
                    kwargs.get('priority', 0),
                    kwargs.get('notes', ''),
                    now,
                    now,
                ),
            )
            task_id = cursor.lastrowid

        return self.get_by_pk(task_id)

    def get_or_create(self, feature_name: str, defaults: dict | None = None) -> tuple[Task, bool]:
        """
        Get a task by feature name, or create it if it doesn't exist.

        Returns (task, created) tuple.
        """
        existing = self.get_by_feature(feature_name)
        if existing:
            return existing, False

        create_kwargs = {'feature_name': feature_name}
        if defaults:
            create_kwargs.update(defaults)

        task = self.create(**create_kwargs)
        return task, True

    def count(self) -> int:
        """Count all tasks."""
        init_db()
        with db_connection() as conn:
            cursor = conn.execute('SELECT COUNT(*) FROM tasks')
            return cursor.fetchone()[0]

    def update(self, task: Task, **kwargs) -> Task:
        """Update a task with new values."""
        if task.id is None:
            raise ValueError('Cannot update task without id')

        init_db()
        now = datetime.now(timezone.utc).isoformat()

        # Build update query
        updates = ['updated_at = ?']
        params = [now]

        for key, value in kwargs.items():
            updates.append(f'{key} = ?')
            if key in ('created_at', 'updated_at', 'last_synced_at') and isinstance(value, datetime):
                params.append(value.isoformat())
            else:
                params.append(value)

        params.append(task.id)

        with db_connection() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", params)

        return self.get_by_pk(task.id)

    def delete(self, task: Task) -> bool:
        """Delete a task."""
        if task.id is None:
            return False

        init_db()
        with db_connection() as conn:
            conn.execute('DELETE FROM tasks WHERE id = ?', (task.id,))
        return True

    def delete_by_feature(self, feature_name: str) -> bool:
        """Delete a task by feature name."""
        init_db()
        with db_connection() as conn:
            cursor = conn.execute('DELETE FROM tasks WHERE feature_name = ?', (feature_name,))
            return cursor.rowcount > 0


# Global manager instance (like Django's Task.objects)
tasks = TaskManager()


def save_task(task: Task) -> Task:
    """
    Save a task to SQLite.

    If task has an id, updates existing. Otherwise creates new.
    """
    init_db()
    now = datetime.now(timezone.utc).isoformat()

    if task.id is not None:
        # Update existing
        with db_connection() as conn:
            conn.execute(
                '''
                UPDATE tasks SET
                    title = ?, description = ?, status = ?, worktree_path = ?,
                    priority = ?, notes = ?, updated_at = ?, last_synced_at = ?
                WHERE id = ?
                ''',
                (
                    task.title,
                    task.description,
                    task.status,
                    task.worktree_path,
                    task.priority,
                    task.notes,
                    now,
                    task.last_synced_at.isoformat() if task.last_synced_at else None,
                    task.id,
                ),
            )
        return tasks.get_by_pk(task.id)
    else:
        # Check if exists by feature_name
        existing = tasks.get_by_feature(task.feature_name)
        if existing:
            task.id = existing.id
            return save_task(task)

        # Create new
        with db_connection() as conn:
            cursor = conn.execute(
                '''
                INSERT INTO tasks (
                    feature_name, title, description, status, worktree_path,
                    priority, notes, created_at, updated_at, last_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    task.feature_name,
                    task.title,
                    task.description,
                    task.status,
                    task.worktree_path,
                    task.priority,
                    task.notes,
                    task.created_at.isoformat(),
                    now,
                    task.last_synced_at.isoformat() if task.last_synced_at else None,
                ),
            )
            task.id = cursor.lastrowid

        return tasks.get_by_pk(task.id)


# ==============================================================================
# Utility Functions
# ==============================================================================


def ensure_db() -> None:
    """Ensure database exists and is initialized."""
    init_db()


def get_tasks_by_status() -> dict[str, list[Task]]:
    """Get all tasks grouped by status."""
    all_tasks = tasks.all()
    result = {
        TaskStatus.TODO.value: [],
        TaskStatus.IN_PROGRESS.value: [],
        TaskStatus.DONE.value: [],
    }
    for task in all_tasks:
        if task.status in result:
            result[task.status].append(task)
    return result
