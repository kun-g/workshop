"""作业测试用的插件替身：状态和开关都在文件里，守护进程和作业进程看到同一份。

JOBTEST_DIR 指向测试目录，config.json 控制各个替身睡多久、交什么结果；
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


class SlowFlow:
    def hold_workdir(self, item, target):
        note("hold-start", item["id"])
        time.sleep(config().get("hold_seconds", 0))
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
            time.sleep(config().get("check_seconds", 0))
            note("assess-end", item["id"])
        return Evidence(config().get("checks", [["true"]]), config().get("passed", True))

    def after(self, item, stage, key, target):
        note("after", item["id"], stage)


class SlowSource:
    def poll(self, target, cursor):
        note("source-start")
        time.sleep(config().get("source_seconds", 0))
        return Poll([], "after-batch")


class SlowChannel:
    def ask(self, key, card):
        return self.notify(key, card)

    def notify(self, key, card):
        note("send-start", key)
        time.sleep(config().get("channel_seconds", 0))
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


class QuickExecutor:
    def __init__(self, name):
        self.name = name

    def available(self, config):
        return True

    def release(self, item, target):
        note("release-start", item["id"])
        time.sleep(config().get("release_seconds", 0))
        note("release-end", item["id"])
        reason = config().get("release_reason", "")
        return Release(not reason, reason)

    def launch(self, token, brief, item, config, target):
        note("launch", self.name, item["id"], token)
        return Started(f"session:{self.name}:{item['id']}", f"handle:{token}", "/fake/workdir")

    def resume(self, token, session, brief, item, config, target):
        note("launch", self.name, item["id"], token)
        return Started(session, f"handle:{token}", "/fake/workdir")

    def poll(self, token, started):
        return "running"

    def stop(self, token, started):
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
        plugins.register("executor", name, QuickExecutor(name))
    plugins.register("channel", "channel", SlowChannel())
    plugins.register("source", "source", SlowSource())
    return plugins
