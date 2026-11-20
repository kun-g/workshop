"""一轮结算：查结果、失败、收尾、授权推进。"""
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from .gates import decide
from .models import Event, Receipt, RETIRED_FIELDS, STAGES, Started, due, stamp
from .ports import Unavailable, Retryable
from .models import After, Evidence, RoundUsage
from .jobs import Pending
from . import ladder


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


POLL_RETRIES = 3


def poll_retry(engine, row, exc):
    """刚起来时执行者常报一次看不清；同一轮连续 3 次才记失败标记。"""
    count = row.get("poll_retries", 0) + 1
    if count <= POLL_RETRIES:
        engine.fact("round_changed", {"token": row["token"], "poll_retries": count}, row["item"])
    if count >= POLL_RETRIES:
        engine.delivery_error(f"poll:{row['token']}", exc, row["item"])


def poll_ok(engine, row):
    if row.get("poll_retries"):
        engine.fact("round_changed", {"token": row["token"], "poll_retries": 0}, row["item"])
    engine.delivery_ok(f"poll:{row['token']}")


def clear_polls(engine):
    """没在跑的轮（交了回执、判了失败、结束了）留下的轮询失败标记一并清掉。"""
    prefix = "delivery:poll:"
    for control in engine.log.all("controls", where=("failed", True)):
        if not control["id"].startswith(prefix):
            continue
        row = engine.log.get("rounds", control["id"][len(prefix):])
        if not row or row["status"] not in ("starting", "running") or row.get("receipt") is not None:
            engine.delivery_ok(control["id"][len("delivery:"):])


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
            poll_ok(engine, row)
            if state == "exited":
                reason = "执行者已退出但没有交回执"
        except Pending:
            return
        except Retryable as exc:
            poll_retry(engine, row, exc)
            return
    if reason:
        # poll 期间可能刚交回执；自动失败与真实回执由同一个写事务仲裁。
        engine.automatic_failure(row["token"], reason)


def required_reviews(engine, item, evidence, target):
    matched = any(path.startswith(prefix) for path in evidence.paths for prefix in target.get("two_reviewers", []))
    return max(item.get("review_required", 1), evidence.reviewers, 2 if matched else 1)


def failure_changes(engine, item, row, takeover=None):
    """takeover 是事务之前找好的阶梯接手者（ladder.find 的结果）；到上限且有接手者时换人，不记卡住。"""
    failures = item["failures"] + 1
    episode = item.get("failure_episode") if item["failures"] else row["token"]
    changes = {"failures": failures, "failure_episode": episode}
    if failures >= ladder.FAILURE_LIMIT and takeover:
        changes.update(ladder.handover(engine, item, takeover, "failures", f"连续 {failures} 次没跑成"))
    elif failures >= ladder.FAILURE_LIMIT:
        changes.update(state="卡住", next="项目经理处理失败原因后恢复", blocked_by="连续 10 次没跑成",
                       leave={"kind": "等你", "about": "连续失败"})
    else:
        at = stamp(engine.clock() + timedelta(seconds=min(300 * 2 ** (failures - 1), 3600)))
        engine.timer(item, f"retry:{row['token']}", at, "重试这一轮")
        changes.update(state="定时", next=at, retry_at=at,
                       leave={"kind": "工序", "owner": "车间", "at": at, "about": "重试"})
    return changes


def gate_count(item, row, reason):
    """这一次没过关口之后的签名和连续次数。"""
    reason = reason or "请补充阶段结果与下一步"
    signature = f'{item["stage"]}:{row["receipt"].get("advance")}:{reason}'
    return reason, signature, item.get("gate_count", 0) + 1 if item.get("gate_signature") == signature else 1


