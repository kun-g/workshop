"""撤掉一件活的问卡，放回原阶段：reask.py <活> <原因>

用在问卡问错了、或已不需要 Kun 回答时（例如 11-15 撤回 gc#171 多余的需求变更卡）。
缺的机制：撤问命令。
"""
import argparse

from _common import back_to_stage, find, open_engine, running

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("item")
p.add_argument("reason")
a = p.parse_args()
engine, rules = open_engine()
item = find(engine, rules, a.item)
if item["state"] != "等你" or running(engine, item["id"]):
    raise SystemExit(f"没动：{item['id']} 现在是 {item['stage']} {item['state']}，只处理没在跑的等你的活")
with engine.exclusive(), engine.log.transaction():
    engine.cancel_questions(item)
    back_to_stage(engine, engine.log.get("items", item["id"]), a.reason)
item = engine.log.get("items", item["id"])
print(item["id"], item["stage"], item["state"])
