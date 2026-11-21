"""ws#330：issue 的「上线后人验」节——接单认它，上线后经介入点服务登记一个等待，回「过」才关单。

真引擎 + 真插件 + 真介入点服务（独立状态目录、真实规则）；替身命令回放 gh、git。不用 sleep，时间用引擎的假时钟。
"""
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest

from workshop.core.models import Receipt
from workshop.plugins.issue_delivery import dod
from workshop.reports.report import status, why
from conftest import finish, item
from test_issue_delivery import BODY, PR_BODY, world, edit, calls, result, snapshot  # noqa: F401
from test_issue_end_to_end import (POST, commit, engine_for, ensure_round, hand_over, remote, set_body, stages_run,
                                   visual_reply)

HUMAN = '''## 上线后人验
- 下一张标题带【Draft】的 issue 去掉【Draft】后进入接单：status 里它从等资料变成执行。
- 第一封真实来件按规则分拣：why 里看得到分到哪一类。
'''
ITEMS = ['下一张标题带【Draft】的 issue 去掉【Draft】后进入接单：status 里它从等资料变成执行。',
         '第一封真实来件按规则分拣：why 里看得到分到哪一类。']
VISUAL = ('## 要不要亲眼看\n是\n看什么：打开状态页看错误提示\n在哪看：飞书私聊\n怎么回：引用送达回复过或不对\n'
          'Kun 确认：会话 `2ba67f64-4e2b-47b0-9213-00c871c6e28c`\n> 看错误提示\n')


def human_body(extra=''):
    return BODY + HUMAN + extra


# ---- 接单核对和 check-issue ----

def test_registration_accepts_items_without_workshop_checks():
    problems, _ = dod.registration(human_body(), {})
    assert problems == []
    assert dod.human_items(human_body()) == ITEMS


@pytest.mark.parametrize('section', ['## 上线后人验\n', '## 上线后人验\n\n上线后看一眼。\n',
                                     '## 上线后人验\n```text\n- 代码块里的不算\n```\n'])
def test_registration_names_missing_items(section):
    problems, _ = dod.registration(BODY + section, {})
    assert problems == ['「上线后人验」须逐项列出，每项第一行写看什么、怎么判']


def test_without_section_registration_unchanged():
    assert dod.registration(BODY, {})[0] == []
    assert dod.human_items(BODY) == [] and not dod.has_section(BODY, dod.HUMAN)


def test_intake_names_missing_human_items(world):
    edit(world, issue={'number': 7, 'body': BODY + '## 上线后人验\n上线后看。\n'})
    evidence = world['flow'].assess(snapshot(world, '接单'), Receipt('done', '核对'), world['target'])
    assert evidence.awaits_update and '「上线后人验」须逐项列出' in evidence.reason
    edit(world, issue={'number': 7, 'body': human_body()})
    evidence = world['flow'].assess(snapshot(world, '接单'), Receipt('done', '核对'), world['target'])
    assert evidence.passed
    assert world['io'].read(snapshot(world, '接单'), world['target'])['human'] is True


# ---- 收尾登记等待 ----

def human_world(world, name, body=None, **rule):
    from test_intervention_business import Port
    from workshop.plugins.intervention.service import Service
    set_body(world, body or human_body())
    # 默认不写怎么查上线：合并即交付（issue交付 R3）；要等上线的测试另给 deployment。
    engine = engine_for(world, name, **{'deployment': None, **rule})
    # 独立的介入点服务实例，真实规则（rules/workshop.yaml 的 intervention.points）。
    service = Service(world['root'] / (name + 'service'), Path('rules/workshop.yaml'))
    sent = []
    service.transport = lambda *a: sent.append(a) or {'state': 'sent', 'external': 'om_visual'}
    service.poll_reply = lambda wait: None
    engine.plugins.register('wait', 'intervention', Port(service))
    engine.rules.data['wait_services'] = {'intervention': {}}
    from test_issue_end_to_end import start
    start(engine)
    return engine, service, sent


def settle(engine, ticks=4):
    for _ in range(ticks):
        engine.clock.now += timedelta(seconds=60)
        engine.tick()


def human_waits(service):
    return [w for w in service.store.all('wait') if w['point'] == 'human_check']


def answer(service, wait, value, operation='answer'):
    # 和 workshop answer-wait 同一条：会话回答，带原话、会话号和问题版本。
    return service.call('answer', {'operation': operation, 'ref': wait['id'], 'value': value, 'source': {
        'channel': 'session', 'session': '2ba67f64-4e2b-47b0-9213-00c871c6e28c', 'quote': value,
        'version': wait['content']['version']}})


