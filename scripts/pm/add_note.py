"""给一件活补一条资料，下一轮 agent 会读到：add_note.py <活> <键> <文件>

做法是建一个立即触发的定时，文件内容当资料。用来补执行回执里的证据（验收拿不到，ws#121）、
或交代情况（例如「授权停在执行中是授权中心给自己发版造成的，不要重新申请」）。
资料里写清是谁补的、出处在哪、验收者怎么自己复核。交付对象被标坏时定时不会触发，先 resume。
缺的机制：加资料命令；验收直接拿到执行回执。
"""
import argparse
from pathlib import Path

from _common import find, open_engine, stamp

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("item")
p.add_argument("key", help="这条资料的键，同一件活里不要重复")
p.add_argument("file", type=Path)
a = p.parse_args()
text = a.file.read_text().strip()
if not text:
    raise SystemExit("资料是空的")
engine, rules = open_engine()
item = find(engine, rules, a.item)
with engine.exclusive(), engine.log.transaction():
    engine.timer(item, a.key, stamp(engine.clock()), "项目经理补充：" + text)
print("已加资料", item["id"], a.key, f"{len(text)} 字")
