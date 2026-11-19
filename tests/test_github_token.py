import base64
import io
import json
import subprocess
import sys
from types import SimpleNamespace
import urllib.error

import pytest

from workshop import github_token


@pytest.fixture
def minting(tmp_path, monkeypatch):
    config = {'client_id': 'test', 'repositories': ['owner/source', 'owner/other', 'elsewhere/repo'],
              'repo_permissions': {'owner/source': {'issues': 'write', 'contents': 'write'},
                                   'owner/other': {'issues': 'read'}}}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    args = SimpleNamespace(config=str(path), repo='owner/source', app='test-app', key_service='test',
                           key_account='test', relations_read=True)
    calls = []
    def run(argv, **kwargs):
        output = base64.b64encode(b'test-key') if argv[0].endswith('security') else b'signature'
        return subprocess.CompletedProcess(argv, 0, output, b'')
    monkeypatch.setattr(subprocess, 'run', run)
    def api(endpoint, token, data=None):
        calls.append((endpoint, data))
        if endpoint == '/app':
            return {'slug': 'test-app'}
        if endpoint.endswith('/installation'):
            return {'id': 1}
        if endpoint.startswith('/installation/repositories?'):
            return {'total_count': 2, 'repositories': [{'full_name': 'owner/source'}, {'full_name': 'owner/other'}]}
        return {'token': 'read-token', 'permissions': data['permissions']}
    monkeypatch.setattr(github_token, 'api', api)
    return args, config, path, calls


def test_relations_include_all_allowed_same_installation_repositories_readonly(minting):
    args, _, _, calls = minting
    assert github_token.mint(args) == 'read-token'
    assert ('/app/installations/1/access_tokens', {
        'repositories': ['other', 'source'], 'permissions': {'issues': 'read', 'metadata': 'read'}}) in calls
    assert ('/repos/owner/other/installation', None) in calls


def test_regular_actions_stay_single_repository(minting):
    args, _, _, calls = minting
    args.relations_read = False
    github_token.mint(args)
    assert calls[-1][1] == {'repositories': ['source'], 'permissions': {'issues': 'write', 'contents': 'write'}}


def test_missing_read_permission_does_not_silently_shrink_scope(minting):
    args, config, path, calls = minting
    del config['repo_permissions']['owner/other']
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match='explicitly allow'):
        github_token.mint(args)
    assert calls == []


def test_other_installation_does_not_issue_partial_token(minting, monkeypatch):
    args, _, _, calls = minting
    original = github_token.api
    def api(endpoint, token, data=None):
        return {'id': 2} if endpoint == '/repos/owner/other/installation' else original(endpoint, token, data)
    monkeypatch.setattr(github_token, 'api', api)
    with pytest.raises(ValueError, match='span installations'):
        github_token.mint(args)
    assert not any(path.endswith('/access_tokens') for path, _ in calls)


def test_wrong_granted_permissions_fail_closed(minting, monkeypatch):
    args, _, _, _ = minting
    original = github_token.api
    def api(endpoint, token, data=None):
        return {'token': 'must-not-escape', 'permissions': {'issues': 'write'}} if data else original(endpoint, token, data)
    monkeypatch.setattr(github_token, 'api', api)
    with pytest.raises(ValueError, match='read-only'):
        github_token.mint(args)


def test_token_missing_configured_repository_is_not_usable(minting, monkeypatch):
    args, _, _, _ = minting
    original = github_token.api
    def api(endpoint, token, data=None):
        if endpoint.startswith('/installation/repositories?'):
            return {'total_count': 1, 'repositories': [{'full_name': 'owner/source'}]}
        return original(endpoint, token, data)
    monkeypatch.setattr(github_token, 'api', api)
    with pytest.raises(ValueError, match='coverage is incomplete'):
        github_token.mint(args)


def test_keychain_failure_is_classified_without_secret(minting, monkeypatch):
    args, _, _, _ = minting
    def run(argv, **kwargs):
        raise subprocess.CalledProcessError(44, argv, b'', b'user canceled private-key-material')
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(github_token.SigningFailure) as error:
        github_token.mint(args)
    assert error.value.kind == 'keychain'
    assert 'security exit 44' in error.value.detail
    assert 'private-key-material' not in error.value.detail


def test_github_http_failure_keeps_status_and_message_but_not_body_secrets():
    jwt = 'eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiJ0ZXN0In0.c2lnbmF0dXJlLXNlY3JldA'
    def fail(request, timeout):
        body = io.BytesIO(json.dumps({'message': 'Bad credentials ghs_abcdefghijklmnopqrstuvwxyz0123456789 ' + jwt}).encode())
        raise urllib.error.HTTPError(request.full_url, 403, 'Forbidden', {}, body)
    import workshop.github_token as module
    original = module.urllib.request.urlopen
    module.urllib.request.urlopen = fail
    try:
        with pytest.raises(module.SigningFailure) as error:
            module.api('/app', 'jwt')
    finally:
        module.urllib.request.urlopen = original
    assert error.value.kind == 'github'
    assert '/app 返回 HTTP 403' in error.value.detail and 'Bad credentials' in error.value.detail
    assert 'ghs_' not in error.value.detail and jwt not in error.value.detail
    assert '[已隐藏]' in error.value.detail


def test_network_failure_is_classified():
    import workshop.github_token as module
    original = module.urllib.request.urlopen
    def fail(request, timeout):
        raise urllib.error.URLError(ConnectionRefusedError(61, 'Connection refused'))
    module.urllib.request.urlopen = fail
    try:
        with pytest.raises(module.SigningFailure) as error:
            module.api('/app', 'jwt')
    finally:
        module.urllib.request.urlopen = original
    assert error.value.kind == 'network' and '/app 网络错误' in error.value.detail


def test_main_prints_one_classified_line_and_never_the_token(minting, monkeypatch, capsys):
    args, _, path, _ = minting
    monkeypatch.setattr(sys, 'argv', ['github_token', '--repo', args.repo, '--config', str(path),
                                      '--app', args.app, '--key-service', args.key_service,
                                      '--key-account', args.key_account])
    def mint(_):
        raise github_token.SigningFailure('github', 'token ghs_abcdefghijklmnopqrstuvwxyz0123456789 rejected')
    monkeypatch.setattr(github_token, 'mint', mint)
    assert github_token.main() == 1
    out, err = capsys.readouterr()
    assert out == ''
    assert err.startswith('SIGNING_ERROR ')
    reason = json.loads(err.removeprefix('SIGNING_ERROR '))
    assert reason['kind'] == 'github' and 'ghs_' not in reason['detail'] and '[已隐藏]' in reason['detail']


def test_main_unexpected_failure_keeps_only_the_exception_type(minting, monkeypatch, capsys):
    args, _, path, _ = minting
    monkeypatch.setattr(sys, 'argv', ['github_token', '--repo', args.repo, '--config', str(path),
                                      '--app', args.app, '--key-service', args.key_service,
                                      '--key-account', args.key_account])
    def mint(_):
        raise RuntimeError('private-key-material')
    monkeypatch.setattr(github_token, 'mint', mint)
    assert github_token.main() == 1
    out, err = capsys.readouterr()
    assert out == '' and 'private-key-material' not in err
    assert json.loads(err.removeprefix('SIGNING_ERROR ')) == {'kind': 'unknown', 'detail': 'RuntimeError'}
