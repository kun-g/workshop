#!/usr/bin/env python3
"""#174 隔离回放：现役数据只读；已有中心申请只查状态；送达仅进本地收件箱。"""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import yaml
from workshop.core.models import Event
from workshop.core.ports import WaitRejected
from workshop.plugins.intervention.client import Client, request, Rejected
from workshop.plugins.issue_delivery import authorization, visual
from workshop.plugins.issue_delivery.io import IO as IssueIO
from workshop.plugins.scheduled_release.cluster import ClusterRelease
from workshop.plugins.scheduled_release.io import IO as ReleaseIO, read, write
from workshop.plugins.watch_updates.flow import Flow as WatchFlow
from workshop.plugins.watch_updates.io import IO as WatchIO


TRANSPORT = '''import json, sqlite3, sys
from pathlib import Path
root=Path(sys.argv[1]); m=json.load(sys.stdin)
if m['action']=='reply':
 print('{}'); raise SystemExit
with sqlite3.connect(root/'inbox.db') as db:
 db.execute('CREATE TABLE IF NOT EXISTS calls (action TEXT, key TEXT, payload TEXT)')
 db.execute('CREATE TABLE IF NOT EXISTS inbox (key TEXT PRIMARY KEY, payload TEXT)')
 db.execute('INSERT INTO calls VALUES (?,?,?)',(m['action'],m['key'],json.dumps(m['payload'])))
 if m['action']=='send':
  sent=dict(m['payload'])
  if 'card' in sent:
   # 卡片照真实送达命令补送达号，收件箱存的就是会发出去的那张。
   from workshop.plugins.intervention.transport import with_key
   sent['card']=with_key(sent['card'],m['key'])
  db.execute('INSERT INTO inbox VALUES (?,?)',(m['key'],json.dumps(sent,ensure_ascii=False)))
  state='unknown' if m['payload']['kind']=='visual' else 'sent'
 else:
  assert db.execute('SELECT 1 FROM inbox WHERE key=?',(m['key'],)).fetchone()
  state='sent'
 db.commit()
print(json.dumps({'state':state,'external':'local:'+m['key']}))
'''


def emit(step, value):
    print(json.dumps({'step': step, **value}, ensure_ascii=False), flush=True)


def authorization_child(path):
    data = read(path)
    io = IssueIO(state_dir=path.parent / 'issue-state')
    item = {'id':'replay:authorization:' + data['scenario']}
    result = authorization.after(io, item, data['target'], data['spec'])
    state = io.read(item, data['target'])
    emit('authorization', {'scenario':data['scenario'], 'pid':os.getpid(), 'request':data['spec']['request'],
        'after':asdict(result), 'record':state['authorizations'][data['spec']['checkpoint']], 'new_requests':0})


