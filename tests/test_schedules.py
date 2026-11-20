"""核心 R34：用户周期任务，和定时发版共用一套调度。"""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess

import pytest

from conftest import build, hold_for_kun
from workshop.core import schedules
from workshop.core.models import Cadence
from workshop.reports.report import status

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]


def at(*args):
    return datetime(*args, tzinfo=UTC)


def add(engine, schedule="daily", **options):
    options.setdefault("target", "alpha")
    options.setdefault("title", "日报")
    options.setdefault("every", "daily")
    options.setdefault("at", "09:00")
    return schedules.add(engine, schedule, **options)


def tick(engine, times=1):
    """守护进程的一拍；一拍里的异常只记成 tick_failed，这里不许有。"""
    for _ in range(times):
        engine.tick()
    assert not facts(engine, "tick_failed")


def facts(engine, kind, schedule=None):
    return [f for f in engine.log.facts() if f["kind"] == kind
            and (schedule is None or f["body"].get("schedule", (f["body"].get("body") or {}).get("schedule")) == schedule)]


def opened(engine, schedule=None):
    return [f for f in facts(engine, "event", schedule) if f["source"] == schedules.SOURCE]


def triggers(engine, schedule=None):
    """到点触发 = 开活或重入跳过。"""
    return len(opened(engine, schedule)) + len(facts(engine, "schedule_skipped", schedule))


def control(engine, schedule="daily"):
    return engine.log.get("controls", schedules.PREFIX + schedule)


def restart(engine, path):
    """守护进程重启：关掉库，重新装一个引擎，假时钟接着走。"""
    now = engine.clock.now
    engine.log.close()
    fresh = build(path)
    fresh.clock.now = now
    return fresh


def last_item(engine, schedule="daily"):
    return engine.log.get("items", schedules.latest(engine.log, schedule))


def end(engine, schedule="daily"):
    engine.abandon(last_item(engine, schedule)["id"], "测试：上一件结案")


def notifications(engine):
    """车间不给 Kun 发消息：没有卡片、没有新登记的等待（等待只来自测试自己把活停在等你）。"""
    return len(facts(engine, "card_created")) + len([f for f in facts(engine, "wait_bound")
                                                     if not f["body"]["operation"].startswith("bind:")])


def wait_on_kun(engine, schedule="daily", ref="decision"):
    item = last_item(engine, schedule)
    if item.get("wait_id"):
        # 再一次停到等你：先让上一个等待失效，等你从这次进入算起。
        engine.change(item, state="定时", wait_id=None, next="测试：离开等你")
    hold_for_kun(engine, key=item["object_key"], ref=ref)
    assert last_item(engine, schedule)["state"] == "等你"


def schema(engine):
    return sorted(row[0] for row in engine.log.db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))


def test_cli_add_list_disable_only_write_facts(tmp_path, capsys):
    from workshop.cli import main
    engine = build(tmp_path)
    before, count = schema(engine), len(engine.log.facts())
    engine.log.close()
    run = lambda *argv: main(["--state-dir", str(tmp_path), "schedule", *argv])
    assert run("add", "daily-report", "--target", "alpha", "--title", "日报", "--every", "daily", "--at", "09:00") == 0
    assert "已登记周期任务 daily-report：下一次 " in capsys.readouterr().out
    assert run("add", "weekly-sync", "--target", "alpha", "--title", "周会", "--every", "weekly",
               "--weekday", "mon", "--at", "02:00", "--tz", "Asia/Shanghai") == 0
    capsys.readouterr()
    assert run("list") == 0
    listed = capsys.readouterr().out.splitlines()
    assert listed[0].split("\t") == ["任务", "周期", "时刻", "时区", "交付对象", "流程", "下一次", "状态", "上一件"]
    assert listed[1].startswith("daily-report\tdaily\t09:00\tAsia/Shanghai\talpha\tflow\t") and listed[1].endswith("\t启用\t—")
    assert listed[2].startswith("weekly-sync\tweekly mon\t02:00\tAsia/Shanghai\talpha\tflow\t")
    assert run("disable", "weekly-sync") == 0
    capsys.readouterr()
    assert run("list") == 0
    assert capsys.readouterr().out.splitlines()[2].endswith("\t停用\t—")
    # 写错的登记不落事实。
    assert run("add", "bad", "--target", "alpha", "--title", "x", "--every", "weekly", "--at", "02:00") == 2
    assert run("add", "bad", "--target", "alpha", "--title", "x", "--every", "daily", "--at", "25:00") == 2
    assert run("add", "bad", "--target", "alpha", "--title", "x", "--every", "daily", "--at", "09:00", "--tz", "Mars/Base") == 2
    assert run("add", "bad", "--target", "nowhere", "--title", "x", "--every", "daily", "--at", "09:00") == 2
    assert run("add", "daily-report", "--target", "alpha", "--title", "x", "--every", "daily", "--at", "09:00") == 2
    assert run("disable", "missing") == 2
    engine = build(tmp_path)
    # 只写事实，不改库：表和索引原样；新增的只有登记、停用这三条 control 事实。
    assert schema(engine) == before
    added = engine.log.facts()[count:]
    assert [(f["kind"], f["body"]["id"]) for f in added] == [
        ("control", "@schedule:daily-report"), ("control", "@schedule:weekly-sync"), ("control", "@schedule:weekly-sync")]
    assert control(engine, "weekly-sync")["enabled"] is False and control(engine, "daily-report")["enabled"] is True
    engine.log.close()


