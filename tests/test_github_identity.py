import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from workshop import github_identity as identity
from workshop.core.ports import TargetBlocked
from workshop.plugins.executors.local.isolation import session_environment
from workshop.plugins.executors.local import runner
from workshop.core.jobs import encode, execute, read
from conftest import build, configuration, event


@pytest.fixture
def signing(tmp_path):
    script = tmp_path / 'signer.py'
    script.write_text("import sys\nassert sys.argv[1:] == ['--repo', 'kun-g/workshop']\nprint('fake-app-token')\n")
    return {'mode': 'app', 'gh': '/usr/bin/gh', 'signer': [sys.executable, str(script)]}


@pytest.mark.parametrize('driver', ['claude', 'codex', 'cursor'])
def test_session_signs_once_only_in_memory_and_preserves_spec(tmp_path, signing, driver):
    spec = {'driver': driver, 'readonly': False, 'env': {}, 'github_identity': signing, 'repo': 'kun-g/workshop'}
    original = json.dumps(spec)
    env = session_environment(spec, {'PATH': '/global-wrapper', 'GH_TOKEN': 'human', 'GITHUB_TOKEN': 'human',
                                    'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'credential.helper', 'GIT_CONFIG_VALUE_0': 'human'})
    assert env['GH_TOKEN'] == env['GITHUB_TOKEN'] == 'fake-app-token'
    assert env['PATH'].split(os.pathsep)[0] == '/usr/bin'
    assert env['GIT_CONFIG_VALUE_0'] == ''
    assert 'fake-app-token' not in original and json.dumps(spec) == original
    assert all('fake-app-token' not in value for key, value in env.items() if key.startswith('GIT_CONFIG_'))


@pytest.mark.parametrize('stage', ['审查', '验收'])
def test_readonly_never_calls_signer(tmp_path, signing, stage):
    signing['signer'] = ['/does-not-exist']
    spec = {'readonly': True, 'env': {'GH_TOKEN': 'profile-override'}, 'github_identity': signing,
            'repo': 'kun-g/workshop', 'gh_config': str(tmp_path / stage)}
    env = session_environment(spec, {'GH_TOKEN': 'human', 'GITHUB_TOKEN': 'human', 'SSH_AUTH_SOCK': 'sock',
                                    'AWS_SECRET_ACCESS_KEY': 'secret'})
    assert not any(key in env for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'SSH_AUTH_SOCK', 'AWS_SECRET_ACCESS_KEY'))
    assert env['GIT_CONFIG_VALUE_0'] == '' and not list(Path(env['GH_CONFIG_DIR']).iterdir())


def test_inherit_leaves_grant_console_identity_unchanged():
    source = {'GH_TOKEN': 'existing', 'PATH': '/wrappers'}
    assert identity.environment({'mode': 'inherit'}, 'kun-g/grant-console', source) == source


@pytest.mark.parametrize('code,output', [(1, 'leaked-token'), (0, ''), (0, 'two tokens')])
def test_signing_failure_no_action_and_no_secret(tmp_path, signing, monkeypatch, code, output):
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, output, 'private-key-material')
    monkeypatch.setattr(subprocess, 'run', fake)
    @identity.scoped
    def action(target):
        return identity.run(['gh', 'pr', 'merge', '1'], text=True, capture_output=True)
    with pytest.raises(identity.IdentityError) as error:
        action({'repo': 'kun-g/workshop', 'github_identity': signing})
    assert len(calls) == 1 and calls[0][-2:] == ['--repo', 'kun-g/workshop']
    assert 'private-key-material' not in str(error.value) and 'leaked-token' not in str(error.value)


def test_actions_use_real_gh_scope_and_redact_outputs(signing, monkeypatch):
    calls = []
    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-2:] == ['--repo', 'kun-g/workshop']:
            return subprocess.CompletedProcess(argv, 0, 'fake-app-token\n', '')
        assert kwargs['env']['GH_TOKEN'] == 'fake-app-token'
        return subprocess.CompletedProcess(argv, 0, 'ok fake-app-token', 'error fake-app-token')
    monkeypatch.setattr(subprocess, 'run', fake)
    @identity.scoped
    def action(target):
        return [identity.run(['/wrapper/gh', *args], text=True, capture_output=True) for args in
                (['pr', 'merge', '1'], ['issue', 'comment', '1'], ['issue', 'edit', '1', '--add-label', 'ready'])]
    result = action({'repo': 'kun-g/workshop', 'github_identity': signing})
    assert len(calls) == 4 and all(c[0][0] == '/usr/bin/gh' for c in calls[1:])
    assert all('fake-app-token' not in r.stdout + r.stderr for r in result)
    # Scope ended; subsequent inherit call cannot reuse the token.
    @identity.scoped
    def inherited(target):
        return identity._scope.get()['env']
    assert inherited({'repo': 'kun-g/grant-console', 'github_identity': {'mode': 'inherit'}}) is None


