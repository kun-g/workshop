"""一轮结算：查结果、失败、收尾、授权推进。"""
from dataclasses import asdict
from datetime import datetime, timedelta
from .gates import decide
from .models import Event, Receipt, STAGES, Started, due, stamp
from .ports import Unavailable, Retryable
from .cards import frozen_question, question, notice
from .models import After, Evidence, RoundUsage
from .jobs import Pending


def abort(engine, token, reason="人手动结束这一轮"):
    with engine.log.transaction():
        row = engine.log.get("rounds", token)
        if not row or row["status"] == "ended":
            raise ValueError("没有可结束的轮")
        engine.fact("round_changed", {"token": token, "abort_requested": reason}, row["item"])


def automatic_failure(engine, token, reason):
    with engine.log.transaction():
        latest = engine.log.get("rounds", token)
        if not latest or latest.get("receipt") is not None or latest["status"] not in ("starting", "running"):
            return
        if latest.get("receipt_rejected"):
            reason += f'；最后一次交的回执被拒：{latest["receipt_rejected"]}'
        if engine.submit(token, {"outcome": "failed", "summary": reason}):
            engine.fact("round_changed", {"token": token, "automatic_failure": True}, latest["item"])


def check_round(engine, row):
    with engine.log.transaction():
        row = engine.log.get("rounds", row["token"])
        if not row or row.get("receipt") is not None or row["status"] != "running":
            return
    reason = row.get("abort_requested")
    if not reason and row.get("deadline", "9999") <= stamp(engine.clock()):
        reason = "这一轮超过规则期限"
    if not reason:
        try:
            state = engine.call("executor", row["executor"], "poll", row["token"],
                              Started(**row["started"]) if row.get("started") else None, identity=[row["token"]])
            if state not in ("running", "exited"):
                raise ValueError("执行者返回了未知存活状态")
            if state == "exited":
                reason = "执行者已退出但没有交回执"
        except Pending:
            return
        except Retryable as exc:
            engine.delivery_error(f"poll:{row['token']}", exc, row["item"])
            return
    if reason:
        # poll 期间可能刚交回执；自动失败与真实回执由同一个写事务仲裁。
        engine.automatic_failure(row["token"], reason)


def required_reviews(engine, item, evidence, target):
    matched = any(path.startswith(prefix) for path in evidence.paths for prefix in target.get("two_reviewers", []))
    return max(item.get("review_required", 1), evidence.reviewers, 2 if matched else 1)


def failure_changes(engine, item, row):
    failures = item["failures"] + 1
    episode = item.get("failure_episode") if item["failures"] else row["token"]
    changes = {"failures": failures, "failure_episode": episode}
    if failures == 3:
        engine.card(item, f"failing:{episode}", "notice", notice(f'{item["title"]} 连续 3 次没跑成'))
    if failures >= 10:
        changes.update(state="卡住", next="项目经理处理失败原因后恢复", blocked_by="连续 10 次没跑成",
                       leave={"kind": "等你", "about": "连续失败"})
    else:
        at = stamp(engine.clock() + timedelta(seconds=min(300 * 2 ** (failures - 1), 3600)))
        engine.timer(item, f"retry:{row['token']}", at, "重试这一轮")
        changes.update(state="定时", next=at, retry_at=at,
                       leave={"kind": "工序", "owner": "车间", "at": at, "about": "重试"})
    return changes


