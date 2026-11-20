"""介入点发件基线：一行发件算一条，办结时间取自核心事实。"""
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cache
import json
from pathlib import Path
import sqlite3

from .metrics import instant


WINDOW = timedelta(days=7)


@dataclass
class Counts:
    sent: int = 0
    responded: int = 0
    folded: int = 0
    unanswered: int = 0
    pending: int = 0
    indirect: int = 0
    manager_help: int = 0


def messages(log, intervention_db, as_of=None):
    now = instant(as_of) if as_of is not None else datetime.now(timezone.utc)

    @cache
    def completions(item):
        # 两种核心路径：收尾作业更新活，或执行轮直接完成。按活和事实种类走索引。
        rows = log.db.execute("SELECT at FROM facts WHERE item=? AND kind IN ('item_changed','round_finished') "
                              "AND json_extract(body,'$.state')='完成'", (item,))
        return tuple(instant(at) for (at,) in rows)

    groups = defaultdict(Counts)
    uri = Path(intervention_db).expanduser().resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as outbox:
        outbox.row_factory = sqlite3.Row
        rows = outbox.execute("SELECT target,channel,category,point,sent_at,responded_at,status,item,items "
                              "FROM message_outbox WHERE respondable=1 AND status IN ('delivered','folded')")
        for row in rows:
            sent = instant(row['sent_at'])
            if sent > now:
                continue
            deadline = sent + WINDOW
            counts = groups[row['target'], row['channel'], row['category']]
            counts.sent += 1
            counts.folded += row['status'] == 'folded'
            counts.manager_help += row['point'] == 'manager_help'
            if row['responded_at'] and instant(row['responded_at']) <= now:
                counts.responded += 1
            elif now >= deadline:
                counts.unanswered += 1
            else:
                counts.pending += 1

            items = json.loads(row['items'])
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                raise ValueError('message_outbox.items 必须是活号数组')
            members = set(items)
            if row['item']:
                members.add(row['item'])
            cutoff = min(now, deadline)
            resolved_after = min((at for item in members for at in completions(item) if sent < at <= cutoff),
                                 default=None)
            counts.indirect += resolved_after is not None

    total = sum(c.sent for c in groups.values())
    lines = [f'可回应已发 {total} 条（不含授权中心卡片）',
             '回应率按显式回应计算，间接回应单列。',
             '人\t通道\t类别\t发出\t被回应\t回应率\t折叠\t未回应\t待回应\t间接回应\t处理不了推送']
    for group, c in sorted(groups.items()):
        values = (*group, c.sent, c.responded, f'{c.responded}/{c.sent}', c.folded,
                  c.unanswered, c.pending, c.indirect, c.manager_help)
        lines.append('\t'.join(map(str, values)))
    return '\n'.join(lines)
