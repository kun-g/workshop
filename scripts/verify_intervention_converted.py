#!/usr/bin/env python3
"""#176 合并前 DoD 第 8、9 项：在转换后的现役副本上，先 workshop、后 grant-console，逐批走真实路径。

uv run python scripts/verify_intervention_converted.py --output <目录>

做法：
- 现役状态库、介入点库、项目经理库在线备份成副本（只读现役；复用 verify_intervention_cutover.Replay），
  切换前在副本上登记一件在途的活（一个在跑的程序作业、一条在等的等待），再用 convert_intervention.py cutover 转换，
  比较切换前后在跑的轮、程序作业和等待；
- 之后所有路径都在这份转换后的副本上走：真实 GitHub 只读（写只记账，见 verify_intervention_execution.RecordingIO），
  真实隔离介入点服务，真实订阅执行者（codex）轮次，真实守护进程和系统 Python 看门，授权中心只读已有申请；
- 送达命令写本地收件箱（verify_intervention_converted_inbox.py）；飞书、ask-kun、gh、orca、launchctl、kubectl
  换成只记录调用、退出 1 的替身（lark-cli 只放行读一条私聊消息的 GET）；
- 第 8 项有路径失败就不跑第 9 项；走不了的路径写明原因，不用替身冒充。
不碰现役守护进程、现役介入点服务和现役 launchd，不写现役状态目录，不新建授权申请，不发飞书，不在 GitHub 上写。
"""
import argparse
from contextlib import suppress, contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
HOME = Path.home()
LIVE = HOME / '.local/state/workshop'
LIVE_SERVICE = HOME / '.local/state/workshop-intervention'
# #173 已登记的 Kun 原话和会话号（issue #173 正文「Kun 的原话」），只作历史原话的隔离回放，不代表新的授权。
HISTORY_ISSUE, HISTORY_QUOTE, HISTORY_SESSION = 173, '全A', '10d910bd-0dfd-4785-97d0-dee39ce5883b'
DECISION = {'key': 'replay-path', 'version': 'v1', 'question': '回放：两条路径先做哪条',
            'reason': '验证会话回答只恢复这件活，不产生真实授权', 'recommend': 'A'}
# #174 用过的真实私聊报告反馈（只读回放）。
REPORT, FEEDBACK = 'wu-daily-2026-11-18-v2', 'om_x100b60ec8b5e7ca0dfc5adda0a64553'
ALLOWED_KINDS = ('visual', 'visual_receipt', 'update_report', 'manager_help', 'status_reply')
GUARD = '''#!{python}
# 隔离回放的 lark-cli 守门：只放行读一条私聊消息（盯更新反馈回源核验身份），其余只记录、退出 1。
import os, re, sys
argv = sys.argv[1:]
ok = (len(argv) == 7 and argv[0] == '--profile' and argv[2:4] == ['api', 'GET']
      and re.fullmatch(r'/open-apis/im/v1/messages/om_[A-Za-z0-9_]+', argv[4]) and argv[5:] == ['--as', 'bot'])
with open({log!r}, 'a') as f:
    f.write('lark-cli' + ('（只读放行）' if ok else '') + '\\t' + ' '.join(argv) + '\\n')
if ok:
    # 去掉守门自己所在的目录再交给下一个 lark-cli（agent 记账包装会按 PATH 找下一个，不去掉就绕回这里）。
    here = os.path.dirname(os.path.realpath(__file__))
    env = dict(os.environ, PATH=os.pathsep.join(d for d in os.environ.get('PATH', '').split(os.pathsep)
                                                if d and os.path.realpath(d) != here))
    os.execve({real!r}, [{real!r}, *argv], env)
sys.exit(1)
'''


