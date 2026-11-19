"""ws#215：issue 交付端到端——接单程序核对登记，执行一轮做到 PR 去掉 Draft，交付由程序查两段 DoD、合并、上线，收尾由程序关 issue。

真引擎 + 真插件 + 替身命令（gh、git、kubectl 回放测试目录里的 JSON）；不用 sleep，时间用引擎的假时钟。
"""
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess

import pytest

from workshop.core.models import Event, Receipt
from workshop.plugins.issue_delivery.dod import CONSTRAINT
from workshop.reports.report import status, why
from conftest import build, configuration, current, finish, item
from test_issue_delivery import BODY, PR_BODY, world, edit, calls, result, snapshot  # noqa: F401


def engine_for(world, name, **rule):
    cfg = configuration()
    cfg.pop('taskhome')
    cfg['targets']['alpha'] = {**world['target'], 'plugin': 'issue_delivery', 'sources': [], **rule}
    engine = build(world['root'] / name, cfg)
    engine.plugins.register('flow', 'issue_delivery', world['flow'])
    return engine


def start(engine):
    engine.ingest(Event('github', 'labeled', 'alpha', '7', 'test', {}, action='ready'))
    engine.tick()


def remote(world):
    return json.loads(world['store'].read_text())


def stages_run(engine):
    """起过模型轮的阶段；程序步不记执行轮。"""
    return [r['stage'] for r in engine.log.all('rounds')]


def commit(world, files, message='change'):
    repo = world['target']['checkout']
    for name, text in files.items():
        Path(repo, name).write_text(text)
        Path(repo, name).chmod(0o755)
    subprocess.run(['/usr/bin/git', '-C', repo, 'add', '.'], check=True)
    subprocess.run(['/usr/bin/git', '-C', repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost',
                    'commit', '-qm', message], check=True)
    return subprocess.check_output(['/usr/bin/git', '-C', repo, 'rev-parse', 'HEAD'], text=True).strip()


def hand_over(engine, world, **extra):
    """执行者交回执；不填 advance，交接看 PR 状态。"""
    return finish(engine, key='7', evidence=[result(world)], **extra)


def test_happy_path_only_execution_runs_a_model_round(world):
    engine = engine_for(world, 'happy')
    start(engine)
    assert item(engine, '7')['stage'] == '执行'
    assert not engine.log.all('rounds')
    engine.tick()
    assert current(engine, '7')['stage'] == '执行'
    hand_over(engine, world)
    engine.tick()
    done = item(engine, '7')
    assert done['state'] == '完成', (done['state'], done['next'], done.get('gate_reason'), done.get('blocked_by'))
    assert stages_run(engine) == ['执行']
    state = remote(world)
    assert state['pr']['merged'] and state['issue']['state'] == 'closed'
    assert engine.log.all('cards') == []
    engine.log.close()



def ensure_round(engine):
    if not engine.active():
        engine.tick()
    return current(engine, '7')


def set_body(world, body):
    issue = remote(world)['issue']
    issue['body'] = body
    edit(world, issue=issue)


def set_pr(world, **changes):
    pr = remote(world)['pr']
    pr.update(changes)
    edit(world, pr=pr)


def test_intake_waits_for_missing_section_without_rounds_or_cards(world):
    """接单核对（核心 R2、R3）：缺一节停在接单「等资料」，不派轮、不发卡；issue 补齐后来一条更新，下一拍接单。"""
    set_body(world, BODY.replace('## 约束\n无\n', ''))
    engine = engine_for(world, 'intake')
    start(engine)
    waiting = item(engine, '7')
    assert waiting['stage'] == '接单' and waiting['state'] == '等资料'
    assert CONSTRAINT in waiting['next']
    for _ in range(3):
        engine.clock.now += timedelta(hours=1)
        engine.tick()
    assert item(engine, '7')['state'] == '等资料'
    assert not engine.log.all('rounds') and not engine.log.all('cards')
    assert '等资料 1' in status(engine.log) and CONSTRAINT in status(engine.log)
    assert CONSTRAINT in why(engine.log, 'alpha:7')
    set_body(world, BODY)
    engine.ingest(Event('github', 'issue-edited', 'alpha', '7', 'test', {'kind': 'issue', 'number': 7}))
    engine.tick()
    assert item(engine, '7')['stage'] == '执行'
    assert item(engine, '7')['checks'] == [['scripts/check.sh']]
    engine.log.close()


