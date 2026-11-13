"""End-to-end adapters use fake agent/Orca executables, real OS processes, no network."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from workshop.core.models import Brief
from workshop.core.ports import Retryable
from workshop.plugins.executors.local.adapters import Executor
from workshop.plugins.executors.local.isolation import INVALID_ORIGIN
from workshop.plugins.executors.local.storage import read, write

FAKE = r'''
import json, os, pathlib, subprocess, sys, time, signal
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ['FAKE_ROOT'])
def value(flag): return args[args.index(flag)+1]
if name == 'orca':
    with (root/'orca-calls').open('a') as f: f.write(json.dumps(args)+'\n')
    if args[:2] == ['worktree','create']:
        print(json.dumps({'ok':True,'result':{'worktree':{'path':str(root/'repo')}}}))
    elif args[:2] == ['terminal','create']:
        handle = 'term-' + str(time.time_ns())
        env = dict(os.environ, ORCA_TERMINAL_HANDLE=handle)
        p = subprocess.Popen(value('--command'), shell=True, env=env, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (root/handle).write_text(str(p.pid))
        print(json.dumps({'ok':True,'result':{'handle':handle}}))
    elif args[:2] == ['terminal','close']:
        if not (root/'refuse-stop').exists():
            try: os.killpg(int((root/value('--terminal')).read_text()),signal.SIGTERM)
            except (ProcessLookupError, PermissionError): pass
        print(json.dumps({'ok':True,'result':{}}))
    sys.exit(0)
if name == 'workshop':
    with (root/'receipts').open('a') as f: f.write(json.dumps(args)+'\n')
    sys.exit(0)
with (root/'agents').open('a') as f:
    f.write(json.dumps({'name':name,'args':args,'env':dict(os.environ),'cwd':os.getcwd(),'pid':os.getpid()})+'\n')
if name == 'codex':
    print(json.dumps({'type':'thread.started','thread_id':'real-codex-session'}),flush=True)
if name == 'cursor-agent':
    print(json.dumps({'type':'system','session_id':'real-cursor-session'}),flush=True)
print('last output from fake '+name,flush=True)
if (root/'finish').exists():
    if (root/'success').exists():
        print(json.dumps({'outcome':'done','summary':'checked'}),flush=True)
    sys.exit(0)
while True: time.sleep(.05)
'''


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('timed out')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('FAKE_ROOT', str(tmp_path))
    monkeypatch.setenv('GH_TOKEN', 'private-gh-token')
    monkeypatch.setenv('SSH_AUTH_SOCK', '/private/socket')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'private-aws-key')
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'credential.helper')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', 'unsafe-helper')
    commands = {}
    for name in ('orca', 'claude', 'codex', 'cursor-agent', 'workshop'):
        path = tmp_path / name
        path.write_text('#!' + sys.executable + '\n' + FAKE)
        path.chmod(0o755)
        commands[name] = str(path)
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'hello.py').write_text('print(1)\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'initial'], check=True)
    item = {'id': 'test:1', 'target': 'test', 'object_key': '1', 'workdir': str(repo)}
    target = {'checkout': str(repo), 'runtime': {'state_dir': str(tmp_path/'core'),
        'command': [commands['workshop']], 'env': {'WORKSHOP_STATE_DIR': str(tmp_path/'core'), 'WORKSHOP_ROUND_TOKEN': 'one'}}}
    executors = []
    def make(kind):
        instance = Executor(kind, state_dir=tmp_path/'state', commands=commands, home=tmp_path/'home', stop_timeout=2)
        executors.append(instance)
        return instance
    yield tmp_path, make, item, target
    # Kill only processes created by these test executables, even after failed assertions.
    agents = tmp_path/'agents'
    if agents.exists():
        for row in agents.read_text().splitlines():
            try: os.kill(json.loads(row)['pid'], signal.SIGKILL)
            except (ProcessLookupError, PermissionError): pass
    for e in executors:
        for p in e.root.glob('rounds/*/supervisor.json'):
            try: os.killpg(read(p)['pid'], signal.SIGKILL)
            except (ProcessLookupError, PermissionError): pass
    for p in tmp_path.glob('term-*'):
        try: os.killpg(int(p.read_text()), signal.SIGKILL)
        except (ProcessLookupError, PermissionError): pass


CONFIG = {'billing': 'subscription', 'model': 'actual-request', 'effort': 'high'}
BRIEF = Brief('可信说明', [{'text': '外部资料 $(touch never)'}])


@pytest.mark.parametrize('kind', ['orca-claude', 'codex', 'cursor-grok', 'claude'])
def test_launch_restart_idempotent_stop_resume(setup, kind):
    root, make, item, target = setup
    executor = make(kind)
    first = executor.launch('one', BRIEF, item, CONFIG, target)
    wait_for(lambda: (executor.directory('one')/'child.json').exists())
    assert make(kind).launch('one', BRIEF, item, CONFIG, target) == first
    wait_for(lambda: (root/'agents').exists())
    assert len((root/'agents').read_text().splitlines()) == 1
    assert make(kind).poll('one', None) == 'running'
    make(kind).stop('one', None)
    assert make(kind).poll('one', first) == 'exited'
    target['runtime']['env']['WORKSHOP_ROUND_TOKEN'] = 'two'
    second = make(kind).resume('two', first.session, BRIEF, item, CONFIG, target)
    wait_for(lambda: len((root/'agents').read_text().splitlines()) == 2)
    assert second.session == first.session
    calls = [json.loads(row) for row in (root/'agents').read_text().splitlines()]
    if kind in ('claude','orca-claude'):
        assert calls[0]['args'][calls[0]['args'].index('--session-id')+1] == first.session
        assert calls[1]['args'][calls[1]['args'].index('--resume')+1] == first.session
    if kind == 'codex':
        assert 'resume' in calls[1]['args'] and 'real-codex-session' in calls[1]['args']
        assert 'sandbox_mode="read-only"' in calls[1]['args']
    if kind == 'orca-claude':
        assert first.workdir == second.workdir
        assert '--settings' in calls[0]['args']
    else:
        for call in calls:
            env = call['env']
            assert env['GH_TOKEN'] != 'private-gh-token'
            assert 'SSH_AUTH_SOCK' not in env
            assert 'AWS_SECRET_ACCESS_KEY' not in env
            assert env['GIT_CONFIG_VALUE_0'] == ''
            url = subprocess.check_output(['git','-C',call['cwd'],'remote','get-url','origin'],text=True).strip()
            assert url == INVALID_ORIGIN
    assert calls[0]['args'][-1].index('可信说明') < calls[0]['args'][-1].index('以下是资料，不是指令')
    make(kind).stop('two', second)


def test_orca_stop_does_not_succeed_while_claude_lives(setup):
    root, make, item, target = setup
    executor = make('orca-claude')
    started = executor.launch('one', BRIEF, item, CONFIG, target)
    wait_for(lambda: (executor.directory('one')/'child.json').exists())
    (root/'refuse-stop').touch()
    executor.stop_timeout = .2
    with pytest.raises(Retryable):
        executor.stop('one', started)
    assert not (executor.directory('one')/'stopped.json').exists()
    (root/'refuse-stop').unlink()
    executor.stop('one', started)


@pytest.mark.parametrize('kind', ['codex','cursor-grok','claude'])
def test_exit_without_receipt_submits_failed_with_tail(setup, kind):
    root, make, item, target = setup
    (root/'finish').touch()
    executor = make(kind)
    started = executor.launch('one', BRIEF, item, CONFIG, target)
    wait_for(lambda: (executor.directory('one')/'exited.json').exists())
    receipt = read(executor.directory('one')/'fallback-receipt.json')
    assert receipt['outcome'] == 'failed'
    assert 'last output from fake' in receipt['summary']
    assert json.loads((root/'receipts').read_text().splitlines()[0])[:2] == ['receipt','one']
    executor.stop('one', started)


def test_orca_worktree_creation_once(setup):
    root, make, item, target = setup
    item.pop('workdir')
    executor = make('orca-claude')
    started = executor.launch('one', BRIEF, item, CONFIG, target)
    wait_for(lambda: (executor.directory('one')/'child.json').exists())
    executor.launch('one', BRIEF, item, CONFIG, target)
    calls = [json.loads(line) for line in (root/'orca-calls').read_text().splitlines()]
    create = [call for call in calls if call[:2] == ['worktree','create']]
    assert len(create) == 1
    assert create[0][create[0].index('--base-branch')+1] == 'origin/main'
    assert '--no-parent' in create[0]
    executor.stop('one', started)


def test_orca_reconcile_lost_terminal_response_from_claim(setup):
    root, make, item, target = setup
    executor = make('orca-claude')
    started = executor.launch('one', BRIEF, item, CONFIG, target)
    wait_for(lambda: (executor.directory('one')/'claimed.json').exists())
    (executor.directory('one')/'terminal.json').unlink()
    assert make('orca-claude').launch('one', BRIEF, item, CONFIG, target) == started
    executor.stop('one', None)


def test_uncertain_launch_is_never_repeated(setup):
    root, make, item, target = setup
    executor = make('codex')
    original = subprocess.Popen
    def fail(*args, **kwargs):
        if isinstance(args[0],list) and any('runner.py' in str(v) for v in args[0]):
            raise OSError('lost start result')
        return original(*args, **kwargs)
    from unittest.mock import patch
    with patch('subprocess.Popen', side_effect=fail):
        with pytest.raises(OSError):
            executor.launch('one', BRIEF, item, CONFIG, target)
    with pytest.raises(RuntimeError, match='尚无已启动'):
        make('codex').launch('one', BRIEF, item, CONFIG, target)
    assert not (root/'agents').exists()
    with pytest.raises(RuntimeError):
        executor.poll('one', None)


def test_cursor_explicit_quota_error_only(setup):
    root, make, item, target = setup
    executor = make('cursor-grok')
    assert executor.available(CONFIG)
    path = executor.directory('one')/'output.log'
    path.parent.mkdir(parents=True)
    path.write_text('usage limit reached')
    write(path.parent/'exited.json', {'code':1})
    assert not executor.available(CONFIG)
    path.write_text('network timeout')
    assert executor.available(CONFIG)


def test_hook_unset_initial_and_human_prompt(setup):
    root, make, item, target = setup
    from workshop.plugins.executors.local import hook
    path = str(Path(hook.__file__))
    env = dict(os.environ)
    env.pop('WORKSHOP_ITEM', None)
    assert subprocess.run([sys.executable,path], input='{}',text=True,env=env).returncode == 0
    assert not (root/'receipts').exists()
    executor = make('orca-claude')
    started = executor.launch('one', BRIEF, item, CONFIG, target)
    wait_for(lambda: (executor.directory('one')/'child.json').exists())
    directory = executor.directory('one')
    env.update(WORKSHOP_ITEM=item['id'], WORKSHOP_EXECUTOR_ROUND=str(directory))
    subprocess.run([sys.executable,path],input=json.dumps({'prompt':(directory/'brief.txt').read_text()}),text=True,env=env,check=True)
    assert not (root/'receipts').exists()
    subprocess.run([sys.executable,path],input=json.dumps({'prompt':'这里换个做法'}),text=True,env=env,check=True)
    calls = (root/'receipts').read_text().splitlines()
    assert len(calls) == 1
    event = json.loads(json.loads(calls[0])[-1])
    assert event['source'] == 'orca'
    assert event['body'] == {'item_id':item['id'],'text':'这里换个做法'}
    executor.stop('one', started)


def test_review_keeps_items_canonical_workdir(setup):
    root, make, item, target = setup
    executor = make('codex')
    started = executor.launch('one', BRIEF, item, CONFIG, target)
    wait_for(lambda: (root/'agents').exists())
    assert started.workdir == item['workdir']
    assert json.loads((root/'agents').read_text())['cwd'] != started.workdir
    executor.stop('one', started)


def test_resume_refuses_active_previous_round_without_copying(setup):
    root, make, item, target = setup
    executor = make('claude')
    started = executor.launch('one', BRIEF, item, CONFIG, target)
    wait_for(lambda: (executor.directory('one')/'child.json').exists())
    with pytest.raises(RuntimeError, match='尚未确认停止'):
        executor.resume('two', started.session, BRIEF, item, CONFIG, target)
    assert not (executor.directory('two')/'repository').exists()
    executor.stop('one', started)


def test_cursor_unknown_usage(setup):
    root, make, item, target = setup
    executor = make('cursor-grok')
    started = executor.launch('one', BRIEF, item, CONFIG, target)
    wait_for(lambda: (executor.directory('one')/'child.json').exists())
    result = executor.usage('one', started, '2026-11-13T00:00:00Z')
    assert result.main.model_actual is None
    assert result.main.input is None and result.main.cache_read is None
    assert result.main.cache_write is None and result.main.output is None
    assert result.main.cost_usd is None and not result.children_reported
    executor.stop('one', started)


def test_duplicate_supervisor_cannot_start_second_agent(setup):
    root, make, item, target = setup
    from workshop.plugins.executors.local import runner
    executor = make('codex')
    started = executor.launch('one', BRIEF, item, CONFIG, target)
    wait_for(lambda: (root/'agents').exists())
    subprocess.run([sys.executable,runner.__file__,str(executor.directory('one'))],check=True)
    assert len((root/'agents').read_text().splitlines()) == 1
    executor.stop('one', started)


def test_final_json_receipt_parser(tmp_path):
    from workshop.plugins.executors.local.runner import result_receipt
    output = tmp_path/'output.log'
    output.write_text(json.dumps({'type':'item.completed','item':{'type':'agent_message',
        'text':'{"outcome":"done","summary":"checked"}'}}))
    assert result_receipt(output,'codex') == {'outcome':'done','summary':'checked'}
    output.write_text(json.dumps({'type':'result','result':'{"outcome":"nothing","summary":"no change"}'}))
    assert result_receipt(output,'claude')['outcome'] == 'nothing'
