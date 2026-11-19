from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import pytest

from workshop.core.models import Event, Receipt
from workshop.plugins.watch_updates.entries import Entries as Flow, feedback
from workshop.plugins.watch_updates.io import IO, read, write
from workshop.plugins.watch_updates.model import SCHEMA, reconcile, release_range, validate_judgment, validate_software, version
from workshop.plugins.watch_updates.report import render
from workshop.plugins.watch_updates.source import Source


def software(name='n8n', **extra):
    return {'id': name, 'name': name, 'kind': 'infra', 'upstream': 'example/' + name,
            'source': 'server', 'tag_pattern': r'(?:v|n8n@)?\d+\.\d+\.\d+(?:-rc\.\d+)?',
            'prerelease': False, 'operated_by_us': True, 'evidence': 'running service',
            'profile': {'exposed': True, 'data': 'workflows', 'users': ['Kun'], 'paired': []},
            'instances': [{'id': 'one', 'host': 'dc1', 'service': 'first.example', 'version': '1.0.0',
                           'role': 'server', 'read_at': '2026-11-14T01:00:00+00:00', 'evidence': 'version output 1.0.0'},
                          {'id': 'two', 'host': 'dc2', 'service': 'second.example', 'version': '1.1.0',
                           'role': 'server', 'read_at': '2026-11-14T01:00:00+00:00', 'evidence': 'version output 1.1.0'}],
            'notes': [], **extra}


def release(tag, **extra):
    return {'tag_name': tag, 'body': 'Release notes ' + tag, 'html_url': 'https://example.com/' + tag,
            'prerelease': False, 'draft': False, **extra}


def judgment(urgent=False):
    return {'instances': [{'id': p, 'verdict': '该升' if urgent else '不用升',
             'priority': '紧急' if urgent else '普通', 'reason': '影响公开服务' if urgent else '没有相关修复',
             'headline': '影响公开服务' if urgent else '没有相关修复',
             'points': [{'version': '2.0.0', 'text': '修复影响公开服务的漏洞'}] if urgent else [], 'references': ['profile'], 'links': ['https://example.com/2.0.0'],
             'signals': {'high_risk_vulnerability': urgent},
             'features': [{'id': 'mcp', 'summary': 'agent 操作 GitLab', 'direction': 'project'}]}
            for p in ('one', 'two')]}


class FakeIO(IO):
    def __init__(self, root):
        self.time = datetime(2026, 11, 16, 1, 0, tzinfo=timezone.utc)
        super().__init__(lambda: self.time)
        self.upstream = [release('1.0.0'), release('1.1.0'), release('2.0.0'), release('stable')]
        self.sent = []
        self.messages = {}
        self.release_calls = []

    def releases(self, target, software):
        self.release_calls.append(software['id'])
        if isinstance(self.upstream, Exception):
            raise self.upstream
        return deepcopy(self.upstream)

    def message(self, target, message_id):
        return self.messages.get(message_id)

    def send_report(self, target, report):
        self.sent.append(deepcopy(report))


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Test'], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 't@localhost'], check=True)
    config = {k: str(repo / (k + '.json')) for k in ('watchlist', 'needs', 'sources')}
    config.update(enabled=True, timezone='Asia/Singapore', hour=9, weekday=0, data_repo=str(repo),
                  state_dir=str(tmp_path/'state'), reports=str(tmp_path/'reports'), snapshots=str(repo/'snapshots'),
                  directions=[{'id': 'project', 'path': str(repo/'pm.md')}])
    (repo/'pm.md').write_text('让 agent 操作 GitLab')
    write(config['watchlist'], {'schema': SCHEMA, 'software': [software()], 'excluded': {}})
    write(config['needs'], {'needs': [{'id': 'need', 'owner': 'Kun', 'source': 'session:1',
                                     'statement': '想让 agent 操作 GitLab', 'state': 'open', 'confirmed': False}]})
    write(config['sources'], {'sources': [{'id': 'server', 'where': 'dc1,dc2'}]})
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'initial'], check=True)
    io = FakeIO(tmp_path)
    return io, {'name': 'alpha', 'watch_updates': config}, Flow(io), Source(io)


def item_for(event):
    return {'id': 'alpha:watch_updates:' + event.object_key, 'target': 'alpha', 'object_key': event.object_key,
            'stage': '接单', 'materials': [{'source': event.source, 'ext_id': event.ext_id, 'body': event.body}],
            'result': {'outcome': 'done', 'summary': '程序'}}


