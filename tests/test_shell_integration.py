"""Tests for shell integration (`wt shell-init` + directory cd emission)."""

from __future__ import annotations

import pytest

from worktree_manager import commands


def test_emit_cd_target_writes_path_when_env_set(tmp_path, monkeypatch):
    cd_file = tmp_path / 'cd-target'
    monkeypatch.setenv(commands.CD_TARGET_ENV, str(cd_file))

    commands._emit_cd_target(tmp_path / 'worktrees' / 'foo')

    assert cd_file.read_text().strip() == str(tmp_path / 'worktrees' / 'foo')


def test_emit_cd_target_noop_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(commands.CD_TARGET_ENV, raising=False)
    # Should not raise and should not create any file.
    commands._emit_cd_target(tmp_path / 'foo')


def test_emit_cd_target_swallows_write_errors(tmp_path, monkeypatch):
    # Point at a path whose parent does not exist so the write fails.
    monkeypatch.setenv(commands.CD_TARGET_ENV, str(tmp_path / 'missing' / 'cd-target'))
    # Must not raise; failure is logged and ignored.
    commands._emit_cd_target(tmp_path / 'foo')


@pytest.mark.parametrize(
    ('shell', 'needle'),
    [
        ('bash', 'wt() {'),
        ('zsh', 'wt() {'),
        ('fish', 'function wt'),
    ],
)
def test_shell_init_emits_wrapper(shell, needle, capsys):
    rc = commands.shell_init_cmd(shell=shell)
    out = capsys.readouterr().out
    assert rc == 0
    assert needle in out
    assert commands.CD_TARGET_ENV in out


def test_shell_init_autodetects_from_env(monkeypatch, capsys):
    monkeypatch.setenv('SHELL', '/usr/bin/fish')
    assert commands.shell_init_cmd(shell=None) == 0
    assert 'function wt' in capsys.readouterr().out


def test_shell_init_rejects_unknown_shell():
    assert commands.shell_init_cmd(shell='powershell') == 1
