"""回执交不上时原因要回到执行者（ws#30）：假 cursor-agent / codex + 真车间命令行。

真实失败（11-13 状态库）：cursor-grok 在说明文字后面跟回执 JSON，包装进程只认整段 JSON，
判成「未交回执」；codex 交 outcome=success、顶层带 head，被拒后原因没回给它，下一轮
接回同一会话照抄同样的错。
"""
from datetime import timedelta
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from conftest import build, current, event, item as core_item, reach
from workshop.core.models import Brief
from workshop.plugins.executors.local.adapters import Executor
from workshop.plugins.executors.local.runner import result_receipt
from workshop.plugins.executors.local.storage import read

FAKE = r'''
import json, os, pathlib, re, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(sys.argv[0]).parent
prompt = sys.stdin.read()
calls = root / 'agents'
count = len(calls.read_text().splitlines()) if calls.exists() else 0
with calls.open('a') as f:
    f.write(json.dumps({'name': name, 'args': args, 'prompt': prompt, 'pid': os.getpid()}) + '\n')
scenario = (root / 'scenario').read_text().strip()
resumed = '--resume' in args or 'resume' in args
if name == 'codex':
    session = args[args.index('resume') + 1] if 'resume' in args else f'codex-session-{count}'
    print(json.dumps({'type': 'thread.started', 'thread_id': session}), flush=True)
else:
    session = args[args.index('--resume') + 1] if '--resume' in args else f'cursor-session-{count}'
    print(json.dumps({'type': 'system', 'session_id': session}), flush=True)
good = {'outcome': 'done', 'summary': '审查通过，没有必须改。', 'advance': '交付',
        'evidence': [json.dumps({'schema': 'issue-delivery/v1', 'head': 'abc', 'must_fix': [], 'suggestions': []})]}
bad = {'outcome': 'success', 'summary': '审查通过', 'advance': '验收', 'head': 'abc', 'must_fix': [], 'suggestions': []}
if scenario in ('typed-example', 'two-types-then-fixed'):
    samples = [json.loads(v) for v in re.findall(r'```json\n(.*?)\n```', prompt, re.S)]
    example = next(v for v in samples if 'outcome' in v)
    notification = next(v for v in samples if 'notice' in v)
    assert isinstance(example['evidence'], list)
    assert notification['notice'][0]['key']
    good = {**good, 'evidence': example['evidence'], **notification}
    if resumed:
        assert 'evidence 必须是字符串列表' in prompt and 'notice 必须是列表' in prompt
    text = json.dumps(good if resumed or scenario == 'typed-example' else
                      {**good, 'evidence': '{}', 'notice': '完成'}, ensure_ascii=False)
elif scenario == 'normal':
    text = '审查通过。\n\n```json\n' + json.dumps(good, ensure_ascii=False, indent=2) + '\n```'
elif scenario == 'rejected-then-fixed':
    text = '复核完毕。' + json.dumps(good if resumed else {**good, 'head': 'abc'}, ensure_ascii=False)
elif scenario == 'long':
    text = '结论如下。' + json.dumps({**good, 'summary': '长' * 100000}, ensure_ascii=False)
elif scenario == 'no-receipt':
    text = '我先按只读方式看这次要审的提交。'
elif scenario == 'stubborn-session':
    # 同一会话里复用已做完的审查、照抄同样的错；新会话看到说明里的原因才交对。
    fixed = not resumed and 'outcome 是 "success"' in prompt
    text = json.dumps(good if fixed else bad, ensure_ascii=False)
if name == 'codex':
    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}}), flush=True)
else:
    print(json.dumps({'type': 'result', 'subtype': 'success', 'result': text, 'session_id': session}), flush=True)
'''

CONFIG = {'billing': 'subscription', 'model': 'fake-model'}


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('timed out')