def advance(flow, item, target, stage, payload=None):
    item['stage'] = stage
    receipt = Receipt('done', 'test', evidence=[json.dumps({'schema': SCHEMA, **payload})] if payload else [])
    item['result'] = asdict(receipt)
    evidence = flow.assess(item, receipt, target)
    assert evidence.passed, evidence.reason or evidence.blocked
    return flow.after(item, stage, stage, target)


def judged(world, urgent=False):
    io, target, flow, source = world
    poll = source.poll(target, None)
    item = item_for(next(e for e in poll.events if e.body['kind'] == 'version'))
    advance(flow, item, target, '接单')
    advance(flow, item, target, '说清楚')
    assert not flow.automatic(item, '执行', target)
    advance(flow, item, target, '执行', {'judgment': judgment(urgent)})
    return item, poll


def weekly(world, poll):
    io, target, flow, source = world
    item = item_for(next(e for e in poll.events if e.body['kind'] == 'reconcile'))
    advance(flow, item, target, '接单')
    proposal = {'sources': [{'id': 'server', 'ok': True, 'evidence': 'read containers'}],
                'software': [software()], 'removed': [], 'needs': []}
    advance(flow, item, target, '说清楚', {'inventory': proposal})
    advance(flow, item, target, '执行')
    advance(flow, item, target, '交付')
    return item


def test_semver_filters_and_full_range():
    rows = [release('stable'), release('beta'), release('2.0.0'), release('1.1.0'),
            release('1.2.0-rc.2', prerelease=True), release('1.0.0')]
    s = software()
    assert [r['tag_name'] for r in release_range(s, rows)] == ['1.1.0', '2.0.0']
    s['prerelease'] = True
    assert [r['tag_name'] for r in release_range(s, rows)] == ['1.1.0', '1.2.0-rc.2', '2.0.0']
    assert version('1.2.0-rc.10') > version('1.2.0-rc.2')
    assert version('1.2.0') > version('1.2.0-rc.10')
    assert version('n8n@2.0.0') == version('v2.0.0')


def test_reconcile_scope_uninstalled_idle_blind_and_excluded():
    old = {'software': [software('idle'), software('gone')], 'excluded': {'excluded': 'Kun 不用盯'}}
    proposed = {'sources': [{'id': 'server', 'ok': True, 'evidence': 'read'}],
                'software': [software('new'), software('auto', auto_updates=True), software('image', business_image=True),
                             software('lock', lock_dependency=True), software('bundled', bundled_with='n8n'),
                             software('shared'), software('excluded')],
                'removed': [{'id': 'gone', 'source': 'server', 'uninstalled': True, 'evidence': 'removed'}]}
    updated, changes = reconcile(old, proposed, 'now')
    assert {s['id'] for s in updated['software']} == {'idle', 'new', 'shared'}
    proposed['sources'][0]['ok'] = False
    with pytest.raises(ValueError, match='移出'):
        reconcile(old, proposed, 'now')
    proposed['removed'] = []
    retained = reconcile(old, proposed, 'now')[0]['software']
    for before, after in zip(old['software'], retained):
        for a, b in zip(before['instances'], after['instances']):
            assert all(a[k] == b[k] for k in ('version', 'read_at', 'evidence'))
            assert b['last_attempt'] == {'at': 'now', 'ok': False, 'error': 'read'}


def test_source_once_per_version_no_version_command_and_failure_dedup(world):
    io, target, flow, source = world
    first = source.poll(target, None)
    assert len(first.events) == 2
    assert source.poll(target, first.cursor).events == []
    io.time += timedelta(days=1)
    second = source.poll(target, first.cursor)
    assert second.events == []
    io.upstream.append(release('3.0.0'))
    io.time += timedelta(days=1)
    third = source.poll(target, second.cursor)
    event = third.events[0]
    assert [r['tag_name'] for r in event.body['releases']] == ['1.1.0', '2.0.0', '3.0.0']
    io.upstream = ValueError('upstream unavailable')
    io.time += timedelta(days=1)
    failed = source.poll(target, third.cursor)
    assert len(failed.events) == 1 and failed.events[0].body['kind'] == 'failure'
    io.time += timedelta(days=1)
    assert source.poll(target, failed.cursor).events == []
    item = item_for(failed.events[0])
    assert flow.assess(item, Receipt('done', ''), target).blocked == 'upstream unavailable'


