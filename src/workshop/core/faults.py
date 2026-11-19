"""故障隔离，以及到点重试。"""
from datetime import timedelta
import uuid
from .models import stamp
from .ports import Retryable, TargetBlocked
from .flows import entries


# 连续失败几次后升级为隔离；隔离期间的自动重试每次最多隔一小时。
RETRY_THRESHOLD = 3
RETRY_CAP_SECONDS = 3600


def transient(exc):
    """能自动重试的瞬时故障。

    端口约定里只有 Retryable 表示暂时不可用、可重试；等待服务一时不可达
    （WaitUnavailable）和 App 令牌一时签不出（IdentityError）都继承它。
    其余异常——TypeError、KeyError、配置缺字段、TargetBlocked 的动作前提缺失——
    仍按代码错误立即隔离，交项目经理。
    """
    return isinstance(exc, Retryable)


def retry_delay(count):
    """第 count 次失败后隔多久再试；第三次起进隔离，之后每小时一次。"""
    if count >= RETRY_THRESHOLD:
        return RETRY_CAP_SECONDS
    return min(300 * 2 ** (count - 1), RETRY_CAP_SECONDS)


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


def block(engine, item, reason, plugin=False, retry_at=None, failure_key=None):
    """把一件活标成卡住。

    retry_at 有值时是插件瞬时故障的隔离：车间到点自己重试，工序登记在车间名下，
    状态页和项目经理事项不用人手工 resume。
    """
    with engine.log.transaction():
        item = engine.log.get("items", item["id"])
        if plugin and item.get("plugin_blocked"):
            return
        saved = item.get("before_plugin_failure")
        if plugin and not item.get("plugin_blocked"):
            saved = {k: item.get(k) for k in ("state", "next", "blocked_by", "leave")}
        if plugin and retry_at:
            following = "到点自动重试插件故障；不用手工恢复"
            leave = {"kind": "工序", "owner": "车间", "at": retry_at, "about": "插件故障自动重试"}
        else:
            following = "项目经理修复阻塞后恢复"
            leave = {"kind": "等你", "about": reason}
        engine.change(item, "work_blocked", state="卡住", next=following, blocked_by=reason,
                      plugin_blocked=plugin, plugin_failure_key=failure_key if plugin else None,
                      plugin_retry_at=retry_at if plugin else None,
                      before_plugin_failure=saved, leave=leave)


def broken(engine, target, port, name, exc, retry=None):
    """隔离一个插件（或一个交付对象的动作前提）。

    retry 有值表示这是瞬时故障：标坏控制记下自动重试的时刻，到点由 wake_broken
    解除隔离并恢复被卡的活；真正的代码错误不传 retry，仍等项目经理处理。
    """
    # 瞬时故障和动作前提缺失只停当前交付对象里用这个流程插件的流程；同一插件的
    # 别的交付对象照常跑。真正的代码错误没法判断波及面，停所有用它的交付对象。
    current_only = transient(exc) or isinstance(exc, TargetBlocked)
    affected = [(target, None)]
    if port == 'flow':
        affected = [(t, flow) for t, rule in engine.rules.data['targets'].items()
                    for flow in entries(rule)
                    if engine.rules.target(t, flow)['plugin'] == name
                    and (not current_only or t == target)]
    retry_at = retry.get("at") if retry else None
    with engine.log.transaction():
        for target_name, flow in affected:
            scoped = flow is not None and 'flows' in engine.rules.data['targets'][target_name]
            key = f'@flow:{target_name}:{name}' if scoped else target_name
            state = engine.control(key)
            schedule = {'target': target_name, 'plugin': name, 'port': port,
                        'reason': f'{port}/{name}: {exc}',
                        'auto_retry': bool(retry), 'retry_at': retry_at,
                        'retry_count': retry.get('count') if retry else None,
                        'retry_key': retry.get('key') if retry else None,
                        'action_blocked': isinstance(exc, TargetBlocked)}
            if not state.get('broken'):
                engine.fact('plugin_failed', {'target': target_name, 'flow': flow, 'port': port,
                                           'name': name, 'error': repr(exc)})
                engine.fact('control', {'id': key, 'broken': True,
                                        'action_blocked': isinstance(exc, TargetBlocked),
                                        'since': stamp(engine.clock()), **schedule})
                auto = bool(retry)
            elif state.get('auto_retry'):
                # 同一段自动重试隔离里又失败：更新下次重试时间；真正的代码错误
                # （retry 为空）改成永久隔离。已经是永久隔离的代码错误则不被后来
                # 的瞬时故障改成自动重试，等项目经理修复。
                engine.fact('control', {'id': key, **schedule})
                auto = bool(retry)
            else:
                auto = False
            when = retry_at if auto else None
            # 只有没结束的活会被标卡住；不读已结束活的大正文（核心 R27）。
            for item in engine.log.open_items():
                if (item['target'] != target_name or item['state'] in ('完成', '放弃')
                        or (flow is not None and engine.item_target(item)['flow'] != flow)):
                    continue
                if not item.get('plugin_blocked'):
                    engine.block(item, f'{port}/{name}: {exc}', plugin=True, retry_at=when, failure_key=key)
                elif item.get('plugin_failure_key') == key and item.get('plugin_retry_at') != when:
                    # 同一段隔离里的自动重试时间变了，状态页和项目经理事项跟着改。
                    if when:
                        engine.change(item, next="到点自动重试插件故障；不用手工恢复",
                                      plugin_retry_at=when,
                                      leave={"kind": "工序", "owner": "车间", "at": when,
                                             "about": "插件故障自动重试"})
                    else:
                        engine.change(item, next="项目经理修复阻塞后恢复", plugin_retry_at=None,
                                      leave=engine.blocked_leave("项目经理修复阻塞后恢复"))


