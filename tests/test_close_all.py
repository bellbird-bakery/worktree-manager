"""Tests for `wt close-all` bulk teardown.

Covers the selection gate (`_classify_worktrees`) and the per-worktree teardown
(`_teardown_worktree`) in isolation, mocking git/docker so nothing real runs.
"""

from __future__ import annotations

import pytest

from worktree_manager import commands
from worktree_manager.config import ProjectConfig
from worktree_manager.registry import WorktreeEntry, WorktreePorts


def _entry(feature_name, index, path=None):
    return WorktreeEntry(
        path=path or f'/wt/{feature_name}',
        compose_project_name=f'proj-{feature_name}',
        ports=WorktreePorts(web=8000 + index * 10, db=5432 + index * 10, redis=6379 + index),
        feature_name=feature_name,
        index=index,
    )


@pytest.fixture
def clean_merged_pushed(monkeypatch):
    """Default git state: every worktree is merged, clean, and fully pushed."""
    monkeypatch.setattr(commands, 'get_branch_for_path', lambda path: f'feature/{path.rsplit("/", 1)[-1]}')
    monkeypatch.setattr(commands, 'is_branch_merged', lambda branch, base, repo_path=None: True)
    monkeypatch.setattr(commands, 'has_uncommitted_changes_in_path', lambda path: False)
    monkeypatch.setattr(commands, 'has_unpushed_commits', lambda path: False)


# --- _classify_worktrees: the merged/clean/pushed selection gate -------------------


def test_classify_closes_merged_clean_pushed(clean_merged_pushed):
    wts = [_entry('a', 1), _entry('b', 2)]
    to_close, skipped = commands._classify_worktrees(wts, base_branch='develop', force=False)
    assert [wt.feature_name for wt in to_close] == ['a', 'b']
    assert skipped == []


def test_classify_skips_unmerged(clean_merged_pushed, monkeypatch):
    monkeypatch.setattr(commands, 'is_branch_merged', lambda branch, base, repo_path=None: False)
    to_close, skipped = commands._classify_worktrees([_entry('a', 1)], base_branch='develop', force=False)
    assert to_close == []
    assert [wt.feature_name for wt, _ in skipped] == ['a']
    assert skipped[0][1] == ['unmerged']


def test_classify_skips_dirty(clean_merged_pushed, monkeypatch):
    monkeypatch.setattr(commands, 'has_uncommitted_changes_in_path', lambda path: True)
    to_close, skipped = commands._classify_worktrees([_entry('a', 1)], base_branch='develop', force=False)
    assert to_close == []
    assert skipped[0][1] == ['dirty']


def test_classify_skips_unpushed(clean_merged_pushed, monkeypatch):
    monkeypatch.setattr(commands, 'has_unpushed_commits', lambda path: True)
    to_close, skipped = commands._classify_worktrees([_entry('a', 1)], base_branch='develop', force=False)
    assert to_close == []
    assert skipped[0][1] == ['unpushed']


def test_classify_combines_reasons_in_order(clean_merged_pushed, monkeypatch):
    monkeypatch.setattr(commands, 'is_branch_merged', lambda branch, base, repo_path=None: False)
    monkeypatch.setattr(commands, 'has_uncommitted_changes_in_path', lambda path: True)
    monkeypatch.setattr(commands, 'has_unpushed_commits', lambda path: True)
    _, skipped = commands._classify_worktrees([_entry('a', 1)], base_branch='develop', force=False)
    assert skipped[0][1] == ['unmerged', 'dirty', 'unpushed']


def test_classify_force_closes_everything(clean_merged_pushed, monkeypatch):
    # Even a worktree that fails every gate is closed under --force.
    monkeypatch.setattr(commands, 'is_branch_merged', lambda branch, base, repo_path=None: False)
    monkeypatch.setattr(commands, 'has_uncommitted_changes_in_path', lambda path: True)
    monkeypatch.setattr(commands, 'has_unpushed_commits', lambda path: True)
    to_close, skipped = commands._classify_worktrees(
        [_entry('a', 1), _entry('b', 2)], base_branch='develop', force=True
    )
    assert [wt.feature_name for wt in to_close] == ['a', 'b']
    assert skipped == []


