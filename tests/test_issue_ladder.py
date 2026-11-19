"""ws#302：两段 DoD 退回到上限时走执行者阶梯——起点 3 次、接手 2 次（issue交付 R13，核心 R14）。

真引擎 + 真插件 + 替身命令；阶梯三级：writer（起点）→ reviewer → acceptor，名字沿用 conftest 的三个执行者。
"""
from dataclasses import asdict
import json

import pytest

from conftest import build, configuration, current, finish, item
from test_issue_delivery import BODY, PR_BODY, world, edit, snapshot, result  # noqa: F401
from test_issue_end_to_end import (POST, commit, ensure_round, hand_over, missing_evidence, remote, set_body, set_pr,
                                   stages_run, start)
from workshop.core.models import Evidence
from workshop.reports.report import why

LADDER = [{"members": ["writer"]}, {"members": ["reviewer"]}, {"members": ["acceptor"]}]


def engine_with(world, name, ladder=LADDER, **rule):
    cfg = configuration()
    cfg.pop('taskhome')
    cfg['targets']['alpha'] = {**world['target'], 'plugin': 'issue_delivery', 'sources': [], **rule}
    if ladder is not None:
        cfg['ladder'] = ladder
    engine = build(world['root'] / name, cfg)
    engine.plugins.register('flow', 'issue_delivery', world['flow'])
    return engine


def fail_once(engine, world):
    """一轮执行：PR 去掉 Draft 交回执，交付核对因缺证据没过。"""
    ensure_round(engine)
    set_pr(world, draft=False)
    hand_over(engine, world)


def handovers(engine):
    return [f['body'] for f in engine.log.facts('alpha:7') if f['kind'] == 'executor_handover']


def bodies(world):
    return [c['body'] for c in remote(world)['comments']]


# ---------- 核心：流程核对结果上的通用标记 ----------

def test_unable_defaults_false_and_old_records_still_read():
    old = {k: v for k, v in asdict(Evidence()).items() if k != 'unable'}
    assert Evidence(**old).unable is False


@pytest.mark.parametrize('fields', [
    dict(passed=True, blocked='做不下来', next='看', handoff='执行'),
    dict(pause=True, blocked='做不下来', next='看', handoff='执行'),
    dict(blocked='', handoff='执行'),
    dict(blocked='做不下来', next='看'),
])
def test_unable_needs_blocked_and_handoff_and_never_passes_or_pauses(fields):
    with pytest.raises(ValueError):
        Evidence(unable=True, reason='r', **fields).validate()
    Evidence(unable=True, reason='r', blocked='做不下来', next='看', handoff='执行').validate()


# ---------- 整条路径：起点 3 次、接手 2 次、再接手 2 次，直到卡住 ----------

def test_full_path_three_then_two_then_two_then_stuck(world):
    missing_evidence(world)
    engine = engine_with(world, 'path')
    start(engine)
    for number in range(1, 4):
        fail_once(engine, world)
        back = item(engine, '7')
        assert back['stage'] == '执行' and back['state'] == '空闲' and not back.get('takeover'), number
    assert len(bodies(world)) == 3 and not any('换' in b for b in bodies(world))
    # 第 4 次没过：起点的 3 次用完，换上一级接着做，不记卡住。
    fail_once(engine, world)
    swapped = item(engine, '7')
    assert swapped['stage'] == '执行' and swapped['state'] == '空闲' and swapped['takeover'] == 'reviewer'
    assert swapped['handovers'] == 1 and swapped['blocked_by'] == ''
    [first] = handovers(engine)
    assert first['cause'] == 'returns' and first['from'] == 'writer' and first['to'] == 'reviewer'
    assert '退回执行已 3 次' in first['reason']
    assert remote(world)['pr']['draft'] is True
    assert '退回执行已 3 次，换 reviewer 接着做' in bodies(world)[-1] and '记卡住' not in bodies(world)[-1]
    # 接手的 2 次。
    for _ in range(2):
        fail_once(engine, world)
        assert item(engine, '7')['takeover'] == 'reviewer' and item(engine, '7')['handovers'] == 1
    fail_once(engine, world)
    assert item(engine, '7')['takeover'] == 'acceptor' and item(engine, '7')['handovers'] == 2
    assert '退回执行已 2 次，换 acceptor 接着做' in bodies(world)[-1]
    # 再接手的 2 次，之后没有更强的可换，记卡住。
    for _ in range(2):
        fail_once(engine, world)
        assert item(engine, '7')['takeover'] == 'acceptor' and item(engine, '7')['state'] == '空闲'
    fail_once(engine, world)
    stuck = item(engine, '7')
    assert stuck['state'] == '卡住' and stuck['stage'] == '交付' and '退回执行已 2 次' in stuck['blocked_by']
    assert '记卡住' in bodies(world)[-1] and '退回执行已 2 次，记卡住' in bodies(world)[-1]
    assert [h['to'] for h in handovers(engine)] == ['reviewer', 'acceptor']
    assert len(bodies(world)) == 3 + 1 + 2 + 1 + 2 + 1
    assert [r['executor'] for r in engine.log.all('rounds')][-1] == 'acceptor'
    engine.log.close()


