"""同一件活上还没处理的事件攒成一批跑一轮（核心 R33）。

事件进到一件活时，视图在活上记一条待处理（序号、到达时刻、窗口、能不能攒批、到达时有没有轮在跑）。
活空闲要起轮时按 plan 定本批：上一批没跑完（启动前失败、启动后失败）原样重跑；
否则从最小的待处理序号起取，遇到不可攒批的或凑满 20 条就停。
本批在预订一轮时写进轮次（round_reserved.batch），视图记为在途；这一轮结算且没失败，
才写 batch_done 把本批标成已处理。之后到达的事件留在队列，进下一轮。
标记为跳过的事件不进任何批次；水位只按序号连续推进，跳过的也不能把它推过还没处理的事件。
窗口到点由主循环按时间戳判，不阻塞等待。
"""
from datetime import datetime, timedelta

from .models import stamp

WINDOW_MS = 500  # 默认合批窗口，从第一条待处理事件到达算起，后到的不重置。
LIMIT = 20  # 单批上限；凑满不等窗口。


def enqueue(item, seq, body, arrived, order=None):
    """视图在事件进到活时调用。旧事实（没有 batchable 字段）不进队列，重放结果与当时一致。
    定时到点是核心自己约的时刻，到点直接把活放回空闲，不作为待处理事件进批次；
    否则失败重试的定时会在在途批次之后留下一条，让下一批混进它。
    order 是事件进车间时的事实序号：要归件的事件归件有先后，队列仍按进车间的先后排。"""
    if "batchable" not in body or body.get("source") == "timer":
        return
    entry = {"seq": seq, "order": seq if order is None else order, "at": arrived, "alone": not body["batchable"],
             "rework": body.get("action") == "rework",
             # 上一轮还在跑时到的：它结束后不再等窗口，把当时所有待处理事件合成一批。
             "held": item.get("state") == "跑着"}
    if body.get("batch_window_ms") is not None:
        entry["window"] = body["batch_window_ms"]
    queue = item.setdefault("queue", [])
    index = len(queue)
    while index and queue[index - 1].get("order", queue[index - 1]["seq"]) > entry["order"]:
        index -= 1
    queue.insert(index, entry)


def settle(item, consumed=()):
    """标记已处理；水位按挂到活上的事实序号连续推进，不越过任何还没处理的事件。
    队列按进车间先后排，归件有先后时两种顺序可能不一致：已处理、跳过的先留在队列里，
    等序号比它小的都处理完再一起出队。"""
    consumed = set(consumed)
    queue = item.get("queue") or []
    for entry in queue:
        if entry["seq"] in consumed:
            entry["done"] = True
    low = min((e["seq"] for e in queue if not e.get("done") and not e.get("skip")), default=None)
    passed = [e for e in queue if (e.get("done") or e.get("skip")) and (low is None or e["seq"] < low)]
    item["watermark"] = max([item.get("watermark", 0), *(e["seq"] for e in passed)])
    item["queue"] = [e for e in queue if e not in passed]
    if consumed:
        item["inflight"] = [seq for seq in item.get("inflight") or [] if seq not in consumed]


def consume(item, seqs):
    """一轮跑完、没失败：本批标成已处理。留在队列里的返工事件（在途批次之外到的）
    重新挂上返工，它们自己的那一批不会因为在途批次先用掉返工标记而落空。"""
    settle(item, seqs)
    if any(e["rework"] for e in waiting(item)):
        item["pending_rework"] = True


def skip(item, seq):
    """标记为跳过；已定进在途批次的照常处理，不改。"""
    if seq in (item.get("inflight") or []):
        return  # engine.skip_event 已先拒绝；重放旧事实时照样不动在途批次。
    for entry in item.get("queue") or []:
        if entry["seq"] == seq:
            entry["skip"] = True
    settle(item)


def waiting(item):
    return [e for e in item.get("queue") or [] if not e.get("skip") and not e.get("done")]


def plan(item, window=WINDOW_MS):
    """返回 (本批序号, 最早定批时刻)；时刻为 None 表示现在就能定。"""
    inflight = item.get("inflight") or []
    if inflight:
        return list(inflight), None
    entries = waiting(item)
    if not entries:
        return [], None
    if entries[0]["alone"]:
        return [entries[0]["seq"]], None
    run = []
    for entry in entries:
        if entry["alone"] or len(run) == LIMIT:
            break
        run.append(entry)
    seqs = [e["seq"] for e in run]
    # 已凑满或后面还有不可攒批的：这一批不会再变；窗口为 0 的（来源要立即取的）也不等。
    # 上一轮跑着时到的（含进车间在轮中、归件在轮后的），轮结束就取。
    if (len(run) == LIMIT or len(run) < len(entries)
            or any(e["held"] or during(item, e) or e.get("window", window) == 0 for e in run)):
        return seqs, None
    at = min(datetime.fromisoformat(e["at"]) + timedelta(milliseconds=e.get("window", window)) for e in run)
    return seqs, stamp(at)


def during(item, entry):
    span = item.get("round_span")
    if not span:
        return False
    # 按时间比，不按字符串：整秒的时间串不带小数，字典序和先后不一致。
    begin, end, at = (datetime.fromisoformat(value) for value in (*span, entry["at"]))
    return begin <= at <= end


def due(item, now, window=WINDOW_MS):
    """空闲活还要等窗口时返回到点时刻，否则 None。"""
    if item.get("state") != "空闲":
        return None
    _, at = plan(item, window)
    return at if at and datetime.fromisoformat(at) > now else None


def later(item, batch):
    """还在队列里、但不在本批的事件序号：它们的正文不进这一轮的派活说明。"""
    return {e["seq"] for e in item.get("queue") or [] if not e.get("done")} - set(batch)
