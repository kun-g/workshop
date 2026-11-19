"""「不用盯」按选的范围登记（盯更新 R15、R17）：报告页、复制反馈、校验、移出与对账不加回。"""
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from test_watch_updates import advance, item_for, release, send_feedback, software, world  # noqa: F401
from workshop.core.models import Event
from workshop.plugins.watch_updates.feedback import feedback
from workshop.plugins.watch_updates.io import read, write
from workshop.plugins.watch_updates.model import SCHEMA
from workshop.plugins.watch_updates.presentation import targets
from workshop.plugins.watch_updates.report import render

NODE = shutil.which('node') or next((p for p in ('/opt/homebrew/bin/node', '/usr/local/bin/node') if Path(p).exists()), None)
SCRIPT = Path(__file__).parents[1] / 'src/workshop/plugins/watch_updates/report.js'
FAKE_DOM = Path(__file__).parent / 'fake_dom.js'


def place(id, host, **extra):
    return {'id': id, 'host': host, 'service': id + '.svc', 'version': '1.0.0', 'role': 'server',
            'read_at': '2026-11-14T01:00:00+00:00', 'evidence': 'version output', **extra}


def fleet():
    """n8n 三处（两处在 dc1）、redis 两处（dc1 和 k8s m1）、vector 只在 k8s m1。"""
    n8n = software('n8n')
    n8n['instances'] = [place('one', 'dc1'), place('three', 'dc1'), place('two', 'dc2')]
    redis = software('redis')
    redis['instances'] = [place('redis-dc1', 'dc1'), place('redis-m1', 'k8s m1 / cache')]
    vector = software('vector')
    vector['instances'] = [place('vector-m1', 'k8s m1 / logging')]
    return [n8n, redis, vector]


def judgment_for(soft):
    return {'instances': [{'id': p['id'], 'verdict': '该升', 'priority': '紧急', 'headline': '影响公开服务',
             'points': [{'version': '2.0.0', 'text': '修复影响公开服务的漏洞'}], 'references': ['profile'],
             'links': ['https://example.com/2.0.0'], 'signals': {'high_risk_vulnerability': True}, 'features': []}
            for p in soft['instances'] if p['role'] == 'server']}


def reported(world, softwares=None):
    """真实流程跑到日报发出：每个软件判成紧急该升，返回 {软件 id: 活} 和报告。"""
    io, target, flow, source = world
    softwares = softwares or fleet()
    write(io.file(target, 'watchlist'), {'schema': SCHEMA, 'software': softwares, 'excluded': {}})
    poll = source.poll(target, None)
    items = {}
    for event in poll.events:
        if event.body['kind'] != 'version':
            continue
        item = item_for(event)
        advance(flow, item, target, '接单')
        advance(flow, item, target, '说清楚')
        advance(flow, item, target, '执行', {'judgment': judgment_for(event.body['software'])})
        items[event.body['software']['id']] = item
    for item in items.values():
        advance(flow, item, target, '交付')
    report = io.state(target)['reports'][io.sent[-1]['id']]
    return items, report, poll


def instances(io, target):
    return {s['id']: [p['id'] for p in s['instances']] for s in io.watchlist(target)['software']}


def submit(world, item, report, items, mid):
    io, target, flow, _ = world
    io.messages[mid] = json.dumps({'report': report['id'], 'items': items})
    event = Event('cli:feishu', mid, 'alpha', 'ignored', '反馈', {})
    classified = flow.classify(event, item, target, lambda **kwargs: {'item': item})
    if classified.discard:
        return classified
    item['materials'].append({'source': event.source, 'ext_id': mid, 'body': event.body})
    advance(flow, item, target, '执行')
    return classified


def row_id(report, software_id, instance=None, role='server'):
    return next(k for k, t in report['feedback_targets'].items() if t['kind'] == 'instances'
                and t['software'] == software_id and t['role'] == role
                and (instance is None or t.get('place', {}).get('id') == instance))


def reconcile_once(world, poll, softwares):
    """对账提案原样报回全部部署，看被排除的部署会不会加回来。"""
    io, target, flow, _ = world
    item = item_for(next(e for e in poll.events if e.body['kind'] == 'reconcile'))
    advance(flow, item, target, '接单')
    proposal = {'sources': [{'id': 'server', 'ok': True, 'evidence': 'read containers'}],
                'software': softwares, 'removed': [], 'needs': []}
    advance(flow, item, target, '说清楚', {'inventory': proposal})


