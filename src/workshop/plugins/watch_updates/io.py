"""文件事务、只读上游与私聊交付；所有路径、身份和命令来自规则。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
from time import monotonic
from urllib.parse import quote
from urllib.request import urlopen

import yaml

from workshop.github_identity import run, scoped
from .model import SCHEMA, digest, upstream, validate_software


def path(value):
    return Path(value).expanduser().resolve()


def read(file, default=None):
    try:
        return yaml.safe_load(Path(file).read_text())
    except FileNotFoundError:
        return {} if default is None else default


def write(file, value):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=file.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, file)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class IO:
    def __init__(self, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def config(self, target):
        return target['watch_updates']

    def now(self):
        return self.clock().isoformat()

    def file(self, target, name):
        return path(self.config(target)[name])

    def state_file(self, target):
        return self.file(target, 'state_dir') / 'state.json'

    def state(self, target):
        return read(self.state_file(target), {'versions': {}, 'reports': {}, 'weeks': {}, 'outbox': {}, 'consumed': []})

    @contextmanager
    def locked(self, target):
        root = self.file(target, 'state_dir')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (root / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def save(self, target, state):
        write(self.state_file(target), state)

    def watchlist(self, target):
        value = read(self.file(target, 'watchlist'))
        if not isinstance(value, dict) or value.get('schema') != SCHEMA or not isinstance(value.get('software'), list):
            raise ValueError('盯更新清单格式不匹配；按实施文档转换后再启用')
        for row in value['software']:
            validate_software(row)
        if len({r['id'] for r in value['software']}) != len(value['software']):
            raise ValueError('盯更新清单的软件标识重复')
        return value

    def needs(self, target):
        return read(self.file(target, 'needs'), {'needs': []})['needs']

    def directions(self, target):
        result = []
        for row in self.config(target).get('directions', []):
            file = path(row['path'])
            result.append({'id': row['id'], 'path': str(file), 'text': file.read_text()})
        return result

    @scoped
    def releases(self, target, software):
        source = upstream(software)
        if source['kind'] == 'manual':
            return []
        if source['kind'] == 'gitlab_releases':
            return self.gitlab_releases(source['project'])
        repo = source['repo']
        endpoint = 'tags' if source['kind'] == 'github_tags' else 'releases'
        result = run(['gh', 'api', '--paginate', '--slurp', f"repos/{repo}/{endpoint}?per_page=100"],
                     capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise ValueError('上游查不到：' + result.stderr[-500:])
        rows = [row for page in json.loads(result.stdout) for row in page]
        if endpoint == 'tags':
            return [{'tag_name': r['name'], 'html_url': f"https://github.com/{repo}/tree/{quote(r['name'], safe='')}",
                     'body': '上游仅提供 tag，没有发布说明；不能据此编造变化或升级理由。',
                     'notes_missing': True} for r in rows]
        return rows

    def gitlab_releases(self, project):
        """只读 gitlab.com 公开接口，不携带本机 GitLab 或 GitHub 凭据。"""
        rows = []
        page = 1
        deadline = monotonic() + 90
        for _ in range(100):
            url = f'https://gitlab.com/api/v4/projects/{quote(project, safe="")}/releases?per_page=100&page={page}'
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError('GitLab releases 读取超时，不能用部分结果判断')
            with urlopen(url, timeout=min(30, remaining)) as response:
                batch = json.load(response)
                following = response.headers.get('X-Next-Page')
            if not isinstance(batch, list):
                raise ValueError('GitLab releases 返回格式不正确')
            if any(not isinstance(r, dict) or not isinstance(r.get('tag_name'), str)
                   or not isinstance(r.get('_links'), dict) or not isinstance(r['_links'].get('self'), str)
                   for r in batch):
                raise ValueError('GitLab releases 缺少 tag_name 或发布说明链接')
            rows.extend({'tag_name': r['tag_name'], 'html_url': r['_links']['self'],
                         'body': r.get('description') or '', 'draft': r.get('upcoming_release', False)} for r in batch)
            if following == '' or not batch or (following is None and len(batch) < 100):
                return rows
            next_page = int(following) if following is not None else page + 1
            if next_page <= page:
                raise ValueError('GitLab releases 分页没有前进')
            page = next_page
        raise ValueError('GitLab releases 超出分页上限，未完成读取，不能用部分结果判断')

    def command(self, argv, **kwargs):
        return subprocess.run(argv, capture_output=True, text=True, timeout=90, **kwargs)

    def commit(self, target, files, message):
        """只提交列明文件；不会带入 workspace 中其他人的暂存改动。"""
        root = self.file(target, 'data_repo')
        names = [str(path(file).relative_to(root)) for file in files]
        def git(*args):
            result = self.command(['git', '-C', str(root), *args])
            if result.returncode:
                raise ValueError('盯更新文件提交失败：' + result.stderr[-500:])
            return result.stdout.strip()
        git('add', '--', *names)
        changed = git('diff', '--cached', '--name-only', '--', *names)
        if changed:
            git('commit', '--only', '-m', message, '--', *names)
        return git('rev-parse', 'HEAD')

    def message(self, target, message_id):
        import re
        if not re.fullmatch(r'om_[A-Za-z0-9_]+', message_id):
            return None
        c = self.config(target)
        result = self.command([c['lark'], '--profile', c['profile'], 'api', 'GET',
                               f'/open-apis/im/v1/messages/{message_id}', '--as', 'bot'])
        if result.returncode:
            return None
        value = json.loads(result.stdout)
        if value.get('ok') is not True:
            return None
        matches = [m for m in value.get('data', {}).get('items', []) if m.get('message_id') == message_id]
        for msg in matches if len(matches) == 1 else []:
            sender = msg.get('sender', {})
            if (msg.get('message_id') == message_id and msg.get('msg_type') == 'text'
                    and sender.get('sender_type') == 'user' and sender.get('id_type') == 'open_id'
                    and sender.get('id') == c['kun_open_id']):
                return json.loads(msg['body']['content']).get('text')
        return None

    def send_report(self, target, report):
        """文本与附件各有固定幂等键；未确认发送成功时保留原报告重试。"""
        c = self.config(target)
        file = path(report['path'])
        journal_file = self.file(target, 'state_dir') / ('delivery-' + digest(report['id']) + '.json')
        journal = read(journal_file)
        for kind, argument in [('summary', ['--text', report['summary']]), ('file', ['--file', file.name])]:
            old = journal.get(kind)
            if old and old.get('sent'):
                continue
            if old and (self.clock() - datetime.fromisoformat(old['started'])).total_seconds() >= 3500:
                raise ValueError('报告发送结果不明，已超过服务端幂等窗口；须按发送记录核对，禁止重发')
            journal[kind] = old or {'started': self.now()}
            write(journal_file, journal)
            argv = [c['lark'], '--profile', c['profile'], 'im', '+messages-send', '--as', 'bot',
                    '--user-id', c['kun_open_id'], '--idempotency-key', digest([report['id'], kind]),
                    '--format', 'json', *argument]
            result = self.command(argv, cwd=file.parent)
            value = json.loads(result.stdout) if result.stdout else {}
            if result.returncode or value.get('ok') is not True:
                raise ValueError('报告私聊发送未确认：' + result.stderr[-500:])
            journal[kind].update(sent=True, response=value)
            write(journal_file, journal)

