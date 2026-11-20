"""用户周期任务（核心 R34）：到点开一件活，和定时发版共用 Cadence 的扫描。

登记、停用只写 control 事实（id 为 @schedule:<任务号>，kind=schedule），记下的下一次时刻也在这里，跨重启不丢。
到点开活走同一套接单：一条来源为 schedule 的事件，去重标识是 <任务号>:<槽位>，事实表的唯一约束保证同一槽位只落一条。
上一件、连续跳过次数、有没有报过停摆，都从事实表算，不另存计数：重启、半程崩溃都不会丢或重算。
"""
from datetime import datetime, timedelta
import json
import re

from .models import Cadence, Event, WEEKDAYS, stamp

PREFIX = "@schedule:"
SOURCE = "schedule"          # 到点开活的事件
SKIPPED = "schedule-skipped"  # 重入跳过
STARVED = "schedule-starved"  # 停摆告警
FAILED = "schedule-failed"    # 任务本身坏了（例如流程从规则里拿掉）
CATCHUP = ("coalesce_one", "skip")
ENDED = ("完成", "放弃")
STARVE_SKIPS = 3
STARVE_HOLD = timedelta(hours=24)
# 「skip」补跑策略下，扫到时离槽位超过这么久，算停机错过，只推进不开活。
LATE = timedelta(minutes=5)


def cadence(row):
    return Cadence(row["every"], at=row["at"], weekday=row.get("weekday", 0), zone=row["zone"])


def rows(log, enabled_only=False):
    found = [row for row in log.all("controls", where=("kind", "schedule")) if row["id"].startswith(PREFIX)]
    return [row for row in found if row.get("enabled")] if enabled_only else found


