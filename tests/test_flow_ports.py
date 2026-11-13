from copy import deepcopy
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


def test_classifier_can_defer_without_fault_or_creating_item(engine, monkeypatch):
    flow = engine.plugins.get('flow', 'flow')
    monkeypatch.setattr(flow, 'classify', lambda *args: None)
    engine.ingest(event(), ingress='external')
    engine.classify_events()
    assert engine.log.all('items') == [] and not engine.control('alpha').get('broken')
    monkeypatch.setattr(flow, 'classify', lambda *args: Classification())
    engine.classify_events()
    assert item(engine)


def test_aliases_replay_pause_and_collision(engine, monkeypatch):
    engine.ingest(event())
    flow = engine.plugins.get('flow', 'flow')
    monkeypatch.setattr(flow, 'after', lambda *args: After(aliases=['7']))
    engine.after(flow, item(engine), '接单', 'once', engine.rules.target('alpha'))
    engine.after(flow, item(engine), '接单', 'once', engine.rules.target('alpha'))
    engine.ingest(event(key='7', ext_id='same-object'))
    assert len(engine.log.all('items')) == 1
    assert len([f for f in engine.log.facts() if f['kind'] == 'alias_bound']) == 1
    engine.pause('alpha')
    engine.ingest(event(key='7', ext_id='paused'))
    engine.resume('alpha', [engine.missed('alpha')[0]['id']])
    assert len(engine.log.all('items')) == 1 and len(item(engine)['materials']) == 3
    engine.ingest(event(key='other', ext_id='other'))
    with pytest.raises(ValueError, match='别名冲突'):
        engine.after(flow, item(engine, 'other'), '接单', 'collision', engine.rules.target('alpha'))
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


def test_pending_stays_same_stage_no_failure_and_notifies_once(engine, monkeypatch):
    reach(engine, '交付')
    flow = engine.plugins.get('flow', 'flow')
    monkeypatch.setattr(flow, 'after', lambda *args: After(pending=True, reason='等外部完成', next='继续核对'))
    for _ in range(7):
        finish(engine, '验收')
        assert item(engine)['stage'] == '交付'
        assert item(engine)['failures'] == 0
    cards = [c for c in engine.log.all('cards') if 'pending:' in c['key']]
    assert len(cards) == 1
    monkeypatch.setattr(flow, 'after', lambda *args: After())
    finish(engine, '验收')
    assert item(engine)['stage'] == '验收' and item(engine)['after_pending_count'] == 0


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
    engine.after(flow, item(engine), '接单', 'alias', engine.rules.target('alpha'))
    engine.tick()
    monkeypatch.setattr(flow, 'classify', lambda *args: pytest.fail('agent should not classify'))
    engine.ingest(event(key='7', ext_id='agent', action='rework'), round_token=current(engine)['token'], ingress='external')
    assert not item(engine)['pending_rework'] and len(engine.log.all('items')) == 1
