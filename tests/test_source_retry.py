"""来源读取的有限退避：假时钟、真实身份/来源入口及作业结果边界。"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
import subprocess

import pytest

from conftest import FakeSource, event, item
from test_jobs import ManualJobs
from test_review_round3 import approval_wait
from workshop import github_identity
from workshop.core.engine import Engine
from workshop.core.jobs import execute, read
from workshop.core.models import Poll, stamp
from workshop.core.ports import Retryable
from workshop.plugins.sources.github import GitHubSource


RETRY = 'retry:source:alpha:source'


def wire_source(engine, tmp_path, monkeypatch, fault='timeout'):
    """真实来源读空仓库；只替换最外层命令，不替换异常分类逻辑。"""
    target = engine.rules.data['targets']['alpha']
    target.update(repo='kun-g/workshop', github_identity={
        'mode': 'app', 'gh': '/fake/gh', 'signer': ['fake-signer']})
    engine.plugins.register('source', 'source', GitHubSource('/fake/gh', tmp_path / 'cache'))
    state = {'fault': fault, 'signs': 0, 'reads': 0}

    def command(argv, **kwargs):
        signing = argv[0] == 'fake-signer'
        state['signs' if signing else 'reads'] += 1
        failure = state['fault']
        if signing:
            assert not kwargs['env'].get('GH_TOKEN')
            if failure == 'sign-timeout':
                raise subprocess.TimeoutExpired(argv, 30, output='private-signing-output')
            if failure == 'sign-oserror':
                raise OSError('private-signing-output')
            if failure in ('sign-exit', 'sign-empty', 'sign-invalid'):
                output = {'sign-exit': 'private-signing-output', 'sign-empty': '', 'sign-invalid': 'two tokens'}[failure]
                return subprocess.CompletedProcess(argv, int(failure == 'sign-exit'), output, 'private-signing-output')
            return subprocess.CompletedProcess(argv, 0, 'fake-app-secret', '')
        assert argv[1:4] == ['api', '--method', 'GET']
        assert kwargs['env']['GH_TOKEN'] == 'fake-app-secret'
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, 12, output='fake-app-secret')
        if failure == 'oserror':
            raise OSError('fake-app-secret')
        if failure == 'program':
            raise RuntimeError('program error')
        if failure == 'http':
            return subprocess.CompletedProcess(argv, 1, 'HTTP/2 503\n\n[]', 'fake-app-secret')
        body = '{invalid' if failure == 'json' else '[]'
        return subprocess.CompletedProcess(argv, 0, 'HTTP/2 200\n\n' + body, '')

    monkeypatch.setattr(subprocess, 'run', command)
    return state


@pytest.mark.parametrize('fault', ['timeout', 'oserror', 'sign-timeout', 'sign-oserror',
                                   'sign-exit', 'sign-empty', 'sign-invalid', 'http'])
def test_reads_retry_twice_then_recover_without_changing_wait(engine, tmp_path, monkeypatch, fault):
    card = approval_wait(engine)
    before_item = deepcopy(item(engine))
    before_card = engine.log.get('cards', card['id'])
    before_cursor = engine.log.get('cursors', 'alpha:source')
    # FakeSource's opaque cursor is not a valid GitHub cursor; start this source afresh.
    engine.fact('cursor', {'id': 'alpha:source', 'cursor': None})
    state = wire_source(engine, tmp_path, monkeypatch, fault)
    for advance in (0, 60):
        engine.clock.now += timedelta(seconds=advance)
        engine.sources()
        assert not engine.control('alpha').get('broken')
        assert not engine.control('alpha').get('action_blocked')
        assert item(engine) == before_item
        assert engine.log.get('cards', card['id']) == before_card
        assert engine.log.get('cursors', 'alpha:source')['cursor'] is None
        calls = state.copy()
        engine.sources()
        assert state == calls  # 提前叫醒不能绕过核心的退避时间。
    assert state['signs'] == 2
    if fault.startswith('sign-'):
        assert state['reads'] == 0
    state['fault'] = None
    engine.clock.now += timedelta(seconds=120)
    engine.sources()
    assert engine.control(RETRY)['count'] == 0
    assert engine.control(RETRY)['since'] is None and engine.control(RETRY)['at'] is None
    assert engine.log.get('cursors', 'alpha:source') != before_cursor
    assert item(engine) == before_item
    assert engine.log.get('cards', card['id']) == before_card
    facts = json.dumps(engine.log.facts(), ensure_ascii=False)
    assert 'fake-app-secret' not in facts and 'private-signing-output' not in facts
    state['fault'] = fault
    engine.clock.now += timedelta(seconds=20)
    engine.sources()
    assert engine.control(RETRY)['count'] == 1
    assert engine.control(RETRY)['since'] == stamp(engine.clock())
    assert engine.control(RETRY)['at'] == stamp(engine.clock() + timedelta(minutes=1))


@pytest.mark.parametrize('recover_at_deadline', [False, True])
@pytest.mark.parametrize('hold_notices', [False, True])
def test_deadline_survives_restart_and_isolates_once(engine, tmp_path, monkeypatch, recover_at_deadline, hold_notices):
    approval_wait(engine)
    engine.fact('cursor', {'id': 'alpha:source', 'cursor': None})
    state = wire_source(engine, tmp_path, monkeypatch)
    if hold_notices:
        engine.rules.data['notices'] = 'hold'
    t0 = engine.clock()
    before = item(engine)
    other = FakeSource()
    other.events = [event('second', target='beta', ext_id='second')]
    engine.plugins.register('source', 'other', other)
    engine.rules.data['targets']['beta']['sources'] = ['other']
    for minute, next_minute in zip((0, 1, 3, 8, 18, 28), (1, 3, 8, 18, 28, 30)):
        engine.clock.now = t0 + timedelta(minutes=minute)
        engine.sources()
        assert not engine.control('alpha').get('broken')
        assert item(engine) == before
        assert engine.control(RETRY)['at'] == stamp(t0 + timedelta(minutes=next_minute))
        # 事实重放及引擎重建后继续使用原首次失败时间，不延长窗口。
        engine.log.rebuild()
        engine = Engine(engine.log, engine.rules, engine.plugins, engine.clock)
        assert engine.control(RETRY)['since'] == stamp(t0)
    engine.clock.now = t0 + timedelta(minutes=30)
    state['fault'] = None if recover_at_deadline else 'oserror'
    engine.sources()
    assert not engine.control('beta').get('broken') and len(other.cursors) == 7
    failures = [f for f in engine.log.facts() if f['kind'] == 'plugin_failed']
    if recover_at_deadline:
        assert not failures and not engine.control('alpha').get('broken')
        assert item(engine) == before and engine.control(RETRY)['count'] == 0
        return
    assert engine.control('alpha')['broken']
    assert item(engine)['state'] == '卡住' and item(engine)['plugin_blocked']
    assert len(failures) == 1 and '1800 秒' in failures[0]['body']['error']
    assert 'GitHub 命令未确认完成' in failures[0]['body']['error']
    calls = state.copy()
    for _ in range(3):
        engine.clock.now += timedelta(hours=1)
        engine.sources()
    assert state == calls
    assert len([f for f in engine.log.facts() if f['kind'] == 'plugin_failed']) == 1
    notifications = [c for c in engine.log.all('cards') if c['id'].startswith('@alpha:plugin:source:')]
    held = [f for f in engine.log.facts() if f['kind'] == 'notice_held' and f['item'] == '@alpha']
    assert len(notifications) + len(held) == 1
    engine.resume('alpha', [])
    state['fault'] = None
    engine.sources()
    assert not engine.control('alpha').get('broken') and not item(engine).get('plugin_blocked')


@pytest.mark.parametrize('fault', ['json', 'program', 'config'])
def test_nontransient_errors_still_isolate_immediately(engine, tmp_path, monkeypatch, fault):
    wire_source(engine, tmp_path, monkeypatch, fault)
    if fault == 'config':
        engine.rules.data['targets']['alpha']['github_identity']['mode'] = 'invalid'
    engine.sources()
    assert engine.control('alpha')['broken']
    assert not engine.control(RETRY).get('count')


def test_sources_have_separate_windows_and_preserve_failed_cursor(engine, monkeypatch):
    original = engine.plugins.get('source', 'source')
    other = FakeSource()
    engine.plugins.register('source', 'other', other)
    engine.rules.data['targets']['alpha']['sources'].append('other')
    engine.fact('cursor', {'id': 'alpha:source', 'cursor': 'confirmed'})
    seen = []
    def failing(target, cursor):
        seen.append(cursor)
        raise Retryable('read failed')
    monkeypatch.setattr(original, 'poll', failing)
    engine.sources()
    engine.clock.now += timedelta(seconds=60)
    monkeypatch.setattr(other, 'poll', failing)
    engine.sources()
    assert seen == ['confirmed', 'confirmed', 'after-batch']
    assert engine.control(RETRY)['count'] == 2
    assert engine.control('retry:source:alpha:other')['count'] == 1
    assert engine.control(RETRY)['since'] != engine.control('retry:source:alpha:other')['since']
    assert engine.log.get('cursors', 'alpha:source')['cursor'] == 'confirmed'


def test_background_error_result_roundtrip_and_jobs_restart(engine, tmp_path, monkeypatch):
    state = wire_source(engine, tmp_path, monkeypatch)
    locks = {}
    jobs = ManualJobs(tmp_path / 'jobs', [], locks)
    jobs.clock = lambda: engine.clock().timestamp()
    engine.jobs = jobs
    try:
        for minute in (0, 1, 3):
            engine.clock.now = datetime(2026, 11, 13, 8, minute, tzinfo=engine.clock().tzinfo)
            if minute == 3:
                state['fault'] = None
            engine.sources()  # 仅登记作业，插件尚未调用。
            path = next(iter(locks))
            execute(path, engine.plugins)  # 独立作业执行真实来源及身份适配代码。
            result = read(path / 'result.json')
            if minute != 3:
                assert result['error']['kind'] == 'Retryable'
                assert 'fake-app-secret' not in json.dumps(result)
            os.close(locks.pop(path))
            jobs.launcher.shutdown()
            jobs = ManualJobs(tmp_path / 'jobs', [], locks)
            jobs.clock = lambda: engine.clock().timestamp()
            engine = Engine(engine.log, engine.rules, engine.plugins, engine.clock, jobs=jobs)
            engine.sources()  # 新管理器接回同一结果，不重发命令。
            assert not engine.control('alpha').get('broken')
            assert engine.control(RETRY)['count'] == (0 if minute == 3 else (1 if minute == 0 else 2))
        assert state['signs'] == 3
    finally:
        for fd in locks.values():
            os.close(fd)
        jobs.launcher.shutdown()


@pytest.mark.parametrize('argv', [['gh', 'pr', 'merge', '1'], ['gh', 'issue', 'comment', '1'],
                                 ['gh', 'issue', 'edit', '1', '--add-label', 'ready']])
def test_ambiguous_writes_remain_blocked_and_are_not_replayed(engine, monkeypatch, argv):
    writes = []
    def command(command, **kwargs):
        if command[0] == 'fake-signer':
            return subprocess.CompletedProcess(command, 0, 'fake-app-secret', '')
        writes.append(command)
        raise subprocess.TimeoutExpired(command, 10, output='fake-app-secret')
    monkeypatch.setattr(subprocess, 'run', command)
    target = {'repo': 'kun-g/workshop', 'github_identity': {
        'mode': 'app', 'gh': '/fake/gh', 'signer': ['fake-signer']}}
    @github_identity.scoped
    def write(target):
        github_identity.run(argv, text=True, capture_output=True)
    engine.ingest(event())
    flow = engine.plugins.get('flow', 'flow')
    monkeypatch.setattr(flow, 'brief', lambda *args: write(target))
    engine.tick()
    assert engine.control('alpha')['broken'] and engine.control('alpha')['action_blocked']
    for minute in (1, 3, 8, 18, 28, 30):
        engine.clock.now += timedelta(minutes=minute)
        engine.tick()
    assert len(writes) == 1
    assert not engine.control(RETRY).get('count')


def test_recovered_batch_commits_cursor_and_events_only_once(engine, monkeypatch):
    source = engine.plugins.get('source', 'source')
    engine.fact('cursor', {'id': 'alpha:source', 'cursor': 'before'})
    reads = []
    def poll(target, cursor):
        reads.append(cursor)
        if len(reads) <= 2:
            raise Retryable('read failed')
        return Poll([event('new', ext_id='recovered')], 'after')
    monkeypatch.setattr(source, 'poll', poll)
    for seconds in (0, 60, 120, 20):
        engine.clock.now += timedelta(seconds=seconds)
        engine.sources()
    assert reads == ['before', 'before', 'before', 'after']
    events = [f for f in engine.log.facts() if f['kind'] == 'event' and f['ext_id'] == 'recovered']
    assert len(events) == 1
    assert engine.log.get('items', 'alpha:new') is not None
    assert engine.log.get('cursors', 'alpha:source')['cursor'] == 'after'


def test_late_failed_result_uses_elapsed_time_not_attempt_count(engine, monkeypatch):
    def fail(target, cursor):
        raise Retryable('last read error')
    monkeypatch.setattr(engine.plugins.get('source', 'source'), 'poll', fail)
    engine.sources()
    engine.clock.now += timedelta(minutes=35)
    engine.sources()
    assert engine.control('alpha')['broken'] and engine.control(RETRY)['count'] == 2
    failure = next(f for f in engine.log.facts() if f['kind'] == 'plugin_failed')
    assert '2100 秒' in failure['body']['error'] and 'last read error' in failure['body']['error']
