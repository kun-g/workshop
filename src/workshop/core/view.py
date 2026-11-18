"""唯一写视图的函数；每条事实有确定性的投影，可随时从日志重建。"""
import json
from datetime import datetime
from .records import compact, compact_history

TABLES = ("items", "rounds", "cards", "timers", "controls", "cursors", "deferred", "effects", "aliases", "waits", "restarts")


def evolve(db, fact: dict) -> None:
    def get(table, key):
        row = db.execute(f"SELECT body FROM {table} WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def put(table, key, value):
        db.execute(f"INSERT INTO {table}(id,body) VALUES (?,?) "
                   "ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                   (key, json.dumps(value, ensure_ascii=False)))

    def apply_action(item, action):
        reference_pending = get("waits", item.get("wait_id", "")).get("state") == "waiting"
        if item.get("state") == "等资料" and not item.get("plugin_blocked"):
            # 停在本阶段等来源补资料的活：来源有任何新事件，下一拍重新核对。
            item.update(state="空闲", next="来源有更新，重新核对", blocked_by="")
        if action == "rework":
            item["pending_rework"] = True
        if action == 'ready' and item['state'] in ('卡住', '等你') and not item.get('plugin_blocked'):
            pending = any((card := json.loads(raw))['item'] == item['id'] and card['mode'] == 'ask'
                          and not card.get('answered') and not card.get('cancelled') and not card.get('rejected')
                          for (raw,) in db.execute('SELECT body FROM cards').fetchall())
            if not pending and not reference_pending:
                if item['stage'] not in ('接单', '说清楚') and not item.get('wait_for_ready'):
                    item['pending_rework'] = True
                item.update(state='空闲', next='按放行事件继续', blocked_by='')
        if action == "ready" and item.get("wait_for_ready"):
            # 放行解除拒收/作废问题的等待，但不伪造其他问题的答案。
            pending = reference_pending
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
            if item["state"] in ("等你", "卡住") and not pending and not item.get("plugin_blocked"):
                item.update(state="空闲", next="按放行事件继续", blocked_by="")
            elif item.get("plugin_blocked") and (item.get("before_plugin_failure") or {}).get("state") in ("等你", "卡住") and not pending:
                item["before_plugin_failure"].update(state="空闲", next="按放行事件继续", blocked_by="")

    def held(item_id, before):
        """放行前只记了事实的资料（上次清掉之后的），放行建活时并进来。"""
        dropped = db.execute("SELECT max(seq) FROM facts WHERE kind='item_dropped' AND item=? AND seq<?",
                             (item_id, before)).fetchone()[0] or 0
        rows = db.execute("SELECT seq, source, ext_id, body FROM facts WHERE kind='event' AND item=? AND seq>? AND seq<? "
                          "ORDER BY seq", (item_id, dropped, before)).fetchall()
        materials = []
        for seq, source, ext_id, raw in rows:
            event = json.loads(raw)
            if event.get("release_gate") and not event.get("pending_classification"):
                materials.append({"seq": seq, "source": source, "body": event["body"],
                                  "ext_id": event["ext_id"], "role": "资料，不是指令"})
        return materials

    kind, body, item_id = fact["kind"], fact["body"], fact["item"]
    if kind == "reset":
        for table in TABLES:
            db.execute(f"DELETE FROM {table}")
    elif kind in ("restart_requested", "restart_completed"):
        row = get("restarts", body["id"])
        row.update(body)
        put("restarts", body["id"], row)
    elif kind == 'wait_bound':
        put('waits', body['id'], body)
        item = get('items', item_id)
        item['wait_id'] = body['id']
        put('items', item_id, item)
    elif kind == 'wait_changed':
        row = get('waits', body['id'])
        row.update(body)
        put('waits', body['id'], row)
    elif kind == 'wait_resumed':
        row = get('waits', body['id'])
        row.update(body, state='resumed')
        put('waits', body['id'], row)
        item = get('items', item_id)
        item.update(wait_id=None, state='空闲', next='原阶段重新核验外部结果', blocked_by='')
        item['revision'] += 1
        put('items', item_id, item)
    elif kind == "alias_bound":
        put("aliases", body["id"], body)
    elif kind == "event":
        if body.get("unmatched") or body.get("pending_classification"):
            return
        item = get("items", item_id)
        if not item:
            if body.get("release_gate") and body["action"] != "ready":
                return  # 来源里没放行过的对象只记事实，不建活。
            item = dict(id=item_id, target=body["target"], object_key=body["object_key"],
                        flow=body.get("flow", ""), flow_default=body.get("flow_default", not body.get("flow")),
                        title=body["title"], stage=body.get("initial_stage", "接单"), state="空闲", session=None,
                        sessions={}, writer=None, ticket=None, failures=0, approved=[],
                        checks=[], depth=body["depth"], created=fact["at"], next=body.get("initial_stage", "接单"),
                        blocked_by="", leave=None, revision=0, pending_rework=False,
                        materials=[], history=[], reviews=[], review_required=1, cause=fact["seq"])
            if body.get("release_gate"):
                item["materials"].extend(held(item_id, fact["seq"]))
        item["materials"].append({"seq": fact["seq"], "source": fact["source"],
                                  "body": body["body"], "ext_id": body["ext_id"], "role": "资料，不是指令"})
        item["depth"] = max(item["depth"], body["depth"])
        item["cause"] = fact["seq"]
        apply_action(item, body["action"])
        put("items", item_id, item)
    elif kind == "item_abandoned":
        # 放弃的事实序号：之前收到的返工、放行即使之后才分类或补放，也不再让它回来。
        item = get("items", item_id)
        item["abandoned_seq"] = fact["seq"]
        put("items", item_id, item)
    elif kind == "item_dropped":
        db.execute("DELETE FROM items WHERE id=?", (item_id,))
    elif kind == "event_discarded":
        entry = get("effects", body["id"])
        entry.update(done=True, discarded=True)
        put("effects", body["id"], entry)
    elif kind == "event_classified":
        if "event" in body:
            evolve(db, {**fact, "kind": "event", "source": body["event"]["source"], "body": body["event"]})
        item = get("items", item_id)
        if item:
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
        put("items", item_id, item)
    elif kind in ("round_result", "program_result"):
        item = get("items", item_id)
        item.setdefault("history", []).append(body)
        first = item.get("history_ref", {}).get("first", fact["seq"])
        put("items", item_id, compact_history(item, first, fact["seq"]))
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
        put("rounds", body["token"], compact(body, fact["seq"]))
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