def test_daily_only_urgent_weekly_empty_and_permissions(world):
    io, target, flow, source = world
    item, poll = judged(world)
    advance(flow, item, target, '交付')
    assert io.sent == []
    assert flow.brief(item, '执行', target).readonly is True
    assert Path(flow.brief(item, '执行', target).workdir).is_dir()
    pending = advance(flow, item, target, '验收')
    assert pending.pending and '等反馈' in pending.next
    week = weekly(world, poll)
    assert len(io.sent) == 1 and io.sent[0]['kind'] == 'weekly'
    assert '不用升' in io.sent[0]['summary']
    assert flow.brief(week, '说清楚', target).readonly is False
    assert Path(flow.brief(week, '说清楚', target).workdir).is_dir()
    assert Path(flow.brief(week, '说清楚', target).workdir).is_relative_to(Path(target['watch_updates']['state_dir']))
    assert not Path(flow.brief(week, '说清楚', target).workdir).is_relative_to(Path(target['watch_updates']['data_repo']))
    advance(flow, week, target, '交付')
    assert len(io.sent) == 1
    html = Path(io.sent[0]['path']).read_text()
    assert 'first.example' in html and 'second.example' in html
    assert '复制反馈' in html and '其余全部没问题' not in html


def send_feedback(world, item, report, rows, mid='om_feedback', needs=None):
    io, target, flow, source = world
    selected = []
    for row in rows:
        for identity, target_row in report['feedback_targets'].items():
            if target_row['kind'] == 'instances' and row.get('decision') and {'key': row['key'], 'instance': row['instance']} in target_row['members']:
                selected.append({'id': identity, 'choice': row['decision'], 'note': row.get('note', ''),
                                 **({'scope': row['scope']} if 'scope' in row else {})})
            if target_row['kind'] == 'feature' and target_row['key'] == row['key'] and target_row['instance'] == row['instance']:
                selected.extend({'id': identity, 'choice': f['choice']} for f in row.get('features', []) if f['id'] == target_row['feature'])
    selected.extend({'id': 'need-' + n['id'], 'choice': n['choice']} for n in needs or [])
    text = json.dumps({'report': report['id'], 'items': selected})
    io.messages[mid] = text
    event = Event('cli:feishu', mid, 'alpha', 'ignored', '反馈', {'text': 'forged', 'from_kun': True})
    classified = flow.classify(event, item, target, lambda **kwargs: {'item': item})
    assert classified.action == 'rework'
    item['materials'].append({'source': event.source, 'ext_id': mid, 'body': event.body})
    advance(flow, item, target, '执行')


def test_partial_upgrade_and_feature_feedback_do_not_auto_approve(world):
    io, target, flow, source = world
    item, poll = judged(world, urgent=True)
    advance(flow, item, target, '交付')
    assert len(io.sent) == 1 and io.sent[0]['kind'] == 'daily'
    key = item['object_key']
    send_feedback(world, item, io.sent[0], [{'key': key, 'instance': 'one', 'decision': '',
                                          'features': [{'id': 'mcp', 'choice': '想用'}]}])
    assert advance(flow, item, target, '验收').next.startswith('等反馈')
    assert any(n['source'].startswith('报告反馈') for n in io.needs(target))
    send_feedback(world, item, io.sent[0], [{'key': key, 'instance': 'one', 'decision': '升'}], 'om_upgrade')
    after = advance(flow, item, target, '验收')
    assert after.pending and after.next == '待升级：one'
    saved = io.state(target)['versions'][key]
    assert 'om_upgrade' == saved['feedback']['one']['message_id']
    assert saved['feedback']['one']['quote'] == io.messages['om_upgrade']


def test_correction_rejudges_and_suppression_persists_no_command_execution(world, tmp_path):
    io, target, flow, source = world
    item, poll = judged(world, urgent=True)
    advance(flow, item, target, '交付')
    command = 'touch ' + str(tmp_path/'should-not-exist')
    send_feedback(world, item, io.sent[0], [{'key': item['object_key'], 'instance': 'one',
                                          'decision': '判错了', 'note': command}])
    assert not flow.automatic(item, '执行', target)
    assert command in io.watchlist(target)['software'][0]['notes']
    advance(flow, item, target, '执行', {'judgment': judgment()})
    assert io.state(target)['versions'][item['object_key']]['needs_judgment'] is False
    assert not (tmp_path/'should-not-exist').exists()
    send_feedback(world, item, io.sent[0], [{'key': item['object_key'], 'instance': 'one', 'decision': '不用盯',
                                          'scope': 'software'}], 'om_drop')
    assert io.watchlist(target)['software'] == []
    assert 'n8n' in io.watchlist(target)['excluded']
    assert not advance(flow, item, target, '验收').pending


