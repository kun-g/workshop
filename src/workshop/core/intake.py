"""收事件、归类，以及交回执。"""
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import json
from .gates import advance_problem, validate_stages, initial_stage
from .models import Event, Receipt, ReceiptError, stamp
from .ports import Retryable
from .models import Classification
from .jobs import Pending
from .flows import entries, matches
from .stages import gate


def ingest(engine, event: Event, round_token: str | None = None, *, ingress: str = "source", directed=False) -> bool:
    if ingress == "external":
        event = replace(event, source=f"cli:{event.source}")
    if round_token:
        row = engine.log.get("rounds", round_token)
        if not row:
            raise ValueError("未知事件来源轮次令牌")
        parent = engine.log.get("items", row["item"])
        event = replace(event, depth=parent["depth"] + 1, action="update")
        ingress = "agent"
    elif ingress == "external":
        event = replace(event, depth=0, action="update")
    elif ingress != "source":
        raise ValueError("事件入口必须是可信来源、外部入口或带轮次令牌的 agent")
    event.validate()
    target = engine.rules.target(event.target)
    # 只有核心内部的定时与会话放行可绕过触发器定向；外部自报流程不授予权限。
    if directed:
        selected = [(engine.rules.target(event.target, event.flow), event.action)]
    elif 'flows' not in target:
        action = event.action
        for trigger in target.get('legacy_triggers', {}).get(event.source, []) if event.action_id else []:
            if matches(event.body, trigger.get('when', {})):
                action = trigger['action'] if ingress == 'source' else 'update'
                break
        selected = [(target, action)]
    else:
        selected = []
        for flow in entries(target):
            config = engine.rules.target(event.target, flow)
            for trigger in config['triggers']:
                if trigger['source'] == event.source and matches(event.body, trigger.get('when', {})):
                    selected.append((config, trigger['action'] if ingress == 'source' else 'update'))
                    break
    if not selected and round_token:
        # 回执子事件的来源带轮次标识，默认跟随原流程；跨对象时用对方兼容流程。
        flow = parent.get('flow', '') if parent['target'] == event.target else ''
        selected = [(engine.rules.target(event.target, flow), 'update')]
    with engine.log.transaction():
        if not selected:
            return engine.fact('event', {**asdict(event), 'ingress': ingress, 'unmatched': True},
                             source=event.source, ext_id=json.dumps([event.target, '', event.ext_id], ensure_ascii=False))
        added = False
        for config, action in selected:
            scoped = replace(event, flow=config['flow'], flow_default=config['flow_default'], action=action,
                             ext_id=event.action_id if action != 'update' and event.action_id else event.ext_id)
            # 旧兼容流程的历史去重标识仍有效；新流程不受旧记录影响。
            if config['flow_default'] and 'flows' in target and not directed:
                ids = [scoped.ext_id, *event.previous_ids]
                if any(engine.log.db.execute("SELECT 1 FROM facts WHERE source=? AND ext_id=? AND json_extract(body, '$.target')=?",
                                           (event.source, key, event.target)).fetchone() for key in ids):
                    continue
            scoped = replace(scoped, object_key=engine.resolve_key(scoped.target, scoped.object_key, scoped.flow))
            added = engine.record_event(scoped, ingress, namespaced='flows' in target and not directed) or added
        return added


def record_event(engine, event, ingress, *, namespaced=False):
    first = initial_stage(engine.rules.target(event.target, event.flow))
    with engine.log.transaction():
        paused = engine.control(event.target).get("paused", False)
        kind = "deferred_event" if paused else "event"
        added = engine.fact(kind, {**asdict(event), "ingress": ingress, "pending_classification": ingress == "external",
                                 **({'initial_stage': first} if not paused else {}),
                                 **gate(ingress)}, event.item_id, source=event.source,
                                 ext_id=json.dumps([event.target, event.flow, event.ext_id], ensure_ascii=False) if namespaced else event.ext_id)
        if added and not paused:
            if ingress == "external":
                engine.classification_intent(event)
                return added
            item = engine.log.get("items", event.item_id)
            if item is None:
                return added  # 没放行的对象只记了事实。
            if item["depth"] > engine.rules.data["limits"]["chain_depth"]:
                engine.block(item, "事件连锁超过上限")
            elif engine.flow_broken(event.target, event.flow):
                engine.block(item, "流程插件等待修复", plugin=True)
        return added


def classification_intent(engine, event, received_seq=None):
    """received_seq：原来收到时的事实序号；暂停后补放的事件带原序号，放弃前收到的旧返工照样作废。"""
    seq = engine.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0]
    engine.fact("effect", {"id": f"classify:{seq}", "kind": "classify", "done": False,
                         "event": asdict(event), "item": event.item_id, "event_seq": seq,
                         "received_seq": received_seq or seq, "defer_creation": True}, event.item_id)