def test_due_opens_exactly_one_item_and_repeated_scans_dedupe(tmp_path):
    engine = build(tmp_path)
    add(engine)
    assert control(engine)["next_at"] == "2026-11-14T09:00:00+08:00"
    engine.clock.now = at(2026, 11, 14, 1)
    tick(engine, 3)
    [event] = opened(engine)
    assert event["ext_id"] == "daily:2026-11-14T09:00:00+08:00"
    item = engine.log.get("items", "alpha:daily@2026-11-14T09:00:00+08:00")
    # 走同一套接单、阶段和关口：从接单开始，照常派轮。
    assert item["stage"] == "接单" and item["state"] == "跑着" and item["title"] == "日报（2026-11-14 09:00）"
    assert control(engine)["next_at"] == "2026-11-15T09:00:00+08:00"
    # 同一到点时间再扫：把记下的下一次退回去，模拟重复扫描同一槽位，仍只有一条。
    engine.fact("control", {"id": schedules.PREFIX + "daily", "next_at": "2026-11-14T09:00:00+08:00"})
    tick(engine, 2)
    assert len(opened(engine)) == 1 and not facts(engine, "schedule_skipped")
    assert control(engine)["next_at"] == "2026-11-15T09:00:00+08:00"
    found = Cadence("daily", at="09:00").scan(engine.clock(), "2026-11-14T09:00:00+08:00")
    assert schedules.fired(engine.log, found.ext_id("daily"))
    schedules.fire(engine, control(engine), found)  # 直接再开一次也被 ext_id 去重挡住。
    assert len(opened(engine)) == 1
    engine.log.close()


def test_restart_across_due_time_fires_exactly_once(tmp_path):
    engine = build(tmp_path)
    add(engine)
    engine.clock.now = at(2026, 11, 14, 0, 59)
    tick(engine)
    assert not opened(engine)
    engine.clock.now = at(2026, 11, 14, 1, 30)  # 停着的时候跨过了到点时间
    engine = restart(engine, tmp_path)
    tick(engine, 2)
    engine = restart(engine, tmp_path)
    tick(engine, 2)
    assert len(opened(engine)) == 1 and not facts(engine, "schedule_skipped")
    assert control(engine)["next_at"] == "2026-11-15T09:00:00+08:00"
    engine.log.close()


def test_downtime_coalesces_once_and_skip_policy_only_advances(tmp_path):
    engine = build(tmp_path)
    add(engine, "merged")
    add(engine, "skipping", catchup="skip")
    engine.log.close()
    engine = build(tmp_path)
    engine.clock.now = at(2026, 11, 17, 3)  # 停机错过 14、15、16、17 日四次
    tick(engine, 3)
    [event] = opened(engine, "merged")
    assert event["ext_id"] == "merged:2026-11-17T09:00:00+08:00"  # 锚定最近错过的槽位
    assert event["body"]["body"]["missed"] == 4
    assert control(engine, "merged")["next_at"] == "2026-11-18T09:00:00+08:00"
    assert not opened(engine, "skipping") and not facts(engine, "schedule_skipped", "skipping")
    assert control(engine, "skipping")["next_at"] == "2026-11-18T09:00:00+08:00"
    # skip 策略只跳过停机错过的；按时到点照常开活。
    engine.clock.now = at(2026, 11, 18, 1)
    tick(engine)
    assert len(opened(engine, "skipping")) == 1
    engine.log.close()


