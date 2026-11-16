#!/usr/bin/env python3
"""真实服务 + argv 通道子进程的隔离送达验证；通道仅写本地验收收件箱。"""
import argparse
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
from workshop.plugins.intervention.client import request


def transport(root):
    """替代外部发送端，不替代 Service.transport/dispatch 的生产调用链。"""
    message = json.load(sys.stdin)
    scenario = message['payload']['scenario']
    plans = {
        'recovery': [('send', 'unknown'), ('reconcile', 'timeout'), ('reconcile', 'absent'), ('send', 'sent')],
        'interrupted': [('send', 'interrupt'), ('reconcile', 'sent')],
        'cancelled': [],
        'cancel_uncertain': [('send', 'unknown'), ('reconcile', 'absent')],
    }
    with sqlite3.connect(root / 'transport.db') as db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS calls (seq INTEGER PRIMARY KEY, scenario TEXT, action TEXT, key TEXT, reply TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS inbox (key TEXT PRIMARY KEY, scenario TEXT, payload TEXT)')
        count = db.execute('SELECT count(*) FROM calls WHERE scenario=?', (scenario,)).fetchone()[0]
        action, reply = plans[scenario][count]  # 多调用/重复发送直接失败，不掩盖错误。
        assert action == message['action'], (scenario, count, message['action'])
        db.execute('INSERT INTO calls(scenario,action,key,reply) VALUES (?,?,?,?)', (scenario, action, message['key'], reply))
        if reply in ('sent', 'interrupt') and action == 'send':
            db.execute('INSERT INTO inbox VALUES (?,?,?)', (message['key'], scenario, json.dumps(message['payload'], ensure_ascii=False)))
        if reply == 'sent' and action == 'reconcile':
            assert db.execute('SELECT count(*) FROM inbox WHERE key=?', (message['key'],)).fetchone()[0] == 1
        db.commit()
    if reply == 'interrupt':
        with (root / 'arrived').open('wb', buffering=0) as arrived:
            arrived.write(b'persisted\n')
    if reply in ('timeout', 'interrupt'):
        # 不 sleep；测试闸门阻止回执。生产 timeout 或进程组终止负责打断。
        fd = os.open(root / 'gate', os.O_RDWR)
        try:
            os.read(fd, 1)
        finally:
            os.close(fd)
        raise AssertionError('验收闸门不应被释放')
    print(json.dumps({'state': reply, **({'external': 'local-inbox:' + message['key']} if reply == 'sent' else {})}))


def verify(args):
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='ws172-send-', dir='/tmp')).resolve()
    (root / 'run').mkdir(mode=0o700)
    (root / 'rules').mkdir()
    os.mkfifo(root / 'arrived', 0o600)
    os.mkfifo(root / 'gate', 0o600)
    endpoint = root / 'run/service.sock'
    # 唯一规则的验收副本：只把 transport 接到隔离 argv；不部署、不修改规则目录。
    rules = yaml.safe_load(args.rules.read_text())
    rules['intervention']['transport'] = [str(args.service_python), '-I', str(Path(__file__).resolve()), '--transport', str(root)]
    rulepath = root / 'rules/workshop.yaml'
    rulepath.write_text(yaml.safe_dump(rules, allow_unicode=True, sort_keys=False))
    process = None
    transcript = []
    def report(step, result):
        entry = {'step': step, 'result': result}
        transcript.append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)
    def call(action, data):
        began = time.monotonic()
        result = request(endpoint, action, data, timeout=25)
        report(action, {'request': data, 'response': result, 'elapsed_seconds': round(time.monotonic() - began, 3)})
        return result
    def launch():
        reader, writer = os.pipe()
        argv = [str(args.service_python), '-I', '-m', 'workshop.plugins.intervention',
            '--state-dir', str(root / 'service'), '--rules', str(rulepath), '--endpoint', str(endpoint),
            'serve', '--ready-fd', str(writer)]
        with (root / 'service.stdout').open('ab') as stdout, (root / 'service.stderr').open('ab') as stderr:
            child = subprocess.Popen(argv, cwd=root, pass_fds=(writer,), stdout=stdout, stderr=stderr, start_new_session=True)
        os.close(writer)
        try:
            readable, _, _ = select.select([reader], [], [], 15)
            if not readable or os.read(reader, 64) != b'ready\n':
                child.kill(); child.wait(timeout=10)
                raise RuntimeError((root / 'service.stderr').read_text())
        finally:
            os.close(reader)
        report('service_started', {'pid': child.pid, 'argv': argv})
        return child
    def stop():
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    def queue(scenario):
        permit = call('permit', {'operation': 'verify:permit:' + scenario, 'point': 'manager_help',
            'owner': 'workshop:172', 'scope': 'incident:acceptance-172:' + scenario,
            'basis': {'action': '验收本地收件箱', 'reason': '隔离故障演练，不是真实求助',
                      'evidence': 'https://github.com/kun-g/workshop/issues/172', 'status_location': '隔离验收库'}})['ref']
        delivery = call('enqueue', {'operation': 'verify:enqueue:' + scenario, 'permit': permit,
            'payload': {'scenario': scenario, 'source': 'https://github.com/kun-g/workshop/issues/172',
                        'text': '隔离发送证据，不发飞书'}})['ref']
        return permit, delivery
    def snapshot():
        with sqlite3.connect(root / 'transport.db') as db:
            calls = [dict(zip(('seq', 'scenario', 'action', 'key', 'reply'), row)) for row in db.execute('SELECT * FROM calls ORDER BY seq')]
            inbox = [dict(zip(('key', 'scenario', 'payload'), row)) for row in db.execute('SELECT * FROM inbox ORDER BY rowid')]
        return {'calls': calls, 'inbox': inbox}
    try:
        meta = subprocess.run([str(args.service_python), '-I', '-c',
            'import json,sys,importlib.metadata; print(json.dumps({"python":sys.version,"wheel":importlib.metadata.version("workshop-intervention")}))'],
            capture_output=True, text=True, check=True)
        report('installation', {'stdout': meta.stdout, 'stderr': meta.stderr, 'exit_code': meta.returncode,
                                'state_dir': str(root / 'service'), 'rules': str(rulepath),
                                'transport_argv': rules['intervention']['transport']})
        process = launch()
        _, ref = queue('recovery')
        assert call('dispatch', {'ref': ref})['state'] == 'uncertain'  # unknown
        assert call('dispatch', {'ref': ref})['state'] == 'uncertain'  # 真正的 20 秒子进程超时
        assert call('get', {'ref': ref})['payload']['scenario'] == 'recovery'
        stop(); process = launch()
        assert call('get', {'ref': ref})['state'] == 'uncertain'
        assert call('dispatch', {'ref': ref})['state'] == 'sent'  # reconcile absent，才重发
        before = snapshot()
        assert call('dispatch', {'ref': ref})['state'] == 'sent'
        assert snapshot() == before
        report('recovery_counts', before)

        _, interrupted = queue('interrupted')
        reader = os.open(root / 'arrived', os.O_RDONLY | os.O_NONBLOCK)
        waiter = subprocess.Popen([str(args.service_python), '-I', '-c',
            'import sys,json; from workshop.plugins.intervention.client import request; '
            'print(json.dumps(request(sys.argv[1], "dispatch", {"ref":sys.argv[2]}, timeout=25)))',
            str(endpoint), interrupted], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            readable, _, _ = select.select([reader], [], [], 10)
            assert readable and os.read(reader, 64) == b'persisted\n'
            # 真实 argv 已落本地收件箱，服务记录还在 sending；终止整个验收进程组。
            stop()
            stdout, stderr = waiter.communicate(timeout=10)
            assert waiter.returncode != 0
            report('interrupted_client', {'exit_code': waiter.returncode, 'stdout': stdout, 'stderr': stderr})
        finally:
            os.close(reader)
            if waiter.poll() is None:
                waiter.kill(); waiter.communicate(timeout=10)
        process = launch()
        assert call('get', {'ref': interrupted})['state'] == 'sending'
        assert call('dispatch', {'ref': interrupted})['state'] == 'sent'
        before = snapshot()
        assert call('dispatch', {'ref': interrupted})['state'] == 'sent'
        assert snapshot() == before
        report('interrupted_counts', before)

        permit, cancelled = queue('cancelled')
        call('cancel', {'operation': 'verify:cancel:queued', 'ref': permit, 'reason': '已解决，取消隔离发送'})
        assert call('dispatch', {'ref': cancelled})['state'] == 'cancelled'
        permit, uncertain = queue('cancel_uncertain')
        assert call('dispatch', {'ref': uncertain})['state'] == 'uncertain'
        call('cancel', {'operation': 'verify:cancel:uncertain', 'ref': permit, 'reason': '已解决，先对账'})
        assert call('dispatch', {'ref': uncertain})['state'] == 'cancelled'
        records = snapshot()
        actions = {name: [x['action'] for x in records['calls'] if x['scenario'] == name]
                   for name in ('recovery', 'interrupted', 'cancelled', 'cancel_uncertain')}
        assert actions == {'recovery': ['send', 'reconcile', 'reconcile', 'send'],
                           'interrupted': ['send', 'reconcile'], 'cancelled': [], 'cancel_uncertain': ['send', 'reconcile']}
        counts = {name: sum(x['scenario'] == name for x in records['inbox']) for name in actions}
        assert counts == {'recovery': 1, 'interrupted': 1, 'cancelled': 0, 'cancel_uncertain': 0}
        report('delivery_result', {'actions': actions, 'delivered': counts, 'production_messages': 0, 'records': records})
    finally:
        stop()
        report('service_output', {'stdout': (root / 'service.stdout').read_text() if (root / 'service.stdout').exists() else '',
                                  'stderr': (root / 'service.stderr').read_text() if (root / 'service.stderr').exists() else ''})
        (args.output / 'transcript.json').write_text(json.dumps(transcript, ensure_ascii=False, indent=2))
        (args.output / 'runtime.txt').write_text(str(root))
        for source, name in [(root / 'service/intervention.db', 'intervention.db'), (root / 'transport.db', 'transport.db')]:
            if source.exists():
                with sqlite3.connect(source) as original, sqlite3.connect(args.output / name) as copy:
                    original.backup(copy)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--transport', type=Path)
    parser.add_argument('--rules', type=Path)
    parser.add_argument('--service-python', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.transport:
        transport(args.transport)
    else:
        args.rules, args.output = args.rules.resolve(), args.output.resolve()
        args.service_python = args.service_python.absolute()  # 保留 venv 解释器链接。
        verify(args)


if __name__ == '__main__':
    main()
