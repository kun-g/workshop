"""新版报告：大跨度发布、多部署、角色继承、复制反馈。"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from test_watch_updates import (advance, item_for, judgment, judged, release, software,
                                world)  # noqa: F401
from workshop.core.models import Event
from workshop.plugins.watch_updates.feedback import feedback
from workshop.plugins.watch_updates.io import write
from workshop.plugins.watch_updates.model import current_judgment, reconcile, validate_judgment
from workshop.plugins.watch_updates.presentation import targets
from workshop.plugins.watch_updates.report import blind_spots, render, summary


def report_fixture():
    """全部为合成资料：每个软件跨 40 个发布，不使用真实运维状态。"""
    versions = {}
    for name, verdict in [('Teleport', '该升'), ('Sentry', '该升'), ('Docker', '可以等')]:
        soft = software(name)
        if name == 'Teleport':
            soft['instances'] = soft['instances'][:1] + [
                {**soft['instances'][0], 'id': 'agent-' + host, 'host': host, 'service': 'agent', 'role': 'agent'}
                for host in ('dc1', 'dc2', 'sc1', 'Mac')]
        rows = [release(f'1.{i}.0') for i in range(1, 41)]
        supplied = {'instances': [{**judgment()['instances'][0], 'id': p['id'], 'verdict': verdict,
            'headline': '修复公开入口的认证绕过' if verdict == '该升' else '当前未遇到这些问题，可以等下一轮',
            'links': [rows[-1]['html_url']], 'features': [],
            'points': [{'version': f'1.{i}.0', 'text': text} for i, text in (
                (38, '修复影响现有部署的认证检查'), (37, '修复恢复连接时的错误重试'), (36, '补齐长期运行服务的资源回收'))]}
            for p in soft['instances'] if p['role'] == 'server']}
        value = validate_judgment(supplied, soft, rows, [], [])
        versions[name + '-1.40.0'] = {'software': soft, 'releases': rows, 'judgment': value}
    report = {'id': 'wu-weekly-fixture-v2', 'kind': 'weekly', 'anchor': 'fixture', 'versions': list(versions),
              'snapshot': deepcopy(versions), 'needs': [], 'blind': [{'id': 'test-cluster', 'evidence': '只读身份未取到清单'}],
              'changes': [{'id': 'new-tool', 'name': '新工具', 'action': '加入'}]}
    report['feedback_targets'] = targets(report, versions, [])
    report['summary'] = summary(report['versions'], versions, True, report['blind'])
    return report, versions


def test_large_report_is_short_and_human_readable():
    report, versions = report_fixture()
    html = render(report, versions, [], datetime.now(timezone.utc))
    assert len(versions['Teleport-1.40.0']['releases']) == 40
    assert html.count('data-software=') == 3
    assert html.count('<li>') == 6  # 两个该升，各三条；可以等不铺发布记录。
    assert '<a href="https://example.com/1.38.0" rel="noreferrer">1.38.0</a>' in html
    for empty in ('本项没有', '未反馈', '已等 0 天', '发布说明原文', '<select', 'references', 'signals', 'profile'):
        assert empty not in html
    assert '可能用得上的新功能' not in html and '需要确认' not in html
    assert html.count('data-feedback="Teleport-agents"') == 1
    assert 'dc1、dc2、sc1、Mac · 跟服务端走' in html
    assert html.index('服务端 dc1') < html.index('agent <small>')
    assert '先升服务端' in html and 'agent 不能比服务端新' in html
    assert '清单变化 1 项' in html and '<details open' not in html
    lines = report['summary'].splitlines()
    assert len(lines) == 5 and lines[-1] == '盲区 1 项'
    assert 'Teleport · 1.0.0→1.40.0 · 该升 · 1 个实例 · agent 4 处' in lines
    assert '认证绕过' not in report['summary']


@pytest.mark.parametrize('change', [
    {'headline': '字' * 41}, {'headline': '根据profile判断'}, {'headline': '分成\n两行'},
    {'points': [{'version': '2.0.0', 'text': '修复'}] * 4},
    {'points': [{'version': '9.0.0', 'text': '没读过的发布'}]},
    {'points': [{'version': '2.0.0', 'text': '字' * 46}]},
    {'points': [{'version': '2.0.0', 'text': '修复认证绕过；资料未证实已触发，不标紧急'}]},
    {'headline': '管着约 47 个容器的面板有漏洞'},
])
def test_human_fields_are_validated(change):
    value = judgment()
    value['instances'][0].update(change)
    with pytest.raises(ValueError):
        validate_judgment(value, software(), [release('2.0.0')], [], [{'id': 'project'}])


def test_roles_required_and_agents_inherit_without_separate_judgments():
    soft = software()
    soft['instances'][1]['role'] = 'agent'
    value = judgment(True)
    value['instances'] = value['instances'][:1]
    value = validate_judgment(value, soft, [release('2.0.0')], [], [{'id': 'project'}])
    assert value['instances'][1]['verdict'] == '该升' and value['instances'][1]['inherited']
    assert not value['instances'][1]['features']
    again = validate_judgment(deepcopy(value), soft, [release('2.0.0')], [], [{'id': 'project'}])
    assert again == value  # assess 和 after 都校验，不重复添升级顺序。
    soft['instances'][0].pop('role')
    with pytest.raises(ValueError, match='role'):
        reconcile({'software': [], 'excluded': {}}, {'software': [soft], 'sources': [{'id': 'server', 'ok': True, 'evidence': 'read'}]}, 'now')


def test_no_server_and_unknown_server_never_invent_agent_conclusion():
    for no_server in (False, True):
        soft = software()
        soft['instances'][1]['role'] = 'agent'
        soft['instances'][0].update(version='unknown', error='无法读取')
        if no_server:
            soft['instances'] = soft['instances'][1:]
        value = validate_judgment({'instances': []}, soft, [release('2.0.0')], [], [])
        assert all(r['verdict'] == '版本不明' for r in value['instances'])
        assert all(not r.get('signals') for r in value['instances'])


def test_feedback_groups_expand_snapshot_and_ignore_prose():
    report, versions = report_fixture()
    state = {'reports': {report['id']: report}, 'versions': versions}
    # 后来的清单增加 agent，不会被旧报告一并选中。
    versions['Teleport-1.40.0']['software']['instances'].append({'id': 'new-agent', 'role': 'agent'})
    payload = {'report': report['id'], 'items': [{'id': 'Teleport-agents', 'choice': '升', 'note': '窗口期再升'}]}
    text = '其他软件都升（这句不作选择）\n```json\n' + json.dumps(payload) + '\n```\n'
    parsed = feedback(text, state)
    assert {r['instance'] for r in parsed['items']} == {'agent-dc1', 'agent-dc2', 'agent-sc1', 'agent-Mac'}
    assert all(r['decision'] == '升' and r['note'] == '窗口期再升' for r in parsed['items'])
    for bad in ({**payload, 'schema': 'watch-feedback/v1'}, {**payload, 'report': 'missing'},
                {**payload, 'items': payload['items'] * 2},
                {**payload, 'items': [{'id': 'new-agent', 'choice': '升'}]},
                {**payload, 'items': [{'id': 'Teleport-agents', 'choice': '随便'}]}):
        with pytest.raises(ValueError):
            feedback(json.dumps(bad), state)


def test_unknown_is_counted_once_and_remains_collapsed():
    report, versions = report_fixture()
    place = versions['Sentry-1.40.0']['software']['instances'][1]
    place.update(version='unknown', error='测试网络不可达')
    current = versions['Sentry-1.40.0']
    current['judgment'] = validate_judgment(current['judgment'], current['software'], current['releases'], [], [])
    report.pop('snapshot')
    report['blind'] = blind_spots(report['versions'], versions, [])
    report['feedback_targets'] = targets(report, versions, [])
    assert len(blind_spots(report['versions'], versions, report['blind'])) == 1
    html = render(report, versions, [], datetime.now(timezone.utc))
    # 原始错误留在对账记录里，页面只按类计数、列名字（Kun 11-15：写给程序看的长段不上页面）。
    assert '测试网络不可达' not in html
    assert '<summary>1 处版本不明</summary>' in html


def test_inventory_invalidates_legacy_judgment_and_updates_roles(world):
    io, target, flow, source = world
    item, poll = judged(world)
    state = io.state(target)
    old = state['versions'][item['object_key']]
    old['judgment'].pop('format')
    for place in old['software']['instances']:
        place.pop('role')
    io.save(target, state)
    assert not flow.automatic(item, '执行', target)
    week = item_for(next(e for e in poll.events if e.body['kind'] == 'reconcile'))
    soft = software()
    soft['instances'][1]['role'] = 'agent'
    advance(flow, week, target, '说清楚', {'inventory': {'sources': [{'id': 'server', 'ok': True, 'evidence': 'read'}], 'software': [soft]}})
    saved = io.state(target)
    assert saved['versions'][item['object_key']]['needs_judgment']
    assert saved['versions'][item['object_key']]['software']['instances'][1]['role'] == 'agent'
    assert any(row['object_key'] == item['object_key'] for row in saved['outbox'].values())
    assert not current_judgment(saved['versions'][item['object_key']])


def test_multiple_pending_versions_share_one_card_and_choice():
    report, versions = report_fixture()
    previous = deepcopy(versions['Teleport-1.40.0'])
    previous['releases'] = previous['releases'][:-1]
    previous['judgment']['instances'][0]['points'] = []
    versions['Teleport-1.39.0'] = previous
    report['versions'].append('Teleport-1.39.0')
    report['snapshot'] = deepcopy(versions)
    report['feedback_targets'] = targets(report, versions, [])
    html = render(report, versions, [], datetime.now(timezone.utc))
    assert html.count('data-software="Teleport"') == 1
    assert html.count('data-feedback="Teleport-agents"') == 1
    value = feedback(json.dumps({'report': report['id'], 'items': [{'id': 'Teleport-agents', 'choice': '升'}]}),
                     {'reports': {report['id']: report}})
    assert len(value['items']) == 8
    assert {r['key'] for r in value['items']} == {'Teleport-1.39.0', 'Teleport-1.40.0'}
    # 单独服务端选择不波及组里的 agent。
    selected = feedback(json.dumps({'report': report['id'], 'items': [{'id': 'Teleport:one', 'choice': '没问题'}]}),
                        {'reports': {report['id']: report}})
    assert len(selected['items']) == 2 and all(r['instance'] == 'one' for r in selected['items'])


def test_legacy_judgment_waits_then_appears_as_blind_spot(world):
    io, target, flow, source = world
    item, poll = judged(world)
    state = io.state(target)
    current = state['versions'][item['object_key']]
    current['judgment'].pop('format')
    current['judgment']['instances'][0]['reason'] = '长篇校验自证不应渲染'
    io.save(target, state)
    week = item_for(next(e for e in poll.events if e.body['kind'] == 'reconcile'))
    assert advance(flow, week, target, '交付').pending
    from datetime import timedelta
    io.time += timedelta(minutes=61)
    advance(flow, week, target, '交付')
    assert io.sent[0]['versions'] == []
    assert io.sent[0]['blind'] == [{'judgment_pending': item['object_key']}]
    html = Path(io.sent[0]['path']).read_text()
    assert '长篇校验自证不应渲染' not in html and '个软件没判成' in html


def test_waiting_age_appears_only_after_first_day():
    report, versions = report_fixture()
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    snapshot = report['snapshot']['Teleport-1.40.0']
    snapshot['first_reported'] = now.isoformat()
    assert '已等 0 天' not in render(report, versions, [], now)
    snapshot['first_reported'] = '2026-11-09T00:00:00+00:00'
    assert '已等 7 天' in render(report, versions, [], now)
    snapshot['feedback'] = {p['id']: {'decision': '升'} for p in snapshot['software']['instances']}
    assert '已等 7 天' not in render(report, versions, [], now)


def test_blind_spots_are_grouped_with_short_names():
    from workshop.plugins.watch_updates.report import blind_groups
    blind = [{'id': 'servers', 'ok': False, 'evidence': 'ssh 全部 Operation not permitted，详见 collector/ssh-*.json'},
             {'software': 'GitLab（自建，git.example）', 'host': 'gitlab-1 (aws i-0123)', 'service': 'gitlab',
              'version': '版本不明', 'reason': 'DNS no such host'},
             {'software': 'NetBird', 'host': 'dc1', 'service': 'netbird', 'version': '版本不明', 'reason': 'x'},
             {'software': 'NetBird', 'host': 'dc2', 'service': 'netbird', 'version': '版本不明', 'reason': 'x'}]
    groups = blind_groups(blind, {})
    assert groups == [('3 处版本不明', '对账没连上：服务器，这些部署沿用上次读数或读不到版本。',
                       ['GitLab · gitlab-1', 'NetBird ×2'])]
    assert 'Operation not permitted' not in str(groups)


def test_version_only_changes_are_not_listed():
    report, versions = report_fixture()
    report['changes'] = [{'id': 'a', 'name': 'Alpha', 'action': '更新'}, {'id': 'b', 'name': 'Beta', 'action': '加入'}]
    html = render(report, versions, [], datetime.now(timezone.utc))
    assert '清单变化 1 项' in html and 'Alpha' not in html and 'Beta · 加入' in html


def test_release_vocabulary_is_not_mistaken_for_hedging():
    value = judgment()
    value['instances'][0].update(headline='SSH 转发隔离和角色校验有修复', points=[{'version': '2.0.0', 'text': 'TLS 校验拒绝非规范 IP 地址'}])
    validate_judgment(value, software(), [release('2.0.0')], [], [{'id': 'project'}])


def test_same_feature_on_several_instances_is_asked_once():
    from workshop.plugins.watch_updates.presentation import targets
    report, versions = report_fixture()
    for source in (versions, report['snapshot']):
        rows = source['Sentry-1.40.0']['judgment']['instances']
        rows.append({**rows[0], 'id': 'second'})
        for row in rows:
            row['features'] = [{'id': 'queue-alert', 'summary': '队列耗尽预警', 'direction': 'project'}]
    found = [t for t in targets(report, versions, []).values() if t['kind'] == 'feature']
    assert len(found) == 1
