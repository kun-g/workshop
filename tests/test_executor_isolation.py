"""No credential file access, copied auth or real keychain calls."""
import json
import os
from pathlib import Path

from workshop.plugins.executors.local.isolation import environment, session_environment


def test_keep_original_home_and_login_but_remove_deployment_credentials(tmp_path):
    home=tmp_path/'real-home'
    auth=home/'.codex/auth.json'
    auth.parent.mkdir(parents=True)
    auth.write_text('{"tokens":{"refresh_token":"must-not-copy"}}')
    source={'HOME':str(home),'PATH':'/bin','GH_TOKEN':'secret','GITHUB_TOKEN':'secret','SSH_AUTH_SOCK':'socket',
        'AWS_ACCESS_KEY_ID':'aws','AZURE_CLIENT_SECRET':'azure','GOOGLE_APPLICATION_CREDENTIALS':'google',
        'CLOUDFLARE_API_TOKEN':'cloudflare','DIGITALOCEAN_ACCESS_TOKEN':'digitalocean','KUBECONFIG':'kube',
        'CODEX_HOME':str(home/'.codex'),'CLAUDE_CONFIG_DIR':str(home/'.claude'),
        'CLAUDE_CODE_OAUTH_TOKEN':'subscription-token','GIT_CONFIG_COUNT':'3','GIT_CONFIG_VALUE_0':'helper'}
    gh=tmp_path/'round/gh-config'
    env=environment(gh,source)
    for key in ('GH_TOKEN','GITHUB_TOKEN','SSH_AUTH_SOCK','AWS_ACCESS_KEY_ID','AZURE_CLIENT_SECRET',
                'GOOGLE_APPLICATION_CREDENTIALS','CLOUDFLARE_API_TOKEN','DIGITALOCEAN_ACCESS_TOKEN'):
        assert key not in env
    assert env['HOME']==str(home) and env['CODEX_HOME']==source['CODEX_HOME']
    assert env['CLAUDE_CODE_OAUTH_TOKEN']=='subscription-token'
    assert env['GH_CONFIG_DIR']==str(gh) and not list(gh.iterdir())
    for key in ('KUBECONFIG','AWS_CONFIG_FILE','AWS_SHARED_CREDENTIALS_FILE','GIT_CONFIG_GLOBAL'):
        assert env[key]=='/dev/null'
    assert env['GIT_CONFIG_NOSYSTEM']=='1' and env['GIT_TERMINAL_PROMPT']=='0'
    assert env['GIT_CONFIG_VALUE_0']==''
    assert list(tmp_path.rglob('auth.json'))==[auth]
    assert json.loads(auth.read_text())['tokens']['refresh_token']=='must-not-copy'


def test_terminal_environment_is_the_base_and_spec_contains_only_additions(tmp_path):
    base={'HOME':'/real/home','PATH':'/login/bin','LOGIN_ONLY':'kept','CLAUDECODE':'old',
          'WORKSHOP_RULES':'stale','ANTHROPIC_API_KEY':'metered-secret'}
    spec={'readonly':False,'billing':'subscription','env':{'WORKSHOP_ITEM':'alpha:one','WORKSHOP_ROUND_TOKEN':'round'}}
    result=session_environment(spec,base)
    assert result['LOGIN_ONLY']=='kept' and result['PATH']=='/login/bin'
    assert result['HOME']=='/real/home' and result['WORKSHOP_ITEM']=='alpha:one'
    assert 'CLAUDECODE' not in result and 'WORKSHOP_RULES' not in result and 'ANTHROPIC_API_KEY' not in result
    assert spec['env']=={'WORKSHOP_ITEM':'alpha:one','WORKSHOP_ROUND_TOKEN':'round'}
