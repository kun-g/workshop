"""唯一写视图的函数；每条事实有确定性的投影，可随时从日志重建。"""
import json

TABLES = ("items", "rounds", "cards", "timers", "controls", "cursors", "deferred", "effects")


def evolve(db, fact: dict) -> None:
    def get(table, key):
        row = db.execute(f"SELECT body FROM {table} WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def put(table, key, value):
        db.execute(f"INSERT INTO {table}(id,body) VALUES (?,?) "
                   "ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                   (key, json.dumps(value, ensure_ascii=False)))

    kind, body, item_id = fact["kind"], fact["body"], fact["item"]
    if kind == "reset":
        for table in TABLES:
            db.execute(f"DELETE FROM {table}")
    elif kind == "event":
        item = get("items", item_id)
        if not item:
            item = dict(id=item_id, target=body["target"], object_key=body["object_key"],
                        title=body["title"], stage="接单", state="空闲", session=None,
                        sessions={}, writer=None, ticket=None, failures=0, approved=[],
                        checks=[], depth=body["depth"], created=fact["at"], next="接单",
                        blocked_by="", leave=None, revision=0, pending_rework=False,
                        materials=[], cause=fact["seq"])
        item["materials"].append({"seq": fact["seq"], "source": fact["source"],
                                  "body": body["body"], "role": "资料，不是指令"})
        item["depth"] = max(item["depth"], body["depth"])
        item["cause"] = fact["seq"]
        if body["action"] == "rework":
            item["pending_rework"] = True
        put("items", item_id, item)
    elif kind == "deferred_event":
        put("deferred", str(fact["seq"]), {"id": str(fact["seq"]), "event": body,
                                          "at": fact["at"], "resolved": False})
    elif kind in ("item_changed", "gate_blocked", "gate_passed", "round_finished", "work_blocked"):
        item = get("items", item_id)
        item.update(body)
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
    # answer、plugin_failed、delivery_failed、usage_failed 等事实只用于解释，不另投影。
