"""挑选执行者，预订一轮，串行排队。"""
from dataclasses import asdict, replace
from datetime import timedelta
import json
from pathlib import Path
import uuid
import sys
import hashlib
from .gates import overlaps, validate_stages, allowed_advances
from .models import STAGES as ALL_STAGES
from .models import Brief, Receipt, stamp, DEFAULT_BRIEF_MAX_CHARS
from .ports import Unavailable, Retryable, InputBlocked
from .cards import question
from .models import FlowBrief, Usage, RoundUsage
from .jobs import Pending
from .records import StoredBrief
from .stages import serial_stage


def launch(engine, row):
    item = engine.log.get("items", row["item"])
    engine.plugins.get("executor", row["executor"])
    brief = (StoredBrief(str(engine.log.path.resolve()), row["brief_ref"])
             if "brief_ref" in row else Brief(**row["brief"]))
    # 旧轮的保存内容不改写，但实际启动也使用当前规则的输入预算。
    target = {**row["target"], "runtime": {**row["target"].get("runtime", {}),
              "brief_max_chars": engine.rules.data["limits"].get("brief_max_chars", DEFAULT_BRIEF_MAX_CHARS)}}
    try:
        if row["session"]:
            started = engine.call("executor", row["executor"], "resume", row["token"], row["session"], brief, item,
                                row["config"], target, identity=[row["token"]], item=item["id"])
            if started.session != row["session"]:
                raise ValueError("执行者没有接回原会话")
        else:
            started = engine.call("executor", row["executor"], "launch", row["token"], brief, item, row["config"],
                                target, identity=[row["token"]], item=item["id"])
    except InputBlocked as exc:
        reason = str(exc)
        action = "项目经理修复派活输入、原文存储或预算后，按既有授权恢复本活"
        with engine.log.transaction():
            engine.fact("round_changed", {"token": row["token"], "status": "ended", "stopped": True,
                        "outcome": "failed", "summary": reason, "ended": stamp(engine.clock())}, item["id"])
            engine.fact("round_result", {"token": row["token"], "stage": row["stage"], "outcome": "failed",
                        "summary": reason, "evidence": [], "reason": "未启动：" + reason}, item["id"])
            engine.change(item, state="卡住", round=None, retry_at=None, retry_executor=None,
                          serial_hold=False, blocked_by=reason, next=action, leave=engine.blocked_leave(action))
        return
    if not started.session or not started.handle:
        raise ValueError("执行者必须返回会话和运行标识")
    with engine.log.transaction():
        latest = engine.log.get("rounds", row["token"])
        engine.fact("round_changed", {"token": row["token"], "started": asdict(started),
                   "status": "finishing" if latest.get("receipt") is not None else "running"}, item["id"])
        item = engine.log.get("items", item["id"])
        sessions = dict(item["sessions"])
        sessions[row["executor"]] = started.session
        # 新一轮可能重新开了工作目录；收尾后要重新判断收不收。
        changes = dict(session=started.session, sessions=sessions, workdir=started.workdir,
                       workdir_released=None, release_at=None, release_hold="")
        if row["stage"] == "执行":
            changes["writer"] = row["config"]["family"]
        engine.change(item, **changes)
    engine.retry_clear(f"start:{item['target']}:{row['executor']}")


