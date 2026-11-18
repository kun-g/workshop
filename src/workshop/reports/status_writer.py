"""装配层启动的状态网页线程；不持有核心的连接或拍锁。"""
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import threading

from workshop.core.log import Log
from workshop.core.models import utcnow
from .report import overview
from .status_page import page

PAGE_KINDS = ('item_changed', 'card_changed', 'round_changed', 'control', 'answer',
              'work_blocked', 'round_finished', 'gate_passed', 'gate_blocked',
              'round_reserved', 'wait_resumed', 'event', 'event_classified',
              'item_dropped', 'card_created', 'reset')
PAGE_MIN_GAP = timedelta(seconds=5)
PAGE_MAX_AGE = timedelta(minutes=5)


def write_page(path, content):
    """同目录临时文件原子替换，多个调用各用自己的临时文件。"""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class StatusWriter:
    def __init__(self, database, clock=utcnow, *, plugins=None):
        self.database = Path(database)
        self.clock = clock
        self.plugins = plugins
        self.last = None
        self.seq = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self.run, name='workshop-status-page', daemon=True)

    def refresh(self):
        now = self.clock()
        if self.last is not None and now - self.last < PAGE_MIN_GAP:
            return
        log = Log.readonly(self.database)
        try:
            with log.read_transaction():
                changes = log.selected_facts(PAGE_KINDS, ('state',), after=self.seq)
                if self.last is not None and not changes and now - self.last < PAGE_MAX_AGE:
                    return
                content = page(overview(log, plugins=self.plugins), now.astimezone())
                seq = changes[-1]['seq'] if changes else self.seq
            # 排版和写文件不占核心连接；失败不消费游标，下次仍重试。
            write_page(self.database.parent / 'status.html', content)
            self.seq, self.last = seq, now
        finally:
            log.close()

    def wake(self):
        self._wake.set()

    def run(self):
        while not self._stop.is_set():
            self._wake.wait(PAGE_MIN_GAP.total_seconds())
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.refresh()
            except Exception as exc:
                print(f'车间 status.html 没写成：{exc!r}', file=sys.stderr)

    def __enter__(self):
        self._thread.start()
        self.wake()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self.wake()
        # 退出不能被查看工具拖住；线程使用独立连接并随进程结束。
        self._thread.join(timeout=1)