def load(name):
    """按路径复用前几单的验证脚本（不复制代码）。"""
    spec = importlib.util.spec_from_file_location('_w176_' + Path(name).stem, SCRIPTS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def emit(step, value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1, default=str)
    print(f'\n----- {step} -----\n{text.rstrip()}', flush=True)


def until(predicate, seconds, what):
    """查条件加总超时（E8，不 sleep）。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        select.select([], [], [], .2)
    raise TimeoutError(f'{seconds} 秒内没等到：{what}')


class NotWalkable(Exception):
    """这条路径在转换后的副本上走不了；写明原因，不冒充。"""


class Stop(BaseException):
    """--stop-after：走完指定路径就停（复跑某一段用）。"""


class Paths:
    def __init__(self, stop_after=None):
        self.results = []
        self.stop_after = stop_after

    @contextmanager
    def walk(self, code, title):
        print(f'\n===== [{code}] {title} =====', flush=True)
        try:
            yield
        except NotWalkable as exc:
            self.results.append((code, title, '没走', str(exc)))
            print(f'[{code}] 没走：{exc}', flush=True)
        except Exception as exc:
            self.results.append((code, title, '失败', f'{type(exc).__name__}: {exc}'))
            print(f'[{code}] 失败：\n{traceback.format_exc()}', flush=True)
        else:
            self.results.append((code, title, '走通', ''))
            print(f'[{code}] 走通', flush=True)
        if code == self.stop_after:
            raise Stop(code)

    def failed(self, prefix):
        return [r for r in self.results if r[0].startswith(prefix) and r[2] == '失败']


# ---------- 副本和环境 ----------

def backup(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def prepare(cutover, output):
    replay = cutover.Replay(output, 'converted')
    emit('副本：准备（verify_intervention_cutover.Replay.prepare）', replay.prepare())
    state = replay.state
    # 项目经理库：守护进程和看门在副本上接着用现役已有的事项（不起重复会话）。
    backup(LIVE / 'managers.db', state / 'managers.db')
    # 盯更新的状态和报告（只读复制）；授权批次只复制原同步检查点（production.json 等）。
    shutil.copytree(LIVE / 'watch_updates', state / 'watch_updates', ignore=shutil.ignore_patterns('*.lock'))
    batches = LIVE / 'grant-console-release/batches'
    latest = max((p for p in batches.glob('*.json') if (p.with_suffix('') / 'deploy/result.json').exists()
                  and json.loads((p.with_suffix('') / 'deploy/result.json').read_text()).get('grant', {}).get('id')),
                 key=lambda p: p.stat().st_mtime)
    copy = state / 'grant-console-release/batches'
    (copy / latest.stem / 'deploy').mkdir(parents=True)
    shutil.copy2(latest, copy / latest.name)
    for name in ('production.json', 'result.json', 'spec.json', 'approval.json', 'runs.json'):
        if (latest.with_suffix('') / 'deploy' / name).exists():
            shutil.copy2(latest.with_suffix('') / 'deploy' / name, copy / latest.stem / 'deploy' / name)
    # 外部命令：codex、claude 用真的（订阅执行者和项目经理）；其余换成只记录的替身。
    bin_dir = replay.root / 'bin-live'
    bin_dir.mkdir()
    real_lark = shutil.which('lark-cli')
    for tool in ('ask-kun', 'gh', 'orca', 'launchctl', 'kubectl', 'cursor-agent'):
        (bin_dir / tool).write_text(cutover.RECORDER.format(log=replay.calls))
        (bin_dir / tool).chmod(0o755)
    (bin_dir / 'lark-cli').write_text(GUARD.format(python=sys.executable, log=str(replay.calls), real=real_lark or '/nonexistent'))
    (bin_dir / 'lark-cli').chmod(0o755)
    os.environ['PATH'] = f'{bin_dir}:{os.environ["PATH"]}'
    for key in ('WORKSHOP_ROUND_TOKEN', 'WORKSHOP_RULES', 'GH_TOKEN', 'GITHUB_TOKEN'):
        os.environ.pop(key, None)
    os.environ.update(WORKSHOP_STATE_DIR=str(state), WORKSHOP_EXECUTOR_STATE_DIR=str(replay.root / 'executors'))
    # 规则：同一份规则的副本，只改指向（副本状态、副本服务、本地收件箱），来源关掉，主检出指向不存在的目录（不收回现役 worktree）。
    data = yaml.safe_load(replay.rules.read_text())
    data['intervention']['transport'] = [sys.executable, str(SCRIPTS / 'verify_intervention_converted_inbox.py'), str(replay.root)]
    data['intervention']['status']['command'] = [sys.executable, '-m', 'workshop.reports.manager_status', '--state-dir', str(state)]
    # 真实守护进程要起状态页服务：副本给一个空闲的本机端口（Replay 填的 9 号端口要特权，现役的 8765 不碰）。
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        data['status_page'] = f'http://127.0.0.1:{probe.getsockname()[1]}/status.html'
    data['managers']['manual'] = str(ROOT / 'docs/项目经理.md')
    isolation = ' 本轮是转换后副本上的隔离验收：只读本轮状态目录和资料；不得运行生产命令，不修改生产状态，不访问远端，不发消息。确认受控事项已核对后交 resolved；不要等待外部条件。'
    for name in data['managers']['candidates']:
        data['executor_profiles'][name]['prompt'] += isolation
    data['executor_profiles']['manager-codex']['settings']['sandbox_workspace_write']['writable_roots'] = [str(replay.root)]
    for name, target in data['targets'].items():
        target['sources'] = {}
        target['checkout'] = str(replay.root / 'no-checkout' / name)
        target['issue_delivery_state_dir'] = str(state / 'issue_delivery')
        target['github_cache_dir'] = str(state / 'github-responses')
        for flow in target.get('flows', {}).values():
            if 'scheduled_release_state_dir' in flow:
                flow['scheduled_release_state_dir'] = str(state / ('grant-console-release' if name == 'grant-console' else ''))
    watch = data['targets']['workshop']['watch_updates']
    watch.update(state_dir=str(state / 'watch_updates'), reports=str(state / 'watch_updates/reports'),
                 data_repo=str(replay.root / 'watch-data'), snapshots=str(replay.root / 'watch-data/snapshots'))
    replay.rules.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    from workshop.core.log import Log
    log = Log(state / 'workshop.db')
    try:
        log.append('control', {'id': '@rules', 'data': data, 'path': str(replay.rules)})
    finally:
        log.close()
    emit('副本：环境', {'state': str(state), 'service_db': str(replay.service / 'intervention.db'), 'rules': str(replay.rules),
                     'endpoint': str(replay.endpoint), 'stub_bin': str(bin_dir), 'real_lark_for_GET_only': real_lark,
                     'batch_copied': latest.stem, 'managers_db': str(state / 'managers.db')})
    return replay, data


def child_env(replay):
    return {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}


def run(replay, argv, *, input=None, timeout=600):
    started = time.monotonic()
    result = subprocess.run(argv, cwd=ROOT, env=child_env(replay), capture_output=True, text=True, input=input, timeout=timeout)
    return result.returncode, (f'$ {" ".join(map(str, argv))}\nexit={result.returncode}（{time.monotonic() - started:.2f} 秒）\n'
                               f'{result.stdout}{result.stderr}')


def cli(replay, *argv, **kwargs):
    return run(replay, [sys.executable, '-m', 'workshop.cli', '--state-dir', str(replay.state), '--rules', str(replay.rules), *argv], **kwargs)


# ---------- 切换前后的在途记录 ----------

def inflight(replay):
    db = sqlite3.connect(f'file:{replay.state / "workshop.db"}?mode=ro', uri=True)
    try:
        rounds = {json.loads(b)['token']: json.loads(b) for (b,) in db.execute(
            "SELECT body FROM rounds WHERE json_extract(body,'$.status')!='ended'")}
        waits = {json.loads(b)['id']: json.loads(b) for (b,) in db.execute(
            "SELECT body FROM waits WHERE json_extract(body,'$.state') NOT IN ('acknowledged','cancelled')")}
        items = {json.loads(b)['id']: json.loads(b) for (b,) in db.execute(
            "SELECT body FROM items WHERE json_extract(body,'$.state') NOT IN ('完成','放弃')")}
    finally:
        db.close()
    from workshop.core.jobs import held, read
    jobs = {}
    root = replay.state / 'jobs'
    for path in sorted(root.iterdir()) if root.exists() else []:
        spec = read(path / 'spec.json')
        if spec and not (path / 'result.json').exists() and held(path):
            jobs[path.name] = {'spec': spec, 'pid': read(path / 'pid'), 'held': True}
    return {'rounds': rounds, 'jobs': jobs, 'waits': waits,
            'items': {k: {f: v.get(f) for f in ('stage', 'state', 'round', 'wait_id', 'next')} for k, v in items.items()}}


def seed_inflight(replay, managers, issue_body):
    """切换前在副本上登记一件在途的活：在跑的程序作业（verify_managers --program，FIFO 闸门）和在等的取舍等待。"""
    from workshop.core.log import Log
    from workshop.core.models import stamp, utcnow
    from workshop.plugins.intervention.client import Client
    item = 'workshop:cutover-inflight'
    log = Log(replay.state / 'workshop.db')
    at = stamp(utcnow())
    try:
        log.append('event', {'source': 'replay', 'ext_id': 'inflight', 'target': 'workshop', 'object_key': 'cutover-inflight',
                             'title': '隔离回放：切换前在途的活', 'body': {'kind': '隔离回放'}, 'depth': 0, 'action': 'ready',
                             'flow': '', 'initial_stage': '执行'}, item, source='replay', ext_id='inflight')
        config = {'endpoint': str(replay.endpoint), 'timeout': 5}
        operation = 'decision:' + item + ':inflight'
        ref = Client().create(operation, {'point': 'decision', 'owner': item, 'content': {
            'key': 'inflight', 'version': 'v1', 'stage': '执行', 'question': '隔离回放：切换前在等的取舍',
            'reason': '只用来比较切换前后等待是否保留', 'recommend': '保留'}}, config)
        log.append('wait_bound', {'id': operation, 'provider': 'intervention', 'ref': ref, 'operation': operation, 'item': item,
                                  'stage': '执行', 'state': 'waiting', 'token': None, 'attempt': 0, 'next_at': at},
                   item, source='wait-bind', ext_id=operation, at=at)
        log.append('item_changed', {'state': '等你', 'next': '等回答', 'leave': {'kind': '等你', 'about': '切换前在等的取舍'}}, item, at=at)
    finally:
        log.close()
    job = replay.state / 'jobs/verification'
    job.mkdir(parents=True, exist_ok=True)
    from workshop.core.jobs import write
    write(job / 'spec.json', {'port': 'flow', 'method': 'assess', 'name': 'issue_delivery', 'item': item, 'label': '隔离回放程序作业'})
    (replay.root / 'issue.md').write_text(issue_body)
    gate = replay.root / 'program.gate'
    if not gate.exists():
        os.mkfifo(gate)
    program = subprocess.Popen([sys.executable, str(SCRIPTS / 'verify_managers.py'), '--program', str(replay.root)],
                               env=child_env(replay), start_new_session=True,
                               stdout=(replay.root / 'program.log').open('a'), stderr=subprocess.STDOUT)
    until(lambda: (job / 'ready').exists(), 30, '程序作业起来')
    return {'item': item, 'wait_ref': ref, 'job': str(job), 'program_pid': program.pid}, program


def release_program(replay, program):
    with (replay.root / 'program.gate').open('wb', buffering=0) as gate:
        gate.write(b'1')
    program.wait(timeout=30)


def restart_program(replay, item):
    job = replay.state / 'jobs/verification'
    for name in ('result.json', 'ready', 'pid'):
        (job / name).unlink(missing_ok=True)
    from workshop.core.jobs import write
    write(job / 'spec.json', {'port': 'flow', 'method': 'assess', 'name': 'issue_delivery', 'item': item, 'label': '隔离回放程序作业'})
    program = subprocess.Popen([sys.executable, str(SCRIPTS / 'verify_managers.py'), '--program', str(replay.root)],
                               env=child_env(replay), start_new_session=True,
                               stdout=(replay.root / 'program.log').open('a'), stderr=subprocess.STDOUT)
    until(lambda: (job / 'ready').exists(), 30, '程序作业起来')
    return program


def compare(before, after):
    lines, same = [], True
    for kind in ('rounds', 'jobs', 'waits'):
        for key, value in before[kind].items():
            kept = after[kind].get(key) == value
            same &= kept
            lines.append(f'{kind}\t{key}\t切换前={value.get("status") or value.get("state") or ("在跑" if value.get("held") else "")}'
                         f'\t切换后={"相同" if kept else "变了：" + json.dumps(after[kind].get(key), ensure_ascii=False)[:300]}')
    for key, value in before['items'].items():
        if any(key == v.get('item') for kind in ('rounds', 'waits') for v in before[kind].values()) or any(
                key == j['spec'].get('item') for j in before['jobs'].values()):
            kept = after['items'].get(key) == value
            same &= kept
            lines.append(f'items\t{key}\t切换前={value}\t切换后={"相同" if kept else after["items"].get(key)}')
    counts = {k: len(before[k]) for k in ('rounds', 'jobs', 'waits')}
    return same, f'切换前在途：{counts}\n' + '\n'.join(lines)


# ---------- 进程内引擎（真实订阅执行者轮次） ----------

def engine_for(replay, data, registry, target, *, paused=()):
    from workshop.core.engine import Engine
    from workshop.core.log import Log
    from workshop.deployment import assemble
    config = deepcopy(data)
    config['executors'] = {role: [{'use': 'codex'}] for role in ('writer', 'reviewer', 'acceptor')}
    config['stage_executors'] = {}
    for name in config['targets']:
        config['targets'][name]['selector'] = 'ordered'
    config.pop('managers', None)
    path = replay.root / f'rules-engine-{target}.yaml'
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    log = Log(replay.state / 'workshop.db')
    for name in config['targets']:
        log.append('control', {'id': name, 'paused': name in paused})
    return Engine(log, assemble(path).rules, registry), log, path


def converted_flow(execution, replay, data, target_name):
    """issue 交付流程原样，只换派活文字（同 verify_intervention_execution.ReplayFlow 的做法）；GitHub 写只记账。"""
    from workshop.core.models import FlowBrief
    from workshop.plugins.issue_delivery import IssueDelivery
    target = {**data['targets'][target_name], 'name': target_name}

    class ConvertedFlow(IssueDelivery):
        def __init__(self):
            super().__init__(execution.RecordingIO(target, replay.state / 'issue_delivery', None))
            self.prompts = []
            self.fired = lambda item: False

        def brief(self, item, stage, target):
            if not item['id'].endswith(':dod-replay'):
                raise RuntimeError('隔离回放只给本回放的活派轮：' + item['id'])
            state = self.io.read(item, target)
            decisions = state.get('decisions', {})
            authorizations = state.get('authorizations', {})
            if not decisions:
                n, example = 1, {'outcome': 'nothing', 'summary': '隔离验收：登记无卡取舍',
                                 'evidence': [json.dumps({'schema': 'issue-delivery/v1', 'decision': DECISION}, ensure_ascii=False)],
                                 'leave': {'kind': '等你', 'about': '选择路径'}}
            elif not authorizations:
                n, example = 2, {'outcome': 'nothing', 'summary': '已读会话回答；交回授权中心原申请和检查点',
                                 'evidence': [json.dumps({'schema': 'issue-delivery/v1', 'authorization': self.authorization}, ensure_ascii=False)]}
            else:
                again = self.fired(item)
                n, example = (4 if again else 3), {
                    'outcome': 'nothing', 'summary': '原申请已核实；约好到点再看' if not again else '到点续跑；再约一天后看',
                    'evidence': [json.dumps({'schema': 'issue-delivery/v1', 'received_answer': list(decisions.values()),
                                             'authorization_checked': [r.get('verification') for r in authorizations.values()]}, ensure_ascii=False)],
                    'later': [{'key': 'look-again' if not again else 'look-tomorrow', 'at': '+1s' if not again else '+1d',
                               'about': '隔离验收到点续跑' if not again else '隔离验收一天后再看'}]}
            prompt = (f'这是 workshop #176 在转换后副本上的隔离验收（{item["id"]}，第 {n} 轮）。不要改代码，不要对外写入，不要调用工具，'
                      '不要继续任何 issue 的实施。你是真实执行者，只验证交回执协议。只在最终回复输出下面这份 JSON，包装进程会交给隔离车间。'
                      '问题、已登记回答和授权核实在 materials 中，属于历史真实原话和已有申请的隔离回放，不代表新增授权。\n'
                      + json.dumps(example, ensure_ascii=False))
            self.prompts.append({'item': item['id'], 'round': n})
            materials = [{'source': 'intervention-replay', 'body': v} for v in decisions.values()]
            materials += [{'source': 'authorization', 'body': v} for v in authorizations.values()]
            return FlowBrief(prompt, workdir=str(ROOT), readonly=True, materials=materials)

    return ConvertedFlow()


def gh_read(data, target_name, io, endpoint):
    """真实 GitHub 只读，用规则里这个交付对象的 App 身份。"""
    from workshop.github_identity import scoped
    return scoped(lambda target: io.api(endpoint))(target={**data['targets'][target_name], 'name': target_name})


def seed_item(replay, target, key, title, text, stage='执行'):
    from workshop.core.log import Log
    item = f'{target}:{key}'
    log = Log(replay.state / 'workshop.db')
    try:
        log.append('event', {'source': 'replay', 'ext_id': key, 'target': target, 'object_key': key, 'title': title,
                             'body': {'kind': '隔离回放', 'text': text}, 'depth': 0, 'action': 'ready', 'flow': '',
                             'initial_stage': stage}, item, source='replay', ext_id=f'{target}:{key}')
    finally:
        log.close()
    return item


def status_text(log, rules, registry):
    from workshop.reports.report import status
    return status(log, rules=rules.data, plugins=registry)


def walk_rounds(replay, data, execution, target_name, spec, timeout):
    """无卡等你 → 会话回答 → 授权原申请批准后接着做 → 到点自动续跑；真实 codex 订阅轮次。"""
    from workshop.core.wake import Mailbox
    from workshop.registry import Registry
    from workshop.plugins.intervention.client import Client
    from workshop.reports.report import why
    registry = Registry()
    flow = converted_flow(execution, replay, data, target_name)
    flow.authorization = spec
    registry.register('flow', 'issue_delivery', flow)
    other = [n for n in data['targets'] if n != target_name]
    engine, log, rules_path = engine_for(replay, data, registry, target_name, paused=other)
    results = {}
    try:
        item_id = f'{target_name}:dod-replay'
        if not log.get('items', item_id):
            issue = gh_read(data, target_name, flow.io, f'repos/{data["targets"][target_name]["repo"]}/issues/176')
            seed_item(replay, target_name, 'dod-replay', '隔离回放：' + issue['title'], issue['body'])
            engine.change(log.get('items', item_id), stage='执行', checks=[['scripts/check.sh']], paths=[])
        flow.fired = lambda item: any(t.get('fired') and t['item'] == item['id'] and t['id'].endswith(':look-again')
                                      for t in log.all('timers'))
        cfg = engine.rules.data['wait_services']['intervention']
        client = Client()
        answered = False
        authorized_seen = False
        deadline = time.monotonic() + timeout
        with Mailbox(replay.state, [replay.endpoint.with_suffix('.changed')]) as mailbox:
            while time.monotonic() < deadline:
                engine.tick()
                item = log.get('items', item_id)
                mine = [r for r in log.all('rounds') if r['item'] == item_id]
                ended = [r for r in mine if r['status'] == 'ended']
                if item['state'] == '卡住' or engine.flow_broken(target_name, 'issue_delivery'):
                    raise RuntimeError(f'{item_id} 卡住：{item.get("blocked_by")}；{item.get("next")}')
                if item['state'] == '等你' and not answered:
                    emit(f'{target_name}：等你时 workshop status（副本）', status_text(log, engine.rules, registry))
                    wait = log.get('waits', item['wait_id'])
                    emit(f'{target_name}：等待记录（介入点服务）', client.read(wait['ref'], cfg))
                    results['no_card_wait'] = {'wait': wait['ref'], 'cards': [c for c in log.all('cards') if c['item'] == item_id]}
                    argv = ['answer-wait', wait['ref'], 'A', '--version', 'v1', '--quote', HISTORY_QUOTE, '--session', HISTORY_SESSION]
                    for name in ('会话回答', '同一回答再登记一次'):
                        code, text = cli(replay, *argv)
                        emit(f'{target_name}：{name}', text)
                        assert code == 0
                    wrong = list(argv)
                    wrong[wrong.index('--version') + 1] = 'old'
                    code, text = cli(replay, *wrong)
                    emit(f'{target_name}：旧版本的回答被拒', text)
                    assert code != 0
                    engine.awaken(type('Wake', (), {'paths': {replay.endpoint.with_suffix('.changed')}, 'messages': []})(),
                                  engine.wake_paths())
                    answered = True
                state = flow.io.read(item, engine.item_target(item))
                if state.get('authorizations') and not authorized_seen:
                    record = next(iter(state['authorizations'].values()))
                    if record.get('verification'):
                        emit(f'{target_name}：授权中心原申请只读核实（流程 after，未新建申请）', record)
                        results['authorization'] = record['verification']
                        authorized_seen = True
                timers = [t for t in log.all('timers') if t['item'] == item_id]
                if len(ended) >= 4 and item['state'] == '定时':
                    break
                mailbox.wait(1)
            else:
                raise TimeoutError(f'{timeout} 秒内 {item_id} 没走完四轮')
        resumed = [f for f in log.facts() if f['kind'] == 'wait_resumed' and f.get('item') == item_id]
        rows = [{k: r.get(k) for k in ('token', 'executor', 'session', 'status', 'stopped', 'summary', 'outcome')} for r in mine]
        emit(f'{target_name}：本活各轮（真实 codex 订阅执行者）', rows)
        emit(f'{target_name}：本活定时（到点自动续跑）', [{k: t.get(k) for k in ('id', 'at', 'about', 'kind', 'owner', 'fired')} for t in timers])
        emit(f'{target_name}：why（副本）', why(log, item_id, engine.rules.data, registry))
        emit(f'{target_name}：派活记录', flow.prompts)
        results.update(rounds=len(ended), resumed=len(resumed), prompts=flow.prompts, writes=flow.io.writes,
                       timers_fired=[t['id'] for t in timers if t.get('fired')])
        assert answered and results['authorization']['result'] == 'approved'
        assert [p['round'] for p in flow.prompts][:4] == [1, 2, 3, 4], flow.prompts
        assert any(t.endswith(':look-again') for t in results['timers_fired'])
        assert not [c for c in log.all('cards') if c['item'] == item_id]
        return results
    finally:
        for row in engine.active():
            if row['item'].endswith(':dod-replay'):
                engine.abort(row['token'])
        log.close()


# ---------- 各条路径 ----------

def intake(replay, data, execution, target_name):
    """真实来源：读这个仓库所有开着的 issue，副本里没有活的逐个过真实接单（标签写只记账）。"""
    from dataclasses import replace
    from workshop.core.models import Event
    from workshop.registry import Registry
    registry = Registry()
    flow = converted_flow(execution, replay, data, target_name)
    registry.register('flow', 'issue_delivery', flow)
    # 归类只在交付对象没暂停时跑；只开本对象，另一个暂停。不 tick，不派轮。
    engine, log, _ = engine_for(replay, data, registry, target_name, paused=[n for n in data['targets'] if n != target_name])
    repo = data['targets'][target_name]['repo']
    try:
        pending = [e['id'] for e in log.all('effects', unless=('done', True)) if e.get('kind') == 'classify'
                   and e['event']['target'] == target_name]
        emit(f'{target_name}：副本里待归类的现役事件', pending)
        opened = [i for i in gh_read(data, target_name, flow.io, f'repos/{repo}/issues?state=open&per_page=100') if 'pull_request' not in i]
        rows, walked = [], None
        for issue in opened:
            item_id = f'{target_name}:{issue["number"]}'
            existing = log.get('items', item_id)
            row = {'issue': issue['number'], 'title': issue['title'], 'author': issue['user']['login'],
                   'labels': [l['name'] for l in issue['labels']], 'item_in_copy': (existing or {}).get('state')}
            if existing is None:
                before = len(flow.io.writes)
                body = {'kind': 'issue', 'repo': repo, 'number': issue['number'], 'data': issue, 'actor': '',
                        'author': issue['user']['login'], 'label': '', 'text': (issue.get('body') or '').strip(),
                        'edited': False, 'by_workshop': False}
                ext = f'{repo}:issue:{issue["number"]}:body'
                event = Event('github', f'{ext}:converted-replay', target_name, str(issue['number']), f'{repo} #{issue["number"]}',
                              body, action_id=ext, previous_ids=[ext])
                seq = log.db.execute('SELECT coalesce(max(seq),0) FROM facts').fetchone()[0]
                engine.ingest(event)
                engine.classify_events()
                created = log.get('items', item_id)
                facts = [(k, json.loads(b)) for k, b in log.db.execute(
                    "SELECT kind, body FROM facts WHERE seq>? AND kind IN ('event_discarded','event_classified')", (seq,))]
                row.update(labels_recorded=flow.io.writes[before:], item_created=bool(created),
                           priority=((created.get('relations') or {}).get('priority', 'P1')) if created else None,
                           classification=[{'kind': k, 'summary': b.get('summary'), 'action': b.get('action')} for k, b in facts])
                if created:
                    walked = row
            rows.append(row)
        emit(f'{target_name}：开着的 issue 逐个过真实接单（App 只读；标签写只记账）', rows)
        if walked:
            emit(f'{target_name}：自动打标签接单的活', {**walked, 'status': status_text(log, engine.rules, registry)})
            assert walked['labels_recorded'] and walked['priority'] == 'P1'
            return
        # 副本里写全了的开着的 issue 都已有活（现役接单过了），没有能新接的；只列现役接单的历史，不冒充。
        auto = [i for i in log.all('items') if i['target'] == target_name and i.get('object_key', '').isdigit()
                and any(r['item_in_copy'] and r['issue'] == int(i['object_key']) for r in rows)]
        emit(f'{target_name}：现役已接单的开着的 issue（副本里的历史，不是本次回放）',
             [{k: i.get(k) for k in ('id', 'stage', 'state')} | {'priority': (i.get('relations') or {}).get('priority', 'P1')} for i in auto])
        raise NotWalkable(f'{repo} 开着的 issue 里没有「写全了、还没标签、副本里也没有活」的：写全了的都已被现役接单（副本里已有活），'
                          '没接的都没写全；正向路径要一个新写全的 issue，或改写真实正文（不做）')
    finally:
        log.close()


def authorization_child(path):
    """新进程里只读查授权中心已有申请（同 #174 的做法），状态写进副本的 issue 交付目录。"""
    from workshop.plugins.issue_delivery import authorization
    from workshop.plugins.issue_delivery.io import IO
    data = json.loads(path.read_text())
    io = IO(state_dir=data['state_dir'])
    item = {'id': data['item']}
    result = authorization.after(io, item, data['target'], data['spec'])
    record = io.read(item, data['target'])['authorizations'][data['spec']['checkpoint']]
    print(json.dumps({'pid': os.getpid(), 'item': data['item'], 'request': data['spec']['request'], 'action': data['spec']['action'],
                      'after': asdict(result), 'record': record, 'new_requests': 0}, ensure_ascii=False))


def authorization_check(replay, data, target_name, item, spec, name):
    path = replay.root / f'authorization-{name}.json'
    target = {k: v for k, v in data['targets'][target_name].items() if k in ('authorization_client', 'authorization_url')}
    path.write_text(json.dumps({'state_dir': str(replay.state / 'issue_delivery'), 'item': item, 'target': target, 'spec': spec},
                               ensure_ascii=False))
    outputs = []
    for _ in range(2):
        code, text = run(replay, [sys.executable, str(Path(__file__).resolve()), '--authorization-child', str(path)])
        emit(f'{target_name}：{name}', text)
        assert code == 0
        outputs.append(json.loads(text.splitlines()[-1]))
    assert outputs[0]['pid'] != outputs[1]['pid'] and {o['request'] for o in outputs} == {spec['request']}
    return outputs


def pause_all(replay, data):
    from workshop.core.log import Log
    log = Log(replay.state / 'workshop.db')
    try:
        for name in data['targets']:
            log.append('control', {'id': name, 'paused': True})
    finally:
        log.close()


def find_visual_issue(io, repo, prefer=176):
    from workshop.plugins.issue_delivery import dod, visual
    candidates = [io.api(f'repos/{repo}/issues/{prefer}')]
    candidates += [i for i in io.api(f'repos/{repo}/issues?state=all&per_page=100&sort=updated') if 'pull_request' not in i]
    for issue in candidates:
        contract = dod.visual_contract(issue.get('body') or '')
        if all(contract.values()) and visual.supported(contract):
            return issue, contract
    return None, None


def visual_path(replay, data, execution, business, target_name, version):
    """亲眼看：同一约定只送一次（重派不重发），Kun 点「通过」归件到这次等待，回一张「已收到」。"""
    from workshop.plugins.intervention.client import Client, request
    from workshop.plugins.issue_delivery import visual
    target = {**data['targets'][target_name], 'name': target_name}
    io = execution.RecordingIO(target, replay.state / 'issue_delivery', None)
    from workshop.github_identity import scoped
    issue, contract = scoped(lambda target: find_visual_issue(io, target['repo']))(target=target)
    if issue is None:
        raise NotWalkable(f'{target["repo"]} 里找不到带可送约定（飞书私聊、按钮或引用回过/不对）的 issue')
    emit(f'{target_name}：亲眼看约定（真实 issue #{issue["number"]} 正文「要不要亲眼看」）', contract)
    item = {'id': f'{target_name}:dod-replay', 'stage': '收尾'}
    io.save(item, target, {**io.read(item, target), 'visual': True, 'merge': version, 'delivered': [version]})
    # 交付完成与否由交付流程判（要真合并上线）；这里同 #174 的做法，只把「已交付」交给亲眼看这一步。
    flow = SimpleNamespace(io=io, _delivery_done=lambda *a: True, _issue=lambda *a: issue)
    config = {'endpoint': str(replay.endpoint), 'timeout': 25}
    port = Client()
    services = lambda name: (port, config)
    first = visual.after(flow, item, target, services)
    second = visual.after(flow, item, target, services)
    entry = io.read(item, target)['visual_deliveries'][version]
    emit(f'{target_name}：亲眼看送达两次调用', {'first': asdict(first), 'second': asdict(second), 'entry': entry})
    assert first.wait and second.wait and first.wait['ref'] == second.wait['ref']
    with sqlite3.connect(replay.root / 'inbox.db') as db:
        card = json.loads(db.execute('SELECT payload FROM inbox WHERE key=?', (entry['delivery'],)).fetchone()[0])['card']
    buttons = [b for e in card['body']['elements'] for b in ([e] + e.get('elements', [])) if b.get('tag') == 'button']
    value = next(b['behaviors'][0]['value'] for b in buttons if b['behaviors'][0]['value']['choice'] == 'pass')
    status = data['intervention']['status']
    click = {'operation': f'workshop-click:converted-{target_name}', 'event': {'header': {'event_id': f'converted-{target_name}'}, 'event': {
        'operator': {'open_id': status['recipient']}, 'context': {'open_chat_id': status['chat'], 'open_message_id': entry['external']},
        'action': {'tag': 'button', 'value': value}}}}
    # 按契约：工作台把 card.action.trigger 原样包进 status_click。点击人是隔离回放，不冒充 Kun 的真实回答。
    result = request(replay.endpoint, 'status_click', click, timeout=30)
    again = request(replay.endpoint, 'status_click', click, timeout=30)
    emit(f'{target_name}：点「通过」（同一事件两次）', {'first': result, 'again': again})
    assert result['accepted'] and result['answered'] and result['answer'] == '过'
    closed = visual.after(flow, item, target, services)
    record = port.read(entry['wait'], config)
    emit(f'{target_name}：回复归件到这次等待', {'after': closed and asdict(closed), 'wait': record})
    assert closed is None and record['answer']['value'] == '过'
    return entry['delivery']


def report_path(replay, data, business):
    """盯更新报告：同一报告送一次，再调不重发；Kun 的真实私聊反馈回源核验后归件（#174 同一份报告和反馈，只读）。"""
    from workshop.core.models import Event
    from workshop.plugins.intervention.client import Client
    from workshop.plugins.watch_updates.flow import Flow as WatchFlow
    from workshop.plugins.watch_updates.io import IO as WatchIO
    watch = {**data['targets']['workshop'], 'name': 'workshop'}
    settings = dict(watch['watch_updates'])
    watch['watch_updates'] = settings
    data_dir = Path(settings['data_repo'])
    data_dir.mkdir(parents=True, exist_ok=True)
    for key in ('watchlist', 'needs', 'sources'):
        source = Path(settings[key]).expanduser()
        shutil.copy2(source, data_dir / source.name)
        settings[key] = str(data_dir / source.name)
    # 报告送达日志放副本里新的目录：现役当时经旧通道发的记录不带进来，看新服务这一次。
    settings['state_dir'] = str(replay.state / 'watch_updates-replay')
    shutil.copytree(replay.state / 'watch_updates', settings['state_dir'], ignore=shutil.ignore_patterns('delivery-*.json', '*.lock'))
    state = json.loads((Path(settings['state_dir']) / 'state.json').read_text())
    report = state['reports'][REPORT]
    copied = {**report, 'path': str(Path(settings['reports']) / Path(report['path']).name)}
    state['reports'][REPORT] = copied
    config = {'endpoint': str(replay.endpoint), 'timeout': 25}
    port = Client()
    io = WatchIO()
    io.services = lambda name: (port, config)
    io.save(watch, state)
    io.send_report(watch, copied)
    io.send_report(watch, copied)
    with sqlite3.connect(replay.root / 'inbox.db') as db:
        sent = db.execute("SELECT key FROM calls WHERE action='send' AND kind='update_report'").fetchall()
    emit('workshop：盯更新报告调两次送达', {'report': REPORT, 'artifact': copied['path'], 'inbox_sends': sent})
    assert len(sent) == 1
    event = Event('cli:feishu', FEEDBACK, 'workshop', 'check', '真实私聊反馈只读回放', {})
    flow = WatchFlow(io)
    classification = flow.classify(event, None, watch, lambda **kw: {'id': 'workshop:watch_updates:' + kw['object_key']})
    assert not classification.discard
    item = {'id': 'workshop:watch_updates:' + classification.object_key, 'object_key': classification.object_key,
            'materials': [{'source': event.source, 'ext_id': event.ext_id, 'body': event.body}]}
    with io.locked(watch):
        current, check = flow.snapshot(item, watch)
        flow.adopt_feedback(item, watch, current, check)
        flow.consume_feedback(item, watch, current)
        io.save(watch, current)
    matching = [{'version': key, 'instance': place, 'feedback': value} for key, version in current['versions'].items()
                for place, value in version.get('feedback', {}).items() if value.get('message_id') == FEEDBACK]
    emit('workshop：真实私聊反馈回源核验后归件', {'classification': asdict(classification), 'matched': matching,
                                       'unanswered_is_approval': False, 'upgrade_calls': 0})
    assert len(matching) == 1


def stop(child):
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=30)


