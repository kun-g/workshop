#!/usr/bin/env python3
"""隔离状态库中的真实来源回放；只结束本脚本启动的进程，不发消息。"""
import argparse
from dataclasses import fields
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from workshop.core.engine import Engine
from workshop.core.log import Log
from workshop.core.models import After, Event
from workshop.deployment import assemble
from workshop.core.wake import Mailbox, Wake
from workshop.core import waits
from workshop.registry import Registry
from workshop.plugins.intervention.client import request


FAULT_SERVER = '''
import os,sys
from workshop.plugins.intervention.service import Service
from workshop.plugins.intervention.server import serve
original = Service.call
def boundary(self, action, data):
    result = original(self, action, data)
    if action == sys.argv[5]:
        os._exit(91)  # 服务已经提交，故意不把确认交给客户端。
    return result
Service.call = boundary
serve(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]))
'''


def core(args):
    config = assemble(args.rules).rules
    config.data['wait_services']['intervention']['endpoint'] = str(args.runtime / 'run/service.sock')
    log = Log(args.runtime / 'core/workshop.db')
    engine = Engine(log, config, Registry())
    fact = json.loads(args.answer.read_text())
    source = fact['body']
    item_id = source['target'] + ':' + source['object_key']
    if args.core == 'bind':
        if not log.get('items', item_id):
            engine.ingest(Event(**{f.name: source[f.name] for f in fields(Event) if f.name in source}), directed=True)
            issue = json.loads(args.issue.read_text())
            engine.ingest(Event('github', f"{issue['html_url']}:{issue['updated_at']}", source['target'],
                                source['object_key'], issue['title'], issue, action='update'))
        item = log.get('items', item_id)
        with log.transaction():
            changes = engine.pending_changes(item, After(pending=True, reason='回放真实放行等待', next='原阶段核验',
                wait={'provider': 'intervention', 'ref': args.ref, 'operation': 'verify:bind:172'}), {},
                {'key': 'verify:after:172', 'receipt': {}, 'to': '说清楚', 'complete': False})
            engine.change(item, **changes)
        if args.crash:
            os._exit(92)  # 核心暂停已提交，但调用方没有收到确认。
    else:
        if args.woken:
            changed = args.runtime / 'run/service.changed'
            engine.awaken(Wake(paths={changed}), engine.wake_paths())
        waits.reconcile(engine)
        if args.crash:
            os._exit(93)  # 恢复已提交，尚未确认给独立服务。
    item = log.get('items', item_id)
    print(json.dumps({'state': item['state'], 'stage': item['stage'], 'approved': item['approved'],
                      'waits': log.all('waits')}, ensure_ascii=False))
    log.close()