def test_git_helper_only_supplies_exact_repository(signing):
    env = identity.environment(signing, 'kun-g/workshop', {'PATH': os.environ['PATH']})
    helper = str(Path(identity.__file__).with_name('github_credential.py'))
    for protocol, host, path, expected in [('https', 'github.com', 'kun-g/workshop.git', True),
                                         ('https', 'github.com', 'kun-g/grant-console.git', False),
                                         ('https', 'evil.example', 'kun-g/workshop.git', False),
                                         ('http', 'github.com', 'kun-g/workshop.git', False)]:
        result = subprocess.run([sys.executable, helper, 'get'], env=env, text=True, capture_output=True,
                                input=f'protocol={protocol}\nhost={host}\npath={path}\n\n', check=True)
        assert ('fake-app-token' in result.stdout) == expected


def test_runner_failure_stops_before_agent_and_submits_once(tmp_path, signing, monkeypatch):
    signing['signer'] = ['/missing-signer']
    spec = {'readonly': False, 'env': {}, 'github_identity': signing, 'repo': 'kun-g/workshop',
            'token': 'round', 'receipt_command': ['workshop']}
    (tmp_path / 'spec.json').write_text(json.dumps(spec))
    submitted = []
    monkeypatch.setattr(runner, 'submit', lambda directory, spec, env, name, receipt: submitted.append(receipt) or {'code': 0})
    monkeypatch.setattr(runner, 'for_spec', lambda spec: pytest.fail('agent preparation must not run'))
    runner.main(tmp_path)
    runner.main(tmp_path)
    assert len(submitted) == 1
    assert submitted[0]['outcome'] == 'nothing' and submitted[0]['leave']['kind'] == '等你'
    assert len(submitted[0]['notice']) == 1
    assert not (tmp_path / 'child.json').exists()


def test_target_blocked_serialized_and_only_one_target_notified(tmp_path):
    config = configuration()
    config['targets']['beta']['plugin'] = 'flow'
    engine = build(tmp_path / 'state', config)
    engine.ingest(event())
    engine.ingest(event(target='beta', key='two'))
    engine.broken('alpha', 'flow', 'flow', TargetBlocked('签发不可用'))
    engine.broken('alpha', 'flow', 'flow', TargetBlocked('签发不可用'))
    assert engine.control('alpha')['broken']
    assert not engine.control('beta').get('broken')
    engine.release_here('alpha:one', '放', 'session-retry')
    assert not engine.control('alpha')['broken']
    assert not engine.log.get('items', 'alpha:one')['plugin_blocked']
    assert engine.log.get('items', 'alpha:one')['state'] == '空闲'
    notices = [c for c in engine.log.all('cards') if c['mode'] == 'notice']
    assert len(notices) == 1
    # Production job boundary preserves the typed failure, not a generic plugin failure.
    class Plugin:
        def fail(self):
            raise identity.IdentityError('signing failed')
    engine.plugins.register('flow', 'failure', Plugin())
    directory = tmp_path / 'job'
    directory.mkdir()
    (directory / 'spec.json').write_text(json.dumps({'port': 'flow', 'name': 'failure', 'method': 'fail', 'args': encode([])}))
    execute(directory, engine.plugins)
    assert read(directory / 'result.json')['error']['kind'] == 'TargetBlocked'
    engine.log.close()


def test_nested_relationship_reads_use_separate_scope_and_restore_write_scope(signing, monkeypatch):
    calls = []
    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-2:] == ['--repo', 'kun-g/workshop']:
            return subprocess.CompletedProcess(argv, 0, 'read-token' if '--relations-read' in argv else 'write-token', '')
        return subprocess.CompletedProcess(argv, 0, kwargs['env']['GH_TOKEN'], '')
    monkeypatch.setattr(subprocess, 'run', fake)
    @identity.scoped(relations_read=True)
    def read(target):
        return identity.run(['gh', 'api', 'graphql'], capture_output=True, text=True)
    @identity.scoped
    def action(target):
        identity.run(['gh', 'api', 'before'], capture_output=True, text=True)
        read(target)
        identity.run(['gh', 'api', 'after'], capture_output=True, text=True)
    action({'repo': 'kun-g/workshop', 'github_identity': signing})
    commands = [(argv, kw['env']['GH_TOKEN']) for argv, kw in calls if argv[0] == '/usr/bin/gh']
    assert [token for _, token in commands] == ['write-token', 'read-token', 'write-token']
    assert sum('--relations-read' in argv for argv, _ in calls) == 1


def test_failed_relationship_signer_never_falls_back_to_existing_write_token(signing, monkeypatch):
    calls = []
    def fake(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, '', 'secret')
    monkeypatch.setattr(subprocess, 'run', fake)
    @identity.scoped(relations_read=True)
    def read(target):
        identity.run(['gh', 'api', 'graphql'])
    with pytest.raises(identity.IdentityError):
        read({'repo': 'kun-g/workshop', 'github_identity': signing})
    assert len(calls) == 1 and '--relations-read' in calls[0]


def test_relation_identity_override_does_not_change_normal_identity(signing):
    target = {'repo': 'kun-g/grant-console', 'github_identity': {'mode': 'inherit'}, 'relations_identity': signing}
    assert identity.configuration(target) == {'mode': 'inherit'}
    assert identity.configuration(target, relations_read=True) == signing