def gate_retry(engine, item, row, reason, takeover=None):
    """同一关口连续没过：起点执行者 3 次、阶梯接手者 2 次到上限；有接手者（事务之前找好）就换人，否则卡住。"""
    reason, signature, count = gate_count(item, row, reason)
    changes = dict(gate_signature=signature, gate_count=count, gate_reason=reason,
                   retry_executor=row["executor"], state="空闲", next=reason,
                   leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": reason})
    engine.fact("gate_blocked", {"gate_reason": reason}, item["id"])
    if count >= ladder.gate_limit(item) and takeover:
        changes.update(ladder.handover(engine, item, takeover, "gate", reason))
    elif count >= ladder.gate_limit(item):
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
        # 已收下的回执：旧版本存下的 ask、notice 没有去处，去掉再核对。
        receipt = Receipt.parse({k: v for k, v in row["receipt"].items() if k not in RETIRED_FIELDS}, received_at)
        target = engine.item_target(item)
        port, name = "flow", target["plugin"]
        evidence = (Evidence(**row["assessment"]) if row.get("assessment") else
                    Evidence() if row.get("automatic_failure") else
                    engine.call(port, name, "assess", item, receipt, target,
                              identity=[item["id"], item["stage"], token, row["receipt"]], item=item["id"]))
        evidence.validate()
        if evidence.superseded:
            receipt = Receipt("nothing", receipt.summary)
        elif (evidence.handoff and not evidence.blocked and receipt.outcome != "failed"
              and (program or not (receipt.later or receipt.leave))):
            # 流程按外部状态判交接：不看回执自报的推进，没交接到就按本阶段没做完重来。
            # 程序步在最后一段自带「已收尾」登记，交接走了就不算收尾，去掉它。
            receipt = replace(receipt, outcome="done", advance=evidence.handoff,
                              **({"leave": None} if program else {}))
        stages = engine.call(port, name, "stages", target)
        decision = decide(item, receipt, evidence, stages, engine.rules)
        if program:
            row = {**row, "assessment": asdict(evidence)}
            engine.change(item, program=row)
        else:
            engine.fact("round_changed", {"token": token, "assessment": asdict(evidence)}, item["id"])
        required = engine.required_reviews(item, evidence, target)
        reviews = list(item.get("reviews", []))
        reviewing = not program and item["stage"] == "审查" and evidence.passed and receipt.outcome == "done"
        if reviewing and row["config"]["family"] not in reviews:
            reviews.append(row["config"]["family"])
        another_review = reviewing and len(reviews) < required
        rejected = item["stage"] == "验收" and not evidence.passed and not evidence.blocked and not evidence.superseded and not row.get("automatic_failure")
        # 「等你」只看有效等待引用：流程没登记等待的等你不成立，交项目经理。
        wait_ready = receipt.leave and receipt.leave["kind"] == "等你" and not engine.waiting(item)
        missing_leave = decision.complete and not (receipt.leave or receipt.later)
        advancing = (receipt.outcome == "done" and decision.allowed and not missing_leave
                     and not engine.waiting(item) and not wait_ready and not another_review and not rejected and not evidence.blocked
                     and not evidence.awaits_update)
        after_result = After()
        rechecking = (bool(evidence.recheck_after) and receipt.outcome == "nothing" and not receipt.advance
                      and not receipt.later and not receipt.leave and not engine.waiting(item))
        if advancing or evidence.pause:
            key = f"after:{token}"
            after_result = engine.after({**item, "result": receipt.json()}, row["stage"], key, target)
        # 这一次会不会到上限，在事务之前判断；会的话先问阶梯上面有没有可用的接手者（可用性检查可能异步）。
        takeover = None
        if evidence.unable:
            # 流程判这个执行者做不下来了：同样先问阶梯；决定好了才让流程发外部消息（换人还是卡住，措辞不同）。
            if ladder.levels(engine.rules.data) and ladder.writing(evidence.handoff):
                takeover = ladder.find(engine, item, item.get("executor"))
            if callable(getattr(engine.plugins.get(port, name), "unable_decided", None)):
                engine.call(port, name, "unable_decided", item, evidence, target, takeover,
                            identity=[item["id"], "unable", token, takeover and takeover["to"]], item=item["id"])
        elif not program and ladder.levels(engine.rules.data) and ladder.writing(row["stage"]):
            gate_reason = "停下必须登记工序、送回或等你" if missing_leave else decision.reason
            if ((receipt.outcome == "failed" and item["failures"] + 1 >= ladder.FAILURE_LIMIT)
                    or gate_count(item, row, gate_reason)[2] >= ladder.gate_limit(item)):
                takeover = ladder.find(engine, item, item.get("executor") or row["executor"])
        with engine.log.transaction():
            item = engine.log.get("items", item["id"])
            # 本轮结果写完整原因：交过被拒的回执时，具体字段和合法取值一并记下（代交的摘要里常没有）。
            summary = engine.failure_reason(row, receipt) or receipt.summary
            engine.fact("program_result" if program else "round_result", {"token": token, "stage": row["stage"], "outcome": receipt.outcome,
                      "summary": summary, "evidence": receipt.evidence, "reason": evidence.reason},
                      item["id"], source="result", ext_id=token)
            if not program and receipt.outcome != "failed" and row.get("batch"):
                # 核心 R33：这一轮没失败才把它带上的事件标成已处理；失败按同一批重跑。
                engine.fact("batch_done", {"token": token, "seqs": row["batch"]}, item["id"])
            for later in receipt.later:
                engine.timer(item, f'{token}:{later["key"]}', later["at"], later["about"], base=received_at)
            # 最后一个程序阶段的 leave 是自动补的收尾登记，不是后续工序。
            # 保留登记供回查，但不建立即定时重新唤醒已完成的活。
            if (receipt.leave and receipt.leave["kind"] in ("工序", "送回")
                    and not (program and decision.complete)):
                leave = receipt.leave
                # 到点车间接着做（介入点 R10）：送回不再默认找 Kun。
                engine.timer(item, f"leave:{token}", leave["at"], leave["about"], leave["kind"], leave.get("owner", "车间"), base=received_at)
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
                changes.update(state="空闲", next=evidence.reason, approved=[], pending_advance=None,
                               pending_episode=item.get("pending_episode", 0) + 1,
                               after_wait=None, gate_count=0, blocked_by="", serial_hold=False)
                converge = (item.get("relations") or {}).get("converge")
                if converge and STAGES.index(item["stage"]) >= STAGES.index(converge):
                    changes["stage"] = converge
            elif evidence.unable and takeover:
                # 换人：接手者从流程指的阶段接着做；和通过关口退回执行一样清掉审查记录。
                changes.update(ladder.handover(engine, item, takeover, "returns", evidence.blocked),
                               stage=evidence.handoff, approved=[], reviews=[], review_required=1,
                               after_pending_count=0, pending_episode=item.get("pending_episode", 0) + 1)
            elif evidence.blocked:
                changes.update(state="卡住", blocked_by=evidence.blocked, next=evidence.next,
                               leave=engine.blocked_leave(evidence.next))
            elif evidence.awaits_update:
                # 等来源补资料：不派轮、不发卡，来源有新事件时视图把活放回空闲重核。
                changes.update(state="等资料", next=evidence.reason, blocked_by="", gate_reason=evidence.reason,
                               gate_count=0, leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()),
                                                    "about": evidence.reason})
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
                    {"key": key, "receipt": receipt.json(), "to": row["stage"] if evidence.pause else decision.stage,
                     "complete": False if evidence.pause else decision.complete}))
                if after_result.rerun:
                    changes.update(reviews=item.get("reviews", []), retry_executor=row["executor"])
            elif rejected:
                changes.update(stage="执行", state="空闲", next="验收不过，退回执行", approved=[], reviews=[],
                               after_pending_count=0, pending_episode=item.get("pending_episode", 0) + 1,
                               review_required=1, pending_advance=None, gate_count=0,
                               leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": "修正验收问题"})
            elif receipt.outcome == "failed":
                changes.update(engine.failure_changes(item, row, takeover))
            elif wait_ready:
                changes.update(state="卡住", wait_for_ready=True, next="项目经理处理阻塞；要 Kun 取舍时按流程登记等待",
                               blocked_by=receipt.leave["about"], leave=engine.blocked_leave(receipt.leave["about"]))
            elif another_review:
                changes.update(stage="审查", state="空闲", next="另一家审查", gate_count=0,
                               leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": "另一家审查"})
            elif advancing:
                changes.update(after_pending_count=0, pending_episode=item.get("pending_episode", 0) + 1)
                changes.update(stage=decision.stage, state="完成" if decision.complete else "空闲",
                               next="已收尾" if decision.complete else decision.stage, blocked_by="", gate_count=0,
                               leave=receipt.leave or {"kind": "工序", "at": stamp(engine.clock()), "owner": "车间", "about": decision.stage})
                if decision.stage == "执行" and item["stage"] not in ("接单", "说清楚"):
                    changes.update(approved=[], reviews=[], review_required=1)
                engine.fact("gate_passed", {"gate_reason": decision.reason, "gate_stage": decision.stage, "actions": evidence.actions,
                            "action_targets": evidence.action_targets, "authorized_actions": evidence.authorized_actions}, item["id"])
            elif decision.needs_approval and receipt.outcome == "done":
                changes.update(state="卡住", next="流程按原申请号核实授权中心的结果后恢复",
                               blocked_by=decision.reason, leave=engine.blocked_leave(decision.reason))
                engine.fact("gate_blocked", {"gate_reason": decision.reason}, item["id"])
            elif not program and (receipt.later or receipt.leave):
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
                changes.update(engine.gate_retry(item, row, reason, takeover))
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


def drive(engine):
    """结算当前还开着的每一轮；最后清掉已经不在跑的轮留下的轮询失败标记。"""
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
    clear_polls(engine)
