"""推送入口测试用的守护进程：主循环、信箱、叫醒和来源照生产（cli run 的循环体），只把流程和执行者换成记录替身。

argv：状态目录、规则 JSON。执行者每次启动一轮往状态目录的 launches.log 追加一行。
"""
import json
from pathlib import Path
import sys

from workshop.core.engine import Engine
from workshop.core.log import Log
from workshop.core.models import Evidence, STAGES, Started
from workshop.core.rules import Rules
from workshop.core.wake import Mailbox
from workshop.plugins.selectors.ordered import OrderedSelector
from workshop.registry import Registry

STATE = Path(sys.argv[1])


class Flow:
    def classify(self, event, item, target, find_external=None):
        return "update"

    def stages(self, target):
        return STAGES[:-1]

    def brief(self, item, stage, target):
        return f"完成{stage}并交回执"

    def assess(self, item, receipt, target):
        return Evidence([["true"]], reason="等来源补资料", awaits_update=True)

    def after(self, item, stage, key, target, services=None):
        return None


class Executor:
    def available(self, config):
        return True

    def launch(self, token, brief, item, config, target):
        with (STATE / "launches.log").open("a") as out:
            out.write(json.dumps({"token": token, "item": item["id"], "batch": brief.batch}) + "\n")
        return Started(f"session:{token}", f"handle:{token}", "/fake/workdir")

    def resume(self, token, session, brief, item, config, target):
        return self.launch(token, brief, item, config, target)

    def poll(self, token, started):
        return "running"

    def stop(self, token, started):
        return None

    def usage(self, token, started, since):
        return None


def main():
    data = json.loads(Path(sys.argv[2]).read_text())
    registry = Registry()
    registry.register("selector", "ordered", OrderedSelector())
    registry.register("flow", "flow", Flow())
    for name in ("writer", "reviewer", "acceptor"):
        registry.register("executor", name, Executor())
    engine = Engine(Log(STATE / "workshop.db"), Rules.validate(data), registry)
    paths = engine.wake_paths()
    with Mailbox(STATE, paths) as mailbox:
        (STATE / "daemon-ready").write_text("ready")
        received_at = None
        while True:
            engine.tick(received_at)
            # 常规周期按生产的最长 60 秒；推送要靠信箱盯 spool 目录即时叫醒，不等这一下。
            wake = mailbox.wait(engine.wait_timeout())
            engine.awaken(wake, paths)
            received_at = wake.received_at


if __name__ == "__main__":
    main()
