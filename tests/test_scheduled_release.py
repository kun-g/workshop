from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from conftest import build, configuration, event, current
from workshop.core.engine import Engine
from workshop.core.models import Event
from workshop.core.ports import Retryable
from workshop.plugins.scheduled_release import ScheduledRelease, CHECKS, STAGES as SCHEDULED_STAGES
from workshop.plugins.scheduled_release.io import IO, read, write
from workshop.plugins.scheduled_release.source import MainBranchSource
import workshop.plugins.scheduled_release.source as source_module
from workshop import running


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text):
    (repo / name).write_text(text)
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', name)
    return git(repo, 'rev-parse', 'HEAD')


class LocalIO(IO):
    def __init__(self, root, remote):
        super().__init__(root)
        self.remote = remote
        self.calls = []
        self.identity = {'pid': 100, 'start': 'old'}
        self.old_alive = True
        self.fresh_heartbeat = False

    def command(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        argv = [str(self.remote) if str(v).startswith('https://github.com/') else v for v in argv]
        return super().command(argv, **kwargs)

    def daemon(self):
        return self.identity.copy()

    def process(self, pid):
        return {'pid': 100, 'start': 'old'} if self.old_alive else None

    def heartbeat_ready(self, root, daemon):
        return self.fresh_heartbeat


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo, remote = tmp_path / 'checkout', tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
    (repo / 'scripts').mkdir()
    a = commit(repo, 'readme', 'a')
    script = repo / 'scripts/check.sh'
    script.write_text('#!/bin/sh\nprintf "checked:"\ngit rev-parse HEAD\necho check-stderr >&2\n')
    script.chmod(0o755)
    b = commit(repo, 'readme', 'b')
    subprocess.run(['git', 'clone', '-q', '--bare', str(repo), str(remote)], check=True)
    git(repo, 'reset', '--hard', a)
    monkeypatch.setenv(running.VARIABLE, a)  # 守护进程在跑 A
    io = LocalIO(tmp_path / 'state', remote)
    flow = ScheduledRelease(io)
    target = {'name': 'alpha', 'repo': 'example/workshop', 'checkout': str(repo),
              'scheduled_release_state_dir': str(tmp_path / 'state'),
              'sources': {'main_branch': {'every': '1h'}}, 'prepare': [], 'github_identity': {'mode': 'inherit'}}
    body = {'kind': 'ahead', 'from': a, 'to': b}
    data = {'id': f'alpha:scheduled_release:{b}', 'target': 'alpha', 'object_key': b,
            'materials': [{'source': 'main_branch', 'body': body}]}
    return dict(root=tmp_path, repo=repo, remote=remote, a=a, b=b, io=io, flow=flow, target=target, data=data)


def after(w, stage, key=None):
    return w['flow'].after(w['data'], stage, key or stage, w['target'])


def state(w):
    return read(w['flow'].path(w['data'], w['target']))


def restart(w, *, heartbeat=True, old_alive=False):
    w['io'].identity = {'pid': 101, 'start': 'new'}
    w['io'].old_alive = old_alive
    w['io'].fresh_heartbeat = heartbeat


def test_source_hour_cursor_and_equal_head_and_network_failure(world, monkeypatch):
    w = world
    calls = []
    real = source_module.run
    def run(argv, **kwargs):
        calls.append(argv)
        return real([str(w['remote']) if str(a).startswith('https://github.com/') else a for a in argv], **kwargs)
    monkeypatch.setattr(source_module, 'run', run)
    now = datetime(2026, 11, 14, 9, 59, 59, tzinfo=timezone.utc)
    source = MainBranchSource(lambda: now)
    first = source.poll(w['target'], None)
    assert first.events[0].body == {'kind': 'ahead', 'from': w['a'], 'to': w['b']}
    assert source.poll(w['target'], first.cursor).events == [] and len(calls) == 1
    now += timedelta(seconds=1)
    second = source.poll(w['target'], first.cursor)
    assert len(second.events) == 1 and second.events[0].object_key == w['b'] and len(calls) == 2
    assert MainBranchSource(lambda: now).poll(w['target'], second.cursor).events == [] and len(calls) == 2
    # 主检出被别的程序快进到 B（#126），在跑的仍是 A：照样开发版的活。
    git(w['repo'], 'merge', '--ff-only', w['b'])
    now += timedelta(hours=1)
    moved = source.poll(w['target'], second.cursor)
    assert [e.body for e in moved.events] == [{'kind': 'ahead', 'from': w['a'], 'to': w['b']}]
    monkeypatch.setenv(running.VARIABLE, w['b'])
    now += timedelta(hours=1)
    assert source.poll(w['target'], moved.cursor).events == []
    monkeypatch.setattr(source_module, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 1, '', 'network down'))
    with pytest.raises(Retryable, match='network down'):
        source.poll(w['target'], second.cursor)


