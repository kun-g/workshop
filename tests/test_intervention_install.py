"""独立服务部署边界；launchctl 为可控外部边界，真实进程另做隔离演练。"""
import hashlib
import os
from pathlib import Path
import plistlib
import pwd
import runpy
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def installer(tmp_path, monkeypatch):
    module = runpy.run_path(str(ROOT / 'scripts/install_intervention.py'))
    globals_ = module['activate'].__globals__
    state, agents = tmp_path / 'state', tmp_path / 'agents'
    state.mkdir(mode=0o700)
    agents.mkdir()
    rules = tmp_path / 'rules.yaml'
    rules.write_text('intervention: {}')
    python = tmp_path / 'environment/bin/python'
    endpoint = state / 'service.sock'
    label = 'com.kun.workshop-intervention.test'
    loaded, calls = [], []
    failures = {'bootstrap': []}
    def command(argv, check=True):
        calls.append(argv)
        if argv[1] == 'print':
            return subprocess.CompletedProcess(argv, 0 if loaded else 113)
        if argv[1] == 'bootstrap':
            if failures['bootstrap']:
                code = failures['bootstrap'].pop(0)
                if code:
                    return subprocess.CompletedProcess(argv, code, '', 'I/O error')
            loaded[:] = [str(argv[-1])]
        if argv[1] == 'bootout':
            loaded[:] = []
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setitem(globals_, 'command', command)
    monkeypatch.setitem(globals_, 'ready', lambda *args: {'pid': 123})
    # 重试不真等（E8）：间隔换成记一笔。
    sleeps = []
    monkeypatch.setitem(globals_, 'sleep', sleeps.append)
    return module, globals_, (state, rules, endpoint, python, label, agents), calls, loaded, failures, sleeps


def test_install_then_repeat_is_idempotent_and_independent(installer):
    module, _, args, calls, loaded, failures, sleeps = installer
    assert module['activate'](*args)['action'] == 'installed'
    path = Path(loaded[0])
    plist = plistlib.loads(path.read_bytes())
    assert plist['KeepAlive'] and plist['RunAtLoad']
    # 重装后送达命令要找得到 lark-cli：和车间守护进程共用一份 PATH。
    agents = runpy.run_path(str(ROOT / 'scripts/launch_agents.py'))
    # 安装器按系统用户的家目录算 PATH（发版和检查副本里 HOME 是临时目录），这里也一样，不读 HOME。
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert plist['EnvironmentVariables']['PATH'] == agents['search_path'](home)
    assert '/opt/homebrew/bin' in plist['EnvironmentVariables']['PATH'].split(':')
    assert plist['ProgramArguments'][:4] == [str(args[3]), '-I', '-m', 'workshop.plugins.intervention']
    assert module['activate'](*args)['action'] == 'unchanged'
    assert len([a for a in calls if a[1] == 'bootstrap']) == 1
    assert not any(a[1] == 'bootout' for a in calls)


def test_failed_upgrade_restores_old_launch_definition(installer, monkeypatch):
    module, globals_, args, calls, loaded, failures, sleeps = installer
    module['activate'](*args)
    path = Path(loaded[0]); old = path.read_bytes()
    monkeypatch.setitem(globals_, 'ready', lambda *a: (_ for _ in ()).throw(RuntimeError('未就绪')))
    newer = (*args[:3], args[3].with_name('python-new'), *args[4:])
    with pytest.raises(RuntimeError, match='未就绪'):
        module['activate'](*newer)
    assert path.read_bytes() == old and loaded == [str(path)]
    assert len([a for a in calls if a[1] == 'bootstrap']) == 3
    assert not sleeps


def test_transient_bootstrap_failure_is_retried_and_install_succeeds(installer):
    # 第一次退 5（I/O error），第二次成功：整次发版成功，重试间隔不真等。
    module, _, args, calls, loaded, failures, sleeps = installer
    failures['bootstrap'] = [5, 0]
    assert module['activate'](*args)['action'] == 'installed'
    assert [a[1] for a in calls if a[1] == 'bootstrap'] == ['bootstrap', 'bootstrap']
    assert loaded == [str(args[5] / 'com.kun.workshop-intervention.test.plist')]
    assert len(sleeps) == 1 and sleeps[0] == module['BOOTSTRAP_INTERVAL']


def test_three_bootstrap_failures_fail_the_step_without_running_ready(installer, monkeypatch):
    module, globals_, args, calls, loaded, failures, sleeps = installer
    failures['bootstrap'] = [5, 5, 5]
    ready_calls = []
    monkeypatch.setitem(globals_, 'ready', lambda *a: ready_calls.append(a) or {'pid': 123})
    with pytest.raises(RuntimeError, match='连试 3 次'):
        module['activate'](*args)
    assert [a[1] for a in calls if a[1] == 'bootstrap'] == ['bootstrap'] * 3
    assert not loaded and not ready_calls and len(sleeps) == 2


def test_readiness_rejects_other_environment_or_wrong_rules(tmp_path, monkeypatch):
    module = runpy.run_path(str(ROOT / 'scripts/install_intervention.py'))
    rules = tmp_path / 'rules.yaml'; rules.write_text('intervention: {}')
    python = tmp_path / 'python'
    response = {'rules': str(rules), 'rules_sha256': hashlib.sha256(rules.read_bytes()).hexdigest(),
                'python': str(python), 'pid': 123}
    monkeypatch.setitem(module['ready'].__globals__, 'request', lambda *a, **k: response)
    assert module['ready'](tmp_path / 'sock', rules, python, timeout=0)['pid'] == 123
    response['python'] = '/other/python'
    with pytest.raises(RuntimeError, match='其他环境'):
        module['ready'](tmp_path / 'sock', rules, python, timeout=0)
    response['rules_sha256'] = 'old'
    with pytest.raises(RuntimeError, match='现役规则'):
        module['ready'](tmp_path / 'sock', rules, python, timeout=0)


def test_real_rules_keep_install_outside_check_prepare():
    from workshop.core.rules import Rules
    target = Rules.load(ROOT / 'rules/workshop.yaml').target('workshop')
    assert target['prepare'] == [{'argv': ['uv', 'sync', '--locked']}]
    assert target['release_prepare'] == [{'argv': ['uv', 'run', 'python', 'scripts/install_intervention.py',
                                                  '--rules', 'rules/workshop.yaml']},
                                         {'argv': ['uv', 'run', 'python', 'scripts/convert_intervention.py',
                                                   '--rules', 'rules/workshop.yaml', 'cutover']}]
    assert Rules.load(ROOT / 'rules/workshop.yaml').target('workshop', 'scheduled_release')['release_prepare'] == target['release_prepare']
