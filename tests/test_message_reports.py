"""介入点 R35a：按真实送达、回答与核心办结事实，从报告入口核对基线。"""
from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest
import yaml

from conftest import Clock, event
from test_intervention_business import completed, spec
from test_managers import record, update
from workshop.authorization import verify
from workshop.cli import main
from workshop.core.log import Log
from workshop.plugins.intervention.service import Service


@pytest.fixture
def ledger(tmp_path):
    clock = Clock()
    rules = yaml.safe_load(Path('rules/workshop.yaml').read_text())
    rules['intervention']['status'] = {'recipient': 'tester', 'chat': 'test-chat', 'command': ['false']}
    rules['intervention']['transport'] = ['false']
    path = tmp_path / 'rules.yaml'
    path.write_text(yaml.safe_dump(rules, allow_unicode=True))
    service = Service(tmp_path / 'service', path, now=clock)
    service.transport = lambda config, action, row: {'state': 'sent', 'external': 'test:' + row['id']}
    log = Log(tmp_path / 'workshop.db')
    try:
        yield service, log, clock
    finally:
        service.store.close()
        log.close()


def report(ledger, capsys):
    service, log, clock = ledger
    assert main(['--state-dir', str(Path(log.path).parent), 'reports', 'messages',
                 '--intervention-db', str(service.state / 'intervention.db'), '--as-of', clock().isoformat()]) == 0
    text = capsys.readouterr().out
    assert '不含授权中心卡片' in text
    lines = [line.split('\t') for line in text.splitlines() if '\t' in line]
    assert lines[0] == '人 通道 类别 发出 被回应 回应率 折叠 未回应 待回应 间接回应 处理不了推送'.split()
    rows = {}
    for person, channel, category, sent, replied, rate, folded, missed, pending, indirect, help_count in lines[1:]:
        key = (person, channel, category)
        assert key not in rows
        assert int(sent) == int(replied) + int(missed) + int(pending)
        rows[key] = (int(sent), int(replied), rate, int(folded), int(missed), int(pending), int(indirect), int(help_count))
    return rows


def visual(service, item, *, dispatch=True):
    permit = service.call('permit', {'operation': item + ':permit', 'point': 'visual', 'owner': item,
        'scope': 'contract:' + item, 'basis': {'contract': '看页面', 'location': '测试目标', 'response': '过或不对'}})['ref']
    delivery = service.call('enqueue', {'operation': item + ':enqueue', 'permit': permit,
        'payload': {'kind': 'visual', 'text': '看页面', 'version': 'v1'}})['ref']
    if dispatch:
        assert service.call('dispatch', {'ref': delivery})['state'] == 'sent'
    return delivery


def respond(service, item, *, delivery=None):
    point = 'visual' if delivery else 'decision'
    content = {'key': point, 'version': 'v1', 'stage': '执行'}
    if delivery:
        content.update(delivery=delivery, response='过或不对')
    else:
        content.update(question='继续吗', reason='需要确认', recommend='继续')
    ref = service.call('create', {'operation': item + ':wait:' + point, 'point': point, 'owner': item, 'content': content})['ref']
    return service.call('answer', {'operation': item + ':answer:' + point, 'ref': ref, 'value': '过',
        'source': {'channel': 'session', 'session': 'test-session', 'quote': '过', 'version': 'v1'}})


def manager_card(service, items):
    for item in items:
        row = record('item:' + item)
        row.update(episode='episode:' + item, facts=[{'digest': 'd', 'at': row['business']['idle_since'],
                                                       'value': {'item': item, 'reason': '等处理'}}])
        update(service, row)
    service.call('manager_flush', {'business': {'total': 0, 'idle_since': '2026-11-13T00:00:00+00:00'}})


def open_item(log, item, at):
    target, key = item.split(':', 1)
    log.append('event', asdict(event(key, target, item)), item, at=at.isoformat())


def complete(log, item, at, *, kind='item_changed'):
    log.append(kind, {'stage': '收尾', 'state': '完成', 'next': '已收尾'}, item, at=at.isoformat())