def classify_events(engine):
    for entry in engine.log.all("effects", unless=("done", True)):
        if entry.get("kind") != "classify" or entry["done"]:
            continue
        event = Event(**entry["event"])
        control = engine.control(event.target)
        if control.get("paused") or engine.flow_broken(event.target, event.flow) or not engine.retry_due(entry["id"]):
            continue
        target = engine.rules.target(event.target, event.flow)
        try:
            item = engine.log.get("items", event.item_id)
            result = engine.call("flow", target["plugin"], "classify", event, item, target,
                               identity=[entry["id"], entry["event"]], lookup=engine.scope(event.target, event.flow))
            decision = Classification(result) if isinstance(result, str) else result
            if not isinstance(decision, Classification):
                raise ValueError("流程返回了未知判定")
            if type(decision.discard) is not bool:
                raise ValueError("丢弃标记必须是布尔值")
            if decision.summary is not None and not isinstance(decision.summary, str):
                raise ValueError("判定摘要必须是字符串")
            note = {"summary": decision.summary.strip()} if decision.summary and decision.summary.strip() else {}
            if decision.discard:
                engine.fact("event_discarded", {"id": entry["id"], "event_seq": entry["event_seq"], **note})
                engine.retry_clear(entry["id"])
                continue
            action = decision.action
            if decision.object_key is not None:
                key = engine.resolve_key(event.target, decision.object_key, event.flow)
                if not isinstance(key, str) or not key or not engine.log.get("items", f"{engine.prefix(event.target, event.flow)}:{key}"):
                    raise ValueError("可信归件只能指向已有活")
                event = replace(event, object_key=key)
            else:
                event = replace(event, object_key=engine.resolve_key(event.target, event.object_key, event.flow))
            if action not in ("update", "ready", "rework"):
                raise ValueError("流程返回了未知事件动作")
            action = engine.after_abandon(event.item_id, entry.get("received_seq", entry["event_seq"]), action)
            with engine.log.transaction():
                engine.fact("event_classified", {"id": entry["id"], "action": action,
                          "event_seq": entry["event_seq"], **note,
                          **({"event": {**asdict(event), "action": action,
                                        "initial_stage": initial_stage(target)}} if entry.get("defer_creation") else {})}, event.item_id)
            engine.retry_clear(entry["id"])
        except Pending:
            continue
        except Retryable as exc:
            engine.retry_failure(event.target, "flow", target["plugin"], entry["id"], exc)
        except Exception as exc:
            engine.broken(event.target, "flow", target["plugin"], exc)


def after_abandon(engine, item_id, seq, action):
    """放弃之前收到的事件，晚到的分类或补放只当资料：返工、放行降为普通更新。"""
    item = engine.log.get("items", item_id)
    if action in ("rework", "ready") and item and seq < item.get("abandoned_seq", 0):
        return "update"
    return action


def submit(engine, token: str, data: dict) -> bool:
    row = engine.log.get("rounds", token) if isinstance(token, str) else None
    if row and row.get("question_blocked") and row.get("receipt") is not None:
        if data == row["question_rejections"][-1]["receipt"]:
            return False  # 第二次坏卡已结算；调用方重发原文也不能再结算。
    try:
        try:
            receipt = Receipt.parse(data, engine.clock())
        except ValueError as exc:
            # advance 填错也并进同一次拒绝，执行者一次改全。
            advance = data.get("advance") if isinstance(data, dict) else None
            extra = engine.advance_error(token, advance) if isinstance(advance, str) else ""
            if extra and extra not in str(exc):
                raise ValueError(f"{exc}；{extra}") from exc
            raise
        for event in receipt.events:
            engine.rules.target(event["target"])
        extra = engine.advance_error(token, receipt.advance)
        if extra:
            raise ValueError(extra)
    except (ValueError, TypeError, KeyError) as exc:
        ask_only = isinstance(exc, ReceiptError) and exc.ask_only
        if ask_only:
            # 先核对其余协议和动态约束，不能把阶段错误或未知目标藏在坏卡后面。
            clean = {**data, "ask": [], "leave": None}
            try:
                candidate = Receipt.parse(clean, engine.clock())
                for event in candidate.events:
                    engine.rules.target(event["target"])
                if engine.advance_error(token, candidate.advance):
                    ask_only = False
            except (ValueError, TypeError, KeyError):
                ask_only = False
        with engine.log.transaction():
            count = engine.receipt_rejected(token, exc, data=data, ask_only=ask_only)
            if ask_only and count >= 2:
                # 保存原证据，但不执行这份回执要求的任何动作；由项目经理修复后恢复。
                engine.fact("round_changed", {"token": token, "question_blocked": True}, row["item"])
                return engine.submit(token, {"outcome": "nothing", "summary": "问卡格式不对，修正后恢复",
                                           "evidence": candidate.evidence})
        raise
    with engine.log.transaction():
        row = engine.log.get("rounds", token)
        if not row:
            raise ValueError("未知轮次令牌")
        if row.get("receipt") is not None:
            if row["receipt"] != data:
                raise ValueError("该轮已交不同回执")
            return False
        if row["status"] not in ("starting", "running"):
            raise ValueError("该轮不能再交回执")
        return engine.fact("receipt", {"token": token, "receipt": data}, row["item"],
                         source="receipt", ext_id=token)


