from dataclasses import replace
from datetime import timedelta

import pytest

from conftest import event, item, current, reach, finish
from workshop.core.models import Relations
from workshop.core.ports import Retryable
from workshop.core.report import status
from workshop.core import relations
from workshop.core.jobs import Pending, encode, decode


def ref(key='beta#1', complete=False):
    return dict(id=key, name=key, complete=complete)


def install(engine, snapshot):
    flow = engine.plugins.get('flow', 'flow')
    state = {'value': snapshot}
    def read(objects, target):
        value = state['value']
        if isinstance(value, BaseException):
            raise value
        return {o['id']: value for o in objects}
    flow.relations = read
    return state


def test_prerequisites_wait_release_and_metrics(engine):
    state = install(engine, Relations(prerequisites=[ref(), ref('beta#2', True)]))
    engine.ingest(event())
    engine.tick()
    assert item(engine)['state'] == '等前置'
    assert engine.occupied() == 0
    assert not engine.log.all('cards')
    assert '等前置：beta#1' in status(engine.log)
    engine.clock.now += timedelta(seconds=60)
    state['value'] = replace(state['value'], prerequisites=[ref(complete=True), ref('beta#2', True)])
    engine.tick()
    assert current(engine)['stage'] == '接单'
    assert item(engine)['state'] == '跑着'
    from workshop.core.metrics import calculate, read_snapshot
    result = calculate(*read_snapshot(engine.log.path), engine.clock.now-timedelta(seconds=60), engine.clock.now)
    assert result['summary']['seconds']['等前置'] == 60
    assert result['summary']['seconds']['排队'] == 0
    assert result['summary']['occupied_seconds'] == 0


@pytest.mark.parametrize('cached', [False, True])
def test_read_failure_blocks_only_affected_and_recovers(engine, cached):
    state = install(engine, Relations(prerequisites=[ref()]) if cached else Retryable('HTTP 403'))
    engine.ingest(event())
    engine.tick()
    state['value'] = Retryable('HTTP 403')
    engine.ingest(event(target='beta', ext_id='2'))
    engine.tick()
    assert item(engine)['state'] == '卡住'
    assert item(engine, target='beta')['state'] == '跑着'
    assert not any(r['item'] == 'alpha:one' for r in engine.active())
    assert not engine.control('alpha').get('broken')
    count = len(engine.log.all('cards'))
    engine.tick()
    assert len(engine.log.all('cards')) == count
    state['value'] = Relations()
    engine.tick()
    assert item(engine)['state'] == '跑着'


def test_first_read_and_expired_read_never_dispatch(engine):
    state = install(engine, Pending('reading'))
    engine.ingest(event())
    engine.tick()
    assert engine.occupied() == 0
    state['value'] = Relations(prerequisites=[ref()])
    engine.tick()
    state['value'] = Pending('refreshing')
    engine.tick()
    assert item(engine)['state'] == '等前置'
    assert engine.occupied() == 0
    state['value'] = Relations()
    engine.tick()
    assert engine.occupied() == 1


def test_members_only_wait_at_acceptance_and_release_without_occupancy(engine):
    state = install(engine, Relations(members=[ref('one'), ref('two')], converge='验收'))
    reach(engine, '执行')
    assert current(engine)['stage'] == '执行'
    assert item(engine)['state'] == '跑着'
    for stage in ('证据', '审查', '交付'):
        finish(engine, stage)
        assert current(engine)['stage'] == stage
    engine.change(item(engine), serial_hold=True)
    finish(engine, '验收')
    assert item(engine)['state'] == '等子单'
    assert not engine.active() and engine.occupied() == 0
    assert not item(engine)['serial_hold']
    assert not engine.log.all('cards')
    assert not any(r['stage'] == '验收' for r in engine.log.all('rounds'))
    assert '等子单：one、two' in status(engine.log)
    engine.clock.now += timedelta(seconds=60)
    state['value'] = replace(state['value'], members=[ref('one', True), ref('two')])
    engine.tick()
    assert item(engine)['state'] == '等子单'
    assert not engine.active()
    state['value'] = replace(state['value'], members=[ref('one', True), ref('two', True)])
    engine.tick()
    assert current(engine)['stage'] == '验收'
    from workshop.core.metrics import calculate, read_snapshot
    result = calculate(*read_snapshot(engine.log.path), engine.clock.now-timedelta(seconds=60), engine.clock.now)
    assert result['summary']['seconds']['等子单'] == 60
    assert result['summary']['seconds']['等前置'] == 0
    assert result['summary']['occupied_seconds'] == 0


def test_completed_members_do_not_skip_execution(engine):
    install(engine, Relations(members=[ref(complete=True)], converge='验收'))
    reach(engine, '验收')
    assert [r['stage'] for r in engine.log.all('rounds')] == list(engine.plugins.get('flow', 'flow').path[:-1])