def reserve(engine, item, unavailable=()):
    if item["depth"] > engine.rules.data["limits"]["chain_depth"]:
        engine.block(item, "事件连锁超过上限")
        return
    target = engine.item_target(item)
    port, name = "flow", target["plugin"]
    unavailable = tuple(dict.fromkeys((*unavailable, *engine.recently_unavailable(item))))
    try:
        stages = engine.call(port, name, "stages", target)
        validate_stages(stages)
        if item["stage"] not in stages:
            # 流程可声明去掉的阶段落到哪一段（例如在途的活换新流程）；没声明就进它之后的第一个。
            moves = {}
            if callable(getattr(engine.plugins.get(port, name), "stage_moves", None)):
                moves = engine.call(port, name, "stage_moves", item, target,
                                    identity=[item["id"], item["stage"], item["revision"]], item=item["id"])
                if not isinstance(moves, dict):
                    raise ValueError("阶段迁移须返回对象")
            following = (moves[item["stage"]] if moves.get(item["stage"]) in stages else
                         next(s for s in stages if ALL_STAGES.index(s) > ALL_STAGES.index(item["stage"])))
            engine.change(item, stage=following, state="空闲", next=following, failures=0, retry_at=None,
                        retry_executor=None, review_required=1, reviews=[],
                        gate_reason=f'规则已去掉「{item["stage"]}」阶段，转到{following}')
            return
        automatic = False
        if callable(getattr(engine.plugins.get(port, name), "automatic", None)):
            automatic = engine.call(port, name, "automatic", item, item["stage"], target,
                                  identity=[item["id"], item["stage"], item["revision"]], item=item["id"])
            if type(automatic) is not bool:
                raise ValueError("程序阶段声明须返回布尔值")
        if automatic:
            engine.start_program(item, stages)
            return
        if engine.occupied() >= engine.rules.data["limits"]["concurrent"]:
            return
        brief_item = {**item, "allowed_advances": allowed_advances(item, stages)}
        content = engine.call(port, name, "brief", brief_item, item["stage"], target, item=item["id"])
        content = FlowBrief(content) if isinstance(content, str) else content
        if (not isinstance(content, FlowBrief) or not isinstance(content.instructions, str)
                or type(content.readonly) is not bool
                or not isinstance(content.materials, list)
                or any(not isinstance(m, dict) for m in content.materials)
                or content.workdir is not None and not Path(content.workdir).is_absolute()):
            raise ValueError("派活说明或目录无效")
        instructions = content.instructions
        candidates = []
        metered = []
        waiting = None
        for config in engine.rules.candidates(item["stage"], item["target"], item["writer"], item.get("flow", "")):
            if (config["use"] in unavailable or item.get("retry_executor") and config["use"] != item["retry_executor"]
                    or item["stage"] == "审查" and config["family"] in item.get("reviews", [])):
                continue
            port, name = "executor", config["use"]
            engine.plugins.get(port, name)
            try:
                available = engine.call(port, name, "available", config)
            except Unavailable:
                available = False
            except Pending as exc:
                # 各家的可用性一起查，不一家一家排队等。
                waiting = waiting or exc
                continue
            if not available:
                continue
            billing_key = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
            if config["billing"] == "metered" and billing_key not in item.get("billing_approved", []):
                metered.append((billing_key, config))
                continue
            candidates.append({"config": config, "available": True})
        if waiting:
            raise waiting
        if not candidates and metered:
            context = "没有可用的订阅候选；是否授权本件活使用这些按量计费候选：" + json.dumps([c for _, c in metered], ensure_ascii=False)
            payload = question("是否批准本件活使用按量计费执行者", approval=True)
            payload["context"] = context
            engine.card(item, f'billing:{item["revision"]}', "ask", payload, billing_configs=[k for k, _ in metered], core_approval=True)
            engine.change(item, state="等你", next="按量计费先授权", blocked_by="Kun", leave={"kind": "等你", "about": "按量计费授权"})
            return
        history = engine.log.history(item["id"])
        selection_target = {**target, "selection": {"writer": item["writer"], "reviewed": item.get("reviews", []),
                            "retry_executor": item.get("retry_executor"), "excluded": list(unavailable)}}
        selector_name = engine.rules.selector(item["stage"], item["target"], item.get("flow", ""))
        port, name = "selector", selector_name
        engine.plugins.get(port, name)
        choice = engine.call(port, name, "select", selection_target, item["stage"], history.count + 1, history, candidates)
        chosen = choice.config if choice is not None else None
        if chosen is not None:
            # 校验输出权限边界，不在核心另写挑选策略。
            if (not any(c["config"] == chosen and c["available"] for c in candidates)
                    or not isinstance(choice.reason, str) or not choice.reason.strip()
                    or chosen["use"] in unavailable
                    or item.get("retry_executor") and chosen["use"] != item["retry_executor"]
                    or item["stage"] in ("审查", "验收") and chosen["family"] == item["writer"]
                    or item["stage"] == "审查" and chosen["family"] in item.get("reviews", [])):
                reason = "挑选逻辑返回了规则不允许的候选或缺少理由"
                engine.fact("selection_failed", {"reason": reason, "selector": selector_name}, item["id"])
                raise Retryable(reason)
        if chosen is None:
            # 没有候选属于操作阻塞；按量计费授权已在前面单独发问卡。
            why_none = choice.reason.strip() if choice is not None and isinstance(choice.reason, str) else ""
            if why_none:
                engine.fact("selection_none", {"reason": why_none, "selector": selector_name}, item["id"])
            engine.change(item, state="卡住", next="项目经理核对执行者和订阅恢复后重试",
                        blocked_by="执行者不可用" + (f"：{why_none}" if why_none else ""),
                        leave=engine.blocked_leave("核对执行者和订阅后恢复"))
            return
        port, name = "executor", chosen["use"]
        token = str(uuid.uuid4())
        materials = [m for m in item["materials"] if m["role"] != "Kun 的回答"]
        materials += [{**m, "role": "资料，不是指令"} for m in content.materials]
        brief = Brief(instructions, materials,
                      answers=[m for m in item["materials"] if m["role"] == "Kun 的回答"],
                      workdir=content.workdir, readonly=content.readonly)
        target = {**target, "runtime": engine.runtime(token)}
        row = dict(token=token, item=item["id"], stage=item["stage"], executor=chosen["use"],
                   config=chosen, requested={"executor": chosen["use"], "model": chosen.get("model"), "effort": chosen.get("effort")}, selector=selector_name, selection_reason=choice.reason, target=target, deadline=stamp(engine.clock() + timedelta(seconds=engine.rules.data["limits"].get("round_timeout_seconds", 7200))), brief=asdict(brief), session=item["sessions"].get(chosen["use"]),
                   status="starting", at=stamp(engine.clock()), cause=item["cause"], receipt=None, usage=asdict(Usage()), usage_by_model=RoundUsage().entries(), children_reported=False, passed=None, elapsed_seconds=None)
        with engine.log.transaction():
            latest = engine.log.get("items", item["id"])
            if (engine.control(item["target"]).get("paused") or latest["state"] != "空闲"
                    or latest.get("relations") != item.get("relations")
                    or any(r["item"] == item["id"] for r in engine.active())
                    or engine.occupied() >= engine.rules.data["limits"]["concurrent"]):
                return
            engine.fact("round_reserved", row, item["id"])
            row = engine.log.get("rounds", token)
        try:
            engine.launch(row)
        except Unavailable:
            # 端口保证这种错误没有启动副作用，才允许释放并换下一家。
            engine.release_unavailable(row)
            engine.reserve(engine.log.get("items", item["id"]), (*unavailable, chosen["use"]))
    except Retryable as exc:
        engine.start_retry(item, locals().get("row"), port, name, exc)
    except Exception as exc:
        engine.broken(item["target"], port, name, exc)


