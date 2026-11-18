"""完成钩子：阶段走完之后要做的事，以及到点再查一次。"""
from dataclasses import asdict
from .models import due, stamp
from .ports import Retryable
from .cards import notice
from .models import After
from . import waits
from .jobs import Pending


def after(engine, item, stage, key, target, *, recheck=False, restarting=False):
    request = engine.log.get("restarts", key)
    if request and not request.get("done") and not restarting:
        raise Pending(key, "等待重启后完成钩子", item["id"])
    effect_key = key
    identity = [key, item.get("pending_episode", 0), item.get("after_pending_count", 0)]
    if restarting:
        if not engine.first_ok_at:
            raise ValueError("首拍未成功，不能重调完成钩子")
        effect_key = f"{key}:restarted:{engine.instance}"
        identity.append(engine.instance)
        target = {**target, "runtime": {**target.get("runtime", {}),
                  "daemon": engine.heartbeat_process, "first_ok_at": engine.first_ok_at}}
    def invoke():
        # 同一完成钩子每次回查是一次新调用；回查次数进身份，免得认回上一次的结果。
        result = engine.call("flow", target["plugin"], "after", item, stage, key, target,
                             identity=identity,
                             item=item["id"], lookup={'wait_services': engine.rules.data.get('wait_services', {})})
        return asdict(result) if isinstance(result, After) else result
    if recheck and not restarting and (engine.log.get("effects", key) or {}).get("done"):
        engine.fact("effect", {"id": key, "done": False})
    value = engine.effect(effect_key, invoke)
    result = After(**value) if isinstance(value, dict) else After(aliases=value or [])
    if (not isinstance(result.blocked, str) or result.blocked and
            (result.pending or not isinstance(result.next, str) or not result.next.strip())):
        raise ValueError("回查阻塞须有下一步，不能同时继续等待")
    if (type(result.pending) is not bool or type(result.rerun) is not bool or not isinstance(result.reason, str)
            or not isinstance(result.next, str) or result.pending and (not result.reason or not result.next)):
        raise ValueError("仍留本阶段须有原因和下一步")
    if type(result.wake_on_completed) is not bool:
        raise ValueError("完成唤醒标记须是布尔值")
    if type(result.restart) is not bool or not isinstance(result.notice, str):
        raise ValueError("重启标记须是布尔值，通知须是字符串")
    if not isinstance(result.recovery_id, str):
        raise ValueError("恢复标识必须是字符串")
    if result.recovery_id:
        if result.recovery_id != (item.get("recovery") or {}).get("id"):
            raise ValueError("流程消费的不是本活当前恢复计划")
        engine.fact("recovery_applied", {"id": result.recovery_id}, item["id"],
                  source="recovery", ext_id="applied:" + result.recovery_id)
    if result.notice.strip():
        engine.card(item, f"after-notice:{key}", "notice", notice(result.notice))
    if restarting and result.restart:
        raise ValueError("重启后完成钩子不能再次请求重启")
    if result.wait is not None:
        waits.descriptor(result.wait)
        if not result.pending or result.rerun or result.blocked:
            raise ValueError("等待引用须配 pending=True，不得同时要求重跑或阻塞")
    if result.pending and not result.rerun and result.wait is None:
        due(result.recheck_after, engine.clock())
    aliases = result.aliases
    if not isinstance(aliases, list) or not all(isinstance(a, str) and a for a in aliases):
        raise ValueError("流程返回的别名必须是非空字符串列表")
    with engine.log.transaction():
        for alias in aliases:
            identity = f'{engine.prefix(item["target"], item.get("flow", ""))}:{alias}'
            existing = engine.log.get("aliases", identity) or engine.log.get("items", identity)
            if existing and existing["object_key"] != item["object_key"]:
                raise ValueError(f"别名冲突：{identity}")
            engine.fact("alias_bound", {"id": identity, "object_key": item["object_key"],
                      "item": item["id"]}, item["id"], source="alias", ext_id=f"{key}:{alias}")
    if key.startswith("after:program:"):
        engine.fact("program_after", {"stage": stage, "result": asdict(result)}, item["id"],
                  source="program-after", ext_id=f"{key}:{item.get('pending_episode', 0)}:{item.get('after_pending_count', 0)}")
    if result.restart:
        engine.fact("restart_requested", {"id": key, "item": item, "stage": stage,
                    "instance": engine.instance, "done": False}, item["id"],
                    source="restart", ext_id=key)
        engine.restart_requested = True
        raise Pending(key, "等待重启后完成钩子", item["id"])
    if restarting:
        with engine.log.transaction():
            engine.fact("effect", {"id": key, "done": True, "result": asdict(result)})
            engine.fact("restart_completed", {"id": key, "done": True,
                        "daemon": engine.heartbeat_process, "first_ok_at": engine.first_ok_at}, item["id"])
        engine.restart_completed = True
    return result


