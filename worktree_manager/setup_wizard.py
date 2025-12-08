"""
Interactive setup wizard for first-time users.

Guides users through initial configuration of worktree-manager.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from .config import Config, get_config

console = Console()


def run_setup_wizard() -> bool:
    """
    Run the interactive setup wizard.

    Returns:
        True if setup completed successfully, False if cancelled.
    """
    console.print()
    console.print(
        Panel.fit(
            '[bold blue]Welcome to Worktree Manager Setup[/bold blue]\n\n'
            'This wizard will help you configure the tool for first use.',
            border_style='blue',
        )
    )
    console.print()

    config = get_config()

    # Step 1: Base branch
    console.print('[bold]Step 1: Base Branch[/bold]')
    console.print('New feature branches will be created from this branch.')
    console.print()

    base_branch = Prompt.ask(
        'Base branch',
        default=config.base_branch,
    )
    config.base_branch = base_branch
    console.print()

    # Step 2: Ignore patterns
    console.print('[bold]Step 2: Branch Ignore Patterns[/bold]')
    console.print('These branch patterns will be hidden from listings.')
    console.print('Current patterns:')
    for pattern in config.ignore_patterns:
        console.print(f'  • {pattern}')
    console.print()

    if Confirm.ask('Would you like to modify ignore patterns?', default=False):
        _configure_ignore_patterns(config)
    console.print()

    # Step 3: Docker settings
    console.print('[bold]Step 3: Docker Settings[/bold]')
    console.print()

    stream_output = Confirm.ask(
        'Stream Docker build output in real-time?',
        default=config.get_docker_setting('stream_output', True),
    )
    config.set_docker_setting('stream_output', stream_output)

    auto_build = Confirm.ask(
        'Auto-build containers when starting work on a task?',
        default=config.get_docker_setting('auto_build', True),
    )
    config.set_docker_setting('auto_build', auto_build)
    console.print()

    # Step 4: Confirm and save
    console.print('[bold]Step 4: Review Configuration[/bold]')
    console.print()
    console.print(f'  Base branch: [cyan]{config.base_branch}[/cyan]')
    console.print(f'  Ignore patterns: [cyan]{len(config.ignore_patterns)} patterns[/cyan]')
    console.print(f'  Stream Docker output: [cyan]{stream_output}[/cyan]')
    console.print(f'  Auto-build containers: [cyan]{auto_build}[/cyan]')
    console.print()

    if Confirm.ask('Save this configuration?', default=True):
        config.setup_completed = True
        config.save()
        console.print()
        console.print('[green]✓ Configuration saved![/green]')
        console.print()
        console.print('You can run this wizard again with: [cyan]worktree-manager setup[/cyan]')
        console.print('Or edit the config directly at: [cyan]~/.config/dispatch-guru/config.json[/cyan]')
        return True
    else:
        console.print('[yellow]Setup cancelled. No changes saved.[/yellow]')
        return False


def _configure_ignore_patterns(config: Config) -> None:
    """Interactive configuration of ignore patterns."""
    while True:
        console.print()
        console.print('Current ignore patterns:')
        if config.ignore_patterns:
            for i, pattern in enumerate(config.ignore_patterns, 1):
                console.print(f'  {i}. {pattern}')
        else:
            console.print('  (none)')
        console.print()

        action = Prompt.ask(
            'Action',
            choices=['add', 'remove', 'done'],
            default='done',
        )

        if action == 'add':
            pattern = Prompt.ask('Enter pattern (e.g., dependabot/*)')
            if pattern:
                config.add_ignore_pattern(pattern)
                console.print(f'[green]Added: {pattern}[/green]')
        elif action == 'remove':
            if not config.ignore_patterns:
                console.print('[yellow]No patterns to remove[/yellow]')
                continue
            idx = Prompt.ask(
                'Enter number to remove',
                choices=[str(i) for i in range(1, len(config.ignore_patterns) + 1)],
            )
            pattern = config.ignore_patterns[int(idx) - 1]
            config.remove_ignore_pattern(pattern)
            console.print(f'[yellow]Removed: {pattern}[/yellow]')
        else:
            break


def check_first_run() -> bool:
    """
    Check if this is a first run and offer to run setup.

    Returns:
        True if setup was run, False otherwise.
    """
    config = get_config()

    if config.setup_completed:
        return False

    # Check if config file exists (user may have manually configured)
    config_file = Path.home() / '.config' / 'dispatch-guru' / 'config.json'
    if config_file.exists():
        return False

    console.print()
    console.print('[yellow]First time running worktree-manager![/yellow]')

    if Confirm.ask('Would you like to run the setup wizard?', default=True):
        return run_setup_wizard()
    else:
        # Mark as completed so we don't ask again
        config.setup_completed = True
        config.save()
        console.print('[dim]Skipped setup. Run [cyan]worktree-manager setup[/cyan] anytime.[/dim]')
        return False
