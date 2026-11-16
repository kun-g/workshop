"""后台盯梢：盯几件活，有该看的变化、或到了预期时间就退出，把项目经理叫醒。

watch.py --items 'grant-console:(170|171|scheduled_release)' --expect '21:47 批次同步超时，应转卡住' [--by 21:47] [--max 90]

退出条件（任一）：
- 有活进了等你、卡住、完成，或换了阶段；
- 到了 --by 加 --grace 分钟，盯的活还没有上面这些变化（11-15 漏过两次，都是预期的事到点没发生、状态也没变）；
- 超过 --max 分钟。
只读状态库，不起模型。用 Claude Code 的后台命令跑，结束时会通知。
"""
import argparse
import re
import sqlite3
import time
from datetime import datetime, timedelta

from _common import STATE_DIR

WATCHED = ("等你", "卡住", "完成")
p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("--items", required=True, help="活 id 的正则，例如 'grant-console:(170|171)'")
p.add_argument("--expect", required=True, help="在等什么，叫醒时原样打出来")
p.add_argument("--by", help="预期什么时候发生，本地时间 HH:MM；不填就只按 --max 兜底")
p.add_argument("--grace", type=int, default=5, help="预期时间之后再宽限几分钟，默认 5")
p.add_argument("--max", type=int, default=90, help="最长盯几分钟，默认 90")
p.add_argument("--every", type=int, default=60, help="几秒查一次，默认 60")
a = p.parse_args()
pattern = re.compile(a.items)
start = datetime.now()
deadline = start + timedelta(minutes=a.max)
due = None
if a.by:
    h, m = map(int, a.by.split(":"))
    due = start.replace(hour=h, minute=m, second=0, microsecond=0)
    if due < start - timedelta(hours=12):
        due += timedelta(days=1)
    due += timedelta(minutes=a.grace)


def snapshot():
    db = sqlite3.connect(f"file:{STATE_DIR / 'workshop.db'}?mode=ro", uri=True)
    try:
        rows = db.execute("select id, json_extract(body,'$.stage'), json_extract(body,'$.state') from items").fetchall()
    finally:
        db.close()
    return {i: (stage, state) for i, stage, state in rows if pattern.search(i)}


def show(snap):
    return "\n".join(f"  {i} {stage} {state}" for i, (stage, state) in sorted(snap.items())) or "  （没有匹配的活）"


base = snapshot()
print(f"{start:%H:%M} 开始盯：{a.expect}\n{show(base)}", flush=True)
while True:
    time.sleep(a.every)
    now, snap = datetime.now(), snapshot()
    changed = [f"  {i}: {base.get(i, ('新', ''))} → {v}" for i, v in sorted(snap.items())
               if i not in base or v[0] != base[i][0] or (v[1] != base[i][1] and v[1] in WATCHED)]
    if changed:
        print(f"{now:%H:%M} 有变化：\n" + "\n".join(changed), flush=True)
        break
    if due and now >= due:
        print(f"{now:%H:%M} 到了预期时间 {a.by}（宽限 {a.grace} 分钟），还没变化。预期：{a.expect}\n现在：\n{show(snap)}", flush=True)
        break
    if now >= deadline:
        print(f"{now:%H:%M} 盯满 {a.max} 分钟，没变化。\n{show(snap)}", flush=True)
        break