def verify(args):
    args.output.mkdir(parents=True, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix='ws172-', dir='/tmp')).resolve()
    (runtime / 'run').mkdir(mode=0o700)
    (runtime / 'core').mkdir(mode=0o700)
    endpoint = runtime / 'run/service.sock'
    process = None
    transcript = []
    def report(name, value):
        entry = {'step': name, 'result': value}
        transcript.append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
    def launch(fault=''):
        reader, writer = os.pipe()
        with (runtime / 'service.log').open('ab') as output:
            child = subprocess.Popen([str(args.service_python), '-I', '-c', FAULT_SERVER,
                str(runtime / 'service'), str(args.rules), str(endpoint), str(writer), fault],
                pass_fds=(writer,), cwd=runtime, stdout=output, stderr=output)
        os.close(writer)
        try:
            readable, _, _ = select.select([reader], [], [], 15)
            if not readable or os.read(reader, 64) != b'ready\n':
                child.kill(); child.wait(timeout=10)
                raise RuntimeError((runtime / 'service.log').read_text())
        finally:
            os.close(reader)
        return child
    def stop():
        if process and process.poll() is None:
            process.kill(); process.wait(timeout=10)
    def worker(action, ref='', crash=False, woken=False):
        argv = [sys.executable, str(Path(__file__).resolve()), '--rules', str(args.rules),
            '--issue', str(args.issue), '--answer', str(args.answer), '--runtime', str(runtime), '--core', action]
        if ref:
            argv += ['--ref', ref]
        if crash:
            argv += ['--crash']
        if woken:
            argv += ['--woken']
        done = subprocess.run(argv, capture_output=True, text=True, timeout=20)
        report('core:' + action, {'argv': argv, 'exit_code': done.returncode, 'stdout': done.stdout, 'stderr': done.stderr})
        expected = (92 if action == 'bind' else 93) if crash else 0
        assert done.returncode == expected
        return json.loads(done.stdout) if done.stdout else None
    def lost_reply(action, data):
        try:
            request(endpoint, action, data)
        except (OSError, ValueError):
            pass
        else:
            raise AssertionError('应在确认前退出')
        assert process.wait(timeout=10) == 91
        report(action + ':committed_without_reply', {'exit_code': 91})
    issue = json.loads(args.issue.read_text())
    fact = json.loads(args.answer.read_text())
    source = fact['body']['body']
    assert source['channel'] == 'session' and source['quote'] and source['session']
    create = {'operation': 'verify:create:172', 'point': 'requirement', 'owner': 'workshop:172',
              'content': {'question': '是否开始本单', 'issue': issue['html_url'], 'title': issue['title'],
                          'replay': '回放已登记的真实放行；不向生产新增问题'}}
    try:
        isolated = subprocess.run([str(args.service_python), '-I', '-c',
            'import importlib.util; import workshop.plugins.intervention.service as s; '
            'assert importlib.util.find_spec("workshop.core") is None; print(s.__file__)'],
            cwd=runtime, capture_output=True, text=True, check=True)
        report('independent_install', {'stdout': isolated.stdout, 'core_available': False,
                                      'issue': issue['html_url'], 'answer_fact': fact['seq'], 'quote': source['quote']})
        with Mailbox(runtime / 'core', [endpoint.with_suffix('.changed')]) as mailbox:
            process = launch('create')
            lost_reply('create', create)
            process = launch()
            ref = request(endpoint, 'create', create)['ref']
            assert request(endpoint, 'create', create)['ref'] == ref
            report('create_replay', {'ref': ref})
            worker('bind', ref, crash=True)
            paused = worker('bind', ref)
            assert paused['state'] == '等你' and paused['stage'] == '接单'
            stop()
            paused = worker('reconcile', woken=True)
            assert paused['state'] == '等你' and paused['stage'] == '接单'
            process = launch('answer')
            response = {'operation': 'verify:answer:172', 'ref': ref, 'value': source['quote'], 'source': source}
            lost_reply('answer', response)
            process = launch()
            token = request(endpoint, 'answer', response)['token']
            wake = mailbox.wait(5)
            assert endpoint.with_suffix('.changed') in wake.paths
            report('file_event', {'paths': sorted(map(str, wake.paths))})
            worker('reconcile', crash=True, woken=True)
            stop()
            process = launch('ack')
            unconfirmed = worker('reconcile', woken=True)
            assert process.wait(timeout=10) == 91
            assert unconfirmed['waits'][0]['state'] == 'resumed'
            report('ack:committed_without_reply', {'exit_code': 91})
            process = launch()
            resumed = worker('reconcile', woken=True)
            assert resumed['state'] == '空闲' and resumed['stage'] == '接单' and not resumed['approved']
            assert request(endpoint, 'get', {'ref': ref})['acknowledged'] is True
            again = worker('reconcile')
            assert again == resumed
            report('result', {'ref': ref, 'token': token, 'stage': resumed['stage'], 'approved': [], 'acknowledged': True})
            # 普通完成/进度没有许可；也没有配置真实发送通道。
            for point in ('progress', 'completed'):
                try:
                    request(endpoint, 'permit', {'operation': 'verify:deny:' + point, 'point': point,
                        'scope': 'incident:172', 'owner': 'workshop:172', 'basis': {}})
                except ValueError:
                    report('denied', {'point': point, 'sent': 0})
                else:
                    raise AssertionError('未登记介入点不得有许可')
            stop()
            # 正常独立入口在主环境不可导入、核心库不存在时仍能重启服务。
            hidden = runtime / 'core-offline'
            (runtime / 'core').rename(hidden)
            try:
                started = subprocess.run([str(args.service_python), '-I', '-m', 'workshop.plugins.intervention',
                    '--state-dir', str(runtime / 'service'), '--rules', str(args.rules), '--endpoint', str(endpoint), 'start'],
                    cwd=runtime, capture_output=True, text=True, timeout=20, check=True)
                identity = json.loads(started.stdout)
                report('independent_restart_core_offline', identity)
                assert request(endpoint, 'get', {'ref': ref})['token'] == token
                os.kill(identity['started'], 15)
            finally:
                hidden.rename(runtime / 'core')
        with sqlite3.connect(runtime / 'service/intervention.db') as db:
            kinds = dict(db.execute('SELECT kind,count(*) FROM journal GROUP BY kind'))
            assert kinds == {'created': 1, 'answered': 1, 'acknowledged': 1}
        with sqlite3.connect(runtime / 'core/workshop.db') as db:
            counts = dict(db.execute("SELECT kind,count(*) FROM facts WHERE kind IN ('wait_bound','wait_resumed') GROUP BY kind"))
            assert counts == {'wait_bound': 1, 'wait_resumed': 1}
        report('counts', {'plugin': kinds, 'core': counts})
        report('limitations', {'production_switched': False, 'live_message_sent': False,
                               'mode': '真实 App 来源及已登记回答的隔离回放，不是新的生产授权'})
    finally:
        stop()
        (args.output / 'transcript.json').write_text(json.dumps(transcript, ensure_ascii=False, indent=2))
        (args.output / 'runtime.txt').write_text(str(runtime))
        for source, name in [(runtime / 'service/intervention.db', 'intervention.db'),
                             (runtime / 'core/workshop.db', 'workshop.db')]:
            if source.exists():
                with sqlite3.connect(source) as original, sqlite3.connect(args.output / name) as copy:
                    original.backup(copy)
        shutil.copy2(args.issue, args.output / 'issue.json')
        shutil.copy2(args.answer, args.output / 'answer-fact.json')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rules', type=Path, required=True)
    parser.add_argument('--issue', type=Path, required=True)
    parser.add_argument('--answer', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--service-python', type=Path)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--core', choices=['bind', 'reconcile'])
    parser.add_argument('--ref', default='')
    parser.add_argument('--crash', action='store_true')
    parser.add_argument('--woken', action='store_true')
    args = parser.parse_args()
    for name in ('rules', 'issue', 'answer', 'output', 'service_python', 'runtime'):
        path = getattr(args, name)
        if path:
            setattr(args, name, path.resolve())
    # 不 resolve 虚拟环境解释器的软链接：那会越过虚拟环境。
    if args.service_python:
        raw = parser.parse_args().service_python
        args.service_python = raw.absolute()
    core(args) if args.core else verify(args)


if __name__ == '__main__':
    main()
