"""故障隔离，以及到点重试。"""
from datetime import timedelta
import uuid
from .models import stamp
from .ports import TargetBlocked
from .flows import entries


def delivery_error(engine, key, exc, item=None):
    state = engine.control(f"delivery:{key}")
    if not state.get("failed"):
        with engine.log.transaction():
            engine.fact("delivery_failed", {"key": key, "error": repr(exc)}, item)
            engine.fact("control", {"id": f"delivery:{key}", "failed": True, "since": stamp(engine.clock())})


def delivery_ok(engine, key):
    if engine.control(f"delivery:{key}").get("failed"):
        engine.fact("control", {"id": f"delivery:{key}", "failed": False})


def delivery_retired(rules, key):
    """送达键对应的通道或交付对象是否已不在现行规则里。

    送达键的形状和它们对应的规则：
    - channel:<名>：旧规则顶层 channel 指定的通道；现行规则不再收这个条目，等于一个都没有。
    - <交付对象>:...：旧的逐卡送达以交付对象打头，read: 和 answer: 后面也是卡号。
    - poll:/finish:<令牌>：轮的令牌归核心状态，不归规则；残留由轮次结算清掉。
    其余形状现行规则和核心都不产出，按退役处理，免得旧标记永远挂着。
    """
    parts = key.split(":")
    head = parts[0]
    if head == "channel":
        channels = rules.data.get("channel")
        names = {channels} if isinstance(channels, str) else set(channels or ())
        return len(parts) < 2 or parts[1] not in names
    if head in ("poll", "finish"):
        return False
    name = parts[1] if head in ("read", "answer") and len(parts) > 1 else head
    return name not in rules.data.get("targets", {})


def clear_retired(engine):
    """规则里已经没有的通道或交付对象留下的送达失败标记载不住了，清掉。"""
    prefix = "delivery:"
    for control in engine.log.all("controls", where=("failed", True)):
        if control["id"].startswith(prefix) and delivery_retired(engine.rules, control["id"][len(prefix):]):
            engine.delivery_ok(control["id"][len(prefix):])


def block(engine, item, reason, plugin=False):
    with engine.log.transaction():
        item = engine.log.get("items", item["id"])
        if plugin and item.get("plugin_blocked"):
            return
        saved = item.get("before_plugin_failure")
        if plugin and not item.get("plugin_blocked"):
            saved = {k: item.get(k) for k in ("state", "next", "blocked_by", "leave")}
        engine.change(item, "work_blocked", state="卡住", next="项目经理修复阻塞后恢复", blocked_by=reason,
                    plugin_blocked=plugin, before_plugin_failure=saved, leave={"kind": "等你", "about": reason})


def broken(engine, target, port, name, exc):
    affected = [(target, None)]
    if port == 'flow':
        affected = [(t, flow) for t, rule in engine.rules.data['targets'].items()
                    for flow in entries(rule)
                    if engine.rules.target(t, flow)['plugin'] == name
                    and (not isinstance(exc, TargetBlocked) or t == target)]
    with engine.log.transaction():
        for target_name, flow in affected:
            scoped = flow is not None and 'flows' in engine.rules.data['targets'][target_name]
            key = f'@flow:{target_name}:{name}' if scoped else target_name
            if not engine.control(key).get('broken'):
                engine.fact('plugin_failed', {'target': target_name, 'flow': flow, 'port': port,
                                           'name': name, 'error': repr(exc)})
                engine.fact('control', {'id': key, 'broken': True, 'action_blocked': isinstance(exc, TargetBlocked)})
            # 只有没结束的活会被标卡住；不读已结束活的大正文（核心 R27）。
            for item in engine.log.open_items():
                if (item['target'] == target_name and item['state'] not in ('完成', '放弃')
                        and not item.get('plugin_blocked')
                        and (flow is None or engine.item_target(item)['flow'] == flow)):
                    engine.block(item, f'{port}/{name}: {exc}', plugin=True)


def retry_due(engine, key):
    return (engine.control(f"retry:{key}").get("at") or "") <= stamp(engine.clock())


def retry_clear(engine, key):
    if engine.control(f"retry:{key}").get("count"):
        engine.fact("control", {"id": f"retry:{key}", "count": 0, "at": None, "since": None})


def retry_failure(engine, target, port, name, key, exc):
    count = engine.control(f"retry:{key}").get("count", 0) + 1
    at = stamp(engine.clock() + timedelta(seconds=min(300 * 2 ** (count - 1), 3600)))
    engine.fact("plugin_retry", {"target": target, "port": port, "name": name, "count": count, "error": str(exc)})
    engine.fact("control", {"id": f"retry:{key}", "count": count, "at": at})
    if count >= 3:
        engine.broken(target, port, name, exc)
    return at


def start_retry(engine, item, row, port, name, exc):
    with engine.log.transaction():
        if row and row["status"] == "starting":
            engine.fact("round_changed", {"token": row["token"], "status": "ended", "stopped": True,
                      "outcome": "failed", "summary": str(exc), "ended": stamp(engine.clock())}, item["id"])
            engine.fact("round_result", {"token": row["token"], "stage": row["stage"], "outcome": "failed",
                      "summary": str(exc), "evidence": [], "reason": "暂时启动失败"}, item["id"])
        # 先登记工序，再隔离，故障解除时仍保持这条退避安排。
        count = engine.control(f"retry:start:{item['target']}:{name}").get("count", 0) + 1
        at = stamp(engine.clock() + timedelta(seconds=min(300 * 2 ** (count - 1), 3600)))
        engine.timer(item, f"start:{uuid.uuid4()}", at, "重试启动")
        engine.change(item, state="定时", round=None, next=at, retry_at=at,
                    leave={"kind": "工序", "owner": "车间", "at": at, "about": "重试启动"})
        engine.retry_failure(item["target"], port, name, f"start:{item['target']}:{name}", exc)

