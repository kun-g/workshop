#!/usr/bin/env python3
"""#173 隔离回放：已读真实来源，写标签仅记账；真实服务/执行者交回执与续接。"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import uuid

from workshop.core.engine import Engine
from workshop.core.log import Log
from workshop.core.models import Event, FlowBrief
from workshop.core.rules import Rules
from workshop.core.wake import Mailbox
from workshop.plugins.intervention.client import Client
from workshop.plugins.issue_delivery import IssueDelivery
from workshop.plugins.issue_delivery import decisions
from workshop.plugins.issue_delivery.io import IO
from workshop.registry import Registry
from workshop.reports.report import status, why

ROOT = Path(__file__).resolve().parents[1]


def show(name, data):
    print(json.dumps({'step': name, 'result': data}, ensure_ascii=False, default=str), flush=True)


class RecordingIO(IO):
    """唯一替换的外部写边界；来源数据与需求文件来自真实 GitHub 的只读结果。"""
    def __init__(self, target, directory, original):
        super().__init__(gh=target['github_identity']['gh'], state_dir=directory)
        self.original = original
        self.overlay = None
        self.writes = []

    def api(self, endpoint, *, data=None):
        if data is not None:
            if endpoint.endswith('/labels') and data == {'labels': ['Ready for Agent']}:
                self.writes.append({'endpoint': endpoint, 'data': data})
                return [{'name': 'Ready for Agent'}]
            raise AssertionError('隔离回放禁止其它 GitHub 写入：' + endpoint)
        if self.overlay is not None and endpoint.endswith('/issues/173'):
            return deepcopy(self.overlay)
        return super().api(endpoint)


@contextmanager
def service_process(directory, rules):
    directory.mkdir(mode=0o700, parents=True)
    endpoint = directory / 'service.sock'
    reader, writer = os.pipe()
    with (directory / 'process.log').open('w') as output:
        process = subprocess.Popen([sys.executable, '-m', 'workshop.plugins.intervention', '--state-dir', str(directory),
            '--rules', str(rules), '--endpoint', str(endpoint), 'serve', '--ready-fd', str(writer)],
            pass_fds=(writer,), stdout=output, stderr=output)
        os.close(writer)
        try:
            ready, _, _ = select.select([reader], [], [], 15)
            if not ready or os.read(reader, 64) != b'ready\n':
                raise RuntimeError('隔离服务未启动；看 process.log')
            yield endpoint
        finally:
            os.close(reader)
            process.terminate()
            process.wait(timeout=10)


def failures(output, rules_path):
    """真实 socket、独立服务规则与调用方编码错误；不替换 Client。"""
    import yaml
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    rules = yaml.safe_load(rules_path.read_text())
    valid = output / 'valid.yaml'
    valid.write_text(yaml.safe_dump(rules, allow_unicode=True))
    value = {'key': 'failure', 'version': 'v1', 'stage': '执行', 'question': 'A 还是 B',
             'reason': '隔离验收', 'recommend': 'A'}
    with service_process(output / 'service', valid) as endpoint:
        client = Client()
        cfg = {'endpoint': str(endpoint), 'timeout': 1}
        for name, data, config in [('rejected', {k:v for k,v in value.items() if k != 'recommend'}, cfg),
                                  ('unavailable', value, {**cfg, 'endpoint': str(output / 'missing.sock')})]:
            result = decisions.after(None, {'id': 'replay:failure'}, name, {}, lambda _: (client, config), data)
            show(name, result.__dict__)
            assert bool(result.blocked) == (name == 'rejected')
            assert result.pending == (name == 'unavailable')
        try:
            decisions.after(None, {'id': 'replay:failure'}, 'caller-error', {}, lambda _: (client, cfg),
                            {**value, 'caller_bug': {'not-json'}})
        except TypeError as exc:
            show('caller-error', {'type': type(exc).__name__, 'error': str(exc), 'translated': False})
        else:
            raise AssertionError('调用方错误被吞掉')
        old = client.create('version:v1', {'point': 'decision', 'owner': 'replay:version', 'content': value}, cfg)
        new = client.create('version:v2', {'point': 'decision', 'owner': 'replay:version',
                            'content': {**value, 'version': 'v2', 'question': '更新后的问题'}}, cfg)
        from workshop.core.ports import WaitRejected
        try:
            client.invoke('answer', {'operation': 'answer-old', 'ref': old, 'value': 'A',
                'source': {'channel': 'session', 'session': 'replay', 'quote': '历史回答的隔离副本', 'version': 'v1'}}, cfg)
        except WaitRejected as exc:
            show('superseded-answer', {'old_ref': old, 'new_ref': new, 'error': str(exc)})
        else:
            raise AssertionError('旧问题答案未拒绝')
    rules['intervention']['points']['decision']['wait'] = 'yes'
    bad = output / 'bad.yaml'
    bad.write_text(yaml.safe_dump(rules, allow_unicode=True))
    argv = [sys.executable, '-m', 'workshop.plugins.intervention', '--rules', str(bad),
            '--state-dir', str(output / 'bad-state'), '--endpoint', str(output / 'bad.sock'), 'serve']
    result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    show('bad-rule-startup', {'argv': argv, 'exit_code': result.returncode,
                            'stdout': result.stdout, 'stderr': result.stderr})
    assert result.returncode and 'intervention.points.decision.wait' in result.stderr


class ReplayFlow(IssueDelivery):
    """只替换验收任务的派活文字；判回执、等待接线和恢复沿用现役流程实现。"""
    def __init__(self, io, workdir):
        super().__init__(io)
        self.workdir = workdir
        self.prompts = []

    def brief(self, item, stage, target):
        records = self.io.read(item, target).get('decisions', {})
        round_number = len(item.get('history', []))
        example = ({'outcome': 'nothing', 'summary': '隔离验收：登记无卡取舍',
                    'evidence': [json.dumps({'schema': 'issue-delivery/v1', 'decision': {
                        'key': 'replay-path', 'version': 'v1', 'question': '回放：两条路径先做哪条',
                        'reason': '验证会话回答只恢复这件活，不产生真实授权', 'recommend': 'A'}}, ensure_ascii=False)],
                    'leave': {'kind': '等你', 'about': '选择路径'}} if not records else
                   {'outcome': 'nothing', 'summary': '已读问题和会话回答，约好再看',
                    'evidence': [json.dumps({'schema': 'issue-delivery/v1', 'received_answer': list(records.values())}, ensure_ascii=False)],
                    'later': [{'key': 'look-again', 'at': '+1s' if round_number == 1 else '+1d', 'about': '隔离验收到点续跑'}]})
        prompt = ('这是 workshop #173 的隔离验收。不要改代码，不要对外写入，不要调用工具，不要继续父 issue 的实施。'
                  '你是真实执行者，只验证交回执协议。只在最终回复输出下面这份 JSON，包装进程会交给隔离车间。'
                  '问题与已登记回答在 materials 中，属于历史真实原话的隔离回放，不代表新增授权。\n' + json.dumps(example, ensure_ascii=False))
        self.prompts.append({'round': round_number, 'records': records})
        return FlowBrief(prompt, workdir=str(self.workdir), readonly=True,
                         materials=[{'source': 'intervention-replay', 'body': v} for v in records.values()])


def replay(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    rules = Rules.load(args.rules)
    original = next(Event(**e) for e in json.loads(args.source.read_text())['events']
                    if e['body'].get('kind') == 'issue' and str(e['body'].get('number')) == '173')
    original_issue = original.body['data']
    target = rules.target('workshop')
    io = RecordingIO(target, output / 'flow', original_issue)
    target = {**target, 'sources': [], 'issue_delivery_state_dir': str(output / 'flow')}
    config = deepcopy(rules.data)
    config['targets'] = {'workshop': {**target, 'flows': {'issue_delivery': target['flows']['issue_delivery']}}}
    config['executors'] = {role: {'use': args.executor} for role in ('writer', 'reviewer', 'acceptor')}
    config['stage_executors'] = {}
    config['targets']['workshop']['selector'] = 'ordered'
    config['limits']['concurrent'] = 1
    config['wait_services']['intervention']['endpoint'] = str(output / 'service/service.sock')
    import yaml
    rule_path = output / 'rules.yaml'
    rule_path.write_text(yaml.safe_dump(config, allow_unicode=True))
    actual_rules = Rules.load(rule_path)
    os.environ['WORKSHOP_EXECUTOR_STATE_DIR'] = str(output / 'executors')
    registry = Registry()
    flow = ReplayFlow(io, ROOT)
    registry.register('flow', 'issue_delivery', flow)
    log = Log(output / 'core/workshop.db')
    engine = Engine(log, actual_rules, registry)
    try:
        # 同一真实来源的三个隔离正文版本；不声称这些改动存在于远端。
        import re
        # 原正文是改版（ws#286）前的旧写法；写全的那份加上「约束」节，缺项那份删掉它。
        full = original_issue['body'].rstrip('\n') + '\n\n## 约束\n无\n'
        variants = [('missing', re.sub(r'## 约束\n.*?(?=\n## |\Z)', '', full, flags=re.S)),
                    ('missing-quote', re.sub(r'## Kun 的原话\n.*?(?=\n## |\Z)', '## Kun 的原话\n', full, flags=re.S)),
                    ('complete', full)]
        for n, (name, body) in enumerate(variants):
            io.overlay = {**original_issue, 'body': body, 'labels': []}
            before = len(io.writes)
            event = replace(original, ext_id='replay:' + name, body={**original.body, 'data': io.overlay, 'text': body})
            engine.ingest(event)
            engine.classify_events()
            created = engine.log.get('items', 'workshop:173') is not None
            show('intake-' + name, {'labels_recorded': io.writes[before:], 'item_created': created,
                                    'remote_written': False, 'overlay': True})
            assert created == (name == 'complete')
        # 补齐原来缺节的正文：另一个独立状态库，源对象仍为同一个真实 issue。
        io.overlay = {**original_issue, 'body': full, 'labels': []}
        io2 = RecordingIO(target, output / 'patched-flow', original_issue)
        io2.overlay = io.overlay
        patched = IssueDelivery(io2).classify(replace(original, ext_id='patched'), None, target, None)
        show('intake-patched', {'action': patched.action, 'labels_recorded': io2.writes, 'remote_written': False})
        assert patched.action == 'ready'
        io.overlay = None
        engine.change(log.get('items', 'workshop:173'), stage='执行', checks=[['scripts/check.sh']], paths=[])
        with service_process(output / 'service', rule_path) as endpoint, Mailbox(output / 'core', [endpoint.with_suffix('.changed')]) as mailbox:
            client = Client()
            cfg = actual_rules.data['wait_services']['intervention']
            show('service', client.invoke('ping', {}, cfg))
            deadline = time.monotonic() + args.timeout
            answered = False
            expiry_injected = False
            while time.monotonic() < deadline:
                engine.tick()
                item = log.get('items', 'workshop:173')
                rows = log.all('rounds')
                if item['state'] == '卡住' or engine.flow_broken('workshop', 'issue_delivery'):
                    raise RuntimeError(item.get('blocked_by') or item.get('next'))
                if item['state'] == '等你' and not answered:
                    show('status-waiting', status(log, rules=actual_rules.data, plugins=registry))
                    assert engine.occupied() == 0
                    wait = log.get('waits', item['wait_id'])
                    record = client.read(wait['ref'], cfg)
                    show('wait-record', record)
                    # 用 #173 已登记的历史原话回放；不向生产新增问答。
                    session = '10d910bd-0dfd-4785-97d0-dee39ce5883b'
                    assert '全A' in full and session in full
                    argv = [sys.executable, '-m', 'workshop.cli', '--state-dir', str(output / 'core'),
                            'answer-wait', wait['ref'], 'A', '--version', 'v1', '--quote', '全A', '--session', session]
                    for name in ('answer', 'answer-duplicate'):
                        result = subprocess.run(argv, text=True, capture_output=True)
                        show(name, {'argv': argv, 'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
                        assert result.returncode == 0
                    wrong = argv.copy(); wrong[wrong.index('--version') + 1] = 'old'
                    result = subprocess.run(wrong, text=True, capture_output=True)
                    show('answer-stale', {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
                    assert result.returncode != 0
                    engine.awaken(type('Wake', (), {'paths': {endpoint.with_suffix('.changed')}, 'messages': []})(), engine.wake_paths())
                    answered = True
                # 第二轮按原会话接回，退出后把本验收专用的厂商会话号改成不存在的号，验证下一轮自动新开。
                ended = [r for r in rows if r['status'] == 'ended']
                if len(ended) == 2 and not expiry_injected:
                    executor = registry.get('executor', args.executor)
                    spec = json.loads((executor.directory(ended[-1]['token']) / 'spec.json').read_text())
                    vendor = Path(spec['session_file'])
                    data = json.loads(vendor.read_text())
                    data['vendor_id'] = str(uuid.uuid4())
                    vendor.write_text(json.dumps(data))
                    # recover_session 会从上一轮输出重建 vendor.json；同步注入该隔离副本，保留原始输出。
                    transcript = executor.directory(ended[-1]['token']) / 'output.log'
                    original_output = transcript.read_text()
                    transcript.with_name('output-before-injection.log').write_text(original_output)
                    lines = []
                    for line in original_output.splitlines():
                        try:
                            row = json.loads(line)
                        except ValueError:
                            lines.append(line)
                            continue
                        if row.get('type') == 'thread.started':
                            row['thread_id'] = data['vendor_id']
                        lines.append(json.dumps(row, ensure_ascii=False))
                    transcript.write_text('\n'.join(lines) + '\n')
                    expiry_injected = True
                    show('expired-session-injected', {'local_session_file': str(vendor)})
                if len(ended) >= 3:
                    directory = registry.get('executor', args.executor).directory(ended[-1]['token'])
                    spec = json.loads((directory / 'spec.json').read_text())
                    recovery = directory / 'session-recovery.json'
                    show('fresh-session', {'fresh': spec['fresh_session'],
                         'recovery': json.loads(recovery.read_text()) if recovery.exists() else None})
                    assert spec['fresh_session'] and recovery.exists()
                    show('rounds', [{k:r.get(k) for k in ('token','executor','session','status','stopped','summary','usage')} for r in ended])
                    show('resumed-count', sum(f['kind'] == 'wait_resumed' for f in log.facts()))
                    show('sends', log.all('cards'))
                    show('why', why(log, 'workshop:173', actual_rules.data, registry))
                    show('prompts', flow.prompts)
                    assert not log.all('cards') and engine.occupied() == 0
                    assert sum(f['kind'] == 'wait_resumed' for f in log.facts()) == 1
                    break
                mailbox.wait(1)
            else:
                raise TimeoutError('真实执行者回放超过总期限')
    finally:
        for row in engine.active():
            engine.abort(row['token'])
        log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rules', type=Path, default=ROOT / 'rules/workshop.yaml')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--failures-only', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executor', default='codex', help='唯一规则中现有的订阅执行者配置档')
    parser.add_argument('--timeout', type=int, default=480)
    args = parser.parse_args()
    if args.failures_only:
        failures(args.output.resolve(), args.rules)
    else:
        if args.source is None:
            parser.error('--source 必填')
        replay(args)


if __name__ == '__main__':
    main()
