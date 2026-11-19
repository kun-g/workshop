from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from watch_helpers import fake, setup_watch, calls, configure, NOW, manager_command, delegated  # noqa: F401  fake 是夹具
from test_scheduled_release import git, commit
from workshop.watch import Watch, ReleaseRollback, save_json


@pytest.fixture
def rollback(tmp_path, fake, monkeypatch):
    repo = tmp_path / 'checkout'
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
    (repo / 'subdir').mkdir()
    (repo / 'subdir/keep').write_text('keep')
    a = commit(repo, 'readme', 'a')
    b = commit(repo, 'readme', 'b')
    watch = setup_watch(fake, tmp_path)
    root = watch.directory
    target = {'checkout': str(repo), 'flows': {'scheduled_release': {'scheduled_release_state_dir': str(root)}}}
    rules = {'targets': {'workshop': target}}
    with sqlite3.connect(root / 'workshop.db') as db:
        db.execute('CREATE TABLE controls(id TEXT, body TEXT)')
        db.execute('INSERT INTO controls VALUES (?,?)', ('@rules', json.dumps({'data': rules})))
    record = {'from': a, 'to': b, 'at': (NOW - timedelta(seconds=240)).isoformat(),
              'status': 'restarting', 'prepare': [[sys.executable, '-c', 'import os; print(os.getcwd())']],
              'prepare_cwds': ['subdir'], 'item': 'workshop:scheduled_release:' + b,
              'daemon': {'pid': 123, 'start': 'old'}}
    save_json(root / 'release.json', record)
    captured = []
    original = ReleaseRollback.command
    def command(self, argv, **kwargs):
        captured.append((argv, kwargs))
        if argv[0] == 'launchctl':
            return dict(argv=argv, exit_code=0, stdout='restarted\n', stderr='')
        return original(self, argv, **kwargs)
    monkeypatch.setattr(ReleaseRollback, 'command', command)
    return dict(repo=repo, root=root, watch=watch, record=record, rules=rules,
                a=a, b=b, captured=captured, fake=fake)


def read(w):
    return json.loads((w['root'] / 'release.json').read_text())


def write(w, **changes):
    save_json(w['root'] / 'release.json', {**w['record'], **changes})


def mutations(w):
    return [(a, k) for a, k in w['captured'] if 'reset' in a or a[0] in ('launchctl', sys.executable)]


def fresh(w):
    return Watch(w['root'], manager_command=manager_command(w['fake']))


def test_success_restores_previous_commit_and_persists_dedup(rollback):
    w = rollback
    state = w['watch'].run(NOW)
    assert git(w['repo'], 'rev-parse', 'HEAD') == w['a']
    record = read(w)
    assert record['status'] == 'rolled_back' and record['rolled_back_at'] == NOW.isoformat()
    assert all(record[k] == w['record'][k] for k in ('from', 'to', 'item', 'daemon'))
    assert len(record['rollback']['runs']) == 3
    assert record['rollback']['runs'][1]['stdout'] == str(w['repo'] / 'subdir') + '\n'
    assert record['rollback']['runs'][2]['argv'] == ['launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/com.kun.workshop']
    assert len(delegated(w['fake'])) == 1
    assert next(v for k, v in state['active'].items() if k.startswith('release:'))['confirmed']
    fresh(w).run(NOW)
    assert len(mutations(w)) == 3 and len(delegated(w['fake'])) == 1


@pytest.mark.parametrize('age,status,expected', [(60, 'restarting', False), (180, 'restarting', False),
    (181, 'restarting', True), (240, 'ok', False), (240, 'rolled_back', False)])
def test_threshold_and_terminal_states(rollback, age, status, expected):
    w = rollback
    write(w, at=(NOW - timedelta(seconds=age)).isoformat(), status=status)
    w['watch'].run(NOW)
    assert bool(mutations(w)) == expected
    assert git(w['repo'], 'rev-parse', 'HEAD') == (w['a'] if expected else w['b'])


def test_no_release_leaves_existing_watch_alone(rollback):
    w = rollback
    (w['root'] / 'release.json').unlink()
    assert w['watch'].run(NOW)['active'] == {}
    assert not w['captured'] and not calls(w['fake'])