def resume_restarts(engine):
    """成功心跳之后发起重调；作业照常异步完成，首调不经过阶段等待的退避。"""
    for request in engine.log.all("restarts", unless=("done", True)):
        item, key = request["item"], request["id"]
        if request["instance"] == engine.instance:
            continue
        target = engine.item_target(item)
        if engine.flow_broken(item["target"], item.get("flow", "")) or not engine.retry_due("restart:" + key):
            continue
        try:
            engine.after(item, request["stage"], key, target, restarting=True)
            engine.retry_clear("restart:" + key)
        except Pending:
            pass
        except Retryable as exc:
            engine.retry_failure(item["target"], "flow", target["plugin"], "restart:" + key, exc)
        except Exception as exc:
            engine.broken(item["target"], "flow", target["plugin"], exc)


def pending_changes(engine, item, result, target, hook):
    if result.wait is not None:
        return waits.pending(engine, item, result.wait, hook)
    if result.wake_on_completed:
        hook = {**hook, "completion_cursor": engine.log.db.execute("SELECT coalesce(max(seq),0) FROM facts").fetchone()[0]}
    count = item.get("after_pending_count", 0) + 1
    changes = dict(next=result.next, gate_reason=result.reason, after_pending_count=count, after_wait=None)
    if count >= target.get("pending_limit", 5):
        changes.update(state="卡住", blocked_by="阶段等待达到上限",
                       after_wait={**hook, "delay": result.recheck_after, "rerun": result.rerun},
                       next=result.next + "；处理后用 recover --mode wait --reason 续等",
                       leave=engine.blocked_leave(result.next))
    elif result.rerun:
        changes.update(state="空闲", leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": result.next})
    else:
        # 正常等待采用流程的回查间隔；故障重试才走 retry 的指数退避。
        at = due(result.recheck_after, engine.clock())
        engine.timer(item, f"{hook['key']}:{item.get('pending_episode', 0)}:{count}", at, result.next)
        changes.update(state="定时", after_wait={**hook, "at": at},
                       leave={"kind": "工序", "owner": "车间", "at": at, "about": result.next})
    return changes


def recheck_after(engine, item):
    hook = item.get("after_wait")
    if not hook or item["state"] != "空闲" or stamp(engine.clock()) < hook["at"] or not engine.retry_due(hook["key"]):
        return
    target = engine.item_target(item)
    try:
        result = engine.after({**item, "result": hook["receipt"]}, item["stage"], hook["key"], target, recheck=True)
        with engine.log.transaction():
            if result.blocked:
                engine.fact("gate_blocked", {"gate_reason": result.blocked}, item["id"])
                engine.change(item, state="卡住", blocked_by=result.blocked, gate_reason=result.blocked,
                            next=result.next, after_wait=None, leave=engine.blocked_leave(result.next))
            elif result.pending:
                engine.change(item, **engine.pending_changes(item, result, target, hook))
            else:
                engine.change(item, stage=hook["to"], state="完成" if hook["complete"] else "空闲",
                            next=hook["to"], after_wait=None, after_pending_count=0,
                            pending_episode=item.get("pending_episode", 0) + 1, blocked_by="",
                            leave=hook["receipt"].get("leave") or {"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": hook["to"]})
        engine.retry_clear(hook["key"])
    except Retryable as exc:
        engine.retry_failure(item["target"], "flow", target["plugin"], hook["key"], exc)
    except Exception as exc:
        engine.broken(item["target"], "flow", target["plugin"], exc)


def effect(engine, key, action):
    entry = engine.log.get("effects", key)
    if not (entry or {}).get("done"):
        if entry is None or "done" not in entry:
            # 作业没出结果时每拍都会回到这里；意图只记一次。
            engine.fact("effect", {"id": key, "done": False})
        result = action()
        engine.fact("effect", {"id": key, "done": True, "result": result})
    return engine.log.get("effects", key).get("result")



def wake_completed(engine):
    """完成事实只提前叫醒同目标订阅者；故障重试期限和授权仍由原路径核验。"""
    for item in engine.log.all('items', where=('state', '定时'), fields=('id', 'target', 'state', 'after_wait')):
        hook = item.get('after_wait') or {}
        if item['state'] != '定时' or 'completion_cursor' not in hook:
            continue
        if engine.control(item['target']).get('paused'):
            continue
        facts = engine.log.selected_facts(('item_changed',), ('state',), after=hook['completion_cursor'])
        completed = [f for f in facts if f['body'].get('state') == '完成' and f['item'] != item['id']
                     and (engine.log.db.execute("SELECT json_extract(body,'$.target') FROM items WHERE id=?",
                                               (f['item'],)).fetchone() or [None])[0] == item['target']]
        if not completed:
            continue
        with engine.log.transaction():
            for timer in engine.log.all('timers', unless=('fired', True)):
                if timer['item'] == item['id'] and timer['id'].startswith(item['id'] + ':' + hook['key']):
                    engine.fact('timer_fired', {'id':timer['id']}, item['id'])
            engine.change(item, state='空闲', after_wait={**hook, 'at':stamp(engine.clock())},
                          next='同目标有活完成，回查原阶段')