def add(engine, schedule, *, target, title, every, at, weekday=None, zone="Asia/Shanghai",
        flow="", catchup="coalesce_one", about=""):
    if not isinstance(schedule, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", schedule):
        raise ValueError("任务号只用小写字母、数字、- 和 _，以字母或数字开头，最长 64")
    if every not in ("daily", "weekly"):
        raise ValueError("周期只支持 daily（每天）和 weekly（每周）")
    if every == "weekly" and weekday not in WEEKDAYS:
        raise ValueError("每周的任务要给星期：" + "、".join(WEEKDAYS))
    if every == "daily" and weekday is not None:
        raise ValueError("每天的任务不填星期")
    if catchup not in CATCHUP:
        raise ValueError("补跑策略只支持 " + "、".join(CATCHUP))
    if not isinstance(title, str) or not title.strip():
        raise ValueError("任务要有标题")
    if target not in engine.rules.data["targets"]:
        raise ValueError(f"没有这个交付对象：{target}")
    try:
        config = engine.rules.target(target, flow)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{target} 没有流程 {flow!r}") from exc
    rhythm = Cadence(every, at=at, weekday=WEEKDAYS.index(weekday) if weekday else 0, zone=zone)
    now = engine.clock()
    row = {"id": PREFIX + schedule, "kind": "schedule", "schedule": schedule, "target": target,
           "flow": flow, "flow_name": config["flow"], "title": title.strip(), "about": about.strip(),
           "every": every, "at": at, "weekday": rhythm.weekday, "zone": zone, "catchup": catchup,
           "enabled": True, "created_at": stamp(now), "next_at": rhythm.key(rhythm.after(now)), "held": None}
    with engine.log.transaction():
        if engine.log.get("controls", row["id"]):
            raise ValueError(f"任务号已用过：{schedule}；停用的不能复用，换一个任务号")
        engine.fact("control", row)
    return row


def disable(engine, schedule):
    with engine.log.transaction():
        row = engine.log.get("controls", PREFIX + schedule)
        if not row or row.get("kind") != "schedule":
            raise ValueError(f"没有这条周期任务：{schedule}")
        if not row.get("enabled"):
            return row
        engine.fact("control", {"id": row["id"], "enabled": False, "disabled_at": stamp(engine.clock())})
    return engine.log.get("controls", row["id"])


def latest(log, schedule):
    """这条任务开出来的最后一件活：最近一条到点事件挂到的活（暂停期间补放的也算）。"""
    found = log.db.execute(
        "SELECT item FROM facts WHERE source=? AND ext_id>=? AND ext_id<? AND kind IN ('event','deferred_event') "
        "ORDER BY seq DESC LIMIT 1", (SOURCE, schedule + ":", schedule + ";")).fetchone()
    return found[0] if found else None


def fired(log, ext_id):
    return log.db.execute("SELECT 1 FROM facts WHERE source=? AND ext_id=?", (SOURCE, ext_id)).fetchone() is not None


def held(item):
    """挂起的样子：等你，或流程在等授权中心核实（卡在关口或在回查）。不是挂起返回 None。"""
    if item["state"] == "等你":
        return "等你"
    if "授权中心" in (item.get("gate_reason") or "") and (item["state"] == "卡住" or item.get("after_wait")):
        return "等授权"
    return None


def skips(log, schedule, item_id):
    """连续跳过次数：上一件活还开着时记下的跳过；它结案后新开的活从 0 数起。"""
    return log.db.execute("SELECT count(*) FROM facts WHERE item=? AND kind='schedule_skipped' "
                          "AND json_extract(body,'$.schedule')=?", (item_id, schedule)).fetchone()[0]


def starved_fact(log, schedule, item_id):
    found = log.db.execute("SELECT body, at FROM facts WHERE source=? AND ext_id=?",
                           (STARVED, f"{schedule}:{item_id}")).fetchone()
    return found


def scan(engine):
    now = engine.clock()
    for row in rows(engine.log, enabled_only=True):
        if row["target"] not in engine.rules.data["targets"]:
            continue  # 交付对象从规则里拿掉了：任务留着，规则恢复后接着按记下的时刻走。
        try:
            settle(engine, row, now)
        except (ValueError, KeyError, TypeError) as exc:
            # 一条任务坏了不拖累别的任务和这一拍；同一个到点时刻只记一条，不推进，修好后照常补。
            engine.fact("schedule_failed", {"schedule": row["schedule"], "next_at": row.get("next_at"), "error": str(exc)},
                        source=FAILED, ext_id=f"{row['schedule']}:{row.get('next_at')}")


def settle(engine, row, now):
    schedule = row["schedule"]
    rhythm = cadence(row)
    last_id = latest(engine.log, schedule)
    last = engine.log.get("items", last_id) if last_id else None
    running = last is not None and last["state"] not in ENDED
    changes = {}
    hold = row.get("held")
    kind = held(last) if running else None
    since = engine.log.state_since(last["id"]) if kind else None
    if kind and (not hold or hold.get("item") != last["id"] or hold.get("kind") != kind
                 or kind == "等你" and since and since != hold.get("since")):
        # 等你从进入这个状态算起；等授权时流程反复回查会改状态，从第一次看到算起。
        hold = {"item": last["id"], "kind": kind, "since": since or stamp(now)}
        changes["held"] = hold
    elif not kind and hold:
        hold = changes["held"] = None
    found = rhythm.scan(now, row.get("next_at"))
    with engine.log.transaction():
        if found.due:
            late = found.missed > 1 or now - datetime.fromisoformat(found.slot) > LATE
            if fired(engine.log, found.ext_id(schedule)):
                pass  # 这个槽位开过活，只是下一次时刻没来得及记下（半程崩溃）：只推进。
            elif row.get("catchup") == "skip" and late:
                pass  # 停机错过的槽位只推进，不开活。
            elif running:
                # 重入跳过：上一件还没结束（含等你、等授权），不并发开第二件。
                engine.fact("schedule_skipped", {"schedule": schedule, "slot": found.slot, "missed": found.missed,
                            "reason": f"上一件 {last['id']} 还没结束（{last['state']}）"}, last["id"],
                            source=SKIPPED, ext_id=f"{schedule}:{found.slot}")
            else:
                fire(engine, row, found)
            changes["next_at"] = found.next_at
        if running:
            starve(engine, row, last, hold, now)
        if changes:
            persist(engine, row, changes)


def fire(engine, row, found):
    local = datetime.fromisoformat(found.slot)
    event = Event(SOURCE, found.ext_id(row["schedule"]), row["target"], f"{row['schedule']}@{found.slot}",
                  f"{row['title']}（{local:%Y-%m-%d %H:%M}）",
                  {"kind": "schedule", "schedule": row["schedule"], "slot": found.slot,
                   "missed": found.missed, "about": row.get("about", "")},
                  action="ready", flow=row.get("flow", ""))
    # 核心内部的到点和定时一样定向进流程，不经来源触发器；之后走同一套接单、阶段和关口。
    engine.ingest(event, directed=True)


def starve(engine, row, last, hold, now):
    """连续跳过 3 次，或上一件挂起超过 24 小时：记一条停摆事实；同一件挂起的活只记一条，不发消息。"""
    schedule = row["schedule"]
    count = skips(engine.log, schedule, last["id"])
    if count >= STARVE_SKIPS:
        reason = f"上一件 {last['id']} 没结束，连续跳过 {count} 次"
    elif hold and now - datetime.fromisoformat(hold["since"]) > STARVE_HOLD:
        reason = f"上一件 {last['id']} {hold['kind']}超过 24 小时（自 {hold['since']}）"
    else:
        return
    if starved_fact(engine.log, schedule, last["id"]):
        return
    engine.fact("schedule_starved", {"schedule": schedule, "item": last["id"], "skips": count,
                "held": hold, "reason": reason}, last["id"], source=STARVED, ext_id=f"{schedule}:{last['id']}")


def persist(engine, row, changes):
    engine.fact("control", {"id": row["id"], **changes})


def deadlines(log):
    """守护进程要按时醒：每条任务的下一次时刻，以及挂起满 24 小时的时刻。"""
    result = []
    for row in rows(log, enabled_only=True):
        result.append(row.get("next_at"))
        if row.get("held"):
            result.append(stamp(datetime.fromisoformat(row["held"]["since"]) + STARVE_HOLD + timedelta(seconds=1)))
    return result


def starved(log):
    """status 要显示的停摆：报过停摆、挂起的那件还没结案的启用任务。结案后不再显示。"""
    result = []
    for row in rows(log, enabled_only=True):
        last_id = latest(log, row["schedule"])
        last = log.get("items", last_id) if last_id else None
        if last is None or last["state"] in ENDED:
            continue
        fact = starved_fact(log, row["schedule"], last_id)
        if fact:
            body = json.loads(fact[0])
            result.append({"schedule": row["schedule"], "item": last_id, "state": last["state"],
                           "reason": body["reason"], "at": fact[1], "skips": skips(log, row["schedule"], last_id)})
    return result


def listing(log):
    lines = ["任务\t周期\t时刻\t时区\t交付对象\t流程\t下一次\t状态\t上一件"]
    for row in rows(log):
        rhythm = row["every"] + (f" {WEEKDAYS[row.get('weekday', 0)]}" if row["every"] == "weekly" else "")
        state = "启用" if row.get("enabled") else "停用"
        last = latest(log, row["schedule"]) or "—"
        lines.append("\t".join((row["schedule"], rhythm, row["at"], row["zone"], row["target"],
                                row.get("flow_name") or row.get("flow") or "—", row.get("next_at") or "—", state, last)))
    return "\n".join(lines) if len(lines) > 1 else "没有周期任务"
