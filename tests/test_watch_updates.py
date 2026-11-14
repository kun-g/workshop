from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import pytest

from workshop.core.models import Event, Receipt
from workshop.plugins.watch_updates.flow import Flow, feedback
from workshop.plugins.watch_updates.io import IO, read, write
from workshop.plugins.watch_updates.model import SCHEMA, reconcile, release_range, validate_judgment, version
from workshop.plugins.watch_updates.report import render
from workshop.plugins.watch_updates.source import Source


def software(name='n8n', **extra):
    return {'id': name, 'name': name, 'kind': 'infra', 'upstream': 'example/' + name,
            'source': 'server', 'tag_pattern': r'(?:v|n8n@)?\d+\.\d+\.\d+(?:-rc\.\d+)?',
            'prerelease': False, 'operated_by_us': True, 'evidence': 'running service',
            'profile': {'exposed': True, 'data': 'workflows', 'users': ['Kun'], 'paired': []},
            'instances': [{'id': 'one', 'host': 'dc1', 'service': 'first.example', 'version': '1.0.0',
                           'read_at': '2026-11-14T01:00:00+00:00', 'evidence': 'version output 1.0.0'},
                          {'id': 'two', 'host': 'dc2', 'service': 'second.example', 'version': '1.1.0',
                           'read_at': '2026-11-14T01:00:00+00:00', 'evidence': 'version output 1.1.0'}],
            'notes': [], **extra}


def release(tag, **extra):
    return {'tag_name': tag, 'body': 'Release notes ' + tag, 'html_url': 'https://example.com/' + tag,
            'prerelease': False, 'draft': False, **extra}


def judgment(urgent=False):
    return {'instances': [{'id': p, 'verdict': '该升' if urgent else '不用升',
             'priority': '紧急' if urgent else '普通', 'reason': '影响公开服务' if urgent else '没有相关修复',
             'summary': '从在用版本起的要点', 'references': ['profile'], 'links': ['https://example.com/2.0.0'],
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
    assert reconcile(old, proposed, 'now')[0]['software'] == old['software']


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
    assert '没有该升' in io.sent[0]['summary']
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
    text = json.dumps({'schema': 'watch-feedback/v1', 'report': report['id'], 'items': rows, 'needs': needs or []})
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
    send_feedback(world, item, io.sent[0], [{'key': item['object_key'], 'instance': 'one', 'decision': '不用盯'}], 'om_drop')
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
    with pytest.raises(ValueError, match='须引用'):
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
    assert len(after.events) == 1 and after.events[0].body['error'] == 'version command exit 1'
    assert source.poll(target, after.cursor).events == []


def test_report_delivery_journal_survives_partial_failure(world):
    io, target, flow, source = world
    c = target['watch_updates']
    c.update(lark='lark-cli', profile='test', kun_open_id='test')
    real = IO(io.clock)
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        fail = '--file' in argv and len(calls) == 2
        return subprocess.CompletedProcess(argv, 1 if fail else 0, json.dumps({'ok': not fail, 'data': {'message_id': 'om_sent'}}), '')
    real.command = command
    report = {'id': 'weekly', 'path': str(Path(c['reports'])/'weekly.html'), 'summary': 'summary'}
    with pytest.raises(ValueError, match='发送未确认'):
        real.send_report(target, report)
    real.send_report(target, report)
    assert sum('--text' in a for a in calls) == 1
    assert sum('--file' in a for a in calls) == 2
    real.send_report(target, report)
    assert len(calls) == 3


def test_real_engine_wait_feedback_rework_completion_and_replay(world, tmp_path):
    from conftest import build, configuration
    from workshop.core.report import status, why
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
        io.messages['om_engine'] = json.dumps({'schema': 'watch-feedback/v1', 'report': report['id'],
            'items': [{'key': event.object_key, 'instance': i, 'decision': '没问题'} for i in ('one','two')]})
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