@pytest.mark.parametrize('missing,reason', [
    ('## 合并前 DoD\n- 错误输出和非零退出码保留：`scripts/check.sh` 通过。\n', '合并前 DoD'),
    ('## 约束\n无\n', '约束'),
])
def test_intake_names_each_missing_item(world, missing, reason):
    set_body(world, BODY.replace(missing, ''))
    engine = engine_for(world, 'intake-' + reason)
    start(engine)
    assert item(engine, '7')['state'] == '等资料' and reason in item(engine, '7')['next']
    engine.log.close()


def test_draft_pr_is_not_a_handover(world):
    """issue交付 R13：交接以 PR 去掉 Draft 为准；回执自报推进也不算，同阶段重来。"""
    set_pr(world, draft=True)
    engine = engine_for(world, 'draft')
    start(engine)
    for _ in range(2):
        ensure_round(engine)
        hand_over(engine, world, advance='交付')
        retrying = item(engine, '7')
        assert retrying['stage'] == '执行' and 'Draft' in retrying['gate_reason']
    set_pr(world, draft=False)
    row = ensure_round(engine)
    assert 'Draft' in row['brief']['instructions']
    hand_over(engine, world)
    engine.tick()
    assert item(engine, '7')['state'] == '完成'
    assert stages_run(engine) == ['执行'] * 3
    engine.log.close()


def test_draft_three_times_is_stuck(world):
    set_pr(world, draft=True)
    engine = engine_for(world, 'draft-stuck')
    start(engine)
    for _ in range(3):
        ensure_round(engine)
        hand_over(engine, world)
    stuck = item(engine, '7')
    assert stuck['stage'] == '执行' and stuck['state'] == '卡住' and 'Draft' in stuck['blocked_by']
    assert not engine.log.all('cards')
    engine.log.close()


def missing_evidence(world):
    set_pr(world, draft=False, body='<!-- workshop-item: alpha:7 -->\n## 合并前 DoD\n- 错误输出和非零退出码保留：`scripts/check.sh` 通过。\n')


def test_merge_dod_failure_returns_pr_to_draft_with_raw_output(world):
    """进交付查合并前 DoD（核心 R4，issue交付 R6）：少证据就改回 Draft、在 PR 留原样输出、退回执行；补上后合并收尾。"""
    missing_evidence(world)
    engine = engine_for(world, 'dod')
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    back = item(engine, '7')
    assert back['stage'] == '执行'
    state = remote(world)
    assert state['pr']['draft'] is True and not state['pr']['merged']
    [comment] = state['comments']
    assert comment['number'] == 11 and 'raw check output' in comment['body'] and '没附证据' in comment['body']
    assert '<!-- workshop-comment: alpha:7 -->' in comment['body']
    brief = ensure_round(engine)['brief']['instructions']
    assert '合并前 DoD' in brief and 'raw check output' in brief
    set_pr(world, draft=False, body=PR_BODY)
    hand_over(engine, world)
    engine.tick()
    assert item(engine, '7')['state'] == '完成'
    assert stages_run(engine) == ['执行', '执行']
    assert remote(world)['issue']['state'] == 'closed' and remote(world)['pr']['merged']
    assert not engine.log.all('cards')
    engine.log.close()


def test_failed_check_in_clean_copy_returns_to_execution(world):
    sha = commit(world, {'scripts/check.sh': '#!/bin/sh\necho actual-failure >&2\nexit 2\n'}, 'fail')
    set_pr(world, head={'sha': sha})
    engine = engine_for(world, 'check-fails')
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    assert item(engine, '7')['stage'] == '执行'
    [comment] = remote(world)['comments']
    assert 'actual-failure' in comment['body'] and 'exit 2' in comment['body']
    engine.log.close()