@pytest.mark.parametrize('problem', ['dirty', 'untracked', 'branch', 'detached', 'head', 'from',
    'json', 'time', 'naive_time', 'to', 'item', 'prepare', 'cwd_count', 'escape', 'symlink', 'not_object'])
def test_unsafe_or_corrupt_record_never_mutates_checkout(rollback, problem):
    w = rollback
    if problem == 'dirty':
        (w['repo'] / 'readme').write_text('unsaved')
    elif problem == 'untracked':
        (w['repo'] / 'notes').write_text('unsaved')
    elif problem == 'branch':
        git(w['repo'], 'checkout', '-qb', 'human')
    elif problem == 'detached':
        git(w['repo'], 'checkout', '--detach')
    elif problem == 'head':
        commit(w['repo'], 'readme', 'c')
    elif problem == 'json':
        (w['root'] / 'release.json').write_text('{broken')
    elif problem == 'not_object':
        (w['root'] / 'release.json').write_text('[]')
    elif problem == 'symlink':
        (w['repo'] / 'outside').symlink_to(w['repo'].parent)
        git(w['repo'], 'add', 'outside')
        git(w['repo'], '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'symlink')
        write(w, to=git(w['repo'], 'rev-parse', 'HEAD'), prepare_cwds=['outside'])
    else:
        changes = {'from': {'from': 'f' * 40}, 'time': {'at': 'bad'}, 'naive_time': {'at': '2026-11-14T00:00:00'},
                   'to': {'to': 'short'}, 'item': {'item': 'other:release'}, 'prepare': {'prepare': ['shell string']},
                   'cwd_count': {'prepare_cwds': []}, 'escape': {'prepare_cwds': ['..']}}
        write(w, **changes[problem])
    before = git(w['repo'], 'rev-parse', 'HEAD'), git(w['repo'], 'status', '--porcelain')
    state = w['watch'].run(NOW)
    assert not mutations(w)
    assert before == (git(w['repo'], 'rev-parse', 'HEAD'), git(w['repo'], 'status', '--porcelain'))
    assert any('无法安全退回' in v['text'] for k, v in state['active'].items() if k.startswith('release:'))
    fresh(w).run(NOW)
    assert len(delegated(w['fake'])) == 1


@pytest.mark.parametrize('stage,count', [('reset', 1), ('prepare', 2), ('kickstart', 3)])
def test_command_failure_is_recorded_and_not_retried(rollback, monkeypatch, stage, count):
    w = rollback
    original = ReleaseRollback.command
    def command(self, argv, **kwargs):
        matches = stage in argv or stage == 'prepare' and argv[0] == sys.executable
        if matches:
            w['captured'].append((argv, kwargs))
            return dict(argv=argv, exit_code=19, stdout='original-out\n', stderr='original-err\n')
        return original(self, argv, **kwargs)
    monkeypatch.setattr(ReleaseRollback, 'command', command)
    w['watch'].run(NOW)
    record = read(w)
    assert record['status'] == 'rollback_failed'
    assert len(record['rollback']['runs']) == count
    assert record['rollback']['runs'][-1]['exit_code'] == 19
    assert record['rollback']['runs'][-1]['stdout'] == 'original-out\n'
    assert record['rollback']['runs'][-1]['stderr'] == 'original-err\n'
    fresh(w).run(NOW)
    assert len(mutations(w)) == count and len(delegated(w['fake'])) == 1
    assert git(w['repo'], 'rev-parse', 'HEAD') == (w['b'] if stage == 'reset' else w['a'])


def test_interrupted_action_does_not_repeat(rollback, monkeypatch):
    w = rollback
    original = ReleaseRollback.command
    def interrupted(self, argv, **kwargs):
        result = original(self, argv, **kwargs)
        if 'reset' in argv:
            raise SystemExit('simulated crash after git reset')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(ReleaseRollback, 'command', interrupted)
        with pytest.raises(SystemExit):
            w['watch'].run(NOW)
    assert read(w)['status'] == 'rolling_back' and read(w)['rollback']['pending']
    fresh(w).run(NOW)
    assert read(w)['status'] == 'rollback_failed'
    assert len(mutations(w)) == 1
    assert '中断' in read(w)['rollback']['reason']


def test_legacy_prepare_cwd_and_credential_isolation(rollback, monkeypatch):
    w = rollback
    monkeypatch.setenv('GH_TOKEN', 'secret')
    monkeypatch.setenv('SSH_AUTH_SOCK', 'secret')
    record = dict(w['record'])
    record.pop('prepare_cwds')
    record['prepare'] = [[sys.executable, '-c', 'import os; print(os.getcwd(), os.getenv("GH_TOKEN"), os.getenv("SSH_AUTH_SOCK"))']]
    save_json(w['root'] / 'release.json', record)
    w['watch'].run(NOW)
    assert read(w)['rollback']['runs'][1]['stdout'] == f"{w['repo']} None None\n"


def test_two_watchers_and_flow_lock_do_not_repeat_or_overwrite_ok(rollback):
    w = rollback
    with (w['root'] / 'scheduled-release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert w['watch'].run(NOW)['active'] == {}
        assert not mutations(w)
        write(w, status='ok')
    w['watch'].run(NOW)
    assert read(w)['status'] == 'ok' and not mutations(w)
    write(w)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: fresh(w).run(NOW), range(2)))
    assert all(any(k.startswith('release:') for k in r['active']) for r in results)
    assert len(mutations(w)) == 3 and len(delegated(w['fake'])) == 1
    # 一次锁忙不意味着故障恢复；保留之前的通知标识。
    with (w['root'] / 'scheduled-release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fresh(w).run(NOW)
    fresh(w).run(NOW)
    assert len(delegated(w['fake'])) == 1


def test_corrupt_interrupted_journal_does_not_crash_watch(rollback):
    w = rollback
    write(w, status='rolling_back', rollback='damaged')
    w['watch'].run(NOW)
    assert not mutations(w) and read(w)['status'] == 'rollback_failed'
    assert read(w)['rollback']['unreadable'] == 'damaged'
    fresh(w).run(NOW)
    assert len(delegated(w['fake'])) == 1


def test_corrupt_rules_fail_closed_but_existing_faults_still_run(rollback):
    w = rollback
    with sqlite3.connect(w['root'] / 'workshop.db') as db:
        db.execute('UPDATE controls SET body=? WHERE id=?', (json.dumps({'data': []}), '@rules'))
    (w['root'] / 'heartbeat.json').write_text('{broken')
    state = w['watch'].run(NOW)
    assert not mutations(w)
    assert 'heartbeat' in state['active'] and any(k.startswith('release:') for k in state['active'])


def test_manager_failure_does_not_repeat_rollback(rollback):
    w = rollback
    configure(w['fake'], manager_exit=1)
    with pytest.raises(RuntimeError):
        w['watch'].run(NOW)
    assert read(w)['status'] == 'rolled_back'
    configure(w['fake'])
    fresh(w).run(NOW)
    assert len(mutations(w)) == 3
    assert len(delegated(w['fake'])) == 1 and not calls(w['fake'])


def test_system_python_can_rollback_without_project_environment(rollback):
    w = rollback
    binary = w['root'] / 'bin'
    binary.mkdir()
    launchctl = binary / 'launchctl'
    launchctl.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    launchctl.chmod(0o755)
    interpreter = '/usr/bin/python3' if Path('/usr/bin/python3').exists() else sys.executable
    script = Path(__file__).resolve().parents[1] / 'src/workshop/watch/__init__.py'
    result = subprocess.run([interpreter, '-I', str(script), '--state-dir', str(w['root']), '--manager-command', json.dumps(manager_command(w['fake']))],
        cwd=w['root'], env={**os.environ, 'PATH': str(binary) + os.pathsep + os.environ['PATH']}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert read(w)['status'] == 'rolled_back'
    assert git(w['repo'], 'rev-parse', 'HEAD') == w['a']


def test_cluster_flow_never_selects_self_rollback_target(rollback):
    w = rollback
    w['rules']['targets']['grant-console'] = {'checkout': '/never-touch', 'flows': {
        'scheduled_release': {'scheduled_release_state_dir': str(w['root']), 'scheduled_release_mode': 'cluster'}}}
    with sqlite3.connect(w['root'] / 'workshop.db') as db:
        db.execute("UPDATE controls SET body=? WHERE id='@rules'", (json.dumps({'data': w['rules']}),))
    name, target = ReleaseRollback(w['root']).configuration()
    assert name == 'workshop' and target['checkout'] == str(w['repo'])