def gate_retry(engine, item, row, reason):
    reason = reason or "请补充阶段结果与下一步"
    signature = f'{item["stage"]}:{row["receipt"].get("advance")}:{reason}'
    count = item.get("gate_count", 0) + 1 if item.get("gate_signature") == signature else 1
    changes = dict(gate_signature=signature, gate_count=count, gate_reason=reason,
                   retry_executor=row["executor"], state="空闲", next=reason,
                   leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": reason})
    engine.fact("gate_blocked", {"gate_reason": reason}, item["id"])
    if count >= 3:
        changes.update(state="卡住", next="项目经理修正关口失败原因后恢复", blocked_by=reason,
                       leave=engine.blocked_leave("修正关口失败原因后恢复"))
    return changes


def settle_executor(engine, row):
    if row.get("stopped") and row.get("usage_checked"):
        return True
    if not engine.retry_due(f"stop:{row['token']}"):
        return False
    item = engine.log.get("items", row["item"])
    token = row["token"]
    port, name = "executor", row["executor"]
    try:
        engine.plugins.get(port, name)
        started = Started(**row["started"]) if row.get("started") else None
        if not row.get("stopped"):
            engine.call(port, name, "stop", token, started, identity=[token], item=item["id"])
            engine.fact("round_changed", {"token": token, "stopped": True}, item["id"])
        if not row.get("usage_checked"):
            value = None
            try:
                value = engine.call(port, name, "usage", token, started, row["at"], identity=[token, row["at"]], item=item["id"])
                if value is not None:
                    value.validate()
            except Exception as exc:
                value = None
                engine.fact("usage_failed", {"token": token, "error": repr(exc)}, item["id"])
            measured = value or RoundUsage()
            engine.fact("round_changed", {"token": token, "usage": asdict(measured.main),
                      "usage_by_model": measured.entries(), "children_reported": measured.children_reported,
                      "usage_checked": True}, item["id"])
        engine.retry_clear(f"stop:{token}")
        return True
    except Retryable as exc:
        engine.retry_failure(item["target"], port, name, f"stop:{token}", exc)
    except Exception as exc:
        # 关不掉不能假装释放占位；仍按退避重试停止，但立即隔离业务。
        engine.retry_failure(item["target"], port, name, f"stop:{token}", exc)
        engine.broken(item["target"], port, name, exc)
    return False


def finish(engine, row):
    program = row.get("program", False)
    if not program and not engine.settle_executor(row):
        return
    item = engine.log.get("items", row["item"])
    if engine.flow_broken(item["target"], item.get("flow", "")) or not engine.retry_due(f"finish:{row['token']}"):
        return
    engine.cleanup_flows()
    token = row["token"]
    port, name = "flow", engine.item_target(item)["plugin"]
    try:
        received_at = datetime.fromisoformat(row["received"])
        receipt = Receipt.parse(row["receipt"], received_at)
        target = engine.item_target(item)
        port, name = "flow", target["plugin"]
        evidence = (Evidence(blocked="问卡格式不对", next="项目经理修正问卡格式后恢复当前阶段") if row.get("question_blocked") else
                    Evidence(**row["assessment"]) if row.get("assessment") else
                    Evidence() if row.get("automatic_failure") else
                    engine.call(port, name, "assess", item, receipt, target,
                              identity=[item["id"], item["stage"], token, row["receipt"]], item=item["id"]))
        evidence.validate()
        if evidence.superseded:
            receipt = Receipt("nothing", receipt.summary)
        stages = engine.call(port, name, "stages", target)
        decision = decide(item, receipt, evidence, stages, engine.rules)
        if program:
            row = {**row, "assessment": asdict(evidence)}
            engine.change(item, program=row)
        else:
            engine.fact("round_changed", {"token": token, "assessment": asdict(evidence)}, item["id"])
        required = engine.required_reviews(item, evidence, target)
        reviews = list(item.get("reviews", []))
        reviewing = not program and item["stage"] == "审查" and evidence.passed and receipt.outcome == "done" and not receipt.ask
        if reviewing and row["config"]["family"] not in reviews:
            reviews.append(row["config"]["family"])
        another_review = reviewing and len(reviews) < required
        rejected = item["stage"] == "验收" and not evidence.passed and not evidence.blocked and not evidence.superseded and not row.get("automatic_failure")
        wait_ready = receipt.leave and receipt.leave["kind"] == "等你" and not receipt.ask and not engine.answerable(item)
        missing_leave = decision.complete and not (receipt.leave or receipt.ask or receipt.later)
        advancing = (receipt.outcome == "done" and decision.allowed and not missing_leave
                     and not receipt.ask and not engine.waiting(item) and not wait_ready and not another_review and not rejected and not evidence.blocked)
        after_result = After()
        rechecking = (bool(evidence.recheck_after) and receipt.outcome == "nothing" and not receipt.advance
                      and not receipt.ask and not receipt.later and not receipt.leave and not engine.waiting(item))
        if advancing:
            key = f"after:{token}"
            after_result = engine.after({**item, "result": receipt.json()}, row["stage"], key, target)
        with engine.log.transaction():
            item = engine.log.get("items", item["id"])
            # 本轮结果写完整原因：交过被拒的回执时，具体字段和合法取值一并记下（代交的摘要里常没有）。
            summary = engine.failure_reason(row, receipt) or receipt.summary
            engine.fact("program_result" if program else "round_result", {"token": token, "stage": row["stage"], "outcome": receipt.outcome,
                      "summary": summary, "evidence": receipt.evidence, "reason": evidence.reason},
                      item["id"], source="result", ext_id=token)
            for ask in receipt.ask:
                engine.card(item, f'{token}:{ask["key"]}', "ask", ask)
            completed = advancing and (decision.complete or (
                bool(evidence.done_key) and STAGES.index(decision.stage) > STAGES.index(row["stage"])))
            engine.receipt_notices(item, row["stage"], token, receipt, evidence, completed)
            for later in receipt.later:
                engine.timer(item, f'{token}:{later["key"]}', later["at"], later["about"], base=received_at)
            # 最后一个程序阶段的 leave 是自动补的收尾登记，不是后续工序。
            # 保留登记供回查，但不建立即定时重新唤醒已完成的活。
            if (receipt.leave and receipt.leave["kind"] in ("工序", "送回")
                    and not (program and decision.complete)):
                leave = receipt.leave
                engine.timer(item, f"leave:{token}", leave["at"], leave["about"], leave["kind"], leave.get("owner", "Kun"), base=received_at)
            changes = dict(failures=0, round=None, retry_at=None, cause=max(item["cause"], row["receipt_seq"]),
                           retry_executor=None, review_required=required, reviews=reviews,
                           # 没跑成的原因交给流程写进下一轮说明；跑成了就清掉。
                           last_failure=summary if receipt.outcome == "failed" else None)
            if evidence.checks:
                changes["checks"] = evidence.checks
            if evidence.paths:
                # 说清楚时登记的预计路径和之后实际改动的并集，串行路径排队按它判。
                changes["paths"] = sorted(set(item.get("paths", [])) | set(evidence.paths))
            if evidence.superseded:
                engine.cancel_questions(item)
                changes.update(state="空闲", next=evidence.reason, approved=[], pending_advance=None,
                               pending_episode=item.get("pending_episode", 0) + 1,
                               after_wait=None, gate_count=0, blocked_by="", serial_hold=False)
                if item.get("aggregate") and (item.get("relations") or {}).get("converge"):
                    changes["stage"] = item["relations"]["converge"]
            elif evidence.blocked:
                changes.update(state="卡住", blocked_by=evidence.blocked, next=evidence.next,
                               leave=engine.blocked_leave(evidence.next))
            elif rechecking:
                # 新的一段等待，沿用完成钩子的定时、退避与恢复；不能推进到下一阶段。
                changes.update(engine.pending_changes({**item, "after_pending_count": 0},
                    After(pending=True, reason=evidence.reason, next=evidence.next,
                          recheck_after=evidence.recheck_after), target,
                    {"key": f"after:{token}", "receipt": receipt.json(), "to": row["stage"], "complete": False}))
                changes["blocked_by"] = ""
            elif after_result.blocked:
                changes.update(state="卡住", blocked_by=after_result.blocked, next=after_result.next,
                               after_wait=None, leave=engine.blocked_leave(after_result.next))
            elif after_result.pending:
                changes.update(engine.pending_changes(item, after_result, target,
                    {"key": key, "receipt": receipt.json(), "to": decision.stage, "complete": decision.complete}))
                if after_result.rerun:
                    changes.update(reviews=item.get("reviews", []), retry_executor=row["executor"])
            elif rejected:
                changes.update(stage="执行", state="空闲", next="验收不过，退回执行", approved=[], reviews=[],
                               after_pending_count=0, pending_episode=item.get("pending_episode", 0) + 1,
                               review_required=1, pending_advance=None, gate_count=0,
                               leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": "修正验收问题"})
                if item.get("aggregate"):
                    about = "总单验收未通过；修复子单后放行总单，重新固定结果验收"
                    changes.update(state="卡住", blocked_by=about, next=about, wait_for_ready=True,
                                   leave=engine.blocked_leave(about))
            elif receipt.outcome == "failed":
                changes.update(engine.failure_changes(item, row))
            elif wait_ready:
                changes.update(state="卡住", wait_for_ready=True, next="项目经理处理阻塞；需要判断时提交问卡",
                               blocked_by=receipt.leave["about"], leave=engine.blocked_leave(receipt.leave["about"]))
            elif another_review:
                changes.update(stage="审查", state="空闲", next="另一家审查", gate_count=0,
                               leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": "另一家审查"})
            elif advancing:
                changes.update(after_pending_count=0, pending_episode=item.get("pending_episode", 0) + 1)
                changes.update(stage=decision.stage, state="完成" if decision.complete else "空闲",
                               next="已收尾" if decision.complete else decision.stage, blocked_by="", gate_count=0,
                               leave=receipt.leave or {"kind": "工序", "at": stamp(engine.clock()), "owner": "车间", "about": decision.stage})
                if decision.stage == "执行" and item["stage"] != "说清楚":
                    changes.update(approved=[], reviews=[], review_required=1)
                engine.fact("gate_passed", {"gate_reason": decision.reason}, item["id"])
            elif (decision.needs_approval or decision.frozen) and receipt.outcome == "done" and not receipt.ask:
                payload = (frozen_question(item, decision.frozen, decision.needs_approval) if decision.frozen
                           else question(decision.reason, approval=True))
                card_id = engine.card(item, f"approval:{token}", "ask", payload, actions=list(decision.needs_approval),
                                    frozen_files=list(decision.frozen), core_approval=True)
                changes.update(state="等你", next=decision.reason, blocked_by="Kun", leave={"kind": "等你", "about": decision.reason},
                               pending_advance={"token": token, "card": card_id, "from": item["stage"], "to": receipt.advance,
                                                "evidence": asdict(evidence), "receipt": row["receipt"], "stages": list(stages)})
                engine.fact("gate_blocked", {"gate_reason": decision.reason}, item["id"])
            elif not program and (receipt.ask or receipt.later or receipt.leave):
                leave = receipt.leave
                if not leave and receipt.later:
                    first = receipt.later[0]
                    leave = {"kind": "工序", "owner": "车间", "at": due(first["at"], received_at), "about": first["about"]}
                changes.update(state="定时", next="等约好的时间", leave=leave)
            elif receipt.outcome == "nothing":
                at = stamp(engine.clock() + timedelta(minutes=5))
                engine.timer(item, f"nothing:{token}", at, "没有新结果，稍后再看")
                changes.update(state="定时", next=at, leave={"kind": "工序", "owner": "车间", "at": at, "about": "稍后再看"})
            else:
                reason = "停下必须登记工序、送回或等你" if missing_leave else decision.reason
                changes.update(engine.gate_retry(item, row, reason))
            if engine.waiting(item) and changes["state"] != "卡住":
                changes.update(state="等你", next="等回答", blocked_by="Kun", leave={"kind": "等你", "about": "等回答"})
            elif receipt.later and changes["state"] == "空闲":
                changes.update(state="定时", next="等约好的时间")
            if program:
                changes["program"] = None
            else:
                engine.fact("round_changed", {"token": token, "status": "ended", "ended": stamp(engine.clock()),
                           "outcome": receipt.outcome, "summary": summary,
                           "passed": evidence.passed if row["stage"] in ("审查", "验收") and receipt.outcome != "nothing" and not row.get("automatic_failure") else None}, item["id"])
            if item.get("plugin_blocked") and engine.flow_broken(item["target"], item.get("flow", "")):
                changes["before_plugin_failure"] = {k: changes.get(k, item.get(k)) for k in ("state", "next", "blocked_by", "leave")}
                changes.update(state="卡住", next="修复插件后继续", blocked_by=item["blocked_by"])
            engine.change(item, "item_changed" if program else "round_finished", **changes)
            for index, data in enumerate(receipt.events):
                engine.ingest(Event(**{**data, "source": f"round:{token}", "ext_id": str(index)}), round_token=token)
        engine.retry_clear(f"finish:{token}")
    except Retryable as exc:
        engine.retry_failure(item["target"], port, name, f"finish:{token}", exc)
    except Exception as exc:
        engine.broken(item["target"], port, name, exc)


def complete_approval(engine, item):
    pending = item.get("pending_advance")
    if not pending or engine.waiting(item) or item["state"] != "空闲":
        return
    receipt = Receipt.parse(pending["receipt"], engine.clock())
    evidence = Evidence(**pending["evidence"])
    decision = decide(item, receipt, evidence, pending["stages"], engine.rules)
    if not decision.allowed:
        with engine.log.transaction():
            card = engine.log.get("cards", pending["card"])
            label = card.get("choice_label", card.get("answer", "未批准"))
            reason = f"Kun 没批准，选择了：{label}（{card.get('answer', '未知')}）"
            engine.fact("core_feedback", {"body": reason, "card": card["id"], "choice": card.get("answer")}, item["id"])
            engine.change(item, stage="执行", state="空闲", next=reason, pending_advance=None, reviews=[],
                        review_required=1, approved=[], retry_executor=None, gate_count=0, gate_reason=reason,
                        leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": "按拒绝意见修改"})
            if (item.get("relations") or {}).get("members"):
                engine.change(item, state="卡住", blocked_by=reason, wait_for_ready=True,
                              next="按拒绝意见修改后放行", leave=engine.blocked_leave(reason))
        return
    target = engine.item_target(item)
    try:
        key = f"after:{pending['token']}"
        with engine.log.transaction():
            latest = engine.log.get("items", item["id"])
            if latest["pending_rework"] or latest["stage"] != pending["from"]:
                return
            if not engine.log.get("effects", key):
                engine.fact("effect", {"id": key, "done": False})
        result = engine.after({**item, "result": receipt.json()}, pending["from"], key, target)
        with engine.log.transaction():
            latest = engine.log.get("items", item["id"])
            if latest["pending_rework"]:
                return
            changes = (engine.pending_changes(latest, result, target, {"key": key, "receipt": receipt.json(), "to": decision.stage, "complete": decision.complete}) if result.pending else
                       dict(state="空闲", next=decision.stage, gate_reason="已按原证据授权推进",
                            after_pending_count=0, pending_episode=latest.get("pending_episode", 0) + 1))
            engine.change(latest, "gate_passed", stage=pending["from"] if result.pending else decision.stage,
                        pending_advance=None, gate_count=0, **changes)
    except Exception as exc:
        engine.broken(item["target"], "flow", target["plugin"], exc)


def drive(engine):
    """结算当前还开着的每一轮。"""
    for row in engine.active():
        item = engine.log.get("items", row["item"])
        try:
            expired = row.get("deadline", "9999") <= stamp(engine.clock())
            if row["status"] == "starting" and not row.get("started") and not row.get("abort_requested") and not expired:
                if engine.flow_broken(item["target"], item.get("flow", "")):
                    continue
                try:
                    engine.launch(row)
                except Unavailable:
                    # 启动作业晚于预订出结果：放开这一轮，避开最近没法用的执行者另挑。
                    engine.release_unavailable(row)
                    engine.reserve(engine.log.get("items", item["id"]))
                    continue
                row = engine.log.get("rounds", row["token"])
            if row["status"] == "starting" and (row.get("abort_requested") or expired):
                engine.automatic_failure(row["token"], row.get("abort_requested") or "这一轮超过规则期限")
            else:
                engine.check_round(row)
            row = engine.log.get("rounds", row["token"])
            if row["status"] == "finishing":
                engine.finish(row)
        except Pending:
            continue  # 这一轮在等作业出结果，下一拍再看。
        except Retryable as exc:
            engine.start_retry(item, row, "executor", row["executor"], exc)
        except Exception as exc:
            engine.broken(item["target"], "executor", row["executor"], exc)