def test_fourth_dod_failure_is_stuck(world):
    missing_evidence(world)
    engine = engine_for(world, 'dod-limit')
    start(engine)
    for _ in range(3):
        ensure_round(engine)
        set_pr(world, draft=False)
        hand_over(engine, world)
        assert item(engine, '7')['stage'] == '执行'
    ensure_round(engine)
    set_pr(world, draft=False)
    hand_over(engine, world)
    stuck = item(engine, '7')
    assert stuck['stage'] == '交付' and stuck['state'] == '卡住' and '3 次' in stuck['blocked_by']
    comments = remote(world)['comments']
    assert len(comments) == 4 and '记卡住' in comments[-1]['body'] and 'raw check output' in comments[-1]['body']
    assert remote(world)['pr']['draft'] is True
    assert not engine.log.all('cards')
    engine.log.close()


def test_stuck_at_limit_keeps_raw_output_on_pr_and_in_why(world):
    """ws#254：到上限记卡住时，PR 改回 Draft、评论原样输出；why 的交付记录有失败的检查名、退出码和输出结尾。"""
    sha = commit(world, {'scripts/check.sh': '#!/bin/sh\necho first-line\necho flaky-test-failed >&2\nexit 5\n'}, 'fail')
    set_pr(world, head={'sha': sha})
    engine = engine_for(world, 'stuck-output')
    start(engine)
    for _ in range(3):
        ensure_round(engine)
        set_pr(world, draft=False)
        hand_over(engine, world)
        assert item(engine, '7')['stage'] == '执行'
    ensure_round(engine)
    set_pr(world, draft=False)
    hand_over(engine, world)
    stuck = item(engine, '7')
    assert stuck['state'] == '卡住' and '退回执行已 3 次' in stuck['blocked_by']
    assert remote(world)['pr']['draft'] is True
    comments = remote(world)['comments']
    assert len(comments) == 4
    last = comments[-1]
    assert last['number'] == 11 and '<!-- workshop-comment: alpha:7 -->' in last['body']
    assert '记卡住' in last['body'] and 'exit 5' in last['body'] and 'flaky-test-failed' in last['body']
    record = [line for line in why(engine.log, 'alpha:7').splitlines() if '程序完成 交付' in line][-1]
    assert 'scripts/check.sh（exit 5）' in record and '退回执行已 3 次' in record
    assert 'first-line' in record and 'flaky-test-failed' in record
    # 卡住后再核：新的失败留一条，同一失败再核不重复留。
    flow = world['flow']
    evidence = flow._return({**stuck, 'checks': [['scripts/check.sh']]}, world['target'], [['scripts/check.sh']],
                            '合并前 DoD', 'again', '', head=sha)
    assert evidence.blocked
    flow._return({**stuck, 'checks': [['scripts/check.sh']]}, world['target'], [['scripts/check.sh']],
                 '合并前 DoD', 'again', '', head=sha)
    assert len(remote(world)['comments']) == 5
    engine.log.close()


def test_visual_rejections_do_not_count_toward_return_limit(world):
    """ws#254：亲眼看两次「不对」后，第一次合并前 DoD 没过仍退回执行，不卡住。"""
    engine, service, wait = visual_until_card(world, 'visual-count')
    for round_, number in enumerate((12, 13)):
        visual_reply(service, wait, f'不对：第 {round_ + 1} 次', operation=f'reply{round_}')
        for _ in range(4):
            engine.clock.now += timedelta(seconds=60)
            engine.tick()
        back = item(engine, '7')
        assert back['stage'] == '执行' and back['state'] != '卡住', back
        assert world['io'].read(back, world['target']).get('returns', 0) == 0
        # 修复是新的 PR、新的合并提交：亲眼看按交付版本再送一次。
        sha = commit(world, {f'fix{number}.txt': 'fix\n'}, f'fix {number}')
        old = remote(world)['pr']
        edit(world, sha=sha, prs=remote(world).get('prs', []) + [old],
             pr={'number': number, 'body': PR_BODY, 'head': {'sha': sha}, 'state': 'open', 'merged': False})
        if round_ == 1:
            break
        ensure_round(engine)
        finish(engine, key='7', evidence=[result(world, head=sha)])
        for _ in range(3):
            engine.tick()
        assert item(engine, '7')['state'] == '等你'
        wait = [w for w in service.store.all('wait') if w['id'] != wait['id']][-1]
    ensure_round(engine)
    missing_evidence(world)
    hand_over(engine, world)
    back = item(engine, '7')
    assert back['stage'] == '执行' and back['state'] != '卡住', back
    assert world['io'].read(back, world['target'])['returns'] == 1
    assert remote(world)['pr']['draft'] is True and '没附证据' in remote(world)['comments'][-1]['body']
    engine.log.close()


