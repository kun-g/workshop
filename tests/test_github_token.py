import base64
import json
import subprocess
from types import SimpleNamespace

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
