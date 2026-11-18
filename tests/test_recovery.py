"""ws#81：阻塞不消耗人的判断；恢复追加事实，不跳过关口。"""
from datetime import timedelta
import json
from pathlib import Path

import pytest

from workshop.cli import main, parser
from workshop.core.cards import question
from workshop.core.models import After, Evidence, Receipt
from workshop.reports.report import status, why
from conftest import build, configuration, current, event, finish, item, reach
from test_issue_delivery import world, snapshot, edit, result  # noqa: F401


def test_waiting_requires_live_question_and_hold_does_not_hold_ask(tmp_path):
    cfg = configuration()
    cfg['notices'] = 'hold'
    engine = build(tmp_path, cfg)
    reach(engine, '执行')
    finish(engine, outcome='nothing', notice=[{'key': 'blocked', 'text': '缺工具'}],
           leave={'kind': '等你', 'about': '缺工具'})
    assert item(engine)['state'] == '卡住'
    assert '缺工具' in status(engine.log) and '卡住' in status(engine.log).splitlines()
    assert not engine.log.all('cards')
    engine.recover('alpha:one', 'rework', '工具已装', '继续')
    engine.tick()
    finish(engine, ask=[{'key': 'choice', **question('选择范围')}], leave={'kind': '等你', 'about': '选择范围'})
    assert item(engine)['state'] == '等你'
    cards = engine.log.all('cards')
    assert len(cards) == 1 and cards[0]['mode'] == 'ask'
    assert cards[0]['id'] in status(engine.log) or cards[0]['external'] in status(engine.log)
    engine.answer_here(cards[0]['id'], 'yes', '继续', 'test-session')
    engine.tick()
    assert current(engine)['stage'] == '执行'
    engine.log.close()


@pytest.mark.parametrize('reason', ['缺工具 promtool', '缺 checks 权限', '重试用完', '阶段等待达到上限'])
def test_explicit_operational_block_has_cause_and_action(engine, reason):
    reach(engine, '执行')
    flow = engine.plugins.get('flow', 'flow')
    flow.assess = lambda *a: Evidence(blocked=reason, next='项目经理修复后恢复')
    finish(engine, outcome='nothing')
    row = item(engine)
    assert row['state'] == '卡住' and row['blocked_by'] == reason
    assert row['leave']['kind'] == '工序' and row['leave']['owner'] == '项目经理'
    assert reason in status(engine.log) and '修复后恢复' in status(engine.log)
    assert not engine.active() and not engine.log.all('cards')


def test_plugin_maps_blocked_evidence(world):
    receipt = Receipt('nothing', '缺工具', evidence=[result(world, blocked={'reason': '缺工具', 'next': '安装后恢复'})])
    evidence = world['flow'].assess(snapshot(world, '执行'), receipt, world['target'])
    assert evidence.blocked == '缺工具' and evidence.next == '安装后恢复'
    evidence.validate()
    bad = Receipt('done', '错误', advance='收尾', evidence=receipt.evidence)
    assert not world['flow'].assess(snapshot(world, '执行'), bad, world['target']).passed


@pytest.mark.parametrize('state', ['卡住', '放弃'])
def test_rework_validation_history_and_rebuild(engine, state):
    engine.ingest(event())
    engine.change(item(engine), checks=[['true']], state=state, blocked_by='原故障')
    before = item(engine)
    for reason, quote in [('', '继续'), (' ', '继续'), ('已修复', ''), ('已修复', ' ')]:
        with pytest.raises(ValueError):
            engine.recover('alpha:one', 'rework', reason, quote)
        assert item(engine) == before
    recovery = engine.recover('alpha:one', 'rework', '已修复', '再试一次')
    assert item(engine)['pending_rework']
    with pytest.raises(ValueError, match='已经登记返工'):
        engine.recover('alpha:one', 'rework', '已修复', '再试一次')
    engine.tick()
    assert current(engine)['stage'] == '执行'
    assert '再试一次' in item(engine)['gate_reason']
    before = item(engine)
    with pytest.raises(ValueError, match='还有一轮'):
        engine.recover('alpha:one', 'rework', '重复', '继续')
    assert item(engine) == before
    history = why(engine.log, 'alpha:one')
    assert '已修复' in history and '再试一次' in history and state in history
    engine.log.rebuild()
    assert item(engine) == before and item(engine)['recovery']['id'] == recovery['id']


@pytest.mark.parametrize('legacy', [False, True])
def test_pending_limit_continue_without_reexecution_then_finish(engine, legacy):
    reach(engine, '交付')
    flow = engine.plugins.get('flow', 'flow')
    calls = []
    online = False
    def after(data, stage, key, target):
        calls.append((stage, key))
        if stage == '交付' and not online:
            return After(pending=True, reason='线上尚未完成', next='回查上线', recheck_after='+6m')
        return After()
    flow.after = after
    finish(engine, '验收')
    while item(engine)['state'] == '定时':
        engine.clock.now += timedelta(hours=2)
        engine.tick()
    assert item(engine)['state'] == '卡住' and item(engine)['stage'] == '交付'
    assert item(engine)['after_pending_count'] == 5
    if legacy:
        engine.change(item(engine), after_wait=None)
    before = item(engine)
    with pytest.raises(ValueError):
        engine.recover('alpha:one', 'wait', ' ')
    assert item(engine) == before
    engine.recover('alpha:one', 'wait', '外部部署已恢复')
    assert item(engine)['after_pending_count'] == 0
    assert item(engine)['stage'] == '交付' and item(engine)['state'] == '定时'
    assert not engine.active()
    num_calls = len(calls)
    engine.clock.now += timedelta(minutes=5)
    engine.tick()
    assert len(calls) == num_calls  # 插件建议的六分钟没有被缩短。
    engine.clock.now += timedelta(minutes=1)
    engine.tick()
    assert len(calls) == num_calls + 1 and not engine.active()
    assert item(engine)['after_pending_count'] == 1
    online = True
    engine.clock.now += timedelta(minutes=10)
    engine.tick()
    assert current(engine)['stage'] == '验收'
    finish(engine, '收尾')
    finish(engine, leave={'kind': '送回', 'at': '+7d', 'about': '复看'})
    assert item(engine)['state'] == '完成'
    assert '外部部署已恢复' in why(engine.log, 'alpha:one')
    assert len({key for stage, key in calls if stage == '交付'}) == 1


def test_cli_recover_rework_requires_quote_and_reason(tmp_path, monkeypatch, capsys):
    engine = build(tmp_path)
    engine.ingest(event())
    engine.change(item(engine), state='放弃', checks=[['true']])
    monkeypatch.setenv('WORKSHOP_STATE_DIR', str(tmp_path))
    assert main(['recover', 'alpha:one', '--mode', 'rework', '--reason', '已修复']) == 2
    assert '--quote' in capsys.readouterr().err
    assert main(['recover', 'alpha:one', '--mode', 'rework', '--reason', '已修复', '--quote', '继续']) == 0
    assert item(engine)['pending_rework']
    assert '已修复' in why(engine.log, 'alpha:one')
    with pytest.raises(SystemExit):
        parser().parse_args(['recover', 'alpha:one', '--mode', 'wait'])
    engine.log.close()