def active_broken(engine, target, flow=''):
    """当前真正生效的隔离控制：流程级优先，其次对象级；都没坏返回空。

    多流程交付对象里来源仍会读，隔离期间还会有新事件进来；record_event 用这段
    控制上的自动重试安排把新活挂到同一段隔离上，到点一起恢复。
    """
    plugin = engine.rules.target(target, flow)['plugin']
    for key in (f'@flow:{target}:{plugin}', target):
        control = engine.control(key)
        if control.get('broken'):
            return control
    return {}


def retry_due(engine, key):
    return (engine.control(f"retry:{key}").get("at") or "") <= stamp(engine.clock())


def retry_clear(engine, key):
    state = engine.control(f"retry:{key}")
    if state.get("count") or state.get("at") or state.get("since"):
        if state.get("count"):
            engine.fact("plugin_retry_ok", {"target": state.get("target"), "port": state.get("port"),
                                            "name": state.get("name"), "count": state.get("count")})
        engine.fact("control", {"id": f"retry:{key}", "count": 0, "at": None, "since": None,
                                "target": None, "port": None, "name": None})
    # 重试成功：这一段隔离自动解除，被它卡住的活按手工 resume 同一条路恢复。
    for control in engine.log.all("controls", where=("auto_retry", True)):
        if control.get("broken") and control.get("retry_key") == key:
            clear_broken(engine, control["id"], control)


def retry_failure(engine, target, port, name, key, exc):
    state = engine.control(f"retry:{key}")
    count = state.get("count", 0) + 1
    now = engine.clock()
    since = state.get("since") or stamp(now)
    at = stamp(now + timedelta(seconds=retry_delay(count)))
    with engine.log.transaction():
        engine.fact("plugin_retry", {"target": target, "port": port, "name": name, "count": count,
                                     "error": str(exc), "since": since, "at": at})
        engine.fact("control", {"id": f"retry:{key}", "count": count, "at": at, "since": since,
                                "target": target, "port": port, "name": name})
    if transient(exc) and count >= RETRY_THRESHOLD:
        engine.broken(target, port, name, exc, retry={"at": at, "count": count, "key": key})
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
        at = stamp(engine.clock() + timedelta(seconds=retry_delay(count)))
        engine.timer(item, f"start:{uuid.uuid4()}", at, "重试启动")
        engine.change(item, state="定时", round=None, next=at, retry_at=at,
                    leave={"kind": "工序", "owner": "车间", "at": at, "about": "重试启动"})
        engine.retry_failure(item["target"], port, name, f"start:{item['target']}:{name}", exc)


def target_broken(engine, target):
    """这个交付对象还有没有没解除的插件隔离（对象级或流程级）。"""
    if engine.control(target).get("broken"):
        return True
    prefix = f"@flow:{target}:"
    return any(control.get("broken") and control["id"].startswith(prefix)
               for control in engine.log.all("controls"))


def restore_items(engine, target, plugin=None, failure_key=None):
    """把被插件故障卡住的活恢复原状。

    failure_key 给定时只恢复这一段隔离卡住的活；旧事实没记隔离键的，等同目标
    不再有别的隔离时才恢复，宁可按老规矩等项目经理，也不提前放开别的故障。
    """
    for item in engine.log.open_items():
        if item["target"] != target or not item.get("plugin_blocked"):
            continue
        # 手动恢复按流程插件筛；按隔离键恢复时键本身已经说明是哪一段隔离，
        # 来源、执行者、工单这些端口名不是流程插件名，不能再按插件筛。
        if failure_key is None and plugin is not None and engine.item_target(item)["plugin"] != plugin:
            continue
        item_key = item.get("plugin_failure_key")
        if failure_key is not None and item_key != failure_key:
            if item_key is not None or target_broken(engine, target):
                continue
        restored = dict(item.get("before_plugin_failure") or {})
        if restored.get("state") == "等你" and not item.get("wait_for_ready") and not engine.waiting(item):
            restored.update(state="空闲", next="按已收到的回答继续", blocked_by="")
        engine.change(item, **restored, plugin_blocked=False, before_plugin_failure=None,
                      plugin_failure_key=None, plugin_retry_at=None)


def clear_broken(engine, key, control, *, wake=False):
    """解除一段插件隔离并恢复被它卡住的活；手动 resume 和到点自动重试走同一条路。

    解锁、恢复活和到点事实在同一个事务里：中途失败整笔回滚，下一拍仍能看到
    auto_retry 再试，不会留下「已解除隔离但活还卡着」的中间状态。
    """
    with engine.log.transaction():
        if wake:
            engine.fact("plugin_retry_wake", {"id": key, "target": control.get("target"),
                                              "plugin": control.get("plugin"),
                                              "retry_at": control.get("retry_at"),
                                              "count": control.get("retry_count")})
        engine.fact("control", {"id": key, "broken": False, "action_blocked": False,
                                "auto_retry": False, "retry_at": None})
        target = control.get("target")
        if target:
            restore_items(engine, target, control.get("plugin"), failure_key=key)


def wake_broken(engine):
    """到点的自动重试：解除隔离、恢复被卡的活，让这一拍按原来的路径再试一次。

    返回是否解除了隔离；收回工作目录这类每进程只做一次的步骤据此重新放行。
    """
    now = stamp(engine.clock())
    woke = False
    for control in engine.log.all("controls", where=("auto_retry", True)):
        if not control.get("broken") or not control.get("retry_at") or control["retry_at"] > now:
            continue
        clear_broken(engine, control["id"], control, wake=True)
        woke = True
    return woke