def test_overlap_only_records_skip_while_previous_is_open(tmp_path):
    engine = build(tmp_path)
    add(engine)
    engine.clock.now = at(2026, 11, 14, 1)
    tick(engine)
    first = last_item(engine)
    assert first["state"] == "跑着"
    engine.clock.now = at(2026, 11, 15, 1)
    tick(engine, 2)
    assert len(opened(engine)) == 1
    [skipped] = facts(engine, "schedule_skipped")
    assert skipped["item"] == first["id"] and skipped["body"]["slot"] == "2026-11-15T09:00:00+08:00"
    assert "还没结束（跑着）" in skipped["body"]["reason"]
    assert control(engine)["next_at"] == "2026-11-16T09:00:00+08:00"
    engine.log.close()


def test_starved_after_three_skips_survives_restart_and_is_recorded_once(tmp_path):
    engine = build(tmp_path)
    add(engine)
    engine.clock.now = at(2026, 11, 14, 1)
    schedules.scan(engine)
    item_id = last_item(engine)["id"]
    for day, ref in ((15, "first"), (16, "second")):
        # 每次到点前不久停到等你：两次跳过时都在等你，但每段都没满 24 小时。
        engine.clock.now = at(2026, 11, day, 0, 30)
        wait_on_kun(engine, ref=ref)
        tick(engine)
        engine.clock.now = at(2026, 11, day, 1)
        tick(engine)
    assert len(facts(engine, "schedule_skipped")) == 2 and not facts(engine, "schedule_starved")
    engine = restart(engine, tmp_path)
    engine.clock.now = at(2026, 11, 17, 0, 30)
    wait_on_kun(engine, ref="third")
    tick(engine)
    engine.clock.now = at(2026, 11, 17, 1)
    tick(engine)
    assert len(facts(engine, "schedule_skipped")) == 3
    [starved] = facts(engine, "schedule_starved")
    assert starved["at"] == "2026-11-17T01:00:00+00:00" and starved["item"] == item_id
    assert starved["body"]["skips"] == 3 and "连续跳过 3 次" in starved["body"]["reason"]
    # 再过 24 小时以上：等你满 24 小时和第 4、5 次跳过叠加，跨重启多拍扫描都不重落。
    engine.clock.now = at(2026, 11, 18, 2)
    for _ in range(2):
        engine = restart(engine, tmp_path)
        tick(engine, 3)
    engine.clock.now = at(2026, 11, 19, 1, 1)
    engine = restart(engine, tmp_path)
    tick(engine, 3)
    assert len(facts(engine, "schedule_skipped")) == 5
    assert len(facts(engine, "schedule_starved")) == 1 and notifications(engine) == 0
    shown = status(engine.log)
    assert "周期任务停摆 1 条" in shown and f"daily\t{item_id}\t等你\t上一件 {item_id} 没结束，连续跳过 3 次" in shown
    engine.log.close()


def test_waiting_on_kun_for_24_hours_alone_is_reported(tmp_path):
    engine = build(tmp_path)
    add(engine, "weekly", every="weekly", weekday="mon")
    engine.clock.now = at(2026, 11, 16, 1)  # 当地周一 09:00
    schedules.scan(engine)
    engine.clock.now = start = at(2026, 11, 16, 2)
    wait_on_kun(engine, "weekly")
    tick(engine)
    engine.clock.now = start + timedelta(hours=23, minutes=59)
    tick(engine, 2)
    assert not facts(engine, "schedule_starved") and not facts(engine, "schedule_skipped")
    assert "周期任务停摆" not in status(engine.log)
    # 守护进程按挂起满 24 小时的时刻醒，不等下一次到点。
    engine.clock.now = start + timedelta(hours=23, minutes=59, seconds=30)
    tick(engine)
    assert "2026-11-17T02:00:01+00:00" in schedules.deadlines(engine.log) and engine.wait_timeout() <= 31
    engine.clock.now = start + timedelta(hours=24, minutes=1)
    tick(engine)
    [starved] = facts(engine, "schedule_starved")
    assert starved["at"] == "2026-11-17T02:01:00+00:00" and starved["body"]["skips"] == 0
    assert "等你超过 24 小时" in starved["body"]["reason"] and notifications(engine) == 0
    assert "周期任务停摆 1 条" in status(engine.log)
    for _ in range(2):
        engine = restart(engine, tmp_path)
        tick(engine, 3)
    assert len(facts(engine, "schedule_starved")) == 1
    engine.log.close()


