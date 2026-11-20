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


def enqueue(item, seq, body, arrived):
    """视图在事件进到活时调用。旧事实（没有 batchable 字段）不进队列，重放结果与当时一致。
    定时到点是核心自己约的时刻，到点直接把活放回空闲，不作为待处理事件进批次；
    否则失败重试的定时会在在途批次之后留下一条，让下一批混进它。"""
    if "batchable" not in body or body.get("source") == "timer":
        return
    entry = {"seq": seq, "at": arrived, "alone": not body["batchable"], "rework": body.get("action") == "rework",
             # 上一轮还在跑时到的：它结束后不再等窗口，把当时所有待处理事件合成一批。
             "held": item.get("state") == "跑着"}
    if body.get("batch_window_ms") is not None:
        entry["window"] = body["batch_window_ms"]
    item.setdefault("queue", []).append(entry)


def settle(item, consumed=()):
    """去掉已处理的，水位按序号连续推进：队首是已处理或跳过的才往前走。"""
    consumed = set(consumed)
    watermark = item.get("watermark", 0)
    rest, passing = [], True
    for entry in item.get("queue") or []:
        done = entry["seq"] in consumed
        if passing and (done or entry.get("skip")):
            watermark = max(watermark, entry["seq"])
            continue
        passing = False
        if not done:
            rest.append(entry)
    item["queue"], item["watermark"] = rest, watermark
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
        return
    for entry in item.get("queue") or []:
        if entry["seq"] == seq:
            entry["skip"] = True
    settle(item)


def waiting(item):
    return [e for e in item.get("queue") or [] if not e.get("skip")]


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
    # 后面还有不可攒批的或已凑满：这一批不会再变；上一轮跑着时到的，结束就取。
    # 窗口为 0 的（来源要立即取的）也不等。
    if len(run) < len(entries) or any(e["held"] or e.get("window", window) == 0 for e in run):
        return seqs, None
    at = min(datetime.fromisoformat(e["at"]) + timedelta(milliseconds=e.get("window", window)) for e in run)
    return seqs, stamp(at)


def due(item, now, window=WINDOW_MS):
    """空闲活还要等窗口时返回到点时刻，否则 None。"""
    if item.get("state") != "空闲":
        return None
    _, at = plan(item, window)
    return at if at and datetime.fromisoformat(at) > now else None


def later(item, batch):
    """还在队列里、但不在本批的事件序号：它们的正文不进这一轮的派活说明。"""
    return {e["seq"] for e in item.get("queue") or []} - set(batch)
