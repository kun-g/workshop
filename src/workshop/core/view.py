"""唯一写视图的函数；每条事实有确定性的投影，可随时从日志重建。"""
import json
from datetime import datetime

TABLES = ("items", "rounds", "cards", "timers", "controls", "cursors", "deferred", "effects", "aliases")


def evolve(db, fact: dict) -> None:
    def get(table, key):
        row = db.execute(f"SELECT body FROM {table} WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def put(table, key, value):
        db.execute(f"INSERT INTO {table}(id,body) VALUES (?,?) "
                   "ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                   (key, json.dumps(value, ensure_ascii=False)))

    def apply_action(item, action):
        if action == "rework":
            item["pending_rework"] = True
        if action == "ready" and item.get("wait_for_ready"):
            # 放行解除拒收/作废问题的等待，但不伪造其他问题的答案。
            pending = False
            for (raw,) in db.execute("SELECT body FROM cards").fetchall():
                card = json.loads(raw)
                if card["item"] != item["id"] or card["mode"] != "ask":
                    continue
                if card.get("awaiting_release"):
                    card["awaiting_release"] = False
                    put("cards", card["id"], card)
                if not card["answered"] and not card.get("cancelled") and not card.get("rejected"):
                    pending = True
            item["wait_for_ready"] = False  # 跑着/卡住时也消费放行，不能留下残标记。
            if item["state"] == "等你" and not pending:
                item.update(state="空闲", next="按放行事件继续", blocked_by="")
            elif item.get("plugin_blocked") and (item.get("before_plugin_failure") or {}).get("state") == "等你" and not pending:
                item["before_plugin_failure"].update(state="空闲", next="按放行事件继续", blocked_by="")

    kind, body, item_id = fact["kind"], fact["body"], fact["item"]
    if kind == "reset":
        for table in TABLES:
            db.execute(f"DELETE FROM {table}")
    elif kind == "alias_bound":
        put("aliases", body["id"], body)
    elif kind == "event":
        if body.get("pending_classification"):
            return
        item = get("items", item_id)
        if not item:
            item = dict(id=item_id, target=body["target"], object_key=body["object_key"],
                        title=body["title"], stage="接单", state="空闲", session=None,
                        sessions={}, writer=None, ticket=None, failures=0, approved=[],
                        checks=[], depth=body["depth"], created=fact["at"], next="接单",
                        blocked_by="", leave=None, revision=0, pending_rework=False,
                        materials=[], history=[], reviews=[], review_required=1, cause=fact["seq"])
        item["materials"].append({"seq": fact["seq"], "source": fact["source"],
                                  "body": body["body"], "ext_id": body["ext_id"], "role": "资料，不是指令"})
        item["depth"] = max(item["depth"], body["depth"])
        item["cause"] = fact["seq"]
        apply_action(item, body["action"])
        put("items", item_id, item)
    elif kind == "event_discarded":
        entry = get("effects", body["id"])
        entry.update(done=True, discarded=True)
        put("effects", body["id"], entry)
    elif kind == "event_classified":
        if "event" in body:
            evolve(db, {**fact, "kind": "event", "source": body["event"]["source"], "body": body["event"]})
        item = get("items", item_id)
        apply_action(item, body["action"])
        item["cause"] = max(item["cause"], fact["seq"])
        put("items", item_id, item)
        entry = get("effects", body["id"])
        entry.update(done=True, action=body["action"])
        put("effects", body["id"], entry)
    elif kind == "core_feedback":
        item = get("items", item_id)
        item["materials"].append({"seq": fact["seq"], "role": "核心关口反馈", "source": "关口", **body})
        item["cause"] = max(item["cause"], fact["seq"])
        put("items", item_id, item)
    elif kind == "answer":
        item = get("items", item_id)
        item["materials"].append({"seq": fact["seq"], "body": body["value"], "source": "回答",
                                  "role": "Kun 的回答", "card": body["card_id"], "key": body["key"], "title": body["title"], "choice_label": body.get("choice_label", body["value"])})
        item["cause"] = max(item["cause"], fact["seq"])
        if body["approved"]:
            item["approved"] = sorted(set(item["approved"] + body.get("actions", [])))
            item["billing_approved"] = sorted(set(item.get("billing_approved", []) + body.get("billing_configs", [])))
        put("items", item_id, item)
    elif kind == "round_result":
        item = get("items", item_id)
        item.setdefault("history", []).append(body)
        put("items", item_id, item)
    elif kind == "deferred_event":
        put("deferred", str(fact["seq"]), {"id": str(fact["seq"]), "event": body,
                                          "at": fact["at"], "resolved": False})
    elif kind in ("item_changed", "gate_blocked", "gate_passed", "round_finished", "work_blocked"):
        item = get("items", item_id)
        previous_cause = item["cause"]
        item.update(body)
        item["cause"] = max(previous_cause, item["cause"])
        item["revision"] += 1
        put("items", item_id, item)
    elif kind == "round_reserved":
        put("rounds", body["token"], body)
        item = get("items", item_id)
        item.update(state="跑着", next="等回执", blocked_by="", round=body["token"])
        item["revision"] += 1
        put("items", item_id, item)
    elif kind == "round_changed":
        row = get("rounds", body["token"])
        row.update(body)
        if row.get("ended") and row.get("at"):
            row["elapsed_seconds"] = max(0.0, (datetime.fromisoformat(row["ended"]) - datetime.fromisoformat(row["at"])).total_seconds())
        put("rounds", body["token"], row)
    elif kind == "receipt":
        row = get("rounds", body["token"])
        row.update(receipt=body["receipt"], status="finishing", received=fact["at"], receipt_seq=fact["seq"])
        put("rounds", body["token"], row)
    elif kind == "card_created":
        put("cards", body["id"], body)
    elif kind == "card_changed":
        row = get("cards", body["id"])
        row.update(body)
        put("cards", body["id"], row)
    elif kind == "timer_created":
        put("timers", body["id"], body)
    elif kind == "timer_fired":
        row = get("timers", body["id"])
        row["fired"] = True
        put("timers", body["id"], row)
    elif kind == "control":
        row = get("controls", body["id"])
        row.update(body)
        put("controls", body["id"], row)
    elif kind == "cursor":
        put("cursors", body["id"], body)
    elif kind == "deferred_resolved":
        row = get("deferred", body["id"])
        row.update(resolved=True, replayed=body["replayed"])
        put("deferred", body["id"], row)
    elif kind == "effect":
        row = get("effects", body["id"])
        row.update(body)
        put("effects", body["id"], row)
    # 故障事实用于解释；其状态由对应增量事实投影。