def test_waiting_for_authorization_for_24_hours_is_reported(tmp_path):
    engine = build(tmp_path)
    add(engine, "weekly", every="weekly", weekday="mon")
    engine.clock.now = at(2026, 11, 16, 1)
    schedules.scan(engine)
    item = last_item(engine, "weekly")
    engine.clock.now = start = at(2026, 11, 16, 2)
    reason = "动作等待授权中心核实原申请和实际生效：对外发消息"
    with engine.log.transaction():
        engine.fact("gate_blocked", {"gate_reason": reason}, item["id"])
        engine.change(item, state="卡住", blocked_by=reason, next="流程按原申请号核实授权中心的结果后恢复",
                      leave=engine.blocked_leave(reason))
    tick(engine)
    engine.clock.now = start + timedelta(hours=23, minutes=59)
    tick(engine)
    assert not facts(engine, "schedule_starved")
    engine.clock.now = start + timedelta(hours=24, minutes=1)
    tick(engine, 2)
    [starved] = facts(engine, "schedule_starved")
    assert "等授权超过 24 小时" in starved["body"]["reason"] and not facts(engine, "schedule_skipped")
    assert notifications(engine) == 0
    engine.log.close()


def test_closing_the_held_item_clears_starved_and_resets_skips(tmp_path):
    engine = build(tmp_path)
    add(engine)
    engine.clock.now = at(2026, 11, 14, 1)
    schedules.scan(engine)
    first = last_item(engine)["id"]
    for day in (15, 16, 17):
        engine.clock.now = at(2026, 11, day, 1)
        schedules.scan(engine)
    assert schedules.skips(engine.log, "daily", first) == 3 and len(facts(engine, "schedule_starved")) == 1
    assert "周期任务停摆 1 条" in status(engine.log)
    engine.clock.now = at(2026, 11, 17, 2)
    end(engine)
    tick(engine)
    assert "周期任务停摆" not in status(engine.log) and not schedules.starved(engine.log)
    engine.clock.now = at(2026, 11, 18, 1)
    tick(engine)
    second = last_item(engine)["id"]
    assert second != first and schedules.skips(engine.log, "daily", second) == 0
    engine.clock.now = at(2026, 11, 19, 1)
    tick(engine)
    assert schedules.skips(engine.log, "daily", second) == 1
    assert len(facts(engine, "schedule_starved")) == 1 and "周期任务停摆" not in status(engine.log)
    engine.log.close()


def test_schedules_of_the_same_kind_do_not_affect_each_other(tmp_path):
    engine = build(tmp_path)
    add(engine, "a", title="A")
    add(engine, "b", title="B", at="10:00")
    engine.clock.now = at(2026, 11, 14, 1)
    schedules.scan(engine)
    wait_on_kun(engine, "a")
    engine.clock.now = at(2026, 11, 15, 1, 30)  # A 的上一件等你超过 24 小时，期间 B 已到点一次
    schedules.scan(engine)
    assert [f["item"] for f in facts(engine, "schedule_starved")] == [last_item(engine, "a")["id"]]
    assert len(opened(engine, "b")) == 1 and not facts(engine, "schedule_skipped", "b")
    end(engine, "b")
    engine.clock.now = at(2026, 11, 15, 2)  # B 的第二次到点：A 仍挂着，B 照常开活
    schedules.scan(engine)
    assert len(opened(engine, "b")) == 2 and not facts(engine, "schedule_skipped", "b")
    assert len(facts(engine, "schedule_skipped", "a")) == 1
    engine.log.close()


