"""No real credentials, keychain, authentication calls or network in these tests."""
import json
from pathlib import Path
import stat

import pytest
from workshop.plugins.executors.local.isolation import environment


def save(home, name, value):
    path = home/name
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.mark.parametrize('kind', ['codex', 'cursor-grok', 'claude', 'orca-claude'])
def test_only_selected_cli_subscription_auth_survives(tmp_path, kind):
    original, isolated = tmp_path/'original',tmp_path/'isolated'
    save(original,'.codex/auth.json',{'auth_mode':'chatgpt','tokens':{'access_token':'codex-oauth','refresh_token':'refresh'},'OPENAI_API_KEY':'paid-key'})
    save(original,'.codex/.credentials.json',{'mcp-secret':'never-copy'})
    save(original,'.cursor/auth.json',{'accessToken':'cursor-oauth','refreshToken':'refresh','apiKey':'paid-key','bedrockCredentials':{'secretKey':'aws'}})
    save(original,'.claude/.credentials.json',{'claudeAiOauth':{'accessToken':'claude-oauth','refreshToken':'refresh'},'mcpOAuth':{'secret':'never-copy'}})
    save(original,'.config/gh/hosts.yml',{'oauth_token':'github-secret'})
    source={'HOME':str(original),'PATH':'/bin:/usr/bin','LANG':'en_US.UTF-8','GH_TOKEN':'github-secret',
        'GITHUB_TOKEN':'github-secret','GH_CONFIG_DIR':str(original/'.config/gh'),'SSH_AUTH_SOCK':'ssh-secret',
        'AWS_ACCESS_KEY_ID':'aws','AWS_SHARED_CREDENTIALS_FILE':'aws-file','AZURE_CLIENT_SECRET':'azure',
        'GOOGLE_APPLICATION_CREDENTIALS':'gcp','CLOUDFLARE_API_TOKEN':'cf','DIGITALOCEAN_ACCESS_TOKEN':'do',
        'CODEX_HOME':str(original/'.codex'),'CLAUDE_CONFIG_DIR':str(original/'.claude'),
        'XDG_CONFIG_HOME':str(original/'.config'),'NODE_OPTIONS':'preload-secrets',
        'OPENAI_API_KEY':'paid-key','ANTHROPIC_API_KEY':'paid-key','CURSOR_API_KEY':'paid-key'}
    env=environment(isolated,kind,original,source,read_keychain=lambda *a:None)
    assert not any(key in env for key in source if key not in ('HOME','PATH','LANG','GH_CONFIG_DIR','CODEX_HOME','CLAUDE_CONFIG_DIR','XDG_CONFIG_HOME'))
    assert env['HOME']==str(isolated)
    assert env['GH_CONFIG_DIR']==str(isolated/'.config/gh')
    assert env['GIT_CONFIG_GLOBAL']=='/dev/null' and env['GIT_CONFIG_NOSYSTEM']=='1'
    assert env['GIT_TERMINAL_PROMPT']=='0' and env['GIT_CONFIG_VALUE_0']==''
    files=[p for p in isolated.rglob('*') if p.is_file()]
    assert len(files)==1
    content=files[0].read_text()
    assert 'never-copy' not in content and 'paid-key' not in content and 'github-secret' not in content
    assert stat.S_IMODE(files[0].stat().st_mode)==0o600
    assert not list((isolated/'.config/gh').iterdir())
    if kind=='codex':
        assert files[0]==isolated/'.codex/auth.json' and 'codex-oauth' in content
    elif kind=='cursor-grok':
        assert env['AGENT_CLI_CREDENTIAL_STORE']=='file'
        assert files[0]==isolated/'.cursor/auth.json' and 'cursor-oauth' in content
    else:
        assert files[0]==isolated/'.claude/.credentials.json' and 'claude-oauth' in content


@pytest.mark.parametrize('kind', ['claude','cursor-grok'])
def test_only_named_keychain_items_and_no_mcp_or_cloud_credentials(tmp_path,kind):
    calls=[]
    def fake_keychain(service,account=None):
        calls.append((service,account))
        return {'Claude Code-credentials':json.dumps({'claudeAiOauth':{'accessToken':'oauth'},'mcpOAuth':{'secret':'no'}}),
                'cursor-access-token':'oauth','cursor-refresh-token':'refresh'}[service]
    home=tmp_path/'home'
    environment(home,kind,tmp_path/'source',{},fake_keychain)
    if kind=='claude':
        assert calls==[('Claude Code-credentials',None)]
        auth=json.loads((home/'.claude/.credentials.json').read_text())
        assert set(auth)=={'claudeAiOauth'}
    else:
        assert calls==[('cursor-access-token','cursor-user'),('cursor-refresh-token','cursor-user')]
        assert set(json.loads((home/'.cursor/auth.json').read_text()))=={'accessToken','refreshToken'}


def test_preparing_same_home_keeps_refreshed_auth_and_generated_history(tmp_path):
    original,home=tmp_path/'original',tmp_path/'home'
    save(original,'.codex/auth.json',{'tokens':{'access_token':'old'}})
    environment(home,'codex',original,{},lambda *a:None)
    save(home,'.codex/auth.json',{'tokens':{'access_token':'refreshed'}})
    save(home,'.codex/sessions/current.jsonl',{'session':'history'})
    environment(home,'codex',original,{},lambda *a:None)
    assert json.loads((home/'.codex/auth.json').read_text())['tokens']['access_token']=='refreshed'
    assert (home/'.codex/sessions/current.jsonl').exists()
    assert json.loads((original/'.codex/auth.json').read_text())['tokens']['access_token']=='old'


def test_history_moves_without_credentials_and_cursor_cwd_is_remapped(tmp_path):
    import hashlib
    from workshop.plugins.executors.local.history import archive_and_remove_home,restore_history
    old,new=tmp_path/'old',tmp_path/'new'
    save(old/'home','.cursor/auth.json',{'accessToken':'never-archive'})
    old_key=hashlib.md5(b'/old-snapshot').hexdigest()
    new_key=hashlib.md5(b'/new-snapshot').hexdigest()
    save(old/'home',f'.cursor/chats/{old_key}/session/store.db',{'history':'kept'})
    archive_and_remove_home(old,'cursor-grok')
    assert not (old/'home').exists()
    assert not (old/'records/.cursor/auth.json').exists()
    restore_history(old,new/'home','cursor-grok','/old-snapshot','/new-snapshot')
    assert (new/f'home/.cursor/chats/{new_key}/session/store.db').exists()
    assert not (new/'home/.cursor/auth.json').exists()
