"""立即触发的请求在异步作业失效后仍可重放，发版从有事做的阶段开始。"""
from datetime import datetime, timezone

import pytest

from test_scheduled_release import world, make_engine, restart
from test_wake import jobs_in
from workshop.core.jobs import Pending, execute
from workshop.plugins.sources.main_branch import MainBranchSource
import workshop.plugins.sources.main_branch as source_module


@pytest.mark.parametrize('inflight', [True, False])
def test_request_survives_invalidated_poll(world, monkeypatch, inflight):
    w = world
    engine = make_engine(w)
    source = MainBranchSource(lambda: datetime(2026, 11, 16, 10, 30, tzinfo=timezone.utc))
    engine.plugins.register('source', 'main_branch', source)
    real = source_module.run
    monkeypatch.setattr(source_module, 'run', lambda argv, **kw: real([
        str(w['remote']) if str(a).startswith('https://github.com/') else a for a in argv], **kw))
    request = w['root'] / 'state' / 'release-now'
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text('first')
    cursor = '2026-11-16T10:00:00+00:00'
    try:
        with jobs_in(w['root']) as jobs:
            def start():
                with pytest.raises(Pending):
                    jobs.call('source', 'main_branch', 'poll', [w['target'], cursor], identity=['alpha'])
                return jobs.path('source', 'main_branch', 'poll', ['alpha'])
            path = start()
            if inflight:
                jobs.invalidate('source', 'main_branch', ['alpha'])
            execute(path, engine.plugins, None)
            if not inflight:
                jobs.invalidate('source', 'main_branch', ['alpha'])
            # 核心丢掉在途/未消费结果；下一次查询必须还能恢复同一个请求。
            path = start()
            execute(path, engine.plugins, None)
            result = jobs.call('source', 'main_branch', 'poll', [w['target'], cursor], identity=['alpha'])
            assert [e.object_key for e in result.events] == [w['b']]
            assert result.events[0].ext_id == 'now:first'
            assert not source.poll(w['target'], result.cursor).events
            request.write_text('second')
            again = source.poll(w['target'], result.cursor)
            assert [e.ext_id for e in again.events] == ['now:second']
    finally:
        engine.log.close()


def test_self_release_uses_only_configured_nonempty_stages(world):
    w = world
    w['target']['stages'] = ['证据', '交付', '验收', '收尾']
    engine = make_engine(w)
    try:
        for _ in range(5):
            engine.tick()
            if engine.restart_requested:
                break
        restart(w)
        for _ in range(5):
            engine.tick()
        item = engine.log.get('items', w['data']['id'])
        assert item['state'] == '完成'
        stages = [f['body']['stage'] for f in engine.log.facts()
                  if f['item'] == item['id'] and f['kind'] == 'program_result']
        assert stages == ['证据', '交付', '验收', '收尾']
    finally:
        engine.log.close()


def test_new_request_during_query_is_not_acknowledged_with_old_result(world, monkeypatch):
    w = world
    request = w['root'] / 'state' / 'release-now'
    request.parent.mkdir(parents=True)
    request.write_text('first')
    real = source_module.run
    def command(argv, **kw):
        result = real([str(w['remote']) if str(a).startswith('https://github.com/') else a for a in argv], **kw)
        request.write_text('second')
        return result
    monkeypatch.setattr(source_module, 'run', command)
    source = MainBranchSource()
    first = source.poll(w['target'], None)
    second = source.poll(w['target'], first.cursor)
    assert first.events[0].ext_id == 'now:first'
    assert second.events[0].ext_id == 'now:second'
    assert not source.poll(w['target'], second.cursor).events


