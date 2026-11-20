"""推送式来源（核心 R37）：入口只写 spool，守护进程下一拍把 spool 读成事件，和轮询共用归一键。

默认关闭：交付对象的 sources 里不列 webhook、不写 webhook 配置，就不起入口、不开端口。
推送只是加速层；入口停了，轮询照常取到同样的变化（同键去重，只落一行）。
游标记上一次读走的文件名：事件和游标同事务确认后，下一次读取才删这些文件；
确认前崩了文件还在，重读时按 (source, ext_id) 去重。读不懂的文件挪进 quarantine/，不挡后面的。
"""
import json
from pathlib import Path

from workshop import github_events
from workshop.core.models import Poll

from . import spool
from .config import settings


class WebhookSource:
    def validate(self, target):
        settings(target)

    def wake_paths(self, target):
        # 守护进程盯着 spool 目录：入口写进一份，主循环立即醒来重读本来源。
        return [settings(target)["spool"]]

    def forget(self, root, names):
        for name in names:
            (Path(root) / name).unlink(missing_ok=True)

    def poll(self, target, cursor):
        root = Path(settings(target)["spool"])
        root.mkdir(parents=True, exist_ok=True)
        consumed = json.loads(cursor).get("consumed", []) if cursor else []
        self.forget(root, consumed)
        events, names = [], []
        for path in spool.pending(root):
            try:
                envelope = spool.read(path)
            except (ValueError, UnicodeDecodeError):
                spool.quarantine(path)
                continue
            events.extend(github_events.from_webhook(envelope["event"], envelope["payload"], target))
            names.append(path.name)
        return Poll(events, json.dumps({"consumed": names}), normalized=True)


def make_source():
    return WebhookSource()
