"""卡片和通道：发出、读回、作废、在这里回答。"""
from dataclasses import asdict
from datetime import timedelta
from .models import stamp
from .ports import ChannelRejected
from .cards import notice, validate_question, validate_notice
from .models import Answer, ChannelPoll
from .jobs import Pending

# 对账连续不确定、已告警之后，隔多久再对一次（秒）。
RECONCILE_AGAIN = 60

def card(engine, item, key, mode, payload, **extra):
    if mode == "ask":
        validate_question(payload)
    else:
        validate_notice(payload)
    card_id = f'{item["id"]}:{key}'
    if mode == "notice" and engine.rules.data.get("notices") == "hold":
        # 规则 notices: hold（Kun 11-14：通知全是垃圾消息）：通知不发，只记一条供 why 查看；要 Kun 定的仍用问卡。
        if not engine.log.get("cards", card_id):
            engine.fact("notice_held", {"stage": item.get("stage", ""), "key": key, "subject": payload.get("subject", ""),
                      "text": payload.get("text", ""), "reason": "规则 notices: hold，通知只记不发"}, item["id"])
        return card_id
    if not engine.log.get("cards", card_id):
        engine.fact("card_created", dict(id=card_id, item=item["id"], mode=mode,
                  payload=payload, sent=False, answered=False, sending=False, cancelled=False, key=key, **extra), item["id"])
    return card_id


def receipt_notices(engine, item, stage, token, receipt, evidence, closing):
    """执行者回执里的通知只在 Kun 需要知道时发：这一轮留下等你，或这件活完成。

    完成通知按 Evidence.done_key 只发一张，不看执行者用什么 key；其余的不发卡，记 notice_held 供 why 查看。
    """
    waiting = bool(receipt.leave and receipt.leave.get("kind") == "等你" and engine.answerable(item))
    done = f'notice:完成:{evidence.done_key}' if evidence.done_key else "notice:完成"
    for entry in receipt.notice:
        if waiting:
            engine.card(item, f'notice:{entry["key"]}', "notice", entry)
            continue
        if closing and not engine.log.get("cards", f'{item["id"]}:{done}'):
            engine.card(item, done, "notice", entry)
            continue
        reason = (f'这次完成已发过通知（{evidence.done_key or "这件活"}）' if closing
                  else "进度通知不发：只在留下等你或完成时发")
        engine.fact("notice_held", {"token": token, "stage": stage, "key": entry["key"],
                  "subject": entry["subject"], "text": entry["text"], "reason": reason}, item["id"])


def invalid_answer(engine, card, value):
    """审批卡收到非选项回答：通道没坏，记事实并通知 Kun 一次，活继续等原卡。"""
    if card.get("invalid_answer") != value:
        engine.fact("invalid_answer", {"card": card["id"], "value": value, "title": card["payload"]["decision"]}, card["item"])
        engine.fact("card_changed", {"id": card["id"], "invalid_answer": value}, card["item"])
    if card.get("invalid_answer_noticed"):
        return
    item = engine.log.get("items", card["item"])
    name = (item or {}).get("title") or card["item"]
    text = (f"「{name}」的卡片「{card['payload']['decision']}」收到「{value}」，不是卡上的选项，活还在等这张卡。"
            "请在卡片上点按钮重新回答；这条不用回。")
    engine.card({"id": card["item"]}, f"invalid-answer:{card['key']}", "notice", notice(text))
    engine.fact("card_changed", {"id": card["id"], "invalid_answer_noticed": True}, card["item"])


def reconcile_due(engine, card):
    return (engine.control(f"reconcile:{card['id']}").get("at") or "") <= stamp(engine.clock())


