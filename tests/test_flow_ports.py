from copy import deepcopy
from datetime import datetime, timedelta
from dataclasses import asdict

import pytest

from conftest import current, event, finish, item, reach
from workshop.core.models import After, Classification, FlowBrief
from workshop.core.view import TABLES


def test_external_creation_waits_for_classification_and_routes_by_card(engine, monkeypatch):
    engine.ingest(event())
    original = item(engine)
    card = engine.card(original, 'test', 'notice', {'subject': 'test', 'text': 'test', 'fyi': True})
    engine.fact('card_changed', {'id': card, 'external': 'Q99'})
    flow = engine.plugins.get('flow', 'flow')
    def classify(ev, existing, target, lookup):
        found = lookup('Q99')
        assert found['item']['id'] == original['id']
        assert existing is None
        return Classification('update', found['item']['object_key'])
    monkeypatch.setattr(flow, 'classify', classify)
    engine.ingest(event(key='message', ext_id='external'), ingress='external')
    assert engine.log.get('items', 'alpha:message') is None
    engine.classify_events()
    assert len(engine.log.all('items')) == 1
    assert len(item(engine)['materials']) == 2
    before = {t: engine.log.all(t) for t in TABLES}
    engine.log.rebuild()
    assert before == {t: engine.log.all(t) for t in TABLES}


def test_classifier_discards_without_fault_or_creating_item(engine, monkeypatch):
    flow = engine.plugins.get('flow', 'flow')
    monkeypatch.setattr(flow, 'classify', lambda *args: Classification(discard=True))
    engine.ingest(event(), ingress='external')
    engine.classify_events()
    engine.classify_events()
    assert engine.log.all('items') == [] and not engine.control('alpha').get('broken')
    assert len([f for f in engine.log.facts() if f['kind'] == 'event_discarded']) == 1
    assert all(e['done'] for e in engine.log.all('effects'))
    engine.log.rebuild()
    assert engine.log.all('items') == []


def test_aliases_replay_pause_and_collision(engine, monkeypatch):
    engine.ingest(event())
    flow = engine.plugins.get('flow', 'flow')
    monkeypatch.setattr(flow, 'after', lambda *args: After(aliases=['7']))
    engine.after(item(engine), '接单', 'once', engine.rules.target('alpha'))
    engine.after(item(engine), '接单', 'once', engine.rules.target('alpha'))
    engine.ingest(event(key='7', ext_id='same-object'))
    assert len(engine.log.all('items')) == 1
    assert len([f for f in engine.log.facts() if f['kind'] == 'alias_bound']) == 1
    engine.pause('alpha')
    engine.ingest(event(key='7', ext_id='paused'))
    engine.resume('alpha', [engine.missed('alpha')[0]['id']])
    assert len(engine.log.all('items')) == 1 and len(item(engine)['materials']) == 3
    engine.ingest(event(key='other', ext_id='other'))
    with pytest.raises(ValueError, match='别名冲突'):
        engine.after(item(engine, 'other'), '接单', 'collision', engine.rules.target('alpha'))
    assert engine.resolve_key('alpha', '7') == 'one'
    before = engine.log.all('aliases')
    engine.log.rebuild()
    assert engine.log.all('aliases') == before


def test_structured_brief_reaches_executor(engine, monkeypatch, tmp_path):
    flow = engine.plugins.get('flow', 'flow')
    monkeypatch.setattr(flow, 'brief', lambda *args: FlowBrief('只读', str(tmp_path), True))
    engine.ingest(event())
    engine.tick()
    brief = current(engine)['brief']
    assert brief['workdir'] == str(tmp_path) and brief['readonly']
    assert brief['instructions'] == '只读'


@pytest.mark.parametrize('rerun', [False, True])
def test_pending_stops_at_limit_and_notifies_once(engine, monkeypatch, rerun):
    reach(engine, '交付')
    flow = engine.plugins.get('flow', 'flow')
    keys = []
    def after(item, stage, key, target):
        keys.append(key)
        return After(pending=True, rerun=rerun, reason='等外部完成', next='继续核对')
    monkeypatch.setattr(flow, 'after', after)
    finish(engine, '验收')
    for _ in range(4):
        if rerun:
            finish(engine, '验收')
        else:
            assert not engine.active()
            engine.clock.now = datetime.fromisoformat(item(engine)['after_wait']['at'])
            engine.tick()
    assert item(engine)['state'] == '卡住'
    assert item(engine)['stage'] == '交付' and item(engine)['failures'] == 0
    assert len(keys) == 5 and len(set(keys)) == (5 if rerun else 1)
    for _ in range(8):
        engine.clock.now += timedelta(hours=2)
        engine.tick()
    assert len(keys) == 5 and not engine.active()
    assert len([c for c in engine.log.all('cards') if 'pending:' in c['key']]) == 1


