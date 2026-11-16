"""故障隔离，以及到点重试。"""
from datetime import timedelta
import uuid
from .models import stamp
from .ports import TargetBlocked
from .cards import notice
from .flows import entries


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
                incident = engine.log.db.execute('SELECT max(seq) FROM facts').fetchone()[0]
                engine.fact('control', {'id': key, 'broken': True, 'action_blocked': isinstance(exc, TargetBlocked)})
                engine.card({'id': f'@{key}'}, f'plugin:{port}:{name}:{incident}', 'notice',
                          notice(f'{target_name} 插件故障：{exc}'))
            for item in engine.log.all('items'):
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

