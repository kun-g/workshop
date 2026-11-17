"""Process-local GitHub credentials shared by adapters, never by the core.

Rules contain only a signer command and executable paths. A scope lazily signs once,
keeps the token in memory and redacts command output before it reaches any journal.
"""
from contextvars import ContextVar
from functools import wraps
from inspect import signature
import os
from pathlib import Path
import subprocess

from workshop.core.ports import TargetBlocked


class IdentityError(TargetBlocked):
    pass


def configuration(target, *, relations_read=False):
    config = target.get('github_identity', {'mode': 'inherit'})
    if relations_read:
        config = target.get('relations_identity', config)
    if not isinstance(config, dict) or config.get('mode') not in ('inherit', 'app'):
        raise ValueError('github_identity.mode 必须是 inherit 或 app')
    allowed = {'mode'} if config['mode'] == 'inherit' else {'mode', 'signer', 'gh'}
    if set(config) - allowed:
        raise ValueError('github_identity 有未知字段')
    if config['mode'] == 'app':
        if not isinstance(target.get('repo'), str) or len(target['repo'].split('/')) != 2:
            raise ValueError('App 身份必须指定仓库')
        if (not isinstance(config.get('signer'), list) or not config['signer']
                or not all(isinstance(s, str) and s.strip() for s in config['signer'])):
            raise ValueError('App 身份必须指定 signer argv')
        if not isinstance(config.get('gh'), str) or not Path(config['gh']).expanduser().is_absolute():
            raise ValueError('App 身份必须指定真实 gh 的绝对路径')
    return dict(config)


def sign(config, repo, source=None, *, relations_read=False):
    env = dict(os.environ if source is None else source)
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
        env.pop(key, None)
    env['AGENT_KIND'] = 'workshop'
    argv = [str(Path(p).expanduser()) if p.startswith('~') else p for p in config['signer']]
    if relations_read:
        argv.append('--relations-read')
    try:
        result = subprocess.run([*argv, '--repo', repo], env=env, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise IdentityError(f'{repo} App 签发不可用或超时；未执行动作，无个人身份回退') from None
    # Neither successful nor failed signer output may be included in an exception.
    token = result.stdout.strip()
    if result.returncode or not token or any(c.isspace() for c in token):
        raise IdentityError(f'{repo} App 签发失败（exit {result.returncode} 或空/无效令牌）；未执行动作，无个人身份回退')
    return token


def environment(config, repo, source=None, *, relations_read=False):
    env = dict(os.environ if source is None else source)
    if config['mode'] == 'inherit':
        return env
    token = sign(config, repo, env, relations_read=relations_read)
    # A global wrapper can discard a supplied token. Use the configured real gh,
    # and disable inherited git helpers/headers which may choose the human account.
    for key in list(env):
        if key.startswith(('GIT_CONFIG_', 'AGENT_GH_')) or key in ('GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
            env.pop(key, None)
    env.update(GH_TOKEN=token, GITHUB_TOKEN=token, WORKSHOP_GITHUB_REPO=repo,
               GH_PROMPT_DISABLED='1', GIT_TERMINAL_PROMPT='0',
               GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1')
    helper = Path(__file__).with_name('github_credential.py')
    import shlex
    import sys
    settings = [('credential.helper', ''), ('credential.helper', '!' + shlex.join([sys.executable, str(helper)])),
                ('credential.useHttpPath', 'true'), ('http.extraHeader', '')]
    env['GIT_CONFIG_COUNT'] = str(len(settings))
    for n, (key, value) in enumerate(settings):
        env[f'GIT_CONFIG_KEY_{n}'], env[f'GIT_CONFIG_VALUE_{n}'] = key, value
    return env


_scope = ContextVar('workshop_github_identity', default=None)


def scoped(function=None, *, relations_read=False):
    """Bound only at adapter entry points; nested calls reuse the same credential."""
    if function is None:
        return lambda fn: scoped(fn, relations_read=relations_read)
    sig = signature(function)
    @wraps(function)
    def call(*args, **kwargs):
        target = sig.bind(*args, **kwargs).arguments['target']
        config = configuration(target, relations_read=relations_read)
        current = _scope.get()
        if (current and current['repo'] == target.get('repo') and current['config'] == config
                and current['relations_read'] == relations_read):
            return function(*args, **kwargs)
        marker = _scope.set({'config': config, 'repo': target.get('repo'), 'env': None,
                             'relations_read': relations_read})
        try:
            return function(*args, **kwargs)
        finally:
            _scope.reset(marker)
    return call


def run(argv, **kwargs):
    """Use identity only for gh/git; explicit clean check environments stay clean."""
    state = _scope.get()
    active = (state and state['config']['mode'] == 'app' and kwargs.get('env') is None
              and Path(argv[0]).name in ('gh', 'git'))
    if not active:
        return subprocess.run(argv, **kwargs)
    if state['env'] is None:
        state['env'] = environment(state['config'], state['repo'], relations_read=state['relations_read'])
    env = state['env']
    argv = list(argv)
    if Path(argv[0]).name == 'gh':
        argv[0] = str(Path(state['config']['gh']).expanduser())
    kwargs['env'] = env
    try:
        result = subprocess.run(argv, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        raise TargetBlocked('GitHub 命令未确认完成；请回查后继续') from None
    token = env['GH_TOKEN']
    for field in ('stdout', 'stderr'):
        value = getattr(result, field)
        if isinstance(value, str):
            setattr(result, field, value.replace(token, '[redacted]'))
        elif isinstance(value, bytes):
            setattr(result, field, value.replace(token.encode(), b'[redacted]'))
    return result
