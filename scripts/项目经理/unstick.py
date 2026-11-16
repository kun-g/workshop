"""卡住的活放回原阶段重排：unstick.py <活> <原因>

例：unstick.py gc#170 '已补交叉引用的说明，重跑执行'
缺的机制：解卡命令。
"""
import argparse

from _common import back_to_stage, find, open_engine, running

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("item")
p.add_argument("reason")
a = p.parse_args()
engine, rules = open_engine()
item = find(engine, rules, a.item)
if item["state"] != "卡住" or running(engine, item["id"]):
    raise SystemExit(f"没动：{item['id']} 现在是 {item['stage']} {item['state']}，只处理没在跑的卡住的活")
with engine.exclusive(), engine.log.transaction():
    back_to_stage(engine, item, a.reason, failures=0)
item = engine.log.get("items", item["id"])
print(item["id"], item["stage"], item["state"])