def until_human_wait(world, name, **rule):
    engine, service, sent = human_world(world, name, **rule)
    ensure_round(engine)
    hand_over(engine, world)
    settle(engine, 3)
    return engine, service, sent


def test_registers_wait_after_merge_and_status_shows_waiting(world):
    engine, service, sent = until_human_wait(world, 'human-wait')
    waiting = item(engine, '7')
    assert waiting['stage'] == '收尾' and waiting['state'] == '等你', {k: waiting.get(k) for k in ('stage', 'state', 'next', 'gate_reason', 'blocked_by')}
    assert remote(world)['pr']['merged'] and remote(world)['issue']['state'] == 'open'
    [wait] = human_waits(service)
    merge = world['io'].read(waiting, world['target'])['merge']
    assert wait['owner'] == 'alpha:7' and wait['content']['version'] == merge
    assert wait['content']['items'] == ITEMS
    assert all(text in wait['content']['question'] for text in ITEMS)
    # 只登记等待、不发消息：没有许可、没有送达，送达命令一次没起。
    assert service.store.all('delivery') == [] and service.store.all('permit') == [] and sent == []
    assert not engine.log.all('cards')
    text = status(engine.log)
    assert '等你 1' in text and 'alpha:7' in text
    assert '上线后人验' in why(engine.log, 'alpha:7')
    engine.log.close()


def test_pass_closes_issue(world):
    engine, service, _ = until_human_wait(world, 'human-pass')
    [wait] = human_waits(service)
    answer(service, wait, '过')
    settle(engine)
    assert item(engine, '7')['state'] == '完成' and remote(world)['issue']['state'] == 'closed'
    assert stages_run(engine) == ['执行']
    engine.log.close()


def test_fail_returns_to_execution_with_answer_and_not_counted(world):
    engine, service, _ = until_human_wait(world, 'human-fail')
    [wait] = human_waits(service)
    answer(service, wait, '不对：去掉 Draft 后还停在等资料')
    settle(engine)
    back = item(engine, '7')
    assert back['stage'] == '执行' and back['state'] != '卡住', back
    assert remote(world)['issue']['state'] == 'open'
    state = world['io'].read(back, world['target'])
    assert state.get('returns', 0) == 0
    assert state['last_return']['segment'] == '上线后人验'
    brief = ensure_round(engine)['brief']
    assert '上次退回执行的原因（上线后人验' in brief['instructions']
    assert 'Kun 的回答原文：不对：去掉 Draft 后还停在等资料' in brief['instructions']
    assert '亲眼看' not in brief['instructions'].split('上次退回执行的原因')[1]
    assert '开新的 PR 修复' in brief['instructions']
    assert '不对：去掉 Draft 后还停在等资料' in json.dumps(brief['materials'], ensure_ascii=False)
    engine.log.close()


def test_fail_twice_then_dod_failure_still_returns(world):
    """人验退回不算进两段 DoD 的退回上限：两次不过后，合并前 DoD 没过仍是第一次退回。"""
    engine, service, _ = until_human_wait(world, 'human-count')
    for round_, number in enumerate((12, 13)):
        wait = human_waits(service)[-1]
        answer(service, wait, f'不对：第 {round_ + 1} 次', operation=f'a{round_}')
        settle(engine)
        back = item(engine, '7')
        assert back['stage'] == '执行' and world['io'].read(back, world['target']).get('returns', 0) == 0
        sha = commit(world, {f'fix{number}.txt': 'fix\n'}, f'fix {number}')
        old = remote(world)['pr']
        edit(world, sha=sha, prs=remote(world).get('prs', []) + [old],
             pr={'number': number, 'body': PR_BODY, 'head': {'sha': sha}, 'state': 'open', 'merged': False})
        ensure_round(engine)
        if round_ == 1:
            break
        finish(engine, key='7', evidence=[result(world, head=sha)])
        settle(engine, 3)
        assert item(engine, '7')['state'] == '等你'
        # 新的合并提交另登记一个等待；旧版本的回答不能批准新版本。
        assert len(human_waits(service)) == 2
        assert human_waits(service)[-1]['content']['version'] != wait['content']['version']
    from test_issue_end_to_end import missing_evidence
    missing_evidence(world)
    hand_over(engine, world)
    back = item(engine, '7')
    assert back['stage'] == '执行' and back['state'] != '卡住', back
    assert world['io'].read(back, world['target'])['returns'] == 1
    engine.log.close()


