"""作业测试用的插件替身：状态和开关都在文件里，守护进程和作业进程看到同一份。

JOBTEST_DIR 指向测试目录，config.json 控制各个替身的放行条件和结果；
调用记录逐行追加到 calls.log。JOBTEST_REAL_FLOW=1 时 flow 用真的 issue 交付流程，
只把 GitHub 和克隆换成本地替身，检查照真的 verify 跑（含超时）。
"""
import json
import os
from pathlib import Path
import shutil
import time

from workshop.core.models import Evidence, Poll, Release, STAGES, Started
from workshop.plugins.issue_delivery import IssueDelivery
from workshop.plugins.issue_delivery.io import IO
from workshop.plugins.selectors.ordered import OrderedSelector
from workshop.registry import Registry

SHA = "a" * 40


def home():
    return Path(os.environ["JOBTEST_DIR"])


def config():
    return json.loads((home() / "config.json").read_text())


def note(*parts):
    with (home() / "calls.log").open("a") as file:
        file.write("\t".join(str(p) for p in parts) + "\n")


def calls(kind=None):
    path = home() / "calls.log"
    rows = [line.split("\t") for line in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if kind is None or r[0] == kind]


def delay(kind):
    settings = config()
    if kind in settings.get("blocked", []):
        # 真实作业一直阻塞到测试已观察完目标状态；不靠固定 sleep 猜时序。
        deadline = time.monotonic() + 40
        while not (home() / f"allow-{kind}").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"测试没有释放 {kind} 作业")
            pass

class SlowFlow:
    def hold_workdir(self, item, target):
        note("hold-start", item["id"])
        delay("hold")
        note("hold-end", item["id"])
        return config().get("hold_reason", "")

    def stages(self, target):
        return STAGES[:-1]

    def classify(self, event, item, target, find_external=None):
        return "update"

    def brief(self, item, stage, target):
        return f"完成{stage}并交回执"

    def assess(self, item, receipt, target):
        if item["stage"] == "证据":
            note("assess-start", item["id"])
            delay("check")
            note("assess-end", item["id"])
        return Evidence(config().get("checks", [["true"]]), config().get("passed", True))

    def after(self, item, stage, key, target):
        note("after", item["id"], stage)


class SlowSource:
    def poll(self, target, cursor):
        note("source-start")
        delay("source")
        return Poll([], "after-batch")


class SlowChannel:
    def ask(self, key, card):
        return self.notify(key, card)

    def notify(self, key, card):
        note("send-start", key)
        delay("channel")
        note("sent", key)
        return f"external:{key}"

    def reconcile(self, key, card):
        note("reconcile", key)
        if "reconcile_result" in config():
            return config()["reconcile_result"]
        return f"external:{key}" if any(r[1] == key for r in calls("sent")) else "absent"

    def cancel(self, key, external):
        pass

    def poll(self, cards):
        return []


class FakeExecutor:
    def __init__(self, name):
        self.name = name

    def available(self, config):
        return True

    def release(self, item, target):
        note("release-start", item["id"])
        delay("release")
        note("release-end", item["id"])
        reason = config().get("release_reason", "")
        return Release(not reason, reason)

    def launch(self, token, brief, item, config, target):
        delay("executor_launch")
        note("launch", self.name, item["id"], token)
        return Started(f"session:{self.name}:{item['id']}", f"handle:{token}", "/fake/workdir")

    def resume(self, token, session, brief, item, config, target):
        delay("executor_launch")
        note("launch", self.name, item["id"], token)
        return Started(session, f"handle:{token}", "/fake/workdir")

    def poll(self, token, started):
        delay("executor_poll")
        return "running"

    def stop(self, token, started):
        delay("executor_stop")
        note("stop", token)

    def usage(self, token, started, since):
        return None


class LocalIO(IO):
    """GitHub 和克隆换成本地替身；verify 用真的。"""

    def __init__(self):
        super().__init__(gh="/usr/bin/true", git="/usr/bin/true", state_dir=home() / "flow-state", lark="/usr/bin/true")

    def api(self, endpoint, *, data=None):
        return {"number": 7, "state": "open", "body": ""}

    def pr(self, item, target):
        return {"number": 1, "state": "open", "title": "测试", "body": f'<!-- workshop-item: {item["id"]} -->',
                "head": {"sha": SHA}}

    def pages(self, endpoint):
        return []

    def copy(self, target, sha, destination):
        Path(destination).mkdir(parents=True)
        return Path(destination)

    def drop(self, target, path):
        # 和 copy 配套：替身副本只是个空目录，直接删。
        shutil.rmtree(path, ignore_errors=True)


def registry():
    plugins = Registry()
    plugins.register("selector", "ordered", OrderedSelector())
    plugins.register("flow", "flow", IssueDelivery(LocalIO()) if os.environ.get("JOBTEST_REAL_FLOW") else SlowFlow())
    for name in ("writer", "reviewer", "acceptor"):
        plugins.register("executor", name, FakeExecutor(name))
    plugins.register("channel", "channel", SlowChannel())
    plugins.register("source", "source", SlowSource())
    return plugins