def seed(service, key, target, channel, category, *, status='delivered', respondable=True, sent=None, reply=None):
    # 仅固定报表样本使用 SQL，送达、回答、摘要状态变化另从服务入口核验。
    service.store.db.execute('''INSERT INTO message_outbox
        (id, point, category, target, channel, sent_at, respondable, status, responded_at, item, items, version)
        VALUES (?, 'visual', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1')''',
        (key, category, target, channel, sent.isoformat() if sent else None, respondable, status,
         reply.isoformat() if reply else None, key, json.dumps([key])))


VISUAL = ('tester', 'feishu', 'visual')
HELP = ('tester', 'feishu', 'manager_help')


def test_category_denominator_and_second_member_response(ledger, capsys):
    service, log, clock = ledger
    start = clock()
    respond(service, 'alpha:M1', delivery=visual(service, 'alpha:M1'))
    visual(service, 'alpha:M2')
    open_item(log, 'alpha:M3', start)
    visual(service, 'alpha:M3')
    seed(service, 'M4', *VISUAL, sent=start, respondable=False)
    for status in ('failed', 'unknown'):
        seed(service, 'M5:' + status, *VISUAL, status=status)

    digest = visual(service, 'alpha:sent-digest', dispatch=False)
    service.outbox.add_member(digest, 'M6', point='visual', category='visual', item='alpha:M6', version='v1')
    service.call('dispatch', {'ref': digest})
    pending = visual(service, 'alpha:pending-digest', dispatch=False)
    service.outbox.add_member(pending, 'M7', point='visual', category='visual', item='alpha:M7', version='v1')

    items = ['alpha:one', 'alpha:two', 'alpha:three']
    manager_card(service, items)
    respond(service, 'alpha:unrelated')
    help_row = 'SELECT items, responded_at FROM message_outbox WHERE point=\'manager_help\''
    [(members, timestamp)] = service.store.db.execute(help_row)
    assert set(json.loads(members)) == set(items) and timestamp is None
    clock.now += timedelta(hours=1)
    respond(service, items[1])
    [(members, timestamp)] = service.store.db.execute(help_row)
    assert datetime.fromisoformat(timestamp) == clock()
    complete(log, 'alpha:M3', start + timedelta(days=3))
    clock.now = start + timedelta(days=7, seconds=1)
    assert report(ledger, capsys) == {VISUAL: (4, 1, '1/4', 1, 3, 0, 1, 0),
                                      HELP: (1, 1, '1/1', 0, 0, 0, 0, 1)}


def test_digest_children_count_only_after_transport_confirms_delivery(ledger, capsys):
    service, _, clock = ledger
    parent = visual(service, 'alpha:digest', dispatch=False)
    service.outbox.add_member(parent, 'child', point='visual', category='visual', item='alpha:folded', version='v1')
    calls = []
    results = iter([{'state': 'absent'}, {'state': 'unknown'}, {'state': 'sent', 'external': 'test:parent'}])

    def transport(config, action, row):
        calls.append((action, row['id']))
        return next(results)

    service.transport = transport
    db = service.store.db
    for expected in ('failed', 'unknown'):
        service.call('dispatch', {'ref': parent})
        assert db.execute('SELECT status, respondable FROM message_outbox WHERE id=?', (parent,)).fetchone() == (expected, 0)
        assert db.execute('SELECT status, digest_id, sent_at FROM message_outbox WHERE id=?', ('child',)).fetchone() == (
            'pending_digest', parent, None)
        assert report(ledger, capsys) == {}
    clock.now += timedelta(hours=2)
    for _ in range(2):
        service.call('dispatch', {'ref': parent})
    assert calls == [('send', parent), ('send', parent), ('reconcile', parent)]
    [(point, category, target, channel, sent_at, respondable, status, responded_at, resolved_after,
      item, items, version, digest_id)] = db.execute('''SELECT point, category, target, channel, sent_at,
        respondable, status, responded_at, resolved_after, item, items, version, digest_id FROM message_outbox WHERE id='child' ''')
    assert (target, channel, category) == VISUAL
    assert (point, respondable, status, responded_at, resolved_after) == ('visual', 1, 'folded', None, None)
    assert (item, json.loads(items), version, digest_id) == ('alpha:folded', ['alpha:folded'], 'v1', parent)
    assert datetime.fromisoformat(sent_at) == clock()
    assert datetime.fromisoformat(sent_at).utcoffset() == timedelta(0)
    assert report(ledger, capsys) == {VISUAL: (1, 0, '0/1', 1, 0, 1, 0, 0)}


