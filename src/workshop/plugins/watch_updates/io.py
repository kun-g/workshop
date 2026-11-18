"""文件事务、只读上游与私聊交付；所有路径、身份和命令来自规则。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import hashlib
import os
import re
from pathlib import Path
import subprocess
import tempfile
from time import monotonic
from urllib.parse import quote
from urllib.request import urlopen

import yaml

from workshop.github_identity import run, scoped
from .model import SCHEMA, digest, tag_version, upstream, validate_software, version
from .inventory import filter_scopes, upgrade_records, with_scopes

NOTES_MISSING = '上游仅提供 tag，没有发布说明；不能据此编造变化或升级理由。'
PAGE = 30


def floor(software):
    """在用的最低已知版本；比它旧的发布不影响发布范围，读到就可以停。"""
    known = [version(p['version']) for p in software['instances'] if p['version'] != 'unknown']
    return min(known) if known else None


def path(value):
    return Path(value).expanduser().resolve()


def read(file, default=None):
    # 状态、快照是 JSON，用 json 读：PyYAML 读 11-15 首跑时 98MB 的 state.json 要几分钟，
    # 每个流程作业都要读一遍，整条流程被拖住。清单、台账是 YAML。
    try:
        text = Path(file).read_text()
        return json.loads(text) if Path(file).suffix == '.json' else yaml.safe_load(text)
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

    def save_notes(self, target, key, releases):
        """每个版本一个 Markdown 文件，按发布范围从旧到新编号；返回目录。"""
        directory = self.file(target, 'state_dir') / 'notes' / key
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for index, row in enumerate(releases, 1):
            tag = row.get('tag_name', '')
            name = f"{index:04d}-{re.sub(r'[^A-Za-z0-9_.-]', '_', tag)}.md"
            text = f"# {tag}\n\n原文：{row.get('html_url', '')}\n\n{row.get('body') or NOTES_MISSING}\n"
            (directory / name).write_text(text)
        return str(directory)

    def workdir(self, target, name):
        """执行者的工作目录。初始化成 git 仓库：codex exec 不在 git 仓库里会拒绝启动。"""
        directory = self.file(target, 'state_dir') / name
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (directory / '.git').exists():
            result = self.command(['git', 'init', '-q', str(directory)])
            if result.returncode:
                raise ValueError('工作目录初始化失败：' + result.stderr[-500:])
        return str(directory)

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

    def watchlist(self, target, *, filtered=True):
        value = read(self.file(target, 'watchlist'))
        if not isinstance(value, dict) or value.get('schema') != SCHEMA or not isinstance(value.get('software'), list):
            raise ValueError('盯更新清单格式不匹配；按实施文档转换后再启用')
        for row in value['software']:
            validate_software(row)
        if len({r['id'] for r in value['software']}) != len(value['software']):
            raise ValueError('盯更新清单的软件标识重复')
        value = with_scopes(value, self.config(target).get('excluded_scopes', []))
        return filter_scopes(value) if filtered else value

    def inventory_sources(self, target):
        configured = self.config(target).get('inventory_sources')
        return {'sources': configured} if configured is not None else read(self.file(target, 'sources'))

    def upgrades(self, target):
        """读取固定登记路径；缺失是盲区，不触发登录，也不伪造完成记录。"""
        location = self.config(target).get('upgrade_records')
        if not location:
            return {'records': [], 'error': '未登记升级完成记录路径'}
        try:
            file = path(location)
            if not file.is_file():
                return {'records': [], 'error': '尚无升级完成记录：' + str(file)}
            return {'records': upgrade_records(read(file)['records'])}
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return {'records': [], 'error': '升级完成记录不可用：' + str(exc)}

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
        if source['kind'] == 'gitlab_tags':
            return self.gitlab_tags(source['project'], software)
        repo = source['repo']
        if source['kind'] == 'github_releases':
            return self.github_releases(repo, software)
        result = run(['gh', 'api', '--paginate', '--slurp', f"repos/{repo}/tags?per_page=100"],
                     capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise ValueError('上游查不到：' + result.stderr[-500:])
        rows = [row for page in json.loads(result.stdout) for row in page]
        return [{'tag_name': r['name'], 'html_url': f"https://github.com/{repo}/tree/{quote(r['name'], safe='')}",
                 'body': NOTES_MISSING, 'notes_missing': True} for r in rows]

    def github_releases(self, repo, software):
        """发布按创建时间倒序，逐页读到不新于在用最低版本的发布就停。

        大仓库（openai/codex）发布说明很长，per_page=100 时 GitHub 十秒内答不出（504），
        全量翻页也超出时限，所以每页 30 条、够用即停。实测 openai/codex 从 0.137 读起要 17 页、
        约 70 秒，总预算给 180 秒（读取在作业子进程里，不占主循环）。没读到这一处就报错，不拿部分结果判断。
        """
        low = floor(software)
        rows = []
        deadline = monotonic() + 180
        for page in range(1, 101):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError('GitHub releases 读取超时，不能用部分结果判断')
            result = run(['gh', 'api', f'repos/{repo}/releases?per_page={PAGE}&page={page}'],
                         capture_output=True, text=True, timeout=min(60, remaining))
            if result.returncode:
                raise ValueError('上游查不到：' + result.stderr[-500:])
            batch = json.loads(result.stdout)
            if not isinstance(batch, list) or any(not isinstance(r, dict) for r in batch):
                raise ValueError('GitHub releases 返回格式不正确')
            rows.extend(batch)
            if (len(batch) < PAGE or low is None
                    or any(v is not None and v <= low for v in (tag_version(software, r.get('tag_name', '')) for r in batch))):
                return rows
        raise ValueError('GitHub releases 超出分页上限，未完成读取，不能用部分结果判断')

    def gitlab_pages(self, url, label):
        """按页读 gitlab.com 公开接口，不携带本机 GitLab 或 GitHub 凭据；调用方可提前停。"""
        page = 1
        deadline = monotonic() + 90
        for _ in range(100):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError(f'GitLab {label} 读取超时，不能用部分结果判断')
            with urlopen(f'{url}&page={page}', timeout=min(30, remaining)) as response:
                batch = json.load(response)
                following = response.headers.get('X-Next-Page')
            if not isinstance(batch, list) or any(not isinstance(r, dict) for r in batch):
                raise ValueError(f'GitLab {label} 返回格式不正确')
            yield batch
            if following == '' or not batch or (following is None and len(batch) < 100):
                return
            next_page = int(following) if following is not None else page + 1
            if next_page <= page:
                raise ValueError(f'GitLab {label} 分页没有前进')
            page = next_page
        raise ValueError(f'GitLab {label} 超出分页上限，未完成读取，不能用部分结果判断')

    def gitlab_releases(self, project):
        rows = []
        url = f'https://gitlab.com/api/v4/projects/{quote(project, safe="")}/releases?per_page=100'
        for batch in self.gitlab_pages(url, 'releases'):
            if any(not isinstance(r.get('tag_name'), str) or not isinstance(r.get('_links'), dict)
                   or not isinstance(r['_links'].get('self'), str) for r in batch):
                raise ValueError('GitLab releases 缺少 tag_name 或发布说明链接')
            rows.extend({'tag_name': r['tag_name'], 'html_url': r['_links']['self'],
                         'body': r.get('description') or '', 'draft': r.get('upcoming_release', False)} for r in batch)
        return rows

    def gitlab_tags(self, project, software):
        """有的项目不开放 releases 接口（gitlab-org/gitlab 返回 403），改读 tag。

        按版本倒序读，读到不新于在用最低版本的 tag 就停：更旧的 tag 不影响发布范围，
        这样不用翻完上千个 tag。没读到这一处就报错，不拿部分结果判断。
        """
        low = floor(software)
        link = (f'https://gitlab.com/api/v4/projects/{project}/repository/tags/' if project.isdigit()
                else f'https://gitlab.com/{project}/-/tags/')
        rows = []
        url = (f'https://gitlab.com/api/v4/projects/{quote(project, safe="")}/repository/tags'
               '?order_by=version&sort=desc&per_page=100')
        for batch in self.gitlab_pages(url, 'tags'):
            if any(not isinstance(r.get('name'), str) for r in batch):
                raise ValueError('GitLab tags 缺少 name')
            for r in batch:
                notes = (r.get('release') or {}).get('description')
                rows.append({'tag_name': r['name'], 'html_url': link + quote(r['name'], safe=''),
                             'body': notes or NOTES_MISSING, 'notes_missing': not notes})
            parsed = [tag_version(software, r['name']) for r in batch]
            if low is None or any(v is not None and v <= low for v in parsed):
                return rows
        return rows

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

    def send_report(self, target, report, services=None):
        """原报告幂等键保持不变；许可、发送和不确定结果对账由介入点服务处理。"""
        services = services or getattr(self, 'services', None)
        if services is None:
            raise ValueError('报告送达缺介入点服务端口')
        port, config = services('intervention')
        file = path(report['path'])
        journal_file = self.file(target, 'state_dir') / ('delivery-' + digest(report['id']) + '.json')
        journal = read(journal_file)
        operation = 'update-report:' + target['name'] + ':' + report['id']
        permit = port.invoke('permit', {'operation': operation + ':permit', 'point': 'update_report',
            'owner': target['name'] + ':watch_updates:' + report.get('anchor', 'check'),
            'scope': 'report:' + target['name'] + ':' + report['id'],
            'basis': {'contract': '盯更新 R14：只送 HTML 附件', 'artifact': str(file)}}, config)['ref']
        payload = {'kind': 'update_report', 'artifact': str(file), 'report': report['id'],
                   'sha256': hashlib.sha256(file.read_bytes()).hexdigest(),
                   'idempotency_key': digest([report['id'], 'file'])}
        old = journal.get('file', {})
        if old.get('sent') and old.get('response', {}).get('data', {}).get('message_id'):
            payload.update(existing_external=old['response']['data']['message_id'], existing_started=old['started'])
        elif old.get('started'):
            # 旧路径未确认结果也不能视作未发；只允许回查旧记录。
            payload.update(existing_external='unknown', existing_started=old['started'])
        delivery = port.invoke('enqueue', {'operation': operation + ':enqueue', 'permit': permit, 'payload': payload}, config)['ref']
        journal.update(intervention={'permit': permit, 'delivery': delivery, 'idempotency_key': payload['idempotency_key'],
                      'created_at': journal.get('intervention', {}).get('created_at', self.now())})
        write(journal_file, journal)
        result = port.invoke('dispatch', {'ref': delivery}, config)
        journal['intervention'].update(state=result['state'], external=result.get('external'))
        write(journal_file, journal)
        if result['state'] != 'sent':
            raise ValueError('报告发送未确认；按原送达号 ' + delivery + ' 对账，禁止重发')
