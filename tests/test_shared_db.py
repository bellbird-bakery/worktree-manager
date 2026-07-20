"""Tests for shared dev-database support (HANDOVER-dispatch-guru-shared-db.md)."""

from __future__ import annotations

import json

import pytest

from worktree_manager import commands, ports
from worktree_manager.config import ProjectConfig
from worktree_manager.docker_ops import PORT_ENV_SCRUB
from worktree_manager.registry import Registry, WorktreeEntry, WorktreePorts

# --- _set_env_var: the substring-clobber fix (item 1) -----------------------------


def test_set_env_var_replaces_existing_line():
    content = 'WEB_PORT=58000\nDB_PORT=5432\n'
    assert commands._set_env_var(content, 'DB_PORT', 5442) == 'WEB_PORT=58000\nDB_PORT=5442\n'


def test_set_env_var_appends_when_missing():
    assert commands._set_env_var('WEB_PORT=58000\n', 'REDIS_PORT', 6380) == 'WEB_PORT=58000\nREDIS_PORT=6380\n'


def test_set_env_var_appends_newline_when_content_unterminated():
    assert commands._set_env_var('WEB_PORT=58000', 'DB_PORT', 5442) == 'WEB_PORT=58000\nDB_PORT=5442\n'


def test_set_env_var_does_not_clobber_shared_db_port():
    """Rewriting DB_PORT must not touch SHARED_DB_PORT (it merely contains 'DB_PORT=')."""
    content = 'SHARED_DB_PORT=5432\nDB_PORT=5432\n'
    result = commands._set_env_var(content, 'DB_PORT', 5442)
    assert 'SHARED_DB_PORT=5432\n' in result
    assert 'DB_PORT=5442\n' in result
    # SHARED_DB_PORT stays global; only the standalone DB_PORT line changed.
    assert result == 'SHARED_DB_PORT=5432\nDB_PORT=5442\n'


def test_set_env_var_preserves_shared_db_port_when_only_that_is_present():
    """A straight .env.example copy carrying only SHARED_DB_PORT is left untouched by a DB_PORT add."""
    content = 'SHARED_DB_PORT=5432\n'
    result = commands._set_env_var(content, 'DB_PORT', 5442)
    assert result == 'SHARED_DB_PORT=5432\nDB_PORT=5442\n'


# --- _configure_env_ports: shared-DB projects skip the vestigial per-worktree DB_PORT ----


def test_configure_env_ports_writes_db_port_for_per_worktree_db():
    """Non-shared projects still get a per-index DB_PORT and a numeric DB summary."""
    content = 'WEB_PORT=8000\nDB_PORT=5432\nREDIS_PORT=6379\n'
    result, summary = commands._configure_env_ports(
        content, WorktreePorts(web=8010, db=5442, redis=6389), has_shared_db=False
    )
    assert 'WEB_PORT=8010\n' in result
    assert 'DB_PORT=5442\n' in result
    assert 'REDIS_PORT=6389\n' in result
    assert summary == 'WEB=8010, DB=5442, REDIS=6389'


def test_configure_env_ports_skips_db_port_for_shared_db():
    """Shared-DB projects must NOT rewrite DB_PORT to a bogus per-index value."""
    content = 'WEB_PORT=8000\nSHARED_DB_PORT=5432\nDB_PORT=5432\nREDIS_PORT=6379\n'
    result, summary = commands._configure_env_ports(
        content, WorktreePorts(web=8010, db=5442, redis=6389), has_shared_db=True
    )
    assert 'WEB_PORT=8010\n' in result
    assert 'REDIS_PORT=6389\n' in result
    # DB_PORT is left exactly as the template had it — never bumped to the vestigial 5442.
    assert 'DB_PORT=5442' not in result
    assert 'DB_PORT=5432\n' in result
    # SHARED_DB_PORT is the real port and stays global.
    assert 'SHARED_DB_PORT=5432\n' in result
    # Summary tells the user the DB is shared, not a fabricated port.
    assert summary == 'WEB=8010, DB=shared (SHARED_DB_PORT), REDIS=6389'


# --- shared_db_name derivation (item 6) -------------------------------------------


@pytest.mark.parametrize(
    ('project', 'expected'),
    [
        ('wt-foo-bar', 'wt_foo_bar'),
        ('WT-Foo', 'wt_foo'),
        ('plain', 'plain'),
    ],
)
def test_shared_db_name(project, expected):
    assert commands.shared_db_name(project) == expected


