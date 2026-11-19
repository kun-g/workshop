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


VISUAL_BODY = ('## 要不要亲眼看\n是\n\n看什么：本单的亲眼看。\n1. 收尾送来的卡。\n2. 上线后的审批卡。\n\n'
               '在哪看：手机飞书，Ash 私聊。\n\n怎么回：在 Ash 私聊里引用那条送达回「过」，或「不对：哪里不对」。\n')
MERGE = 'c' * 40


def visual_world(tmp_path, service, monkeypatch, messages):
    """真实介入点服务 + 真实送达命令逻辑；飞书命令换成本地记录，回复从 messages 读。"""
    from workshop.plugins.issue_delivery.io import IO as IssueIO
    from workshop.plugins.intervention import service as service_module
    io = IssueIO(state_dir=tmp_path / 'issue')
    item, target = {'id': 'workshop:174'}, {'repo': 'kun-g/workshop'}
    io.save(item, target, {'visual': True, 'merge': MERGE, 'pr': 247})
    issue = {'number': 174, 'title': '介入点：授权原申请续接与约定业务送达',
             'html_url': 'https://github.com/kun-g/workshop/issues/174', 'body': VISUAL_BODY}
    flow = SimpleNamespace(io=io, _delivery_done=lambda *a: True, _issue=lambda *a: issue)
    args = SimpleNamespace(state_dir=str(tmp_path / 'transport'), profile='test', lark='lark', chat_id='chat',
                           recipient='kun', app_id='app')
    sent = []
    def command(args, argv, **kwargs):
        if '+messages-send' in argv:
            sent.append(json.loads(argv[argv.index('--content') + 1]))
            return {'data': {'message_id': 'om_card'}}
        return {'data': {'messages': messages}}
    monkeypatch.setattr(transport, 'command', command)
    def run(argv, input, **kwargs):
        return subprocess.CompletedProcess(argv, 0, json.dumps(transport.deliver(args, json.loads(input))), '')
    monkeypatch.setattr(service_module.subprocess, 'run', run)
    return flow, item, target, sent


def visible(node):
    """人在首屏看得到的文字：不含折叠详情，也不含按钮回传值。"""
    if isinstance(node, list):
        return ''.join(visible(n) for n in node)
    if not isinstance(node, dict) or node.get('tag') == 'collapsible_panel':
        return ''
    return ''.join(v if k == 'content' and isinstance(v, str) else visible(v)
                   for k, v in node.items() if k != 'behaviors')


def front(card):
    return visible([card['header'], *card['body']['elements']])


KUN, CHAT = 'ou_43456ed5ae17472375fcbb6ec0fc4661', 'oc_9d541419240c15d82e50fabbba2ace4e'


def click_event(card, choice, reason=None, who=KUN, message='om_card', event_id='ev-1'):
    """按契约：工作台把 card.action.trigger 原样包进 status_click 写进介入点 socket。"""
    buttons = [b for e in card['body']['elements'] for b in ([e] + e.get('elements', [])) if b.get('tag') == 'button']
    [button] = [b for b in buttons if b['behaviors'][0]['value']['choice'] == choice]
    action = {'tag': 'button', 'value': button['behaviors'][0]['value']}
    if reason is not None:
        action['form_value'] = {'reason': reason}
    return {'operation': 'workshop-click:' + event_id, 'event': {'header': {'event_id': event_id}, 'event': {
        'operator': {'open_id': who}, 'context': {'open_chat_id': CHAT, 'open_message_id': message}, 'action': action}}}