def test_disabled_schedule_never_fires_again(tmp_path):
    engine = build(tmp_path)
    add(engine)
    engine.clock.now = at(2026, 11, 14, 1)
    tick(engine)
    before = len(engine.log.facts())
    schedules.disable(engine, "daily")
    for day in (15, 16, 17):
        engine.clock.now = at(2026, 11, day, 1, 5)
        tick(engine, 2)
    engine = restart(engine, tmp_path)
    tick(engine)
    assert len(opened(engine)) == 1 and not facts(engine, "schedule_skipped") and not facts(engine, "schedule_starved")
    assert not [f for f in engine.log.facts()[before:] if f["source"].startswith("schedule")]
    engine.log.close()


def test_shanghai_nine_oclock_fires_at_utc_one(tmp_path):
    engine = build(tmp_path)
    add(engine, zone="Asia/Shanghai")
    engine.clock.now = at(2026, 11, 14, 0, 59, 59)
    tick(engine)
    assert not opened(engine)
    engine.clock.now = at(2026, 11, 14, 1)
    tick(engine)
    assert [f["at"] for f in opened(engine)] == ["2026-11-14T01:00:00+00:00"]
    engine.log.close()


def test_weekly_monday_two_oclock_in_shanghai_crosses_the_utc_day(tmp_path):
    engine = build(tmp_path)
    add(engine, "monday", every="weekly", weekday="mon", at="02:00")
    assert control(engine, "monday")["next_at"] == "2026-11-16T02:00:00+08:00"
    engine.clock.now = at(2026, 11, 15, 17, 59)  # UTC 周日 17:59
    schedules.scan(engine)
    assert triggers(engine) == 0
    engine.clock.now = at(2026, 11, 15, 18)  # UTC 周日 18:00 = 当地周一 02:00
    schedules.scan(engine)
    assert [f["ext_id"] for f in opened(engine)] == ["monday:2026-11-16T02:00:00+08:00"]
    assert control(engine, "monday")["next_at"] == "2026-11-23T02:00:00+08:00"
    end(engine, "monday")
    for day in range(16, 22):  # 当地周二到周日的 02:00
        engine.clock.now = at(2026, 11, day, 18)
        schedules.scan(engine)
        assert triggers(engine) == 1
    engine.clock.now = at(2026, 11, 22, 18)
    schedules.scan(engine)
    assert [f["ext_id"] for f in opened(engine)][1:] == ["monday:2026-11-23T02:00:00+08:00"] and triggers(engine) == 2
    engine.log.close()


def test_spring_forward_gap_fires_at_three_then_back_to_half_past_two(tmp_path):
    engine = build(tmp_path)
    engine.clock.now = at(2026, 3, 7, 12)
    add(engine, "ny", at="02:30", zone="America/New_York")
    engine.clock.now = at(2026, 3, 8, 6, 59)
    schedules.scan(engine)
    assert triggers(engine) == 0
    engine.clock.now = at(2026, 3, 8, 7)  # 当地 03:00 EDT：02:30 这天不存在
    schedules.scan(engine)
    assert [f["ext_id"] for f in opened(engine)] == ["ny:2026-03-08T03:00:00-04:00"]
    end(engine, "ny")
    engine.clock.now = at(2026, 3, 9, 6, 29)
    schedules.scan(engine)
    assert triggers(engine) == 1
    engine.clock.now = at(2026, 3, 9, 6, 30)  # 第二天回到当地 02:30
    schedules.scan(engine)
    assert [f["ext_id"] for f in opened(engine)][1:] == ["ny:2026-03-09T02:30:00-04:00"]
    engine.log.close()


def test_fall_back_repeated_hour_fires_once(tmp_path):
    engine = build(tmp_path)
    engine.clock.now = at(2026, 10, 31, 12)
    add(engine, "ny", at="01:30", zone="America/New_York")
    moment = at(2026, 11, 1, 4)  # 当地 00:00 EDT，扫到 03:00 EST，01:30 出现两次
    while moment <= at(2026, 11, 1, 8):
        engine.clock.now = moment
        schedules.scan(engine)
        moment += timedelta(minutes=15)
    assert triggers(engine) == 1
    assert [f["ext_id"] for f in opened(engine)] == ["ny:2026-11-01T01:30:00-04:00"]
    assert control(engine, "ny")["next_at"] == "2026-11-02T01:30:00-05:00"
    engine.log.close()


