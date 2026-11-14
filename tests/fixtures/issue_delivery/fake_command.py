#!/usr/bin/env python3
"""外部命令的替身；只回放测试目录 JSON，永不联网。

测试默认在同一进程里调 handle()（见 conftest 的 fake_commands），省掉每次起 Python 的开销；
直接当可执行文件跑时行为相同。
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

GIT = '/usr/bin/git'


class Exit(Exception):
    def __init__(self, code):
        self.code = code


def real_git(args, cwd, env):
    result = subprocess.run([GIT, *args], cwd=cwd, env=env, capture_output=True, text=True, errors='replace')
    return result.returncode, result.stdout, result.stderr


def handle(name, args, *, stdin='', env=None, cwd=None, git=real_git):
    """返回 (退出码, stdout, stderr)；git 是转给真 git 的函数，签名同 real_git。"""
    env = os.environ if env is None else env
    store = Path(env['WORKSHOP_FAKE_STATE'])
    state = json.loads(store.read_text())
    with store.with_suffix('.calls').open('a') as out:
        out.write(json.dumps([name, *args]) + '\n')
    stdout, stderr = [], []

    def save():
        store.write_text(json.dumps(state))

    def emit(value):
        stdout.append(json.dumps(value) + '\n')

    def say(text, to=stdout):
        to.append(text + '\n')

    def passthrough(argv):
        code, out, err = git(argv, cwd, env)
        stdout.append(out)
        stderr.append(err)
        raise Exit(code)

    try:
        if name == 'lark-cli':
            emit(state.get('message', {'ok': False}))
        elif name == 'kubectl':
            if 'deployment' in args:
                # 默认是换版已完成的 Deployment；测试改 rollout 模拟没完成。
                emit(state.get('rollout', {'metadata': {'generation': 2}, 'spec': {'replicas': 1},
                                           'status': {'observedGeneration': 2, 'replicas': 1, 'updatedReplicas': 1, 'availableReplicas': 1}}))
            else:
                emit(state.get('app', {}))
        elif name == 'tag-release.sh':
            state.setdefault('tags', {})['v1.2.3'] = args[-1]
            save()
        elif name == 'git':
            if 'ls-remote' in args:
                if 'refs/heads/production' in args:
                    say(state.get('production_sha', 'e' * 40) + '\trefs/heads/production')
                for tag, sha in state.get('tags', {}).items():
                    say(sha + '\trefs/tags/' + tag + '^{}')
            elif 'push' in args:
                tag = args[-1].removeprefix('refs/tags/')
                state.setdefault('tags', {})[tag] = state['sha']
                save()
            elif 'clone' in args and any(a.startswith('https://github.com/') for a in args):
                # 普通远端 clone 的离线替身；复制已录制的本地提交。
                passthrough([state['checkout'] if a.startswith('https://github.com/') else a for a in args])
            elif 'fetch' in args:
                pass  # 测试提交已在本地仓库里，不能访问网络。
            else:
                passthrough(args)
        elif args[:2] == ['pr', 'merge']:
            if state.get('merge_error'):
                # 合并被拒：stderr 原样回放，PR 状态由测试另行设定。
                say(state['merge_error'], stderr)
                raise Exit(1)
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
                data = json.loads(stdin)
                state['issue'] = {'number': 7, 'state': 'open', **data}
                save()
                emit(state['issue'])
                raise Exit(0)
            if '--include' in args:
                entry = state.get('responses', {}).get(endpoint)
                # 时间窗口的列表替身：记录的全集按真实 since 语义筛选；其他未登记 URL 仍报错。
                parsed = urlsplit(endpoint)
                query = dict(parse_qsl(parsed.query))
                if entry is None and 'since' in query and parsed.path.endswith(('/issues', '/pulls/comments')):
                    since = query.pop('since')
                    full = state.get('responses', {}).get(parsed.path + '?' + urlencode(query))
                    if isinstance(full, list):
                        entry = [r for r in full if r.get('updated_at', '') >= since]
                if entry is None:
                    say('HTTP/2.0 404 Not Found\n\n{}')
                    raise Exit(1)
                value = entry.get('data', entry) if isinstance(entry, dict) and '_response' in entry else entry
                tag = '"AbC' + hashlib.sha256(json.dumps(value).encode()).hexdigest() + '"'
                if 'If-None-Match: ' + tag in args:
                    say('HTTP/2.0 304 Not Modified\n\n')
                else:
                    say('HTTP/2.0 200 OK\netag: ' + tag)
                    if isinstance(entry, dict) and entry.get('next'):
                        say('link: <https://api.github.com/' + entry['next'] + '>; rel="next"')
                    say('\n' + json.dumps(value))
                raise Exit(0)
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
            elif path.endswith('/actions/runs'):
                if state.get('workflow_error'):
                    say(state['workflow_error'], stderr)
                    raise Exit(1)
                # 保留干扰记录，由调用方核对 tag/提交；支持多页回放。
                if '--slurp' in args and 'workflow_pages' in state:
                    emit(state['workflow_pages'])
                    raise Exit(0)
                value = {'workflow_runs': state.get('workflow_runs', [])}
            elif path.endswith('/status'):
                value = {'statuses': state.get('statuses', [])}
            elif '/contents/' in path and not path.endswith('/contents/release.json'):
                name = unquote(path.split('/contents/', 1)[1])
                if state.get('contents_error'):
                    say(state['contents_error'], stderr)
                    raise Exit(1)
                docs = state.get('requirement_contents', {})
                if name in docs:
                    value = {'path': name, 'type': 'file', 'encoding': 'base64',
                             'content': base64.b64encode(docs[name].encode()).decode()}
                elif any(p.startswith(name + '/') for p in docs):
                    entries = {}
                    for p in docs:
                        if p.startswith(name + '/'):
                            rest = p[len(name) + 1:]
                            child = name + '/' + rest.split('/')[0]
                            entries[child] = {'path': child, 'type': 'dir' if '/' in rest else 'file'}
                    value = list(entries.values())
                elif name not in state.get('contents', []):
                    say('gh: Not Found (HTTP 404)', stderr)
                    raise Exit(1)
                else:
                    value = {'path': name, 'content': ''}
            elif path.endswith('/contents/release.json'):
                value = {'content': base64.b64encode(json.dumps({'source_commit': state.get('deployed_sha')}).encode()).decode()}
            else:
                say('unhandled endpoint: ' + endpoint, stderr)
                raise Exit(1)
            emit([value] if '--slurp' in args else value)
        else:
            say('unhandled command: ' + repr(args), stderr)
            raise Exit(1)
    except Exit as done:
        return done.code, ''.join(stdout), ''.join(stderr)
    return 0, ''.join(stdout), ''.join(stderr)


if __name__ == '__main__':
    code, out, err = handle(Path(sys.argv[0]).name, sys.argv[1:], stdin=sys.stdin.read() if '--input' in sys.argv else '')
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)
