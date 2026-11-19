"""状态目录保留：执行者轮次、经理作业、经理会话，以及存量清理命令。"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from workshop import prune as prune_module
from workshop.core.jobs import Jobs, Pending, sweep_directory
from workshop.core.models import Release
from workshop.core.managers import maybe_sweep_sessions, sweep_sessions
from workshop.plugins.executors.local import retention
from workshop.plugins.executors.local.retention import maybe_sweep, sweep_rounds, sweep_state

GIT = '/usr/bin/git'
NOW = time.time()


def age(path, days=0, seconds=0):
    at = NOW - days * 86400 - seconds
    os.utime(path, (at, at))


def round_dir(kind, name, marker='stopped.json', days=4):
    path = Path(kind) / 'rounds' / name
    path.mkdir(parents=True)
    (path / 'brief.txt').write_text('说明\n')
    if marker:
        (path / marker).write_text('{}')
    for entry in path.iterdir():
        age(entry, days=days)
    age(path, days=days)
    return path


def session_dir(state, name, days=8):
    path = Path(state) / 'managers' / name
    path.mkdir(parents=True)
    (path / 'session.json').write_text('{}')
    for entry in path.iterdir():
        age(entry, days=days)
    age(path, days=days)
    return path


def test_rounds_keep_running_unfinished_referenced_fresh_and_protected(tmp_path, monkeypatch):
    kind = tmp_path / 'codex'
    old = round_dir(kind, 'a' * 64)
    unfinished = round_dir(kind, 'b' * 64, marker=None)
    referenced = round_dir(kind, 'c' * 64)
    running = round_dir(kind, 'd' * 64)
    fresh = round_dir(kind, 'e' * 64, days=1)
    protected = round_dir(kind, 'f' * 64)
    info = kind / 'sessions' / 's1' / 'info.json'
    info.parent.mkdir(parents=True)
    info.write_text(json.dumps({'round': referenced.name}))
    monkeypatch.setattr(retention, 'held', lambda path: path.parent == running)

    removed = sweep_rounds(kind, NOW, protected=(protected.name,))
    assert {record['path'] for record in removed} == {old}
    assert not old.exists()
    for path in (unfinished, referenced, running, fresh, protected):
        assert path.exists()
    assert removed[0]['bytes'] > 0


def test_rounds_dry_run_lists_size_and_deletes_nothing(tmp_path):
    kind = tmp_path / 'codex'
    old = round_dir(kind, 'a' * 64)
    (old / 'big.bin').write_bytes(b'x' * 4096)
    removed = sweep_rounds(kind, NOW, dry_run=True)
    assert [record['path'] for record in removed] == [old]
    assert removed[0]['bytes'] >= 4096
    assert old.exists()


def test_nested_probe_worktree_is_removed_before_the_round(tmp_path):
    repo = tmp_path / 'repo'
    subprocess.run([GIT, 'init', '-q', '-b', 'main', str(repo)], check=True)
    (repo / 'README').write_text('x\n')
    subprocess.run([GIT, '-C', str(repo), '-c', 'user.name=T', '-c', 'user.email=t@l',
                    'add', '.'], check=True)
    subprocess.run([GIT, '-C', str(repo), '-c', 'user.name=T', '-c', 'user.email=t@l',
                    'commit', '-qm', 'first'], check=True)
    kind = tmp_path / 'codex'
    round_path = round_dir(kind, 'a' * 64)
    nested = round_path / '.local' / 'probe-core'
    subprocess.run([GIT, '-C', str(repo), 'worktree', 'add', '-q', '--detach', str(nested), 'HEAD'], check=True)
    orca = tmp_path / 'orca'
    orca.write_text('#!' + sys.executable + '\n' + r'''
import json, pathlib, subprocess, sys
args = sys.argv[1:]
root = pathlib.Path(sys.argv[0]).parent
with (root / 'calls').open('a') as f:
    f.write(json.dumps(args) + '\n')
path = args[args.index('--worktree') + 1].removeprefix('path:')
common = subprocess.run(['/usr/bin/git', '-C', path, 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                        capture_output=True, text=True, check=True).stdout.strip()
subprocess.run(['/usr/bin/git', '--git-dir', common, 'worktree', 'remove', path], check=True)
print(json.dumps({'ok': True}))
''')
    orca.chmod(0o755)
    removed = sweep_rounds(kind, NOW, orca=str(orca))
    assert [record['path'] for record in removed] == [round_path]
    assert not round_path.exists() and not nested.exists()
    calls = [json.loads(line) for line in (tmp_path / 'calls').read_text().splitlines()]
    assert calls[0][calls[0].index('--worktree') + 1] == 'path:' + str(nested)


def test_dirty_nested_clone_keeps_the_round(tmp_path, capsys):
    source = tmp_path / 'source'
    subprocess.run([GIT, 'init', '-q', '-b', 'main', str(source)], check=True)
    (source / 'README').write_text('x\n')
    subprocess.run([GIT, '-C', str(source), 'add', '.'], check=True)
    subprocess.run([GIT, '-C', str(source), '-c', 'user.name=T', '-c', 'user.email=t@l',
                    'commit', '-qm', 'first'], check=True)
    kind = tmp_path / 'codex'
    round_path = round_dir(kind, 'a' * 64)
    clone = round_path / '.local' / 'clone'
    subprocess.run([GIT, 'clone', '-q', str(source), str(clone)], check=True)
    (clone / 'README').write_text('unsaved\n')
    kept = []
    removed = sweep_rounds(kind, NOW, kept=kept)
    assert removed == [] and round_path.exists()
    assert kept == [{'path': round_path, 'reason': f'{clone} 有 1 处未提交改动'}]
    assert '1 处未提交改动' in capsys.readouterr().err


def test_nested_worktree_removal_failure_keeps_the_round(tmp_path):
    repo = tmp_path / 'repo'
    subprocess.run([GIT, 'init', '-q', '-b', 'main', str(repo)], check=True)
    (repo / 'README').write_text('x\n')
    subprocess.run([GIT, '-C', str(repo), 'add', '.'], check=True)
    subprocess.run([GIT, '-C', str(repo), '-c', 'user.name=T', '-c', 'user.email=t@l',
                    'commit', '-qm', 'first'], check=True)
    kind = tmp_path / 'codex'
    round_path = round_dir(kind, 'a' * 64)
    subprocess.run([GIT, '-C', str(repo), 'worktree', 'add', '-q', '--detach',
                    str(round_path / '.local' / 'probe'), 'HEAD'], check=True)
    removed = sweep_rounds(kind, NOW, orca=None)
    assert removed == [] and round_path.exists()


def test_maybe_sweep_is_throttled_by_marker(tmp_path):
    kind = tmp_path / 'codex'
    first = round_dir(kind, 'a' * 64)
    second = round_dir(kind, 'b' * 64)
    (kind / '.round-sweep').touch()
    assert maybe_sweep(kind, now=NOW) == [] and first.exists()
    os.utime(kind / '.round-sweep', (NOW - 7200, NOW - 7200))
    removed = maybe_sweep(kind, now=NOW)
    assert {record['path'] for record in removed} == {first, second}
    assert not first.exists() and not second.exists()


def test_sweep_state_walks_every_profile(tmp_path):
    for name in ('codex', 'orca-codex'):
        round_dir(tmp_path / name, ('a' if name == 'codex' else 'b') * 64)
    removed = sweep_state(tmp_path, NOW)
    assert len(removed) == 2
    assert not (tmp_path / 'codex' / 'rounds' / ('a' * 64)).exists()


def test_jobs_sweep_keeps_fresh_results_and_running_dirs(tmp_path):
    def job(name, *, result=False, pid=False, days=0, seconds=0):
        path = tmp_path / name
        path.mkdir()
        if result:
            (path / 'result.json').write_text('{}')
        if pid:
            (path / 'pid').write_text('1')
        for entry in path.iterdir():
            age(entry, days=days, seconds=seconds)
        age(path, days=days, seconds=seconds)
        return path

    old = job('old', result=True, days=1)
    fresh = job('fresh', result=True)
    stale = job('stale', seconds=120)
    running = job('running', pid=True, seconds=120)
    dry = job('dry', result=True, days=1)
    listed = sweep_directory(tmp_path, NOW, keep_seconds=3600, dry_run=True)
    assert {record['path'] for record in listed} == {old, stale, dry}
    assert old.exists() and stale.exists() and dry.exists()

    removed = sweep_directory(tmp_path, NOW, keep_seconds=3600)
    assert {record['path'] for record in removed} == {old, stale, dry}
    assert not old.exists() and not stale.exists() and not dry.exists()
    assert fresh.exists() and running.exists()


def test_manager_sessions_keep_current_and_fresh(tmp_path):
    state = tmp_path / 'state'
    old = session_dir(state, '11111111-1111-1111-1111-111111111111')
    protected = session_dir(state, '22222222-2222-2222-2222-222222222222')
    fresh = session_dir(state, '33333333-3333-3333-3333-333333333333', days=1)
    other = state / 'managers' / 'notes'
    other.mkdir()
    removed = sweep_sessions(state, datetime.now(timezone.utc), protected=(protected.name,))
    assert [record['path'] for record in removed] == [old]
    assert not old.exists() and protected.exists() and fresh.exists() and other.exists()

    dry = sweep_sessions(state, datetime.now(timezone.utc), protected=(protected.name,), dry_run=True)
    assert dry == []  # 刚扫过，只剩受保护的。


def test_manager_session_sweep_throttle(tmp_path):
    state = tmp_path / 'state'
    old = session_dir(state, '11111111-1111-1111-1111-111111111111')
    now = datetime.now(timezone.utc)
    (state / 'managers' / '.swept').write_text((now - timedelta(hours=2)).isoformat())
    removed = maybe_sweep_sessions(state, now)
    assert [record['path'] for record in removed] == [old]
    assert not old.exists()


def test_plan_worktrees_separates_delete_keep_and_orphan(tmp_path):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE items(body TEXT)')
    ended = {'id': 'alpha:7', 'target': 'alpha', 'state': '完成',
             'workdir': str(tmp_path / 'ws-aaaaaaaaaaaaaaaa'), 'sessions': {'writer': 's1'}}
    dirty = {'id': 'alpha:8', 'target': 'alpha', 'state': '完成',
             'workdir': str(tmp_path / 'ws-bbbbbbbbbbbbbbbb'), 'sessions': {'writer': 's1'}}
    running = {'id': 'alpha:9', 'target': 'alpha', 'state': '跑着',
               'workdir': str(tmp_path / 'ws-cccccccccccccccc')}
    for row in (ended, dirty, running):
        db.execute('INSERT INTO items VALUES (?)', (json.dumps(row),))
    log = SimpleNamespace(db=db)
    from workshop.core.models import Release

    class Executor:
        def release(self, item, target, dry_run=False):
            return Release(True, '删') if item['id'] == 'alpha:7' else Release(False, '有未提交改动')

    registry = SimpleNamespace(get=lambda port, name: Executor())
    rules = SimpleNamespace(data={'targets': {}}, target=lambda *args: {})
    candidates, kept, orphans = prune_module.plan_worktrees(log, rules, registry)
    assert [candidate['path'] for candidate in candidates] == [Path(ended['workdir'])]
    assert kept == [(Path(dirty['workdir']), '有未提交改动')]
    assert orphans == []  # 规则里没有 checkout，不扫没有对应活的工作区。


def test_plan_worktrees_asks_flow_before_releasing(tmp_path):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE items(body TEXT)')
    held = {'id': 'alpha:10', 'target': 'alpha', 'state': '完成',
            'workdir': str(tmp_path / 'ws-dddddddddddddddd'), 'sessions': {'writer': 's1'}}
    db.execute('INSERT INTO items VALUES (?)', (json.dumps(held),))
    log = SimpleNamespace(db=db)

    class Flow:
        def hold_workdir(self, item, target):
            return 'PR #9 还开着'

    class Executor:
        def release(self, item, target, dry_run=False):
            raise AssertionError('流程说留就不该问执行者')

    registry = SimpleNamespace(get=lambda port, name: Flow() if port == 'flow' else Executor())
    rules = SimpleNamespace(data={'targets': {}}, target=lambda *args: {'plugin': 'issue'})
    candidates, kept, orphans = prune_module.plan_worktrees(log, rules, registry)
    assert candidates == [] and orphans == []
    assert kept == [(Path(held['workdir']), 'PR #9 还开着')]


def test_prune_command_cleans_old_rounds_jobs_and_sessions(tmp_path, monkeypatch, capsys):
    state = tmp_path / 'state'
    state.mkdir()
    jobs = state / 'manager-jobs'
    old_job = jobs / 'old'
    old_job.mkdir(parents=True)
    (old_job / 'result.json').write_text('{}')
    age(old_job / 'result.json', seconds=7200)
    age(old_job, seconds=7200)
    fresh_job = jobs / 'fresh'
    fresh_job.mkdir()
    (fresh_job / 'result.json').write_text('{}')
    old_session = session_dir(state, '11111111-1111-1111-1111-111111111111')
    fresh_session = session_dir(state, '22222222-2222-2222-2222-222222222222', days=1)

    executors = tmp_path / 'executors'
    round_path = round_dir(executors / 'codex', 'a' * 64)
    monkeypatch.setenv('WORKSHOP_EXECUTOR_STATE_DIR', str(executors))

    log = SimpleNamespace(db=sqlite3.connect(':memory:'))
    log.db.execute('CREATE TABLE items(body TEXT)')
    args = SimpleNamespace(state_dir=state, dry_run=True, verbose=False, executor_state_dir=None)
    rules = SimpleNamespace(data={'targets': {}}, target=lambda *a: {})
    registry = SimpleNamespace(get=lambda *a: None)

    assert prune_module.prune(args, log, rules, registry) == 0
    output = capsys.readouterr().out
    assert '只列出，没动文件' in output
    assert old_job.exists() and old_session.exists() and round_path.exists()

    args.dry_run = False
    assert prune_module.prune(args, log, rules, registry) == 0
    output = capsys.readouterr().out
    assert '已删除' in output and '执行者轮次' in output
    assert not old_job.exists() and fresh_job.exists()
    assert not old_session.exists() and fresh_session.exists()
    assert not round_path.exists()


def test_prune_rechecks_item_and_rounds_before_deleting_worktree(tmp_path, monkeypatch, capsys):
    state = tmp_path / 'state'
    state.mkdir()
    workdir = tmp_path / 'ws-aaaaaaaaaaaaaaaa'
    workdir.mkdir()
    ended = {'id': 'alpha:7', 'target': 'alpha', 'state': '完成',
             'workdir': str(workdir), 'sessions': {'writer': 's1'}}
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE items(body TEXT)')
    db.execute('INSERT INTO items VALUES (?)', (json.dumps(ended),))
    current, rounds = {'item': dict(ended)}, []
    log = SimpleNamespace(db=db, get=lambda table, key: current['item'],
                          all=lambda table, **kwargs: rounds)

    class Executor:
        def __init__(self):
            self.actual = 0
            self.specs = []

        def release(self, item, target, dry_run=False):
            if dry_run:
                return Release(True, '删')
            self.actual += 1
            self.specs = [json.loads((path / 'spec.json').read_text())
                          for path in sorted((state / 'jobs').iterdir())]
            return Release(True)

    executor = Executor()
    registry = SimpleNamespace(get=lambda port, name: executor if port == 'executor' else None)
    rules = SimpleNamespace(data={'targets': {}}, target=lambda *args: {'plugin': 'issue', 'repo': 'alpha/repo'})
    args = SimpleNamespace(state_dir=state, dry_run=False, verbose=False, executor_state_dir=None)
    monkeypatch.setenv('WORKSHOP_EXECUTOR_STATE_DIR', str(tmp_path / 'executors'))

    current['item'] = {**ended, 'pending_rework': True}
    assert prune_module.prune(args, log, rules, registry) == 0
    assert '跳过（活标了返工' in capsys.readouterr().out and executor.actual == 0

    current['item'] = {**ended, 'state': '跑着'}
    assert prune_module.prune(args, log, rules, registry) == 0
    assert '跳过（活现在是 跑着' in capsys.readouterr().out and executor.actual == 0

    current['item'] = dict(ended)
    rounds.append({'workdir': str(workdir), 'status': 'running'})
    assert prune_module.prune(args, log, rules, registry) == 0
    assert '跳过（这个工作目录上还有没停的轮' in capsys.readouterr().out and executor.actual == 0

    rounds.clear()
    assert prune_module.prune(args, log, rules, registry) == 0
    assert '删了' in capsys.readouterr().out and executor.actual == 1
    # 锁的是守护进程实际用的那把；占位和 release_workdirs 的 identity 一样，删完就撤。
    assert (state / 'workshop.lock').exists()
    assert not (state / 'workshop.db.lock').exists()
    assert len(executor.specs) == 1
    spec = executor.specs[0]
    assert (spec['port'], spec['name'], spec['method']) == ('executor', 'writer', 'release')
    assert spec['identity'] == [ended['id'], 0, str(workdir), None]
    assert spec['placeholder'] is True
    assert not list((state / 'jobs').iterdir())


def test_prune_placeholder_blocks_rework_and_uses_the_engine_lock(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    item = {'id': 'alpha:7', 'workdir': str(tmp_path / 'ws-aaaaaaaaaaaaaaaa'),
            'sessions': {'writer': 's1'}}
    identity = prune_module.release_identity(item)
    jobs = Jobs(state / 'jobs', ['/bin/true'], clock=lambda: NOW)
    with prune_module.locked(state):
        assert (state / 'workshop.lock').exists()
        with prune_module.placeholder(state / 'jobs', item, 'writer', NOW + 3600) as path:
            assert jobs.path('executor', 'writer', 'release', identity) == path
            assert jobs.exists('executor', 'writer', 'release', identity)
            assert (path / 'pid').read_text() == '-1'
            # 看门看到同 identity 的收回作业就不返工；直接调也只会 Pending，不会另起一轮。
            with pytest.raises(Pending):
                jobs.call('executor', 'writer', 'release', [item], identity=identity)
        assert not path.exists()
        assert not jobs.exists('executor', 'writer', 'release', identity)
    assert (state / 'workshop.lock').exists()
    assert not (state / 'workshop.db.lock').exists()
