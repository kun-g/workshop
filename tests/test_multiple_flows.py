from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta

import pytest

from conftest import build, configuration, event, current
from workshop.core.engine import Engine, finder
from workshop.core.models import Event, After
from workshop.core.rules import Rules
from workshop.core.holding import resolve
from workshop.reports.report import unfiled, why
from workshop.core.flows import matches


def config():
    data = configuration()
    data['targets']['alpha'] = {
        'default_flow': 'flow', 'sources': [], 'short_name': 'a', 'common': 'shared',
        'flows': {
            'flow': {'triggers': [
                {'source': 'input', 'when': {'kind': 'sample'}, 'action': 'ready'},
                {'source': 'cli:input', 'action': 'update'}]},
            'other': {'common': 'local', 'triggers': [
                {'source': 'input', 'when': {'actor': ['one', 'two'], 'text': '^ok'}, 'action': 'ready'},
                {'source': 'input', 'when': {'kind': 'sample'}, 'action': 'update'},
                {'source': 'cli:input', 'action': 'update'}]}}}
    return data


def sample(ext='1', **kwargs):
    return Event(
        'input', ext, 'alpha', 'one', 'test', {'kind': 'sample', 'actor': 'two', 'text': 'okay'}, **kwargs)


def test_fanout_dedup_unfiled_and_per_target_conditions(tmp_path):
    data = config()
    data['targets']['beta'] = {**deepcopy(data['targets']['alpha']), 'default_flow': 'other',
                               'flows': {'other': {'triggers': [{'source': 'input', 'when': {'kind': 'different'}, 'action': 'ready'}]}}}
    e = build(tmp_path, data)
    assert e.ingest(sample())
    assert not e.ingest(sample())
    assert {i['id'] for i in e.log.all('items')} == {'alpha:one', 'alpha:other:one'}
    assert e.rules.target('alpha', 'other')['common'] == 'local'
    assert e.rules.target('alpha', 'flow')['common'] == 'shared'
    e.ingest(replace(sample('no'), body={}))
    e.ingest(replace(sample(), target='beta'))
    assert len(e.log.all('items')) == 2
    assert '没有流程匹配 alpha' in unfiled(e.log, e.rules.data)
    assert '没有流程匹配 beta' in unfiled(e.log, e.rules.data)
    facts = [f for f in e.log.facts() if f['kind'] == 'event' and not f['body'].get('unmatched')]
    assert len(facts) == 2 and len({f['ext_id'] for f in facts}) == 2
    assert {f['body']['ext_id'] for f in facts} == {'1'}  # upstream lookup keeps original id
    e.tick()
    assert len(e.active()) == 2
    e.ingest(sample('2'))
    e.tick()
    assert len(e.active()) == 2
    e.log.close()


@pytest.mark.parametrize(('body', 'condition', 'expected'), [
    ({'x': 'ok'}, {'x': '^ok'}, True), ({'x': 'not ok'}, {'x': '^ok'}, False),
    ({'x': 'a'}, {'x': ['a', 'b']}, True), ({'x': True}, {'x': 1}, False),
    ({}, {'x': None}, False), ({'x': None}, {'x': None}, True),
    ({'x': 'a'}, {'x': 'a', 'y': 1}, False), ('body', {}, True),
])
def test_field_matching(body, condition, expected):
    assert matches(body, condition) is expected


@pytest.mark.parametrize('trigger,field', [
    ({'source': 'input', 'when': {'x': '^['}, 'action': 'ready'}, 'when.x'),
    ({'source': 'input', 'when': [], 'action': 'ready'}, 'when'),
    ({'source': 'input', 'action': 'oops'}, 'action'),
    ({'source': 'input', 'when': {'x': {'deep': 1}}, 'action': 'ready'}, 'when.x'),
])
def test_invalid_triggers_rejected_before_dispatch(trigger, field):
    data = config()
    data['targets']['alpha']['flows']['other']['triggers'] = [trigger]
    with pytest.raises(ValueError, match=f'alpha 流程 other.*{field}'):
        Rules.validate(data)