def test_request_acknowledged_without_a_new_commit_does_not_repeat(world, monkeypatch):
    from workshop import running
    w = world
    monkeypatch.setenv(running.VARIABLE, w['b'])
    calls = []
    real = source_module.run
    def command(argv, **kw):
        calls.append(argv)
        return real([str(w['remote']) if str(a).startswith('https://github.com/') else a for a in argv], **kw)
    monkeypatch.setattr(source_module, 'run', command)
    request = w['root'] / 'state' / 'release-now'
    request.parent.mkdir(parents=True)
    request.write_text('same-head')
    now = datetime(2026, 11, 16, 10, 30, tzinfo=timezone.utc)
    source = MainBranchSource(lambda: now)
    first = source.poll(w['target'], None)
    assert not first.events
    assert not source.poll(w['target'], first.cursor).events
    assert len(calls) == 1
    # 保留的请求不阻止下一整点按正常时段再比较。
    from datetime import timedelta
    now += timedelta(hours=1)
    assert not source.poll(w['target'], first.cursor).events
    assert len(calls) == 2


def test_configured_entry_stage_survives_pause_and_view_rebuild(world):
    from workshop.core.models import Event
    w = world
    w['target']['stages'] = ['证据', '交付', '验收', '收尾']
    engine = make_engine(w)
    try:
        item = engine.log.get('items', w['data']['id'])
        assert item['stage'] == item['next'] == '证据'
        engine.pause('alpha')
        engine.ingest(Event('main_branch', 'next', 'alpha', 'f' * 40, 'next',
                            {'kind': 'ahead', 'from': w['a'], 'to': 'f' * 40}))
        engine.resume('alpha', [e['id'] for e in engine.missed('alpha')])
        assert engine.log.get('items', 'alpha:' + 'f' * 40)['stage'] == '证据'
        engine.log.rebuild()
        assert engine.log.get('items', 'alpha:' + 'f' * 40)['stage'] == '证据'
    finally:
        engine.log.close()


def test_short_flow_keeps_checks_approval_and_rejection_gates():
    from conftest import configuration
    from workshop.core.rules import Rules
    from workshop.core.gates import decide, validate_stages, allowed_advances
    from workshop.core.models import Evidence, Receipt
    stages = ['证据', '交付', '验收', '收尾']
    rules = Rules.validate(configuration())
    item = {'target': 'alpha', 'stage': '证据'}
    receipt = Receipt('done', 'checked', advance='交付')
    assert not decide(item, receipt, Evidence(passed=False), stages, rules).allowed
    action = rules.data['ask_first'][0]
    assert decide(item, receipt, Evidence(passed=True, actions=[action]), stages, rules).needs_approval == (action,)
    item['stage'] = '验收'
    result = decide(item, Receipt('failed', 'bad'), Evidence(passed=False), stages, rules)
    assert result.allowed and result.stage == '执行'
    assert '执行' in allowed_advances(item, stages)
    for bad in [['交付', '验收', '收尾'], ['证据', '交付', '收尾'], ['证据', '验收', '收尾']]:
        with pytest.raises(ValueError):
            validate_stages(bad)


def test_isolated_dependency_cache_keeps_credentials_out(tmp_path, monkeypatch):
    import json
    import sys
    from workshop.plugins.scheduled_release.io import IO
    markers = ['GH_TOKEN', 'GITHUB_TOKEN', 'SSH_AUTH_SOCK', 'AWS_SESSION_TOKEN',
               'AZURE_CLIENT_SECRET', 'GOOGLE_APPLICATION_CREDENTIALS', 'UV_INDEX_PASSWORD', 'UV_PYTHON']
    for key in markers:
        monkeypatch.setenv(key, 'credential-marker')
    cache = tmp_path / 'dependencies'
    command = [sys.executable, '-c', 'import json,os; print(json.dumps(dict(os.environ)))']
    first = IO(tmp_path).command(command, isolated=True, cache_dir=cache)
    second = IO(tmp_path).command(command, isolated=True, cache_dir=cache)
    env = json.loads(first['stdout'])
    assert first['exit_code'] == second['exit_code'] == 0
    assert env['UV_CACHE_DIR'] == str(cache.resolve())
    assert env['UV_PYTHON'] == str(__import__('pathlib').Path(sys._base_executable).resolve())
    assert env['UV_PYTHON_DOWNLOADS'] == 'never'
    assert env['HOME'] != json.loads(second['stdout'])['HOME']
    assert 'credential-marker' not in first['stdout']
    assert env['GIT_CONFIG_GLOBAL'] == '/dev/null' and env['KUBECONFIG'] == '/dev/null'
