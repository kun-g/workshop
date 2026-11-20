"""Process-local GitHub credentials shared by adapters, never by the core.

Rules contain only a signer command and executable paths. A scope lazily signs once,
keeps the token in memory and redacts command output before it reaches any journal.
"""
from contextvars import ContextVar
from functools import wraps
from inspect import signature
import json
import os
from pathlib import Path
import re
import subprocess

from workshop.core.ports import Retryable, TargetBlocked


class IdentityError(TargetBlocked, Retryable):
    """App 令牌一时签不出：当前目标缺动作前提，但属于可自动重试的瞬时故障。"""


# 签发器只回一行固定前缀的分类结果；其余 stderr 一律不进异常（可能含密钥）。
SIGNING_ERROR = 'SIGNING_ERROR '
SECRET = re.compile(r'(?i)(bearer\s+)\S+'
                    r'|\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'
                    r'|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')


def signing_reason(stderr):
    for line in reversed((stderr or '').splitlines()):
        if not line.startswith(SIGNING_ERROR):
            continue
        try:
            value = json.loads(line[len(SIGNING_ERROR):])
        except ValueError:
            continue
        kind, detail = value.get('kind'), value.get('detail')
        if isinstance(kind, str) and isinstance(detail, str):
            return SECRET.sub('[已隐藏]', f'{kind}: {detail}')[:300]
    return ''


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
    argv.extend(['--repo', repo])
    try:
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = _failure_kind(exc)
        command = _safe_argv(argv, os.environ if source is None else source)
        raise IdentityError(f'{repo} App 签发失败：{reason}；argv: {command}；未执行动作，无个人身份回退') from None
    # Neither successful nor failed signer output may be included in an exception.
    # 例外：签发器自己写的一行分类结果（固定前缀、已去掉疑似密钥）只用来查原因。
    token = result.stdout.strip()
    if result.returncode or not token or any(c.isspace() for c in token):
        reason = signing_reason(result.stderr)
        raise IdentityError(f'{repo} App 签发失败（exit {result.returncode} 或空/无效令牌）'
                            + (f'；{reason}' if reason else '') + '；未执行动作，无个人身份回退')
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


def run_read(argv, **kwargs):
    """Only for side-effect-free commands: an unconfirmed read is safe to retry."""
    return _run(argv, kwargs, read_only=True)


def run(argv, **kwargs):
    """An unconfirmed gh/git action must be checked before it can be repeated."""
    return _run(argv, kwargs, read_only=False)


def _redact(value, env):
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
        if token := env.get(key):
            if isinstance(value, bytes):
                value = value.replace(token.encode(), b'[redacted]')
            else:
                value = value.replace(token, '[redacted]')
    return value


def _safe_argv(argv, env):
    return json.dumps([SECRET.sub('[redacted]', _redact(str(arg), env)) for arg in argv], ensure_ascii=False)


def _failure_kind(exc):
    if isinstance(exc, subprocess.TimeoutExpired):
        return f'TimeoutExpired（超时 {exc.timeout:g} 秒）'
    return type(exc).__name__


def _run(argv, kwargs, *, read_only):
    state = _scope.get()
    executable = Path(argv[0]).name
    if executable not in ('gh', 'git') and not read_only:
        return subprocess.run(argv, **kwargs)
    if executable in ('gh', 'git') and kwargs.get('env') is None and state and state['config']['mode'] == 'app':
        if state['env'] is None:
            state['env'] = environment(state['config'], state['repo'], relations_read=state['relations_read'])
        kwargs['env'] = state['env']
        if executable == 'gh':
            argv = [str(Path(state['config']['gh']).expanduser()), *argv[1:]]
    env = kwargs.get('env') if kwargs.get('env') is not None else os.environ
    try:
        result = subprocess.run(argv, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if not read_only:
            raise TargetBlocked('GitHub 命令未确认完成；请回查后继续') from None
        stderr = getattr(exc, 'stderr', '') or ''
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors='replace')
        tail = SECRET.sub('[redacted]', _redact(stderr, env))[-500:]
        message = f'{_failure_kind(exc)}；argv: {_safe_argv(argv, env)}'
        raise Retryable(message + (f'；stderr: {tail}' if tail else '')) from None
    for field in ('stdout', 'stderr'):
        value = getattr(result, field)
        if isinstance(value, (str, bytes)):
            setattr(result, field, _redact(value, env))
    return result
