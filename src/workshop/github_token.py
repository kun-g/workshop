"""Uncached installation token signer. Secret material lives only in memory/pipes."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request


class SigningFailure(ValueError):
    """签发失败的可分类原因；detail 里不放令牌、私钥或请求头。

    kind 固定为 config/keychain/openssl/network/github/verify/unknown，供
    github_identity.sign 带进核心事实，事后分得清是钥匙串、网络还是 GitHub。
    """
    def __init__(self, kind, detail):
        self.kind, self.detail = kind, detail
        super().__init__(detail)


SECRET = re.compile(r'(?i)(bearer\s+)\S+'
                    r'|\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b'
                    r'|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b')


def safe(text, limit=240):
    """一行、截断、去掉疑似令牌；只用于给人看的错误说明。"""
    text = ' '.join(str(text).split())
    return SECRET.sub('[已隐藏]', text)[:limit]


def http_message(error):
    """GitHub 错误正文里的 message；读不到或不是 JSON 时留空，不把正文整个带出。"""
    try:
        raw = error.read(4096)
    except (OSError, ValueError):
        return ''
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return ''
    message = value.get('message') if isinstance(value, dict) else None
    return safe(message) if isinstance(message, str) else ''


def api(path, token, data=None):
    request = urllib.request.Request('https://api.github.com' + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = http_message(exc)
        raise SigningFailure('github', f'{path} 返回 HTTP {exc.code}' + (f'：{detail}' if detail else '')) from None
    except urllib.error.URLError as exc:
        raise SigningFailure('network', f'{path} 网络错误：{safe(getattr(exc, "reason", exc))}') from None
    except (TimeoutError, OSError) as exc:
        raise SigningFailure('network', f'{path} 网络错误：{type(exc).__name__}') from None
    except (ValueError, TypeError):
        raise SigningFailure('github', f'{path} 返回的不是 JSON 对象') from None


def mint(args):
    try:
        cfg = json.loads(Path(args.config).expanduser().read_text())
    except (OSError, ValueError) as exc:
        raise SigningFailure('config', f'签发配置读取失败：{type(exc).__name__}') from None
    if not isinstance(cfg, dict):
        raise SigningFailure('config', '签发配置必须是对象')
    try:
        allowed = {r.casefold(): r for r in cfg['repositories']}
    except (KeyError, TypeError):
        raise SigningFailure('config', '签发配置缺 repositories') from None
    if not isinstance(cfg.get('client_id'), str) or not cfg['client_id'].strip():
        raise SigningFailure('config', '签发配置缺 client_id')
    if not isinstance(cfg.get('repo_permissions', {}), dict):
        raise SigningFailure('config', '签发配置的 repo_permissions 必须是对象')
    repo = allowed.get(args.repo.casefold())
    if not repo or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise SigningFailure('config', 'repository not allowed')
    relations_read = getattr(args, 'relations_read', False)
    permissions = {k.casefold(): v for k, v in cfg.get('repo_permissions', {}).items()}
    repositories = [repo]
    if relations_read:
        # 一份安装令牌不能跨 owner；同一 owner 的全部允许仓库必须可读，不能静默缩小集合。
        owner = repo.split('/')[0].casefold()
        repositories = sorted(r for r in allowed.values() if r.split('/')[0].casefold() == owner)
        if any(not re.fullmatch(r'[\w.-]+/[\w.-]+', r)
               or permissions.get(r.casefold(), {}).get('issues') not in ('read', 'write') for r in repositories):
            raise SigningFailure('config', 'relationship repositories must explicitly allow issues read')
        requested = {'issues': 'read', 'metadata': 'read'}
    else:
        requested = permissions.get(repo.casefold())
        if not requested:
            raise SigningFailure('config', 'repository permissions must be explicit')
    try:
        key = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', args.key_service,
                              '-a', args.key_account, '-w'], capture_output=True, check=True, timeout=10).stdout
    except subprocess.CalledProcessError as exc:
        raise SigningFailure('keychain', f'读不到私钥（security exit {exc.returncode}）') from None
    except subprocess.TimeoutExpired:
        raise SigningFailure('keychain', '读私钥超时') from None
    except OSError as exc:
        raise SigningFailure('keychain', f'钥匙串工具不可用：{type(exc).__name__}') from None
    def b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b'=')
    now = int(time.time())
    content = b'.'.join(b64(json.dumps(v).encode()) for v in
        ({'alg': 'RS256', 'typ': 'JWT'}, {'iat': now - 60, 'exp': now + 540, 'iss': cfg['client_id']}))
    reader, writer = os.pipe()
    try:
        try:
            os.write(writer, base64.b64decode(key.strip(), validate=True))
        except (ValueError, TypeError):
            raise SigningFailure('keychain', '私钥不是有效的 base64') from None
        os.close(writer)
        writer = None
        signature = subprocess.run(['/usr/bin/openssl', 'dgst', '-sha256', '-sign', f'/dev/fd/{reader}'],
            pass_fds=(reader,), input=content, capture_output=True, check=True, timeout=10).stdout
    except subprocess.CalledProcessError as exc:
        raise SigningFailure('openssl', f'签名失败（openssl exit {exc.returncode}）') from None
    except subprocess.TimeoutExpired:
        raise SigningFailure('openssl', '签名超时') from None
    except OSError as exc:
        raise SigningFailure('openssl', f'签名工具不可用：{type(exc).__name__}') from None
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)
    jwt = (content + b'.' + b64(signature)).decode()
    try:
        if api('/app', jwt)['slug'] != args.app:
            raise SigningFailure('verify', 'wrong app')
        installation = api(f'/repos/{repo}/installation', jwt)['id']
        for other in repositories:
            if other != repo and api(f'/repos/{other}/installation', jwt)['id'] != installation:
                raise SigningFailure('verify', 'relationship repositories span installations')
        result = api(f'/app/installations/{installation}/access_tokens', jwt,
                     {'repositories': [r.split('/')[1] for r in repositories], 'permissions': requested})
        if relations_read and result.get('permissions') != requested:
            raise SigningFailure('verify', 'relationship token did not confirm read-only permissions')
        if relations_read:
            visible = set()
            for page in range(1, (len(repositories) + 99) // 100 + 1):
                listing = api(f'/installation/repositories?per_page=100&page={page}', result['token'])
                if listing['total_count'] != len(repositories):
                    raise SigningFailure('verify', 'relationship token repository coverage is incomplete')
                visible.update(r['full_name'].casefold() for r in listing['repositories'])
            if visible != {r.casefold() for r in repositories}:
                raise SigningFailure('verify', 'relationship token repository coverage is incomplete')
    except (KeyError, TypeError):
        raise SigningFailure('github', 'GitHub 返回值缺字段') from None
    return result['token']


def main():
    parser = argparse.ArgumentParser()
    for name in ('repo', 'config', 'app', 'key-service', 'key-account'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--relations-read', action='store_true')
    args = parser.parse_args()
    try:
        token = mint(args)
    except SigningFailure as exc:
        reason = {'kind': exc.kind, 'detail': safe(exc.detail)}
    except Exception as exc:
        # 未预料的异常只留类型，不把 traceback、局部变量或私钥带出去。
        reason = {'kind': 'unknown', 'detail': type(exc).__name__}
    else:
        print(token)
        return 0
    # 固定前缀的一行分类结果供 github_identity.sign 带进核心事实；其余 stderr 不带。
    print('SIGNING_ERROR ' + json.dumps(reason, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