def test_each_person_channel_category_has_its_own_counts(ledger, capsys):
    service, _, clock = ledger
    start = clock()
    for index, (person, channel, category, answered) in enumerate([
            ('甲', '私聊', '甲类', True), ('甲', '私聊', '甲类', False), ('乙', '私聊', '甲类', False),
            ('甲', '另一个通道', '甲类', True), ('甲', '私聊', '乙类', False), ('甲', '私聊', '乙类', False)]):
        seed(service, str(index), person, channel, category, sent=start, reply=start if answered else None)
    clock.now += timedelta(days=7, seconds=1)
    assert report(ledger, capsys) == {
        ('甲', '私聊', '甲类'): (2, 1, '1/2', 0, 1, 0, 0, 0),
        ('乙', '私聊', '甲类'): (1, 0, '0/1', 0, 1, 0, 0, 0),
        ('甲', '另一个通道', '甲类'): (1, 1, '1/1', 0, 0, 0, 0, 0),
        ('甲', '私聊', '乙类'): (2, 0, '0/2', 0, 2, 0, 0, 0),
    }


def test_three_states_and_late_reply(ledger, capsys):
    service, log, clock = ledger
    start = clock()
    items = ['alpha:M1', 'alpha:M2', 'alpha:M3']
    deliveries = [visual(service, item) for item in items]
    open_item(log, items[2], start)
    clock.now = start + timedelta(days=6, hours=23)
    assert report(ledger, capsys)[VISUAL] == (3, 0, '0/3', 0, 0, 3, 0, 0)
    for offset in (timedelta(days=7), timedelta(days=7, seconds=1)):
        clock.now = start + offset
        assert report(ledger, capsys)[VISUAL] == (3, 0, '0/3', 0, 3, 0, 0, 0)
    clock.now = start + timedelta(days=7, hours=1)
    respond(service, items[1], delivery=deliveries[1])
    complete(log, items[2], clock())
    assert report(ledger, capsys)[VISUAL] == (3, 1, '1/3', 0, 2, 0, 0, 0)


def test_live_core_completion_of_any_member_counts_once(ledger, capsys):
    service, log, clock = ledger
    start = clock()
    items = ['alpha:Y', 'alpha:two', 'alpha:three']
    for item in items:
        open_item(log, item, start - timedelta(days=2))
    complete(log, items[0], start - timedelta(days=1))
    manager_card(service, items)
    clock.now = start + timedelta(days=1)
    assert report(ledger, capsys) == {HELP: (1, 0, '0/1', 0, 0, 1, 0, 1)}
    clock.now += timedelta(days=1)
    complete(log, items[0], clock(), kind='round_finished')
    assert report(ledger, capsys) == {HELP: (1, 0, '0/1', 0, 0, 1, 1, 1)}
    clock.now += timedelta(days=1)
    complete(log, items[1], clock())
    assert report(ledger, capsys) == {HELP: (1, 0, '0/1', 0, 0, 1, 1, 1)}
    assert service.store.db.execute('SELECT responded_at FROM message_outbox').fetchall() == [(None,)]


def test_authorization_request_and_verified_gate_do_not_add_message(ledger, capsys):
    service, log, clock = ledger
    visual(service, 'alpha:ordinary')
    before = report(ledger, capsys)
    before_ids = service.store.db.execute('SELECT id FROM message_outbox').fetchall()
    assert len(before_ids) == 1
    item = 'alpha:authorization'
    open_item(log, item, clock())
    request = spec()
    # 执行回执携原授权申请，核心在关口放行事实里记核实结果。
    evidence = json.dumps({'schema': 'issue-delivery/v1', 'authorization': request})
    log.append('receipt', {'token': 'round:auth', 'receipt': {'outcome': 'nothing', 'summary': '等待原申请',
               'evidence': [evidence]}}, item, at=clock().isoformat())
    authorized = verify(request, completed())
    assert authorized['applied']
    log.append('gate_passed', {'gate_reason': '原授权生效', 'gate_stage': '交付', 'actions': [request['action']],
               'action_targets': {request['action']: authorized['target']}, 'authorized_actions': [authorized]},
               item, at=clock().isoformat())
    service.call('manager_flush', {})
    assert service.store.db.execute('SELECT id FROM message_outbox').fetchall() == before_ids
    assert report(ledger, capsys) == before
