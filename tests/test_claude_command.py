"""Tests for the `claude` CLI command helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from worktree_manager import commands
from worktree_manager.registry import Registry, WorktreeEntry, WorktreePorts


def make_registry(tmp_path: Path) -> Registry:
    wt_path = tmp_path / 'worktrees' / 'auth-fix'
    wt_path.mkdir(parents=True)
    entry = WorktreeEntry(
        path=str(wt_path),
        compose_project_name='auth-fix',
        ports=WorktreePorts(web=8010, db=5442),
        feature_name='auth-fix',
        index=1,
    )
    return Registry(main_repo_path=str(tmp_path / 'main'), worktrees=[entry])


def test_resolve_worktree_by_feature_name(tmp_path):
    registry = make_registry(tmp_path)
    entry = commands.resolve_worktree(registry, feature_name='auth-fix')
    assert entry is not None
    assert entry.feature_name == 'auth-fix'


def test_resolve_worktree_unknown_feature_returns_none(tmp_path):
    registry = make_registry(tmp_path)
    assert commands.resolve_worktree(registry, feature_name='nope') is None


def test_resolve_worktree_from_cwd_subdirectory(tmp_path):
    registry = make_registry(tmp_path)
    subdir = Path(registry.worktrees[0].path) / 'src' / 'app'
    subdir.mkdir(parents=True)
    entry = commands.resolve_worktree(registry, cwd=subdir)
    assert entry is not None
    assert entry.feature_name == 'auth-fix'


def test_resolve_worktree_from_unrelated_cwd_returns_none(tmp_path):
    registry = make_registry(tmp_path)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    assert commands.resolve_worktree(registry, cwd=elsewhere) is None


def test_build_claude_argv_default():
    assert commands.build_claude_argv() == ['claude']


def test_build_claude_argv_continue_and_extra_args():
    argv = commands.build_claude_argv(continue_session=True, extra_args=['-p', 'fix the bug'])
    assert argv == ['claude', '--continue', '-p', 'fix the bug']


def test_claude_cmd_unknown_feature_returns_error(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    monkeypatch.setattr(commands, 'read_registry', lambda *_a, **_kw: registry)
    assert commands.claude_cmd(feature_name='nope') == 1


def test_claude_cmd_no_registry_returns_error(monkeypatch):
    monkeypatch.setattr(commands, 'read_registry', lambda *_a, **_kw: None)
    assert commands.claude_cmd(feature_name='auth-fix') == 1


def test_claude_cmd_missing_claude_binary_returns_error(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    monkeypatch.setattr(commands, 'read_registry', lambda *_a, **_kw: registry)
    monkeypatch.setattr(commands.shutil, 'which', lambda name: None)
    assert commands.claude_cmd(feature_name='auth-fix') == 1


def test_claude_cmd_execs_claude_in_worktree(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    monkeypatch.setattr(commands, 'read_registry', lambda *_a, **_kw: registry)
    monkeypatch.setattr(commands.shutil, 'which', lambda name: '/usr/bin/claude')

    calls: dict = {}
    monkeypatch.setattr(commands.os, 'chdir', lambda path: calls.setdefault('chdir', path))
    monkeypatch.setattr(commands.os, 'execvp', lambda prog, argv: calls.setdefault('exec', (prog, argv)))

    result = commands.claude_cmd(feature_name='auth-fix', continue_session=True)

    assert result == 0
    assert str(calls['chdir']) == registry.worktrees[0].path
    assert calls['exec'] == ('claude', ['claude', '--continue'])


def test_claude_cmd_missing_worktree_directory_returns_error(tmp_path, monkeypatch):
    registry = make_registry(tmp_path)
    registry.worktrees[0].path = str(tmp_path / 'gone')
    monkeypatch.setattr(commands, 'read_registry', lambda *_a, **_kw: registry)
    monkeypatch.setattr(commands.shutil, 'which', lambda name: '/usr/bin/claude')
    assert commands.claude_cmd(feature_name='auth-fix') == 1


@pytest.mark.parametrize('args', [['claude'], ['claude', 'auth-fix']])
def test_cli_parses_claude_command(args):
    from worktree_manager.cli import build_parser

    parser = build_parser()
    parsed = parser.parse_args(args)
    assert parsed.command == 'claude'


def test_cli_claude_continue_flag_before_feature():
    from worktree_manager.cli import build_parser

    parsed = build_parser().parse_args(['claude', '-c', 'auth-fix'])
    assert parsed.continue_session is True
    assert parsed.feature == 'auth-fix'
    assert parsed.claude_args == []


def test_cli_claude_args_after_feature_pass_through():
    from worktree_manager.cli import build_parser

    parsed = build_parser().parse_args(['claude', 'auth-fix', '-p', 'fix the bug'])
    assert parsed.continue_session is False
    assert parsed.feature == 'auth-fix'
    assert parsed.claude_args == ['-p', 'fix the bug']