def verify(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Unix socket path length is limited on macOS.
    runtime = Path(tempfile.mkdtemp(prefix='ws174-', dir='/tmp'))
    rules = yaml.safe_load(args.rules.read_text())
    report_state = read(args.state / 'watch_updates/state.json')
    report = report_state['reports'][args.report]
    batch = read(args.batch)
    batchdir = args.batch.with_suffix('')
    production = read(batchdir / 'deploy/production.json')
    deployed = read(batchdir / 'deploy/result.json')
    target = {**rules['targets']['grant-console'], **rules['targets']['grant-console']['flows']['scheduled_release']}
    target['authorization_client'] = str(args.grant_client)
    base = {'kind':'exec', 'action':'有别人在用的交付对象上线', 'provider_action':'argocd/sync_app',
            'checkpoint':'original-sync', 'target':{'instance':'central','app':target['deployment']['app'],
                                                 'revision':production['revision']}}
    for scenario, identifier, kind, url in (
        ('approved', deployed['grant']['id'], 'exec', None),
        ('denied', args.denied, 'grant', None),
        ('expired', args.expired, 'grant', None),
        ('unknown', deployed['grant']['id'], 'exec', 'http://127.0.0.1:1')):
        checkpoint = root / (scenario + '.json')
        write(checkpoint, {'scenario':scenario, 'spec':{**base,'request':identifier,'kind':kind},
                          'target':{'authorization_client':str(args.grant_client), 'authorization_url':url}})
        responses = []
        for _ in range(2):
            argv = [sys.executable, str(Path(__file__).resolve()), '--authorization-child', str(checkpoint)]
            result = subprocess.run(argv, capture_output=True, text=True, check=True)
            print(result.stdout, end='', flush=True)
            responses.append(json.loads(result.stdout))
        assert responses[0]['pid'] != responses[1]['pid']
        assert {r['request'] for r in responses} == {identifier}
        expected = 'approved' if scenario == 'approved' else scenario
        assert all(r['record']['verification']['result'] == expected for r in responses)
        assert all(r['after']['rerun'] == (scenario == 'approved') for r in responses)
    emit('restart', {'same_original_requests':True, 'new_requests':0})

    # Replay a real exited worker's directory. Never start cluster.worker or sync-production.
    release_root = root / 'release'
    copy = release_root / 'batches' / args.batch.name
    shutil.copytree(batchdir, copy.with_suffix(''), dirs_exist_ok=True)
    target['scheduled_release_state_dir'] = str(release_root)
    batch.update(status='awaiting_grant', result={**deployed, 'status':'awaiting_grant'})
    batch.pop('authorization', None)
    write(copy, batch)
    cluster = ClusterRelease(ReleaseIO())
    result = cluster.grant_outcome(batch, target)
    write(copy, batch)
    assert batch['status'] == 'ok' and not result.pending
    emit('historical_batch', {'original':str(args.batch), 'copy':str(copy), 'request':deployed['grant']['id'],
        'status':batch['status'], 'verification':batch['deployed']['verification'],
        'execution':batch['deployed']['execution'], 'sync_script_calls':0, 'new_requests':0})

    # Start with the actual new rules, self-check, then restart with only transport replaced.
    endpoint = runtime / 'service.sock'
    service_state = root / 'service'
    original = runtime / 'original.yaml'
    original.write_text(yaml.safe_dump(rules, allow_unicode=True))
    local = runtime / 'local.yaml'
    transport = runtime / 'transport.py'; transport.write_text(TRANSPORT)
    rules['intervention']['transport'] = [str(args.service_python), '-I', str(transport), str(root)]
    local.write_text(yaml.safe_dump(rules, allow_unicode=True))
    def launch(rulepath):
        reader, writer = os.pipe()
        argv = [str(args.service_python), '-I', '-m', 'workshop.plugins.intervention',
            '--state-dir', str(service_state), '--rules', str(rulepath), '--endpoint', str(endpoint),
            'serve', '--ready-fd', str(writer)]
        process = subprocess.Popen(argv, pass_fds=(writer,), stdout=(root/'service.stdout').open('a'),
                                   stderr=(root/'service.stderr').open('a'))
        os.close(writer)
        try:
            ready, _, _ = select.select([reader], [], [], 15)
            if not ready or os.read(reader, 64) != b'ready\n':
                process.terminate(); process.wait(timeout=10)
                raise RuntimeError((root/'service.stderr').read_text())
        finally:
            os.close(reader)
        emit('service_started', {'argv':argv,'pid':process.pid, 'selfcheck':request(endpoint,'ping',{})})
        return process
    process = launch(original)
    process.terminate(); process.wait(timeout=10)
    process = launch(local)
    config = {'endpoint':str(endpoint), 'timeout':25}
    port = Client()
    services = lambda name: (port, config)
    try:
        # The business flow uses the same after hook and service as production; no external send.
        from types import SimpleNamespace
        issue_io = IssueIO(state_dir=root/'visual-state')
        # `gh issue view <号> --json number,title,url,body` 的原样输出：卡片用本单真实标题、链接和正文。
        view = json.loads(args.issue.read_text())
        issue = {'number':view['number'], 'title':view['title'], 'html_url':view['url'], 'body':view['body']}
        item = {'id':'workshop:174','stage':'收尾'}
        version = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
        flow = SimpleNamespace(io=issue_io, _delivery_done=lambda *a: True, _issue=lambda *a: issue)
        target_visual = {'name':'workshop','repo':'kun-g/workshop'}
        issue_io.save(item,target_visual,{'effects':{},'visual':True,'merge':version,'delivered':[version],'pr':args.pr})
        first = visual.after(flow,item,target_visual,services)
        assert first.pending and first.wait is None
        second = visual.after(flow,item,target_visual,services)
        third = visual.after(flow,item,target_visual,services)
        assert second.wait == third.wait
        entry = issue_io.read(item,target_visual)['visual_deliveries'][version]
        emit('visual_uncertain_reconciled', {'version':version,'delivery':entry['delivery'],'wait':entry['wait']})
        def card_of(entry):
            with sqlite3.connect(root/'inbox.db') as db:
                return json.loads(db.execute('SELECT payload FROM inbox WHERE key=?',(entry['delivery'],)).fetchone()[0])['card']
        def click(entry, choice, reason=None, event_id=None, who=None):
            # 按契约：工作台把 card.action.trigger 原样包进 status_click，一行写进介入点 socket。
            card = card_of(entry)
            buttons = [b for e in card['body']['elements'] for b in ([e] + e.get('elements', [])) if b.get('tag') == 'button']
            value = next(b['behaviors'][0]['value'] for b in buttons if b['behaviors'][0]['value']['choice'] == choice)
            action = {'tag':'button','value':value, **({'form_value':{'reason':reason}} if reason is not None else {})}
            event_id = event_id or 'replay-' + choice + '-' + entry['delivery'][-8:]
            data = {'operation':'workshop-click:' + event_id, 'event':{'header':{'event_id':event_id}, 'event':{
                'operator':{'open_id':who or rules['intervention']['status']['recipient']},
                'context':{'open_chat_id':rules['intervention']['status']['chat'],'open_message_id':entry['external']},
                'action':action}}}
            result = request(endpoint,'status_click',data)
            emit('click', {'choice':choice,'form_value':action.get('form_value'),'version':value['version'],'result':result})
            return result
        def next_version(name):
            state = issue_io.read(item,target_visual); state.update(merge=name,delivered=[name]); issue_io.save(item,target_visual,state)
            visual.after(flow,item,target_visual,services)
            assert visual.after(flow,item,target_visual,services).wait
            return issue_io.read(item,target_visual)['visual_deliveries'][name]

        newer = version + '-replay-fail'
        current = next_version(newer)
        reply = {'operation':'old-reply','ref':current['wait'],'value':'过',
            'source':{'channel':'external','reference':'local-reply','evidence':'隔离回放，不冒充 Kun 真实回答',
                      'version':version,'delivery':entry['delivery']}}
        try:
            port.invoke('answer',reply,config)
        except WaitRejected as exc:
            emit('old_reply_rejected', {'reason':str(exc)})
        else:
            raise AssertionError('旧版本回复被接受')
        assert click(entry,'pass')['state'] == 'rejected'          # 旧版本卡上的按钮
        assert click(current,'fail','   ')['state'] == 'rejected'   # 不通过空输入
        assert click(current,'pass',who='ou_replay_other')['state'] == 'rejected'
        failed = click(current,'fail','隔离回放：标题看不懂')
        assert failed['accepted'] and failed['answered'] and failed['state'] == 'sent'
        again = click(current,'fail','隔离回放：标题看不懂',event_id='replay-fail-again')
        assert again['accepted'] and not again['answered']
        returned = visual.after(flow,item,target_visual,services)
        assert returned.rerun and returned.reason == '亲眼看未通过'
        emit('fail_click_returns', {'version':newer,'after':asdict(returned),
                                    'answer':visual.answer(issue_io.read(item,target_visual))})

        newest = version + '-replay-pass'
        current = next_version(newest)
        passed = click(current,'pass')
        assert passed['accepted'] and passed['answered'] and passed['answer'] == '过'
        assert visual.after(flow,item,target_visual,services) is None
        emit('pass_click_closes', {'version':newest,'delivery':current['delivery'],'allowed':True})

        watch = {**rules['targets']['workshop'],'name':'workshop'}
        settings = dict(watch['watch_updates']); watch['watch_updates'] = settings
        for key in ('watchlist','needs','sources'):
            source = Path(settings[key]).expanduser()
            destination = root / 'watch-data' / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source,destination); settings[key] = str(destination)
        settings.update(state_dir=str(root/'watch-state'), reports=str(root/'reports'),
                        data_repo=str(root/'watch-data'), snapshots=str(root/'watch-data/snapshots'))
        Path(settings['reports']).mkdir(exist_ok=True)
        copied_report = {**report, 'path':str(Path(settings['reports'])/Path(report['path']).name)}
        shutil.copy2(Path(report['path']).expanduser(),copied_report['path'])
        report_state['reports'][args.report] = copied_report
        io = WatchIO(); io.services=services
        io.save(watch,report_state)
        io.send_report(watch,copied_report)
        io.send_report(watch,copied_report)
        event = Event('cli:feishu',args.feedback,'workshop','check','真实私聊反馈只读回放',{})
        flow = WatchFlow(io)
        classification = flow.classify(event,None,watch,lambda **kw:{'id':'workshop:watch_updates:'+kw['object_key']})
        assert not classification.discard
        item = {'id':'workshop:watch_updates:'+classification.object_key,'object_key':classification.object_key,
                'materials':[{'source':event.source,'ext_id':event.ext_id,'body':event.body}]}
        with io.locked(watch):
            state, check = flow.snapshot(item,watch)
            flow.adopt_feedback(item,watch,state,check)
            flow.consume_feedback(item,watch,state)
            io.save(watch,state)
            before = json.dumps(state,sort_keys=True)
            flow.consume_feedback(item,watch,state)
            assert json.dumps(state,sort_keys=True) == before
        matching = [{'version':key,'instance':place,'feedback':value} for key,current in state['versions'].items()
                    for place,value in current.get('feedback',{}).items() if value.get('message_id')==args.feedback]
        assert len(matching)==1 and matching[0]['instance']=='dc2-sentry' and matching[0]['feedback']['decision']=='升'
        emit('real_private_feedback', {'classification':asdict(classification),'report':args.report,
            'message':args.feedback,'matched':matching,'unanswered_is_approval':False,'upgrade_calls':0})
        with sqlite3.connect(root/'inbox.db') as db:
            calls = db.execute('SELECT action,key,payload FROM calls').fetchall()
            inbox = db.execute('SELECT key,payload FROM inbox').fetchall()
        # 三版亲眼看卡、两张「已收到」、一份报告。
        assert sorted(json.loads(p)['kind'] for _,p in inbox) == ['update_report','visual','visual','visual','visual_receipt','visual_receipt']
        for key,payload in inbox:
            sent = [c for c in calls if c[0]=='send' and c[1]==key]
            assert len(sent)==1
            data=json.loads(payload)
            if data['kind']=='update_report':
                assert data['artifact'].endswith('.html') and 'summary' not in data and 'text' not in data
            if data['kind']=='visual':
                card = data['card']
                shown = json.dumps([card['header'], *(e for e in card['body']['elements'] if e['tag']!='collapsible_panel')],
                                   ensure_ascii=False)
                folded = json.dumps(card['body']['elements'][-1], ensure_ascii=False)
                assert 'text' not in data and data['version'] not in shown and key not in shown
                assert card['body']['elements'][-1]['expanded'] is False and data['version'] in folded and key in folded
                if data['version'] == version:
                    emit('visual_card', {'key':key, 'version':data['version'], 'card':card})
            if data['kind']=='visual_receipt':
                emit('visual_receipt', {'key':key, 'card':data['card']})
        emit('inbox', {'records':[{'key':k,'payload':json.loads(p)} for k,p in inbox],
            'calls':[{'action':a,'key':k} for a,k,_ in calls], 'real_feishu_sends':0, 'new_requests':0})
    finally:
        process.terminate(); process.wait(timeout=10)
    emit('passed', {'output':str(root),'runtime':str(runtime)})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authorization-child',type=Path)
    parser.add_argument('--rules',type=Path,default=Path('rules/workshop.yaml'))
    parser.add_argument('--output',type=Path)
    parser.add_argument('--state',type=Path)
    parser.add_argument('--batch',type=Path)
    parser.add_argument('--grant-client',type=Path)
    parser.add_argument('--service-python',type=Path)
    parser.add_argument('--issue',type=Path)
    parser.add_argument('--pr',type=int)
    parser.add_argument('--report',default='wu-daily-2026-11-18-v2')
    parser.add_argument('--feedback',default='om_x100b60ec8b5e7ca0dfc5adda0a64553')
    parser.add_argument('--denied',default='req-1794980414-3d32bd')
    parser.add_argument('--expired',default='req-1793798209-e21f1b')
    args=parser.parse_args()
    if args.authorization_child:
        authorization_child(args.authorization_child)
    else:
        verify(args)


if __name__=='__main__':
    main()
