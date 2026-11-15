"""定时：记下一个时刻，到点再推进。"""
from .models import Event, due, stamp
from .cards import question, notice


def timer(engine, item, key, at, about, kind="工序", owner="车间", base=None):
    timer_id = f'{item["id"]}:{key}'
    engine.fact("timer_created", dict(id=timer_id, item=item["id"], target=item["target"],
              at=due(at, base or engine.clock()), about=about, kind=kind, owner=owner, fired=False), item["id"],
              source="timer-plan", ext_id=timer_id)


def timers(engine):
    now = stamp(engine.clock())
    for timer in engine.log.all("timers"):
        if timer["fired"] or timer["at"] > now:
            continue
        item = engine.log.get("items", timer["item"])
        if engine.flow_broken(item["target"], item.get("flow", "")):
            continue
        with engine.log.transaction():
            engine.fact("timer_fired", {"id": timer["id"]}, item["id"])
            engine.ingest(Event("timer", timer["id"], item["target"], item["object_key"],
                              item["title"], timer["about"], item["depth"], flow=item.get("flow", "")), directed=True)
            if not engine.control(item["target"]).get("paused"):
                engine.apply_timer(timer, item)


def apply_timer(engine, timer, item):
    if item["state"] == "放弃":
        return  # 放弃前约的工序和送回不再打扰；到点只记事实。
    if timer["kind"] == "送回":
        engine.card(item, timer["id"], "ask", question(timer["about"]))
        if item["state"] not in ("跑着", "卡住"):
            engine.change(item, state="等你", next=timer["about"], blocked_by="Kun",
                        leave={"kind": "等你", "about": timer["about"]})
    elif timer["owner"] != "车间":
        engine.card(item, timer["id"], "notice", notice(f'{timer["owner"]} 的工序到点：{timer["about"]}'))
    elif (item["state"] in ("定时", "完成") and not engine.waiting(item)
          and (not item.get("retry_at") or stamp(engine.clock()) >= item["retry_at"])):
        engine.change(item, state="空闲", next=timer["about"])