def manager_path(replay, data, target_name, program, paths, code):
    """真实守护进程、项目经理（真实订阅）、系统 Python 看门；程序作业在跑不算空闲；同一段空闲只推一次。"""
    from workshop.core.log import Log
    from workshop.core.managers import Store
    from workshop.core.models import Event
    from workshop.core.wake import ring
    from workshop.plugins.intervention.client import request
    state = replay.state
    env = child_env(replay)
    processes = []

    def spawn(name):
        out = (replay.root / f'daemon-{target_name}.log').open('ab')
        child = subprocess.Popen([sys.executable, '-m', 'workshop.cli', '--state-dir', str(state), '--rules', str(replay.rules), 'run'],
                                 env=env, cwd=replay.root, stdout=out, stderr=out, start_new_session=True)
        processes.append(child)
        emit(f'{target_name}：起守护进程（{name}）', {'pid': child.pid, 'state': str(state)})
        return child

    def heartbeat():
        try:
            return json.loads((state / 'heartbeat.json').read_text())
        except (OSError, ValueError):
            return {}

    def records():
        store = Store(state)
        try:
            return store.all()
        finally:
            store.close()

    item_id = f'{target_name}:dod-manager'
    key = 'item:' + item_id
    daemon = None
    try:
        with paths.walk(f'{code}.5', '程序作业在跑不算空闲（真实守护进程心跳）'):
            daemon = spawn('第一次')
            until(lambda: heartbeat().get('process', {}).get('pid') == daemon.pid, 60, '守护进程心跳')
            beat = until(lambda: heartbeat().get('business', {}).get('jobs') == 1 and heartbeat(), 60, '心跳算上在跑的程序作业')
            emit(f'{target_name}：程序作业在跑时的心跳', {'business': beat['business'], 'active': beat.get('active')})
            assert beat['business']['total'] >= 1 and beat['business']['idle_since'] is None
        with paths.walk(f'{code}.6', '守护进程停了看门接管、不重复起会话（真实项目经理）'):
            log = Log(state / 'workshop.db')
            try:
                body = asdict(Event('replay', key, target_name, 'dod-manager', '核对副本里的项目经理', (replay.root / 'issue.md').read_text(), action='ready'))
                log.append('event', body, item_id)
                log.append('item_changed', {'state': '卡住', 'blocked_by': '隔离验收：核对转换后副本的项目经理接管；核对后可记 resolved',
                                            'next': '隔离诊断，核对后交 resolved'}, item_id)
            finally:
                log.close()
            ring(state)
            row = until(lambda: next((r for r in records() if r['id'] == key and r['state'] == 'running'), None), 300, '项目经理会话起来')
            token, session = row['token'], row['started']['session']
            emit(f'{target_name}：真实项目经理在跑', {'record': row, 'heartbeat_business': heartbeat().get('business')})
            assert heartbeat()['business'].get('agents', 0) == 0
            stop(daemon)
            command = [sys.executable, '-m', 'workshop.manager_cli', '--state-dir', str(state), '--rules', str(replay.rules)]
            argv = ['/usr/bin/python3', '-I', str(ROOT / 'src/workshop/watch/__init__.py'), '--state-dir', str(state),
                    '--manager-command', json.dumps(command)]
            result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
            after = next(r for r in records() if r['id'] == key)
            emit(f'{target_name}：守护进程停了，系统 Python 看门接管', {'argv': argv, 'exit_code': result.returncode, 'stdout': result.stdout,
                                                         'stderr': result.stderr, 'record': after})
            assert result.returncode == 0 and after.get('token') == token
            daemon = spawn('看门接管后重启')
            finished = until(lambda: next((r for r in records() if r['id'] == key and r['state'] in ('resolved', 'blocked')), None),
                             600, '项目经理结论')
            emit(f'{target_name}：项目经理结论（同一会话接着做）', finished)
            assert finished.get('last_session', session) == session
        with paths.walk(f'{code}.7', '同一段空闲只推一次（真实守护进程、项目经理命令、介入点服务）'):
            release_program(replay, program)
            beat = until(lambda: heartbeat().get('business', {}).get('total') == 0 and heartbeat()['business'].get('idle_since') and heartbeat(),
                         120, '程序作业结束后进入空闲')
            idle = beat['business']['idle_since']
            emit(f'{target_name}：空闲起点', beat['business'])
            message = replay.root / f'clock-{target_name}.json'
            command = [sys.executable, '-m', 'workshop.manager_cli', '--state-dir', str(state), '--rules', str(replay.rules)]
            for seconds in (299, 300):
                message.write_text(json.dumps({'at': (datetime.fromisoformat(idle) + timedelta(seconds=seconds)).isoformat(),
                                               'business': beat['business'], 'faults': {}, 'takeover': False}))
                # 守护进程每拍短暂持有项目经理锁；命令拿不到锁就原样退 0、什么都不做（让给守护进程），所以重试到它真拿到锁（有输出）。
                attempts = []

                def ticked():
                    done = subprocess.run([*command, 'tick', '--file', str(message)], env=env, capture_output=True, text=True, timeout=60)
                    attempts.append(done.returncode)
                    return done if done.returncode != 0 or '"managers"' in done.stdout else None
                result = until(ticked, 60, '项目经理命令拿到锁')
                current = next((r for r in records() if r['id'] == 'idle'), None)
                registered = bool(current and current.get('active') and (current.get('business') or {}).get('idle_since') == idle) or bool(
                    current and current.get('active') and any(f['value'].get('idle_since') == idle for f in current.get('facts', [])))
                emit(f'{target_name}：空闲 {seconds} 秒（假时钟判五分钟）', {'exit_code': result.returncode, 'stderr': result.stderr[-2000:],
                                                                'tick_attempts_until_lock': attempts,
                                                                'idle_record': current, 'registered_this_segment': registered})
                assert result.returncode == 0 and registered == (seconds == 300)

            def update(name, revision=1):
                record = {'id': f'converted-{target_name}-{name}', 'episode': f'converted-{target_name}-{name}', 'revision': revision,
                          'active': True, 'state': 'blocked', 'facts': [], 'attempts': [],
                          'business': {'total': 0, 'idle_since': idle},
                          'conclusion': {'schema': 'manager/v1', 'result': 'blocked', 'action': '补一件新需求',
                                         'reason': '转换后副本上隔离回放：同一空闲段的第二条结论', 'evidence': '隔离回放：同一空闲段',
                                         'status_location': '处理不了'}}
                return request(replay.endpoint, 'manager_update', {'operation': f'converted:{record["id"]}:{revision}', 'record': record}, timeout=30)
            for name in ('a', 'b'):
                emit(f'{target_name}：同一空闲段登记结论 {name}', update(name))
                emit(f'{target_name}：manager_flush', request(replay.endpoint, 'manager_flush', {}, timeout=60))
            with sqlite3.connect(replay.service / 'intervention.db') as db:
                deliveries = [json.loads(b) for (b,) in db.execute("SELECT body FROM records WHERE kind='delivery'")]
                permits = {json.loads(b)['id']: json.loads(b) for (b,) in db.execute("SELECT body FROM records WHERE kind='permit'")}
            segment = [d for d in deliveries if d['payload'].get('kind') == 'manager_help'
                       and permits.get(d['permit'], {}).get('scope') == 'idle:' + idle]
            with sqlite3.connect(replay.root / 'inbox.db') as db:
                sends = db.execute("SELECT key FROM calls WHERE action='send' AND kind='manager_help'").fetchall()
            emit(f'{target_name}：这一空闲段的求助送达', {'idle_since': idle, 'deliveries': [{k: d.get(k) for k in ('id', 'state', 'external')} for d in segment],
                                                  'inbox_manager_help_sends': [s[0] for s in sends]})
            assert len(segment) == 1 and segment[0]['state'] == 'sent' and [s[0] for s in sends].count(segment[0]['id']) == 1
    finally:
        for child in processes:
            stop(child)


