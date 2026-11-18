#!/usr/bin/env python3
"""#175 隔离进程验证：真实订阅执行者、守护进程、看门、介入点；送达仅本地。"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

import yaml
from workshop.core.log import Log
from workshop.core.rules import Rules
from workshop.core.managers import Store
from workshop.core.models import Brief, Event, stamp
from workshop.core.jobs import write
from workshop.plugins.executors.local.adapters import Executor
from workshop.plugins.intervention.client import request
from workshop.core.wake import ring

ROOT = Path(__file__).resolve().parents[1]


def emit(step, value):
    print(json.dumps({'step': step, **value}, ensure_ascii=False), flush=True)


def wait(predicate, seconds=120):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        select.select([], [], [], .1)
    raise TimeoutError('条件未达成：' + str(predicate))


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def transport(root):
    m = json.load(sys.stdin)
    with sqlite3.connect(root / 'inbox.db') as db:
        db.execute('CREATE TABLE IF NOT EXISTS inbox(key TEXT PRIMARY KEY,payload TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS calls(action TEXT,key TEXT)')
        db.execute('INSERT INTO calls VALUES (?,?)', (m['action'], m['key']))
        exists = db.execute('SELECT 1 FROM inbox WHERE key=?', (m['key'],)).fetchone()
        if m['action'] == 'send':
            db.execute('INSERT INTO inbox VALUES (?,?)', (m['key'], json.dumps(m['payload'], ensure_ascii=False)))
            exists = True
    print(json.dumps({'state': 'sent' if exists else 'absent', 'external': 'local:' + m['key']}))


def program(root):
    directory = root / 'state/jobs/verification'
    with (directory / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # 用真实 issue 原文执行程序工作；FIFO 只拉长调用以便采样，不 sleep。
        digest = hashlib.sha256((root / 'issue.md').read_bytes()).hexdigest()
        write(directory / 'pid', os.getpid())
        (directory / 'ready').write_text(digest)
        with (root / 'program.gate').open('rb', buffering=0) as gate:
            gate.read(1)
        write(directory / 'result.json', {'ok': {'sha256': digest}, 'finished': time.time()})
    ring(root / 'state')


def verify(args):
    root = Path(tempfile.mkdtemp(prefix='ws175-', dir='/tmp')).resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'runtime.txt').write_text(str(root))
    state = root / 'state'
    state.mkdir()
    (root / 'run').mkdir(mode=0o700)
    (root / 'issue.md').write_text(args.issue.read_text())
    config = yaml.safe_load((ROOT / 'rules/workshop.yaml').read_text())
    target = config['targets']['workshop']
    # 暂停隔离目标的业务推进，仍走真实主循环和项目经理支路；来源不访问远端。
    config['targets'] = {'workshop': {'plugin': 'issue_delivery', 'sources': [], 'repo': 'kun-g/workshop',
        'checkout': str(ROOT), 'github_identity': {'mode': 'inherit'}}}
    config['managers']['manual'] = str(ROOT / 'docs/项目经理.md')
    config['managers']['candidates'] = ['manager-codex']
    profile = config['executor_profiles']['manager-codex']
    profile['settings']['sandbox_workspace_write']['writable_roots'] = [str(root)]
    profile['prompt'] += ' 本轮是隔离诊断演练，只能读本轮状态目录和资料；不得运行生产命令，不修改生产状态，不访问远端，不发消息。确认本次受控故障已核对后交 resolved；不要等待外部条件。'
    endpoint = root / 'run/service.sock'
    config['wait_services']['intervention'] = {'endpoint': str(endpoint), 'timeout': 5}
    config['intervention']['transport'] = [sys.executable, str(Path(__file__).resolve()), '--transport', str(root)]
    config['intervention']['status']['command'] = [sys.executable, '-m', 'workshop.reports.manager_status', '--state-dir', str(state)]
    rulepath = root / 'rules.yaml'
    rulepath.write_text(yaml.safe_dump(config, allow_unicode=True))
    env = dict(os.environ, WORKSHOP_STATE_DIR=str(state), WORKSHOP_EXECUTOR_STATE_DIR=str(root / 'executors'))
    for key in ('WORKSHOP_ROUND_TOKEN','WORKSHOP_RULES'):
        env.pop(key, None)
    processes = []
    def spawn(argv, name, ready=False):
        reader, writer = os.pipe() if ready else (None, None)
        if ready:
            argv = [*argv, '--ready-fd', str(writer)]
        with (root / (name + '.stdout')).open('ab') as out, (root / (name + '.stderr')).open('ab') as err:
            child = subprocess.Popen(argv, env=env, cwd=root, stdout=out, stderr=err,
                                     pass_fds=(writer,) if ready else (), start_new_session=True)
        processes.append(child)
        if ready:
            os.close(writer)
            try:
                assert select.select([reader], [], [], 15)[0] and os.read(reader, 64) == b'ready\n'
            finally:
                os.close(reader)
        emit('process', {'name': name, 'argv': argv, 'pid': child.pid})
        return child
    def stop(child):
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=15)
    def start_service():
        return spawn([sys.executable, '-m', 'workshop.plugins.intervention', '--state-dir', str(root / 'service'),
                      '--rules', str(rulepath), '--endpoint', str(endpoint), 'serve'], 'service', ready=True)
    def start_daemon():
        return spawn([sys.executable, '-m', 'workshop.cli', '--state-dir', str(state), '--rules', str(rulepath), 'run'], 'daemon')
    def heartbeat():
        return load(state / 'heartbeat.json', {})
    def records():
        s = Store(state)
        try:
            return s.all()
        finally:
            s.close()
    def call(action, data):
        value = request(endpoint, action, data, timeout=30)
        emit(action, {'response': value})
        return value
    def pm_fact(key, value):
        item_id = key[5:]
        log = Log(state / 'workshop.db')
        try:
            if not log.get('items', item_id):
                body = asdict(Event('replay', key, 'workshop', item_id.split(':',1)[1], value['title'], args.issue.read_text(), action='ready'))
                log.append('event', body, item_id)
            log.append('item_changed', {'state':'卡住', 'blocked_by': value['reason'] + '；原文 ' + str(root/'issue.md'), 'next':'隔离诊断，核对后交 resolved'}, item_id)
        finally:
            log.close()
        ring(state)
    def help_record(key, total=0, revision=1, active=True, state_value='blocked', idle=None):
        return {'id': key, 'episode': key, 'revision': revision, 'active': active, 'state': state_value,
                'facts': [], 'attempts': [], 'business': {'total': total, 'idle_since': idle or '2026-11-18T00:00:00+00:00'},
                'conclusion': {'schema': 'manager/v1', 'result': 'blocked', 'action': '恢复订阅登录',
                    'reason': '隔离回放中登录被撤下', 'evidence': '受控配置下现有订阅均返回不可用；没有按量调用', 'status_location': '处理不了'}}
    def update(row):
        return call('manager_update', {'operation': f"verify:{row['id']}:{row['revision']}", 'record': row})
    normal = None
    executor = None
    try:
        service = start_service()
        log = Log(state / 'workshop.db')
        log.append('control', {'id': 'workshop', 'paused': True})
        log.close()
        daemon = start_daemon()
        wait(heartbeat)
        emit('empty', {'heartbeat': heartbeat()})
        # 真实订阅登录只读校验；不打印凭据。
        executor = Executor('manager-codex', state_dir=root / 'executors', profile=profile)
        assert executor.available(profile), '真实订阅不可用'
        emit('identity', {'subscription_available': True, 'billing': profile['billing'], 'binary': executor.commands['codex']})
        pm_fact('item:workshop:replay-a', {'title': '核对车间项目经理', 'reason': '验证隔离账本与真实 issue 原文；核对后可记 resolved', 'source': str(root / 'issue.md')})
        row = wait(lambda: next((r for r in records() if r['id']=='item:workshop:replay-a' and r['state']=='running'), None))
        token, session = row['token'], row['started']['session']
        emit('real_manager', {'record': row, 'heartbeat': heartbeat()})
        assert heartbeat()['business']['total'] == 0
        accepted = executor.inform(token, __import__('workshop.core.models', fromlist=['Started']).Started(**row['started']), 'real:new-fact', 'issue 原文位于 ' + str(root / 'issue.md'))
        emit('real_inform', {'accepted': accepted, 'transport': 'headless', 'fallback': '退出后带新事实 resume'})
        pm_fact('item:workshop:replay-a', {'title': '核对车间项目经理', 'reason': '第二条事实：状态目录已经固定，无需用户动作', 'source': str(root / 'issue.md')})
        pm_fact('item:workshop:replay-b', {'title': '核对空闲口径', 'reason': '只核对心跳 business 排除项目经理；这是隔离回放，记 resolved 即可'})
        wait(lambda: len([r for r in records() if r.get('token')]) >= 2)
        emit('parallel', {'records': records(), 'ordinary_active': heartbeat()['active']})
        assert heartbeat()['active'] == 0
        # 核心停掉后用真实系统 Python 看门接管；现役守护进程不动。
        stop(daemon)
        command = [sys.executable, '-m', 'workshop.manager_cli', '--state-dir', str(state), '--rules', str(rulepath)]
        watch_argv = ['/usr/bin/python3', '-I', str(ROOT / 'src/workshop/watch/__init__.py'), '--state-dir', str(state), '--manager-command', json.dumps(command)]
        result = subprocess.run(watch_argv, env=env, capture_output=True, text=True, timeout=40)
        emit('takeover', {'argv': watch_argv, 'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr, 'records': records()})
        assert result.returncode == 0
        assert next(r for r in records() if r['id']=='item:workshop:replay-a')['token'] == token
        daemon = start_daemon()
        wait(lambda: heartbeat().get('process', {}).get('pid') == daemon.pid)
        # 重启核对同 token；新的系统故障会另有事项，不和原事项抢会话。
        emit('restored', {'records': records(), 'heartbeat': heartbeat()})
        finished = wait(lambda: next((r for r in records() if r['id']=='item:workshop:replay-a' and r['state']=='resolved'), None), 240)
        assert finished.get('last_session') == session and finished['generation'] >= 2
        emit('resumed_and_resolved', {'record': finished})
        # 一次真实普通 agent 运行：本地校验材料，借 FIFO 保持采样窗口。
        # 非项目经理 token，真实本地执行者，始终暂停该目标的阶段推进。
        os.mkfifo(root / 'agent.gate')
        normal_token = 'verification-business-agent'
        normal_dir = root / 'normal'; normal_dir.mkdir()
        normal_profile = {**profile, 'prompt':'这是本地运行采样。按本轮说明计算材料散列并通过 FIFO 等待采样，不访问远端或生产状态。'}
        brief = Brief('这是本地运行计数验证。执行一次 Python：读取 '+str(root/'issue.md')+' 算 SHA256，写 '+str(root/'agent.ready')+'，随后打开 '+str(root/'agent.gate')+' 读一个字节。不得访问远端或生产状态。', [], workdir=str(normal_dir), readonly=True)
        target = {'runtime': {'command': command, 'env': {'WORKSHOP_STATE_DIR': str(state)}}, 'github_identity': {'mode':'inherit'}}
        normal = executor.launch(normal_token, brief, {'id':'workshop:verification', 'target':'workshop', 'object_key':'verification'}, normal_profile, target)
        log = Log(state / 'workshop.db')
        body = asdict(Event('replay','business','workshop','verification','核对真实资料', args.issue.read_text(), action='ready'))
        log.append('event', body, 'workshop:verification')
        log.append('round_reserved', {'token':normal_token,'item':'workshop:verification','stage':'执行','executor':'manager-codex', 'config':{**normal_profile,'family':'codex'}, 'target':{**Rules.load(rulepath).target('workshop'),**target}, 'session':normal.session, 'status':'running', 'at':stamp(datetime.now(timezone.utc)), 'deadline':stamp(datetime.now(timezone.utc)+timedelta(hours=1)), 'started':asdict(normal), 'receipt':None, 'brief':asdict(brief)}, 'workshop:verification')
        log.close(); ring(state)
        wait(lambda: heartbeat().get('business', {}).get('agents') == 1)
        assert not load(state/'heartbeat.json',{}).get('delivery_failed_count')
        emit('business_agent', {'heartbeat': heartbeat(), 'started': asdict(normal)})
        stop(daemon)
        executor.stop(normal_token, normal)
        normal = None
        log = Log(state / 'workshop.db')
        log.append('round_changed', {'token':normal_token,'status':'ended','stopped':True,'ended':stamp(datetime.now(timezone.utc))}, 'workshop:verification')
        log.append('item_changed', {'state':'完成','round':None}, 'workshop:verification')
        log.close()
        job = state / 'jobs/verification'; job.mkdir(parents=True)
        write(job/'spec.json', {'port':'flow','method':'assess','name':'verification','item':'workshop:verification'})
        os.mkfifo(root / 'program.gate')
        child = spawn([sys.executable, str(Path(__file__).resolve()), '--program', str(root)], 'program')
        wait(lambda: (job/'ready').exists())
        daemon = start_daemon()
        wait(lambda: heartbeat().get('business', {}).get('jobs') == 1)
        emit('business_program', {'heartbeat': heartbeat(), 'sha256': (job/'ready').read_text(), 'pid': child.pid})
        with (root/'program.gate').open('wb', buffering=0) as stream: stream.write(b'1')
        child.wait(timeout=10)
        wait(lambda: heartbeat().get('business', {}).get('total') == 0)
        emit('idle_origin', {'heartbeat': heartbeat()})
        # fake clock 只供判定五分钟，不需要在真实进程上空等五分钟。
        message = root / 'clock.json'
        idle = heartbeat()['business']['idle_since']
        for seconds in (299, 300):
            message.write_text(json.dumps({'at':(datetime.fromisoformat(idle)+timedelta(seconds=seconds)).isoformat(), 'business':heartbeat()['business'], 'faults':{},'takeover':False}))
            result = subprocess.run([*command,'tick','--file',str(message)],env=env,capture_output=True,text=True,timeout=30)
            emit('idle_threshold', {'seconds':seconds, 'exit_code':result.returncode, 'idle_registered': any(r['id']=='idle' for r in records())})
            assert result.returncode == 0
            assert any(r['id']=='idle' for r in records()) == (seconds == 300)
        # 确定发送/取消/重启用真实服务，本地送达，不动飞书。
        waiting = call('create', {'operation':'click-wait','point':'decision','owner':'workshop:replay-a',
            'content':{'key':'subscription','version':'1','stage':'执行','question':'选择可用订阅','reason':'隔离等待验证','recommend':'使用现有订阅'}})
        log = Log(state/'workshop.db')
        log.append('wait_bound', {'id':'click-wait','ref':waiting['ref'],'provider':'intervention','item':'workshop:replay-a','state':'waiting','stage':'执行'}, 'workshop:replay-a')
        log.close()
        help_a = help_record('help-a'); help_a['conclusion']['status_location']='等你'
        update(help_a); update(help_record('help-b'))
        call('manager_flush', {})
        deliveries = []
        with sqlite3.connect(root/'service/intervention.db') as db:
            deliveries = [json.loads(r[0]) for r in db.execute("SELECT body FROM records WHERE kind='delivery'")]
        sent = next(r for r in deliveries if r['payload'].get('primary')=='help-a')
        stop(service); service = start_service()
        update(help_record('help-c')); call('manager_flush', {})
        # 创建真实持久队列后再取消，包含进程重启窗口。
        p = call('permit', {'operation':'cancel-permit','point':'manager_help','scope':'incident:cancel','owner':'cancel','basis':help_record('cancel')['conclusion']})
        d = call('enqueue', {'operation':'cancel-enqueue','permit':p['ref'],'payload':{'kind':'manager_help','text':'不要发送'}})
        call('cancel', {'operation':'cancel-now','ref':p['ref'],'reason':'事项已自动解决'})
        assert call('dispatch', {'ref':d['ref']})['state']=='cancelled'
        click = {'event':{'operator':{'open_id':config['intervention']['status']['recipient']}, 'context':{'open_chat_id':config['intervention']['status']['chat'],'open_message_id':sent['external']}, 'action':{'tag':'button','value':{'ns':'workshop-status/v1','delivery':sent['id'],'section':'等你'}}}}
        before = (state/'workshop.db').read_bytes()
        reply = call('status_click', {'operation':'click-kun','event':click})
        foreign = json.loads(json.dumps(click)); foreign['event']['operator']['open_id']='someone-else'
        assert call('status_click', {'operation':'click-other','event':foreign})['state']=='rejected'
        assert reply['state']=='sent'
        from workshop.reports.report import overview, status
        from workshop.reports.manager_status import sections, HEADINGS
        from workshop.reports.status_page import page
        from workshop.registry import Registry
        log = Log.readonly(state/'workshop.db')
        try:
            view = overview(log, plugins=Registry())
            terminal = status(log, plugins=Registry())
            html = page(view)
            assert reply['snapshot']['lines'] == sections(view)['等你']['lines']
            emit('status_sections', {'terminal_headings':[h for h in HEADINGS if h in terminal.splitlines()],
                 'html_headings':[h for h in HEADINGS if '<h2>'+h+'</h2>' in html],
                 'same_lines':sections(view)['等你']['lines']})
            (args.output/'status.txt').write_text(terminal)
            (args.output/'status.html').write_text(html)
        finally: log.close()
        emit('click_readonly', {'database_unchanged':before==(state/'workshop.db').read_bytes(), 'snapshot':reply['snapshot']})
        stop(daemon)
        offline = call('status_click', {'operation':'click-offline','event':click})
        assert '读不到车间状态' in offline['snapshot']['lines'][0]
        emit('offline', {'snapshot':offline['snapshot']})
        with sqlite3.connect(root/'inbox.db') as db:
            inbox = [dict(key=k,payload=json.loads(p)) for k,p in db.execute('SELECT key,payload FROM inbox')]
        helps = [r for r in inbox if r['payload']['kind']=='manager_help']
        assert len(helps)==1
        emit('inbox', {'messages':inbox, 'help_count':len(helps)})
        (args.output/'card.json').write_text(json.dumps(helps[0]['payload']['card'], ensure_ascii=False, indent=2))
        emit('complete', {'runtime':str(root),'production_restarted':False,'feishu_sends':0})
    finally:
        if normal is not None:
            executor.stop('verification-business-agent', normal)
        for child in reversed(processes):
            stop(child)
        # 只收本次隔离环境的真实执行者，保留所有账本和证据。
        for specfile in (root/'executors/manager-codex/rounds').glob('*/spec.json'):
            spec = load(specfile)
            if spec and executor:
                executor.stop(spec['token'], None)
        (args.output/'runtime.txt').write_text(str(root))


def verify_unavailable(args):
    root = Path(tempfile.mkdtemp(prefix='ws175-unavailable-', dir='/tmp')).resolve()
    (root/'run').mkdir(mode=0o700)
    config = yaml.safe_load((ROOT/'rules/workshop.yaml').read_text())
    config['targets'] = {'workshop': {'plugin':'issue_delivery','sources':[],'github_identity':{'mode':'inherit'}}}
    needs = args.needs_action
    names = ['manager-codex'] if needs else ['unavailable-one','unavailable-two']
    for name in names:
        home = root/name; home.mkdir()
        config['executor_profiles'][name] = {**config['executor_profiles']['manager-codex'], **({} if needs else {'config_dir':str(home)})}
        config['executor_profiles'][name]['settings'] = {'sandbox_workspace_write': {'network_access':True,'writable_roots':[str(root)]}}
        if needs:
            config['executor_profiles'][name]['prompt'] += ' 本轮只检查当前隔离状态目录。不得访问生产状态或远端，不创建业务、不开单、不发消息。当前隔离目录没有业务或需求来源；请查实际状态形成结论。'
    config['managers'].update(candidates=names,manual=str(ROOT/'docs/项目经理.md'))
    endpoint=root/'run/service.sock'
    config['wait_services']['intervention']={'endpoint':str(endpoint),'timeout':5}
    config['intervention']['transport']=[sys.executable,str(Path(__file__).resolve()),'--transport',str(root)]
    path=root/'rules.yaml';path.write_text(yaml.safe_dump(config,allow_unicode=True))
    state=root/'state';state.mkdir()
    log=Log(state/'workshop.db')
    log.append('control',{'id':'@rules','path':str(path),'data':config})
    log.close()
    env={**os.environ,'WORKSHOP_STATE_DIR':str(state),'WORKSHOP_EXECUTOR_STATE_DIR':str(root/'executors')}
    env.pop('WORKSHOP_ROUND_TOKEN',None)
    reader,writer=os.pipe()
    command=[sys.executable,'-m','workshop.plugins.intervention','--state-dir',str(root/'service'), '--rules',str(path),'--endpoint',str(endpoint),'serve','--ready-fd',str(writer)]
    with (root/'stdout').open('w') as out, (root/'stderr').open('w') as err:
        service=subprocess.Popen(command,env=env,stdout=out,stderr=err,pass_fds=(writer,),start_new_session=True)
    os.close(writer)
    try:
        assert select.select([reader],[],[],15)[0] and os.read(reader,64)==b'ready\n'
        began=datetime.now(timezone.utc)
        message=root/'input.json'
        found=None
        for step in range(400 if needs else 40):
            message.write_text(json.dumps({'at':(datetime.now(timezone.utc) if needs else began+timedelta(seconds=step*10)).isoformat(), 'business':{'total':0,'idle_since':began.isoformat()}, 'faults':{'verification':{'reason':'本隔离目录没有业务，也没有需求来源；请检查并判断是否需要新需求' if needs else '受控空登录目录，验证禁止使用按量账户'}},'takeover':False}))
            argv=[sys.executable,'-m','workshop.manager_cli','--state-dir',str(state),'--rules',str(path),'tick','--file',str(message)]
            result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=30)
            assert result.returncode==0,result.stderr
            store=Store(state)
            try: found=next((r for r in store.all() if r['id']=='system:verification' and r['state']==('blocked' if needs else 'unavailable')),None)
            finally: store.close()
            if found:
                response=request(endpoint,'manager_flush',{})
                if any(r['state']=='sent' for r in response['deliveries']):break
        assert found and (needs or len(found['attempts'])==2)
        request(endpoint,'manager_flush',{})
        with sqlite3.connect(root/'inbox.db') as db:
            count=db.execute('SELECT count(*) FROM inbox').fetchone()[0]
        assert count==1
        started=len(list((root/'executors').glob('*/rounds/*/spec.json')))
        assert started == (1 if needs else 0)
        if needs: assert found['conclusion']['action']=='车间空了，要新需求'
        emit('real_needs_action' if needs else 'real_unavailable',{'runtime':str(root),'record':found,'inbox_messages':count,'agent_processes_started':started,'metered_calls':0,'argv':argv,'exit_code':result.returncode})
        args.output.mkdir(parents=True,exist_ok=True)
        (args.output/('needs-action-runtime.txt' if needs else 'unavailable-runtime.txt')).write_text(str(root))
    finally:
        os.close(reader)
        os.killpg(service.pid,signal.SIGTERM);service.wait(timeout=10)
        for name in names:
            executor=Executor(name,state_dir=root/'executors',profile=config['executor_profiles'][name])
            for specfile in (root/'executors'/name/'rounds').glob('*/spec.json'):
                spec=load(specfile)
                if spec:executor.stop(spec['token'],None)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transport',type=Path)
    parser.add_argument('--program',type=Path)
    parser.add_argument('--unavailable',action='store_true')
    parser.add_argument('--needs-action',action='store_true')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--issue',type=Path)
    args=parser.parse_args()
    if args.transport: transport(args.transport)
    elif args.program: program(args.program)
    elif args.unavailable or args.needs_action: verify_unavailable(args)
    else: verify(args)


if __name__=='__main__':
    main()
