"""Tests that the registry keeps each project's worktrees separate.

The registry file is shared by every project on the machine. Entries must be
scoped to their owning repo, while index allocation stays global so that two
projects never hand out the same ports.
"""

from __future__ import annotations

import json

import pytest

from worktree_manager.registry import Registry, WorktreeEntry, WorktreePorts

ALPHA = '/repos/alpha'
BETA = '/repos/beta'


def entry(name: str, index: int, path: str | None = None) -> WorktreeEntry:
    return WorktreeEntry(
        path=path or f'/repos/wt-{name}',
        compose_project_name=f'wt-{name}',
        ports=WorktreePorts(web=58000 + index, db=5432 + index, redis=6379 + index),
        feature_name=name,
        index=index,
    )


@pytest.fixture
def two_projects() -> Registry:
    reg = Registry(main_repo_path=ALPHA)
    reg.add_worktree(entry('alpha-one', 1))
    reg.main_repo_path = BETA
    reg.add_worktree(entry('beta-one', 2))
    return reg


def test_worktrees_are_scoped_to_the_current_project(two_projects):
    two_projects.main_repo_path = ALPHA
    assert [w.feature_name for w in two_projects.worktrees] == ['alpha-one']

    two_projects.main_repo_path = BETA
    assert [w.feature_name for w in two_projects.worktrees] == ['beta-one']


def test_all_worktrees_spans_every_project(two_projects):
    names = {w.feature_name for w in two_projects.all_worktrees()}
    assert names == {'alpha-one', 'beta-one'}


def test_next_index_is_global_so_ports_never_collide(two_projects):
    """Indices 1 and 2 are taken by different projects; the next must be 3."""
    two_projects.main_repo_path = ALPHA
    assert two_projects.get_next_index() == 3


def test_find_by_feature_ignores_other_projects(two_projects):
    two_projects.main_repo_path = ALPHA
    assert two_projects.find_by_feature('beta-one') is None
    assert two_projects.find_by_feature('alpha-one') is not None


def test_removing_an_entry_leaves_other_projects_alone(two_projects):
    two_projects.main_repo_path = BETA
    two_projects.remove_worktree('/repos/wt-beta-one')

    assert two_projects.worktrees == []
    two_projects.main_repo_path = ALPHA
    assert [w.feature_name for w in two_projects.worktrees] == ['alpha-one']


def test_round_trip_preserves_project_grouping(two_projects):
    restored = Registry.from_dict(json.loads(json.dumps(two_projects.to_dict())))

    restored.main_repo_path = ALPHA
    assert [w.feature_name for w in restored.worktrees] == ['alpha-one']
    restored.main_repo_path = BETA
    assert [w.feature_name for w in restored.worktrees] == ['beta-one']


def test_legacy_flat_registry_is_migrated_under_its_main_repo():
    """A v1 file has one flat list and a single main_repo_path."""
    legacy = {
        'main_repo_path': ALPHA,
        'worktrees': [entry('alpha-one', 1).to_dict()],
    }
    reg = Registry.from_dict(legacy)

    assert reg.main_repo_path == ALPHA
    assert [w.feature_name for w in reg.worktrees] == ['alpha-one']
    assert len(reg.all_worktrees()) == 1


def _git(cwd, *args) -> str:
    import subprocess

    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_legacy_entries_are_filed_under_the_repo_that_actually_owns_them(tmp_path):
    """A v1 registry cannot say which project owns each entry.

    The recorded main_repo_path is only the project that ran `wt` last, so
    entries must be attributed by asking git who owns the worktree. Getting this
    wrong lets `wt clean` de-register another project's live worktree.
    """
    owner = tmp_path / 'owner'
    owner.mkdir()
    _git(owner, 'init', '-b', 'main')
    _git(owner, 'config', 'user.email', 't@t.co')
    _git(owner, 'config', 'user.name', 't')
    (owner / 'README.md').write_text('hi\n')
    _git(owner, 'add', '-A')
    _git(owner, 'commit', '-m', 'init')

    linked = tmp_path / 'wt-live'
    _git(owner, 'worktree', 'add', '-b', 'feature/live', str(linked), 'main')

    other = tmp_path / 'other'
    other.mkdir()

    legacy = {
        'main_repo_path': str(other),  # whichever project ran `wt` last
        'worktrees': [entry('live', 1, path=str(linked)).to_dict()],
    }
    reg = Registry.from_dict(legacy)

    reg.main_repo_path = str(owner)
    assert [w.feature_name for w in reg.worktrees] == ['live']

    reg.main_repo_path = str(other)
    assert reg.worktrees == []


def test_read_registry_scopes_to_the_requested_project(tmp_path, monkeypatch):
    """Read-only commands must scope to the project they run in.

    The file's own main_repo_path records whichever project ran `wt` last, so
    `wt list` in project B would otherwise display project A's worktrees.
    """
    from worktree_manager import registry as registry_mod

    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    registry_file = config_dir / 'registry.json'
    monkeypatch.setattr(registry_mod, 'CONFIG_DIR', str(config_dir))
    monkeypatch.setattr(registry_mod, 'REGISTRY_FILE', str(registry_file))

    reg = Registry(main_repo_path=ALPHA)
    reg.add_worktree(entry('alpha-one', 1))
    reg.main_repo_path = BETA
    reg.add_worktree(entry('beta-one', 2))
    registry_file.write_text(json.dumps(reg.to_dict()))

    loaded = registry_mod.read_registry(main_repo_path=ALPHA)
    assert loaded is not None
    assert loaded.main_repo_path == ALPHA
    assert [w.feature_name for w in loaded.worktrees] == ['alpha-one']
