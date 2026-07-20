"""Tests for per-worktree Redis logical DB allocation (replaces REDIS_PORT).

One shared Redis server is isolated per worktree by logical DB number rather than
a per-worktree published port. Each worktree gets two DBs: (broker, cache/channels).
"""

from __future__ import annotations

import json

import pytest

from worktree_manager import commands
from worktree_manager.config import ProjectConfig
from worktree_manager.ports import redis_dbs_for_index
from worktree_manager.registry import WorktreePorts

# --- redis_dbs_for_index: two logical DBs per worktree ----------------------------


def test_main_index_is_zero_one():
    assert redis_dbs_for_index(0) == (0, 1)


def test_worktree_index_offsets():
    assert redis_dbs_for_index(1) == (2, 3)
    assert redis_dbs_for_index(5) == (10, 11)


@pytest.mark.parametrize('index', range(0, 8))
def test_broker_cache_never_collide_across_worktrees(index):
    broker, cache = redis_dbs_for_index(index)
    assert cache == broker + 1
    # No worktree's DBs overlap another's: index N owns {2N, 2N+1}.
    assert broker == 2 * index


# --- ProjectConfig.shared_redis accessors -----------------------------------------


def test_project_config_no_shared_redis_by_default(tmp_path):
    cfg = ProjectConfig.load(repo_path=tmp_path)
    assert cfg.has_shared_redis() is False
    assert cfg.get_shared_redis_container() is None
    assert cfg.get_shared_redis_ensure_command() is None
    assert cfg.get_shared_redis_flush_command() is None
    # Env-var name still has a sane default even when the block is empty.
    assert cfg.get_shared_redis_port_env() == 'SHARED_REDIS_PORT'


def test_project_config_reads_shared_redis_block(tmp_path):
    (tmp_path / '.worktree-manager.json').write_text(
        json.dumps(
            {
                'project_name': 'dispatch-guru',
                'shared_redis': {
                    'container_name': 'dg-shared-redis',
                    'port_env_var': 'SHARED_REDIS_PORT',
                    'ensure_command': 'just redis-shared-up',
                    'flush_command': 'just redis-flush-self',
                },
            }
        )
    )
    cfg = ProjectConfig.load(repo_path=tmp_path)
    assert cfg.has_shared_redis() is True
    assert cfg.get_shared_redis_container() == 'dg-shared-redis'
    assert cfg.get_shared_redis_ensure_command() == 'just redis-shared-up'
    assert cfg.get_shared_redis_flush_command() == 'just redis-flush-self'
    assert cfg.get_shared_redis_port_env() == 'SHARED_REDIS_PORT'


def test_project_config_round_trips_shared_redis(tmp_path):
    cfg = ProjectConfig.load(repo_path=tmp_path)
    cfg.shared_redis = {'container_name': 'dg-shared-redis'}
    cfg.save(tmp_path)
    reloaded = ProjectConfig.load(repo_path=tmp_path)
    assert reloaded.get_shared_redis_container() == 'dg-shared-redis'


# --- _configure_env_ports: shared-Redis writes DB indices, not REDIS_PORT ----------


def test_configure_env_ports_writes_redis_dbs_for_shared_redis():
    content = 'WEB_PORT=8000\nSHARED_DB_PORT=5432\nREDIS_PORT=6379\n'
    result, summary = commands._configure_env_ports(
        content,
        WorktreePorts(web=8010, db=5442, redis=6389),
        has_shared_db=True,
        has_shared_redis=True,
        index=3,
    )
    assert 'WEB_PORT=8010\n' in result
    # index 3 -> (6, 7)
    assert 'REDIS_BROKER_DB=6\n' in result
    assert 'REDIS_CACHE_DB=7\n' in result
    # The retired per-worktree REDIS_PORT is removed entirely.
    assert 'REDIS_PORT' not in result
    assert summary == 'WEB=8010, DB=shared (SHARED_DB_PORT), REDIS=shared (DB 6/7)'


def test_configure_env_ports_shared_redis_neutralises_stale_url():
    content = 'WEB_PORT=8000\nREDIS_PORT=6379\nCELERY_BROKER_URL=redis://redis:6379/0\nREDIS_URL=redis://redis:6379/1\n'
    result, _ = commands._configure_env_ports(
        content,
        WorktreePorts(web=8010, db=5442, redis=6389),
        has_shared_db=True,
        has_shared_redis=True,
        index=1,
    )
    assert '# CELERY_BROKER_URL=redis://redis:6379/0\n' in result
    assert '# REDIS_URL=redis://redis:6379/1\n' in result
    assert 'REDIS_BROKER_DB=2\n' in result
    assert 'REDIS_CACHE_DB=3\n' in result


def test_configure_env_ports_keeps_redis_port_when_not_shared():
    """Default (per-worktree Redis) behaviour is unchanged: REDIS_PORT is written."""
    content = 'WEB_PORT=8000\nDB_PORT=5432\nREDIS_PORT=6379\n'
    result, summary = commands._configure_env_ports(
        content, WorktreePorts(web=8010, db=5442, redis=6389), has_shared_db=False
    )
    assert 'REDIS_PORT=6389\n' in result
    assert 'REDIS_BROKER_DB' not in result
    assert summary == 'WEB=8010, DB=5442, REDIS=6389'


