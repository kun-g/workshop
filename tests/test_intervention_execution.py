"""#173：在真实引擎/流程/服务边界验证接单、优先级和无卡等待；时间用假时钟。"""
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path

import pytest
import yaml

from conftest import event, item, current, finish
from test_issue_delivery import world, BODY, edit, calls
from test_issue_end_to_end import engine_for, start, set_body, remote
from test_github_identity_flow import configure
from test_intervention import LocalPort, service, create, answer, RULES
from workshop.core.models import Event, Relations, Receipt
from workshop.core.rules import Rules
from workshop.core.ports import WaitRejected, WaitUnavailable
from workshop.plugins.intervention.client import Client
from workshop.plugins.intervention.service import Service
from workshop.registry import Registry
from workshop.reports.report import status, why


@pytest.mark.parametrize('body,expected', [(None, 'P1'), ('普通正文', 'P1'),
    ('优先级：P0。', 'P0'), ('优先级: P1', 'P1'), ('父单 #1。优先级：P2', 'P2')])
def test_issue_body_priority_crosses_relations_port(monkeypatch, body, expected):
    from types import SimpleNamespace
    from workshop.plugins.issue_delivery import relations
    connection = {'totalCount': 0, 'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}
    value = {'number': 7, 'body': body, 'parent': None, 'issueDependenciesSummary': {'totalBlockedBy': 0},
             'subIssuesSummary': {'total': 0}, **{field: connection for field in relations.GROUPS}}
    def query(io, text, deadline):
        assert 'number body' in text
        return {'i7': value}
    monkeypatch.setattr(relations, 'query', query)
    result = relations.read(SimpleNamespace(read=lambda *a: {}), [{'id': 'alpha:7', 'object_key': '7'}],
                            {'name': 'alpha', 'repo': 'kun-g/workshop'})
    assert result['alpha:7'].priority == expected


def test_global_priority_fifo_no_preemption_and_blocked_skip(engine):
    engine.rules.data['limits']['concurrent'] = 1
    priority = {'alpha:old': 'P2', 'beta:fast': 'P0', 'alpha:second': 'P0', 'alpha:blocked': 'P0'}
    def relations(objects, target):
        return {o['id']: Relations(priority=priority.get(o['id'], 'P1'),
                prerequisites=[{'id': 'missing', 'name': 'missing', 'complete': False}] if o['id'].endswith('blocked') else []) for o in objects}
    for flow in ('flow', 'other'):
        engine.plugins.get('flow', flow).relations = relations
    engine.ingest(event('old', ext_id='old'))
    engine.ingest(event('blocked', ext_id='blocked'))
    engine.ingest(event('fast', 'beta', 'fast'))
    engine.ingest(event('second', ext_id='second'))
    engine.tick()
    assert current(engine, 'fast', 'beta')['stage'] == '接单'
    assert item(engine, 'blocked')['state'] == '等前置'
    token = current(engine, 'fast', 'beta')['token']
    priority['alpha:second'] = 'P0'
    engine.clock.now += timedelta(seconds=30)
    engine.tick()
    assert current(engine, 'fast', 'beta')['token'] == token
    finish(engine, key='fast', target='beta', outcome='nothing', later=[{'key': 'later', 'at': '+1h', 'about': '再看'}])
    assert current(engine, 'second')['stage'] == '接单'
    finish(engine, key='second', outcome='nothing', later=[{'key': 'later', 'at': '+1h', 'about': '再看'}])
    assert current(engine, 'old')['stage'] == '接单'


def test_priority_cannot_bypass_serial_holder(engine):
    engine.rules.data['targets']['alpha']['serial_paths'] = ['src/']
    engine.ingest(event('holder', ext_id='holder'))
    engine.change(item(engine, 'holder'), stage='执行', paths=['src/a.py'], serial_hold=True)
    engine.tick()
    engine.ingest(event('high', ext_id='high'))
    engine.change(item(engine, 'high'), stage='执行', paths=['src/b.py'], relations={'priority': 'P0'})
    engine.tick()
    assert item(engine, 'high')['state'] == '排队'
    assert len(engine.active()) == 1


def test_auto_intake_rechecks_only_changed_body_and_uses_app(world, monkeypatch):
    configure(world, monkeypatch)
    target = {**world['target'], 'auto_intake': True, 'intake_app_logins': ['lambdatheory-agent[bot]']}
    issue = remote(world)['issue']
    issue.update(user={'login': 'kun-g'}, labels=[], body=BODY.replace('## 不可逆或对外的动作\n无\n', ''))
    edit(world, issue=issue)
    flow = world['flow']
    def source(ext):
        return Event('github', ext, 'alpha', '7', '登记', {'kind': 'issue', 'number': '7', 'text': '伪造齐全正文'})
    assert flow.classify(source('1'), None, target, None).discard
    first = len(calls(world))
    assert flow.classify(source('2'), None, target, None).discard
    assert len(calls(world)) == first + 1  # 只回源，没再读需求文档/打标签。
    issue['body'] = BODY
    edit(world, issue=issue)
    assert flow.classify(source('3'), None, target, None).action == 'ready'
    writes = [c for c in calls(world) if '/issues/7/labels' in str(c)]
    assert len(writes) == 1
    assert flow.classify(source('4'), None, target, None).discard
    assert len([c for c in calls(world) if '/issues/7/labels' in str(c)]) == 1


@pytest.mark.parametrize('body,author', [(BODY.replace('核心 R14', '核心 R999'), 'kun-g'),
    (BODY.replace('核心 R14', '需求变更：核心.md 改 R14').replace('检查失败时让错误可见。', ''), 'kun-g'),
    (BODY, 'stranger'), (BODY, 'lambdatheory-agent[bot]')])
def test_auto_intake_rejects_invalid_basis_missing_quote_or_origin(world, monkeypatch, body, author):
    configure(world, monkeypatch)
    issue = remote(world)['issue']
    issue.update(body=body, labels=[], user={'login': author})
    edit(world, issue=issue)
    target = {**world['target'], 'auto_intake': True, 'intake_app_logins': ['lambdatheory-agent[bot]']}
    value = world['flow'].classify(Event('github', 'one', 'alpha', '7', '登记', {'kind': 'issue', 'number': '7'}), None, target, None)
    assert value.discard
    assert not any('/labels' in str(c) for c in calls(world))


def setup_decision(world, service):
    engine = engine_for(world, 'decision')
    engine.rules.data['notices'] = 'hold'
    engine.rules = Rules({**engine.rules.data, 'wait_services': {'intervention': {'endpoint': '/unused'}}})
    engine.plugins.register('wait', 'intervention', LocalPort(service))
    start(engine)
    engine.tick()
    return engine


def decision():
    return {'schema': 'issue-delivery/v1', 'decision': {'key': 'path', 'version': 'v1', 'question': '先做哪条路径',
             'reason': '两条路径成本不同', 'recommend': '先做 A'}}


def submit_decision(engine):
    return finish(engine, key='7', outcome='nothing', evidence=[json.dumps(decision())], leave={'kind': '等你', 'about': '选路径'})


def test_no_card_wait_releases_slot_and_answer_resumes_exactly_once(world, service):
    engine = setup_decision(world, service)
    submitted = submit_decision(engine)
    waiting = item(engine, '7')
    assert waiting['state'] == '等你', waiting
    assert engine.occupied() == 0 and not waiting.get('serial_hold')
    assert not engine.log.all('cards')
    record = service.store.all('wait')[0]
    text = status(engine.log, rules=engine.rules.data, plugins=engine.plugins)
    assert all(v in text for v in ('先做哪条路径', '先做 A', '等了多久', '两条路径成本不同', 'P1'))
    engine.ingest(Event('session', 'general-release', 'alpha', '7', '泛化放行', {'quote': '放', 'session': 's'}, action='ready'), directed=True)
    engine.tick()
    assert item(engine, '7')['state'] == '等你'
    data = {'operation': 'answer-real', 'ref': record['id'], 'value': 'A',
            'source': {'channel': 'session', 'session': 'conversation-id', 'quote': '先 A', 'version': 'v1'}}
    first = service.call('answer', data)
    assert service.call('answer', data) == first
    engine.clock.now += timedelta(minutes=1)
    engine.tick()
    assert current(engine, '7')['stage'] == '执行'
    assert engine.plugins.get('executor', 'writer').calls[-1][0] == 'resume'
    assert len([f for f in engine.log.facts() if f['kind'] == 'wait_resumed']) == 1
    engine.tick()
    assert len([f for f in engine.log.facts() if f['kind'] == 'wait_resumed']) == 1
    assert 'conversation-id' in why(engine.log, 'alpha:7', engine.rules.data, engine.plugins)
    assert 'conversation-id' in json.dumps(current(engine, '7')['brief'], ensure_ascii=False)
    assert engine.log.get('rounds', submitted['token'])['status'] == 'ended'
    engine.log.close()


@pytest.mark.parametrize('error,state,broken', [(WaitRejected('内容不合格'), '卡住', False),
    (WaitUnavailable('暂时不通'), '定时', False), (RuntimeError('调用方自己的错'), '卡住', True)])
def test_direct_call_failures_stay_with_correct_owner(world, service, monkeypatch, error, state, broken):
    engine = setup_decision(world, service)
    def fail(*args):
        raise error
    monkeypatch.setattr(engine.plugins.get('wait', 'intervention'), 'create', fail)
    submit_decision(engine)
    assert item(engine, '7')['state'] == state
    assert bool(engine.flow_broken('alpha', 'issue_delivery')) == broken
    assert not engine.log.all('cards')
    engine.log.close()


def test_invalid_wait_is_rejected_in_same_round(world, service):
    engine = setup_decision(world, service)
    value = decision()
    del value['decision']['recommend']
    token = current(engine, '7')['token']
    with pytest.raises(ValueError, match='recommend'):
        engine.submit(token, {'outcome': 'nothing', 'summary': '等待', 'evidence': [json.dumps(value)]})
    assert engine.log.get('rounds', token)['receipt'] is None
    submit_decision(engine)
    assert item(engine, '7')['state'] == '等你'
    engine.log.close()


def test_service_versions_and_configuration_are_validated(service, tmp_path):
    ref = create(service)
    old = service.store.get(ref)['content']
    new = service.call('create', {'operation': 'v2', 'point': 'decision', 'owner': 'alpha:one',
                                'content': {**old, 'version': '2', 'question': '换一个问题'}})['ref']
    with pytest.raises(ValueError, match='过期'):
        answer(service, ref)
    with pytest.raises(ValueError, match='版本'):
        answer(service, new)
    with pytest.raises(ValueError, match='content.recommend'):
        service.call('create', {'operation': 'missing', 'point': 'decision', 'owner': 'alpha:one',
                               'content': {k:v for k,v in old.items() if k != 'recommend'}})
    rules = yaml.safe_load(RULES.read_text())
    rules['intervention']['points']['decision']['wait'] = 'yes'
    path = tmp_path / 'bad.yaml'
    path.write_text(yaml.safe_dump(rules))
    Rules.load(path)  # 核心不读介入点业务段。
    with pytest.raises(ValueError, match='intervention.points.decision.wait'):
        Service(tmp_path / 'bad', path)
    rules = Rules.load(RULES)
    rules.data['wait_services']['intervention']['timeout'] = 31
    with pytest.raises(ValueError, match='wait_services.intervention.*timeout'):
        Registry().configure(rules)


def test_source_classification_update_cannot_create_item_and_survives_pause(engine):
    engine.rules.data['targets']['alpha'] = {'default_flow': 'flow', 'flows': {'flow': {
        'triggers': [{'source': 'input', 'action': 'classify'}]}}, 'sources': []}
    engine.pause('alpha')
    engine.ingest(event(action='update'))
    assert item(engine) is None
    engine.resume('alpha', [v['id'] for v in engine.missed('alpha')])
    engine.classify_events()
    assert item(engine) is None
    assert any(f['kind'] == 'event_classified' for f in engine.log.facts())


def test_auto_intake_engine_creates_only_complete_issue(world, monkeypatch):
    configure(world, monkeypatch)
    cfg = {'default_flow': 'issue_delivery', 'flows': {'issue_delivery': {
        'triggers': [{'source': 'github', 'when': {'kind': 'issue'}, 'action': 'classify'}],
        'stages': ['接单', '执行', '交付', '收尾']}}, 'auto_intake': True}
    engine = engine_for(world, 'automatic', **cfg)
    issue = remote(world)['issue']
    issue.update(user={'login': 'kun-g'}, labels=[], body=BODY.replace('## 不可逆或对外的动作\n无\n', ''))
    edit(world, issue=issue)
    for key, body in [('missing', issue['body']), ('complete', BODY)]:
        issue['body'] = body
        edit(world, issue=issue)
        engine.ingest(Event('github', key, 'alpha', '7', '登记', {'kind': 'issue', 'number': '7'}))
        engine.classify_events()
        assert bool(engine.log.get('items', 'alpha:7')) == (key == 'complete')
    assert len(engine.log.all('items')) == 1
    assert not engine.log.all('cards')
    engine.log.close()


def test_source_bootstraps_unlabeled_once_then_keeps_incremental_cursor(world):
    from workshop.plugins.sources.github import GitHubSource
    from test_issue_delivery import source_responses
    responses = source_responses(world)
    base = 'repos/kun-g/workshop'
    issue = responses[base + '/issues?state=all&sort=updated&per_page=100'][0]
    issue['labels'] = []
    responses[base + '/issues?state=open&sort=updated&per_page=100'] = [issue]
    edit(world, responses=responses)
    source = GitHubSource(str(world['commands'] / 'gh'))
    target = {**world['target'], 'auto_intake': True}
    legacy = source.poll(world['target'], None)
    batch = source.poll(target, legacy.cursor)
    assert json.loads(batch.cursor)['intake_bootstrapped']
    fresh = [e for e in batch.events if 'intake-v1-body' in e.ext_id]
    assert len(fresh) == 1
    assert not set(fresh[0].previous_ids) & {e.ext_id for e in legacy.events}
    before = len(calls(world))
    quiet = source.poll(target, batch.cursor)
    assert quiet.events == []
    assert not any('state=open' in str(c) for c in calls(world)[before:])


@pytest.mark.parametrize('failure,state,broken', [('rejected', '卡住', False),
    ('unavailable', '定时', False), ('caller-error', '卡住', True)])
def test_real_socket_failures_in_isolated_engine(world, service, tmp_path, failure, state, broken):
    import runpy
    import tempfile
    service_process = runpy.run_path(str(RULES.parents[1] / 'scripts/verify_intervention_execution.py'))['service_process']
    engine = setup_decision(world, service)
    engine.plugins.register('wait', 'intervention', Client())
    rules = yaml.safe_load(RULES.read_text())
    if failure == 'rejected':
        rules['intervention']['points']['decision']['required_wait'].append('extra')
    path = tmp_path / 'socket-rules.yaml'
    path.write_text(yaml.safe_dump(rules))
    try:
        with tempfile.TemporaryDirectory(prefix='ws173-', dir='/tmp') as directory, service_process(Path(directory) / 'service', path) as endpoint:
            config = {'endpoint': str(endpoint), 'timeout': 1}
            if failure == 'unavailable':
                config['endpoint'] = str(Path(directory) / 'missing.sock')
            elif failure == 'caller-error':
                # 模拟调用方漏传配置；不是服务返回的拒绝。
                config.pop('endpoint')
            engine.rules.data['wait_services']['intervention'] = config
            submit_decision(engine)
            actual = item(engine, '7')
            result = {'case': failure, 'state': actual['state'], 'reason': actual.get('blocked_by') or actual.get('gate_reason'),
                      'flow_broken': bool(engine.flow_broken('alpha', 'issue_delivery')), 'sends': engine.log.all('cards')}
            print(json.dumps(result, ensure_ascii=False))
            assert result['state'] == state and result['flow_broken'] == broken and result['sends'] == []
    finally:
        engine.log.close()