# --- ProjectConfig.shared_db accessors (item 7) -----------------------------------


def test_project_config_no_shared_db_by_default(tmp_path):
    cfg = ProjectConfig.load(repo_path=tmp_path)
    assert cfg.has_shared_db() is False
    assert cfg.get_shared_db_container() is None
    assert cfg.get_shared_db_ensure_command() is None
    assert cfg.get_shared_db_drop_command() is None
    # Env-var name still has a sane default even when the block is empty.
    assert cfg.get_shared_db_port_env() == 'SHARED_DB_PORT'


def test_project_config_reads_shared_db_block(tmp_path):
    (tmp_path / '.worktree-manager.json').write_text(
        json.dumps(
            {
                'project_name': 'dispatch-guru',
                'shared_db': {
                    'container_name': 'dg-shared-postgres',
                    'port_env_var': 'SHARED_DB_PORT',
                    'ensure_command': 'just db-ensure',
                    'drop_command': 'just db-drop-self',
                },
            }
        )
    )
    cfg = ProjectConfig.load(repo_path=tmp_path)
    assert cfg.has_shared_db() is True
    assert cfg.get_shared_db_container() == 'dg-shared-postgres'
    assert cfg.get_shared_db_ensure_command() == 'just db-ensure'
    assert cfg.get_shared_db_drop_command() == 'just db-drop-self'
    assert cfg.get_shared_db_port_env() == 'SHARED_DB_PORT'


def test_project_config_round_trips_shared_db(tmp_path):
    cfg = ProjectConfig.load(repo_path=tmp_path)
    cfg.shared_db = {'container_name': 'dg-shared-postgres'}
    cfg.save(tmp_path)
    reloaded = ProjectConfig.load(repo_path=tmp_path)
    assert reloaded.get_shared_db_container() == 'dg-shared-postgres'


# --- conflict checks skip the vestigial DB port when None (item 2) ----------------


def test_check_port_conflicts_skips_db_when_none(monkeypatch):
    seen = []

    def fake_in_use(port):
        seen.append(port)
        return (True, 'x', 1)  # everything "in use"

    monkeypatch.setattr(ports, 'is_port_in_use', fake_in_use)
    conflicts = ports.check_port_conflicts(58001, None, 6380)
    types = {c.port_type for c in conflicts}
    assert types == {'web', 'redis'}  # db skipped entirely
    assert 5432 not in seen


def test_check_registry_conflicts_skips_db_when_none(monkeypatch):
    reg = Registry(
        main_repo_path='/main',
        worktrees=[
            WorktreeEntry(
                path='/wt/a',
                compose_project_name='a',
                ports=WorktreePorts(web=58001, db=5432, redis=6380),
                feature_name='a',
                index=1,
            )
        ],
    )
    monkeypatch.setattr(ports, 'read_registry', lambda *_a, **_kw: reg)
    # db=None must not report the shared 5432 as a registry conflict.
    conflicts = ports.check_registry_conflicts(59999, None, 9999)
    assert conflicts == []
    # sanity: passing the db port does report it
    conflicts = ports.check_registry_conflicts(59999, 5432, 9999)
    assert [c.port_type for c in conflicts] == ['db']


# --- env scrub list (item 8) ------------------------------------------------------


def test_port_env_scrub_includes_shared_db_port():
    assert 'SHARED_DB_PORT' in PORT_ENV_SCRUB


# --- create fast path: _maybe_ensure_shared_db (item 4) ---------------------------


def _shared_cfg(tmp_path):
    (tmp_path / '.worktree-manager.json').write_text(
        json.dumps(
            {
                'project_name': 'dispatch-guru',
                'shared_db': {
                    'container_name': 'dg-shared-postgres',
                    'ensure_command': 'just db-ensure',
                    'drop_command': 'just db-drop-self',
                },
            }
        )
    )
    return ProjectConfig.load(repo_path=tmp_path)