def reconcile_uncertain(engine, card):
    """连续 3 次不确定就告警；之后每 RECONCILE_AGAIN 秒再对一次，不每拍起作业把主循环叫醒。"""
    key = f"reconcile:{card['id']}"
    count = engine.control(key).get("count", 0)
    at = stamp(engine.clock() + timedelta(seconds=RECONCILE_AGAIN)) if count + 1 >= 3 else None
    with engine.log.transaction():
        engine.fact("control", {"id": key, "count": min(count + 1, 3), "at": at})
        if count + 1 == 3:
            engine.fact("reconcile_failed", {"card": card["id"], "reason": "连续 3 次无法确定是否已发送"}, card["item"])
            incident = engine.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0]
            engine.fact("card_changed", {"id": card["id"], "reconcile_failed": True}, card["item"])
            # 告警自身若也无法对账，保留状态，不递归制造更多告警。
            if not card.get("reconciliation_alert"):
                engine.card({"id": card["item"]}, f"reconcile:{incident}", "notice",
                          notice(f"卡片 {card['id']} 连续 3 次对账不确定，需要检查通道"), reconciliation_alert=True)


def reconcile_clear(engine, card):
    key = f"reconcile:{card['id']}"
    state = engine.control(key)
    if state.get("count") or state.get("at"):
        engine.fact("control", {"id": key, "count": 0, "at": None})
    if card.get("reconcile_failed"):
        engine.fact("card_changed", {"id": card["id"], "reconcile_failed": False}, card["item"])


def undeliverable(engine, card, reason, *, cancelled=False):
    """已知终态单独落事实；不冒充回答，不再发送或对账。"""
    with engine.log.transaction():
        current = engine.log.get("cards", card["id"])
        if current.get("rejected") or current.get("cancelled"):
            return
        engine.fact("card_cancelled_external" if cancelled else "card_rejected",
                  {"id": card["id"], "reason": reason}, card["item"])
        item = engine.log.get("items", card["item"])
        wait = (card["mode"] == "ask" and item and item["state"] != "完成"
                and (item.get("before_plugin_failure") or {}).get("state") != "完成")
        engine.fact("card_changed", {"id": card["id"], "sending": False,
                  "rejected": not cancelled, "cancelled": cancelled,
                  "cancel_done": cancelled, "reconcile_failed": False,
                  "awaiting_release": bool(wait)}, card["item"])
        engine.reconcile_clear(card)
        engine.delivery_ok(f"read:{card['id']}")
        engine.delivery_error(card["id"], RuntimeError(reason), card["item"])
        if wait:
            pending = item.get("pending_advance")
            if pending and pending.get("card") == card["id"]:
                pending = None
            waiting = dict(state="等你", next="卡片无法继续，需要检查后放行", blocked_by="Kun",
                           leave={"kind": "等你", "about": "卡片无法继续，需要检查后放行"})
            changes = dict(wait_for_ready=True, pending_advance=pending)
            if item["state"] == "卡住":
                saved = item.get("before_plugin_failure")
                if saved and saved.get("state") not in ("跑着", "完成"):
                    changes["before_plugin_failure"] = {**saved, **waiting}
            elif item["state"] != "跑着":
                changes.update(waiting)
            engine.change(item, **changes)
        if not card.get("rejection_alert"):
            engine.card({"id": card["item"]}, f"undeliverable:{card['id']}", "notice",
                      notice(f"车间有一张卡发不出去，原因已记录，请看 workshop why {card['item']}"),
                      rejection_alert=True, rejection_of=card["id"])