def test_classify_partitions_mixed_set(clean_merged_pushed, monkeypatch):
    # 'b' is dirty; 'a' and 'c' are clean/merged/pushed.
    monkeypatch.setattr(commands, 'has_uncommitted_changes_in_path', lambda path: path.endswith('/b'))
    to_close, skipped = commands._classify_worktrees(
        [_entry('a', 1), _entry('b', 2), _entry('c', 3)], base_branch='develop', force=False
    )
    assert [wt.feature_name for wt in to_close] == ['a', 'c']
    assert [wt.feature_name for wt, _ in skipped] == ['b']


# --- _teardown_worktree: the shared per-worktree teardown --------------------------


@pytest.fixture
def teardown_spies(monkeypatch):
    """Stub every side-effecting action _teardown_worktree performs; record the calls."""
    calls = {'compose_down': [], 'drop_db': [], 'flush': [], 'removed': []}
    monkeypatch.setattr(commands, 'is_docker_running', lambda: True)
    monkeypatch.setattr(commands, 'fix_permissions', lambda path, proj: True)
    monkeypatch.setattr(
        commands, 'compose_down', lambda path, proj, volumes=False: calls['compose_down'].append((proj, volumes))
    )
    monkeypatch.setattr(commands, 'remove_worktree', lambda path, force=False: calls['removed'].append(path))
    monkeypatch.setattr(commands, 'has_uncommitted_changes_in_path', lambda path: False)
    monkeypatch.setattr(
        commands,
        '_maybe_drop_shared_db',
        lambda path, entry, cfg, keep_volumes: calls['drop_db'].append(entry.compose_project_name),
    )
    monkeypatch.setattr(
        commands,
        '_run_worktree_command',
        lambda path, proj, cmd: calls['flush'].append(cmd) or (True, None),
    )
    return calls


def _cfg(shared_redis=None):
    cfg = ProjectConfig.load(repo_path='/does/not/matter')
    if shared_redis is not None:
        cfg.shared_redis = shared_redis
    return cfg


def test_teardown_no_prune_keeps_volumes_and_data(teardown_spies):
    cfg = _cfg(shared_redis={'flush_command': 'just redis-flush-self'})
    result = commands._teardown_worktree(_entry('a', 1), cfg, prune=False)
    assert result.ok
    assert teardown_spies['compose_down'] == [('proj-a', False)]
    assert teardown_spies['drop_db'] == []  # DB not dropped without --prune
    assert teardown_spies['flush'] == []  # Redis not flushed without --prune
    assert teardown_spies['removed'] == ['/wt/a']


def test_teardown_prune_removes_volumes_drops_db_and_flushes_redis(teardown_spies):
    cfg = _cfg(shared_redis={'flush_command': 'just redis-flush-self'})
    result = commands._teardown_worktree(_entry('a', 1), cfg, prune=True)
    assert result.ok
    assert teardown_spies['compose_down'] == [('proj-a', True)]
    assert teardown_spies['drop_db'] == ['proj-a']
    assert teardown_spies['flush'] == ['just redis-flush-self']
    assert teardown_spies['removed'] == ['/wt/a']


def test_teardown_prune_without_flush_command_skips_flush(teardown_spies):
    cfg = _cfg(shared_redis={})  # no flush_command configured
    commands._teardown_worktree(_entry('a', 1), cfg, prune=True)
    assert teardown_spies['flush'] == []
    assert teardown_spies['drop_db'] == ['proj-a']  # DB drop still happens


def test_teardown_discards_dirty_worktree_without_committing(teardown_spies, monkeypatch):
    # A dirty worktree (reachable only under --force) is deleted, discarding its
    # uncommitted changes — it is NOT auto-committed.
    committed = []
    monkeypatch.setattr(commands, 'commit_all', lambda msg: committed.append(msg) or True)
    monkeypatch.setattr(commands, 'has_uncommitted_changes_in_path', lambda path: True)
    result = commands._teardown_worktree(_entry('a', 1), _cfg(), prune=False)
    assert result.ok
    assert committed == []  # no commit made
    assert teardown_spies['removed'] == ['/wt/a']  # removed anyway


def test_teardown_reports_error_and_does_not_raise(teardown_spies, monkeypatch):
    def boom(path, force=False):
        raise commands.GitError('worktree busy')

    monkeypatch.setattr(commands, 'remove_worktree', boom)
    result = commands._teardown_worktree(_entry('a', 1), _cfg(), prune=False)
    assert not result.ok
    assert 'worktree busy' in result.error


