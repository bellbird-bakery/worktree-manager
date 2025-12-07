"""
Starlette application for worktree manager web interface.

Provides:
- Kanban board for task management
- Worktree list and management
- Database cloning interface
- Sync status and conflict resolution
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import routes, api

logger = logging.getLogger('worktree_manager')

# Paths
PACKAGE_DIR = Path(__file__).parent.parent
TEMPLATES_DIR = PACKAGE_DIR / 'templates'
STATIC_DIR = PACKAGE_DIR / 'static'

# Configure Jinja2 templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ==============================================================================
# Custom Jinja2 Filters (Django compatibility)
# ==============================================================================


def date_filter(value, format_str='M j, H:i'):
    """
    Format a date/datetime using Django-like format codes.

    Supports: M (abbrev month), j (day), H (24-hour), i (minute), s (second).
    """
    if value is None:
        return ''

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value

    # Map Django format codes to Python strftime
    format_map = {
        'M': '%b',      # Abbreviated month (Jan, Feb, etc.)
        'j': '%-d',     # Day without leading zero
        'd': '%d',      # Day with leading zero
        'H': '%H',      # 24-hour
        'i': '%M',      # Minute
        's': '%S',      # Second
        'Y': '%Y',      # 4-digit year
        'y': '%y',      # 2-digit year
    }

    py_format = format_str
    for django_code, py_code in format_map.items():
        py_format = py_format.replace(django_code, py_code)

    try:
        return value.strftime(py_format)
    except Exception:
        return str(value)


def timesince_filter(value):
    """Return a human-readable time difference (e.g., '2 hours ago')."""
    if value is None:
        return ''

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return ''

    now = datetime.now(value.tzinfo) if value.tzinfo else datetime.now()
    diff = now - value

    seconds = diff.total_seconds()

    if seconds < 60:
        return 'just now'
    elif seconds < 3600:
        mins = int(seconds / 60)
        return f'{mins} minute{"s" if mins != 1 else ""}'
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f'{hours} hour{"s" if hours != 1 else ""}'
    elif seconds < 604800:
        days = int(seconds / 86400)
        return f'{days} day{"s" if days != 1 else ""}'
    elif seconds < 2592000:
        weeks = int(seconds / 604800)
        return f'{weeks} week{"s" if weeks != 1 else ""}'
    else:
        months = int(seconds / 2592000)
        return f'{months} month{"s" if months != 1 else ""}'


def truncatewords_filter(value, count=20):
    """Truncate text to a specific number of words."""
    if value is None:
        return ''
    words = str(value).split()
    if len(words) <= count:
        return value
    return ' '.join(words[:count]) + '...'


def pluralize_filter(value, suffix='s'):
    """Return plural suffix if value is not 1."""
    try:
        count = int(value)
    except (ValueError, TypeError):
        return ''
    return '' if count == 1 else suffix


def add_filter(value, arg):
    """Add two values together."""
    try:
        return int(value) + int(arg)
    except (ValueError, TypeError):
        return value


# Register custom filters
templates.env.filters['date'] = date_filter
templates.env.filters['timesince'] = timesince_filter
templates.env.filters['truncatewords'] = truncatewords_filter
templates.env.filters['pluralize'] = pluralize_filter
templates.env.filters['add'] = add_filter


# Global CSRF token for templates (simple approach for standalone app)
def generate_csrf_token():
    """Generate a simple CSRF token."""
    return secrets.token_hex(32)


templates.env.globals['csrf_token'] = generate_csrf_token()


def create_app() -> Starlette:
    """Create and configure the Starlette application."""
    # Ensure task database is initialized
    from ..task_store import ensure_db
    ensure_db()

    # Define routes
    app_routes = [
        # Kanban board (home)
        Route('/', routes.kanban_board, name='kanban'),
        Route('/tasks/{pk:int}/move/', routes.move_task, methods=['POST'], name='move_task'),
        Route('/tasks/{pk:int}/confirm/', routes.confirm_hook, methods=['POST'], name='confirm_hook'),

        # Worktree management
        Route('/worktrees/', routes.worktree_list, name='worktree_list'),
        Route('/worktrees/create/', routes.worktree_create, methods=['GET', 'POST'], name='worktree_create'),
        Route('/worktrees/{feature_name}/status/', routes.worktree_status, name='worktree_status'),
        Route('/worktrees/{feature_name}/close/', routes.worktree_close, methods=['POST'], name='worktree_close'),

        # Sync status and actions
        Route('/sync/', routes.sync_status_view, name='sync_status'),
        Route('/sync/pull/', routes.sync_pull, methods=['POST'], name='sync_pull'),
        Route('/sync/push/', routes.sync_push, methods=['POST'], name='sync_push'),

        # Conflict resolution
        Route('/conflicts/', routes.conflict_list, name='conflict_list'),
        Route('/conflicts/{feature_name}/', routes.conflict_detail, name='conflict_detail'),
        Route('/conflicts/{feature_name}/resolve/', routes.conflict_resolve, methods=['POST'], name='conflict_resolve'),

        # Database cloning
        Route('/worktrees/{target_feature}/clone-db/', routes.clone_db_form, name='clone_db_form'),
        Route('/worktrees/{target_feature}/clone-db/run/', routes.clone_db_action, methods=['POST'], name='clone_db_action'),

        # API routes
        Route('/api/sync/', api.sync_tasks, methods=['POST'], name='api_sync'),
        Route('/api/tasks/', api.list_tasks, name='api_tasks'),
        Route('/api/tasks/{pk:int}/', api.get_task, name='api_task_detail'),
        Route('/api/tasks/{pk:int}/status/', api.update_task_status, methods=['POST'], name='api_task_status'),

        # Static files
        Mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static'),
    ]

    app = Starlette(
        debug=os.getenv('DEBUG', 'false').lower() == 'true',
        routes=app_routes,
    )

    # Store templates in app state for access in routes
    app.state.templates = templates

    return app


def run_server(host: str = '127.0.0.1', port: int = 8000) -> None:
    """Run the web server using uvicorn."""
    import uvicorn

    # Sync from JSON on startup
    try:
        from ..sync import sync_json_to_sqlite
        result = sync_json_to_sqlite()
        if result.created or result.updated:
            logger.info(f'Synced from JSON: {result.created} created, {result.updated} updated')
    except Exception as e:
        logger.warning(f'JSON sync on startup failed: {e}')

    logger.info(f'Starting web server at http://{host}:{port}')
    uvicorn.run(
        'worktree_manager.web:create_app',
        host=host,
        port=port,
        reload=False,
        factory=True,
        log_level='info',
    )


# For uvicorn direct run: uvicorn worktree_manager.web.app:app
app = create_app()