def channels(engine):
    name = engine.rules.data["channel"]
    try:
        engine.plugins.get("channel", name)
        for card in engine.log.all("cards"):
            try:
                if card.get("rejected"):
                    continue
                if card.get("cancelled"):
                    if not card.get("cancel_done"):
                        engine.call("channel", name, "cancel", card["id"], card.get("external"), identity=["cancel", card["id"]])
                        engine.fact("card_changed", {"id": card["id"], "cancel_done": True}, card["item"])
                    engine.delivery_ok(f"read:{card['id']}")
                elif not card["sent"]:
                    external = None
                    method = "ask" if card["mode"] == "ask" else "notify"
                    sending = [card["id"]]
                    # 发送作业还在跑或已出结果，就认它，不另去对账，免得把自己的发送算成不确定。
                    if card.get("sending") and not (engine.jobs and engine.jobs.exists("channel", name, method, sending)):
                        if not reconcile_due(engine, card):
                            continue
                        external = engine.call("channel", name, "reconcile", card["id"], card, identity=[card["id"]])
                        if external is None:
                            engine.reconcile_uncertain(card)
                            continue
                        engine.reconcile_clear(card)
                    if external in (None, "absent"):
                        if not card.get("sending"):
                            engine.fact("card_changed", {"id": card["id"], "sending": True}, card["item"])
                        external = engine.call("channel", name, method, card["id"], card["payload"], identity=sending)
                    if not isinstance(external, str) or not external:
                        raise ValueError("通道没有返回外部编号")
                    engine.fact("card_changed", {"id": card["id"], "external": external, "sent": True, "reconcile_failed": False}, card["item"])
                engine.delivery_ok(card["id"])
                if card.get("rejection_of"):
                    engine.delivery_ok(card["rejection_of"])
            except Pending:
                continue
            except ChannelRejected as exc:
                engine.undeliverable(card, str(exc))
            except Exception as exc:
                engine.delivery_error(card["id"], exc, card["item"])
        cards = [c for c in engine.log.all("cards") if c["mode"] == "ask" and c["sent"]
                 and not c["answered"] and not c.get("cancelled")]
        # 只保留仍待回答的卡，避免内存随历史卡片数增长。
        engine.channel_read_tokens = {c["id"]: engine.channel_read_tokens.get(c["id"]) or c.get("read_token")
                                    for c in cards}
        try:
            result = engine.call("channel", name, "poll", cards, identity=["poll"])
        except Pending:
            result = ChannelPoll()
        except Exception as exc:
            # 整批失败也逐卡记，不能只剩一条无归属的通道错误。
            result = ChannelPoll(checked=[c["id"] for c in cards],
                                 errors={c["id"]: str(exc) for c in cards})
            engine.delivery_error(f"channel:{name}", exc)
        else:
            engine.delivery_ok(f"channel:{name}")
        if isinstance(result, ChannelPoll):
            engine.channel_reads(result)
            answers = result.answers
        else:
            answers = result  # 旧通道的回答列表不含读取诊断。
        for answer in answers:
            with engine.log.transaction():
                card = engine.log.get("cards", answer.card_id)
                if not card or card["mode"] != "ask" or not card["sent"] or card["answered"] or card.get("cancelled"):
                    continue
                if answer.cancelled:
                    engine.undeliverable(card, "上游卡片已作废或已清理", cancelled=True)
                    continue
                # 旧版本把非选项回答记成投递失败；收到任何回答都先清掉那条，免得看门程序误报通道故障。
                engine.delivery_ok(f"answer:{card['id']}")
                if card["payload"]["kind"] == "approval" and answer.value not in [c["key"] for c in card["payload"]["choices"]]:
                    engine.invalid_answer(card, answer.value)
                    continue
                engine.record_answer(card, answer)
    except Exception as exc:
        engine.delivery_error(f"channel:{name}", exc)


def channel_reads(engine, result):
    """只累计完成的读取；同一作业结果在新鲜期被重复收取不再计数。"""
    completed = {}
    with engine.log.transaction():
        for cid in result.checked:
            card = engine.log.get("cards", cid)
            if (not card or card["mode"] != "ask" or not card["sent"] or card["answered"]
                    or card.get("cancelled") or cid in completed
                    or (engine.channel_read_tokens.get(cid) or card.get("read_token")) == result.token):
                continue
            error = result.errors.get(cid, "")
            count = card.get("read_failures", 0) + 1 if error else 0
            if count != card.get("read_failures", 0) or error != card.get("read_error", ""):
                engine.fact("card_changed", {"id": cid, "read_failures": count,
                          "read_error": error}, card["item"])
            if error:
                engine.delivery_error(f"read:{cid}", RuntimeError(error), card["item"])
            else:
                engine.delivery_ok(f"read:{cid}")
            completed[cid] = result.token
    # 事务成功才记住结果；诊断没有变化也要去重，不为 token 追加事实。
    engine.channel_read_tokens.update(completed)


