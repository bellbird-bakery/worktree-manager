"""
Web routes for worktree manager.

Starlette-based routes replacing Django views.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import subprocess
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..docker_ops import compose_down, fix_permissions, is_docker_running
from ..git_ops import (
    get_main_repo_root,
    has_uncommitted_changes_in_path,
    has_unpushed_commits,
    is_branch_merged,
    remove_worktree,
    validate_feature_name,
)
from ..registry import locked_registry, read_registry
from ..sync import (
    detect_conflicts,
    get_sync_status,
    resolve_conflict,
    sync_json_to_sqlite,
    sync_sqlite_to_json,
)
from ..task_store import (
    Task,
    TaskStatus,
    find_repo_root,
    get_task_file,
    get_tasks_by_status,
    save_task,
    tasks,
)

logger = logging.getLogger('worktree_manager')

PACKAGE_DIR = Path(__file__).parent.parent


def get_templates(request: Request):
    """Get Jinja2Templates from app state."""
    return request.app.state.templates


# ==============================================================================
# Kanban Board Routes
# ==============================================================================


async def kanban_board(request: Request) -> Response:
    """Render the Kanban board with all tasks grouped by status."""
    templates = get_templates(request)
    tasks_by_status = get_tasks_by_status()

    todo_tasks = tasks_by_status[TaskStatus.TODO.value]
    in_progress_tasks = tasks_by_status[TaskStatus.IN_PROGRESS.value]
    done_tasks = tasks_by_status[TaskStatus.DONE.value]

    total_count = len(todo_tasks) + len(in_progress_tasks) + len(done_tasks)

    return templates.TemplateResponse(
        request,
        'kanban.html',
        {
            'todo_tasks': todo_tasks,
            'in_progress_tasks': in_progress_tasks,
            'done_tasks': done_tasks,
            'total_count': total_count,
        },
    )


async def move_task(request: Request) -> Response:
    """Handle drag-drop move of a task to a new status column."""
    pk = request.path_params['pk']
    task = tasks.get_by_pk(pk)

    if not task:
        return JSONResponse({'error': 'Task not found'}, status_code=404)

    # Get new status from POST data
    form = await request.form()
    new_status = form.get('status')

    if new_status not in TaskStatus.values():
        return Response(
            f'Invalid status: {new_status}',
            status_code=400,
            media_type='text/plain',
        )

    old_status = task.status

    if old_status == new_status:
        return Response('No change', status_code=200, media_type='text/plain')

    # Build hook context
    try:
        from ..hooks import build_context, get_hook_manager

        ctx = build_context(
            task=task,
            old_status=old_status,
            new_status=new_status,
            source='webapp',
            interactive=False,
        )

        hook_manager = get_hook_manager()
        pre_result = hook_manager.pre_check(ctx)

        if pre_result and pre_result.requires_confirmation:
            return JSONResponse({
                'needs_confirmation': True,
                'confirmation_type': pre_result.confirmation_type,
                'data': pre_result.confirmation_data,
                'task_id': task.id,
                'old_status': old_status,
                'new_status': new_status,
            })
    except ImportError:
        # Hooks not available
        hook_manager = None
        ctx = None

    # Update task status
    task.status = new_status
    save_task(task)

    # Execute hooks
    hook_results = []
    if hook_manager and ctx:
        try:
            hook_results = hook_manager.execute(ctx)
        except Exception as e:
            logger.warning(f'Hook execution failed: {e}')

    # Sync to JSON
    try:
        sync_sqlite_to_json()
    except Exception as e:
        logger.warning(f'JSON sync failed after task move: {e}')

    return JSONResponse({
        'success': True,
        'message': f'Task moved from {old_status} to {new_status}',
        'hooks': [
            {'name': r.hook_name, 'success': r.success, 'message': r.message}
            for r in hook_results
        ] if hook_results else [],
    })


async def confirm_hook(request: Request) -> Response:
    """Handle user confirmation for hooks that need it."""
    pk = request.path_params['pk']
    task = tasks.get_by_pk(pk)

    if not task:
        return JSONResponse({'error': 'Task not found'}, status_code=404)

    form = await request.form()
    action = form.get('action')
    old_status = form.get('old_status')
    new_status = form.get('new_status')
    commit_message = form.get('commit_message', '')

    if new_status not in TaskStatus.values():
        return JSONResponse({'success': False, 'error': 'Invalid status'}, status_code=400)

    # Build context with user confirmation data
    try:
        from ..hooks import build_context, get_hook_manager

        ctx = build_context(
            task=task,
            old_status=old_status,
            new_status=new_status,
            source='webapp',
            interactive=False,
        )
        ctx.user_confirmed = action == 'confirm'
        ctx.user_data = {'commit_message': commit_message}

        hook_manager = get_hook_manager()
    except ImportError:
        hook_manager = None
        ctx = None

    # Update task status
    task.status = new_status
    save_task(task)

    # Execute hooks
    hook_results = []
    if hook_manager and ctx:
        try:
            hook_results = hook_manager.execute(ctx)
        except Exception as e:
            logger.warning(f'Hook execution failed: {e}')

    # Sync to JSON
    try:
        sync_sqlite_to_json()
    except Exception as e:
        logger.warning(f'JSON sync failed after task move: {e}')

    return JSONResponse({
        'success': True,
        'message': f'Task moved to {new_status}',
        'hooks': [
            {'name': r.hook_name, 'success': r.success, 'message': r.message}
            for r in hook_results
        ] if hook_results else [],
    })


# ==============================================================================
# Worktree Management Routes
# ==============================================================================


async def worktree_list(request: Request) -> Response:
    """Show all worktrees with their task status and local availability."""
    templates = get_templates(request)

    # Get local registry
    try:
        registry = read_registry()
        local_worktrees = {w.feature_name: w for w in registry.worktrees} if registry else {}
    except Exception as e:
        logger.warning(f'Failed to read registry: {e}')
        local_worktrees = {}

    # Get all tasks
    all_tasks = tasks.all()

    # Build combined list
    worktree_data = []
    seen_features = set()

    for task in all_tasks:
        local_wt = local_worktrees.get(task.feature_name)
        worktree_data.append({
            'feature_name': task.feature_name,
            'title': task.title,
            'status': task.status,
            'status_display': task.status_display,
            'has_local_worktree': local_wt is not None,
            'path': local_wt.path if local_wt else None,
            'web_port': local_wt.ports.web if local_wt else None,
            'db_port': local_wt.ports.db if local_wt else None,
            'updated_at': task.updated_at,
            'last_synced_at': task.last_synced_at,
        })
        seen_features.add(task.feature_name)

    # Add local worktrees without tasks
    for feature_name, wt in local_worktrees.items():
        if feature_name not in seen_features:
            worktree_data.append({
                'feature_name': feature_name,
                'title': feature_name.replace('-', ' ').replace('_', ' ').title(),
                'status': None,
                'status_display': 'No Task',
                'has_local_worktree': True,
                'path': wt.path,
                'web_port': wt.ports.web,
                'db_port': wt.ports.db,
                'updated_at': None,
                'last_synced_at': None,
            })

    # Sort by status then feature name
    status_order = {
        TaskStatus.IN_PROGRESS.value: 0,
        TaskStatus.TODO.value: 1,
        TaskStatus.DONE.value: 2,
        None: 3,
    }
    worktree_data.sort(key=lambda x: (status_order.get(x['status'], 3), x['feature_name']))

    # Get sync status
    sync_status = get_sync_status()

    return templates.TemplateResponse(
        request,
        'worktrees.html',
        {
            'worktrees': worktree_data,
            'sync_status': sync_status,
            'conflict_count': len(sync_status['conflicts']),
        },
    )


async def worktree_create(request: Request) -> Response:
    """Create a new worktree."""
    templates = get_templates(request)

    if request.method == 'GET':
        return templates.TemplateResponse(request, 'worktree_create.html', {})

    form = await request.form()
    feature_name = form.get('feature_name', '').strip()

    if not feature_name:
        return templates.TemplateResponse(
            request,
            'worktree_create.html',
            {'error': 'Feature name is required'},
        )

    # Validate feature name
    is_valid, error_msg = validate_feature_name(feature_name)
    if not is_valid:
        return templates.TemplateResponse(
            request,
            'worktree_create.html',
            {'error': error_msg, 'feature_name': feature_name},
        )

    # Check if already exists
    try:
        registry = read_registry()
        if registry:
            existing = registry.find_by_feature(feature_name)
            if existing:
                return templates.TemplateResponse(
                    request,
                    'worktree_create.html',
                    {
                        'error': f'Worktree for "{feature_name}" already exists',
                        'feature_name': feature_name,
                    },
                )
    except Exception:
        pass

    # Run the create command
    from ..commands import create_worktree_cmd

    try:
        result = create_worktree_cmd(feature_name)
        if result == 0:
            return RedirectResponse(url=request.url_for('worktree_list'), status_code=303)
        else:
            return templates.TemplateResponse(
                request,
                'worktree_create.html',
                {
                    'error': 'Failed to create worktree. Check console for details.',
                    'feature_name': feature_name,
                },
            )
    except Exception as e:
        logger.error(f'Worktree creation failed: {e}')
        return templates.TemplateResponse(
            request,
            'worktree_create.html',
            {'error': str(e), 'feature_name': feature_name},
        )


async def worktree_status(request: Request) -> Response:
    """Get the status of a worktree before closing."""
    feature_name = request.path_params['feature_name']

    registry = read_registry()
    if not registry:
        return JSONResponse({'error': 'No registry found'}, status_code=400)

    entry = registry.find_by_feature(feature_name)
    if not entry:
        return JSONResponse({'error': f'Worktree "{feature_name}" not found'}, status_code=404)

    worktree_path = entry.path

    if not Path(worktree_path).exists():
        return JSONResponse({
            'exists': False,
            'can_close': True,
            'warnings': [],
        })

    # Check git status
    has_uncommitted = has_uncommitted_changes_in_path(worktree_path)
    has_unpushed = has_unpushed_commits(worktree_path)

    # Get branch name
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True,
            text=True,
            check=True,
            cwd=worktree_path,
        )
        branch_name = result.stdout.strip()
    except Exception:
        branch_name = f'feature/{feature_name}'

    is_merged = is_branch_merged(branch_name, 'develop', worktree_path)

    warnings = []
    if has_uncommitted:
        warnings.append('uncommitted')
    if has_unpushed:
        warnings.append('unpushed')
    if not is_merged:
        warnings.append('unmerged')

    return JSONResponse({
        'exists': True,
        'branch': branch_name,
        'has_uncommitted': has_uncommitted,
        'has_unpushed': has_unpushed,
        'is_merged': is_merged,
        'warnings': warnings,
        'can_close': len(warnings) == 0,
    })


async def worktree_close(request: Request) -> Response:
    """Close a worktree."""
    feature_name = request.path_params['feature_name']

    # Check for force flag
    try:
        body = await request.json()
    except Exception:
        body = {}
    force = body.get('force', False)

    registry = read_registry()
    if not registry:
        return JSONResponse({'error': 'No registry found'}, status_code=400)

    entry = registry.find_by_feature(feature_name)
    if not entry:
        return JSONResponse({'error': f'Worktree "{feature_name}" not found'}, status_code=404)

    worktree_path = entry.path
    errors = []

    # Check if path exists
    if not Path(worktree_path).exists():
        # Path doesn't exist, just clean up registry
        try:
            main_repo = get_main_repo_root()
            with locked_registry(str(main_repo)) as reg:
                reg.remove_worktree(worktree_path)

            # Mark task as done
            from ..commands import complete_task_for_worktree
            complete_task_for_worktree(feature_name)

            return JSONResponse({
                'success': True,
                'message': f'Cleaned up registry entry for {feature_name}',
            })
        except Exception as e:
            logger.error(f'Failed to clean up registry: {e}')
            return JSONResponse({'error': str(e)}, status_code=500)

    # Safety checks (unless force=True)
    if not force:
        warnings = []

        if has_uncommitted_changes_in_path(worktree_path):
            warnings.append('uncommitted changes')

        if has_unpushed_commits(worktree_path):
            warnings.append('unpushed commits')

        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                capture_output=True,
                text=True,
                check=True,
                cwd=worktree_path,
            )
            branch_name = result.stdout.strip()
        except Exception:
            branch_name = f'feature/{feature_name}'

        if not is_branch_merged(branch_name, 'develop', worktree_path):
            warnings.append('branch not merged to develop')

        if warnings:
            return JSONResponse(
                {
                    'error': 'Safety check failed',
                    'warnings': warnings,
                    'message': f'Cannot close: {", ".join(warnings)}. Use force close to override.',
                    'requires_force': True,
                },
                status_code=400,
            )

    # Step 1: Fix permissions
    if is_docker_running():
        try:
            fix_permissions(worktree_path, entry.compose_project_name)
        except Exception as e:
            errors.append(f'Permission fix warning: {e}')

        # Step 2: Stop containers
        try:
            compose_down(worktree_path, entry.compose_project_name, volumes=False)
        except Exception as e:
            errors.append(f'Container stop warning: {e}')

    # Step 3: Remove git worktree
    try:
        remove_worktree(worktree_path, force=True)
    except Exception as e:
        logger.error(f'Failed to remove worktree: {e}')
        return JSONResponse(
            {'error': f'Failed to remove git worktree: {e}', 'warnings': errors},
            status_code=500,
        )

    # Step 4: Remove from registry
    try:
        main_repo = get_main_repo_root()
        with locked_registry(str(main_repo)) as reg:
            reg.remove_worktree(worktree_path)
    except Exception as e:
        errors.append(f'Registry cleanup warning: {e}')

    # Step 5: Mark task as done
    try:
        from ..commands import complete_task_for_worktree
        complete_task_for_worktree(feature_name)
    except Exception as e:
        errors.append(f'Task update warning: {e}')

    return JSONResponse({
        'success': True,
        'message': f'Worktree "{feature_name}" closed successfully',
        'warnings': errors if errors else None,
    })


# ==============================================================================
# Sync Routes
# ==============================================================================


async def sync_status_view(request: Request) -> Response:
    """Show sync status between JSON and SQLite."""
    templates = get_templates(request)
    sync_status = get_sync_status()

    repo_root = find_repo_root()
    task_file = get_task_file(repo_root) if repo_root else None

    return templates.TemplateResponse(
        request,
        'sync_status.html',
        {
            'sync_status': sync_status,
            'task_file': str(task_file) if task_file else None,
            'task_file_exists': task_file.exists() if task_file else False,
            'repo_root': str(repo_root) if repo_root else None,
        },
    )


async def sync_pull(request: Request) -> Response:
    """Pull from JSON to SQLite."""
    result = sync_json_to_sqlite()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JSONResponse({
            'success': True,
            'created': result.created,
            'updated': result.updated,
            'unchanged': result.unchanged,
            'errors': result.errors,
        })

    return RedirectResponse(url=request.url_for('sync_status'), status_code=303)


async def sync_push(request: Request) -> Response:
    """Push from SQLite to JSON."""
    result = sync_sqlite_to_json()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JSONResponse({
            'success': True,
            'created': result.created,
            'updated': result.updated,
            'unchanged': result.unchanged,
            'errors': result.errors,
        })

    return RedirectResponse(url=request.url_for('sync_status'), status_code=303)


async def conflict_list(request: Request) -> Response:
    """List all conflicts between JSON and SQLite."""
    templates = get_templates(request)
    conflicts = detect_conflicts()

    return templates.TemplateResponse(
        request,
        'conflicts.html',
        {'conflicts': conflicts},
    )


async def conflict_detail(request: Request) -> Response:
    """Show details of a specific conflict for resolution."""
    templates = get_templates(request)
    feature_name = request.path_params['feature_name']
    conflicts = detect_conflicts()

    conflict = None
    for c in conflicts:
        if c.feature_name == feature_name:
            conflict = c
            break

    if not conflict:
        return RedirectResponse(url=request.url_for('conflict_list'), status_code=303)

    task = tasks.get_by_feature(feature_name)
    if not task:
        return RedirectResponse(url=request.url_for('conflict_list'), status_code=303)

    return templates.TemplateResponse(
        request,
        'conflict_resolve.html',
        {
            'conflict': conflict,
            'task': task,
            'json_status': conflict.json_data.get('status'),
            'sqlite_status': conflict.sqlite_status,
        },
    )


async def conflict_resolve(request: Request) -> Response:
    """Resolve a conflict by choosing JSON or SQLite version."""
    feature_name = request.path_params['feature_name']
    form = await request.form()
    action = form.get('action')

    if action == 'use_json':
        success = resolve_conflict(feature_name, use_json=True)
    elif action == 'use_local':
        success = resolve_conflict(feature_name, use_json=False)
    else:
        return RedirectResponse(
            url=request.url_for('conflict_detail', feature_name=feature_name),
            status_code=303,
        )

    if success:
        remaining = detect_conflicts()
        if remaining:
            return RedirectResponse(url=request.url_for('conflict_list'), status_code=303)
        return RedirectResponse(url=request.url_for('worktree_list'), status_code=303)
    else:
        return RedirectResponse(
            url=request.url_for('conflict_detail', feature_name=feature_name),
            status_code=303,
        )


# ==============================================================================
# Database Cloning Routes
# ==============================================================================


async def clone_db_form(request: Request) -> Response:
    """Show form for cloning database to a worktree."""
    templates = get_templates(request)
    target_feature = request.path_params['target_feature']

    registry = read_registry()
    if not registry:
        return JSONResponse({'error': 'No registry found'}, status_code=400)

    target = registry.find_by_feature(target_feature)
    if not target:
        return JSONResponse({'error': f'Worktree "{target_feature}" not found'}, status_code=404)

    sources = []

    # Add main repo as source option
    main_repo_path = Path(registry.main_repo_path)
    if main_repo_path.exists():
        compose_file = main_repo_path / 'docker-compose.local.yml'
        if not compose_file.exists():
            compose_file = main_repo_path / 'docker-compose.yml'
        if compose_file.exists():
            sources.append({
                'index': 0,
                'feature_name': 'develop (main repo)',
                'path': str(main_repo_path),
                'is_main': True,
            })

    # Add other worktrees
    for wt in registry.worktrees:
        if wt.feature_name != target_feature and Path(wt.path).exists():
            sources.append({
                'index': wt.index,
                'feature_name': wt.feature_name,
                'path': wt.path,
                'is_main': False,
            })

    sources.sort(key=lambda x: x['index'])

    return templates.TemplateResponse(
        request,
        'clone_db.html',
        {
            'target': {
                'index': target.index,
                'feature_name': target.feature_name,
                'path': target.path,
            },
            'sources': sources,
            'docker_running': is_docker_running(),
        },
    )


async def clone_db_action(request: Request) -> Response:
    """Execute database cloning."""
    target_feature = request.path_params['target_feature']
    form = await request.form()
    source_index = form.get('source_index')

    if source_index is None or source_index == '':
        return JSONResponse({'error': 'Source index required'}, status_code=400)

    try:
        source_index = int(source_index)
    except ValueError:
        return JSONResponse({'error': 'Invalid source index'}, status_code=400)

    registry = read_registry()
    if not registry:
        return JSONResponse({'error': 'No registry found'}, status_code=400)

    target = registry.find_by_feature(target_feature)
    if not target:
        return JSONResponse({'error': f'Target worktree "{target_feature}" not found'}, status_code=404)

    # Find source
    if source_index == 0:
        source_path = registry.main_repo_path
        source_name = 'develop (main repo)'
    else:
        source = registry.find_by_index(source_index)
        if not source:
            return JSONResponse({'error': f'Source worktree #{source_index} not found'}, status_code=404)
        source_path = source.path
        source_name = source.feature_name

    # Find the clone script
    try:
        main_repo = get_main_repo_root()
        script_path = main_repo / 'scripts' / 'worktree-db-clone.sh'
    except Exception:
        script_path = None

    if not script_path or not script_path.exists():
        return JSONResponse({'error': 'Clone script not found'}, status_code=500)

    # Run the clone script with --yes flag
    try:
        result = subprocess.run(
            [str(script_path), '--yes', source_path, target.path],
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode == 0:
            return JSONResponse({
                'success': True,
                'message': f'Database cloned from {source_name} to {target.feature_name}',
                'output': result.stdout,
            })
        else:
            return JSONResponse(
                {
                    'success': False,
                    'error': 'Clone failed',
                    'output': result.stdout + result.stderr,
                },
                status_code=500,
            )

    except subprocess.TimeoutExpired:
        return JSONResponse(
            {'success': False, 'error': 'Clone timed out after 5 minutes'},
            status_code=500,
        )
    except Exception as e:
        logger.error(f'Clone failed: {e}')
        return JSONResponse({'success': False, 'error': str(e)}, status_code=500)
