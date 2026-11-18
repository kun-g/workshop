from datetime import datetime, timedelta

import pytest

from conftest import current, event, finish, item, reach
from workshop.core.engine import Engine
from workshop.core.models import After
from workshop.core.ports import Retryable
from workshop.reports.report import why
from workshop.core.view import TABLES


def automatic(engine, monkeypatch, stages=None):
    flow = engine.plugins.get('flow', 'flow')
    monkeypatch.setattr(flow, 'automatic', lambda i, s, t: stages is None or s in stages, raising=False)
    return flow


def test_all_program_stages_complete_without_rounds_and_replay(engine, monkeypatch):
    flow = automatic(engine, monkeypatch)
    monkeypatch.setattr(engine, 'launch', lambda *a: pytest.fail('no executor'))
    monkeypatch.setattr(flow, 'brief', lambda *a: pytest.fail('no brief'))
    monkeypatch.setattr(engine.plugins.get('selector', 'ordered'), 'select', lambda *a: pytest.fail('no selection'))
    engine.ingest(event())
    for _ in flow.path:
        engine.tick()
    assert item(engine)['state'] == '完成'
    assert engine.log.all('rounds') == []
    assert len(flow.after_keys) == len(flow.path)
    output = why(engine.log, 'alpha:one')
    for stage in flow.path:
        assert f'程序完成 {stage}' in output
        assert f'程序阶段 {stage} after' in output
    assert len(engine.log.full_item(item(engine))['history']) == len(flow.path)
    assert not engine.log.all('timers')
    facts = engine.log.facts('alpha:one')
    for _ in range(10):
        engine.clock.now += timedelta(seconds=1)
        engine.tick()
    assert item(engine)['state'] == '完成'
    assert engine.log.facts('alpha:one') == facts
    before = {t: engine.log.all(t) for t in TABLES}
    engine.log.rebuild()
    assert before == {t: engine.log.all(t) for t in TABLES}


@pytest.mark.parametrize('abandoned', [False, True])
def test_old_program_leave_timer_cannot_keep_repeating(engine, monkeypatch, abandoned):
    flow = automatic(engine, monkeypatch)
    engine.ingest(event())
    for _ in flow.path:
        engine.tick()
    # 模拟升级前留下的立即定时；最多再结算一次，不能继续产生定时链。
    engine.timer(item(engine), 'leave:program:old', '+0s', '已收尾')
    if abandoned:
        engine.change(item(engine), state='空闲')
        engine.abandon('alpha:one', '停止')
    calls = len(flow.after_keys)
    engine.tick()
    assert item(engine)['state'] == ('放弃' if abandoned else '完成')
    assert len(flow.after_keys) == calls + (0 if abandoned else 1)
    facts = engine.log.facts('alpha:one')
    for _ in range(10):
        engine.clock.now += timedelta(seconds=1)
        engine.tick()
    assert engine.log.facts('alpha:one') == facts
    assert all(t['fired'] for t in engine.log.all('timers'))


@pytest.mark.parametrize('kind', ['工序', '送回'])
def test_explicit_leave_after_completion_still_fires(engine, kind):
    reach(engine, '收尾')
    finish(engine, leave={'kind': kind, 'owner': '车间', 'at': '+1h', 'about': '复看'})
    assert item(engine)['state'] == '完成'
    assert len(engine.log.all('timers')) == 1
    engine.clock.now += timedelta(hours=1)
    engine.tick()
    assert engine.log.all('timers')[0]['fired']
    assert current(engine)['stage'] == '收尾'
    assert not engine.log.all('cards')


def test_abandon_running_program_explains_pause_without_mutation(engine, monkeypatch):
    flow = automatic(engine, monkeypatch)
    def pending(*args):
        raise Retryable('still running')
    monkeypatch.setattr(flow, 'after', pending)
    engine.ingest(event())
    engine.tick()
    assert item(engine)['program']
    before = {t: engine.log.all(t) for t in TABLES}
    facts = engine.log.facts()
    with pytest.raises(ValueError) as error:
        engine.abandon('alpha:one', '停止')
    assert 'workshop pause alpha' in str(error.value)
    assert '不会自动放弃或取消在跑作业' in str(error.value)
    assert '项目经理' in str(error.value)
    assert before == {t: engine.log.all(t) for t in TABLES}
    assert engine.log.facts() == facts


def test_two_program_stages_then_executor(engine, monkeypatch):
    automatic(engine, monkeypatch, {'接单', '说清楚'})
    engine.ingest(event())
    engine.tick()
    engine.tick()
    assert item(engine)['stage'] == '执行'
    assert engine.log.all('rounds') == []
    engine.tick()
    assert current(engine)['stage'] == '执行'


@pytest.mark.parametrize('declared', [False, None])
def test_false_or_missing_automatic_keeps_original_path(engine, monkeypatch, declared):
    if declared is False:
        automatic(engine, monkeypatch, set())
    engine.ingest(event())
    engine.tick()
    assert current(engine)['stage'] == '接单'
    assert not item(engine).get('program')


def test_program_gate_failure_never_calls_after(engine, monkeypatch):
    flow = automatic(engine, monkeypatch)
    flow.passed = False
    engine.ingest(event())
    for _ in range(5):
        engine.tick()
    assert item(engine)['state'] == '卡住'
    assert item(engine)['stage'] == '接单'
    assert flow.after_keys == []
    assert engine.log.all('rounds') == []


