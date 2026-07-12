"""Tests for hooks config path alignment and legacy migration."""

from __future__ import annotations

from worktree_manager.hooks import config as cfg


def _point_at(monkeypatch, tmp_path):
    new_dir = tmp_path / 'worktree-manager'
    legacy_dir = tmp_path / 'dispatch-guru'
    legacy_dir.mkdir()
    monkeypatch.setattr(cfg, 'CONFIG_DIR', new_dir)
    monkeypatch.setattr(cfg, 'CONFIG_FILE', new_dir / 'hooks.json')
    monkeypatch.setattr(cfg, 'LEGACY_CONFIG_FILE', legacy_dir / 'hooks.json')
    return new_dir, legacy_dir


def test_migrates_legacy_hooks_config(tmp_path, monkeypatch):
    new_dir, legacy_dir = _point_at(monkeypatch, tmp_path)
    (legacy_dir / 'hooks.json').write_text('{"version": 1, "hooks": {"uv_sync": {"enabled": false}}}')

    loaded = cfg.HookConfig.load()

    assert (new_dir / 'hooks.json').exists()
    assert loaded.is_enabled('uv_sync') is False


def test_no_migration_when_new_exists(tmp_path, monkeypatch):
    new_dir, legacy_dir = _point_at(monkeypatch, tmp_path)
    new_dir.mkdir()
    (new_dir / 'hooks.json').write_text('{"version": 1, "hooks": {"uv_sync": {"enabled": true}}}')
    (legacy_dir / 'hooks.json').write_text('{"version": 1, "hooks": {"uv_sync": {"enabled": false}}}')

    loaded = cfg.HookConfig.load()

    # New file wins; legacy is not copied over it.
    assert loaded.is_enabled('uv_sync') is True


def test_no_legacy_no_error(tmp_path, monkeypatch):
    _point_at(monkeypatch, tmp_path)
    loaded = cfg.HookConfig.load()
    # Falls back to defaults, which enable shared_image.
    assert loaded.is_enabled('shared_image') is True
