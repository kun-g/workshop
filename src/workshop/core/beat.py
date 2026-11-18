"""一拍的顺序。

这一拍只做这些：收作业结果、归类外部事件、清一次流程目录、收来源事件、读通道、定时、结算开着的轮、推进每件活、收回目录、再读通道、写心跳。
失败记账和心跳放在一起。
一拍超过 100 毫秒时，等锁和各步耗时追加到状态目录的 tick-slow.jsonl，只留最近 100 条。
"""
import json
import os
import sys
import time

from .jobs import Pending
from .models import stamp
from . import rounds, relations, waits

SLOW_TICK_MS = 100
SLOW_ITEM_MS = 5
SLOW_TICK_KEEP = 100
SLOW_TICK_BYTES = 256 * 1024


class Span:
    """记一步耗时；这一步抛出去也留下时间。"""

    def __init__(self, spans, name):
        self.spans, self.name = spans, name

    def __enter__(self):
        self.began = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.spans[self.name] = round((time.monotonic() - self.began) * 1000, 3)
        return False


def tick(engine, received_at=None):
    began = time.monotonic()
    engine.tick_started = engine.clock()
    engine.wake_ms = round(max(0, began - received_at) * 1000, 3) if received_at is not None else None
    try:
        engine._tick(began)
    except Exception as exc:
        note_tick_failure(engine, exc)


def one(engine, began=None):
    spans, slow, count = {}, [], 0
    waiting = time.monotonic()
    with engine.exclusive():
        spans["lock"] = round((time.monotonic() - waiting) * 1000, 3)
        try:
            with Span(spans, "reap"):
                if engine.jobs:
                    engine.jobs.reap()
            # 外部事件先归件：事件到开始处理不排在清目录、读来源之后（核心 R27）。
            with Span(spans, "classify"):
                engine.classify_events()
            if not getattr(engine, "cleaned_flows", False):
                with Span(spans, "cleanup"):
                    engine.cleanup_flows()
                engine.cleaned_flows = True
            with Span(spans, "sources"):
                engine.sources()
            with Span(spans, "channels"):
                engine.channels()
            with Span(spans, "timers"):
                engine.timers()
            with Span(spans, "waits"):
                waits.reconcile(engine)
            with Span(spans, "rounds"):
                rounds.drive(engine)
            with Span(spans, "advance"):
                slow, count = advance_open(engine)
            with Span(spans, "release"):
                engine.release_workdirs()
            with Span(spans, "channels_2"):
                engine.channels()
            with Span(spans, "heartbeat"):
                engine.heartbeat(began)
        finally:
            note_slow_tick(engine, began, spans, slow, count)


def advance(engine, item):
    if waits.active(engine, item):
        return
    control = engine.control(item["target"])
    if control.get("paused") or engine.flow_broken(item["target"], item.get("flow", "")):
        return
    item = relations.admit(engine, item)
    if item is None:
        return
    if item["state"] == "等你" and not engine.answerable(item):
        engine.change(item, state="等你", next=item.get("next"), leave=item.get("leave"),
                      gate_reason=item.get("gate_reason") or item.get("blocked_by"))
        item = engine.log.get("items", item["id"])
    if item.get("program"):
        engine.finish(item["program"])
        return
    if (item["pending_rework"] and item["depth"] <= engine.rules.data["limits"]["chain_depth"]
            and not any(r["item"] == item["id"] for r in engine.active())):
        if engine.jobs and item.get("workdir") and not item.get("workdir_released"):
            identity = [item["id"], item.get("pending_episode", 0), item["workdir"], item.get("release_at")]
            if any(engine.jobs.exists("executor", name, "release", identity)
                   for name in item.get("sessions") or {}):
                # 正在收回的目录不能同时给新一轮用；本拍末尾先结算收回结果。
                return
        with engine.log.transaction():
            engine.cancel_questions(item)
            engine.change(item, stage="执行" if item["checks"] else "说清楚", state="空闲",
                          **({} if item["checks"] else {"serial_hold": False}),
                          pending_rework=False, approved=[], pending_advance=None, reviews=[], review_required=1,
                          after_pending_count=0, after_wait=None, pending_episode=item.get("pending_episode", 0) + 1,
                          wait_for_ready=False, retry_executor=None, gate_count=0, next="按新事件返工")
        item = engine.log.get("items", item["id"])
    # 有待办才结算；后续推进看到最新阶段和状态。
    if item.get("after_wait"):
        engine.recheck_after(item)
        item = engine.log.get("items", item["id"])
    if item.get("pending_advance"):
        engine.complete_approval(item)
        item = engine.log.get("items", item["id"])
    item = relations.admit(engine, item)
    if item is None:
        return
    if engine.rules.data.get("taskhome"):
        if not engine.taskhome(item):
            return
        item = engine.log.get("items", item["id"])
    if item["state"] in ("空闲", "排队") and not item.get("after_wait"):
        item = engine.serial_queue(item)
    if item["state"] == "空闲" and not item.get("after_wait"):
        engine.reserve(item)