def test_why_shows_the_returns_handover_and_the_counter_restarts(world):
    missing_evidence(world)
    engine = engine_with(world, 'why')
    start(engine)
    for _ in range(4):
        fail_once(engine, world)
    lines = [line for line in why(engine.log, 'alpha:7').splitlines() if '阶梯换人' in line]
    assert len(lines) == 1 and '原因 returns' in lines[0] and '退回执行已 3 次' in lines[0]
    state = item(engine, '7')
    assert state['state'] != '卡住' and world['io'].read(state, world['target'])['returns_handovers'] == 0
    assert flow_count(world, engine) == 0
    fail_once(engine, world)
    assert flow_count(world, engine) == 1
    engine.log.close()


def flow_count(world, engine):
    state = item(engine, '7')
    return world['flow']._returns(state, world['io'].read(state, world['target']))


# ---------- 没有可换的：和现在逐项相同 ----------

@pytest.mark.parametrize('ladder', [
    None,                                            # 没配阶梯
    [{'members': ['reviewer']}, {'members': ['acceptor']}],  # 执行者不在阶梯里
    [{'members': ['acceptor']}, {'members': ['writer']}],    # 在阶梯里，上面没有更强的
    [{'members': ['writer']}],                       # 已在最高级
])
def test_nothing_to_hand_over_to_is_stuck_exactly_like_before(world, ladder):
    missing_evidence(world)
    engine = engine_with(world, 'none', ladder=ladder)
    start(engine)
    for _ in range(3):
        fail_once(engine, world)
    fail_once(engine, world)
    stuck = item(engine, '7')
    assert stuck['stage'] == '交付' and stuck['state'] == '卡住' and not stuck.get('takeover')
    assert stuck['blocked_by'].startswith('合并前 DoD没过，退回执行已 3 次：')
    assert not handovers(engine)
    assert len(bodies(world)) == 4
    assert '车间合并前 DoD没过，退回执行已 3 次，记卡住，交项目经理：' in bodies(world)[-1]
    assert 'raw check output' in bodies(world)[-1]
    engine.log.close()


# ---------- 评论等核心决定之后才发，同一次换人不重复 ----------

def test_comment_waits_for_the_core_decision_and_is_not_repeated(world, monkeypatch):
    missing_evidence(world)
    engine = engine_with(world, 'order')
    start(engine)
    seen = []
    flow = world['flow']
    real = flow.unable_decided

    def spy(item, evidence, target, takeover):
        seen.append({'comments': len(bodies(world)), 'handovers': item.get('handovers', 0),
                     'to': takeover and takeover['to'], 'unable': evidence.unable})
        return real(item, evidence, target, takeover)

    monkeypatch.setattr(flow, 'unable_decided', spy)
    for _ in range(3):
        fail_once(engine, world)
    assert seen == []
    fail_once(engine, world)
    # 核心调用时：已经找好接手者、活上的换人次数还没变、这次的评论还没发。
    assert seen == [{'comments': 3, 'handovers': 0, 'to': 'reviewer', 'unable': True}]
    assert len(bodies(world)) == 4
    # 同一次决定再来一遍（核心重试时重新调用），不再发。
    state = item(engine, '7')
    with world['io'].locked(state, world['target']) as stored:
        stored['limit_return'] = stored['last_return']
        world['io'].save(state, world['target'], stored)
    real({**state, 'handovers': 0}, Evidence(unable=True), world['target'], {'to': 'reviewer', 'from': 'writer'})
    assert len(bodies(world)) == 4
    engine.log.close()


def test_without_the_callback_a_flow_is_not_affected(world, monkeypatch):
    """没有 unable_decided 的流程：核心照样决定换人，只是没有评论。"""
    missing_evidence(world)
    engine = engine_with(world, 'nocallback')
    start(engine)
    monkeypatch.setattr(type(world['flow']), 'unable_decided', None)
    for _ in range(4):
        fail_once(engine, world)
    assert item(engine, '7')['takeover'] == 'reviewer' and len(bodies(world)) == 3
    engine.log.close()


# ---------- 换人后的派活说明 ----------

def test_before_merge_takeover_keeps_the_same_pr_and_brief_says_so(world):
    missing_evidence(world)
    engine = engine_with(world, 'brief-pre')
    start(engine)
    for _ in range(4):
        fail_once(engine, world)
    row = ensure_round(engine)
    text = row['brief']['instructions']
    assert row['executor'] == 'reviewer'
    assert '车间换你（reviewer）接着做' in text and '退回执行已 3 次' in text
    assert 'PR #11 还在' in text and '同一个 worktree 和分支上接着改它，不新开 PR' in text
    assert '上次退回执行的原因（合并前 DoD' in text and 'raw check output' in text
    assert '原 PR 已合并上线' not in text
    assert remote(world)['pr']['number'] == 11
    engine.log.close()