def test_members_and_prerequisites_are_independent(engine):
    state = install(engine, Relations(prerequisites=[ref('before')], members=[ref()], converge='验收'))
    engine.ingest(event())
    engine.tick()
    assert item(engine)['state'] == '等前置'
    state['value'] = replace(state['value'], prerequisites=[ref('before', True)])
    engine.tick()
    assert current(engine)['stage'] == '接单'


def test_other_hold_is_not_erased_and_related_does_not_block(engine):
    state = install(engine, Relations(related=[ref()]))
    engine.ingest(event())
    engine.change(item(engine), state='卡住', blocked_by='已有故障', next='修复已有故障')
    engine.tick()
    assert item(engine)['blocked_by'] == '已有故障'
    state['value'] = Relations(prerequisites=[ref()])
    engine.tick()
    assert item(engine)['blocked_by'] == '已有故障'
    engine.change(item(engine), state='空闲', blocked_by='')
    state['value'] = Relations(related=[ref()])
    engine.tick()
    assert engine.occupied() == 1


def test_snapshot_validation_and_job_roundtrip():
    value = Relations(prerequisites=[ref()], related=[ref('beta#2')])
    assert decode(encode({'one': value})) == {'one': value}
    with pytest.raises(ValueError):
        Relations(prerequisites=[{'id': 'x', 'name': 'x', 'complete': None}]).validate()
    with pytest.raises(ValueError):
        Relations(members=[ref()]).validate()


def test_wait_releases_serial_position(engine):
    install(engine, Relations(prerequisites=[ref()]))
    engine.ingest(event())
    engine.change(item(engine), stage='执行', serial_hold=True)
    engine.tick()
    assert not item(engine)['serial_hold']
    assert item(engine)['blocked_by'] == ''


def test_slow_brief_cannot_reserve_obsolete_relations(engine):
    state = install(engine, Relations())
    flow = engine.plugins.get('flow', 'flow')
    def brief(work, stage, target):
        state['value'] = Relations(prerequisites=[ref()])
        relations.refresh(engine, [item(engine)])
        return '检查期间关系已变化'
    flow.brief = brief
    engine.ingest(event())
    engine.tick()
    assert engine.occupied() == 0
    engine.tick()
    assert item(engine)['state'] == '等前置'
    engine.log.rebuild()
    assert item(engine)['relations']['prerequisites'][0]['id'] == 'beta#1'
    assert item(engine)['state'] == '等前置'


def test_pending_stage_job_does_not_escape_tick(engine, monkeypatch):
    install(engine, Relations(members=[ref()], converge='验收'))
    engine.ingest(event())
    original = engine.call
    def call(port, name, method, *args, **kw):
        if port == 'flow' and method == 'stages':
            raise Pending('cold-stage-job')
        return original(port, name, method, *args, **kw)
    monkeypatch.setattr(engine, 'call', call)
    engine.tick()
    assert engine.occupied() == 0
    assert not engine.control('@tick').get('failures')


def test_total_rejection_returns_to_own_execution(engine):
    install(engine, Relations(members=[ref(complete=True)], converge='验收'))
    reach(engine, '验收')
    engine.plugins.get('flow', 'flow').passed = False
    finish(engine, '执行')
    assert current(engine)['stage'] == '执行'
    assert item(engine)['state'] == '跑着'


def test_removing_last_member_preserves_own_stage(engine):
    state = install(engine, Relations(members=[ref()], converge='验收'))
    reach(engine, '交付')
    finish(engine, '验收')
    state['value'] = Relations()
    engine.tick()
    assert current(engine)['stage'] == '验收'


def test_existing_skipped_execution_is_restored(engine):
    install(engine, Relations(members=[ref()], converge='验收'))
    engine.ingest(event())
    engine.change(item(engine), stage='验收', aggregate=True, aggregate_origin='说清楚')
    engine.tick()
    assert not item(engine)['aggregate']
    assert current(engine)['stage'] == '说清楚'


def test_rework_while_waiting_for_members_can_execute(engine):
    install(engine, Relations(members=[ref()], converge='验收'))
    reach(engine, '交付')
    finish(engine, '验收')
    engine.change(item(engine), pending_rework=True)
    engine.tick()
    assert current(engine)['stage'] == '执行'


def test_pending_approval_rejection_cannot_be_swallowed_by_convergence(engine):
    from conftest import reach, finish
    from workshop.core.models import Answer
    flow = engine.plugins.get('flow', 'flow')
    flow.actions = ['对外发消息']
    reach(engine, '审查')
    finish(engine, '交付')
    card = next(c for c in engine.log.all('cards') if c.get('actions'))
    install(engine, Relations(members=[ref(complete=True)], converge='验收'))
    engine.plugins.get('channel', 'channel').answers = [Answer(card['id'], 'no', True)]
    for _ in range(3):
        engine.tick()
    assert item(engine)['stage'] == '执行'
    assert item(engine)['state'] == '跑着'
    assert current(engine)['stage'] == '执行'
    assert any(f['kind'] == 'core_feedback' for f in engine.log.facts())
