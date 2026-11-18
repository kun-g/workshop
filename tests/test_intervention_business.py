"""#174 授权绑定、版本绑定、持久送达和不确定结果。"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest
from workshop.authorization import verify, target_key
from workshop.core.gates import decide
from workshop.core.models import Evidence, Receipt, STAGES
from workshop.core.rules import Rules
from workshop.plugins.intervention.service import Service
from workshop.plugins.intervention import transport
from workshop.plugins.issue_delivery import dod
from workshop.plugins.watch_updates.io import IO
from conftest import configuration


class Port:
    def __init__(self, service):
        self.service = service
    def invoke(self, action, data, config):
        return self.service.call(action, data)
    def create(self, operation, content, config):
        return self.invoke('create', {'operation': operation, **content}, config)['ref']
    def poll(self, ref, config):
        return self.invoke('status', {'ref': ref}, config)
    def acknowledge(self, operation, ref, token, config):
        return self.invoke('ack', {'operation': operation, 'ref': ref, 'token':token}, config)
    def read(self, ref, config):
        return self.invoke('get', {'ref': ref}, config)


@pytest.fixture
def service(tmp_path):
    return Service(tmp_path / 'service', Path('rules/workshop.yaml'))


def spec():
    return {'request': 'exec-original', 'action': '有别人在用的交付对象上线', 'kind': 'exec', 'provider_action': 'argocd/sync_app',
            'checkpoint': 'deploy', 'target': {'instance': 'central', 'app': 'app', 'revision': 'a' * 40}}


def completed():
    return {'id': 'exec-original', 'status': 'completed', 'broker': 'argocd', 'action': 'sync_app',
            'result': {**spec()['target'], 'submitted': True, 'dry_run': False, 'operation_id': 'exec-original',
                       'operation_matches': True, 'operation_phase': 'Succeeded',
                       'after': {'revision': 'a' * 40, 'sync': 'Synced', 'health': 'Healthy'}}}


@pytest.mark.parametrize('case,allowed', [('match', True), ('action', False), ('target', False),
    ('denied', False), ('expired', False), ('unknown', False), ('not_applied', False)])
def test_authorization_gate_binds_original_action_target_and_result(case, allowed):
    original = spec()
    raw = completed()
    if case in ('denied', 'expired', 'unknown'):
        raw['status'] = case
    if case == 'not_applied':
        raw['result']['operation_matches'] = False
    result = verify(original, raw)
    if case == 'action':
        result['action'] = '其它动作'
    if case == 'target':
        result['target'] = '另一个目标'
    evidence = Evidence(passed=True, actions=[original['action']], action_targets={original['action']: target_key(original['target'])},
                        authorized_actions=[result])
    evidence.validate()
    config = configuration()
    config['ask_first'].append(original['action'])
    decision = decide({'stage': '审查', 'target': 'alpha', 'approved': [original['action']],
                       'frozen_approved': ['any']}, Receipt('done', '', '交付'), evidence, STAGES[:-1], Rules.validate(config))
    assert decision.allowed is allowed


def test_execution_result_does_not_accept_another_original_request():
    record = completed()
    record['id'] = 'exec-another'
    assert not verify(spec(), record)['applied']
    record = completed()
    record['result']['operation_id'] = 'exec-another'
    assert not verify(spec(), record)['applied']


def test_visual_registration_requires_each_contract_paragraph():
    body = '## 要不要亲眼看\n是\n看什么：版本\n在哪看：手机飞书\n怎么回：引用回复过或不对\n'
    assert all(dod.visual_contract(body).values())
    for label in ('看什么', '在哪看', '怎么回'):
        missing = '\n'.join(line for line in body.splitlines() if not line.startswith(label + '：'))
        assert any(label in s for s in dod.registration(missing, {})[0])


def test_report_uses_service_preserves_key_and_reconciles_uncertain(service, tmp_path):
    port = Port(service)
    services = lambda name: (port, {})
    calls = []
    def channel(config, action, record):
        calls.append((action, record['id'], record['payload']))
        return {'state': 'unknown'} if len(calls) == 1 else {'state': 'sent', 'external': 'om_report'}
    service.transport = channel
    target = {'name': 'alpha', 'watch_updates': {'state_dir': str(tmp_path / 'state')}}
    file = tmp_path / 'daily.html'; file.write_text('<html>真实报告的形状</html>')
    report = {'id': 'daily', 'path': str(file), 'summary': '不能发送的摘要'}
    io = IO()
    with pytest.raises(ValueError, match='原送达号'):
        io.send_report(target, report, services)
    io.send_report(target, report, services)
    io.send_report(target, report, services)
    assert [c[0] for c in calls] == ['send', 'reconcile']
    assert calls[0][1:] == calls[1][1:]
    payload = calls[0][2]
    assert payload['artifact'] == str(file) and 'text' not in payload and 'summary' not in payload
    from workshop.plugins.watch_updates.model import digest
    assert payload['idempotency_key'] == digest(['daily', 'file'])


def test_transport_sends_once_and_reconciles_after_lost_response(tmp_path, monkeypatch):
    args = SimpleNamespace(state_dir=str(tmp_path), profile='test', lark='lark', chat_id='chat', recipient='kun', app_id='app')
    sent = []
    def command(args, argv, **kwargs):
        if '+messages-send' in argv:
            sent.append(argv)
            raise ValueError('响应丢失')
        return {'data': {'messages': [{'chat_id': 'chat', 'sender': {'sender_type': 'app', 'id':'app'},
                'content': 'test [送达 stable]', 'message_id': 'om_sent', 'msg_type': 'text'}]}}
    monkeypatch.setattr(transport, 'command', command)
    request = {'action': 'send', 'key': 'stable', 'payload': {'kind': 'visual', 'text': 'test'}}
    with pytest.raises(ValueError):
        transport.deliver(args, request)
    assert transport.deliver(args, {**request, 'action': 'reconcile'}) == {'state': 'sent', 'external': 'om_sent'}
    assert transport.deliver(args, request)['external'] == 'om_sent'
    assert len(sent) == 1


def test_visual_old_reply_cannot_answer_current_version(service):
    def version(v):
        permit = service.call('permit', {'operation': v+'p','point':'visual','owner':'alpha:one',
            'scope':'contract:alpha:one:'+v,'basis': {'contract':'看页面','location':'飞书','response':'过或不对'}})['ref']
        delivery = service.call('enqueue', {'operation': v+'e', 'permit':permit,
            'payload': {'kind':'visual','version':v,'text':'看页面'}})['ref']
        service.transport = lambda *a: {'state':'sent','external':'om_'+v}
        service.call('dispatch', {'ref':delivery})
        ref = service.call('create', {'operation':v+'w','point':'visual','owner':'alpha:one',
            'content': {'key':'visual','stage':'收尾','version':v,'delivery':delivery,'response':'过或不对'}})['ref']
        return delivery, ref
    old, oldref = version('old')
    new, newref = version('new')
    reply = {'operation':'reply-old','ref':newref,'value':'过','source':{'channel':'external',
        'reference':'om_reply','evidence':'真实身份回查','version':'old','delivery':old}}
    with pytest.raises(ValueError, match='版本'):
        service.call('answer', reply)
    with pytest.raises(ValueError, match='过期'):
        service.call('answer', {**reply, 'ref':oldref})
    reply['source'].update(version='new',delivery=new)
    assert service.call('answer', {**reply, 'operation':'reply-new'})['token']


def test_acceptance_shortcut_still_needs_authorization():
    item = {'stage':'执行','target':'alpha','history':[{'stage':'验收'}], 'approved':['对外发消息']}
    decision = decide(item, Receipt('done', '', '验收'), Evidence(passed=True, actions=['对外发消息']),
                      STAGES[:-1], Rules.validate(configuration()))
    assert not decision.allowed and decision.needs_approval


def test_pending_interval_and_completion_wakeup(engine, monkeypatch):
    from datetime import datetime, timedelta
    from conftest import reach, finish, item, event
    from workshop.core.models import After
    flow = engine.plugins.get('flow', 'flow')
    state = {'done':False,'calls':0}
    def after(i, stage, key, target):
        state['calls'] += 1
        return After() if state['done'] else After(pending=True, reason='等结果', next='回查',
                                                   recheck_after='+5m', wake_on_completed=True)
    reach(engine, '交付')
    monkeypatch.setattr(flow, 'after', after)
    finish(engine, '验收')
    for _ in range(2):
        waiting = item(engine)
        at = datetime.fromisoformat(waiting['after_wait']['at'])
        assert at - engine.clock() == timedelta(minutes=5)
        engine.clock.now = at
        engine.tick()
    before = state['calls']
    engine.ingest(event('batch', ext_id='batch'))
    batch = engine.log.get('items', 'alpha:batch')
    engine.change(batch, stage='收尾', state='完成')
    state['done'] = True
    engine.tick()
    assert state['calls'] == before + 1
    assert item(engine)['stage'] == '验收'
    assert not [t for t in engine.log.all('timers') if t['item']=='alpha:one' and not t['fired']]


@pytest.mark.parametrize('app,revision,allowed', [('app','a'*40,True),('other','a'*40,False),('app','b'*40,False)])
def test_old_authorization_cannot_allow_new_delivery_target(app, revision, allowed):
    from workshop.plugins.issue_delivery.authorization import evidence
    state = {'authorizations':{'old':{'verification':verify(spec(),completed())}}}
    target = {'delivery_action':spec()['action'],'deployment':{'app':app}}
    actual = Evidence(passed=True, actions=[spec()['action']], **evidence(state,target,revision))
    config=configuration(); config['ask_first'].append(spec()['action'])
    decision=decide({'stage':'审查','target':'alpha'},Receipt('done','','交付'),actual,STAGES[:-1],Rules.validate(config))
    assert decision.allowed is allowed


@pytest.mark.parametrize('filename,sender,expected', [('daily.html','app','sent'),('old-daily.html','app','unknown'),('daily.html','other','unknown')])
def test_report_reconciliation_requires_exact_attachment_and_bot(tmp_path, monkeypatch, filename, sender, expected):
    file=tmp_path/'daily.html'; file.write_text('<html/>')
    args=SimpleNamespace(state_dir=str(tmp_path/'transport'), profile='test', lark='lark', chat_id='chat', recipient='kun',app_id='app')
    monkeypatch.setattr(transport,'command',lambda *a,**k: {'data':{'messages':[
        {'chat_id':'chat','sender':{'sender_type':'app','id':sender}, 'msg_type':'file',
         'content':f'<file key="file-key" name="{filename}"/>','message_id':'om_file'}]}})
    result=transport.deliver(args,{'action':'reconcile','key':'report','payload':{'kind':'update_report',
        'artifact':str(file),'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),'existing_external':'unknown'}})
    assert result['state']==expected


def test_unsupported_visual_contract_blocks_before_sending(tmp_path, service):
    from workshop.plugins.issue_delivery.io import IO as IssueIO
    from workshop.plugins.issue_delivery.visual import after
    io=IssueIO(state_dir=tmp_path/'issue')
    item={'id':'alpha:one'}; io.save(item,{}, {'visual':True,'merge':'version'})
    flow=SimpleNamespace(io=io,_delivery_done=lambda *a:True,
        _issue=lambda *a:{'body':'## 要不要亲眼看\n是\n看什么：页面\n在哪看：GitHub PR\n怎么回：评论确认\n'})
    result=after(flow,item,{},lambda name:(Port(service),{}))
    assert result.blocked and not service.store.all('delivery')
