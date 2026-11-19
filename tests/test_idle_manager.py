"""空闲由程序出结论、不起会话；其他事项照旧起会话（介入点 R11、R12）。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from workshop.core.managers import Coordinator, Store, idle_conclusion, validate_conclusion
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


def idle_fact(since=IDLE):
    return {'idle_since': since, 'reason': '业务连续五分钟为零'}


def test_idle_gets_program_conclusion_without_starting_a_session(coordinator):
    c, executor, _ = coordinator
    row = c.store.observe('idle', idle_fact(), 'watch', NOW)
    c.drive(row)
    row = c.store.all()[0]
    assert executor.calls == []
    assert row['active'] and row['state'] == 'blocked' and not row.get('token')
    assert row['conclusion']['action'] == '车间空了，要新需求'
    assert row['conclusion']['status_location'] == '处理不了'
    assert IDLE in row['conclusion']['evidence']
    # 和 Store.receipt 同一套字段约束；介入点也能据它生成卡片。
    validate_conclusion(row['conclusion'])
    card = help_card(row['conclusion'], 'delivery:test')
    assert card['header']['title']['content'] == '车间空了，要新需求'


def test_idle_conclusion_is_published_with_idle_scope(coordinator):
    c, executor, published = coordinator
    c.store.observe('idle', idle_fact(), 'watch', NOW)
    c.run({'total': 0, 'idle_since': IDLE})
    assert executor.calls == []
    calls = [args for method, args in published if method == 'invoke']
    records = [args[1]['record'] for args in calls if args[0] == 'manager_update']
    assert records[0]['state'] == 'blocked'
    assert records[0]['conclusion']['action'] == '车间空了，要新需求'
    # 业务状态可能随事项记录发，也可能随全局 flush 发（两种格式都要能表达空闲段）。
    business = records[0].get('business') or next(args[1]['business'] for args in calls
                                                  if args[0] == 'manager_flush' and 'business' in args[1])
    assert business == {'total': 0, 'idle_since': IDLE}


def test_idle_resolves_and_a_new_idle_segment_concludes_again(coordinator):
    c, executor, _ = coordinator
    c.store.observe('idle', idle_fact(), 'watch', NOW)
    c.drive(c.store.all()[0])
    episode = c.store.all()[0]['episode']
    c.store.resolve(c.store.all()[0], NOW)
    row = c.store.all()[0]
    assert not row['active'] and row['state'] == 'resolved'
    later = '2026-11-19T09:00:00+00:00'
    second = c.store.observe('idle', idle_fact(later), 'watch', later)
    assert second['episode'] != episode and second['state'] == 'queued'
    c.drive(second)
    row = c.store.all()[0]
    assert row['active'] and row['state'] == 'blocked'
    assert row['conclusion']['action'] == '车间空了，要新需求'
    assert later in row['conclusion']['evidence']
    assert executor.calls == []


def test_idle_without_a_usable_subscription_still_concludes(coordinator):
    c, executor, _ = coordinator
    row = c.store.observe('idle', idle_fact(), 'watch', NOW)
    row.update(state='unavailable', conclusion={'schema': 'manager/v1', 'result': 'blocked',
        'action': '恢复项目经理可用的订阅', 'reason': '现有订阅候选及替代尝试都失败，无法继续处理。',
        'evidence': '订阅不可用', 'status_location': '处理不了', 'unavailable': True})
    c.store.save(row, 'exhausted')
    c.drive(c.store.all()[0])
    row = c.store.all()[0]
    assert row['state'] == 'blocked' and row['conclusion']['action'] == '车间空了，要新需求'
    assert executor.calls == []


def test_other_concerns_still_start_a_manager_session(coordinator):
    c, executor, _ = coordinator
    row = c.store.observe('item:a', {'title': '交付检查', 'reason': '卡住'}, 'core', NOW)
    c.drive(row)
    assert [call[0] for call in executor.calls] == ['available', 'launch']
    assert c.store.all()[0]['state'] == 'running'


def test_manager_cli_turns_five_minutes_idle_into_a_conclusion(tmp_path, monkeypatch, capsys):
    from workshop import manager_cli
    rules = tmp_path / 'rules.yaml'
    rules.write_text(yaml.safe_dump(yaml.safe_load((ROOT / 'rules/workshop.yaml').read_text()), allow_unicode=True))
    calls = []

    class FakeJobs:
        def __init__(self, directory, command):
            self.launcher = SimpleNamespace(shutdown=lambda wait=True: None)

        def call(self, port, name, method, args, **kwargs):
            calls.append((port, method))
            return {'ref': 'saved'}

    class FakeRegistry:
        def configure(self, value):
            return value

    monkeypatch.setattr(manager_cli, 'Jobs', FakeJobs)
    monkeypatch.setattr(manager_cli, 'Registry', FakeRegistry)
    state = tmp_path / 'state'
    message = tmp_path / 'input.json'
    message.write_text(json.dumps({'at': NOW, 'faults': {}, 'business': {'total': 0, 'idle_since': IDLE}}))
    assert manager_cli.main(['--state-dir', str(state), '--rules', str(rules), 'tick', '--file', str(message)]) == 0
    managers = json.loads(capsys.readouterr().out)['managers']
    idle = next(row for row in managers if row['id'] == 'idle')
    assert idle['state'] == 'blocked' and idle['conclusion']['action'] == '车间空了，要新需求'
    assert not [call for call in calls if call[0] == 'executor']


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


def idle_record(episode='idle-episode', revision=1, active=True, state='blocked'):
    return {'id': 'idle', 'episode': episode, 'revision': revision, 'active': active,
            'state': state, 'facts': [{'value': idle_fact()}], 'attempts': [],
            'business': {'total': 0, 'idle_since': IDLE},
            'conclusion': idle_conclusion({'facts': [{'value': idle_fact()}]})}


def test_idle_help_is_sent_once_per_idle_segment(service):
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


def test_idle_help_is_cancelled_when_business_recovers_before_send(service):
    s, sent = service
    record = idle_record('idle-pending')
    s.call('manager_update', {'operation': 'idle:p1', 'record': record})
    with s.store.transaction():
        queue(s, record)
    assert s.store.all('delivery')[0]['state'] == 'queued'
    s.call('manager_update', {'operation': 'idle:p2',
                              'record': {**record, 'revision': 2, 'active': False, 'state': 'resolved'}})
    s.call('manager_flush', {})
    assert sent == [] and s.store.all('delivery')[0]['state'] == 'cancelled'