def test_old_log_round_alias_timer_and_reordering_survive(tmp_path):
    e = build(tmp_path)
    # Persist exactly the old event format, with no newly added dataclass fields.
    old = asdict(event())
    for key in ('flow', 'flow_default', 'action_id', 'previous_ids'):
        old.pop(key)
    e.fact('event', {**old, 'release_gate': True}, 'alpha:one', source='input', ext_id='1')
    e.tick()
    token = current(e)['token']
    e.fact('alias_bound', {'id': 'alpha:alias', 'object_key': 'one', 'item': 'alpha:one'}, 'alpha:one')
    rules = config()
    e = Engine(e.log, Rules.validate(rules), e.plugins, e.clock)
    assert e.ingest(sample())  # default old id consumed, the new flow has its own event
    assert len(e.log.get('items', 'alpha:one')['materials']) == 1
    assert resolve(e.log, e.rules.data['targets'], 'a#alias') == 'alpha:one'
    assert 'alpha:one' in why(e.log, 'alpha:one', e.rules.data, e.plugins)
    assert e.submit(token, {'outcome': 'done', 'summary': 'old round', 'advance': '说清楚'})
    e.tick()
    assert e.log.get('items', 'alpha:one')['stage'] == '说清楚'
    other = e.log.get('items', 'alpha:other:one')
    e.timer(other, 'wake', '+1s', 'continue')
    e.clock.now += timedelta(seconds=2)
    e.timers()
    timer_facts = [f for f in e.log.facts() if f['kind'] == 'event' and f['body']['source'] == 'timer']
    assert [f['item'] for f in timer_facts] == ['alpha:other:one']
    assert e.release_here('alpha:other:one', '放', 'test-session') == 'alpha:other:one'
    assert [f['item'] for f in e.log.facts() if f['kind'] == 'event' and f['body']['source'] == 'session'] == ['alpha:other:one']
    rules['targets']['alpha'].pop('default_flow')
    flows = rules['targets']['alpha']['flows']
    rules['targets']['alpha']['flows'] = dict(reversed(list(flows.items())))
    e = Engine(e.log, Rules.validate(rules), e.plugins, e.clock)
    assert e.rules.target('alpha')['flow'] == 'flow'
    assert e.item_target(e.log.get('items', 'alpha:one'))['plugin'] == 'flow'
    before = e.log.all('items')
    e.log.rebuild()
    assert e.log.all('items') == before
    e.log.close()


def test_flow_alias_classification_and_source_id_are_isolated(tmp_path):
    e = build(tmp_path, config())
    e.ingest(sample())
    for identity in ('alpha:one', 'alpha:other:one'):
        obj = e.log.get('items', identity)
        flow = e.plugins.get('flow', e.item_target(obj)['plugin'])
        flow.after = lambda *args: After(aliases=['alias'])
        e.after(obj, '接单', identity, e.item_target(obj))
        lookup = finder(e.log, e.scope('alpha', obj['flow']))
        assert lookup(object_key='alias')['item']['id'] == identity
    e.plugins.get('flow', 'other').upstream[('cli:input', 'msg-1')] = {'sender': 'Kun', 'action': 'rework'}
    e.ingest(replace(sample('msg-1'), object_key='alias'), ingress='external')
    e.classify_events()
    assert e.log.get('items', 'alpha:other:one')['pending_rework']
    assert not e.log.get('items', 'alpha:one')['pending_rework']
    assert resolve(e.log, e.rules.data['targets'], 'alpha:other:alias') == 'alpha:other:one'
    e.log.close()


def test_depth_and_plugin_failure_do_not_stop_other_flow(tmp_path):
    e = build(tmp_path, config())
    # Only the first flow matches the over-limit input.
    e.ingest(replace(sample(), body={'kind': 'sample'}, depth=9))
    e.ingest(sample('2'))
    assert e.log.get('items', 'alpha:one')['state'] == '卡住'
    assert e.log.get('items', 'alpha:other:one')['state'] == '空闲'
    e.broken('alpha', 'flow', 'flow', RuntimeError('crash'))
    assert e.flow_broken('alpha', 'flow')
    assert not e.flow_broken('alpha', 'other')
    e.tick()
    assert [r['item'] for r in e.active()] == ['alpha:other:one']
    e.log.close()


def test_agent_events_cannot_use_triggers_to_gain_ready(tmp_path):
    e = build(tmp_path, config())
    e.ingest(sample())
    e.tick()
    token = current(e)['token']
    e.ingest(replace(sample('child'), object_key='child', action='ready'), round_token=token)
    for identity in ('alpha:child', 'alpha:other:child'):
        obj = e.log.get('items', identity)
        assert obj['depth'] == 1 and not obj['pending_rework']
        facts = [f for f in e.log.facts(identity) if f['kind'] == 'event']
        assert facts[0]['body']['action'] == 'update'
    e.log.close()