def advance_error(engine, token, advance) -> str:
    """交回执时就按本阶段核对 advance，执行者能在同一轮看到原因改对重交；没问题返回空串。"""
    row = engine.log.get("rounds", token) if isinstance(token, str) else None
    if not row or advance is None:
        return ""
    item = engine.log.get("items", row["item"])
    if not item or item["stage"] != row["stage"]:
        return ""
    try:
        target = engine.item_target(item)
        stages = engine.plugins.get("flow", target["plugin"]).stages(target)
        validate_stages(stages)
    except Exception:
        return ""  # 读不到阶段时留给收轮时的关口判定。
    return advance_problem(item, advance, stages)


def failure_reason(row, receipt):
    """没跑成的原因；这一轮自己交的回执被拒过，具体字段原因一并带上（代交的摘要里常没有）。"""
    if receipt.outcome != "failed":
        return None
    rejected = row.get("receipt_rejected")
    if rejected and rejected not in receipt.summary:
        return f"{receipt.summary}；这一轮交的回执被拒过：{rejected}"
    return receipt.summary


def receipt_rejected(engine, token, exc, *, data=None, ask_only=False):
    row = engine.log.get("rounds", token) if isinstance(token, str) else None
    if not row or row.get("receipt") is not None or row["status"] not in ("starting", "running"):
        return 0
    with engine.log.transaction():
        rejected = [*row.get("question_rejections", []), {"receipt": data, "reason": str(exc)}] if ask_only else []
        engine.fact("round_changed", {"token": token, "receipt_rejected": str(exc),
                  "question_rejections": rejected}, row["item"])
        return len(rejected)


def sources(engine):
    for target, rule in engine.rules.data["targets"].items():
        if engine.control(target).get("broken"):
            continue
        # 先结算已经开始的完成钩子，再读新变化，避免别名尚未绑定时另开活。
        if any(r["target"]["name"] == target and r["status"] == "finishing"
               and engine.log.get("effects", f'after:{r["token"]}') for r in engine.active()):
            continue
        if any(i["target"] == target and i.get("program")
               and engine.log.get("effects", f"after:{i['program']['token']}") for i in engine.log.program_items()):
            continue
        for name in rule.get("sources", []):
            key = f"{target}:{name}"
            cursor = (engine.log.get("cursors", key) or {}).get("cursor")
            if not engine.retry_due(f"source:{key}"):
                continue
            try:
                # 身份只认交付对象：游标每次都可能变，带进身份就成了一出结果马上再拉；拉的间隔由新鲜期管。
                batch = engine.call("source", name, "poll", engine.rules.target(target), cursor, identity=[target])
                if not batch.normalized and callable(getattr(engine.plugins.get('source', name), 'normalize', None)):
                    batch = engine.call('source', name, 'normalize', batch, engine.rules.target(target))
                with engine.log.transaction():
                    for event in batch.events:
                        if event.target != target:
                            raise ValueError("来源不能跨交付对象写入")
                        engine.ingest(event)
                    if batch.cursor != cursor:
                        engine.fact("cursor", {"id": key, "cursor": batch.cursor})
                engine.retry_clear(f"source:{key}")
            except Pending:
                continue
            except Retryable as exc:
                engine.source_retry(target, name, exc)
            except Exception as exc:
                engine.broken(target, "source", name, exc)


def source_retry(engine, target, name, exc):
    """只读来源按持续时长退避；其他端口仍沿用各自的失败处理。"""
    key = f"retry:source:{target}:{name}"
    with engine.log.transaction():
        state = engine.control(key)
        now = engine.clock()
        since = state.get("since") if state.get("count") else None
        since = datetime.fromisoformat(since) if since else now
        count = state.get("count", 0) + 1
        elapsed = (now - since).total_seconds()
        delay = (60, 120, 300, 600)[min(count - 1, 3)]
        at = stamp(min(now + timedelta(seconds=delay), since + timedelta(minutes=30))) if elapsed < 1800 else None
        engine.fact("plugin_retry", {"target": target, "port": "source", "name": name,
                  "count": count, "error": str(exc), "since": stamp(since), "elapsed_seconds": elapsed, "at": at})
        engine.fact("control", {"id": key, "count": count, "since": stamp(since), "at": at})
        if elapsed >= 1800:
            engine.broken(target, "source", name, Retryable(f"来源连续失败 {elapsed:.0f} 秒；最后错误：{exc}"))