POST = '''## 上线后 DoD
- 线上跑的是新版本：
```workshop-checks
[["scripts/post.sh"]]
```
'''


def test_post_release_dod_waits_for_running_version_and_returns_without_rollback(world, monkeypatch):
    """上线后 DoD（核心 R4，issue交付 R3、R13）：车间自己的交付对象等在跑版本包含合并提交，再跑上线后检查；没过退回执行、不回滚。"""
    base = world['sha']
    sha = commit(world, {'scripts/post.sh': '#!/bin/sh\necho post-broken\nexit 3\n'}, 'post fails')
    edit(world, sha=sha)
    set_pr(world, head={'sha': sha})
    set_body(world, BODY + POST)
    running = {'commit': base}
    monkeypatch.setattr('workshop.running.current', lambda: running['commit'])
    engine = engine_for(world, 'post', deployment={'kind': 'running'})
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    waiting = item(engine, '7')
    assert waiting['stage'] == '交付' and waiting['state'] == '定时' and remote(world)['pr']['merged']
    assert remote(world)['issue']['state'] == 'open'
    running['commit'] = sha
    engine.clock.now = datetime.fromisoformat(waiting['after_wait']['at'])
    engine.tick()
    engine.tick()
    back = item(engine, '7')
    assert back['stage'] == '执行', (back['state'], back['next'], back.get('blocked_by'))
    [comment] = remote(world)['comments']
    assert comment['number'] == 7 and 'post-broken' in comment['body'] and '不回滚' in comment['body']
    assert 'merge' not in world['io'].read(back, world['target'])
    assert remote(world)['issue']['state'] == 'open'
    # 修复另开 PR：新提交让上线后检查通过，在跑版本包含它后才收尾。
    fixed = commit(world, {'scripts/post.sh': '#!/bin/sh\necho post-ok\n'}, 'post fixed')
    old = remote(world)['pr']
    edit(world, sha=fixed, prs=[old], pr={'number': 12, 'body': PR_BODY, 'head': {'sha': fixed}, 'state': 'open', 'merged': False})
    running['commit'] = fixed
    ensure_round(engine)
    hand_over(engine, world)
    engine.tick()
    assert item(engine, '7')['state'] == '完成'
    assert remote(world)['issue']['state'] == 'closed'
    assert stages_run(engine) == ['执行', '执行']
    engine.log.close()


def test_post_release_dod_without_deployment_rule_does_not_close(world):
    set_body(world, BODY + POST)
    engine = engine_for(world, 'post-no-rule')
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    engine.tick()
    assert item(engine, '7')['stage'] == '交付' and remote(world)['issue']['state'] == 'open'
    engine.log.close()


def test_total_waits_for_children_at_closing(world, monkeypatch):
    """核心 R32，issue交付 R12：总单照常执行、交付；到收尾还有子单没关就「等子单」，不派轮；子单全关后自动收尾。"""
    from workshop.core.models import Relations
    child = {'id': 'kun-g/workshop#8', 'name': 'ws#8', 'complete': False}
    monkeypatch.setattr(world['flow'], 'relations',
                        lambda objects, target: {o['id']: Relations(members=[dict(child)], converge='收尾') for o in objects})
    engine = engine_for(world, 'total')
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    engine.tick()
    waiting = item(engine, '7')
    assert waiting['stage'] == '收尾' and waiting['state'] == '等子单' and 'ws#8' in waiting['next']
    assert remote(world)['pr']['merged'] and remote(world)['issue']['state'] == 'open'
    assert stages_run(engine) == ['执行'] and not engine.log.all('cards')
    child['complete'] = True
    engine.tick()
    assert item(engine, '7')['state'] == '完成' and remote(world)['issue']['state'] == 'closed'
    assert stages_run(engine) == ['执行']
    engine.log.close()