# --- close_all_cmd: orchestration -------------------------------------------------


class _FakeRegistry:
    def __init__(self, worktrees):
        self.worktrees = list(worktrees)
        self.removed = []

    def remove_worktree(self, path):
        self.removed.append(path)


@pytest.fixture
def orchestrator_env(monkeypatch, clean_merged_pushed):
    """Wire close_all_cmd to run in 'main repo', with a spied-on teardown."""
    torn_down = []

    def fake_teardown(entry, project_config, *, prune):
        torn_down.append((entry.feature_name, prune))
        return commands.TeardownResult(feature_name=entry.feature_name, ok=True)

    from pathlib import Path

    # In the main repo, current root == main repo root.
    monkeypatch.setattr(commands, 'get_repo_root', lambda: Path('/main'))
    monkeypatch.setattr(commands, 'get_main_repo_root', lambda: Path('/main'))
    monkeypatch.setattr(commands, '_teardown_worktree', fake_teardown)

    from worktree_manager import config as config_mod

    monkeypatch.setattr(config_mod, 'get_project_config', lambda path=None: _cfg())
    monkeypatch.setattr(commands, 'get_project_config', lambda path=None: _cfg(), raising=False)
    return torn_down


def _install_registry(monkeypatch, registry):
    import contextlib

    monkeypatch.setattr(commands, 'read_registry', lambda *_a, **_kw: registry)

    @contextlib.contextmanager
    def fake_locked(_main):
        yield registry

    monkeypatch.setattr(commands, 'locked_registry', fake_locked)


def test_close_all_errors_outside_main_repo(monkeypatch, orchestrator_env):
    from pathlib import Path

    # Inside a worktree: current root differs from the main repo root.
    monkeypatch.setattr(commands, 'get_repo_root', lambda: Path('/main/wt-x'))
    rc = commands.close_all_cmd(assume_yes=True)
    assert rc == 1
    assert orchestrator_env == []  # nothing torn down


def test_close_all_nothing_to_do(monkeypatch, orchestrator_env):
    _install_registry(monkeypatch, _FakeRegistry([]))
    rc = commands.close_all_cmd(assume_yes=True)
    assert rc == 0
    assert orchestrator_env == []


def test_close_all_tears_down_and_removes_from_registry(monkeypatch, orchestrator_env):
    reg = _FakeRegistry([_entry('a', 1), _entry('b', 2)])
    _install_registry(monkeypatch, reg)
    rc = commands.close_all_cmd(assume_yes=True)
    assert rc == 0
    assert [name for name, _ in orchestrator_env] == ['a', 'b']
    assert reg.removed == ['/wt/a', '/wt/b']


def test_close_all_passes_prune_flag(monkeypatch, orchestrator_env):
    _install_registry(monkeypatch, _FakeRegistry([_entry('a', 1)]))
    commands.close_all_cmd(prune=True, assume_yes=True)
    assert orchestrator_env[0][1] is True  # prune forwarded to teardown


def test_close_all_skips_unsafe_worktrees(monkeypatch, orchestrator_env):
    # 'b' is dirty -> skipped; only 'a' is torn down and removed.
    monkeypatch.setattr(commands, 'has_uncommitted_changes_in_path', lambda path: path.endswith('/b'))
    reg = _FakeRegistry([_entry('a', 1), _entry('b', 2)])
    _install_registry(monkeypatch, reg)
    rc = commands.close_all_cmd(assume_yes=True)
    assert rc == 0
    assert [name for name, _ in orchestrator_env] == ['a']
    assert reg.removed == ['/wt/a']


def test_close_all_failed_teardown_stays_in_registry(monkeypatch, orchestrator_env):
    def failing_teardown(entry, project_config, *, prune):
        return commands.TeardownResult(feature_name=entry.feature_name, ok=False, error='boom')

    monkeypatch.setattr(commands, '_teardown_worktree', failing_teardown)
    reg = _FakeRegistry([_entry('a', 1)])
    _install_registry(monkeypatch, reg)
    rc = commands.close_all_cmd(assume_yes=True)
    # A worktree that failed teardown must NOT be removed from the registry.
    assert reg.removed == []
    assert rc == 0