def cluster_path(replay, data):
    """集群同步按原申请续接：原批次的固定检查点 + 原申请号，只读核实，不重跑同步脚本、不新建申请。"""
    from workshop.plugins.scheduled_release.cluster import ClusterRelease
    from workshop.plugins.scheduled_release.io import IO as ReleaseIO, read, write
    root = replay.state / 'grant-console-release'
    batch_file = next((root / 'batches').glob('*.json'))
    batch = read(batch_file)
    original = deepcopy(batch)
    target = {**data['targets']['grant-console'], **data['targets']['grant-console']['flows']['scheduled_release'],
              'name': 'grant-console', 'scheduled_release_state_dir': str(root)}
    # 原批次在授权中心批准、代执行后结算成 ok；回到「等授权」那一点续接（同 #174），看只按原申请号核实。
    # 现役批次由批次程序直接结算成 ok（deployed 就是批次程序的 result.json）；退回它要是还在等授权时会写的样子。
    worker = batch.get('result') or batch['deployed']
    batch.update(status='awaiting_grant', result={**worker, 'status': 'awaiting_grant'})
    batch.pop('authorization', None)
    batch.pop('deployed', None)
    write(batch_file, batch)
    result = ClusterRelease(ReleaseIO()).grant_outcome(batch, target)
    write(batch_file, batch)
    emit('grant-console：集群同步按原申请续接', {'batch': batch_file.name, 'original_status': original.get('status'),
                                         'request': batch['result']['grant']['id'], 'after': asdict(result),
                                         'status': batch['status'], 'authorization': batch.get('authorization'),
                                         'verification': (batch.get('deployed') or {}).get('verification'),
                                         'execution': (batch.get('deployed') or {}).get('execution'),
                                         'sync_script_calls': 0, 'new_requests': 0})
    assert batch['status'] == 'ok' and not result.pending
    return batch