@pytest.mark.parametrize('old,saved,new', [
    ('说清楚', {}, '接单'),
    ('证据', {}, '交付'),
    ('审查', {}, '交付'),
    ('验收', {'merge': 'a' * 40, 'delivered': ['a' * 40]}, '收尾'),
    ('验收', {'merge': 'a' * 40, 'delivered': []}, '交付'),
])
def test_items_on_removed_stages_move_to_new_stage(world, old, saved, new):
    """在途的活换新流程：落到对等阶段，状态和轮次记录保留。"""
    engine = engine_for(world, 'move-' + old + str(bool(saved.get('delivered'))))
    engine.ingest(Event('github', 'labeled', 'alpha', '7', 'test', {}, action='ready'))
    row = engine.log.get('items', 'alpha:7')
    engine.change(row, stage=old)
    world['io'].save(row, world['target'], {'effects': {}, 'issue': 7, **saved})
    engine.tick()
    moved = item(engine, '7')
    assert moved['stage'] == new and f'转到{new}' in moved['gate_reason']
    engine.log.close()


@pytest.mark.parametrize('stage', ['说清楚', '证据', '审查', '验收'])
def test_round_on_removed_stage_is_superseded(world, stage):
    receipt = Receipt('done', '旧流程的回执', advance='执行', evidence=[result(world)])
    evidence = world['flow'].assess(snapshot(world, stage), receipt, world['target'])
    evidence.validate()
    assert evidence.superseded and not evidence.passed


def visual_until_card(world, name):
    from test_intervention_business import Port
    from workshop.plugins.intervention.service import Service
    set_body(world, BODY.replace('## 要不要亲眼看\n否\n', '## 要不要亲眼看\n是\n看什么：打开状态页看错误提示\n在哪看：飞书私聊\n怎么回：引用送达回复过或不对\nKun 确认：会话 `2ba67f64-4e2b-47b0-9213-00c871c6e28c`\n> 看错误提示\n'))
    engine = engine_for(world, name)
    service = Service(world['root'] / (name+'service'), Path('rules/workshop.yaml'))
    service.transport = lambda *a: {'state':'sent','external':'om_visual'}
    service.poll_reply = lambda wait: None
    port = Port(service)
    engine.plugins.register('wait', 'intervention', port)
    engine.rules.data['wait_services'] = {'intervention': {}}
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    for _ in range(3):
        engine.tick()
    assert item(engine, '7')['state'] == '等你'
    assert not engine.log.all('cards')
    [wait] = service.store.all('wait')
    return engine, service, wait


def visual_reply(service, wait, value, operation='reply'):
    return service.call('answer', {'operation':operation, 'ref':wait['id'], 'value':value,
        'source': {'channel':'external','reference':'om_' + operation,'evidence':'回源身份核实',
                   'version':wait['content']['version'],'delivery':wait['content']['delivery']}})


def test_visual_yes_closes_by_program(world):
    engine, service, wait = visual_until_card(world, 'visual-yes')
    assert remote(world)['issue']['state'] == 'open'
    visual_reply(service, wait, '过')
    for _ in range(4):
        engine.clock.now += timedelta(seconds=60)
        engine.tick()
    assert item(engine, '7')['state'] == '完成' and remote(world)['issue']['state'] == 'closed'
    assert stages_run(engine) == ['执行']
    assert len(service.store.all('delivery')) == 1
    engine.log.close()


def test_visual_no_returns_to_execution(world):
    engine, service, wait = visual_until_card(world, 'visual-no')
    visual_reply(service, wait, '不对：错误提示看不到')
    for _ in range(4):
        engine.clock.now += timedelta(seconds=60)
        engine.tick()
    assert item(engine, '7')['stage'] == '执行' and remote(world)['issue']['state'] == 'open'
    brief = ensure_round(engine)['brief']
    assert '亲眼看' in brief['instructions']
    assert '不对：错误提示看不到' in json.dumps(brief['materials'], ensure_ascii=False)
    assert '上次退回执行的原因（亲眼看' in brief['instructions']
    assert 'Kun 的原文：不对：错误提示看不到' in brief['instructions']
    engine.log.close()