def cancel_questions(engine, item):
    for card in engine.log.all("cards"):
        if card["item"] == item["id"] and card.get("awaiting_release"):
            engine.fact("card_changed", {"id": card["id"], "awaiting_release": False}, item["id"])
        if card["item"] == item["id"] and card["mode"] == "ask" and not card["answered"] and not card.get("cancelled") and not card.get("rejected"):
            engine.fact("card_changed", {"id": card["id"], "cancelled": True}, item["id"])


def record_answer(engine, card, answer, provenance=None):
    """记下 Kun 对一张卡的回答。provenance 记回答从哪个渠道来（飞书卡片不写；会话转录写会话号和原话）。"""
    engine.delivery_ok(f"read:{card['id']}")
    approved = answer.value == "yes" if card.get("core_approval") else answer.approved
    choice_label = next((c["label"] for c in card["payload"]["choices"] if c["key"] == answer.value), answer.value)
    # 回答单独追加，由 evolve 在最新资料上追加，不回写旧资料快照。
    engine.fact("answer", {**asdict(answer), "approved": approved, "choice_label": choice_label, "key": card["payload"].get("key", card["key"]),
              "title": card["payload"]["decision"], "actions": card.get("actions", []), "billing_configs": card.get("billing_configs", []),
              "frozen_files": card.get("frozen_files", []), **({"via": provenance} if provenance else {})},
              card["item"], source="answer", ext_id=card["id"])
    engine.fact("card_changed", {"id": card["id"], "answered": True, "answer": answer.value, "choice_label": choice_label,
              "approved": approved, "read_failures": 0, "read_error": ""}, card["item"])
    item = engine.log.get("items", card["item"])
    if card.get("billing_configs") and not approved:
        engine.change(item, state="等你", wait_for_ready=True, next="按量计费未获批准，等待放行", blocked_by="Kun",
                    leave={"kind": "等你", "about": "调整执行者或授权后放行"})
    elif item["state"] == "等你" and not item.get("wait_for_ready") and not engine.waiting(item):
        engine.change(item, state="空闲", next="按回答继续", blocked_by="")
    elif item["state"] == "等你" and not engine.answerable(item):
        engine.change(item, state="等你", next="卡片无法继续，需要检查后放行")


def answer_here(engine, ref, value, quote, session):
    """Kun 在会话里回答一张卡（渠道由 Kun 选）：按卡号或飞书 Q 号找卡，记下会话号和 Kun 的原话，和卡片回答同样生效。"""
    if not quote.strip() or not session.strip():
        raise ValueError("要写 Kun 的原话和会话号")
    with engine.log.transaction():
        cards = [c for c in engine.log.all("cards") if c["mode"] == "ask" and ref in (c["id"], c.get("external"))]
        if len(cards) != 1:
            raise ValueError(f"找不到唯一一张问卡：{ref}")
        card = cards[0]
        if card["answered"] or card.get("cancelled"):
            raise ValueError("这张卡已经答过或作废")
        keys = [c["key"] for c in card["payload"]["choices"]]
        if keys and value not in keys:
            raise ValueError(f"回答须是选项之一：{', '.join(keys)}")
        payload = card["payload"]
        # 批准与否和卡片通道同一判法：只有审批卡答 yes 才算批准。
        approved = payload["kind"] == "approval" and value == "yes" and "yes" in keys
        engine.record_answer(card, Answer(card["id"], value, approved),
                           provenance={"channel": "session", "session": session, "quote": quote})
    return card