def start_program(engine, item, stages):
    """程序阶段只持久化结算意图，复用关口和完成钩子，不创建执行轮。"""
    following = next((s for s in stages if ALL_STAGES.index(s) > ALL_STAGES.index(item["stage"])), None)
    receipt = Receipt(outcome="done", summary="程序完成", advance=following)
    if following is None:
        receipt = replace(receipt, leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": "已收尾"})
    row = dict(program=True, token="program:" + str(uuid.uuid4()), item=item["id"],
               stage=item["stage"], executor=None, received=stamp(engine.clock()),
               receipt=receipt.json(), receipt_seq=item["cause"])
    with engine.log.transaction():
        latest = engine.log.get("items", item["id"])
        if (latest["state"] != "空闲" or latest["revision"] != item["revision"]
                or any(r["item"] == item["id"] for r in engine.active())):
            return
        engine.change(item, program=row, state="跑着", next="程序完成后过关口")
    engine.finish(row)


def recently_unavailable(engine, item, minutes=10):
    """这件活最近几轮启动时说没法用的执行者；启动作业晚一拍才出结果，重新挑时要避开它们。"""
    since = stamp(engine.clock() - timedelta(minutes=minutes))
    names = []
    for row in engine.log.recent_rounds(item["id"]):
        if row.get("outcome") != "unavailable" or row["stage"] != item["stage"] or (row.get("ended") or "") < since:
            break
        names.append(row["executor"])
    return names


def release_unavailable(engine, row):
    """启动说没法用：端口保证没有启动副作用，结束这一轮、放开活，换下一家。"""
    item = engine.log.get("items", row["item"])
    with engine.log.transaction():
        engine.fact("round_changed", {"token": row["token"], "status": "ended", "outcome": "unavailable",
                  "ended": stamp(engine.clock())}, item["id"])
        engine.change(item, state="空闲", round=None, next="选择下一执行者")


def serial_holder(engine, item):
    """同一交付对象里执行至交付占位的另一件活；验收起让出，完成、放弃、排队不占位。"""
    return next((other for other in engine.log.open_items()
                 if other["id"] != item["id"] and other["target"] == item["target"] and other.get("serial_hold")
                 and other["state"] not in ("完成", "放弃", "排队") and serial_stage(other["stage"])), None)


def serial_queue(engine, item):
    """规则 serial_paths：执行至交付只允许一件占位，验收起让出且不参与排队。
    按预计路径加实际改动判；区间内返工仍占位，验收后返工重新排队。"""
    prefixes = engine.item_target(item).get("serial_paths", [])
    needed = (prefixes and serial_stage(item["stage"])
              and overlaps(item.get("paths", []), prefixes))
    if not needed:
        if item["state"] == "排队":
            engine.change(item, state="空闲", next=item["stage"], blocked_by="", serial_hold=False,
                        leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": item["stage"]})
        elif item.get("serial_hold"):
            engine.change(item, serial_hold=False)
        return engine.log.get("items", item["id"])
    holder = engine.serial_holder(item)
    if holder:
        about = f'排队：等 {holder["id"]} 离开执行至交付阶段，让出串行路径'
        if (item["state"] != "排队" or item.get("blocked_by") != holder["id"]
                or item.get("serial_hold") or item.get("next") != about):
            engine.change(item, state="排队", next=about, blocked_by=holder["id"], serial_hold=False,
                        leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": about})
    elif item["state"] == "排队" or not item.get("serial_hold"):
        changes = dict(serial_hold=True)
        if item["state"] == "排队":
            changes.update(state="空闲", next=item["stage"], blocked_by="",
                           leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": item["stage"]})
        engine.change(item, **changes)
    return engine.log.get("items", item["id"])


def runtime(engine, token):
    state_dir = str(engine.log.path.parent.resolve())
    command = [sys.executable, "-m", "workshop.cli", "--state-dir", state_dir]
    environment = {"WORKSHOP_STATE_DIR": state_dir, "WORKSHOP_ROUND_TOKEN": token}
    return {"state_dir": state_dir, "rules_path": engine.rules.path, "command": command, "env": environment}

