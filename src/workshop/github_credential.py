"""Credential helper for git and the executor's gh wrapper.

Every request signs a fresh installation token with the signer named by the
environment; the token lives only in this process (or in the real gh process's
environment) and is never written to disk or logged. A signer failure fails the
git/gh call with the reason on stderr and never falls back to a stored or human
identity.

``environment()`` (direct workshop actions) keeps the older behaviour: it puts a
token in GH_TOKEN and no signer configuration, so that path still answers from
the token it already signed.
"""
import json
import os
import re
import subprocess
import sys

SIGNER = 'WORKSHOP_GITHUB_SIGNER'
REPO = 'WORKSHOP_GITHUB_REPO'
GH = 'WORKSHOP_GITHUB_GH'
TOKEN_KEYS = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')

# 签发器只回一行固定前缀的分类结果；其余 stderr 一律丢弃（可能含密钥）。
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


def failure_reason(exc):
    """异常的一行说明；去掉疑似密钥，不带 traceback 和签发器原样输出。"""
    text = str(exc) or type(exc).__name__
    return SECRET.sub('[已隐藏]', f'{type(exc).__name__}: {text}')[:300]


def signer_argv():
    raw = os.environ.get(SIGNER)
    if not raw:
        return None
    try:
        argv = json.loads(raw)
    except ValueError:
        raise ValueError('签发器配置不是合法 JSON') from None
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
        raise ValueError('签发器配置必须是 argv 列表')
    return argv


def fresh_token(repo):
    """现签一个令牌；有签发器配置时绝不退回环境里可能过期的令牌。"""
    argv = signer_argv()
    if argv is None:
        token = os.environ.get('GH_TOKEN')
        if not token:
            raise RuntimeError('没有签发器配置，环境里也没有 GH_TOKEN')
        return token
    env = {key: value for key, value in os.environ.items() if key not in TOKEN_KEYS}
    env['AGENT_KIND'] = 'workshop'
    try:
        result = subprocess.run([*argv, '--repo', repo], env=env, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('签发器无法执行：' + type(exc).__name__) from None
    token = result.stdout.strip()
    if result.returncode or not token or any(char.isspace() for char in token):
        reason = signing_reason(result.stderr)
        raise RuntimeError(f'App 签发失败（exit {result.returncode} 或空/无效令牌）'
                           + (f'；{reason}' if reason else ''))
    return token


def git_get():
    fields = {}
    for line in sys.stdin:
        if '=' in line:
            key, value = line.rstrip('\n').split('=', 1)
            fields[key] = value
    repo = fields.get('path', '').removesuffix('.git').strip('/')
    if fields.get('protocol') != 'https' or fields.get('host') != 'github.com':
        return 0
    if not repo or repo.casefold() != os.environ.get(REPO, '').casefold():
        return 0
    try:
        token = fresh_token(repo)
    except Exception as exc:
        print('github 凭据签发失败：' + failure_reason(exc), file=sys.stderr)
        return 1
    print('username=x-access-token\npassword=' + token)
    return 0


def gh_call(args):
    gh, repo = os.environ.get(GH), os.environ.get(REPO)
    if not gh or not repo:
        print('github gh 包装缺少真实 gh 路径或目标仓库', file=sys.stderr)
        return 1
    try:
        token = fresh_token(repo)
    except Exception as exc:
        print('github gh 签发失败：' + failure_reason(exc), file=sys.stderr)
        return 1
    env = dict(os.environ)
    env['GH_TOKEN'] = token
    env['GITHUB_TOKEN'] = token
    try:
        os.execve(gh, [gh, *args], env)
    except OSError as exc:
        print('执行真实 gh 失败：' + failure_reason(exc), file=sys.stderr)
        return 1


def main():
    args = sys.argv[1:]
    if args[:1] == ['gh']:
        return gh_call(args[1:])
    if args == ['get']:
        return git_get()
    return 0


if __name__ == '__main__':
    sys.exit(main())
