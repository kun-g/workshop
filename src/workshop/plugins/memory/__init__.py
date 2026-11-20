"""记忆流程插件：每周做梦、每周备份由 R34 到点开出来的活走这里。

阶段只有接单、执行、收尾，是程序流程，不起模型轮。整件活只做一次（state 里记 done），
结果原样留给 `workshop memory dream/backup` 查看；失败返回 blocked，记成卡住交项目经理，
不发消息（介入点 R13）。
"""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile

from workshop.core.models import After, Evidence
from . import config

STAGES = ("接单", "执行", "收尾")


def read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)


def kind_of(item):
    """从到点事件的开活说明里取：about 写成「记忆：dream」或「记忆：backup」。"""
    for material in item.get("materials", []):
        body = material.get("body") or {}
        if isinstance(body, dict):
            about = body.get("about") or ""
            if about.startswith("记忆："):
                return about.split("：", 1)[1]
            schedule = body.get("schedule") or ""
            if schedule.startswith("memory-"):
                return schedule.split("-", 1)[1]
    return ""


class MemoryFlow:
    def __init__(self, root=None):
        self.root = root
    def stages(self, target):
        return STAGES

    def automatic(self, item, stage, target):
        return True

    def classify(self, event, item, target, find_external=None):
        return "update"

    def describe(self, event, target):
        return f"记忆：{event.body.get('about', '')}"

    def brief(self, item, stage, target):
        return "记忆程序流程"

    def assess(self, item, receipt, target):
        state = read(self.path(item, target))
        if state.get("failure"):
            return Evidence(checks=[["workshop", "memory", "list"]], passed=False, reason=state["failure"])
        return Evidence(checks=[["workshop", "memory", "list"]], passed=True, reason="程序阶段")

    def after(self, item, completed_stage, key, target, services=None):
        path = self.path(item, target)
        state = read(path)
        if state.get("done"):
            return After()
        kind = state.get("kind") or kind_of(item)
        try:
            if not Path(config.resolve(_root(target, item))["path"]).exists():
                # 记忆库还没建：先建一个空库，再把这一件事做在它上面。
                from .store import open_store
                open_store(config.resolve(_root(target, item))["path"]).close()
            result = self.execute(kind, item, target)
        except ValueError as exc:
            state["failure"] = str(exc)
            write(path, state)
            return After(blocked=f"记忆 {kind or '活'} 没做成：{exc}",
                         next="项目经理查明记忆没做成的原因后处理")
        state.update(done=True, kind=kind, last={"at": self.now(), "result": result})
        write(path, state)
        return After()

    def execute(self, kind, item, target):
        """做这一件事，只返回 JSON 能表示的值（路径写成字符串）。"""
        from . import backup, ports, reported
        from . import dream as dreaming
        from .store import open_store
        data = config.resolve(_root(target, item))
        if kind == "dream":
            store = open_store(data["path"])
            try:
                return dreaming.run(store, ports.Facts(data["facts_db"]), self.dreamer(data),
                                    batch=f'dream:{item["id"]}:{self.now()}')
            finally:
                store.close()
        if kind == "backup":
            return {"database": data["path"], "backup": str(backup.run(data["path"], data["backup_dir"])),
                    "backup_dir": data["backup_dir"]}
        if kind == "reported":
            store = open_store(data["path"])
            try:
                written = reported.record(store, ports.Outbox(data["intervention_db"]), scope="*",
                                           batch=f'reported:{item["id"]}:{self.now()}',
                                           watermark=ports.Facts(data["facts_db"]).max_seq())
                return {"written": [row["id"] for row in written]}
            finally:
                store.close()
        raise ValueError(f"不认识的记忆活：{kind or '（到点说明里没有记忆：开头）'}")

    def dreamer(self, settings):
        from .defaults import make_dreamer
        return make_dreamer(None, settings)

    def path(self, item, target):
        root = self.root or (config.state_root(target) / "memory" / "flow")
        return Path(root) / (hashlib.sha256(str(item["id"]).encode()).hexdigest() + ".json")

    @staticmethod
    def now():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


def make_flow():
    return MemoryFlow()


def _root(target, item=None):
    """流程活的状态目录：优先用执行者拿到的 runtime.state_dir（临时状态目录也能对上）。"""
    runtime = (item or {}).get("runtime") if isinstance(item, dict) else None
    if isinstance(runtime, dict) and runtime.get("state_dir"):
        return Path(runtime["state_dir"]).expanduser().resolve()
    return config.state_root(target if isinstance(target, dict) else None)

