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
import urllib.request


def api(path, token, data=None):
    request = urllib.request.Request('https://api.github.com' + path,
        data=None if data is None else json.dumps(data).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def mint(args):
    cfg = json.loads(Path(args.config).expanduser().read_text())
    allowed = {r.casefold(): r for r in cfg['repositories']}
    repo = allowed.get(args.repo.casefold())
    if not repo or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise ValueError('repository not allowed')
    relations_read = getattr(args, 'relations_read', False)
    permissions = {k.casefold(): v for k, v in cfg.get('repo_permissions', {}).items()}
    repositories = [repo]
    if relations_read:
        # 一份安装令牌不能跨 owner；同一 owner 的全部允许仓库必须可读，不能静默缩小集合。
        owner = repo.split('/')[0].casefold()
        repositories = sorted(r for r in allowed.values() if r.split('/')[0].casefold() == owner)
        if any(not re.fullmatch(r'[\w.-]+/[\w.-]+', r)
               or permissions.get(r.casefold(), {}).get('issues') not in ('read', 'write') for r in repositories):
            raise ValueError('relationship repositories must explicitly allow issues read')
        requested = {'issues': 'read', 'metadata': 'read'}
    else:
        requested = permissions.get(repo.casefold())
        if not requested:
            raise ValueError('repository permissions must be explicit')
    key = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', args.key_service,
                          '-a', args.key_account, '-w'], capture_output=True, check=True, timeout=10).stdout
    def b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b'=')
    now = int(time.time())
    content = b'.'.join(b64(json.dumps(v).encode()) for v in
        ({'alg': 'RS256', 'typ': 'JWT'}, {'iat': now - 60, 'exp': now + 540, 'iss': cfg['client_id']}))
    reader, writer = os.pipe()
    try:
        os.write(writer, base64.b64decode(key.strip(), validate=True))
        os.close(writer)
        writer = None
        signature = subprocess.run(['/usr/bin/openssl', 'dgst', '-sha256', '-sign', f'/dev/fd/{reader}'],
            pass_fds=(reader,), input=content, capture_output=True, check=True, timeout=10).stdout
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)
    jwt = (content + b'.' + b64(signature)).decode()
    if api('/app', jwt)['slug'] != args.app:
        raise ValueError('wrong app')
    installation = api(f'/repos/{repo}/installation', jwt)['id']
    for other in repositories:
        if other != repo and api(f'/repos/{other}/installation', jwt)['id'] != installation:
            raise ValueError('relationship repositories span installations')
    result = api(f'/app/installations/{installation}/access_tokens', jwt,
                 {'repositories': [r.split('/')[1] for r in repositories], 'permissions': requested})
    if relations_read and result.get('permissions') != requested:
        raise ValueError('relationship token did not confirm read-only permissions')
    if relations_read:
        visible = set()
        for page in range(1, (len(repositories) + 99) // 100 + 1):
            listing = api(f'/installation/repositories?per_page=100&page={page}', result['token'])
            if listing['total_count'] != len(repositories):
                raise ValueError('relationship token repository coverage is incomplete')
            visible.update(r['full_name'].casefold() for r in listing['repositories'])
        if visible != {r.casefold() for r in repositories}:
            raise ValueError('relationship token repository coverage is incomplete')
    return result['token']


def main():
    parser = argparse.ArgumentParser()
    for name in ('repo', 'config', 'app', 'key-service', 'key-account'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--relations-read', action='store_true')
    args = parser.parse_args()
    try:
        token = mint(args)
    except Exception:
        # No subprocess output, keychain text, headers or response bodies in diagnostics.
        print('App token signing failed; no human fallback', file=sys.stderr)
        return 1
    print(token)
    return 0


if __name__ == '__main__':
    sys.exit(main())