@pytest.fixture
def rig(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key not in ('PATH', 'LANG', 'HOME', 'TMPDIR', 'TERM'):
            monkeypatch.delenv(key)
    commands = {}
    for name in ('codex', 'cursor-agent', 'claude'):
        path = tmp_path / name
        path.write_text('#!' + sys.executable + '\n' + FAKE)
        path.chmod(0o755)
        commands[name] = str(path)
    repo = tmp_path / 'repo'
    repo.mkdir()
    state = tmp_path / 'core'
    engine = build(state)
    executors = []

    def make(kind):
        instance = Executor(kind, state_dir=tmp_path / 'state', commands=commands, home=tmp_path, stop_timeout=2)
        executors.append(instance)
        return instance

    def target(token):
        return {'checkout': str(repo), 'runtime': {
            'state_dir': str(state), 'command': [sys.executable, '-m', 'workshop.cli', '--state-dir', str(state)],
            'env': {'WORKSHOP_STATE_DIR': str(state), 'WORKSHOP_ROUND_TOKEN': token}}}

    def scenario(name):
        (tmp_path / 'scenario').write_text(name)

    def calls():
        path = tmp_path / 'agents'
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []

    try:
        yield engine, make, target, scenario, calls, repo
    finally:
        engine.log.close()
        for row in calls():
            try:
                os.kill(row['pid'], signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for e in executors:
            for p in e.root.glob('rounds/*/supervisor.json'):
                try:
                    os.killpg(read(p)['pid'], signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass


def run_round(engine, executor, target, repo, brief=None, session=None):
    """车间派了一轮（假执行者占位），由真执行者跑完并经真 CLI 交回执。"""
    row = current(engine)
    item = {**core_item(engine), 'workdir': str(repo)}
    brief = brief or Brief('只读审查当前提交。', [])
    if session:
        started = executor.resume(row['token'], session, brief, item, CONFIG, target(row['token']))
    else:
        started = executor.launch(row['token'], brief, item, CONFIG, target(row['token']))
    directory = executor.directory(row['token'])
    wait_for(lambda: (directory / 'exited.json').exists())
    executor.stop(row['token'], started)
    return engine.log.get('rounds', row['token']), directory, started


def test_parser_takes_receipt_after_prose_or_in_fence(tmp_path):
    output = tmp_path / 'output.log'
    receipt = {'outcome': 'done', 'summary': '通过', 'evidence': ['{"schema":"issue-delivery/v1","must_fix":[]}']}
    for text in ('审查通过。\n```json\n' + json.dumps(receipt) + '\n```',
                 '我先看提交。' + json.dumps(receipt),
                 json.dumps({'outcome': 'nothing', 'summary': '草稿'}) + ' 改一下：' + json.dumps(receipt)):
        output.write_text(json.dumps({'type': 'result', 'result': text}) + '\n')
        assert result_receipt(output, 'cursor-grok') == receipt
    # 取值对不对交给核心判，原因才能具体回给执行者。
    output.write_text(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',
                      'text': '{"outcome":"success","summary":"x"}'}}) + '\n')
    assert result_receipt(output, 'codex') == {'outcome': 'success', 'summary': 'x'}
    output.write_text(json.dumps({'type': 'result', 'result': '我先看看提交。'}) + '\n')
    assert result_receipt(output, 'cursor-grok') is None


def test_cursor_normal_receipt_after_prose_is_submitted(rig):
    engine, make, target, scenario, calls, repo = rig
    engine.ingest(event())
    engine.tick()
    scenario('normal')
    row, directory, _ = run_round(engine, make('cursor-grok'), target, repo)
    assert row['receipt']['outcome'] == 'done' and row['receipt']['advance'] == '交付'
    assert read(directory / 'receipt-result.json')['code'] == 0
    assert len(calls()) == 1


def test_cursor_rejected_receipt_reason_goes_back_and_is_fixed_in_same_round(rig):
    engine, make, target, scenario, calls, repo = rig
    engine.ingest(event())
    engine.tick()
    scenario('rejected-then-fixed')
    row, directory, _ = run_round(engine, make('cursor-grok'), target, repo)
    # 拒绝原因进了事实，也发回了同一个会话；改对后这一轮收下。
    assert '回执顶层有不认识的字段 head' in row['receipt_rejected']
    assert row['receipt']['outcome'] == 'done' and 'head' not in row['receipt']
    first, second = calls()
    assert '--resume' not in first['args'] and second['args'][second['args'].index('--resume') + 1] == 'cursor-session-0'
    assert '回执顶层有不认识的字段 head' in second['prompt'] and 'outcome 只能是 done、failed、nothing' in second['prompt']
    assert not (directory / 'receipt-rejected.json').exists()


