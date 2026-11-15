"""一拍的顺序：收、判、通道、定时、各轮、推进、收回目录、再读通道、心跳。"""
import sys
import time

from .jobs import Pending
from .models import stamp
from .ports import Retryable, Unavailable


def tick(engine, received_at=None):
    began = time.monotonic()
    engine.tick_started = engine.clock()
    engine.wake_ms = round(max(0, began - received_at) * 1000, 3) if received_at is not None else None
    try:
        engine._tick(began)
    except Exception as exc:
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


def one(engine, began=None):
    with engine.exclusive():
        if engine.jobs:
            engine.jobs.reap()
        if not getattr(engine, "cleaned_flows", False):
            engine.cleanup_flows()
            engine.cleaned_flows = True
        engine.sources()
        engine.classify_events()
        engine.channels()
        engine.timers()
        for row in engine.active():
            item = engine.log.get("items", row["item"])
            try:
                expired = row.get("deadline", "9999") <= stamp(engine.clock())
                if row["status"] == "starting" and not row.get("started") and not row.get("abort_requested") and not expired:
                    if engine.flow_broken(item["target"], item.get("flow", "")):
                        continue
                    try:
                        engine.launch(row)
                    except Unavailable:
                        # 启动作业晚于预订出结果：放开这一轮，避开最近没法用的执行者另挑。
                        engine.release_unavailable(row)
                        engine.reserve(engine.log.get("items", item["id"]))
                        continue
                    row = engine.log.get("rounds", row["token"])
                if row["status"] == "starting" and (row.get("abort_requested") or expired):
                    engine.automatic_failure(row["token"], row.get("abort_requested") or "这一轮超过规则期限")
                else:
                    engine.check_round(row)
                row = engine.log.get("rounds", row["token"])
                if row["status"] == "finishing":
                    engine.finish(row)
            except Pending:
                continue  # 这一轮在等作业出结果，下一拍再看。
            except Retryable as exc:
                engine.start_retry(item, row, "executor", row["executor"], exc)
            except Exception as exc:
                engine.broken(item["target"], "executor", row["executor"], exc)
        for item in engine.log.all("items"):
            try:
                engine.advance(item)
            except Pending:
                continue  # 这件活在等作业出结果，下一拍再看；别的活照常推进。
        engine.release_workdirs()
        engine.channels()
        engine.heartbeat(began)


def advance(engine, item):
    control = engine.control(item["target"])
    if control.get("paused") or engine.flow_broken(item["target"], item.get("flow", "")):
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
    engine.recheck_after(item)
    item = engine.log.get("items", item["id"])
    engine.complete_approval(item)
    item = engine.log.get("items", item["id"])
    if not engine.taskhome(item):
        return
    item = engine.log.get("items", item["id"])
    if item["state"] in ("空闲", "排队") and not item.get("after_wait"):
        item = engine.serial_queue(item)
    if item["state"] == "空闲" and not item.get("after_wait"):
        engine.reserve(item)
