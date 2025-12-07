"""
Web interface for worktree manager.

A lightweight Starlette-based web UI for the Kanban board and worktree management.
This module is Django-free.
"""

from .app import create_app, run_server

__all__ = ['create_app', 'run_server']