def test_cursor_long_receipt_is_kept_whole(rig):
    engine, make, target, scenario, calls, repo = rig
    engine.ingest(event())
    engine.tick()
    scenario('long')
    row, _, _ = run_round(engine, make('cursor-grok'), target, repo)
    assert len(row['receipt']['summary']) == 100000
    assert len(calls()) == 1


def test_cursor_no_receipt_fails_fast_with_reason(rig):
    engine, make, target, scenario, calls, repo = rig
    engine.ingest(event())
    engine.tick()
    scenario('no-receipt')
    started = time.monotonic()
    row, directory, _ = run_round(engine, make('cursor-grok'), target, repo)
    assert time.monotonic() - started < 15  # 不等到每轮期限
    assert row['receipt']['outcome'] == 'failed'
    assert row['receipt']['summary'].count('没有回执 JSON 对象') == 2
    assert '我先按只读方式看这次要审的提交' in row['receipt']['evidence'][0]
    assert (directory / 'receipt-rejected.json').exists()


def test_codex_wrong_format_reason_reaches_next_round_in_a_fresh_session(rig):
    engine, make, target, scenario, calls, repo = rig
    reach(engine, '审查')
    scenario('stubborn-session')
    codex = make('codex')
    row, directory, started = run_round(engine, codex, target, repo)
    # 第一轮：同一会话里改一次仍照抄，代交带具体原因的 failed。
    assert row['receipt']['outcome'] == 'failed'
    assert 'outcome 是 "success"，只能是 done、failed、nothing' in row['receipt']['summary']
    assert '回执顶层有不认识的字段 head、must_fix、suggestions' in row['receipt_rejected']
    assert [('resume' in c['args']) for c in calls()] == [False, True]
    engine.tick()
    item = core_item(engine)
    assert item['stage'] == '审查' and 'outcome 是 "success"' in item['last_failure']
    # 第二轮：说明带着上一轮原因（流程照 last_failure 写，见 test_issue_delivery），换新会话。
    engine.clock.now += timedelta(hours=1)
    engine.tick()
    brief = Brief('只读审查当前提交。上一轮没跑成的原因：' + item['last_failure'], [])
    row, _, _ = run_round(engine, codex, target, repo, brief=brief, session=started.session)
    third = calls()[2]
    assert 'resume' not in third['args'], '连着交错格式的会话不再接回'
    assert row['receipt']['outcome'] == 'done' and row['receipt']['advance'] == '交付'
    engine.tick()
    assert core_item(engine)['stage'] == '交付'
    assert core_item(engine)['last_failure'] is None


@pytest.mark.parametrize("scenario_name, attempts", [("typed-example", 1), ("two-types-then-fixed", 2)])
def test_readonly_claude_uses_example_and_fixes_both_types(rig, scenario_name, attempts):
    engine, make, target, scenario, calls, repo = rig
    engine.ingest(event())
    engine.tick()
    scenario(scenario_name)
    row, directory, _ = run_round(engine, make('claude'), target, repo)
    assert read(directory / 'spec.json')['readonly'] is True
    assert read(directory / 'receipt-result.json')['code'] == 0
    assert row['receipt']['outcome'] == 'done'
    assert isinstance(row['receipt']['evidence'], list)
    assert row['receipt']['notice'][0]['key'] == 'result'
    assert len(calls()) == attempts
    assert not (directory / 'receipt-rejected.json').exists()
    if attempts == 2:
        assert 'evidence 必须是字符串列表' in row['receipt_rejected']
        assert 'notice 必须是列表' in row['receipt_rejected']
        assert '--resume' in calls()[1]['args']
        assert read(directory / 'fallback-receipt.json')['evidence'] == '{}'
        assert read(directory / 'fallback-receipt-2.json') == row['receipt']
