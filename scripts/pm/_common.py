"""项目经理工具的公共部分：打开线上状态库和车间核心，按 `gc#170` 或 `grant-console:170` 找活。

这些工具绕过 `workshop` 命令直接调核心改状态。每个工具都对应车间缺的一个机制，做成命令后就删掉。
"""
import os
import sys
from pathlib import Path

from workshop.core.engine import Engine, stamp
from workshop.core.log import Log
from workshop.core.holding import resolve
from workshop.core.rules import Rules
from workshop.registry import Registry

STATE_DIR = Path(os.environ.get("WORKSHOP_STATE_DIR", "~/.local/state/workshop")).expanduser()


def open_engine():
    log = Log(STATE_DIR / "workshop.db")
    saved = log.get("controls", "@rules")
    if not saved:
        sys.exit("状态库没有规则快照")
    rules = Rules(Rules.validate(saved["data"]).data, saved.get("path"))
    return Engine(log, rules, Registry()), rules


def find(engine, rules, text):
    return engine.log.get("items", resolve(engine.log, rules.data.get("targets", {}), text))


def running(engine, item_id):
    return [r for r in engine.active() if r["item"] == item_id]


def back_to_stage(engine, item, reason, **extra):
    """放回原阶段重排，原因记成「项目经理：…」。"""
    engine.change(item, state="空闲", blocked_by="", next=item["stage"], gate_reason="项目经理：" + reason,
                  leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": item["stage"]}, **extra)


__all__ = ["open_engine", "find", "running", "back_to_stage", "stamp", "STATE_DIR"]
