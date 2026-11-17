"""暂停、恢复、放弃，以及在这里放开。"""
from dataclasses import asdict, replace
from datetime import timedelta
import uuid
from .models import Event, due, stamp
from .flows import entries
from .stages import gate
from .gates import initial_stage


def drop(engine, item_id):
    """清掉一件还没跑过轮次的活；之后同一对象要再放行才重新建活。"""
    with engine.log.transaction():
        item = engine.log.get("items", item_id)
        if not item:
            raise ValueError(f"找不到活：{item_id}")
        rounds = [r for r in engine.log.all("rounds") if r.get("item") == item_id]
        cards = [c for c in engine.log.all("cards") if c.get("item") == item_id]
        timers = [t for t in engine.log.all("timers") if t.get("item") == item_id and not t.get("fired")]
        effects = [e for e in engine.log.all("effects") if e.get("item") == item_id and not e.get("done")]
        aliases = [a for a in engine.log.all("aliases") if a.get("item") == item_id]
        if item.get("program") or item["state"] != "空闲" or item.get("history") or item.get("sessions") or rounds or cards or timers or effects or aliases:
            raise ValueError(f"{item_id} 已经跑过轮次或有未了的卡、定时、别名，不能清掉")
        engine.fact("item_dropped", {"id": item_id}, item_id)


def abandon(engine, item_id, reason):
    """Kun 决定不做了：停在当前阶段，状态「放弃」，取消没答的问题，让出串行路径。
    记录都留着；要再做就返工（可信的「不对」），回到执行重新排队。"""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("放弃要写原因")
    with engine.log.transaction():
        item = engine.log.get("items", item_id)
        if not item:
            raise ValueError(f"找不到活：{item_id}")
        if item["state"] in ("完成", "放弃"):
            raise ValueError(f"{item_id} 已经{item['state']}，不用放弃")
        if item.get("program"):
            raise ValueError(f"程序阶段还没结算，不能放弃；先用 workshop pause {item['target']} 暂停。"
                             "暂停不会自动放弃或取消在跑作业，请由项目经理处理未结算作业后再决定是否放弃")
        if any(r["item"] == item_id for r in engine.active()):
            raise ValueError(f"{item_id} 还有一轮没结束，先用 abort 结束那一轮再放弃")
        engine.fact("item_abandoned", {"id": item_id, "reason": reason.strip()}, item_id)
        engine.cancel_questions(item)
        # 放弃前收到的返工、插件故障的恢复快照都作废：只有放弃之后的新返工才让它回来。
        engine.change(item, state="放弃", next=f"已放弃：{reason.strip()}", blocked_by="", serial_hold=False,
                    pending_advance=None, after_wait=None, wait_for_ready=False, retry_at=None,
                    pending_rework=False, plugin_blocked=False, before_plugin_failure=None,
                    leave={"kind": "等你", "about": "已放弃；要再做就返工"})


