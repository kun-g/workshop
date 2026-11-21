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


@pytest.fixture
def fresh_signing(tmp_path):
    """每次调用输出不同令牌的假签发器，用来证明每次 get 都现签。"""
    script = tmp_path / 'fresh-signer.py'
    counter = tmp_path / 'fresh-signer.count'
    script.write_text(
        "import os, sys\n"
        "assert sys.argv[1:] == ['--repo', 'kun-g/workshop']\n"
        f"path = {str(counter)!r}\n"
        "n = int(open(path).read()) + 1 if os.path.exists(path) else 1\n"
        "open(path, 'w').write(str(n))\n"
        "print('fresh-token-' + str(n))\n")
    return {'mode': 'app', 'gh': '/usr/bin/gh', 'signer': [sys.executable, str(script)]}, counter


@pytest.mark.parametrize('driver', ['claude', 'codex', 'cursor'])
def test_session_environment_has_signer_not_token_and_preserves_spec(tmp_path, signing, driver):
    spec = {'driver': driver, 'readonly': False, 'env': {}, 'github_identity': signing,
            'repo': 'kun-g/workshop', 'gh_config': str(tmp_path / 'gh-config')}
    original = json.dumps(spec)
    env = session_environment(spec, {'PATH': '/global-wrapper', 'GH_TOKEN': 'human', 'GITHUB_TOKEN': 'human',
                                    'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'credential.helper', 'GIT_CONFIG_VALUE_0': 'human'})
    # 环境里只有签发器配置，没有会过期的令牌。
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
        assert key not in env
    assert json.loads(env['WORKSHOP_GITHUB_SIGNER'])[-1] == str(tmp_path / 'signer.py')
    assert env['WORKSHOP_GITHUB_REPO'] == 'kun-g/workshop'
    bin_dir = Path(env['PATH'].split(os.pathsep)[0])
    assert bin_dir == tmp_path / 'gh-config' / 'bin'
    wrapper = bin_dir / 'gh'
    assert wrapper.exists() and os.access(wrapper, os.X_OK)
    assert 'fake-app-token' not in wrapper.read_text()
    assert env['GIT_CONFIG_VALUE_0'] == ''
    assert 'fake-app-token' not in original and json.dumps(spec) == original
    assert all('fake-app-token' not in value for key, value in env.items() if key.startswith('GIT_CONFIG_'))


def test_git_helper_signs_fresh_for_each_get(tmp_path, fresh_signing):
    signing, _ = fresh_signing
    spec = {'readonly': False, 'env': {}, 'github_identity': signing,
            'repo': 'kun-g/workshop', 'gh_config': str(tmp_path / 'gh-config')}
    env = session_environment(spec, {'PATH': os.environ['PATH']})
    helper = str(Path(identity.__file__).with_name('github_credential.py'))
    tokens = []
    for _ in range(2):
        result = subprocess.run([sys.executable, helper, 'get'], env=env, text=True, capture_output=True, check=True,
                                input='protocol=https\nhost=github.com\npath=kun-g/workshop.git\n\n')
        tokens.append(result.stdout.split('password=', 1)[1].strip())
    assert tokens == ['fresh-token-1', 'fresh-token-2']


def test_git_credential_fill_uses_the_fresh_helper_twice(tmp_path, fresh_signing):
    signing, _ = fresh_signing
    spec = {'readonly': False, 'env': {}, 'github_identity': signing,
            'repo': 'kun-g/workshop', 'gh_config': str(tmp_path / 'gh-config')}
    env = session_environment(spec, {'PATH': os.environ['PATH']})
    tokens = []
    for _ in range(2):
        result = subprocess.run(['/usr/bin/git', 'credential', 'fill'], env=env, text=True,
                                capture_output=True, check=True,
                                input='protocol=https\nhost=github.com\npath=kun-g/workshop.git\n\n')
        tokens.append(dict(line.split('=', 1) for line in result.stdout.splitlines())['password'])
    assert tokens == ['fresh-token-1', 'fresh-token-2']


def test_gh_wrapper_signs_fresh_for_each_call(tmp_path, fresh_signing):
    signing, _ = fresh_signing
    real = tmp_path / 'real-gh'
    real.write_text('#!/bin/sh\nprintf "%s\\n" "$GH_TOKEN"\n')
    real.chmod(0o755)
    signing['gh'] = str(real)
    spec = {'readonly': False, 'env': {}, 'github_identity': signing,
            'repo': 'kun-g/workshop', 'gh_config': str(tmp_path / 'gh-config')}
    env = session_environment(spec, {'PATH': '/usr/bin:/bin'})
    wrapper = str(Path(env['PATH'].split(os.pathsep)[0]) / 'gh')
    assert 'fresh-token' not in Path(wrapper).read_text()
    first = subprocess.run([wrapper, 'api', 'repos/kun-g/workshop'], env=env, text=True, capture_output=True, check=True)
    second = subprocess.run([wrapper, 'api', 'repos/kun-g/workshop'], env=env, text=True, capture_output=True, check=True)
    assert first.stdout.strip() == 'fresh-token-1'
    assert second.stdout.strip() == 'fresh-token-2'


def test_signing_failure_fails_git_and_gh_and_never_uses_an_old_token(tmp_path):
    script = tmp_path / 'fail-signer.py'
    script.write_text(
        "import json, sys\n"
        "print('SIGNING_ERROR ' + json.dumps({'kind': 'keychain', 'detail': '读不到私钥'}), file=sys.stderr)\n"
        "print('leaked-token')\n"
        "sys.exit(1)\n")
    signing = {'mode': 'app', 'gh': str(tmp_path / 'real-gh'), 'signer': [sys.executable, str(script)]}
    spec = {'readonly': False, 'env': {}, 'github_identity': signing,
            'repo': 'kun-g/workshop', 'gh_config': str(tmp_path / 'gh-config')}
    env = session_environment(spec, {'PATH': os.environ['PATH']})
    # 即使有人塞了旧令牌，有签发器配置时也不能退回去用。
    env['GH_TOKEN'] = env['GITHUB_TOKEN'] = 'stale-human-token'
    helper = str(Path(identity.__file__).with_name('github_credential.py'))
    git = subprocess.run([sys.executable, helper, 'get'], env=env, text=True, capture_output=True,
                         input='protocol=https\nhost=github.com\npath=kun-g/workshop.git\n\n')
    assert git.returncode != 0 and 'github 凭据签发失败' in git.stderr and 'keychain: 读不到私钥' in git.stderr
    assert 'stale-human-token' not in git.stdout + git.stderr and 'leaked-token' not in git.stdout + git.stderr
    wrapper = str(Path(env['PATH'].split(os.pathsep)[0]) / 'gh')
    gh = subprocess.run([wrapper, 'api', 'repos/kun-g/workshop'], env=env, text=True, capture_output=True)
    assert gh.returncode != 0 and 'github gh 签发失败' in gh.stderr and 'keychain: 读不到私钥' in gh.stderr
    assert 'stale-human-token' not in gh.stdout + gh.stderr and 'leaked-token' not in gh.stdout + gh.stderr


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


def test_signing_reason_reads_only_the_marked_line_and_scrubs_secrets():
    stderr = ('warning: private-key-material\n'
              'SIGNING_ERROR not-json\n'
              'SIGNING_ERROR ' + json.dumps({'kind': 'keychain', 'detail':
                  '读不到私钥 ghs_abcdefghijklmnopqrstuvwxyz0123456789'}))
    reason = identity.signing_reason(stderr)
    assert reason.startswith('keychain: 读不到私钥')
    assert 'ghs_' not in reason and '[已隐藏]' in reason and 'private-key-material' not in reason


def test_sign_failure_carries_classified_reason_without_signer_output(signing, monkeypatch):
    def fake(argv, **kwargs):
        stderr = ('private-key-material\nSIGNING_ERROR ' +
                  json.dumps({'kind': 'github', 'detail': '/app 返回 HTTP 403：Bad credentials'}))
        return subprocess.CompletedProcess(argv, 1, '', stderr)
    monkeypatch.setattr(subprocess, 'run', fake)
    with pytest.raises(identity.IdentityError) as error:
        identity.sign(signing, 'kun-g/workshop', {'PATH': '/usr/bin'})
    message = str(error.value)
    assert 'github: /app 返回 HTTP 403：Bad credentials' in message
    assert 'private-key-material' not in message
    # 签发失败按瞬时故障处理，核心会退避重试而不是永久隔离。
    from workshop.core.ports import Retryable
    assert isinstance(error.value, Retryable)


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
            'token': 'round', 'receipt_command': ['workshop'], 'gh_config': str(tmp_path / 'gh')}
    (tmp_path / 'spec.json').write_text(json.dumps(spec))
    submitted = []
    monkeypatch.setattr(runner, 'submit', lambda directory, spec, env, name, receipt: submitted.append(receipt) or {'code': 0})
    monkeypatch.setattr(runner, 'for_spec', lambda spec: pytest.fail('agent preparation must not run'))
    # 开工时不再现签（现签推迟到每次 git/gh 调用）；这里模拟开工前仍会失败的配置检查。
    def broken(spec, base=None):
        raise identity.IdentityError('签发配置不可用')
    monkeypatch.setattr(runner, 'session_environment', broken)
    runner.main(tmp_path)
    runner.main(tmp_path)
    assert len(submitted) == 1
    assert submitted[0]['outcome'] == 'nothing' and submitted[0]['leave']['kind'] == '等你'
    assert 'notice' not in submitted[0] and 'ask' not in submitted[0]  # 回执不再带通知（介入点 R13）。
    assert not (tmp_path / 'child.json').exists()


def test_target_blocked_serialized_without_notice(tmp_path):
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
    assert len(notices) == 0
    # Production job boundary preserves the typed failure, not a generic plugin failure.
    class Plugin:
        def fail(self):
            raise identity.IdentityError('signing failed')
    engine.plugins.register('flow', 'failure', Plugin())
    directory = tmp_path / 'job'
    directory.mkdir()
    (directory / 'spec.json').write_text(json.dumps({'port': 'flow', 'name': 'failure', 'method': 'fail', 'args': encode([])}))
    execute(directory, engine.plugins)
    # 签发失败是可自动重试的瞬时故障：作业边界保留 Retryable，核心据此退避而不是永久标坏。
    assert read(directory / 'result.json')['error']['kind'] == 'Retryable'
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