@pytest.mark.parametrize('rerun', [False, True])
@pytest.mark.parametrize('notices', ['send', 'hold'])
def test_program_pending_limit_without_notice(engine, monkeypatch, rerun, notices):
    flow = automatic(engine, monkeypatch)
    engine.rules.data['notices'] = notices
    keys = []
    def after(i, s, k, t):
        keys.append(k)
        return After(pending=True, rerun=rerun, recheck_after='+20m', reason='等待', next='回查')
    monkeypatch.setattr(flow, 'after', after)
    engine.ingest(event())
    engine.tick()
    for _ in range(4):
        if not rerun:
            engine.tick()
            assert len(keys) == item(engine)['after_pending_count']
            engine.clock.now = datetime.fromisoformat(item(engine)['after_wait']['at'])
        engine.tick()
    assert len(keys) == 5
    assert len(set(keys)) == (5 if rerun else 1)
    assert item(engine)['state'] == '卡住'
    for _ in range(3):
        engine.clock.now += timedelta(hours=2)
        engine.tick()
    assert len(keys) == 5 and not engine.log.all('rounds')
    cards = [c for c in engine.log.all('cards') if 'pending:' in c['key']]
    assert len(cards) == 0
    held = [f for f in engine.log.facts() if f['kind'] == 'notice_held']
    assert len(held) == 0


def test_program_after_retry_survives_restart_with_same_key(engine, monkeypatch):
    flow = automatic(engine, monkeypatch)
    keys = []
    def after(i, s, k, t):
        keys.append(k)
        if len(keys) == 1:
            raise Retryable('temporary')
        return After()
    monkeypatch.setattr(flow, 'after', after)
    engine.ingest(event())
    engine.tick()
    assert item(engine)['program']
    engine = Engine(engine.log, engine.rules, engine.plugins, engine.clock)
    engine.tick()
    assert len(keys) == 1
    engine.clock.now += timedelta(hours=1)
    engine.tick()
    assert keys[0] == keys[1]
    assert item(engine)['stage'] == '说清楚'
    assert not item(engine).get('program')
    assert not engine.log.all('rounds')


def test_program_runs_with_executor_slots_full(engine, monkeypatch):
    engine.rules.data['limits']['concurrent'] = 1
    engine.ingest(event(target='beta', ext_id='beta'))
    engine.tick()
    automatic(engine, monkeypatch)
    engine.ingest(event())
    engine.tick()
    assert item(engine)['stage'] == '说清楚'
    assert len(engine.log.all('rounds')) == 1


def test_program_pending_recheck_completes_after_restart(engine, monkeypatch):
    flow = automatic(engine, monkeypatch, {'接单'})
    keys = []
    def after(i, s, k, t):
        keys.append(k)
        return After(pending=True, reason='等待', next='回查') if len(keys) == 1 else After()
    monkeypatch.setattr(flow, 'after', after)
    engine.ingest(event())
    engine.tick()
    at = item(engine)['after_wait']['at']
    engine.log.rebuild()
    engine = Engine(engine.log, engine.rules, engine.plugins, engine.clock)
    engine.tick()
    assert len(keys) == 1
    engine.clock.now = datetime.fromisoformat(at)
    engine.tick()
    assert keys[0] == keys[1]
    assert item(engine)['stage'] == '说清楚'
    assert current(engine)['stage'] == '说清楚'
    assert len(engine.log.all('rounds')) == 1


def test_program_delivery_requires_center_authorization(engine, monkeypatch):
    flow = automatic(engine, monkeypatch)
    flow.actions = ['对外发消息']
    engine.ingest(event())
    for _ in range(8):
        engine.tick()
    assert item(engine)['stage'] == '审查' and item(engine)['state'] == '卡住'
    assert len(flow.after_keys) == 4
    assert not engine.log.all('cards') and not engine.log.all('rounds')


def test_program_jobs_resume_without_inline_calls(tmp_path, monkeypatch):
    import os
    import sys
    import job_plugins
    from conftest import Clock
    from test_jobs import ManualJobs, ForbiddenPlugins, configure, rules, RUNNER
    from workshop.core.log import Log

    monkeypatch.setenv('JOBTEST_DIR', str(tmp_path))
    monkeypatch.setattr(job_plugins.SlowFlow, 'automatic', lambda *a: True, raising=False)
    configure(tmp_path)
    locks = {}
    jobs = ManualJobs(tmp_path / 'jobs', [sys.executable, RUNNER], locks)
    log = Log(tmp_path / 'workshop.db')
    plugins = ForbiddenPlugins()
    engine = Engine(log, rules(), plugins, Clock(), jobs)
    engine.ingest(event())
    restarted = False
    try:
        for _ in range(100):
            engine.tick()
            if item(engine).get('program') and not restarted:
                engine = Engine(log, engine.rules, plugins, engine.clock, jobs)
                restarted = True
            if item(engine)['state'] == '完成':
                break
            jobs.finish()
        assert restarted
        assert item(engine)['state'] == '完成'
        assert not plugins.calls
        assert not log.all('rounds')
        assert not job_plugins.calls('launch')
        assert len(job_plugins.calls('after')) == 8
    finally:
        for descriptor in locks.values():
            os.close(descriptor)
        log.close()
