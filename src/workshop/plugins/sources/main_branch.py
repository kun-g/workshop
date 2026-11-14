"""每小时比较一次主检出与远端；持久游标由核心随事件一起提交。"""
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess

from workshop.core.models import Event, Poll
from workshop.core.ports import Retryable
from workshop.github_identity import scoped, run


class MainBranchSource:
    def __init__(self, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @scoped
    def poll(self, target, cursor):
        config = target.get('sources', {}).get('main_branch', {}) if isinstance(target.get('sources'), dict) else {}
        if config.get('every', '1h') != '1h':
            raise ValueError('main_branch.every 目前只支持 1h')
        slot = self.clock().astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        if cursor == slot:
            return Poll([], cursor)
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
        head = read(['git', '-C', checkout, 'rev-parse', 'HEAD'])
        rows = read(['git', 'ls-remote', remote, 'refs/heads/main']).split()
        if len(rows) != 2 or rows[1] != 'refs/heads/main' or not re.fullmatch('[0-9a-f]{40}', rows[0]):
            raise Retryable('远端 main 没有返回完整提交号')
        to = rows[0]
        events = [] if head == to else [Event('main_branch', slot, target['name'], to,
            f'定时发版 {to[:12]}', {'kind': 'ahead', 'from': head, 'to': to})]
        return Poll(events, slot)


def make_source():
    return MainBranchSource()