def test_pause_replay_does_not_fan_out_again(tmp_path):
    e = build(tmp_path, config())
    e.pause('alpha')
    e.ingest(sample())
    assert not e.log.all('items')
    missed = e.missed('alpha')
    assert len(missed) == 2
    e.resume('alpha', [r['id'] for r in missed])
    assert {i['id'] for i in e.log.all('items')} == {'alpha:one', 'alpha:other:one'}
    e.log.close()


def test_empty_when_and_first_trigger_win(tmp_path):
    data = config()
    triggers = data['targets']['alpha']['flows']['flow']['triggers']
    triggers.append({'source': 'input', 'action': 'update'})
    e = build(tmp_path, data)
    e.ingest(sample())
    rows = [f for f in e.log.facts('alpha:one') if f['kind'] == 'event']
    assert len(rows) == 1 and rows[0]['body']['action'] == 'ready'
    e.ingest(replace(sample('other'), body={}))
    rows = [f for f in e.log.facts('alpha:one') if f['kind'] == 'event']
    assert len(rows) == 2 and rows[-1]['body']['action'] == 'update'
    e.log.close()


def test_receipt_children_follow_parent_flow_with_depth(tmp_path):
    e = build(tmp_path, config())
    e.ingest(sample())
    e.tick()
    token = next(r['token'] for r in e.active() if r['item'] == 'alpha:other:one')
    e.submit(token, {'outcome': 'done', 'summary': 'child', 'advance': '说清楚', 'events': [
        {'source': 'requested', 'ext_id': 'anything', 'target': 'alpha', 'object_key': 'child',
         'title': 'child', 'body': 'follow-up'}]})
    e.tick()
    assert e.log.get('items', 'alpha:other:child')['depth'] == 1
    assert e.log.get('items', 'alpha:child') is None
    e.log.close()


def test_session_release_recovers_only_selected_action_blocked_flow(tmp_path):
    from workshop.core.ports import TargetBlocked
    e = build(tmp_path, config())
    e.ingest(sample())
    e.broken('alpha', 'flow', 'flow', RuntimeError('unrelated bug'))
    e.broken('alpha', 'flow', 'other', TargetBlocked('credential unavailable'))
    e.release_here('alpha:other:one', '放', 'session')
    assert e.flow_broken('alpha', 'flow')
    assert not e.flow_broken('alpha', 'other')
    assert not e.log.get('items', 'alpha:other:one')['plugin_blocked']
    e.log.close()


def test_same_source_id_in_different_targets_does_not_collide(tmp_path):
    data = config()
    data['targets']['beta'] = deepcopy(data['targets']['alpha'])
    e = build(tmp_path, data)
    e.ingest(sample())
    e.ingest(replace(sample(), target='beta'))
    assert {i['id'] for i in e.log.all('items')} == {'alpha:one', 'alpha:other:one', 'beta:one', 'beta:other:one'}
    for target in ('alpha', 'beta'):
        e.ingest(replace(sample('zero'), target=target, body={}))
    unmatched = [f for f in e.log.facts() if f['body'].get('unmatched')]
    assert len(unmatched) == 2
    e.log.close()


def test_old_classification_job_keeps_persisted_identity(tmp_path):
    from workshop.core.models import Classification
    e = build(tmp_path)
    old = asdict(event(ext_id='external'))
    old['source'] = 'cli:input'
    old['action'] = 'update'
    for field in ('flow', 'flow_default', 'action_id', 'previous_ids'):
        old.pop(field)
    e.fact('event', {**old, 'ingress': 'external', 'pending_classification': True},
           'alpha:one', source=old['source'], ext_id=old['ext_id'])
    seq = e.log.facts()[-1]['seq']
    key = f'classify:{seq}'
    e.fact('effect', {'id': key, 'kind': 'classify', 'done': False, 'event': old,
                      'item': 'alpha:one', 'event_seq': seq, 'received_seq': seq, 'defer_creation': True}, 'alpha:one')
    class SavedJob:
        def call(self, port, name, method, args, **kwargs):
            assert kwargs['identity'] == [key, old]
            return Classification('ready')
    e.jobs = SavedJob()
    e.classify_events()
    assert e.log.get('items', 'alpha:one')
    assert e.log.get('effects', key)['done']
    e.log.close()