def test_visual_card_shows_issue_and_pr_and_folds_debug_numbers(tmp_path, service, monkeypatch):
    from workshop.plugins.issue_delivery.visual import after
    flow, item, target, sent = visual_world(tmp_path, service, monkeypatch, [])
    result = after(flow, item, target, lambda name: (Port(service), {}))
    assert result.wait and len(sent) == 1
    card = sent[0]
    delivery = flow.io.read(item, target)['visual_deliveries'][MERGE]['delivery']
    assert card['header']['title']['content'] == '介入点：授权原申请续接与约定业务送达'
    first = card['body']['elements'][0]['content']
    assert '[issue #174](https://github.com/kun-g/workshop/issues/174)' in first
    assert '[PR #247](https://github.com/kun-g/workshop/pull/247)' in first
    shown = front(card)
    assert MERGE not in shown and 'delivery:' not in shown and 'permit:' not in shown and 'workshop:174' not in shown
    assert '通过' in shown and '不通过' in shown and '不通过时写哪里不对' in shown
    blocks = [e.get('text', {}).get('content') for e in card['body']['elements']]
    assert '本单的亲眼看。\n1. 收尾送来的卡。\n2. 上线后的审批卡。' in blocks
    # Kun 11-19：正文只留看什么和按钮；在哪看、怎么回不上卡片。
    assert [e['content'] for e in card['body']['elements'] if e['tag'] == 'markdown'][1:] == ['**看什么**']
    assert '手机飞书' not in shown and '引用' not in shown and '在哪看' not in shown and '怎么回' not in shown
    panel = card['body']['elements'][-1]
    assert panel['tag'] == 'collapsible_panel' and panel['expanded'] is False
    assert panel['header']['title']['content'] == '详情'
    folded = json.dumps(panel, ensure_ascii=False)
    assert MERGE in folded and delivery in folded
    # 同一版本再走一拍不重发；标题后来改了也按原送达号推进，不换内容。
    issue = {**flow._issue(), 'title': '改过的标题'}
    flow._issue = lambda *a: issue
    assert after(flow, item, target, lambda name: (Port(service), {})).wait == result.wait
    assert len(sent) == 1


@pytest.mark.parametrize('text,passed', [('过', True), ('不对：标题不对', False)])
def test_quoted_reply_to_visual_card_is_recognized(tmp_path, service, monkeypatch, text, passed):
    from workshop.plugins.issue_delivery.visual import after
    messages = []
    flow, item, target, sent = visual_world(tmp_path, service, monkeypatch, messages)
    services = lambda name: (Port(service), {})
    wait = after(flow, item, target, services).wait
    messages.append({'reply_to': 'om_card', 'chat_id': 'chat', 'message_id': 'om_reply', 'msg_type': 'text',
                     'content': text, 'sender': {'sender_type': 'user', 'id': 'kun', 'id_type': 'open_id'}})
    assert Port(service).poll(wait['ref'], {})['ready']
    result = after(flow, item, target, services)
    assert (result is None) if passed else (result.rerun and result.reason == '亲眼看未通过')


def test_uncertain_card_send_reconciles_by_folded_delivery_key(tmp_path, monkeypatch):
    args = SimpleNamespace(state_dir=str(tmp_path), profile='test', lark='lark', chat_id='chat', recipient='kun', app_id='app')
    card = {'schema': '2.0', 'header': {'title': {'tag': 'plain_text', 'content': '标题'}},
            'body': {'elements': [{'tag': 'collapsible_panel', 'expanded': False,
                                   'header': {'title': {'tag': 'plain_text', 'content': '详情'}}, 'elements': []}]}}
    sent = []
    def command(args, argv, **kwargs):
        if '+messages-send' in argv:
            sent.append(json.loads(argv[argv.index('--content') + 1]))
            raise ValueError('响应丢失')
        # lark-cli 列出卡片时，折叠面板的正文照样在 content 里。
        return {'data': {'messages': [{'chat_id': 'chat', 'sender': {'sender_type': 'app', 'id': 'app'},
                'content': '<card title="标题">\n▶ 详情\n    送达号：delivery:stable\n▲\n</card>',
                'message_id': 'om_card', 'msg_type': 'interactive'}]}}
    monkeypatch.setattr(transport, 'command', command)
    request = {'action': 'send', 'key': 'delivery:stable', 'payload': {'kind': 'visual', 'card': card}}
    with pytest.raises(ValueError):
        transport.deliver(args, request)
    assert '送达号：delivery:stable' in json.dumps(sent[0]['body']['elements'][-1], ensure_ascii=False)
    assert transport.deliver(args, {**request, 'action': 'reconcile'}) == {'state': 'sent', 'external': 'om_card'}
    assert transport.deliver(args, request)['external'] == 'om_card'
    assert len(sent) == 1