def test_waits_for_running_version_when_rule_says_how(world, monkeypatch):
    """规则写了怎么查上线（deployment）：在跑版本包含合并提交了才登记人验。"""
    running = {'commit': world['sha']}
    monkeypatch.setattr('workshop.running.current', lambda: running['commit'])
    sha = commit(world, {'new.txt': 'new\n'}, 'new')
    edit(world, sha=sha)
    pr = remote(world)['pr']
    pr['head'] = {'sha': sha}
    edit(world, pr=pr)
    engine, service, _ = until_human_wait(world, 'human-deploy', deployment={'kind': 'running'})
    waiting = item(engine, '7')
    assert waiting['stage'] == '交付' and waiting['state'] == '定时', waiting
    assert remote(world)['pr']['merged'] and human_waits(service) == []
    running['commit'] = sha
    engine.clock.now = datetime.fromisoformat(waiting['after_wait']['at'])
    settle(engine, 3)
    assert item(engine, '7')['state'] == '等你' and len(human_waits(service)) == 1
    engine.log.close()


def test_post_release_dod_runs_before_human_check(world, monkeypatch):
    """写了上线后 DoD 的，在它过了以后才登记人验。"""
    running = {'commit': world['sha']}
    monkeypatch.setattr('workshop.running.current', lambda: running['commit'])
    sha = commit(world, {'scripts/post.sh': '#!/bin/sh\necho post-ok\n'}, 'post ok')
    edit(world, sha=sha)
    pr = remote(world)['pr']
    pr['head'] = {'sha': sha}
    edit(world, pr=pr)
    engine, service, _ = human_world(world, 'human-post', body=human_body(POST), deployment={'kind': 'running'})
    ensure_round(engine)
    hand_over(engine, world)
    settle(engine, 3)
    assert item(engine, '7')['stage'] == '交付' and human_waits(service) == []
    running['commit'] = sha
    engine.clock.now = datetime.fromisoformat(item(engine, '7')['after_wait']['at'])
    settle(engine, 3)
    state = world['io'].read(item(engine, '7'), world['target'])
    assert state['post_runs']['runs'][0]['exit_code'] == 0
    assert item(engine, '7')['state'] == '等你' and len(human_waits(service)) == 1
    engine.log.close()


# ---- 和亲眼看同时写 ----

def both_body():
    return human_body().replace('## 要不要亲眼看\n否\n', VISUAL)


def until_visual(world, name):
    engine, service, sent = human_world(world, name, body=both_body())
    ensure_round(engine)
    hand_over(engine, world)
    settle(engine, 3)
    assert item(engine, '7')['state'] == '等你'
    # 先亲眼看：只有亲眼看的等待，人验还没登记。
    [visual] = service.store.all('wait')
    assert visual['point'] == 'visual' and human_waits(service) == []
    return engine, service, visual


def test_visual_first_then_human_both_pass_closes(world):
    engine, service, visual = until_visual(world, 'both-pass')
    visual_reply(service, visual, '过')
    settle(engine)
    assert item(engine, '7')['state'] == '等你' and remote(world)['issue']['state'] == 'open'
    [wait] = human_waits(service)
    answer(service, wait, '过')
    settle(engine)
    assert item(engine, '7')['state'] == '完成' and remote(world)['issue']['state'] == 'closed'
    # 亲眼看照旧送一次；人验不送。
    assert len(service.store.all('delivery')) == 1
    engine.log.close()


def test_visual_pass_human_fail_records_human_segment(world):
    engine, service, visual = until_visual(world, 'both-human-fail')
    visual_reply(service, visual, '过')
    settle(engine)
    [wait] = human_waits(service)
    answer(service, wait, '不对：真实来件没分拣')
    settle(engine)
    back = item(engine, '7')
    assert back['stage'] == '执行'
    state = world['io'].read(back, world['target'])
    assert state['last_return']['segment'] == '上线后人验' and state.get('returns', 0) == 0
    assert '上次退回执行的原因（上线后人验' in ensure_round(engine)['brief']['instructions']
    engine.log.close()


def test_visual_fail_does_not_register_human(world):
    engine, service, visual = until_visual(world, 'both-visual-fail')
    visual_reply(service, visual, '不对：错误提示看不到')
    settle(engine)
    back = item(engine, '7')
    assert back['stage'] == '执行' and human_waits(service) == []
    assert world['io'].read(back, world['target'])['last_return']['segment'] == '亲眼看'
    engine.log.close()


def test_rules_point_only_waits():
    import yaml
    points = yaml.safe_load(Path('rules/workshop.yaml').read_text())['intervention']['points']
    assert points['human_check']['wait'] is True and points['human_check']['send'] is False


def test_question_names_full_version_for_answer_wait(world):
    """问题里写全长合并提交号：answer-wait --version 照抄就对得上。"""
    engine, service, _ = until_human_wait(world, 'human-version')
    [wait] = human_waits(service)
    assert wait['content']['version'] in wait['content']['question']
    engine.log.close()
