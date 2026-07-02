"""Command-line interface for worktree manager."""

import argparse
import sys
from importlib.metadata import version

__version__ = version('worktree-manager')


# Global flags storage
class CLIContext:
    """Global CLI context for flags."""

    yes: bool = False
    non_interactive: bool = False
    quiet: bool = False

    @classmethod
    def reset(cls) -> None:
        """Reset to defaults."""
        cls.yes = False
        cls.non_interactive = False
        cls.quiet = False


def should_prompt() -> bool:
    """Check if we should prompt for user input."""
    return not CLIContext.yes and not CLIContext.non_interactive


def is_quiet() -> bool:
    """Check if we should minimize output."""
    return CLIContext.quiet


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog='worktree-manager',
        description='Git worktree management with Docker Compose isolation',
    )

    # Global flags
    parser.add_argument(
        '-y',
        '--yes',
        action='store_true',
        help='Skip all confirmation prompts',
    )
    parser.add_argument(
        '--non-interactive',
        action='store_true',
        help='Fail instead of prompting (for CI/CD)',
    )
    parser.add_argument(
        '-q',
        '--quiet',
        action='store_true',
        help='Minimal output',
    )
    parser.add_argument(
        '-V',
        '--version',
        action='version',
        version=f'%(prog)s {__version__}',
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # create command
    create_parser = subparsers.add_parser('create', help='Create a new worktree')
    create_parser.add_argument('feature', help="Feature branch name (e.g., 'my-feature')")
    create_parser.add_argument(
        '-t',
        '--type',
        choices=['feature', 'fix'],
        default='feature',
        help='Branch type prefix (default: feature)',
    )
    create_parser.add_argument(
        '--raw',
        action='store_true',
        help="Use branch name as-is without prefix (e.g., '003-my-branch')",
    )

    # list command
    subparsers.add_parser('list', help='List all worktrees')

    # status command
    subparsers.add_parser('status', help='Show status of current worktree')

    # close command
    close_parser = subparsers.add_parser('close', help='Close a worktree')
    close_parser.add_argument('message', help='Commit message')
    close_parser.add_argument(
        '--keep-volumes',
        action='store_true',
        help='Keep Docker volumes instead of removing them with the containers',
    )

    # cleanup-orphans command
    cleanup_parser = subparsers.add_parser('cleanup-orphans', help='Clean up orphaned resources')
    cleanup_parser.add_argument(
        '--dry-run',
        action='store_true',
        help='List orphaned resources without deleting anything',
    )

    # prune command
    subparsers.add_parser('prune', help='Remove worktrees from registry that no longer exist on disk')

    # load-db command
    load_db_parser = subparsers.add_parser('load-db', help='Load production database into worktree')
    load_db_parser.add_argument(
        'feature_name',
        nargs='?',
        help='Target worktree feature name (default: current directory)',
    )
    load_db_parser.add_argument(
        '--dump',
        action='store_true',
        help='Fetch fresh production dump (default: use cached)',
    )
    load_db_parser.add_argument(
        '--backup',
        action='store_true',
        help='Backup current local database before loading (default: skip)',
    )

    # update-last-accessed command
    subparsers.add_parser('update-last-accessed', help='Update last accessed timestamp')

    # web command
    web_parser = subparsers.add_parser('web', help='Start Kanban web interface')
    web_parser.add_argument('--port', type=int, default=8000, help='Port to run on (default: 8000)')
    web_parser.add_argument('--host', default='127.0.0.1', help='Host to bind to (default: 127.0.0.1)')

    # sync-tasks command (syncs tasks with worktree registry)
    subparsers.add_parser('sync-tasks', help='Sync tasks with worktree registry')

    # setup command
    subparsers.add_parser('setup', help='Run interactive setup wizard')

    # config command
    config_parser = subparsers.add_parser('config', help='Show or modify configuration')
    config_parser.add_argument('--add-ignore', metavar='PATTERN', help='Add branch ignore pattern')
    config_parser.add_argument('--remove-ignore', metavar='PATTERN', help='Remove branch ignore pattern')

    # init command
    init_parser = subparsers.add_parser('init', help='Initialize project configuration')
    init_parser.add_argument(
        '--from-legacy',
        action='store_true',
        help='Migrate from legacy dispatch-guru config',
    )

    args = parser.parse_args()

    # Set global context from flags
    CLIContext.yes = args.yes
    CLIContext.non_interactive = args.non_interactive
    CLIContext.quiet = args.quiet

    if not args.command:
        parser.print_help()
        return 0

    # Import command handlers
    from . import commands
    from .config import validate_branch_name

    try:
        if args.command == 'create':
            # Validate branch name before proceeding
            try:
                validate_branch_name(args.feature)
            except ValueError as e:
                print(f'Error: {e}', file=sys.stderr)
                return 1
            branch_type = None if args.raw else args.type
            return commands.create_worktree_cmd(args.feature, branch_type=branch_type)
        elif args.command == 'list':
            return commands.list_worktrees_cmd()
        elif args.command == 'status':
            return commands.show_status()
        elif args.command == 'close':
            return commands.close_worktree(args.message, keep_volumes=args.keep_volumes)
        elif args.command == 'cleanup-orphans':
            return commands.cleanup_orphans(dry_run=args.dry_run)
        elif args.command == 'prune':
            return commands.prune_missing_worktrees()
        elif args.command == 'load-db':
            return commands.load_db_cmd(
                feature_name=args.feature_name,
                do_dump=args.dump,
                do_backup=args.backup,
            )
        elif args.command == 'update-last-accessed':
            return commands.update_last_accessed()
        elif args.command == 'web':
            return commands.start_web(host=args.host, port=args.port)
        elif args.command == 'sync-tasks':
            return commands.sync_tasks_cmd()
        elif args.command == 'setup':
            return commands.setup_cmd()
        elif args.command == 'config':
            return commands.config_cmd(
                add_ignore=args.add_ignore,
                remove_ignore=args.remove_ignore,
            )
        elif args.command == 'init':
            return commands.init_cmd(from_legacy=args.from_legacy)
        else:
            parser.print_help()
            return 1
    except KeyboardInterrupt:
        print('\n\nInterrupted by user')
        return 130
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