# --- _remove_env_var: line-anchored deletion --------------------------------------


def test_remove_env_var_deletes_line():
    assert commands._remove_env_var('WEB_PORT=8000\nREDIS_PORT=6379\n', 'REDIS_PORT') == 'WEB_PORT=8000\n'


def test_remove_env_var_does_not_touch_substring_key():
    """Removing REDIS_PORT must not remove SHARED_REDIS_PORT."""
    content = 'SHARED_REDIS_PORT=6379\nREDIS_PORT=6380\n'
    assert commands._remove_env_var(content, 'REDIS_PORT') == 'SHARED_REDIS_PORT=6379\n'


def test_remove_env_var_missing_is_noop():
    assert commands._remove_env_var('WEB_PORT=8000\n', 'REDIS_PORT') == 'WEB_PORT=8000\n'


# --- _comment_out_stale_redis_urls: only the CI-only `redis` host -----------------


def test_comment_out_stale_redis_urls_comments_redis_host():
    content = 'CELERY_BROKER_URL=redis://redis:6379/0\nREDIS_URL=redis://redis:6379/1\n'
    result = commands._comment_out_stale_redis_urls(content)
    assert result == '# CELERY_BROKER_URL=redis://redis:6379/0\n# REDIS_URL=redis://redis:6379/1\n'


def test_comment_out_stale_redis_urls_preserves_other_hosts():
    """Overrides not using the `redis` hostname must be preserved (they still work)."""
    content = 'CELERY_BROKER_URL=redis://localhost:6379/0\nREDIS_URL=redis://host.docker.internal:6379/1\n'
    assert commands._comment_out_stale_redis_urls(content) == content


def test_comment_out_stale_redis_urls_idempotent():
    content = '# CELERY_BROKER_URL=redis://redis:6379/0\n'
    # An already-commented line is not double-commented.
    assert commands._comment_out_stale_redis_urls(content) == content


def test_comment_out_leaves_result_backend_alone():
    """CELERY_RESULT_BACKEND is dead (settings hardcode django-db); never touched."""
    content = 'CELERY_RESULT_BACKEND=redis://redis:6379/2\n'
    assert commands._comment_out_stale_redis_urls(content) == content


# --- _migrate_env_redis: the backfill/migration transform -------------------------


def test_migrate_env_redis_full_transform():
    content = 'WEB_PORT=58001\nREDIS_PORT=6380\nSHARED_REDIS_PORT=6379\nCELERY_BROKER_URL=redis://redis:6379/0\n'
    result = commands._migrate_env_redis(content, 4, 5)
    assert '\nREDIS_PORT=' not in result and not result.startswith('REDIS_PORT=')
    assert 'SHARED_REDIS_PORT=6379\n' in result  # global, preserved
    assert 'REDIS_BROKER_DB=4\n' in result
    assert 'REDIS_CACHE_DB=5\n' in result
    assert '# CELERY_BROKER_URL=redis://redis:6379/0\n' in result


def test_migrate_env_redis_idempotent():
    content = 'WEB_PORT=58001\nREDIS_PORT=6380\n'
    once = commands._migrate_env_redis(content, 2, 3)
    twice = commands._migrate_env_redis(once, 2, 3)
    assert once == twice


# --- validator: shared-Redis validates SHARED_REDIS_PORT, not REDIS_PORT -----------


def test_validator_shared_redis_checks_shared_port_not_redis_port(tmp_path, monkeypatch):
    from worktree_manager import validator

    (tmp_path / '.worktree-manager.json').write_text(
        json.dumps(
            {
                'project_name': 'dispatch-guru',
                'shared_db': {'container_name': 'dg-shared-postgres'},
                'shared_redis': {'container_name': 'dg-shared-redis'},
            }
        )
    )
    (tmp_path / '.env').write_text(
        'WEB_PORT=58001\nSHARED_DB_PORT=5432\nSHARED_REDIS_PORT=6379\n'
        'REDIS_BROKER_DB=2\nREDIS_CACHE_DB=3\n'
        'POSTGRES_HOST=dg-shared-postgres\nPOSTGRES_NAME=wt_x\nPOSTGRES_USER=dg\nPOSTGRES_PASSWORD=secret\n'
    )
    (tmp_path / 'docker-compose.local.yml').write_text('services: {}\n')

    monkeypatch.setattr(validator, 'is_docker_running', lambda: True)
    monkeypatch.setattr(validator, 'check_port_conflicts', lambda *a, **k: [])
    monkeypatch.setattr(validator, 'check_registry_conflicts', lambda *a, **k: [])
    monkeypatch.setattr(validator, 'read_registry', lambda *_a, **_kw: None)

    report = validator.validate_worktree(str(tmp_path), strict_ports=True)
    names = {r.name for r in report.results}

    # SHARED_REDIS_PORT is validated; the retired REDIS_PORT is not required/flagged.
    assert 'SHARED_REDIS_PORT' in names
    assert 'REDIS_PORT' not in names
    assert not report.has_errors
