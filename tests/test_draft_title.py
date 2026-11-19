"""ws#277（介入点 R3、R4）：标题带【Draft】的 issue 不核对、不接单、不开工；去掉标记后照常接单。

真引擎 + 真插件 + 替身 gh（回放测试目录里的 JSON）；时间用引擎的假时钟，不用 sleep。
"""
from datetime import timedelta

import pytest

from conftest import item
from test_issue_delivery import world, BODY, edit, calls  # noqa: F401
from test_issue_end_to_end import engine_for, start, remote, ensure_round
from test_github_identity_flow import configure
from workshop.core.models import Event
from workshop.reports.report import status, why

APP = 'lambdatheory-agent[bot]'


def auto_target(world):
    return {**world['target'], 'auto_intake': True, 'intake_app_logins': [APP]}


def set_issue(world, **changes):
    issue = remote(world)['issue']
    issue.update(changes)
    edit(world, issue=issue)


def source(ext):
    return Event('github', ext, 'alpha', '7', '登记', {'kind': 'issue', 'number': '7'})


def label_writes(world):
    return [c for c in calls(world) if '/labels' in str(c)]


def test_draft_unlabeled_issue_is_not_checked_labeled_or_created(world, monkeypatch):
    """DoD 1、3：写全、作者可信的未打标签 issue，标题带【Draft】不核对登记、不打标签、不建活、不评论；
    去掉标记、正文一字不改，下一拍同一版正文照常核对并接单。"""
    configure(world, monkeypatch)
    set_issue(world, user={'login': 'kun-g'}, labels=[], title='【Draft】草稿标记', body=BODY)
    flow, target = world['flow'], auto_target(world)
    for ext in ('1', '2'):
        value = flow.classify(source(ext), None, target, None)
        assert value.discard and '【Draft】' in value.summary
    assert not label_writes(world)
    assert not any('/comments' in str(c) for c in calls(world))
    assert not flow.io.read({'id': 'alpha:7', 'target': 'alpha'}, target).get('intake_versions')
    set_issue(world, title='草稿标记')
    assert flow.classify(source('3'), None, target, None).action == 'ready'
    assert len(label_writes(world)) == 1


def test_draft_issue_engine_creates_no_item_until_marker_removed(world, monkeypatch):
    """DoD 1、3 走真引擎：标题带【Draft】不建活；去掉后同一版正文建活进接单。"""
    configure(world, monkeypatch)
    cfg = {'default_flow': 'issue_delivery', 'flows': {'issue_delivery': {
        'triggers': [{'source': 'github', 'when': {'kind': 'issue'}, 'action': 'classify'}],
        'stages': ['接单', '执行', '交付', '收尾']}}, 'auto_intake': True, 'intake_app_logins': [APP]}
    engine = engine_for(world, 'draft-intake', **cfg)
    set_issue(world, user={'login': 'kun-g'}, labels=[], title='【Draft】草稿标记', body=BODY)
    engine.ingest(source('draft'))
    engine.classify_events()
    assert engine.log.get('items', 'alpha:7') is None
    assert not label_writes(world)
    set_issue(world, title='草稿标记')
    engine.ingest(source('final'))
    engine.classify_events()
    assert engine.log.get('items', 'alpha:7')
    assert len(label_writes(world)) == 1
    assert not engine.log.all('cards')
    engine.log.close()


def test_draft_issue_with_ready_label_waits_at_intake(world):
    """DoD 2：已经带 Ready for Agent 的【Draft】issue 建活后停在接单「等资料」，理由写明，不派模型轮；
    去掉标记来一条更新，下一拍接单。"""
    set_issue(world, title='【Draft】草稿标记')
    engine = engine_for(world, 'draft-labeled')
    start(engine)
    waiting = item(engine, '7')
    assert waiting['stage'] == '接单' and waiting['state'] == '等资料'
    assert '标题带【Draft】' in waiting['next']
    for _ in range(3):
        engine.clock.now += timedelta(hours=1)
        engine.tick()
    assert item(engine, '7')['state'] == '等资料'
    assert not engine.log.all('rounds') and not engine.log.all('cards')
    assert '标题带【Draft】' in status(engine.log) and '标题带【Draft】' in why(engine.log, 'alpha:7')
    set_issue(world, title='草稿标记')
    engine.ingest(Event('github', 'issue-edited', 'alpha', '7', 'test', {'kind': 'issue', 'number': 7}))
    engine.tick()
    assert item(engine, '7')['stage'] == '执行'
    engine.log.close()


PLAIN = [('草稿标记', BODY + '\n【Draft】\n'), ('【draft】草稿标记', BODY), ('[Draft] 草稿标记', BODY),
         ('［Draft］草稿标记', BODY)]


@pytest.mark.parametrize('title,body', PLAIN)
def test_only_exact_title_marker_counts_for_auto_intake(world, monkeypatch, title, body):
    """DoD 4：只认标题里原样的【Draft】；正文含、小写、半角或别的括号都按普通 issue 自动接单。"""
    configure(world, monkeypatch)
    set_issue(world, user={'login': 'kun-g'}, labels=[], title=title, body=body)
    assert world['flow'].classify(source('1'), None, auto_target(world), None).action == 'ready'
    assert len(label_writes(world)) == 1


@pytest.mark.parametrize('title,body', PLAIN)
def test_only_exact_title_marker_counts_for_labeled_intake(world, title, body):
    """DoD 4：同上，已带标签的接单核对照常通过。"""
    set_issue(world, title=title, body=body)
    engine = engine_for(world, 'plain')
    start(engine)
    assert item(engine, '7')['stage'] == '执行'
    engine.log.close()


def test_marker_added_after_intake_keeps_stage_and_running_round(world):
    """DoD 5：已过接单的活，标题后来加上【Draft】不改阶段、不中断在跑的轮。"""
    engine = engine_for(world, 'draft-late')
    start(engine)
    running = ensure_round(engine)
    assert item(engine, '7')['stage'] == '执行'
    set_issue(world, title='【Draft】后来加的')
    assert not world['flow'].classify(source('late'), item(engine, '7'), auto_target(world), None).discard
    engine.ingest(Event('github', 'issue-edited', 'alpha', '7', 'test', {'kind': 'issue', 'number': 7}))
    engine.tick()
    after = item(engine, '7')
    assert after['stage'] == '执行'
    assert [r['token'] for r in engine.active()] == [running['token']]
    engine.log.close()