def test_untrusted_message_and_unrelated_report_are_rejected(world):
    io, target, flow, source = world
    event = Event('cli:feishu', 'om_missing', 'alpha', 'invented', 'x', {'from_kun': True})
    assert flow.classify(event, None, target, lambda **kwargs: None).discard
    io.messages['om_missing'] = '{"schema":"watch-feedback/v1","report":"invented"}'
    assert flow.classify(event, None, target, lambda **kwargs: None).discard


def test_weekly_waits_for_same_batch_judgment(world):
    io, target, flow, source = world
    poll = source.poll(target, None)
    weekly(world, poll)
    assert not io.sent
    event = next(e for e in poll.events if e.body['kind'] == 'version')
    item = item_for(event)
    advance(flow, item, target, '接单')
    advance(flow, item, target, '执行', {'judgment': judgment()})
    week = item_for(next(e for e in poll.events if e.body['kind'] == 'reconcile'))
    assert not advance(flow, week, target, '交付').pending
    assert len(io.sent) == 1 and item['object_key'] in io.sent[0]['versions']


def test_old_report_features_survive_rejudgment(world):
    io, target, flow, source = world
    item, poll = judged(world, urgent=True)
    advance(flow, item, target, '交付')
    old_report = io.sent[0]
    send_feedback(world, item, old_report, [{'key': item['object_key'], 'instance': 'one', 'decision': '判错了'}])
    changed = judgment()
    for row in changed['instances']:
        row['features'] = []
    advance(flow, item, target, '执行', {'judgment': changed})
    send_feedback(world, item, old_report, [{'key': item['object_key'], 'instance': 'one', 'decision': '',
                  'features': [{'id': 'mcp', 'choice': '想用'}]}], mid='om_late')
    assert any(n['statement'] == 'agent 操作 GitLab' and 'om_late' in n['source'] for n in io.needs(target))


def test_validation_requires_matching_need_and_actual_vulnerability_exposure(world):
    io, target, flow, source = world
    s = software()
    j = judgment(True)
    s['profile']['exposed'] = False
    with pytest.raises(ValueError, match='紧急'):
        validate_judgment(j, s, io.upstream, io.needs(target), io.directions(target))
    s['profile']['exposed'] = True
    j['instances'][0]['references'] = ['invented']
    with pytest.raises(ValueError, match='references'):
        validate_judgment(j, s, io.upstream, io.needs(target), io.directions(target))


def test_report_escapes_external_text_and_links(world):
    io, target, flow, source = world
    item, poll = judged(world, urgent=True)
    state = io.state(target)
    state['versions'][item['object_key']]['software']['name'] = '<script>alert(1)</script>'
    io.save(target, state)
    advance(flow, item, target, '交付')
    html = Path(io.sent[0]['path']).read_text()
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html


def test_weekly_wake_keeps_pending_without_new_judgment(world):
    io, target, flow, source = world
    item, poll = judged(world)
    advance(flow, item, target, '交付')
    advance(flow, item, target, '验收')
    io.time += timedelta(days=7)
    again = source.poll(target, poll.cursor)
    assert any(e.body['kind'] == 'wake' and e.object_key == item['object_key'] for e in again.events)
    assert flow.automatic(item, '执行', target)


def test_old_blocked_judgment_does_not_hold_new_daily_batch(world):
    io, target, flow, source = world
    first = source.poll(target, None)
    old = item_for(next(e for e in first.events if e.body['kind'] == 'version'))
    advance(flow, old, target, '接单')  # 上次没有交回判断。
    watchlist = io.watchlist(target)
    watchlist['software'].append(software('new'))
    write(io.file(target, 'watchlist'), watchlist)
    io.time += timedelta(days=1)
    second = source.poll(target, first.cursor)
    event = next(e for e in second.events if e.body['kind'] == 'version')
    assert event.body['batch'] == [event.object_key]
    item = item_for(event)
    advance(flow, item, target, '接单')
    advance(flow, item, target, '执行', {'judgment': judgment(True)})
    assert not advance(flow, item, target, '交付').pending
    assert io.sent[0]['versions'] == [item['object_key']]


def test_deadline_reports_completed_urgent_then_supplements_late_urgent(world):
    io, target, flow, source = world
    watchlist = io.watchlist(target)
    watchlist['software'].append(software('slow'))
    write(io.file(target, 'watchlist'), watchlist)
    poll = source.poll(target, None)
    items = [item_for(e) for e in poll.events if e.body['kind'] == 'version']
    for item in items:
        advance(flow, item, target, '接单')
    advance(flow, items[0], target, '执行', {'judgment': judgment(True)})
    assert advance(flow, items[0], target, '交付').pending
    io.time += timedelta(minutes=61)
    assert not advance(flow, items[0], target, '交付').pending
    assert io.sent[0]['blind'] == [{'judgment_pending': items[1]['object_key']}]
    advance(flow, items[1], target, '执行', {'judgment': judgment(True)})
    assert not advance(flow, items[1], target, '交付').pending
    assert len(io.sent) == 2 and io.sent[0]['id'] != io.sent[1]['id']
    assert io.sent[1]['versions'] == [items[1]['object_key']]


