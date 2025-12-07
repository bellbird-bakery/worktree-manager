"""
JSON API endpoints for worktree manager.

Provides REST-like API for task management.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..task_store import Task, TaskStatus, tasks, save_task
from ..registry import read_registry

logger = logging.getLogger('worktree_manager')


async def sync_tasks(request: Request) -> JSONResponse:
    """
    Synchronize tasks with the worktree registry.

    - Creates tasks for worktrees that don't have one
    - Updates worktree_path for existing tasks
    """
    try:
        registry = read_registry()
    except FileNotFoundError:
        return JSONResponse({
            'success': False,
            'error': 'Registry file not found',
            'created': 0,
            'updated': 0,
        })

    if registry is None:
        return JSONResponse({
            'success': False,
            'error': 'No registry found',
            'created': 0,
            'updated': 0,
        })

    created_count = 0
    updated_count = 0

    # Get all feature names from registry
    registry_features = {w.feature_name: w for w in registry.worktrees}

    # Create tasks for worktrees without tasks
    for feature_name, worktree in registry_features.items():
        task, created = tasks.get_or_create(
            feature_name=feature_name,
            defaults={
                'title': feature_name.replace('-', ' ').replace('_', ' ').title(),
                'worktree_path': worktree.path,
                'status': TaskStatus.TODO.value,
            },
        )

        if created:
            created_count += 1
        elif task.worktree_path != worktree.path:
            tasks.update(task, worktree_path=worktree.path)
            updated_count += 1

    return JSONResponse({
        'success': True,
        'created': created_count,
        'updated': updated_count,
        'total_worktrees': len(registry_features),
    })


async def list_tasks(request: Request) -> JSONResponse:
    """List all tasks."""
    all_tasks = tasks.all()
    return JSONResponse({
        'tasks': [
            {
                'id': t.id,
                'feature_name': t.feature_name,
                'title': t.title,
                'status': t.status,
                'status_display': t.status_display,
                'worktree_path': t.worktree_path,
                'priority': t.priority,
                'updated_at': t.updated_at.isoformat(),
            }
            for t in all_tasks
        ]
    })


async def get_task(request: Request) -> JSONResponse:
    """Get a single task by ID."""
    pk = request.path_params['pk']
    task = tasks.get_by_pk(pk)

    if not task:
        return JSONResponse({'error': 'Task not found'}, status_code=404)

    return JSONResponse({
        'id': task.id,
        'feature_name': task.feature_name,
        'title': task.title,
        'description': task.description,
        'status': task.status,
        'status_display': task.status_display,
        'worktree_path': task.worktree_path,
        'priority': task.priority,
        'notes': task.notes,
        'created_at': task.created_at.isoformat(),
        'updated_at': task.updated_at.isoformat(),
    })


async def update_task_status(request: Request) -> JSONResponse:
    """Update a task's status."""
    pk = request.path_params['pk']
    task = tasks.get_by_pk(pk)

    if not task:
        return JSONResponse({'error': 'Task not found'}, status_code=404)

    # Parse request body
    try:
        body = await request.json()
        new_status = body.get('status')
    except Exception:
        # Try form data
        form = await request.form()
        new_status = form.get('status')

    if new_status not in TaskStatus.values():
        return JSONResponse(
            {'error': f'Invalid status: {new_status}'},
            status_code=400,
        )

    old_status = task.status

    if old_status == new_status:
        return JSONResponse({'success': True, 'message': 'No change'})

    # Update task
    task.status = new_status
    save_task(task)

    # Sync to JSON
    try:
        from ..sync import sync_sqlite_to_json
        sync_sqlite_to_json()
    except Exception as e:
        logger.warning(f'JSON sync failed: {e}')

    return JSONResponse({
        'success': True,
        'message': f'Task moved from {old_status} to {new_status}',
    })