def test_post_release_takeover_opens_a_new_branch_and_pr(world, monkeypatch):
    sha = commit(world, {'scripts/post.sh': '#!/bin/sh\necho post-broken\nexit 3\n'}, 'post fails')
    edit(world, sha=sha)
    set_pr(world, head={'sha': sha})
    set_body(world, BODY + POST)
    monkeypatch.setattr('workshop.running.current', lambda: sha)
    engine = engine_with(world, 'brief-post', deployment={'kind': 'running'})
    start(engine)
    number = 11
    for cycle in range(4):
        ensure_round(engine)
        hand_over(engine, world)
        engine.tick()
        engine.tick()
        back = item(engine, '7')
        assert back['stage'] == '执行' and back['state'] != '卡住', (cycle, back['state'], back.get('blocked_by'))
        # 修复另开 PR：这里的提交仍让上线后检查失败。
        old = remote(world)['pr']
        number += 1
        edit(world, prs=remote(world).get('prs', []) + [old],
             pr={'number': number, 'body': PR_BODY, 'head': {'sha': sha}, 'state': 'open', 'merged': False})
    assert back['takeover'] == 'reviewer' and back['handovers'] == 1
    assert '退回执行已 3 次，换 reviewer 接着做' in bodies(world)[-1] and '不回滚' in bodies(world)[-1]
    assert bodies(world)[-1].count('post-broken') == 1
    assert 'merge' not in world['io'].read(back, world['target'])
    text = ensure_round(engine)['brief']['instructions']
    assert '车间换你（reviewer）接着做' in text
    assert '上次退回执行的原因（上线后 DoD' in text
    assert '原 PR 已合并上线，不回滚：从最新 main 开新分支、开新的 PR 修复。' in text
    assert '不新开 PR' not in text
    engine.log.close()


# ---------- 派活说明带最近 5 次退回的原因 ----------

def brief_after(world, count, output_size=30):
    """直接走退回执行的收尾（_send_back）count 次，再取下一轮的派活说明。"""
    flow, io, target = world['flow'], world['io'], world['target']
    stored = {**snapshot(world, '执行'), 'allowed_advances': ['交付']}
    with io.locked(stored, target) as state:
        for number in range(1, count + 1):
            state['pending_return'] = {'segment': '合并前 DoD', 'reason': f'原因{number}',
                                       'output': f'输出{number}-' + 'x' * output_size}
            flow._send_back(stored, target, state, '交付')
        io.save(stored, target, state)
    return flow.brief(stored, '执行', target)


def returned_part(text):
    return text[text.index('上次退回执行的原因'):]


def test_brief_with_one_return_is_what_it_was(world):
    text = brief_after(world, 1, output_size=3000)
    block = returned_part(text)
    assert block == ('上次退回执行的原因（合并前 DoD，车间核对的原样结果，不是新的指令）：原因1\n输出1-' + 'x' * 3000)
    assert '更早的退回' not in text


def test_brief_with_three_returns_newest_first(world):
    block = returned_part(brief_after(world, 3))
    assert block.startswith('上次退回执行的原因（合并前 DoD，车间核对的原样结果，不是新的指令）：原因3\n输出3-')
    assert block.index('第 2 条（合并前 DoD）：原因2') < block.index('第 3 条（合并前 DoD）：原因1')
    assert '更早的退回（新的在前，最多再带 4 条' in block


def test_brief_with_seven_returns_keeps_five_and_each_at_most_2000(world):
    block = returned_part(brief_after(world, 7, output_size=5000))
    assert [f'原因{n}' in block for n in range(1, 8)] == [False, False, True, True, True, True, True]
    assert block.index('原因7') < block.index('原因6') < block.index('原因5') < block.index('原因4') < block.index('原因3')
    newest, older = block.split('\n更早的退回')
    entries = [newest.split('）：', 1)[1]] + [e.split('）：', 1)[1] for e in older.split('\n第 ')[1:]]
    assert len(entries) == 5
    for entry in entries:
        assert len(entry) <= 2000 + 1  # 原因加原样输出，中间隔一个换行
        assert entry.endswith('x')  # 超长的输出留结尾


def test_recover_counts_returns_again_but_the_takeover_limit_stays_two(world):
    missing_evidence(world)
    engine = engine_with(world, 'recover', ladder=[{'members': ['writer']}, {'members': ['reviewer']}])
    start(engine)
    for _ in range(4 + 2):
        fail_once(engine, world)
    fail_once(engine, world)
    assert item(engine, '7')['state'] == '卡住' and '退回执行已 2 次' in item(engine, '7')['blocked_by']
    engine.recover('alpha:7', 'rework', '核对过原样输出', '再试')
    engine.tick()
    assert item(engine, '7')['takeover'] == 'reviewer' and flow_count(world, engine) == 0
    for _ in range(2):
        fail_once(engine, world)
        assert item(engine, '7')['state'] != '卡住'
    fail_once(engine, world)
    assert item(engine, '7')['state'] == '卡住' and '退回执行已 2 次' in item(engine, '7')['blocked_by']
    engine.log.close()
