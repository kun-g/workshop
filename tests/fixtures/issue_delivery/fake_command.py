#!/usr/bin/env python3
"""外部命令的可执行替身；只回放测试目录 JSON，永不联网。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

store = Path(os.environ['WORKSHOP_FAKE_STATE'])
state = json.loads(store.read_text())
args = sys.argv[1:]
name = Path(sys.argv[0]).name
with store.with_suffix('.calls').open('a') as out:
    out.write(json.dumps([name, *args]) + '\n')


def save():
    store.write_text(json.dumps(state))


def emit(value):
    print(json.dumps(value))


if name == 'lark-cli':
    emit(state.get('message', {'ok': False}))
elif name == 'kubectl':
    emit(state.get('app', {}))
elif name == 'tag-release.sh':
    state.setdefault('tags', {})['v1.2.3'] = args[-1]
    save()
elif name == 'git':
    if 'ls-remote' in args:
        if 'refs/heads/production' in args:
            print(state.get('production_sha', 'e' * 40) + '\trefs/heads/production')
        for tag, sha in state.get('tags', {}).items():
            print(sha + '\trefs/tags/' + tag + '^{}')
    elif 'push' in args:
        tag = args[-1].removeprefix('refs/tags/')
        state.setdefault('tags', {})[tag] = state['sha']
        save()
    elif 'clone' in args and any(a.startswith('https://github.com/') for a in args):
        # 普通远端 clone 的离线替身；复制已录制的本地提交。
        local = [state['checkout'] if a.startswith('https://github.com/') else a for a in args]
        sys.exit(subprocess.run(['/usr/bin/git', *local]).returncode)
    elif 'fetch' in args:
        pass  # clone 内已有真实测试提交，不能访问网络。
    else:
        completed = subprocess.run(['/usr/bin/git', *args])
        sys.exit(completed.returncode)
elif args[:2] == ['pr', 'merge']:
    if state.get('merge_error'):
        # 合并被拒：stderr 原样回放，PR 状态由测试另行设定。
        print(state['merge_error'], file=sys.stderr)
        sys.exit(1)
    state['pr']['merged'] = True
    state['pr']['state'] = 'closed'
    state['pr']['merge_commit_sha'] = state['sha']
    state['pr']['merged_at'] = state.get('merged_at', '2026-11-13T08:00:00Z')
    save()
elif args[:2] == ['issue', 'close']:
    state['issue']['state'] = 'closed'
    save()
elif args and args[0] == 'api':
    endpoints = [a for a in args if a.startswith('repos/')]
    endpoint = endpoints[0]
    if '--method' in args and args[args.index('--method') + 1] == 'POST':
        data = json.load(sys.stdin)
        state['issue'] = {'number': 7, 'state': 'open', **data}
        save()
        emit(state['issue'])
        sys.exit(0)
    if '--include' in args:
        entry = state.get('responses', {}).get(endpoint)
        if entry is None:
            print('HTTP/2.0 404 Not Found\n\n{}')
            sys.exit(1)
        value = entry.get('data', entry) if isinstance(entry, dict) and '_response' in entry else entry
        tag = '"AbC' + hashlib.sha256(json.dumps(value).encode()).hexdigest() + '"'
        if 'If-None-Match: ' + tag in args:
            print('HTTP/2.0 304 Not Modified\n\n')
        else:
            print('HTTP/2.0 200 OK\netag: ' + tag)
            if isinstance(entry, dict) and entry.get('next'):
                print('link: <https://api.github.com/' + entry['next'] + '>; rel="next"')
            print('\n' + json.dumps(value))
        sys.exit(0)
    path = endpoint.split('?')[0]
    # prs 是本活更早的 PR（已合并或已关），pr 是当前这个。
    pulls = state.get('prs', []) + ([state['pr']] if state.get('pr') else [])
    if path.endswith('/pulls'):
        value = pulls
    elif '/pulls/' in path and path.endswith('/files'):
        value = state.get('files', [{'filename': 'src/example.py'}])
    elif '/pulls/' in path and path.endswith('/reviews'):
        value = state.get('reviews', [])
    elif '/pulls/' in path:
        number = int(path.rsplit('/', 1)[1])
        value = next((p for p in pulls if p['number'] == number), state['pr'])
    elif path.endswith('/issues'):
        value = [state['issue']] if state.get('issue') else []
    elif '/issues/' in path:
        value = state['issue']
    elif path.endswith('/check-runs'):
        value = {'check_runs': state.get('checks', [{'name': 'check', 'status': 'completed', 'conclusion': 'success'}])}
    elif path.endswith('/status'):
        value = {'statuses': state.get('statuses', [])}
    elif path.endswith('/contents/release.json'):
        value = {'content': base64.b64encode(json.dumps({'source_commit': state.get('deployed_sha')}).encode()).decode()}
    else:
        raise SystemExit('unhandled endpoint: ' + endpoint)
    emit([value] if '--slurp' in args else value)
else:
    raise SystemExit('unhandled command: ' + repr(args))