def advance_open(engine):
    items = {item["id"]: item for item in engine.log.open_items()}
    if engine.rules.data.get("taskhome"):
        items.update((item["id"], item) for item in engine.log.ended_home_items())
    relations.refresh(engine, [i for i in items.values() if i["state"] not in ("完成", "放弃")])
    # 刷新后的快照决定新轮次顺序；稳定排序保留同级的入队顺序。
    items = {key: engine.log.get("items", key) for key in items}
    slow = []
    for item in sorted(items.values(), key=lambda i: (i.get("relations") or {}).get("priority", "P1")):
        started = time.monotonic()
        try:
            engine.advance(engine.log.get("items", item["id"]))
        except Pending:
            pass  # 这件活在等作业出结果，下一拍再看；别的活照常推进。
        finally:
            spent = round((time.monotonic() - started) * 1000, 3)
            if spent >= SLOW_ITEM_MS:
                slow.append({"id": item["id"], "ms": spent, "stage": item.get("stage"), "state": item.get("state")})
    slow.sort(key=lambda row: row["ms"], reverse=True)
    return slow[:8], len(items)


def note_slow_tick(engine, began, spans, slow, count):
    """慢拍写在状态目录，不进事实库：库本身就是要查的开销。"""
    if began is None:
        return
    elapsed = round((time.monotonic() - began) * 1000)
    if elapsed <= SLOW_TICK_MS:
        return
    try:
        active = engine.occupied()
    except Exception:
        active = None
    record = {"at": stamp(engine.clock()), "tick_ms": elapsed, "wake_ms": getattr(engine, "wake_ms", None),
              "active": active, "open": count, "spans": spans, "slow_items": slow}
    try:
        append_slow_tick(engine.log.path.parent / "tick-slow.jsonl", record)
    except OSError:
        return


def append_slow_tick(path, record, keep=SLOW_TICK_KEEP, limit=SLOW_TICK_BYTES):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    if path.stat().st_size <= limit:
        return
    kept = path.read_text(encoding="utf-8").splitlines()[-keep:]
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def note_tick_failure(engine, exc):
    try:
        with engine.log.transaction():
            state = engine.control("@tick")
            errors = [f["body"]["error"] for f in engine.log.facts()
                      if f["kind"] == "tick_failed" and f["seq"] >= state.get("since", 0)] if state.get("failures") else []
            if repr(exc) not in errors:
                engine.fact("tick_failed", {"error": repr(exc)})
            engine.tick_failures = min(10, max(engine.tick_failures, state["failures"]) + 1) if state.get("failures") else 1
            if engine.tick_failures in (1, 3, 10) and engine.tick_failures != state.get("failures"):
                since = state.get("since") if state.get("failures") else engine.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0]
                engine.fact("control", {"id": "@tick", "since": since, "failures": engine.tick_failures})
    except Exception as recording_error:
        print(f"车间 tick 失败且暂时无法记录：{exc!r}; {recording_error!r}", file=sys.stderr)


def heartbeat(engine, began=None):
    now = stamp(engine.clock())
    heartbeat = engine.log.path.parent / "heartbeat.json"
    temporary = heartbeat.with_suffix(".tmp")
    heartbeat_process = getattr(engine, "heartbeat_process", None)
    if heartbeat_process is None:
        try:
            previous = json.loads(heartbeat.read_text())
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        process = previous.get("process")
        process = process if isinstance(process, dict) else {}
        heartbeat_process = {
            "pid": os.getpid(), "version": os.environ.get("WORKSHOP_RUNNING_COMMIT"), "first_at": now,
            "previous": {"at": previous.get("at"),
                         "pid": process.get("pid"),
                         "version": process.get("version")}}

    failures = [c for c in engine.log.all("controls") if c["id"].startswith("delivery:") and c.get("failed")]
    for failure in failures:
        if not failure.get("since"):
            failure["since"] = now
            engine.fact("control", {"id": failure["id"], "since": now})
    # tick_ms：这一拍从进入到写心跳前的耗时；一拍只读写事实库和本地文件，应是毫秒级。
    elapsed = round((time.monotonic() - began) * 1000) if began is not None else None
    temporary.write_text(json.dumps({"at": now, "last_ok_at": now, "process": heartbeat_process, "tick_ms": elapsed, "wake_ms": getattr(engine, "wake_ms", None), "active": engine.occupied(),
        "delivery_failed_count": len(failures),
        "delivery_failed_since": min((c.get("since", now) for c in failures), default=None)}))
    with engine.log.transaction():
        if engine.control("@tick").get("failures"):
            engine.fact("control", {"id": "@tick", "failures": 0, "since": None})
        temporary.replace(heartbeat)
    engine.heartbeat_process = heartbeat_process
    engine.tick_failures = 0