def test_fixed_commit_prepare_and_new_daemon_heartbeat_required(world):
    w = world
    marker = w['root'] / 'prepared'
    w['target']['prepare'] = [{'argv': [sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).write_text(str(Path.cwd()))']}]
    assert after(w, '证据').notice == ''
    checked = state(w)['check']
    assert checked['argv'] == CHECKS[0] and checked['exit_code'] == 0
    assert checked['stdout'] == 'checked:' + w['b'] + '\n' and checked['stderr'] == 'check-stderr\n'
    # 远端变成 C，已检查的 B 仍是唯一上线目标。
    git(w['repo'], 'checkout', '-qb', 'newer', w['b'])
    c = commit(w['repo'], 'readme', 'c')
    git(w['repo'], 'push', str(w['remote']), 'HEAD:main')
    git(w['repo'], 'checkout', 'main')
    assert after(w, '交付').restart
    assert git(w['repo'], 'rev-parse', 'HEAD') == w['b'] != c
    assert marker.read_text() == str(w['repo'])
    release = read(w['io'].root(w['target']) / 'release.json')
    assert release['from'] == w['a'] and release['to'] == w['b'] and release['status'] == 'restarting'
    assert release['prepare'] == [w['target']['prepare'][0]['argv']] and release['item'] == w['data']['id']
    assert after(w, '验收').pending  # 旧进程的新心跳也不够
    restart(w, old_alive=True)
    assert after(w, '验收').pending
    restart(w, heartbeat=False)
    assert after(w, '验收').pending
    restart(w)
    assert not after(w, '验收').pending
    assert read(w['io'].root(w['target']) / 'release.json')['status'] == 'ok'
    assert not after(w, '交付').restart  # 完成钩子重放不能再重启
    assert '发版完成' in after(w, '收尾').notice
    assert len([a for a, k in w['io'].calls if a == w['target']['prepare'][0]['argv']]) == 1
    assert git(w['repo'], 'status', '--porcelain') == ''


@pytest.mark.parametrize('problem', ['dirty', 'untracked', 'branch', 'detached', 'forked'])
def test_unsafe_checkout_untouched(world, problem):
    w = world
    after(w, '证据')
    if problem == 'dirty':
        (w['repo'] / 'readme').write_text('unsaved')
    elif problem == 'untracked':
        (w['repo'] / 'notes').write_text('unsaved')
    elif problem == 'branch':
        git(w['repo'], 'checkout', '-qb', 'human')
    elif problem == 'detached':
        git(w['repo'], 'checkout', '--detach')
    else:
        commit(w['repo'], 'readme', 'fork')
    before = git(w['repo'], 'rev-parse', 'HEAD'), git(w['repo'], 'status', '--porcelain')
    result = after(w, '交付')
    assert not result.restart and result.notice
    assert before == (git(w['repo'], 'rev-parse', 'HEAD'), git(w['repo'], 'status', '--porcelain'))
    assert state(w)['status'] == 'failed'
    assert not after(w, '验收').notice and not after(w, '收尾').notice


def test_prepare_failure_records_fast_forward_and_no_restart(world):
    w = world
    w['target']['prepare'] = [{'argv': [sys.executable, '-c', 'import sys; print("broken", file=sys.stderr); sys.exit(9)']}]
    after(w, '证据')
    result = after(w, '交付')
    assert not result.restart and '已快进但未重启' in result.notice
    assert git(w['repo'], 'rev-parse', 'HEAD') == w['b']
    assert state(w)['prepare_runs']['0']['stderr'] == 'broken\n'
    assert not (w['io'].root(w['target']) / 'release.json').exists()


def make_engine(w, notices='send'):
    config = configuration()
    config['notices'] = notices
    config['targets']['alpha'] = {**w['target'], 'default_flow': 'scheduled_release',
        'flows': {'scheduled_release': {'triggers': [{'source': 'main_branch', 'when': {'kind': 'ahead'}, 'action': 'ready'}]}}, 'sources': []}
    engine = build(w['root'] / 'engine', config)
    engine.plugins.register('flow', 'scheduled_release', w['flow'])
    body = {'kind': 'ahead', 'from': w['a'], 'to': w['b']}
    engine.ingest(Event('main_branch', 'hour1', 'alpha', w['b'], 'release', body))
    w['data']['id'] = 'alpha:' + w['b']  # 此测试中它是默认流程
    return engine


@pytest.mark.parametrize('notices', ['send', 'hold'])
def test_failed_check_finishes_once_no_executors_and_new_commit_can_start(world, notices):
    w = world
    original = w['io'].command
    def failing(argv, **kwargs):
        if argv == CHECKS[0]:
            w['io'].calls.append((argv, kwargs))
            return dict(argv=argv, exit_code=1, stdout='original out', stderr='original err')
        return original(argv, **kwargs)
    w['io'].command = failing
    engine = make_engine(w, notices)
    try:
        for _ in range(10):
            engine.tick()
        assert engine.log.get('items', w['data']['id'])['state'] == '完成'
        assert state(w)['check']['stdout'] == 'original out' and state(w)['check']['exit_code'] == 1
        assert not engine.log.all('rounds') and not engine.restart_requested
        assert git(w['repo'], 'rev-parse', 'HEAD') == w['a']
        engine.ingest(Event('main_branch', 'hour2', 'alpha', w['b'], 'release', w['data']['materials'][0]['body']))
        for _ in range(3):
            engine.tick()
        assert len([a for a, k in w['io'].calls if a == CHECKS[0]]) == 1
        cards = [c for c in engine.log.all('cards') if c['mode'] == 'notice']
        held = [f for f in engine.log.facts() if f['kind'] == 'notice_held']
        assert len(cards) == (1 if notices == 'send' else 0)
        assert len(held) == (0 if notices == 'send' else 1)
        new = 'f' * 40
        engine.ingest(Event('main_branch', 'hour3', 'alpha', new, 'release', {'kind': 'ahead', 'from': w['a'], 'to': new}))
        assert engine.log.get('items', 'alpha:' + new) is not None
    finally:
        engine.log.close()


def test_program_release_does_not_wait_for_active_round_and_resumes(world):
    w = world
    engine = make_engine(w)
    engine.ingest(event(target='beta'))
    try:
        for _ in range(5):
            engine.tick()
        assert engine.restart_requested
        running = current(engine, target='beta')
        assert len(engine.log.all('rounds')) == 1
        restart(w)
        revived = Engine(engine.log, engine.rules, engine.plugins, engine.clock)
        for _ in range(4):
            revived.tick()
        assert current(revived, target='beta')['token'] == running['token']
        assert revived.log.get('items', w['data']['id'])['state'] == '完成'
        assert not revived.restart_requested
        assert len(revived.log.all('rounds')) == 1
    finally:
        engine.log.close()


def test_isolated_commands_do_not_inherit_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('GH_TOKEN', 'test-secret')
    monkeypatch.setenv('SSH_AUTH_SOCK', 'test-sock')
    monkeypatch.setenv('AWS_SESSION_TOKEN', 'test-aws')
    result = IO(tmp_path).command([sys.executable, '-c', 'import os; print([os.getenv(k) for k in ("GH_TOKEN", "SSH_AUTH_SOCK", "AWS_SESSION_TOKEN")])'], cwd=tmp_path, isolated=True)
    assert result['exit_code'] == 0 and result['stdout'].strip() == '[None, None, None]'
    assert list(tmp_path.iterdir()) == []


def test_background_jobs_resume_without_rechecking_or_restarting_twice(world, monkeypatch):
    from test_jobs import ManualJobs
    from workshop.core.jobs import Pending
    import job_plugins
    w = world
    prepared = w['root'] / 'prepared'
    w['target']['prepare'] = [{'argv': [sys.executable, '-c',
        f'from pathlib import Path; Path({str(prepared)!r}).write_text("ready")']}]
    engine = make_engine(w, notices='hold')
    plugins = engine.plugins
    monkeypatch.setattr(job_plugins, 'registry', lambda: plugins)
    locks = {}
    jobs = ManualJobs(engine.log.path.parent / 'jobs', ['unused'], locks)
    engine.jobs = jobs
    restarted = False
    unrelated = plugins.get('flow', 'other')
    args = ({'id': 'beta:running'}, '执行', 'unrelated-key', {'name': 'beta'})
    try:
        for _ in range(160):
            engine.tick()
            if engine.restart_requested:
                assert not restarted
                # 已登记、尚在跑的独立作业跨守护进程重建继续。
                with pytest.raises(Pending):
                    jobs.call('flow', 'other', 'after', args, identity=['unrelated'])
                pending_path = jobs.path('flow', 'other', 'after', ['unrelated'])
                assert pending_path in locks
                restart(w)
                jobs = ManualJobs(engine.log.path.parent / 'jobs', ['unused'], locks)
                engine = Engine(engine.log, engine.rules, plugins, engine.clock, jobs)
                restarted = True
                with pytest.raises(Pending):
                    jobs.call('flow', 'other', 'after', args, identity=['unrelated'])
                assert pending_path in locks
            jobs.finish()
            if engine.log.get('items', w['data']['id'])['state'] == '完成':
                break
        assert restarted
        assert engine.log.get('items', w['data']['id'])['state'] == '完成'
        assert not engine.log.all('rounds')
        assert len([a for a, k in w['io'].calls if a == CHECKS[0]]) == 1
        assert unrelated.after_keys.count('unrelated-key') == 1
        assert jobs.call('flow', 'other', 'after', args, identity=['unrelated']) is None
        assert unrelated.after_keys.count('unrelated-key') == 1
        assert prepared.read_text() == 'ready'
        facts = engine.log.facts(w['data']['id'])
        notices = [f for f in facts if f['kind'] == 'notice_held'
                   and f['body']['subject'].startswith('定时发版完成')]
        assert len(notices) == 1
        assert not engine.log.all('timers')
        for _ in range(10):
            engine.clock.now += timedelta(seconds=1)
            engine.tick()
            jobs.finish()
            assert engine.log.get('items', w['data']['id'])['state'] == '完成'
            assert engine.log.facts(w['data']['id']) == facts
    finally:
        for descriptor in locks.values():
            os.close(descriptor)
        engine.log.close()


def test_heartbeat_probe_demands_fresh_success_and_stable_parent(tmp_path, monkeypatch):
    import workshop.plugins.scheduled_release.io as module
    io = IO(tmp_path)
    now = '2026-11-14T10:00:02+00:00'
    monkeypatch.setattr(io, 'now', lambda: now)
    parent = {'pid': 123, 'start': 'new'}
    monkeypatch.setattr(io, 'daemon', lambda: parent)
    write(tmp_path / 'heartbeat.json', {'last_ok_at': '2026-11-14T10:00:00+00:00'})
    monkeypatch.setattr(module, 'ring', lambda root: None)
    ticks = iter([0, 1, 6])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    assert not io.heartbeat_ready(tmp_path, parent)
    monkeypatch.setattr(module, 'ring', lambda root: write(root / 'heartbeat.json', {'last_ok_at': now}))
    monkeypatch.setattr(module.time, 'monotonic', lambda: 0)
    assert io.heartbeat_ready(tmp_path, parent)
    assert not io.heartbeat_ready(tmp_path, {'pid': 456, 'start': 'other'})


def test_daemon_only_accepts_direct_job_parent(monkeypatch):
    io = IO()
    monkeypatch.setattr(sys, 'argv', ['cli.py', 'run', '--once'])
    with pytest.raises(Retryable, match='不能用 run --once'):
        io.daemon()
    monkeypatch.setattr(sys, 'argv', ['cli.py', '--state-dir', '/tmp/x', 'job', '/tmp/x/jobs/j'])
    monkeypatch.setattr(os, 'getppid', lambda: 1)
    with pytest.raises(Retryable, match='失去父守护进程'):
        io.daemon()
    monkeypatch.setattr(os, 'getppid', lambda: 123)
    monkeypatch.setattr(io, 'process', lambda pid: {'pid': pid, 'start': 'fixed'})
    assert io.daemon() == {'pid': 123, 'start': 'fixed'}


def test_registered_rules_load_both_flows_and_default_issue_ids():
    from workshop.registry import load_rules, Registry
    rules = load_rules(Path('rules/workshop.yaml'))
    issue = rules.target('workshop', 'issue_delivery')
    release = rules.target('workshop', 'scheduled_release')
    assert issue['flow_default'] and issue['release'] == 'none'
    assert not release['flow_default']
    assert release['sources']['main_branch']['every'] == '1h'
    assert ScheduledRelease().stages(release) == ('证据', '交付', '验收', '收尾')
    assert issue['stages'] == ['接单', '执行', '交付', '收尾']
    # 集群批次保留全部七段（含接单占位），不跟 issue 交付的阶段走。
    assert ScheduledRelease().stages(rules.target('grant-console', 'scheduled_release')) == SCHEDULED_STAGES
    assert isinstance(Registry().get('flow', 'scheduled_release'), ScheduledRelease)
    assert isinstance(Registry().get('source', 'main_branch'), MainBranchSource)


def test_source_skips_rolled_back_commit_but_releases_new_merge(world, monkeypatch):
    w = world
    calls = []
    real = source_module.run
    def run(argv, **kwargs):
        calls.append(argv)
        return real([str(w['remote']) if str(a).startswith('https://github.com/') else a for a in argv], **kwargs)
    monkeypatch.setattr(source_module, 'run', run)
    # 来源收到默认流程的配置；发版目录在另一流程下。
    target = dict(w['target'])
    target['flows'] = {'issue_delivery': {}, 'scheduled_release': {
        'scheduled_release_state_dir': target.pop('scheduled_release_state_dir')}}
    root = w['io'].root(w['target'])
    write(root / 'release.json', {'from': w['a'], 'to': w['b'], 'status': 'rolled_back'})
    now = datetime(2026, 11, 14, 10, tzinfo=timezone.utc)
    source = MainBranchSource(lambda: now)
    skipped = source.poll(target, None)
    assert skipped.events == [] and skipped.cursor == now.isoformat()
    assert not source.poll(target, skipped.cursor).events and len(calls) == 1
    now += timedelta(hours=1)
    again = source.poll(target, skipped.cursor)
    assert not again.events and again.cursor != skipped.cursor
    git(w['repo'], 'checkout', '-qb', 'newer', w['b'])
    c = commit(w['repo'], 'readme', 'new')
    git(w['repo'], 'push', str(w['remote']), 'HEAD:main')
    git(w['repo'], 'checkout', 'main')
    now += timedelta(hours=1)
    next_release = source.poll(target, again.cursor)
    assert len(next_release.events) == 1
    assert next_release.events[0].body == {'kind': 'ahead', 'from': w['a'], 'to': c}
    assert not source.poll(target, next_release.cursor).events and len(calls) == 3
    w['data'] = {'id': 'alpha:scheduled_release:' + c, 'object_key': c,
                 'materials': [{'source': 'main_branch', 'body': next_release.events[0].body}]}
    after(w, '证据')
    assert after(w, '交付').restart
    restart(w)
    after(w, '验收')
    assert read(root / 'release.json')['status'] == 'ok'


@pytest.mark.parametrize('status', ['rolling_back', 'rollback_failed', 'rolled_back'])
def test_rollback_cannot_replay_restart_or_finish_successfully(world, status):
    w = world
    engine = make_engine(w)
    engine.ingest(event(target='beta'))
    try:
        for _ in range(5):
            engine.tick()
        running = current(engine, target='beta')
        assert engine.restart_requested
        release = read(w['io'].root(w['target']) / 'release.json')
        write(w['io'].root(w['target']) / 'release.json', {**release, 'status': status})
        # 即使是同一个旧父进程重放交付 key，也不能重启失败版本。
        assert not after(w, '交付').restart
        git(w['repo'], 'reset', '--keep', w['a'])
        restart(w)
        revived = Engine(engine.log, engine.rules, engine.plugins, engine.clock)
        for _ in range(5):
            revived.tick()
        assert revived.log.get('items', w['data']['id'])['state'] == '完成'
        assert state(w)['status'] == 'failed'
        assert not after(w, '收尾').notice
        assert read(w['io'].root(w['target']) / 'release.json')['status'] == status
        assert current(revived, target='beta')['token'] == running['token']
        assert len(revived.log.all('rounds')) == 1 and not revived.restart_requested
        assert not [c for c in revived.log.all('cards') if c['mode'] == 'notice']
    finally:
        engine.log.close()


def test_source_corrupt_release_is_not_treated_as_no_failure(world, monkeypatch):
    w = world
    real = source_module.run
    monkeypatch.setattr(source_module, 'run', lambda argv, **kw: real([
        str(w['remote']) if str(a).startswith('https://github.com/') else a for a in argv], **kw))
    root = w['io'].root(w['target'])
    root.mkdir(parents=True)
    (root / 'release.json').write_text('{broken')
    with pytest.raises(Retryable, match='无法读取发版记录'):
        MainBranchSource().poll(w['target'], None)


def test_release_now_request_compares_immediately_once(world, monkeypatch):
    w = world
    real = source_module.run
    monkeypatch.setattr(source_module, 'run', lambda argv, **kwargs: real(
        [str(w['remote']) if str(a).startswith('https://github.com/') else a for a in argv], **kwargs))
    now = datetime(2026, 11, 14, 9, 30, tzinfo=timezone.utc)
    source = MainBranchSource(lambda: now)
    request = w['root'] / 'state' / 'release-now'
    assert source.wake_paths(w['target']) == [str(request.resolve())]
    slot = now.replace(minute=0).isoformat()
    # 本小时已经比过：没有请求时不再比较。
    assert source.poll(w['target'], slot).events == []
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text('2026-11-14T09:30:00+00:00')
    asked = source.poll(w['target'], slot)
    assert [e.body for e in asked.events] == [{'kind': 'ahead', 'from': w['a'], 'to': w['b']}]
    assert asked.events[0].object_key == w['b'] and asked.events[0].title.startswith('立即发版')
    assert request.read_text() == '2026-11-14T09:30:00+00:00'
    assert asked.cursor == slot + '\n2026-11-14T09:30:00+00:00'
    assert source.poll(w['target'], asked.cursor).events == []


def test_source_without_recorded_version_falls_back_to_checkout(world, monkeypatch):
    # 启动时没记版本的旧守护进程（本改动上线前）仍按主检出比较。
    w = world
    real = source_module.run
    monkeypatch.setattr(source_module, 'run', lambda argv, **kwargs: real(
        [str(w['remote']) if str(a).startswith('https://github.com/') else a for a in argv], **kwargs))
    monkeypatch.delenv(running.VARIABLE)
    now = datetime(2026, 11, 14, 9, tzinfo=timezone.utc)
    assert [e.body for e in MainBranchSource(lambda: now).poll(w['target'], None).events] == [
        {'kind': 'ahead', 'from': w['a'], 'to': w['b']}]
    git(w['repo'], 'merge', '--ff-only', w['b'])
    assert MainBranchSource(lambda: now).poll(w['target'], None).events == []


def test_release_from_is_running_version_when_checkout_already_moved(world):
    # 主检出在发版前已被快进到目标提交；退回点仍是在跑的 A，不是 B。
    w = world
    git(w['repo'], 'merge', '--ff-only', w['b'])
    assert after(w, '证据').notice == ''
    assert after(w, '交付').restart
    release = read(w['io'].root(w['target']) / 'release.json')
    assert release['from'] == w['a'] and release['to'] == w['b'] and release['status'] == 'restarting'


def test_record_running_version_overrides_inherited_value(monkeypatch):
    monkeypatch.setenv(running.VARIABLE, '0' * 40)
    expected = git(Path(running.__file__).resolve().parent, 'rev-parse', 'HEAD')
    assert running.record() == expected == running.current() == os.environ[running.VARIABLE]


def test_release_now_uses_registered_source_wake_path(tmp_path, monkeypatch):
    import workshop.cli as cli
    from workshop.registry import Registry
    config = configuration()
    config['targets']['alpha']['scheduled_release_state_dir'] = str(tmp_path / 'configured')
    engine = build(tmp_path, config)
    engine.log.close()
    request = tmp_path / 'registered' / 'request'
    class Source:
        def wake_paths(self, target):
            return [str(request)]
    registry = Registry()
    registry.register('source', 'main_branch', Source())
    monkeypatch.setattr(cli, 'Registry', lambda *args, **kwargs: registry)
    assert cli.main(['--state-dir', str(tmp_path), 'release-now', 'alpha']) == 0
    assert datetime.fromisoformat(request.read_text()).tzinfo is not None
    assert not (tmp_path / 'configured' / 'release-now').exists()