def test_maybe_ensure_shared_db_noop_without_config(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(commands, '_run_worktree_command', lambda *a, **k: called.append(a) or (True, None))
    commands._maybe_ensure_shared_db(tmp_path, 'wt-foo', ProjectConfig.load(repo_path=tmp_path))
    assert called == []


def test_maybe_ensure_shared_db_runs_when_configured(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(commands, 'is_docker_running', lambda: True)
    monkeypatch.setattr(commands, '_run_worktree_command', lambda *a, **k: called.append(a) or (True, None))
    commands._maybe_ensure_shared_db(tmp_path, 'wt-foo', _shared_cfg(tmp_path))
    assert len(called) == 1
    assert called[0][2] == 'just db-ensure'


def test_maybe_ensure_shared_db_skips_when_auto_build_disabled(tmp_path, monkeypatch):
    from worktree_manager import config as config_mod

    called = []
    monkeypatch.setattr(commands, 'is_docker_running', lambda: True)
    monkeypatch.setattr(commands, '_run_worktree_command', lambda *a, **k: called.append(a) or (True, None))

    class FakeConfig:
        def get_docker_setting(self, key, default=None):
            return False if key == 'auto_build' else default

    monkeypatch.setattr(config_mod, 'get_config', lambda: FakeConfig())
    commands._maybe_ensure_shared_db(tmp_path, 'wt-foo', _shared_cfg(tmp_path))
    assert called == []


def test_maybe_ensure_shared_db_skips_when_docker_down(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(commands, 'is_docker_running', lambda: False)
    monkeypatch.setattr(commands, '_run_worktree_command', lambda *a, **k: called.append(a) or (True, None))
    commands._maybe_ensure_shared_db(tmp_path, 'wt-foo', _shared_cfg(tmp_path))
    assert called == []


# --- close: _maybe_drop_shared_db (item 6) ----------------------------------------


def _entry(tmp_path):
    return WorktreeEntry(
        path=str(tmp_path),
        compose_project_name='wt-foo-bar',
        ports=WorktreePorts(web=58001, db=5442, redis=6380),
        feature_name='foo-bar',
        index=1,
    )


def test_maybe_drop_shared_db_runs_when_configured(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(commands, 'is_docker_running', lambda: True)
    monkeypatch.setattr(commands, '_run_worktree_command', lambda *a, **k: called.append(a) or (True, None))
    commands._maybe_drop_shared_db(tmp_path, _entry(tmp_path), _shared_cfg(tmp_path), keep_volumes=False)
    assert len(called) == 1
    # runs the drop command with the worktree's compose project name
    assert called[0][1] == 'wt-foo-bar'
    assert called[0][2] == 'just db-drop-self'


def test_maybe_drop_shared_db_respects_keep_volumes(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(commands, 'is_docker_running', lambda: True)
    monkeypatch.setattr(commands, '_run_worktree_command', lambda *a, **k: called.append(a) or (True, None))
    commands._maybe_drop_shared_db(tmp_path, _entry(tmp_path), _shared_cfg(tmp_path), keep_volumes=True)
    assert called == []


def test_maybe_drop_shared_db_noop_without_config(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(commands, 'is_docker_running', lambda: True)
    monkeypatch.setattr(commands, '_run_worktree_command', lambda *a, **k: called.append(a) or (True, None))
    commands._maybe_drop_shared_db(
        tmp_path, _entry(tmp_path), ProjectConfig.load(repo_path=tmp_path), keep_volumes=False
    )
    assert called == []


# --- validator awareness (item 7 / wt status) -------------------------------------


def test_validator_shared_db_checks_shared_port_not_db_port(tmp_path, monkeypatch):
    from worktree_manager import validator

    (tmp_path / '.worktree-manager.json').write_text(
        json.dumps({'project_name': 'dispatch-guru', 'shared_db': {'container_name': 'dg-shared-postgres'}})
    )
    (tmp_path / '.env').write_text(
        'WEB_PORT=58001\nREDIS_PORT=6380\nSHARED_DB_PORT=5432\n'
        'POSTGRES_HOST=dg-shared-postgres\nPOSTGRES_NAME=wt_x\nPOSTGRES_USER=dg\nPOSTGRES_PASSWORD=secret\n'
    )
    (tmp_path / 'docker-compose.local.yml').write_text('services: {}\n')

    # Avoid touching the host: no real docker / socket / registry probing.
    monkeypatch.setattr(validator, 'is_docker_running', lambda: True)
    monkeypatch.setattr(validator, 'check_port_conflicts', lambda *a, **k: [])
    monkeypatch.setattr(validator, 'check_registry_conflicts', lambda *a, **k: [])
    monkeypatch.setattr(validator, 'read_registry', lambda *_a, **_kw: None)

    report = validator.validate_worktree(str(tmp_path), strict_ports=True)
    names = {r.name for r in report.results}

    # SHARED_DB_PORT is validated; the vestigial DB_PORT is not required/flagged.
    assert 'SHARED_DB_PORT' in names
    assert 'DB_PORT' not in names
    assert 'Default DB_PORT' not in names
    assert not report.has_errors