def test_pending_recheck_recovers_without_executor_and_replays(engine, monkeypatch):
    reach(engine, '交付')
    flow = engine.plugins.get('flow', 'flow')
    keys = []
    def after(item, stage, key, target):
        keys.append(key)
        assert item['result']['advance'] == '验收'
        return After(pending=True, reason='等外部完成', next='核对') if len(keys) == 1 else After()
    monkeypatch.setattr(flow, 'after', after)
    finish(engine, '验收')
    rounds = len(engine.log.all('rounds'))
    for _ in range(3):
        engine.tick()
    assert len(engine.log.all('rounds')) == rounds and len(keys) == 1
    engine.log.rebuild()
    engine.clock.now = datetime.fromisoformat(item(engine)['after_wait']['at'])
    engine.tick()
    assert keys[0] == keys[1]
    assert item(engine)['stage'] == '验收' and current(engine)['stage'] == '验收'
    assert item(engine)['after_wait'] is None


@pytest.mark.parametrize('stage,next_stage', [('接单','说清楚'), ('说清楚','执行'), ('执行','证据'), ('证据','审查'), ('审查','交付'), ('交付','验收'), ('收尾',None)])
def test_failed_evidence_cannot_advance_and_uses_gate_retry(engine, stage, next_stage):
    reach(engine, stage)
    original = current(engine)['executor']
    engine.plugins.get('flow', 'flow').passed = False
    for attempt in range(3):
        finish(engine, next_stage)
        assert item(engine)['stage'] == stage
        if attempt < 2:
            assert current(engine)['executor'] == original
    assert item(engine)['state'] == '等你'
    assert not engine.control('alpha').get('broken')


def test_unchanged_source_cursor_does_not_append_facts(engine):
    engine.sources()
    count = len([f for f in engine.log.facts() if f['kind'] == 'cursor'])
    engine.sources()
    assert len([f for f in engine.log.facts() if f['kind'] == 'cursor']) == count


def test_sources_wait_for_unsettled_after_to_restore_aliases(engine, monkeypatch):
    engine.ingest(event())
    engine.tick()
    row = current(engine)
    engine.submit(row['token'], {'outcome': 'done', 'summary': 'registered', 'advance': '说清楚'})
    flow = engine.plugins.get('flow', 'flow')
    monkeypatch.setattr(flow, 'after', lambda *args: After(aliases=['7']))
    engine.fact('effect', {'id': f"after:{row['token']}", 'done': False})
    engine.plugins.get('source', 'source').events = [event(key='7', ext_id='new-remote')]
    engine.tick()
    assert engine.resolve_key('alpha', '7') == 'one'
    engine.tick()
    assert len(engine.log.all('items')) == 1


def test_alias_does_not_make_agent_event_trusted(engine, monkeypatch):
    engine.ingest(event())
    flow = engine.plugins.get('flow', 'flow')
    monkeypatch.setattr(flow, 'after', lambda *args: ['7'])
    engine.after(item(engine), '接单', 'alias', engine.rules.target('alpha'))
    engine.tick()
    monkeypatch.setattr(flow, 'classify', lambda *args: pytest.fail('agent should not classify'))
    engine.ingest(event(key='7', ext_id='agent', action='rework'), round_token=current(engine)['token'], ingress='external')
    assert not item(engine)['pending_rework'] and len(engine.log.all('items')) == 1


def test_cleanup_on_startup_keeps_active_round_until_stopped(engine, monkeypatch):
    flow = engine.plugins.get('flow', 'flow')
    monkeypatch.setattr(flow, 'brief', lambda *args: FlowBrief('review', '/owned/copy', True))
    calls = []
    monkeypatch.setattr(flow, 'cleanup', lambda target, keep: calls.append(list(keep)), raising=False)
    engine.ingest(event())
    engine.tick()
    engine.cleaned_flows = False  # 模拟 daemon 重启，未停止的轮仍在事实库。
    engine.tick()
    assert '/owned/copy' in calls[-1]
    finish(engine, '说清楚')
    assert calls[-1] == []


def test_pending_transient_errors_reset_after_successful_recheck(engine, monkeypatch):
    from workshop.core.ports import Retryable
    reach(engine, '交付')
    flow = engine.plugins.get('flow', 'flow')
    calls = []
    def after(item, stage, key, target):
        calls.append(key)
        if len(calls) in (2, 4):
            raise Retryable('temporary')
        return After(pending=True, reason='等完成', next='回查')
    monkeypatch.setattr(flow, 'after', after)
    finish(engine, '验收')
    for _ in range(4):
        engine.clock.now += timedelta(hours=1)
        engine.tick()
    assert not engine.control('alpha').get('broken')
    assert item(engine)['after_pending_count'] == 3
    assert not (engine.log.get('controls', 'retry:' + calls[0]) or {}).get('failures')
