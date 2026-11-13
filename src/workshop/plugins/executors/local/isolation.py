"""Minimal per-session HOME for read-only processes; no repository preparation."""
import json
import os
from pathlib import Path
import subprocess
import sys

from .storage import write


def keychain(service, account=None):
    """Read just the named CLI subscription item, never enumerate the user's keychain."""
    if sys.platform != 'darwin':
        return None
    argv = ['/usr/bin/security', 'find-generic-password', '-s', service, '-w']
    if account:
        argv += ['-a', account]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    return result.stdout.strip() if result.returncode == 0 else None


def json_file(path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('认证文件必须是 JSON 对象')
    return value


def subscription_auth(kind, source_home, dest_home, source, read_keychain):
    """Copy allowlisted subscription fields only, keeping this session's refreshed auth on resume."""
    if kind == 'codex':
        destination = dest_home / '.codex/auth.json'
        if destination.exists():
            return
        auth = json_file(source_home / '.codex/auth.json')
        tokens = auth.get('tokens')
        if isinstance(tokens, dict):
            clean = {key: tokens[key] for key in ('id_token', 'access_token', 'refresh_token', 'account_id') if key in tokens}
            write(destination, {'auth_mode': 'chatgpt', 'tokens': clean})
    elif kind == 'cursor-grok':
        destination = dest_home / '.cursor/auth.json'
        if destination.exists():
            return
        auth = json_file(source_home / '.cursor/auth.json')
        clean = {key: auth[key] for key in ('accessToken', 'refreshToken') if auth.get(key)}
        if not clean.get('accessToken'):
            for field, service in (('accessToken', 'cursor-access-token'), ('refreshToken', 'cursor-refresh-token')):
                value = read_keychain(service, 'cursor-user')
                if value:
                    clean[field] = value
        if clean:
            write(destination, clean)
    else:
        destination = dest_home / '.claude/.credentials.json'
        if destination.exists():
            return
        auth = json_file(source_home / '.claude/.credentials.json')
        if not isinstance(auth.get('claudeAiOauth'), dict):
            value = read_keychain('Claude Code-credentials')
            auth = json.loads(value) if value else {}
        oauth = auth.get('claudeAiOauth')
        if isinstance(oauth, dict):
            fields = ('accessToken', 'refreshToken', 'expiresAt', 'refreshTokenExpiresAt', 'scopes',
                      'subscriptionType', 'subscriptionTypeSource', 'rateLimitTier')
            write(destination, {'claudeAiOauth': {key: oauth[key] for key in fields if key in oauth}})
        elif source.get('CLAUDE_CODE_OAUTH_TOKEN'):
            # Explicit subscription token is the only inherited credential fallback.
            write(destination, {'claudeAiOauth': {'accessToken': source['CLAUDE_CODE_OAUTH_TOKEN']}})


def environment(home: Path, kind: str, source_home: Path, source=None, read_keychain=keychain):
    source = os.environ if source is None else source
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    # Allowlist excludes cloud credentials, credential-file overrides, user config roots,
    # language preload hooks and arbitrary secrets, including names we don't yet know.
    keep = {'PATH', 'LANG', 'TZ', 'TERM', 'COLORTERM', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
            'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}
    result = {k: v for k, v in source.items() if k in keep or k.startswith('LC_')}
    result.update(HOME=str(home), GH_CONFIG_DIR=str(home / '.config/gh'),
                  XDG_CONFIG_HOME=str(home / '.config'), XDG_CACHE_HOME=str(home / '.cache'),
                  XDG_DATA_HOME=str(home / '.local/share'), XDG_STATE_HOME=str(home / '.local/state'),
                  TMPDIR=str(home / 'tmp'),
                  GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
                  GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='credential.helper', GIT_CONFIG_VALUE_0='')
    for path in (home / '.config/gh', home / 'tmp'):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    subscription_auth(kind, source_home, home, source, read_keychain)
    if kind == 'codex':
        result['CODEX_HOME'] = str(home / '.codex')
    elif kind == 'cursor-grok':
        result['AGENT_CLI_CREDENTIAL_STORE'] = 'file'
    else:
        result['CLAUDE_CONFIG_DIR'] = str(home / '.claude')
    return result