def recover(engine, item_id, mode, reason, quote="", ref=None):
    """恢复追加事实；不删除历史，不替代审批，不与正在跑的一轮并发。"""
    if mode not in ("rework", "wait", "verify"):
        raise ValueError("恢复方式只能是 rework、wait、verify")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("恢复必须写非空 --reason")
    if mode == "rework" and (not isinstance(quote, str) or not quote.strip()):
        raise ValueError("返工恢复必须写 Kun 原话 --quote")
    if ref is not None and (mode != "verify" or ref != "main"):
        raise ValueError("只有 verify 可以指定 --ref main")
    item = engine.log.get("items", item_id)
    if not item:
        raise ValueError(f"找不到活：{item_id}")
    if item.get("program"):
        raise ValueError("程序阶段还没结算，不能恢复")
    if any(r["item"] == item_id for r in engine.active()):
        raise ValueError("还有一轮没结束，不能恢复")
    if item["state"] not in ("卡住", "放弃", "等你", "空闲"):
        raise ValueError("只能恢复已停止的活")
    if item.get("pending_rework"):
        raise ValueError("已经登记返工，不能重复恢复")
    if engine.flow_broken(item["target"], item.get("flow", "")):
        raise ValueError("插件尚在隔离，先修复插件")
    if engine.waiting(item) and mode != "rework":
        raise ValueError("仍有有效问卡，不能用恢复代替回答")
    target = engine.item_target(item)
    plan = {}
    if mode == "wait":
        hook = item.get("after_wait")
        if not hook and item.get("blocked_by") == "阶段等待达到上限":
            # 旧版超限时没有保留回查入口，从已结束轮及完成钩子的事实恢复。
            rows = sorted((r for r in engine.log.all("rounds") if r["item"] == item_id
                           and r["stage"] == item["stage"] and r.get("status") == "ended"),
                          key=lambda r: r.get("ended", ""), reverse=True)
            for row in rows:
                key = f'after:{row["token"]}'
                effect = engine.log.get("effects", key) or {}
                value = effect.get("result") or {}
                receipt = row.get("receipt") or {}
                if isinstance(value, dict) and value.get("pending") and not value.get("rerun") and receipt.get("advance"):
                    hook = {"key": key, "receipt": receipt, "to": receipt["advance"], "complete": False,
                            "delay": value.get("recheck_after", "+5m"), "rerun": False}
                    break
        if (item["state"] != "卡住" or item.get("blocked_by") != "阶段等待达到上限"
                or not hook or hook.get("rerun")):
            raise ValueError("不是可续等的阶段回查；需要重新执行时使用 rework")
        at = max(due(hook.get("delay", "+5m"), engine.clock()),
                 stamp(engine.clock() + timedelta(minutes=5)))
        plan = {"at": at}
    elif mode == "verify":
        flow = engine.plugins.get("flow", target["plugin"])
        prepare = getattr(flow, "prepare_recovery", None)
        if not prepare:
            raise ValueError("本流程不支持追加核验")
        plan = prepare(item, {"ref": ref}, target)
        if not isinstance(plan, dict):
            raise ValueError("流程恢复计划必须是对象")
    recovery = {"id": str(uuid.uuid4()), "mode": mode, "reason": reason.strip(),
                "quote": quote.strip(), "plan": plan}
    with engine.exclusive(), engine.log.transaction():
        current = engine.log.get("items", item_id)
        if current != item or any(r["item"] == item_id for r in engine.active()):
            raise ValueError("活已变化，请重新核对后恢复")
        engine.fact("recovery_requested", {**recovery, "from_state": item["state"],
                  "stage": item["stage"]}, item_id)
        changes = dict(recovery=recovery, blocked_by="", wait_for_ready=False, retry_at=None,
                       failures=0, gate_count=0, plugin_blocked=False, before_plugin_failure=None,
                       gate_reason=reason.strip() + (f"；Kun 原话：{quote.strip()}" if quote else ""))
        if mode == "wait":
            engine.timer(item, "recover:" + recovery["id"], at, hook["receipt"]["summary"])
            changes.update(state="定时", after_wait={**hook, "at": at}, after_pending_count=0,
                           pending_episode=item.get("pending_episode", 0) + 1,
                           next="按原退避继续阶段回查", leave={"kind": "工序", "owner": "车间", "at": at,
                                                                   "about": "按原退避继续阶段回查"})
        else:
            engine.cancel_questions(item)
            changes.update(state="空闲", pending_rework=True, next="按恢复事实返工",
                           leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": "恢复后返工"})
        engine.change(item, **changes)
    return recovery


def pause(engine, target):
    engine.rules.target(target)
    engine.fact("control", {"id": target, "paused": True})


def missed(engine, target):
    return [e for e in engine.log.all("deferred") if not e["resolved"] and e["event"]["target"] == target]


def resume(engine, target, selected: list[str], expected: list[str] | None = None):
    engine.rules.target(target)
    with engine.log.transaction():
        missed = engine.missed(target)
        if expected is not None and set(expected) != {e["id"] for e in missed}:
            raise ValueError("遗漏清单已变化，请重新查看后选择")
        if not set(selected).issubset({e["id"] for e in missed}):
            raise ValueError("选择中有未知事件；请重新查看遗漏清单")
        engine.fact("control", {"id": target, "paused": False, "broken": False})
        for entry in missed:
            replay = entry["id"] in selected
            engine.fact("deferred_resolved", {"id": entry["id"], "replayed": replay})
            if replay:
                body = dict(entry["event"])
                ingress = body.pop("ingress", "unknown")
                body.pop("pending_classification", None)
                body.pop("release_gate", None)
                event = Event(**body)
                if ingress == "unknown":
                    event = replace(event, action="update")
                event = replace(event, object_key=engine.resolve_key(event.target, event.object_key, event.flow))
                event = replace(event, action=engine.after_abandon(event.item_id, int(entry["id"]), event.action))
                engine.fact("event", {**asdict(event), "ingress": ingress, "pending_classification": ingress == "external",
                                    "initial_stage": initial_stage(engine.rules.target(event.target, event.flow)),
                                    **gate(ingress)}, event.item_id, source="resume", ext_id=entry["id"])
                if ingress == "external":
                    engine.classification_intent(event, int(entry["id"]))
                    continue
                replayed = engine.log.get("items", event.item_id)
                if replayed is None:
                    continue  # 没放行的对象只记了事实。
                if event.depth > engine.rules.data["limits"]["chain_depth"]:
                    engine.block(replayed, "事件连锁超过上限")
                elif event.source == "timer":
                    timer = engine.log.get("timers", event.ext_id)
                    if timer:
                        engine.apply_timer(timer, replayed)
        # 恢复只解除插件故障；重试耗尽和连锁上限须由新决定处理。
        engine.restore_target(target)


def restore_target(engine, target, plugin=None):
    for flow in entries(engine.rules.data['targets'][target]):
        name = engine.rules.target(target, flow)['plugin']
        key = f'@flow:{target}:{name}'
        if plugin is not None and name != plugin:
            continue
        if engine.control(key).get('broken'):
            engine.fact('control', {'id': key, 'broken': False, 'action_blocked': False})
    # 被插件故障卡住的活都还没结束，未结束活的查询覆盖它们。
    for item in engine.log.open_items():
        if (item["target"] == target and item.get("plugin_blocked")
                and (plugin is None or engine.item_target(item)["plugin"] == plugin)):
            restored = dict(item["before_plugin_failure"])
            if restored["state"] == "等你" and not item.get("wait_for_ready") and not engine.waiting(item):
                restored.update(state="空闲", next="按已收到的回答继续", blocked_by="")
            engine.change(item, **restored, plugin_blocked=False, before_plugin_failure=None)


def release_here(engine, ref, quote, session):
    """会话确认的放行；外部正文不能通过自报来源调用此入口。"""
    from .report import resolve
    if not quote.strip() or not session.strip():
        raise ValueError("要写 Kun 的原话和会话号")
    with engine.log.transaction():
        item_id = resolve(engine.log, engine.rules.data['targets'], ref, allow_new=True)
        target, key = item_id.split(':', 1)
        original = engine.log.get('items', item_id)
        flow = original.get('flow', '') if original else ''
        if original:
            key = original['object_key']
        elif ':' in key:
            flow, key = key.split(':', 1)
        if engine.control(target).get('action_blocked'):
            engine.fact('control', {'id': target, 'broken': False, 'action_blocked': False})
            engine.restore_target(target)
        selected = engine.rules.target(target, flow)
        failure_key = f"@flow:{target}:{selected['plugin']}"
        if engine.control(failure_key).get('action_blocked'):
            engine.restore_target(target, selected['plugin'])
        engine.ingest(Event('session', str(uuid.uuid4()), target, key, f'会话放行 {item_id}',
                          {'channel': 'session', 'session': session, 'quote': quote}, action='ready', flow=flow), directed=True)
    return item_id

