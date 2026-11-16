"""完整关系快照先于派轮；等待由代码承担，不占执行位置。"""
from collections import defaultdict
from dataclasses import asdict

from .jobs import Pending
from .models import Relations, STAGES, stamp


def refresh(engine, items):
    groups = defaultdict(list)
    enabled, ready = set(), set()
    for item in items:
        target = engine.item_target(item)
        if callable(getattr(engine.plugins.get("flow", target["plugin"]), "relations", None)):
            enabled.add(item["id"])
            groups[(item["target"], item.get("flow", ""))].append(item)
    for (name, flow), entries in groups.items():
        if engine.control(name).get("paused") or engine.flow_broken(name, flow):
            continue
        target = engine.item_target(entries[0])
        objects = sorted(({"id": i["id"], "object_key": i["object_key"]} for i in entries), key=lambda i: i["id"])
        try:
            stages = engine.call("flow", target["plugin"], "stages", target)
            snapshots = engine.call("flow", target["plugin"], "relations", objects, target,
                                    identity=[target, objects])
            if not isinstance(snapshots, dict) or set(snapshots) != {i["id"] for i in entries}:
                raise ValueError("关系快照未覆盖本批全部活")
        except Pending:
            continue  # 首读和过期后的刷新均不能沿用旧快照派轮。
        except Exception as exc:
            snapshots = {i["id"]: Relations(error=str(exc)) for i in entries}
        for item in entries:
            value = snapshots[item["id"]]
            try:
                if not isinstance(value, Relations):
                    raise ValueError("关系端口未返回 Relations")
                value.validate()
                if value.members and value.converge not in stages:
                    raise ValueError("关系汇合阶段不在当前流程中")
            except Exception as exc:
                value = Relations(error=str(exc))
            data = asdict(value)
            if item.get("relations") != data:
                engine.change(item, relations=data)
            ready.add(item["id"])
    engine.relations_enabled, engine.relations_ready = enabled, ready


def admit(engine, item):
    """返回可继续推进的最新活，或 None；已有其他等待/故障和在跑轮不被覆盖。"""
    if item["id"] not in getattr(engine, "relations_enabled", set()):
        return item
    if item["id"] not in engine.relations_ready:
        return None
    data = item.get("relations") or {}
    if any(r["item"] == item["id"] for r in engine.active()) or item.get("program"):
        return item
    if item.get("pending_advance"):
        return item  # 先结算已有授权，不能用汇合清掉人的回答。
    if item.get("aggregate") and not data.get("error") and not data.get("members"):
        # 最后一项被移走，恢复进入总单前的正常路径；旧总单的问卡不能批准普通活。
        engine.cancel_questions(item)
        origin = item.get("aggregate_origin") or "说清楚"
        engine.change(item, aggregate=False, aggregate_origin=None, stage=origin, state="空闲",
                      next="已无子单，恢复" + origin, blocked_by="", relation_hold=None,
                      relation_error=False, approved=[], pending_advance=None, wait_for_ready=False,
                      after_wait=None, retry_at=None, retry_executor=None, serial_hold=False,
                      pending_episode=item.get("pending_episode", 0) + 1)
        item = engine.log.get("items", item["id"])
    owned = item.get("relation_hold")
    if item["state"] not in ("空闲", "排队", "等前置", "等子单") and not (
            owned and item["state"] == "卡住" and item.get("relation_error")):
        return item
    error = data.get("error", "")
    waiting = [n for n in data.get("prerequisites", []) if not n["complete"]]
    children = [n for n in data.get("members", []) if not n["complete"]]
    state = "卡住" if error else "等前置" if waiting else "等子单" if children else ""
    if state:
        about = "关系读取失败：" + error if error else state + "：" + "、".join(n["name"] for n in (waiting or children))
        saved = owned or {k: item.get(k) for k in ("state", "next", "blocked_by", "leave")}
        if item["state"] != state or item.get("next") != about or not owned:
            engine.change(item, state=state, next=about, blocked_by=error, serial_hold=False,
                          relation_hold=saved, relation_error=bool(error),
                          leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": about})
        return None
    if owned:
        # 排队位置重新计算，旧串行占位不会复活。
        engine.change(item, state="空闲", next=item["stage"], blocked_by="", relation_hold=None,
                      relation_error=False, serial_hold=False,
                      leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": "关系已满足"})
        item = engine.log.get("items", item["id"])
    converge = data.get("converge")
    if data.get("members") and converge and STAGES.index(item["stage"]) < STAGES.index(converge):
        engine.change(item, stage=converge, state="空闲", next="子单全部完成，进入" + converge,
                      aggregate=True, aggregate_origin=item.get("aggregate_origin") or item["stage"],
                      serial_hold=False, pending_advance=None,
                      after_wait=None, retry_at=None, retry_executor=None)
        item = engine.log.get("items", item["id"])
    return item