def stop_leftovers(replay):
    """收尾：副本里还在跑的进程（空闲时项目经理命令起的会话等，执行者跑在独立进程里）一并停掉，列出来。只认命令行里带副本目录的。"""
    marker = str(replay.root) + '/'

    def alive():
        # 本进程直接管的子进程（介入点服务由 Replay.serve 收尾）不算剩下的。
        listing = subprocess.run(['ps', '-axo', 'pid=,ppid=,command='], capture_output=True, text=True).stdout
        rows = [line.split(None, 2) for line in listing.splitlines() if len(line.split(None, 2)) == 3]
        return [(int(pid), command) for pid, ppid, command in rows
                if marker in command and os.getpid() not in (int(pid), int(ppid))]
    found = alive()
    for pid, _ in found:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    if found:
        until(lambda: not alive(), 60, '副本里剩下的进程退出')
    emit('收尾：停掉副本里还在跑的进程', [{'pid': pid, 'command': command[:300]} for pid, command in found] or '（无）')


def send_records(replay):
    with sqlite3.connect(replay.root / 'inbox.db') as db:
        db.execute('CREATE TABLE IF NOT EXISTS calls (seq INTEGER PRIMARY KEY, action TEXT, key TEXT, kind TEXT, payload TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS inbox (key TEXT PRIMARY KEY, kind TEXT, payload TEXT)')
        calls = db.execute('SELECT seq, action, key, kind FROM calls ORDER BY seq').fetchall()
        inbox = db.execute('SELECT key, kind, payload FROM inbox').fetchall()
    external = [line for line in replay.calls.read_text().splitlines() if line.strip()]
    sends = [c for c in calls if c[1] == 'send']
    counts = {}
    for _, _, key, kind in sends:
        counts[(kind, key)] = counts.get((kind, key), 0) + 1
    not_allowed = [s for s in sends if s[3] not in ALLOWED_KINDS]
    duplicates = {k: v for k, v in counts.items() if v != 1}
    blocked = [line for line in external if not line.startswith('lark-cli（只读放行）')]
    by_kind = {}
    for kind, _ in counts:
        by_kind[kind] = by_kind.get(kind, 0) + 1
    emit('全部出口的发送记录：送达命令收件箱（原样）', [{'key': k, 'kind': kind, 'payload': json.loads(p)} for k, kind, p in inbox])
    emit('全部出口的发送记录：送达命令每次调用（含对账、回复查询）', [{'seq': s, 'action': a, 'key': k, 'kind': kind} for s, a, k, kind in calls])
    emit('全部出口的发送记录：外部命令替身调用（原样）', '\n'.join(external) or '（无）')
    summary = {'sends_by_kind': by_kind, 'not_allowed_sends': len(not_allowed), 'same_key_sent_more_than_once': duplicates,
               'external_calls_blocked': len(blocked), 'external_reads_passed': len(external) - len(blocked)}
    emit('发送记录小结', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--authorization-child', type=Path)
    parser.add_argument('--timeout', type=int, default=1200, help='每个交付对象的执行者轮次总期限（秒）')
    parser.add_argument('--stop-after', help='走完这条路径就停，例如 8.0、8.1（复跑某一段用）')
    args = parser.parse_args()
    if args.authorization_child:
        authorization_child(args.authorization_child)
        return 0
    if not args.output:
        parser.error('--output 必填')
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    cutover, execution, business = load('verify_intervention_cutover.py'), load('verify_intervention_execution.py'), load('verify_intervention_business.py')
    paths = Paths(args.stop_after)
    replay, data = prepare(cutover, args.output)
    batch = json.loads(next((replay.state / 'grant-console-release/batches').glob('*.json')).read_text())
    production = json.loads(next((replay.state / 'grant-console-release/batches').glob('*/deploy/production.json')).read_text())
    approved = {'request': batch['deployed']['grant']['id'], 'kind': 'exec', 'action': '有别人在用的交付对象上线',
                'provider_action': 'argocd/sync_app', 'checkpoint': 'original-sync',
                'target': {'instance': 'central', 'app': data['targets']['grant-console']['deployment']['app'],
                           'revision': production['revision']}}
    program = None
    with replay.serve():
        print('\n========== 切换：转换前登记在途、cutover、对账、比较在途 ==========', flush=True)
        from workshop.github_identity import scoped
        from workshop.plugins.issue_delivery.io import IO as IssueIO
        target = {**data['targets']['workshop'], 'name': 'workshop'}
        history = scoped(lambda target: IssueIO(gh=target['github_identity']['gh'], state_dir=replay.root / 'scratch-io').api(
            f'repos/kun-g/workshop/issues/{HISTORY_ISSUE}'))(target=target)
        assert HISTORY_QUOTE in history['body'] and HISTORY_SESSION in history['body'], '#173 正文里找不到登记的原话'
        current = scoped(lambda target: IssueIO(gh=target['github_identity']['gh'], state_dir=replay.root / 'scratch-io').api(
            'repos/kun-g/workshop/issues/176'))(target=target)
        seeded, program = seed_inflight(replay, None, current['body'])
        emit('切换前在副本上登记的在途（现役此刻没有在跑的程序作业、没有在等的等待）', seeded)
        before = inflight(replay)
        emit('切换前在途（副本）', {'rounds': {k: {f: v.get(f) for f in ('item', 'stage', 'executor', 'status', 'at', 'deadline')}
                                           for k, v in before['rounds'].items()},
                                'jobs': before['jobs'], 'waits': {k: {f: v.get(f) for f in ('item', 'state', 'ref', 'stage')}
                                                                  for k, v in before['waits'].items()}, 'items': before['items']})
        for step, argv in (('cutover（先 workshop 后 grant-console）', ('cutover',)), ('对账表', ('reconcile',))):
            emit('转换：' + step, replay.convert(*argv)[1])
        after = inflight(replay)
        same, text = compare(before, after)
        try:
            with paths.walk('8.0', '在跑的轮、程序作业和等待在切换前后都保留'):
                emit('切换前后在途比较', text)
                emit('切换后 workshop status（副本）', cli(replay, 'status')[1])
                assert same and before['rounds'] and before['jobs'] and before['waits']
            # 副本里现役在跑的轮属于现役会话，副本看不到它们的进程；结束并放弃，免得副本替它们再派轮。
            from workshop.registry import Registry
            registry = Registry()
            registry.register('flow', 'issue_delivery', converted_flow(execution, replay, data, 'workshop'))
            engine, log, _ = engine_for(replay, data, registry, 'workshop', paused=list(data['targets']))
            try:
                copied = [r for r in engine.active() if r['token'] in before['rounds']]
                for row in copied:
                    engine.abort(row['token'], '隔离回放：现役在跑的轮，副本里不接着做')
                until(lambda: (engine.tick(), not [r for r in engine.active() if r['token'] in before['rounds']])[1], 120, '副本里的现役轮结束')
                for item_id in {r['item'] for r in copied}:
                    engine.abandon(item_id, '隔离回放：现役在跑的活，副本里不接着做')
                emit('副本里结束并放弃现役在跑的活（只改副本）', [{k: log.get('items', r['item']).get(k) for k in ('id', 'state', 'next')} for r in copied])
            finally:
                log.close()

            for target_name, code in (('workshop', '8'), ('grant-console', '9')):
                if code == '9':
                    failed = paths.failed('8.')
                    print(f'\n========== 第 8 项门槛：失败 {len(failed)} 条；没走 {len([r for r in paths.results if r[0].startswith("8.") and r[2] == "没走"])} 条 ==========', flush=True)
                    if failed:
                        print('第 8 项有失败的路径，第 9 项（grant-console）不跑。', flush=True)
                        break
                    program = restart_program(replay, 'grant-console:dod-manager')
                    emit('grant-console：起一个在跑的程序作业（同一程序，副本状态目录）', {'pid': program.pid})
                print(f'\n========== 第 {code} 项：{target_name} 这一批 ==========', flush=True)
                with paths.walk(f'{code}.1', '写全了的 issue 自动打标签接单、缺省 P1（真实来源）'):
                    intake(replay, data, execution, target_name)
                with paths.walk(f'{code}.2-4', '无卡等你→会话回答→授权原申请批准后接着做→到点自动续跑（真实 codex 订阅轮次）'):
                    result = walk_rounds(replay, data, execution, target_name, approved, args.timeout)
                    emit(f'{target_name}：轮次小结', {k: result[k] for k in ('rounds', 'resumed', 'authorization', 'timers_fired', 'writes')})
                with paths.walk(f'{code}.3b', '授权原申请：新进程只读查已有申请两次，结果一致，不新建申请'):
                    authorization_check(replay, data, target_name, f'{target_name}:dod-replay', {**approved, 'checkpoint': 'original-sync-restart'}, '原申请已批准')
                with paths.walk(f'{code}.8', '亲眼看送一次、回复归件'):
                    version = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip() if code == '8' else approved['target']['revision']
                    visual_path(replay, data, execution, business, target_name, version)
                if code == '8':
                    with paths.walk('8.9', '盯更新报告送一次、回复归件'):
                        report_path(replay, data, business)
                else:
                    with paths.walk('9.9', '集群同步按原申请续接'):
                        cluster_path(replay, data)
                    with paths.walk('9.10', '缺外部授权类别：对外发消息保持阻断、写明原因'):
                        out = authorization_check(replay, data, 'grant-console', 'grant-console:dod-replay',
                                                  {**approved, 'action': '对外发消息', 'provider_action': 'lark/send_message', 'checkpoint': 'outbound'}, '对外发消息')
                        assert all(o['after']['blocked'] and '对外发消息' in o['after']['blocked'] for o in out)
                    with paths.walk('9.11', '缺外部授权类别：按量计费保持阻断、写明原因'):
                        out = authorization_check(replay, data, 'grant-console', 'grant-console:dod-replay',
                                                  {**approved, 'action': '按量计费', 'provider_action': 'billing/metered', 'checkpoint': 'metered'}, '按量计费授权')
                        assert all(o['after']['blocked'] and '按量计费' in o['after']['blocked'] for o in out)
                pause_all(replay, data)
                manager_path(replay, data, target_name, program, paths, code)
                program = None
                emit(f'{target_name}：这一批走完后的发送记录', send_records(replay))
        except Stop as stop_code:
            print(f'\n--stop-after {stop_code}：到此为止', flush=True)
        finally:
            if program is not None and program.poll() is None:
                release_program(replay, program)
            stop_leftovers(replay)
        print('\n========== 全部出口的发送记录 ==========', flush=True)
        summary = send_records(replay)
    print('\n========== 结果 ==========', flush=True)
    for code, title, result, reason in paths.results:
        print(f'[{code}]\t{result}\t{title}' + (f'\t{reason}' if reason else ''))
    print(f'发送：不许的 {summary["not_allowed_sends"]} 条；同一送达号发了不止一次 {len(summary["same_key_sent_more_than_once"])} 个；'
          f'按类：{summary["sends_by_kind"]}；外部命令替身拦下 {summary["external_calls_blocked"]} 次')
    print(f'总耗时 {time.monotonic() - started:.0f} 秒；副本 {replay.root}')
    bad = [r for r in paths.results if r[2] == '失败'] or summary['not_allowed_sends'] or summary['same_key_sent_more_than_once']
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
