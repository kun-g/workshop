"""同一事实串逐条对账核心投影，防止终态一致掩盖中途时长错误。"""
from dataclasses import asdict
from datetime import timedelta

import pytest

from conftest import event
from workshop.reports.metrics import calculate, histories, read_snapshot


class Audit:
    def __init__(self, engine):
        self.engine = engine
        self.start = engine.clock()
        self.at = self.start
        self.key = 'alpha:one'

    def append(self, kind, body, *, key=None):
        self.at += timedelta(seconds=10)
        key = key or self.key
        self.engine.log.append(kind, body, key, at=self.at.isoformat())
        facts, metadata, rules = read_snapshot(self.engine.log.path)
        replayed, rounds = histories(facts, metadata, rules, self.at + timedelta(microseconds=1))
        projected = {row['id']: row for row in self.engine.log.all('items')}
        for item_id, row in projected.items():
            if row['state'] == '跑着':
                # 核心到结算才离开跑着；指标以轮次端点计在跑，结束后回原状态。
                assert any(r['item'] == item_id for r in rounds)
            else:
                assert replayed[item_id]['state'] == row['state'], (kind, body, item_id)
        for item_id, row in replayed.items():
            # 未接单只记事实、不建核心活；移除也不保留核心活。
            if item_id not in projected:
                assert row['state'] == '移除' or row['waiting'], (kind, body, item_id)
        return replayed

    def event(self, **extra):
        return self.append('event', {**asdict(event()), **extra})

    def result(self):
        return calculate(*read_snapshot(self.engine.log.path), self.start,
                         self.at + timedelta(seconds=10))['summary']


def test_every_state_transition_matches_projection_after_each_fact(engine):
    a = Audit(engine)
    a.event(release_gate=True)
    a.event(action='ready')
    a.append('item_changed', {'state': '等前置'})
    a.append('gate_passed', {'state': '空闲'})
    a.append('round_reserved', {'token': 'round', 'executor': 'worker', 'at': a.at.isoformat()})
    a.append('round_changed', {'token': 'round', 'stopped': True})
    a.append('round_changed', {'token': 'round', 'status': 'ended', 'ended': a.at.isoformat()})
    # 在跑时长按轮次区间算；核心的「跑着」投影与指标轮次区间是显式口径例外。
    a.append('round_finished', {'state': '定时'})
    a.append('gate_blocked', {'state': '卡住'})
    a.event(action='ready')
    a.append('item_changed', {'state': '等子单'})
    a.append('gate_passed', {'state': '空闲'})
    a.append('work_blocked', {'state': '卡住'})
    a.append('event_classified', {'id': 'classification', 'action': 'ready'})
    a.append('wait_bound', {'id': 'waiting', 'state': 'waiting'})
    a.append('item_changed', {'state': '定时'})
    a.append('wait_changed', {'id': 'waiting', 'state': 'ready'})
    a.append('wait_resumed', {'id': 'waiting'})
    a.append('round_finished', {'state': '完成'})
    a.append('item_dropped', {})
    a.event()
    a.append('reset', {})
    a.event()


@pytest.mark.parametrize('guard', ['wait', 'plugin'])
def test_ready_respects_waits_and_plugin_failures(engine, guard):
    # 问卡已退役（#176）：「等你」只看等待引用，卡片不再挡放行。
    a = Audit(engine)
    a.event()
    a.append('item_changed', {'state': '等你'})
    if guard == 'wait':
        a.append('wait_bound', {'id': 'wait', 'state': 'waiting'})
    else:
        a.append('work_blocked', {'state': '卡住', 'plugin_blocked': True})
    a.event(action='ready')
    if guard == 'wait':
        a.append('wait_resumed', {'id': 'wait'})
    else:
        a.append('item_changed', {'plugin_blocked': False})
        a.event(action='ready')


def test_classified_ready_and_wait_recovery_have_correct_intermediate_durations(engine):
    a = Audit(engine)
    a.event()
    a.append('work_blocked', {'state': '卡住'})
    a.append('event_classified', {'id': 'classified', 'action': 'ready',
                                  'event': {**asdict(event()), 'action': 'update'}})
    a.append('wait_bound', {'id': 'wait', 'state': 'waiting'})
    a.append('item_changed', {'state': '定时'})
    a.append('wait_resumed', {'id': 'wait'})
    a.append('round_finished', {'state': '完成'})
    seconds = a.result()['seconds']
    assert seconds['卡住'] == 10
    assert seconds['定时'] == 10
    assert seconds['排队'] == 40