def visual_body(response):
    return VISUAL_BODY.replace('怎么回：在 Ash 私聊里引用那条送达回「过」，或「不对：哪里不对」。', '怎么回：' + response)


BUTTONS = '点卡片上的「通过」；不通过就在卡片输入框写哪里不对，再点「不通过」。'


@pytest.mark.parametrize('choice,reason,answer,passed', [
    ('pass', None, '过', True), ('fail', '  标题看不懂  ', '不对：标题看不懂', False)])
def test_visual_buttons_answer_this_delivery(tmp_path, service, monkeypatch, choice, reason, answer, passed):
    from workshop.plugins.issue_delivery.visual import after
    flow, item, target, sent = visual_world(tmp_path, service, monkeypatch, [])
    flow._issue()['body'] = visual_body(BUTTONS)
    services = lambda name: (Port(service), {})
    wait = after(flow, item, target, services).wait
    card = sent[0]
    assert '按钮没反应时' not in front(card) and '引用' not in front(card)
    result = service.call('status_click', click_event(card, choice, reason))
    assert result['accepted'] and result['answer'] == answer and result['state'] == 'sent'
    assert len(sent) == 2 and sent[1]['header']['title']['content'] == '已收到：' + ('通过' if passed else '不通过')
    record = service.store.get(wait['ref'], 'wait')
    assert record['answer']['value'] == answer
    assert record['answer']['source']['delivery'] == flow.io.read(item, target)['visual_deliveries'][MERGE]['delivery']
    # 重复点击（新事件）不重复作答，也不再回第二条已收到。
    again = service.call('status_click', click_event(card, choice, reason, event_id='ev-2'))
    assert again['accepted'] and not again['answered'] and len(sent) == 2
    other = 'fail' if passed else 'pass'
    assert not service.call('status_click', click_event(card, other, '改主意', event_id='ev-3'))['accepted']
    result = after(flow, item, target, services)
    assert (result is None) if passed else (result.rerun and result.reason == '亲眼看未通过')


@pytest.mark.parametrize('case,reason', [
    ('empty', '不通过要在输入框写哪里不对'), ('other', '只接受指定本人私聊点击'),
    ('message', '按钮未绑定原送达'), ('old', '这张卡的版本已过期')])
def test_visual_button_rejections(tmp_path, service, monkeypatch, case, reason):
    from workshop.plugins.issue_delivery.visual import after
    flow, item, target, sent = visual_world(tmp_path, service, monkeypatch, [])
    flow._issue()['body'] = visual_body(BUTTONS)
    services = lambda name: (Port(service), {})
    wait = after(flow, item, target, services).wait
    card = sent[0]
    event = {'empty': lambda: click_event(card, 'fail', '   '),
             'other': lambda: click_event(card, 'pass', who='ou_other'),
             'message': lambda: click_event(card, 'pass', message='om_other'),
             'old': lambda: click_event(card, 'pass')}[case]
    if case == 'old':
        # 新版本合并后，旧卡上的按钮不放行新版本。
        state = flow.io.read(item, target); state['merge'] = 'd' * 40; flow.io.save(item, target, state)
        after(flow, item, target, services)
        assert len(sent) == 2
    result = service.call('status_click', event())
    assert not result['accepted'] and result['reason'] == reason
    assert all(w['answer'] is None for w in service.store.all('wait'))