def test_visual_fail_button_text_reaches_rework_brief(world):
    from test_intervention_business import click_event
    engine, service, wait = visual_until_card(world, 'visual-button')
    [delivery] = service.store.all('delivery')
    result = service.call('status_click', click_event(delivery['payload']['card'], 'fail', '状态页错误提示被遮住',
                                                      message=delivery['external']))
    assert result['accepted'] and result['answer'] == '不对：状态页错误提示被遮住'
    for _ in range(4):
        engine.clock.now += timedelta(seconds=60)
        engine.tick()
    assert item(engine, '7')['stage'] == '执行'
    assert 'Kun 的原文：不对：状态页错误提示被遮住' in ensure_round(engine)['brief']['instructions']
    engine.log.close()


def test_execution_brief_names_receipt_fields_and_handover_rule(world):
    engine = engine_for(world, 'brief')
    start(engine)
    brief = ensure_round(engine)['brief']['instructions']
    assert 'outcome 只能是 done、failed、nothing' in brief and 'issue-delivery/v1' in brief
    assert 'Draft' in brief
    engine.log.close()


def test_auto_close_keyword_returns_to_execution(world):
    """PR 写了自动关 issue 的语句：合并会越过上线后 DoD 关 issue，执行交接时就不算交，同阶段重来。"""
    set_pr(world, body=PR_BODY + 'Closes #7\n')
    engine = engine_for(world, 'auto-close')
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    back = item(engine, '7')
    assert back['stage'] == '执行' and '自动关闭' in back['gate_reason']
    state = remote(world)
    assert not state['pr']['merged'] and state['issue']['state'] == 'open'
    assert '自动关闭' in ensure_round(engine)['brief']['instructions']
    engine.log.close()


def test_ci_pending_at_delivery_waits_without_rounds(world):
    edit(world, checks=[{'name': 'check', 'status': 'in_progress', 'conclusion': None}])
    engine = engine_for(world, 'ci-pending')
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    waiting = item(engine, '7')
    assert waiting['stage'] == '交付' and waiting['state'] != '卡住', waiting
    assert not engine.flow_broken('alpha', '') and not remote(world)['pr']['merged']
    edit(world, checks=[{'name': 'check', 'status': 'completed', 'conclusion': 'success'}])
    for _ in range(3):
        engine.clock.now += timedelta(minutes=10)
        engine.tick()
    assert item(engine, '7')['state'] == '完成' and remote(world)['pr']['merged']
    assert stages_run(engine) == ['执行']
    engine.log.close()


def test_merge_conflict_does_not_merge(world):
    set_pr(world, mergeable=False, mergeable_state='dirty')
    engine = engine_for(world, 'conflict')
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    engine.tick()
    row = item(engine, '7')
    assert not remote(world)['pr']['merged'] and remote(world)['issue']['state'] == 'open'
    assert row['stage'] == '执行' and remote(world)['pr']['draft']
    assert '合并冲突' in current(engine, '7')['brief']['instructions']
    assert not engine.flow_broken('alpha', '')
    engine.log.close()


def test_head_changed_after_dod_is_rechecked_not_merged(world):
    target = world['target']
    row = snapshot(world, '交付')
    world['io'].save(row, target, {'effects': {}, 'issue': 7, 'pr': 11, 'dod_head': 'a' * 40})
    after = world['flow'].after(row, '交付', 'deliver', target)
    assert after.pending and '重新查合并前 DoD' in after.reason
    assert not remote(world)['pr']['merged']


def test_renaming_out_of_legacy_frozen_path_has_no_approval_gate(world):
    edit(world, files=[{'filename':'docs/check.sh','previous_filename':'scripts/check.sh','status':'renamed'}])
    engine = engine_for(world, 'frozen', frozen_paths=['scripts/'])
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    engine.tick()
    assert remote(world)['pr']['merged']
    assert not engine.log.all('cards')
    engine.log.close()


