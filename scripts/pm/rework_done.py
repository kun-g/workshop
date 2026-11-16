"""已完成的活退回返工：rework_done.py <活> <Kun 原话> <会话号>

`workshop recover --mode rework` 不收已完成的活，这里发一条定向返工事件，效果同 Kun 在 issue 里回「不对」。
必须有 Kun 的原话。
缺的机制：recover --mode rework 收已完成的活。
"""
import argparse
import uuid

from workshop.core.models import Event

from _common import find, open_engine

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("item")
p.add_argument("quote")
p.add_argument("session")
a = p.parse_args()
if not a.quote.strip() or not a.session.strip():
    raise SystemExit("原话和会话号都不能空")
engine, rules = open_engine()
item = find(engine, rules, a.item)
event = Event("session", str(uuid.uuid4()), item["target"], item["object_key"], f"会话返工 {item['id']}",
              {"channel": "session", "session": a.session, "quote": a.quote}, action="rework", flow=item.get("flow", ""))
with engine.exclusive(), engine.log.transaction():
    engine.ingest(event, directed=True)
item = engine.log.get("items", item["id"])
print(item["id"], "pending_rework=", item.get("pending_rework"), item["state"])
