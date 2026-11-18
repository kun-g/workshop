"""独立服务部署边界；launchctl 为可控外部边界，真实进程另做隔离演练。"""
import hashlib
from pathlib import Path
import plistlib
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
    def command(argv, check=True):
        calls.append(argv)
        if argv[1] == 'print':
            return subprocess.CompletedProcess(argv, 0 if loaded else 113)
        if argv[1] == 'bootstrap':
            loaded[:] = [str(argv[-1])]
        if argv[1] == 'bootout':
            loaded[:] = []
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setitem(globals_, 'command', command)
    monkeypatch.setitem(globals_, 'ready', lambda *args: {'pid': 123})
    return module, globals_, (state, rules, endpoint, python, label, agents), calls, loaded


def test_install_then_repeat_is_idempotent_and_independent(installer):
    module, _, args, calls, loaded = installer
    assert module['activate'](*args)['action'] == 'installed'
    path = Path(loaded[0])
    plist = plistlib.loads(path.read_bytes())
    assert plist['KeepAlive'] and plist['RunAtLoad']
    assert plist['ProgramArguments'][:4] == [str(args[3]), '-I', '-m', 'workshop.plugins.intervention']
    assert module['activate'](*args)['action'] == 'unchanged'
    assert len([a for a in calls if a[1] == 'bootstrap']) == 1
    assert not any(a[1] == 'bootout' for a in calls)


def test_failed_upgrade_restores_old_launch_definition(installer, monkeypatch):
    module, globals_, args, calls, loaded = installer
    module['activate'](*args)
    path = Path(loaded[0]); old = path.read_bytes()
    monkeypatch.setitem(globals_, 'ready', lambda *a: (_ for _ in ()).throw(RuntimeError('未就绪')))
    newer = (*args[:3], args[3].with_name('python-new'), *args[4:])
    with pytest.raises(RuntimeError, match='未就绪'):
        module['activate'](*newer)
    assert path.read_bytes() == old and loaded == [str(path)]
    assert len([a for a in calls if a[1] == 'bootstrap']) == 3


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
                                                  '--rules', 'rules/workshop.yaml']}]
    assert Rules.load(ROOT / 'rules/workshop.yaml').target('workshop', 'scheduled_release')['release_prepare'] == target['release_prepare']