@pytest.mark.parametrize('accepted', [False, True])
def test_legacy_acceptance_return_with_delivered_merge_closes_without_rounds(world, accepted):
    """在途迁移：旧流程验收没过退回执行、代码已合并交付的活（现役 ws#214）不再起执行轮，程序核对后收尾；
    验收通过过的活被返工，照常起执行轮。"""
    engine = engine_for(world, f'legacy-{accepted}')
    engine.ingest(Event('github', 'labeled', 'alpha', '7', 'test', {}, action='ready'))
    row = engine.log.get('items', 'alpha:7')
    engine.change(row, stage='执行', checks=[['scripts/check.sh']],
                  history=[{'stage': '接单'}, {'stage': '说清楚'}, {'stage': '执行'}, {'stage': '交付'}, {'stage': '验收'}])
    sha = world['sha']
    set_pr(world, merged=True, state='closed', merge_commit_sha=sha, merged_at='2026-11-18T06:04:41Z')
    world['io'].save(row, world['target'], {'effects': {}, 'issue': 7, 'pr': 11, 'merge': sha, 'delivered': [sha],
                                            **({'accepted': True} if accepted else {})})
    for _ in range(4):
        engine.tick()
    if accepted:
        assert stages_run(engine) == ['执行']
    else:
        assert item(engine, '7')['state'] == '完成' and remote(world)['issue']['state'] == 'closed'
        assert stages_run(engine) == []
    engine.log.close()


@pytest.mark.parametrize('mergeable,merge_state', [(False, 'clean'), (True, 'dirty'), (False, 'dirty')])
def test_conflict_returns_to_draft_then_rechecks_new_head(world, monkeypatch, mergeable, merge_state):
    set_pr(world, mergeable=mergeable, mergeable_state=merge_state)
    verified = []
    verify = world['io'].verify

    def record(target, sha, checks):
        verified.append(sha)
        return verify(target, sha, checks)

    monkeypatch.setattr(world['io'], 'verify', record)
    engine = engine_for(world, 'conflict-roundtrip')
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    back = item(engine, '7')
    assert back['stage'] == '执行' and back['state'] != '卡住'
    assert remote(world)['pr']['draft'] and not verified
    assert not any(c[1:3] == ['pr', 'merge'] for c in calls(world))
    [comment] = remote(world)['comments']
    assert 'mergeable_state' in comment['body'] and 'mergeable' in comment['body']
    assert '合并冲突' in comment['body'] and '<!-- workshop-comment: alpha:7 -->' in comment['body']
    brief = ensure_round(engine)['brief']['instructions']
    assert '合最新 main、解冲突、重跑检查后推新提交' in brief
    sha = commit(world, {'resolved.txt': 'resolved'}, 'resolve conflict')
    set_pr(world, head={'sha': sha}, mergeable=True, mergeable_state='clean', draft=False)
    edit(world, sha=sha)
    hand_over(engine, world)
    engine.tick()
    assert item(engine, '7')['state'] == '完成'
    assert verified == [sha] and remote(world)['pr']['merged']
    assert world['io'].read(back, world['target'])['returns'] == 1
    assert stages_run(engine) == ['执行', '执行'] and not engine.log.all('cards')
    engine.log.close()


@pytest.mark.parametrize('mergeable,merge_state', [(False, 'dirty'), (True, 'dirty'),
                                                   (False, 'clean'), (True, 'behind'),
                                                   (True, 'blocked'), (True, 'unstable')])
def test_merge_rejection_returns_once_and_can_deliver_after_fix(world, mergeable, merge_state):
    edit(world, merge_error='gh rejection tail',
         merge_failure_pr={'mergeable': mergeable, 'mergeable_state': merge_state})
    engine = engine_for(world, 'merge-rejected')
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    # A new plugin instance must consume the persisted failure, without retrying the merge.
    engine.plugins.register('flow', 'issue_delivery', type(world['flow'])(world['io']))
    engine.tick()
    back = item(engine, '7')
    assert back['stage'] == '执行' and back['state'] != '卡住', back
    assert remote(world)['pr']['draft'] and not remote(world)['pr']['merged']
    assert len([c for c in calls(world) if c[1:3] == ['pr', 'merge']]) == 1
    [comment] = remote(world)['comments']
    assert 'gh rejection tail' in comment['body'] and merge_state in comment['body']
    brief = ensure_round(engine)['brief']['instructions']
    assert 'gh rejection tail' in brief and '重跑检查后推新提交' in brief
    assert world['io'].read(back, world['target'])['returns'] == 1
    edit(world, merge_error='')
    set_pr(world, mergeable=True, mergeable_state='clean', draft=False)
    hand_over(engine, world)
    engine.tick()
    assert item(engine, '7')['state'] == '完成'
    assert 'merge_failure' not in world['io'].read(back, world['target'])
    assert not engine.flow_broken('alpha', '') and not engine.log.all('cards')
    engine.log.close()