def test_unsupported_reply_channel_still_blocks():
    from workshop.plugins.issue_delivery.visual import supported
    assert supported({'location': '手机飞书，Ash 私聊', 'response': BUTTONS})
    assert supported({'location': '手机飞书，Ash 私聊', 'response': '引用那条送达回「过」，或「不对：哪里不对」'})
    assert not supported({'location': 'GitHub PR', 'response': BUTTONS})
    assert not supported({'location': '飞书私聊', 'response': '评论确认'})


def test_send_command_missing_is_unsent_and_resends_same_key(tmp_path, service, monkeypatch):
    """11-19：重装后服务 PATH 里没有 lark-cli。起不来记没发出，等待原因带错误，修好后照原号发一次。"""
    from workshop.plugins.issue_delivery.visual import after
    flow, item, target, sent = visual_world(tmp_path, service, monkeypatch, [])
    real = transport.command
    def missing(args, argv, **kwargs):
        raise transport.Unstartable('lark-cli 起不来（PATH=/usr/bin:/bin:/usr/sbin:/sbin）：[Errno 2] No such file or directory')
    monkeypatch.setattr(transport, 'command', missing)
    services = lambda name: (Port(service), {})
    result = after(flow, item, target, services)
    delivery = flow.io.read(item, target)['visual_deliveries'][MERGE]['delivery']
    record = service.store.get(delivery, 'delivery')
    assert record['state'] == 'unsent' and 'lark-cli 起不来' in record['error']
    assert result.pending and result.reason.startswith('亲眼看没发出：lark-cli 起不来') and delivery in result.reason
    assert result.next == '修好送达命令后照原送达号重发' and not sent
    monkeypatch.setattr(transport, 'command', real)
    assert after(flow, item, target, services).wait and len(sent) == 1
    assert service.store.get(delivery, 'delivery')['state'] == 'sent'
    assert after(flow, item, target, services).wait and len(sent) == 1


def test_transport_unstartable_clears_started_but_reconcile_stays_unknown(tmp_path, monkeypatch):
    args = SimpleNamespace(state_dir=str(tmp_path), profile='test', lark=str(tmp_path / 'no-such-lark-cli'),
                           chat_id='chat', recipient='kun', app_id='app')
    request = {'action': 'send', 'key': 'delivery:k', 'payload': {'kind': 'visual', 'card': {'body': {'elements': []}}}}
    result = transport.deliver(args, request)
    assert result['state'] == 'absent' and 'no-such-lark-cli' in result['error']
    record = json.loads(next(tmp_path.glob('*.json')).read_text())
    assert 'started' not in record and record['send_calls'] == 0
    # 起不来的那次没有发送标记，对账确定没发；有发送标记时起不来只能说不明。
    assert transport.deliver(args, {**request, 'action': 'reconcile'}) == {'state': 'absent'}
    record['started'] = '2026-11-19T00:00:00+00:00'
    next(tmp_path.glob('*.json')).write_text(json.dumps(record))
    with pytest.raises(transport.Unstartable):
        transport.deliver(args, {**request, 'action': 'reconcile'})


def test_service_cannot_start_transport_marks_send_unsent(service, monkeypatch, tmp_path):
    permit = service.call('permit', {'operation': 'p', 'point': 'visual', 'owner': 'alpha:one',
        'scope': 'contract:alpha:one:v', 'basis': {'contract': '看', 'location': '飞书私聊', 'response': '点通过或不通过'}})['ref']
    ref = service.call('enqueue', {'operation': 'e', 'permit': permit, 'payload': {'kind': 'visual', 'version': 'v', 'card': {}}})['ref']
    original = service.configuration
    monkeypatch.setattr(service, 'configuration', lambda: {**original(), 'transport': [str(tmp_path / 'missing-python'), '-m', 'x']})
    record = service.call('dispatch', {'ref': ref})
    assert record['state'] == 'unsent' and '送达命令起不来' in record['error']