def test_data_check_command_is_runnable_from_reader_directory(world):
    io, target, flow, source = world
    item, poll = judged(world)
    directory = flow.brief(item, '执行', target).workdir
    result = subprocess.run(flow.checks(target)[0], cwd=directory, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '1 个软件' in result.stdout


def test_unknown_version_snapshot_and_git_commit(world):
    io, target, flow, source = world
    poll = source.poll(target, None)
    item = item_for(next(e for e in poll.events if e.body['kind'] == 'reconcile'))
    row = software()
    row['instances'][0].update(version='unknown', error='version command exit 1')
    proposal = {'sources': [{'id': 'server', 'ok': True, 'evidence': 'read'}], 'software': [row], 'needs': []}
    advance(flow, item, target, '说清楚', {'inventory': proposal})
    state = io.state(target)
    assert state['weeks'][item['object_key']]['commit']
    after = source.poll(target, poll.cursor)
    assert after.events == []  # 单处版本不明不再另开失败活，周报列盲区。
    advance(flow, item, target, '执行')
    io.time += timedelta(minutes=61)
    advance(flow, item, target, '交付')
    assert 'version command exit 1' not in io.sent[0]['summary']
    assert '处沿用上次版本' in Path(io.sent[0]['path']).read_text()
    assert io.watchlist(target)['software'][0]['instances'][0]['version'] == '1.0.0'
    assert source.poll(target, after.cursor).events == []


def test_report_delivery_journal_survives_partial_failure(world):
    io, target, flow, source = world
    real = IO(io.clock)
    with pytest.raises(ValueError, match='缺介入点服务'):
        real.send_report(target, {'id': 'weekly', 'path': '/unused'})



def test_unknown_deployment_does_not_block_known_deployment_or_invent_judgment(world):
    io, target, flow, source = world
    row = software()
    row['instances'][0].update(version='unknown', error='ssh 连不上')
    write(io.file(target, 'watchlist'), {'schema': SCHEMA, 'software': [row]})
    assert [r['tag_name'] for r in release_range(row, io.upstream)] == ['2.0.0']
    item, poll = judged(world, urgent=True)
    assert not any(e.body['kind'] == 'failure' for e in poll.events)
    saved = io.state(target)['versions'][item['object_key']]['judgment']['instances']
    unknown = next(r for r in saved if r['id'] == 'one')
    assert unknown['verdict'] == '版本不明' and unknown['reason'] == 'ssh 连不上'
    assert unknown['features'] == []  # 即使模型给这处「紧急该升」，也不能猜。
    advance(flow, item, target, '交付')
    report = io.sent[0]
    assert 'n8n · 1.1.0→2.0.0 · 该升 · 2 个实例' in report['summary']
    assert 'ssh 连不上' not in report['summary']
    html = Path(report['path']).read_text()
    assert 'ssh 连不上' not in html and '版本不明' in html and 'second.example' in html
    known_only = {'instances': [judgment()['instances'][1]]}
    assert len(validate_judgment(known_only, row, io.upstream, io.needs(target), io.directions(target))['instances']) == 2


def test_all_unknown_deployments_fail_once_without_guessing(world):
    io, target, flow, source = world
    row = software()
    for place in row['instances']:
        place.update(version='unknown', error='ssh 连不上')
    write(io.file(target, 'watchlist'), {'schema': SCHEMA, 'software': [row]})
    first = source.poll(target, None)
    failures = [e for e in first.events if e.body['kind'] == 'failure']
    assert len(failures) == 1 and '所有部署版本不明' in failures[0].body['error']
    assert not any(e.body['kind'] == 'version' for e in first.events)
    io.time += timedelta(days=1)
    assert source.poll(target, first.cursor).events == []


@pytest.mark.parametrize('unknown', [False, True])
def test_manual_upstream_skips_daily_fetch_and_appears_in_empty_weekly(world, unknown):
    io, target, flow, source = world
    row = software('DSM', upstream={'kind': 'manual', 'reason': '没有公开版本查询接口'})
    row.pop('tag_pattern')
    row.pop('prerelease')
    row['instances'][0]['version'] = 'DSM 7.3-81180'
    if unknown:
        for place in row['instances']:
            place.update(version='unknown', error='未取得版本')
    validate_software(row)
    write(io.file(target, 'watchlist'), {'schema': SCHEMA, 'software': [row]})
    io.upstream = AssertionError('manual 不应调用任何上游')
    poll = source.poll(target, None)
    assert [e.body['kind'] for e in poll.events] == ['reconcile'] and io.release_calls == []
    item = item_for(poll.events[0])
    proposal = {'sources': [{'id': 'server', 'ok': True, 'evidence': 'read'}], 'software': [row], 'needs': []}
    advance(flow, item, target, '说清楚', {'inventory': proposal})
    advance(flow, item, target, '执行')
    advance(flow, item, target, '交付')
    assert io.sent[0]['versions'] == []
    assert any(b.get('kind') == 'manual' for b in io.sent[0]['blind'])
    assert io.sent[0]['summary'].endswith('盲区 ' + str(3) + ' 项')
    assert '没有版本接口' in Path(io.sent[0]['path']).read_text()
    io.time += timedelta(days=1)
    assert source.poll(target, poll.cursor).events == []
    assert io.release_calls == []


def test_github_tags_paginate_and_mark_missing_notes(monkeypatch):
    import workshop.plugins.watch_updates.io as module
    calls = []
    rows = [{'name': t} for t in ['1.1.0', '2.0.0', '3.0.0-rc.1', 'stable']]
    def command(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps([rows[:1], rows[1:]]), '')
    monkeypatch.setattr(module, 'run', command)
    row = software(upstream={'kind': 'github_tags', 'repo': 'example/n8n'})
    validate_software(row)
    releases = IO().releases({}, row)
    assert [r['tag_name'] for r in release_range(row, releases)] == ['1.1.0', '2.0.0']
    assert calls == [['gh', 'api', '--paginate', '--slurp', 'repos/example/n8n/tags?per_page=100']]
    assert releases[1]['html_url'] == 'https://github.com/example/n8n/tree/2.0.0'
    assert releases[1]['notes_missing'] and '没有发布说明' in releases[1]['body']


def test_github_releases_read_page_by_page_and_stop_at_installed(monkeypatch):
    import workshop.plugins.watch_updates.io as module
    calls = []
    # 发布按创建时间倒序；在用最低 1.0.0，第二页读到它就停，不翻第三页。
    pages = [[release(f'3.0.{i}') for i in range(module.PAGE)],
             [release('2.0.0'), release('1.1.0'), release('1.0.0')] + [release(f'0.9.{i}') for i in range(module.PAGE - 3)],
             [release('0.1.0')]]
    def command(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(pages[len(calls) - 1]), '')
    monkeypatch.setattr(module, 'run', command)
    row = software(upstream={'kind': 'github_releases', 'repo': 'example/n8n'})
    validate_software(row)
    releases = IO().releases({}, row)
    assert calls == [['gh', 'api', f'repos/example/n8n/releases?per_page={module.PAGE}&page={p}'] for p in (1, 2)]
    assert [r['tag_name'] for r in release_range(row, releases)][:3] == ['1.1.0', '2.0.0', '3.0.0']
    assert releases[0]['body'] == 'Release notes 3.0.0'
    # 首版字符串清单继续按 GitHub Releases 查；不满一页就是读完了。
    calls.clear()
    pages[0] = [release('1.2.0')]
    IO().releases({}, software())
    assert calls == [['gh', 'api', f'repos/example/n8n/releases?per_page={module.PAGE}&page=1']]


def test_gitlab_public_releases_paginate_encode_project_and_preserve_notes(monkeypatch):
    import io as streams
    import workshop.plugins.watch_updates.io as module
    calls = []
    def fetch(url, timeout):
        calls.append(url)
        page = len(calls)
        tag = '1.1.0' if page == 1 else '2.0.0'
        response = streams.BytesIO(json.dumps([{'tag_name': tag, 'description': '完整发布说明 ' + tag,
            '_links': {'self': 'https://gitlab.com/group/sub/repo/-/releases/' + tag}}]).encode())
        response.headers = {'X-Next-Page': '2' if page == 1 else ''}
        return response
    monkeypatch.setattr(module, 'urlopen', fetch)
    monkeypatch.setattr(module, 'run', lambda *a, **kw: pytest.fail('GitLab 不使用 gh 或其凭据'))
    row = software(upstream={'kind': 'gitlab_releases', 'project': 'group/sub/repo'})
    validate_software(row)
    rows = IO().releases({}, row)
    assert [r['tag_name'] for r in release_range(row, rows)] == ['1.1.0', '2.0.0']
    assert rows[1]['body'] == '完整发布说明 2.0.0'
    assert rows[1]['html_url'].endswith('/-/releases/2.0.0')
    assert calls == [f'https://gitlab.com/api/v4/projects/group%2Fsub%2Frepo/releases?per_page=100&page={p}' for p in (1, 2)]


def test_bad_upstream_is_rejected_and_gitlab_failure_is_deduplicated(world, monkeypatch):
    from urllib.error import URLError
    import workshop.plugins.watch_updates.io as module
    for upstream in ({'kind': 'shell', 'repo': 'x/y'}, {'kind': 'manual'},
                     {'kind': 'gitlab_releases', 'project': 'https://private.example/x'}):
        with pytest.raises(ValueError):
            validate_software(software(upstream=upstream))
    io, target, flow, source = world
    row = software(upstream={'kind': 'gitlab_releases', 'project': 'group/repo'})
    write(io.file(target, 'watchlist'), {'schema': SCHEMA, 'software': [row]})
    io.releases = IO.releases.__get__(io)
    def fail(*args, **kwargs):
        raise URLError('连接失败')
    monkeypatch.setattr(module, 'urlopen', fail)
    poll = source.poll(target, None)
    assert len([e for e in poll.events if e.body['kind'] == 'failure']) == 1
    io.time += timedelta(days=1)
    assert source.poll(target, poll.cursor).events == []


def test_feishu_gets_only_the_attachment_and_summary_stays_on_record(world):
    io, target, flow, source = world
    item, poll = judged(world, urgent=True)
    advance(flow, item, target, '交付')
    report = io.sent[0]
    lines = [line for line in report['summary'].splitlines() if line.startswith('n8n ·')]
    assert lines == ['n8n · 1.0.0 / 1.1.0→2.0.0 · 该升 · 2 个实例']
    assert '影响公开服务' not in report['summary']
    from test_intervention_business import Port
    from workshop.plugins.intervention.service import Service
    service = Service(Path(target['watch_updates']['state_dir']) / 'intervention', Path('rules/workshop.yaml'))
    calls = []
    service.transport = lambda config, action, record: (calls.append(record['payload']) or {'state': 'sent', 'external': 'om_sent'})
    real = IO(io.clock)
    real.send_report(target, report, lambda name: (Port(service), {}))
    assert len(calls) == 1 and calls[0]['artifact'] == report['path']
    assert 'text' not in calls[0] and 'summary' not in calls[0]


def test_real_engine_wait_feedback_rework_completion_and_replay(world, tmp_path):
    from conftest import build, configuration
    from workshop.reports.report import status, why
    io, target, flow, source = world
    config = configuration()
    config['targets']['alpha'] = {**target, 'sources': [], 'plugin': 'flow'}
    engine = build(tmp_path/'engine', config)
    engine.plugins.register('flow', 'flow', flow)
    engine.clock.now = io.time
    event = next(e for e in source.poll(target, None).events if e.body['kind'] == 'version')
    event = Event(**{**asdict(event), 'action': 'ready'})
    engine.ingest(event, ingress='source')
    try:
        for _ in range(10):
            engine.tick()
            if engine.active():
                break
        active = engine.active()
        assert len(active) == 1 and active[0]['stage'] == '执行', engine.log.all('items')
        row = active[0]
        engine.submit(row['token'], {'outcome': 'done', 'summary': '该升：修复影响本服务的漏洞', 'advance': '证据',
            'evidence': [json.dumps({'schema': SCHEMA, 'judgment': judgment(True)})]})
        for _ in range(10):
            engine.tick()
        item = engine.log.all('items')[0]
        assert item['state'] == '定时' and item['stage'] == '验收', item
        assert '等反馈' in status(engine.log)
        assert len(engine.log.all('rounds')) == 1
        report = io.sent[0]
        io.messages['om_engine'] = json.dumps({'report': report['id'],
            'items': [{'id': i, 'choice': '没问题'} for i in ('one','two')]})
        engine.ingest(Event('feishu', 'om_engine', 'alpha', 'feedback', 'report feedback', {}), ingress='external')
        for _ in range(14):
            engine.tick()
        item = engine.log.all('items')[0]
        assert item['state'] == '完成', item
        assert len(engine.log.all('rounds')) == 1  # 消费反馈不重新起模型。
        assert '该升' in why(engine.log, item['id'])
        assert len(io.sent) == 1
        before = deepcopy(item)
        engine.log.rebuild()
        assert engine.log.all('items')[0] == before
    finally:
        engine.log.close()


def test_tag_pattern_version_group_reads_prefixed_and_suffixed_tags():
    row = software(tag_pattern=r'docker-v(?P<version>\d+\.\d+\.\d+)')
    tags = ['docker-v1.0.5', 'docker-v1.2.0', 'docker-v1.3.0-rc.1', 'api/v9.0.0', 'docker-v0.9.0']
    assert [r['tag_name'] for r in release_range(row, [release(t) for t in tags])] == ['docker-v1.0.5', 'docker-v1.2.0']
    # 稳定版带固定后缀（GitLab 的 -ee）时，后缀不算预发布。
    row = software(tag_pattern=r'v(?P<version>\d+\.\d+\.\d+)-ee')
    assert [r['tag_name'] for r in release_range(row, [release(t) for t in ('v1.1.1-ee', 'v1.1.1-ce', 'v0.9.0-ee')])] == ['v1.1.1-ee']


def test_gitlab_tags_read_by_version_desc_and_stop_at_installed(monkeypatch):
    import io as streams
    import workshop.plugins.watch_updates.io as module
    calls = []
    pages = [[{'name': 'v2.0.0-ee', 'release': {'description': '说明 2.0.0'}}, {'name': 'v1.5.0-ee'}],
             [{'name': 'v1.0.0-ee'}, {'name': 'v0.9.0-ee'}],
             [{'name': 'v0.1.0-ee'}]]
    def fetch(url, timeout):
        calls.append(url)
        response = streams.BytesIO(json.dumps(pages[len(calls) - 1]).encode())
        response.headers = {'X-Next-Page': str(len(calls) + 1)}
        return response
    monkeypatch.setattr(module, 'urlopen', fetch)
    monkeypatch.setattr(module, 'run', lambda *a, **kw: pytest.fail('GitLab 不使用 gh 或其凭据'))
    row = software(upstream={'kind': 'gitlab_tags', 'project': 'gitlab-org/gitlab'},
                   tag_pattern=r'v(?P<version>\d+\.\d+\.\d+)-ee')
    validate_software(row)
    rows = IO().releases({}, row)
    # 在用最低 1.0.0，第二页读到它就停，不再翻第三页。
    assert calls == [('https://gitlab.com/api/v4/projects/gitlab-org%2Fgitlab/repository/tags'
                      f'?order_by=version&sort=desc&per_page=100&page={p}') for p in (1, 2)]
    assert [r['tag_name'] for r in release_range(row, rows)] == ['v1.5.0-ee', 'v2.0.0-ee']
    assert rows[0]['body'] == '说明 2.0.0' and not rows[0]['notes_missing']
    assert rows[1]['notes_missing'] and '没有发布说明' in rows[1]['body']
    assert rows[1]['html_url'] == 'https://gitlab.com/gitlab-org/gitlab/-/tags/v1.5.0-ee'


def test_release_notes_go_to_files_and_workdirs_are_git_repos(world):
    io, target, flow, source = world
    io.upstream = [release('1.1.0', author={'login': 'x' * 1000}, assets=[{'url': 'y' * 1000}]),
                   release('2.0.0', body='安全修复 CVE-2026-1 ' * 1000)]
    poll = source.poll(target, None)
    event = next(e for e in poll.events if e.body['kind'] == 'version')
    # 事件只带版本、链接和正文长度，不带正文和 GitHub 元数据。
    assert event.body['releases'] == [
        {'tag_name': '1.1.0', 'html_url': 'https://example.com/1.1.0', 'prerelease': False, 'draft': False, 'notes_chars': len('Release notes 1.1.0')},
        {'tag_name': '2.0.0', 'html_url': 'https://example.com/2.0.0', 'prerelease': False, 'draft': False,
         'notes_chars': len('安全修复 CVE-2026-1 ' * 1000)}]
    files = sorted(Path(event.body['notes']).iterdir())
    assert [f.name for f in files] == ['0001-1.1.0.md', '0002-2.0.0.md']
    assert 'https://example.com/2.0.0' in files[1].read_text() and 'CVE-2026-1' in files[1].read_text()
    item = item_for(event)
    advance(flow, item, target, '接单')
    advance(flow, item, target, '说清楚')
    brief = flow.brief(item, '执行', target)
    assert 'notes' in brief.instructions and 'CVE-2026-1' not in json.dumps(brief.materials, ensure_ascii=False)
    week = item_for(next(e for e in poll.events if e.body['kind'] == 'reconcile'))
    for directory in (brief.workdir, flow.brief(week, '说清楚', target).workdir):
        assert (Path(directory) / '.git').is_dir()
