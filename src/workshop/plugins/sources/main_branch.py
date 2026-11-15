"""每小时比较一次主检出与远端；持久游标由核心随事件一起提交。

`workshop release-now` 在发版状态目录写 release-now，来源立刻多比较一次，之后的发版流程与整点相同。
"""
from datetime import datetime, timezone
import base64
import json
from pathlib import Path
import re
import subprocess

from workshop.core.models import Event, Poll
from workshop.core.ports import Retryable
from workshop.github_identity import scoped, run


REQUEST = 'release-now'


class MainBranchSource:
    def __init__(self, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def release_config(target):
        # 来源拿到的是对象默认流程配置；批次目录由对应流程配置。
        configs = [{**target, **local} for flow, local in target.get('flows', {}).items()
                   if local.get('plugin', flow) == 'scheduled_release']
        config = configs[0] if len(configs) == 1 else target
        return config, Path(config.get('scheduled_release_state_dir', '~/.local/state/workshop')).expanduser().resolve()

    def wake_paths(self, target):
        return [str(self.release_config(target)[1] / REQUEST)]

    @scoped
    def poll(self, target, cursor):
        config = target.get('sources', {}).get('main_branch', {}) if isinstance(target.get('sources'), dict) else {}
        if config.get('every', '1h') != '1h':
            raise ValueError('main_branch.every 目前只支持 1h')
        slot = self.clock().astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        config, root = self.release_config(target)
        request = root / REQUEST
        asked = request.read_text().strip() if request.exists() else ''
        if cursor == slot and not asked:
            return Poll([], cursor)
        cluster = config.get('scheduled_release_mode', 'self') == 'cluster'
        try:
            release = json.loads((root / ('cluster-release.json' if cluster else 'release.json')).read_text())
        except FileNotFoundError:
            release = {}
        except (OSError, ValueError) as exc:
            raise Retryable('无法读取发版记录，未确认是否应跳过失败提交') from exc
        if not isinstance(release, dict):
            raise Retryable('发版记录须为对象')
        if cluster and release and release.get('status') not in ('ok', 'rejected', 'failed'):
            # pending、结果不明或上线失败都保留占位；不能整点再发一张卡。
            return Poll([], slot)
        checkout = str(Path(target['checkout']).expanduser().resolve())
        remote = f"https://github.com/{target['repo']}.git"
        def read(argv):
            try:
                result = run(argv, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise Retryable('整点比较未完成') from exc
            if result.returncode:
                raise Retryable('整点比较失败：' + result.stderr[-500:])
            return result.stdout.strip()
        rows = read(['git', 'ls-remote', remote, 'refs/heads/main']).split()
        if len(rows) != 2 or rows[1] != 'refs/heads/main' or not re.fullmatch('[0-9a-f]{40}', rows[0]):
            raise Retryable('远端 main 没有返回完整提交号')
        to = rows[0]
        if cluster:
            if release.get('status') in ('rejected', 'failed') and release.get('to') == to:
                return Poll([], slot)
            try:
                record = json.loads(read(['gh', 'api', f"repos/{target['repo']}/contents/release.json?ref=production"]))
                head = json.loads(base64.b64decode(record['content']))['source_commit']
                if not isinstance(head, str) or not re.fullmatch('[0-9a-f]{40}', head):
                    raise ValueError('发布记录没有完整源提交')
            except (KeyError, ValueError, TypeError) as exc:
                raise Retryable('无法读取 production 发布记录，未确认是否有新合并') from exc
        else:
            head = read(['git', '-C', checkout, 'rev-parse', 'HEAD'])
            if release.get('status') == 'rolled_back' and release.get('to') == to:
                return Poll([], slot)
        ext = 'now:' + asked if asked else slot
        events = [] if head == to else [Event('main_branch', ext, target['name'], to,
            ('立即发版 ' if asked else '定时发版 ') + to[:12], {'kind': 'ahead', 'from': head, 'to': to})]
        if asked:
            # 请求只用一次；本小时不再另比一次。
            request.unlink(missing_ok=True)
        return Poll(events, slot)


def make_source():
    return MainBranchSource()