class Tree(HTMLParser):
    VOID = {'input', 'meta', 'link', 'br', 'img'}

    def __init__(self):
        super().__init__()
        self.root = {'tag': '#document', 'attrs': {}, 'children': []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {'tag': tag, 'attrs': {k: v or '' for k, v in attrs}, 'children': []}
        self.stack[-1]['children'].append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack[-1]['tag'] == tag:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack[-1]['tag'] not in ('script', 'style'):
            self.stack[-1]['children'].append({'tag': '#text', 'attrs': {}, 'text': data, 'children': []})


def run_page(html, steps, tmp_path):
    """在最小 DOM 里跑报告页自己的 report.js，按步骤点按钮、看哪些输入露出来、复制反馈。"""
    if NODE is None:
        pytest.skip('本机没有 node')
    parser = Tree()
    parser.feed(html)
    (tmp_path / 'tree.json').write_text(json.dumps(parser.root, ensure_ascii=False))
    (tmp_path / 'steps.json').write_text(json.dumps(steps, ensure_ascii=False))
    result = subprocess.run([NODE, str(FAKE_DOM), str(tmp_path / 'tree.json'), str(SCRIPT), str(tmp_path / 'steps.json')],
                            capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def synthetic_report(softwares):
    versions = {}
    for soft in softwares:
        from workshop.plugins.watch_updates.model import validate_judgment
        releases = [release('1.0.0'), release('2.0.0')]
        versions[soft['id'] + '-2.0.0'] = {'software': soft, 'releases': releases,
                                           'judgment': validate_judgment(judgment_for(soft), soft, releases, [], [])}
    report = {'id': 'wu-daily-fixture-v2', 'kind': 'daily', 'anchor': 'fixture', 'versions': list(versions),
              'snapshot': versions, 'needs': [], 'blind': [], 'changes': []}
    report['feedback_targets'] = targets(report, versions, [])
    return report, versions


def agent_software():
    soft = software('netbird')
    soft['instances'] = [place('mgmt', 'dc2'),
                         place('mac-agent', 'Mac / brew netbird', role='agent'),
                         place('dc1-agent', 'dc1', role='agent'),
                         place('m1-agent', 'k8s m1 / netbird', role='agent'),
                         place('sc-agent', 'k8s sc-prod', role='agent'),
                         place('m1-agent-2', 'k8s m1 / sre', role='agent')]
    return soft


def test_scope_options_only_list_what_the_row_touches():
    report, _ = synthetic_report([fleet()[1], agent_software()])
    registry = report['feedback_targets']
    options = lambda key: [(o['id'], o['label']) for o in registry[key]['scopes']]
    # 单处部署：机器上的部署列机器，不列集群；集群里的部署列集群，不把「k8s m1 / cache」当机器。
    assert options('redis-dc1') == [('here', '只这一处'), ('software', '整个 redis'), ('host:dc1', '机器 dc1')]
    assert options('redis-m1') == [('here', '只这一处'), ('software', '整个 redis'), ('cluster:m1', '集群 m1')]
    assert registry['redis-dc1']['scopes'][0]['instances'] == ['redis-dc1']
    # agent 合成行：涉及几台机器列几台、几个集群列几个，「只这一处」展开到组里每一处。
    assert options('netbird-agents') == [('here', '只这一处'), ('software', '整个 netbird'),
                                         ('host:Mac', '机器 Mac'), ('host:dc1', '机器 dc1'),
                                         ('cluster:m1', '集群 m1'), ('cluster:sc-prod', '集群 sc-prod')]
    assert registry['netbird-agents']['scopes'][0]['instances'] == ['mac-agent', 'dc1-agent', 'm1-agent', 'sc-agent', 'm1-agent-2']
    assert options('mgmt') == [('here', '只这一处'), ('software', '整个 netbird'), ('host:dc2', '机器 dc2')]


def test_page_shows_scope_only_for_drop_and_copies_scope(tmp_path):
    report, versions = synthetic_report([fleet()[1], agent_software()])
    html = render(report, versions, [], datetime(2026, 11, 16, tzinfo=timezone.utc))
    # 每个部署行一份，默认收起：redis 两处、netbird 服务端一处、agent 合成一行。
    assert html.count('class="drop" data-for="不用盯" hidden') == html.count('data-feedback=') == 4
    snapshots = run_page(html, [
        ['snapshot', 'redis-dc1'],
        ['click', 'redis-dc1', '不用盯'], ['snapshot', 'redis-dc1'],
        *[step for choice in ('升', '没问题', '判错了') for step in (['click', 'redis-dc1', choice], ['snapshot', 'redis-dc1'])],
        ['click', 'redis-dc1', '不用盯'], ['type', 'redis-dc1', '.reason', '这台要下线'],
        ['click', 'netbird-agents', '不用盯'], ['radio', 'netbird-agents', 'cluster:m1'],
        ['click', 'redis-m1', '升'], ['type', 'redis-m1', '.note', '周末升'],
        ['click', 'mgmt', '没问题'],
        ['copy', None],
    ], tmp_path)
    closed, dropped, upgrade, fine, wrong, text = snapshots
    assert closed == {'drop': False, 'note': False, 'scopes': [['here', True], ['software', False], ['host:dc1', False]]}
    assert dropped == {'drop': True, 'note': False, 'scopes': [['here', True], ['software', False], ['host:dc1', False]]}
    assert upgrade == {'drop': False, 'note': True, 'scopes': dropped['scopes']}
    assert fine == {'drop': False, 'note': False, 'scopes': dropped['scopes']}
    assert wrong == {'drop': False, 'note': True, 'scopes': dropped['scopes']}
    lines = text.splitlines()
    assert '- redis · dc1 · redis-dc1.svc：不用盯（范围：只这一处；原因：这台要下线）' in lines
    assert '- netbird · agent（全部）：不用盯（范围：集群 m1）' in lines
    assert '- redis · k8s m1 / cache · redis-m1.svc：升（周末升）' in lines
    assert '- netbird · dc2 · mgmt.svc：没问题' in lines
    payload = json.loads(text.split('```json\n')[1].split('\n```')[0])
    assert payload['items'] == [
        {'id': 'redis-dc1', 'choice': '不用盯', 'note': '这台要下线', 'scope': 'here'},
        {'id': 'redis-m1', 'choice': '升', 'note': '周末升'},
        {'id': 'mgmt', 'choice': '没问题', 'note': ''},
        {'id': 'netbird-agents', 'choice': '不用盯', 'note': '', 'scope': 'cluster:m1'}]


@pytest.mark.parametrize('row', [
    {'id': 'redis-dc1', 'choice': '不用盯', 'scope': 'cluster:m1'},     # 另一行的选项
    {'id': 'redis-dc1', 'choice': '不用盯', 'scope': 'host:dc9'},       # 没登记的机器
    {'id': 'redis-dc1', 'choice': '不用盯', 'scope': {'kind': 'host'}},  # 不是选项编号
    {'id': 'redis-dc1', 'choice': '没问题', 'scope': 'here'},           # 别的选择不带范围
])
def test_scope_outside_snapshot_rejects_whole_feedback(world, row):
    io, target, flow, _ = world
    items, report, _ = reported(world)
    before = {k: Path(target['watch_updates'][k]).read_bytes() for k in ('watchlist', 'needs')}
    good = {'id': row_id(report, 'n8n', 'one'), 'choice': '不用盯', 'scope': 'software'}
    with pytest.raises(ValueError):
        feedback(json.dumps({'report': report['id'], 'items': [good, row]}), io.state(target))
    assert submit(world, items['redis'], report, [good, row], 'om_bad').discard
    assert all(Path(target['watch_updates'][k]).read_bytes() == v for k, v in before.items())
    assert io.watchlist(target)['excluded'] == {}


def test_only_here_keeps_other_deployments_and_reconcile_does_not_add_back(world):
    io, target, flow, source = world
    items, report, poll = reported(world)
    one = row_id(report, 'n8n', 'one')
    submit(world, items['n8n'], report, [{'id': one, 'choice': '不用盯', 'note': '', 'scope': 'here'}], 'om_here')
    assert instances(io, target)['n8n'] == ['three', 'two']  # 同一台 dc1 上的 three 留着
    assert instances(io, target)['redis'] == ['redis-dc1', 'redis-m1']
    rule = read(io.file(target, 'watchlist'))['excluded_scopes'][-1]
    assert rule == {'kind': 'instance', 'software': 'n8n', 'value': 'one', 'message_id': 'cli:feishu:om_here',
                    'report': report['id'], 'reason': f'Kun 在报告 {report["id"]} 选择不用盯'}
    state = io.state(target)
    current = state['versions'][items['n8n']['object_key']]
    assert [p['id'] for p in current['software']['instances']] == ['three', 'two']
    assert current['status'] != 'done'
    reconcile_once(world, poll, fleet())
    assert instances(io, target)['n8n'] == ['three', 'two']
    # 后面的周报照样问 n8n 剩下的两处。
    advance(flow, item_for(next(e for e in poll.events if e.body['kind'] == 'reconcile')), target, '交付')
    weekly = io.sent[-1]
    assert weekly['kind'] == 'weekly'
    asked = {t['place']['id'] for t in weekly['feedback_targets'].values()
             if t['kind'] == 'instances' and t['software'] == 'n8n'}
    assert asked == {'three', 'two'}
    # 剩下两处都回了「没问题」，这个版本收尾。
    submit(world, items['n8n'], weekly, [{'id': row_id(weekly, 'n8n', p), 'choice': '没问题'} for p in ('three', 'two')], 'om_ok')
    assert not advance(flow, items['n8n'], target, '验收').pending


@pytest.mark.parametrize('scope, gone, kept', [
    ('host:dc1', {'n8n': ['one', 'three'], 'redis': ['redis-dc1']}, {'n8n': ['two'], 'redis': ['redis-m1'], 'vector': ['vector-m1']}),
    ('cluster:m1', {'redis': ['redis-m1'], 'vector': ['vector-m1']}, {'n8n': ['one', 'three', 'two'], 'redis': ['redis-dc1']}),
])
def test_machine_or_cluster_registers_scope_and_survives_reconcile(world, scope, gone, kept):
    io, target, flow, _ = world
    items, report, poll = reported(world)
    row = row_id(report, 'redis', 'redis-dc1' if scope.startswith('host') else 'redis-m1')
    submit(world, items['redis'], report, [{'id': row, 'choice': '不用盯', 'note': '要退役', 'scope': scope}], 'om_scope')
    kind, value = scope.split(':')
    rules = read(io.file(target, 'watchlist'))['excluded_scopes']
    assert {'kind': kind, 'value': value, 'reason': '要退役', 'message_id': 'cli:feishu:om_scope', 'report': report['id']} in rules
    assert instances(io, target) == kept  # vector 在 m1 没有剩余部署，整个移出
    record = io.state(target)['feedback_records']['cli:feishu:om_scope']
    assert {(c['software'], c['instance']) for c in record['changes']} == {(s, p) for s, ps in gone.items() for p in ps}
    reconcile_once(world, poll, fleet())
    assert instances(io, target) == kept
    # 其他软件进行中的版本也不再问被移出的部署。
    state = io.state(target)
    for software_id, removed in gone.items():
        left = [p['id'] for p in state['versions'][items[software_id]['object_key']]['software']['instances']]
        assert not set(left) & set(removed)
    if 'vector' in gone:
        assert state['versions'][items['vector']['object_key']]['status'] == 'done'


def test_whole_software_and_last_deployment_leave_the_list(world):
    io, target, flow, _ = world
    items, report, poll = reported(world)
    submit(world, items['redis'], report, [
        {'id': row_id(report, 'redis', 'redis-dc1'), 'choice': '不用盯', 'scope': 'software'},
        {'id': row_id(report, 'vector', 'vector-m1'), 'choice': '不用盯', 'note': '只有一处'},
    ], 'om_whole')
    assert instances(io, target) == {'n8n': ['one', 'three', 'two']}
    excluded = io.watchlist(target)['excluded']
    assert excluded == {'redis': f'Kun 在报告 {report["id"]} 选择不用盯'}
    rules = read(io.file(target, 'watchlist'))['excluded_scopes']
    assert any(r['kind'] == 'instance' and r['value'] == 'vector-m1' and r['reason'] == '只有一处' for r in rules)
    reconcile_once(world, poll, fleet())
    assert instances(io, target) == {'n8n': ['one', 'three', 'two']}
    log = subprocess.run(['git', '-C', target['watch_updates']['data_repo'], 'log', '--format=%s'],
                         capture_output=True, text=True, check=True).stdout
    assert '反馈 cli:feishu:om_whole' in log


def test_old_report_without_scope_drops_only_that_row(world):
    io, target, flow, _ = world
    items, report, _ = reported(world)
    state = io.state(target)
    for value in state['reports'][report['id']]['feedback_targets'].values():
        value.pop('scopes', None)  # 改版前发出的报告没有范围选项
    io.save(target, state)
    send_feedback(world, items['n8n'], report, [{'key': items['n8n']['object_key'], 'instance': 'two', 'decision': '不用盯'}],
                  'om_old')
    assert instances(io, target)['n8n'] == ['one', 'three']
    assert io.watchlist(target)['excluded'] == {}
    assert submit(world, items['n8n'], report, [{'id': row_id(report, 'n8n', 'one'), 'choice': '不用盯',
                                                 'scope': 'software'}], 'om_old_scope').discard