def test_crash_after_opening_before_next_at_is_idempotent(tmp_path, monkeypatch):
    engine = build(tmp_path)
    add(engine)
    engine.log.close()
    computed = []
    real = Cadence.scan

    def spy(self, *args, **kwargs):
        found = real(self, *args, **kwargs)
        computed.append(found)
        return found
    monkeypatch.setattr(Cadence, "scan", spy)
    persist = schedules.persist
    monkeypatch.setattr(schedules, "persist", lambda *args: None)  # 开活事实已写，next_at 还没落盘就崩了
    engine = build(tmp_path)
    engine.clock.now = at(2026, 11, 16, 3)  # 停机跳过 14、15、16 三个周期
    schedules.scan(engine)
    assert len(opened(engine)) == 1 and control(engine)["next_at"] == "2026-11-14T09:00:00+08:00"
    before = computed[-1].ext_id("daily")
    monkeypatch.setattr(schedules, "persist", persist)
    engine.clock.now += timedelta(minutes=5)  # 仍早于下一个未来周期
    engine = restart(engine, tmp_path)
    schedules.scan(engine)
    after = computed[-1].ext_id("daily")
    assert after == before == "daily:2026-11-16T09:00:00+08:00"
    assert [f["ext_id"] for f in opened(engine)] == [before] and not facts(engine, "schedule_skipped")
    assert control(engine)["next_at"] == "2026-11-17T09:00:00+08:00"
    engine.log.close()


def test_release_and_user_schedules_call_the_same_helper(tmp_path, monkeypatch):
    from workshop import running
    from workshop.plugins.scheduled_release.source import MainBranchSource
    import workshop.plugins.scheduled_release.source as source_module
    calls = []
    real = Cadence.scan

    def spy(self, *args, **kwargs):
        calls.append(self.every)
        return real(self, *args, **kwargs)
    monkeypatch.setattr(Cadence, "scan", spy)
    monkeypatch.setenv(running.VARIABLE, "a" * 40)
    monkeypatch.setattr(source_module, "run",
                        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "b" * 40 + "\trefs/heads/main\n", ""))
    target = {"name": "alpha", "repo": "example/workshop", "checkout": str(tmp_path / "checkout"),
              "scheduled_release_state_dir": str(tmp_path / "release"), "sources": {"main_branch": {"every": "1h"}},
              "github_identity": {"mode": "inherit"}}
    released = MainBranchSource(lambda: at(2026, 11, 14, 1)).poll(target, None)
    assert calls == ["minutes"] and released.events[0].ext_id == "2026-11-14T01:00:00+00:00"
    engine = build(tmp_path)
    add(engine)
    engine.clock.now = at(2026, 11, 14, 1)
    tick(engine)
    assert calls[1:] and set(calls[1:]) == {"daily"} and len(opened(engine)) == 1
    engine.log.close()
    # 静态：定时发版的来源不再自己切槽位、推进下一次或拼去重标识，只调 Cadence.scan。
    path = ROOT / "src/workshop/plugins/scheduled_release/source.py"
    tree = ast.parse(path.read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert not {"timedelta", "EPOCH"} & names
    assert not any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.FloorDiv) for n in ast.walk(tree))
    assert any(isinstance(n, ast.Attribute) and n.attr == "scan" for n in ast.walk(tree))


def test_wait_timeout_wakes_for_the_next_due_time(tmp_path):
    engine = build(tmp_path)
    add(engine)
    engine.clock.now = at(2026, 11, 14, 0, 59, 30)
    tick(engine)
    assert engine.wait_timeout() == 30
    engine.log.close()


def test_status_in_a_read_only_copy_lists_starved(tmp_path):
    from workshop.core.log import Log
    engine = build(tmp_path)
    add(engine)
    engine.clock.now = at(2026, 11, 14, 1)
    schedules.scan(engine)
    for day in (15, 16, 17):
        engine.clock.now = at(2026, 11, day, 1)
        schedules.scan(engine)
    engine.log.close()
    log = Log.readonly(tmp_path / "workshop.db")
    try:
        with log.read_transaction():
            assert "周期任务停摆 1 条" in status(log)
        with pytest.raises(sqlite3.OperationalError):
            log.db.execute("INSERT INTO facts(kind,source,ext_id,body,at) VALUES ('x','x','x','{}','x')")
    finally:
        log.close()
