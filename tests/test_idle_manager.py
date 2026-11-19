"""空闲的三种情况：没活 → 要新需求；全在明确等 → 静默；其余起会话（介入点 R11、R12）。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from workshop.core.log import Log
from workshop.core.managers import (Coordinator, Store, classify_idle, idle_conclusion,
                                    validate_conclusion)
from workshop.core.models import Started
from workshop.plugins.intervention.manager import help_card, queue
from workshop.plugins.intervention.service import Service

ROOT = Path(__file__).resolve().parents[1]
NOW = '2026-11-19T08:06:00+00:00'
IDLE = '2026-11-19T08:00:00+00:00'


class Executor:
    def __init__(self):
        self.calls = []

    def available(self, config):
        self.calls.append(('available', config))
        return True

    def launch(self, token, brief, item, config, target):
        self.calls.append(('launch', item['id']))
        return Started('session', {'kind': 'test'})

    def resume(self, token, session, brief, item, config, target):
        self.calls.append(('resume', item['id']))
        return Started(session, {'kind': 'test'})

    def stop(self, token, started):
        self.calls.append(('stop', token))

    def poll(self, token, started):
        return 'running'

    def inform(self, *args):
        return True


def rules_config():
    return {'managers': {'candidates': ['one'], 'target': 'test', 'service': 'intervention',
                         'manual': str(ROOT / 'docs/项目经理.md')},
            'executor_profiles': {'one': {'billing': 'subscription'}},
            'wait_services': {'intervention': {}}}


@pytest.fixture
def coordinator(tmp_path):
    store = Store(tmp_path)
    executor = Executor()
    published = []

    def call(port, name, method, args, **kwargs):
        if port == 'wait':
            published.append((method, args))
            return {'ref': 'saved'}
        return getattr(executor, method)(*args)

    rules = SimpleNamespace(data=rules_config(), target=lambda name: {}, path=str(ROOT / 'rules/workshop.yaml'))
    c = Coordinator(store, rules, SimpleNamespace(get=lambda port, name: executor),
                    SimpleNamespace(call=call), ['manager'], NOW)
    yield c, executor, published
    store.close()


def idle_fact(since=IDLE, situation='none'):
    fact = {'idle_since': since, 'reason': '业务连续五分钟为零'}
    if situation is not None:
        fact['situation'] = situation
    return fact


def test_no_open_items_gets_blocked_conclusion_without_session(coordinator):
    c, executor, _ = coordinator
    row = c.store.observe('idle', idle_fact(situation='none'), 'watch', NOW)
    c.drive(row)
    row = c.store.all()[0]
    assert executor.calls == []
    assert row['active'] and row['state'] == 'blocked' and not row.get('token')
    assert row['conclusion']['action'] == '车间空了，要新需求'
    assert row['conclusion']['status_location'] == '处理不了'
    assert IDLE in row['conclusion']['evidence']
    validate_conclusion(row['conclusion'])
    card = help_card(row['conclusion'], 'delivery:test')
    assert card['header']['title']['content'] == '车间空了，要新需求'


def test_all_items_waiting_gets_resolved_without_session_or_card(coordinator):
    c, executor, published = coordinator
    row = c.store.observe('idle', idle_fact(situation='waiting'), 'watch', NOW)
    c.run({'total': 0, 'idle_since': IDLE})
    assert executor.calls == []
    row = c.store.all()[0]
    assert row['active'] and row['state'] == 'resolved'
    assert row['conclusion'] == {'schema': 'manager/v1', 'result': 'resolved'}
    records = [args[1]['record'] for method, args in published
               if method == 'invoke' and args[0] == 'manager_update']
    assert records[0]['state'] == 'resolved'


@pytest.mark.parametrize('situation', ['other', None])
def test_idle_that_needs_judgment_still_starts_a_manager_session(coordinator, situation):
    c, executor, _ = coordinator
    row = c.store.observe('idle', idle_fact(situation=situation), 'watch', NOW)
    c.drive(row)
    assert [call[0] for call in executor.calls] == ['available', 'launch']
    assert c.store.all()[0]['state'] == 'running'


@pytest.mark.parametrize('situation', ['none', 'waiting'])
def test_running_idle_session_is_stopped_when_idle_needs_no_judgment(coordinator, situation):
    c, executor, _ = coordinator
    c.store.observe('idle', idle_fact(situation='other'), 'watch', NOW)
    c.drive(c.store.all()[0])
    token = c.store.all()[0]['token']
    assert token and c.store.all()[0]['state'] == 'running'
    c.store.observe('idle', idle_fact(situation=situation), 'watch', NOW)
    c.drive(c.store.all()[0])
    row = c.store.all()[0]
    assert ('stop', token) in executor.calls
    assert [call[0] for call in executor.calls] == ['available', 'launch', 'stop']
    assert row['active'] and not row.get('token')
    if situation == 'waiting':
        assert row['state'] == 'resolved'
        assert row['conclusion'] == {'schema': 'manager/v1', 'result': 'resolved'}
    else:
        assert row['state'] == 'blocked'
        assert row['conclusion']['action'] == '车间空了，要新需求'


@pytest.mark.parametrize(('situation', 'session', 'state', 'action'), [
    ('none', {'schema': 'manager/v1', 'result': 'resolved'}, 'blocked', '车间空了，要新需求'),
    ('none', {'schema': 'manager/v1', 'result': 'blocked', 'action': '回复检查结果', 'reason': '卡在验证',
              'evidence': '看过', 'status_location': '等你'}, 'blocked', '车间空了，要新需求'),
    ('waiting', {'schema': 'manager/v1', 'result': 'blocked', 'action': '回复检查结果', 'reason': '卡在验证',
                 'evidence': '看过', 'status_location': '等你'}, 'resolved', None),
])
def test_session_conclusion_is_rewritten_from_the_last_fact(coordinator, situation, session, state, action):
    c, executor, _ = coordinator
    c.store.observe('idle', idle_fact(situation='other'), 'watch', NOW)
    c.drive(c.store.all()[0])
    token = c.store.all()[0]['token']
    c.store.observe('idle', idle_fact(situation=situation), 'watch', NOW)
    row = c.store.all()[0]
    row['delivered'] = len(row['facts'])  # 会话已经看过最后一条事实
    c.store.save(row, 'informed')
    c.store.db.execute('INSERT INTO receipts VALUES (?,?)', (token, json.dumps(session)))
    c.drive(c.store.all()[0])
    row = c.store.all()[0]
    assert ('stop', token) in executor.calls
    assert row['active'] and not row.get('token')
    assert row['state'] == state
    if action:
        assert row['conclusion']['action'] == action
    else:
        assert row['conclusion'] == {'schema': 'manager/v1', 'result': 'resolved'}
    c.drive(c.store.all()[0])
    assert c.store.all()[0]['state'] == state
    assert [call[0] for call in executor.calls] == ['available', 'launch', 'stop']


def test_new_idle_segment_is_classified_again(coordinator):
    c, executor, _ = coordinator
    c.store.observe('idle', idle_fact(situation='waiting'), 'watch', NOW)
    c.drive(c.store.all()[0])
    episode = c.store.all()[0]['episode']
    c.store.resolve(c.store.all()[0], NOW)
    later = '2026-11-19T09:00:00+00:00'
    second = c.store.observe('idle', idle_fact(later, situation='none'), 'watch', later)
    assert second['episode'] != episode and second['state'] == 'queued'
    c.drive(second)
    row = c.store.all()[0]
    assert row['active'] and row['state'] == 'blocked'
    assert row['conclusion']['action'] == '车间空了，要新需求'
    assert later in row['conclusion']['evidence']
    assert executor.calls == []


def test_unavailable_idle_gets_the_program_conclusion_when_it_needs_none(coordinator):
    c, executor, _ = coordinator
    row = c.store.observe('idle', idle_fact(situation='waiting'), 'watch', NOW)
    row.update(state='unavailable', conclusion={'schema': 'manager/v1', 'result': 'blocked',
        'action': '恢复项目经理可用的订阅', 'reason': '现有订阅候选及替代尝试都失败，无法继续处理。',
        'evidence': '订阅不可用', 'status_location': '处理不了', 'unavailable': True})
    c.store.save(row, 'exhausted')
    c.drive(c.store.all()[0])
    row = c.store.all()[0]
    assert row['state'] == 'resolved' and row['conclusion']['result'] == 'resolved'
    assert executor.calls == []


def test_other_concerns_still_start_a_manager_session(coordinator):
    c, executor, _ = coordinator
    row = c.store.observe('item:a', {'title': '交付检查', 'reason': '卡住'}, 'core', NOW)
    c.drive(row)
    assert [call[0] for call in executor.calls] == ['available', 'launch']
    assert c.store.all()[0]['state'] == 'running'


def write_items(path, items, controls=()):
    log = Log(path)
    try:
        for item in items:
            log.db.execute('INSERT INTO items(id,body) VALUES (?,?)', (item['id'], json.dumps(item, ensure_ascii=False)))
        for control in controls:
            log.db.execute('INSERT INTO controls(id,body) VALUES (?,?)', (control['id'], json.dumps(control, ensure_ascii=False)))
    finally:
        log.close()


def test_classify_idle_covers_waiting_and_needs_judgment(tmp_path):
    empty = Log(tmp_path / 'empty.db')
    assert classify_idle(empty) == 'none'
    empty.close()

    waiting = [{'id': f'item:{state}', 'state': state, 'target': 't'} for state in
               ('等你', '定时', '等前置', '等子单', '等资料')]
    write_items(tmp_path / 'waiting.db', waiting)
    log = Log.readonly(tmp_path / 'waiting.db')
    assert classify_idle(log) == 'waiting'
    log.close()

    for item in ({'id': 'a', 'state': '卡住', 'target': 't'}, {'id': 'b', 'state': '排队', 'target': 't'},
                 {'id': 'c', 'state': '空闲', 'target': 't'},
                 {'id': 'd', 'state': '等你', 'target': 't', 'plugin_blocked': 'p'},
                 {'id': 'e', 'state': '定时', 'target': 'paused'}):
        name = item['id']
        write_items(tmp_path / f'{name}.db', [item],
                    [{'id': 'paused', 'paused': True}] if item['id'] == 'e' else ())
        log = Log.readonly(tmp_path / f'{name}.db')
        assert classify_idle(log) == 'other'
        log.close()


def run_tick(tmp_path, monkeypatch, capsys, business):
    from workshop import manager_cli
    rules = tmp_path / 'rules.yaml'
    config = yaml.safe_load((ROOT / 'rules/workshop.yaml').read_text())
    config['managers']['manual'] = str(ROOT / 'docs/项目经理.md')
    rules.write_text(yaml.safe_dump(config, allow_unicode=True))
    executor, calls = Executor(), []

    class FakeJobs:
        def __init__(self, directory, command):
            self.launcher = SimpleNamespace(shutdown=lambda wait=True: None)

        def call(self, port, name, method, args, **kwargs):
            calls.append((port, method))
            return getattr(executor, method)(*args) if port == 'executor' else {'ref': 'saved'}

    class FakeRegistry:
        def configure(self, value):
            return value

    monkeypatch.setattr(manager_cli, 'Jobs', FakeJobs)
    monkeypatch.setattr(manager_cli, 'Registry', FakeRegistry)
    state = tmp_path / 'state'
    state.mkdir(exist_ok=True)
    message = tmp_path / 'input.json'
    message.write_text(json.dumps({'at': NOW, 'faults': {}, 'business': business}))
    assert manager_cli.main(['--state-dir', str(state), '--rules', str(rules), 'tick', '--file', str(message)]) == 0
    managers = json.loads(capsys.readouterr().out)['managers']
    return next(row for row in managers if row['id'] == 'idle'), executor, calls, state


def test_manager_cli_empty_core_concludes_new_work(tmp_path, monkeypatch, capsys):
    state = tmp_path / 'state'
    state.mkdir()
    Log(state / 'workshop.db').close()
    row, executor, calls, _ = run_tick(tmp_path, monkeypatch, capsys,
                                       {'total': 0, 'idle_since': IDLE})
    assert row['state'] == 'blocked' and row['conclusion']['action'] == '车间空了，要新需求'
    assert executor.calls == [] and not [call for call in calls if call[0] == 'executor']


def test_manager_cli_all_waiting_is_silent(tmp_path, monkeypatch, capsys):
    state = tmp_path / 'state'
    state.mkdir()
    write_items(state / 'workshop.db', [{'id': 'workshop:check', 'state': '定时', 'target': 'workshop'}])
    row, executor, calls, _ = run_tick(tmp_path, monkeypatch, capsys,
                                       {'total': 0, 'idle_since': IDLE})
    assert row['state'] == 'resolved' and row['conclusion']['result'] == 'resolved'
    assert executor.calls == [] and not [call for call in calls if call[0] == 'executor']


def test_manager_cli_stuck_item_starts_a_session(tmp_path, monkeypatch, capsys):
    state = tmp_path / 'state'
    state.mkdir()
    write_items(state / 'workshop.db', [{'id': 'workshop:check', 'state': '卡住', 'target': 'workshop'}])
    row, executor, calls, _ = run_tick(tmp_path, monkeypatch, capsys,
                                       {'total': 0, 'idle_since': IDLE})
    assert row['state'] == 'running', row.get('error')
    assert ('executor', 'launch') in calls


def test_manager_cli_without_core_state_starts_a_session(tmp_path, monkeypatch, capsys):
    row, executor, calls, _ = run_tick(tmp_path, monkeypatch, capsys,
                                       {'total': 0, 'idle_since': IDLE})
    assert row['state'] == 'running', row.get('error')
    assert ('executor', 'launch') in calls


def active_idle_from_previous_segment(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    Log(state / 'workshop.db').close()
    store = Store(state)
    row = store.observe('idle', idle_fact('2026-11-19T07:00:00+00:00', situation='waiting'),
                        'watch', '2026-11-19T07:05:00+00:00')
    row.update(state='resolved', conclusion={'schema': 'manager/v1', 'result': 'resolved'})
    store.save(row, 'concluded')
    store.close()


def test_manager_cli_new_idle_segment_is_not_observed_before_five_minutes(tmp_path, monkeypatch, capsys):
    # 旧空闲段还开着，新段才过 60 秒：不能拿当前核心状态给新段提前出结论。
    active_idle_from_previous_segment(tmp_path)
    row, executor, calls, _ = run_tick(tmp_path, monkeypatch, capsys,
                                       {'total': 0, 'idle_since': '2026-11-19T08:05:00+00:00'})
    assert row['state'] == 'resolved'
    assert row['conclusion'] == {'schema': 'manager/v1', 'result': 'resolved'}
    assert [fact['value']['idle_since'] for fact in row['facts']] == ['2026-11-19T07:00:00+00:00']
    assert executor.calls == [] and not [call for call in calls if call[0] == 'executor']


def test_manager_cli_reclassifies_the_new_segment_once_it_is_due(tmp_path, monkeypatch, capsys):
    active_idle_from_previous_segment(tmp_path)
    row, executor, calls, _ = run_tick(tmp_path, monkeypatch, capsys,
                                       {'total': 0, 'idle_since': IDLE})
    assert row['state'] == 'blocked'
    assert row['conclusion']['action'] == '车间空了，要新需求'
    assert IDLE in row['conclusion']['evidence']
    assert executor.calls == [] and not [call for call in calls if call[0] == 'executor']


@pytest.fixture
def service(tmp_path, monkeypatch):
    path = tmp_path / 'rules.yaml'
    path.write_text(yaml.safe_dump(yaml.safe_load((ROOT / 'rules/workshop.yaml').read_text()), allow_unicode=True))
    s = Service(tmp_path / 'service', path)
    sent = []
    monkeypatch.setattr(s, 'transport', lambda config, action, record:
                        sent.append((action, record)) or {'state': 'sent', 'external': 'message:' + record['id']})
    yield s, sent
    s.store.close()


def idle_record(episode='idle-episode', revision=1, active=True, situation='none'):
    row = {'id': 'idle', 'episode': episode, 'revision': revision, 'active': active,
           'facts': [{'value': idle_fact(situation=situation)}], 'attempts': [],
           'business': {'total': 0, 'idle_since': IDLE}}
    row['state'] = 'resolved' if situation == 'waiting' else 'blocked'
    row['conclusion'] = idle_conclusion(row)
    return row


def test_empty_idle_help_is_sent_once_per_idle_segment(service):
    s, sent = service
    record = idle_record()
    s.call('manager_update', {'operation': 'idle:1', 'record': record})
    s.call('manager_flush', {})
    assert len(sent) == 1 and sent[0][1]['payload']['kind'] == 'manager_help'
    card = sent[0][1]['payload']['card']
    assert card['header']['title']['content'] == '车间空了，要新需求'
    assert card['body']['elements'][-1]['text']['content'] == '看处理不了栏'
    s.call('manager_update', {'operation': 'idle:2', 'record': {**record, 'revision': 2}})
    s.call('manager_flush', {})
    assert len(sent) == 1


def test_waiting_idle_sends_no_card_and_cancels_a_pending_one(service):
    s, sent = service
    record = idle_record('idle-waiting', situation='waiting')
    s.call('manager_update', {'operation': 'idle:w1', 'record': record})
    s.call('manager_flush', {})
    assert sent == [] and s.store.all('delivery') == []

    blocked = idle_record('idle-pending', revision=2, situation='none')
    s.call('manager_update', {'operation': 'idle:p1', 'record': blocked})
    with s.store.transaction():
        queue(s, blocked)
    assert s.store.all('delivery')[0]['state'] == 'queued'
    resolved = {**blocked, 'revision': 3, 'state': 'resolved',
                'conclusion': {'schema': 'manager/v1', 'result': 'resolved'}}
    s.call('manager_update', {'operation': 'idle:p2', 'record': resolved})
    s.call('manager_flush', {})
    assert sent == [] and s.store.all('delivery')[0]['state'] == 'cancelled'