@pytest.mark.parametrize('during_merge', [False, True])
@pytest.mark.parametrize('mergeable,merge_state', [(None, 'clean'), (True, 'unknown'), (None, 'unknown')])
def test_unknown_mergeability_waits_and_recovers(world, during_merge, mergeable, merge_state):
    values = {'mergeable': mergeable, 'mergeable_state': merge_state}
    if during_merge:
        edit(world, merge_error='not computed', merge_failure_pr=values)
    else:
        set_pr(world, **values)
    engine = engine_for(world, 'unknown')
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    waiting = item(engine, '7')
    assert waiting['stage'] == '交付' and waiting['state'] == '定时', waiting
    assert not remote(world)['pr'].get('draft') and not remote(world).get('comments')
    assert len([c for c in calls(world) if c[1:3] == ['pr', 'merge']]) == int(during_merge)
    edit(world, merge_error='')
    set_pr(world, mergeable=True, mergeable_state='clean')
    engine.clock.now = datetime.fromisoformat(waiting['after_wait']['at'])
    engine.tick()
    engine.tick()
    assert item(engine, '7')['state'] == '完成' and stages_run(engine) == ['执行']
    engine.log.close()


def test_conflicts_share_return_limit_with_both_dod_segments(world, monkeypatch):
    sha = commit(world, {'scripts/post.sh': '#!/bin/sh\necho post-broken\nexit 3\n'}, 'post fails')
    edit(world, sha=sha)
    set_pr(world, head={'sha': sha})
    set_body(world, BODY + POST)
    monkeypatch.setattr('workshop.running.current', lambda: sha)
    engine = engine_for(world, 'shared-limit', deployment={'kind': 'running'})
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    engine.tick()
    assert item(engine, '7')['stage'] == '执行'
    assert world['io'].read(item(engine, '7'), world['target'])['returns'] == 1
    old = remote(world)['pr']
    edit(world, prs=[old], pr={'number': 12, 'body': PR_BODY, 'head': {'sha': sha}, 'state': 'open', 'merged': False})
    ensure_round(engine)
    missing_evidence(world)
    hand_over(engine, world)
    assert world['io'].read(item(engine, '7'), world['target'])['returns'] == 2
    ensure_round(engine)
    set_pr(world, body=PR_BODY, draft=False, mergeable=False, mergeable_state='dirty')
    hand_over(engine, world)
    assert item(engine, '7')['stage'] == '执行'
    assert world['io'].read(item(engine, '7'), world['target'])['returns'] == 3
    ensure_round(engine)
    set_pr(world, draft=False, mergeable=True, mergeable_state='clean')
    edit(world, merge_error='blocked again', merge_failure_pr={'mergeable_state': 'blocked'})
    hand_over(engine, world)
    engine.tick()
    stuck = item(engine, '7')
    assert stuck['state'] == '卡住' and '退回执行已 3 次' in stuck['blocked_by']
    # 到上限也和普通退回一样在 PR 留原样输出（ws#254）。
    comments = remote(world)['comments']
    assert len(comments) == 4 and '记卡住' in comments[-1]['body'] and 'blocked again' in comments[-1]['body']
    assert not engine.log.all('cards')
    engine.log.close()


def test_conflict_appearing_after_dod_returns_without_attempting_merge(world, monkeypatch):
    verify = world['io'].verify

    def conflict_after_check(target, sha, checks):
        runs = verify(target, sha, checks)
        set_pr(world, mergeable=False, mergeable_state='dirty')
        return runs

    monkeypatch.setattr(world['io'], 'verify', conflict_after_check)
    engine = engine_for(world, 'conflict-race')
    start(engine)
    ensure_round(engine)
    hand_over(engine, world)
    engine.tick()
    assert item(engine, '7')['stage'] == '执行' and remote(world)['pr']['draft']
    assert not any(c[1:3] == ['pr', 'merge'] for c in calls(world))
    assert world['io'].read(item(engine, '7'), world['target'])['returns'] == 1
    assert '合并冲突' in remote(world)['comments'][0]['body']
    engine.log.close()
