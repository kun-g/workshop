"""主循环一拍毫秒级：外部调用都在后台作业里跑，一拍只登记、启动、读结果。

替身插件（job_plugins）的状态和开关在文件里，作业进程用 job_runner 装同一套替身。
一拍期间把等子进程、发网络请求的函数换成一调用就报错，证明一拍里没有这些调用。
"""
from contextlib import contextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

from conftest import Clock, event
from workshop.core.cards import notice
from workshop.core.engine import Engine
from workshop.core.jobs import Jobs, Pending
from workshop.core.log import Log
from workshop.core.ports import Retryable
from workshop.core.report import status
from workshop.core.rules import Rules

import job_plugins

RUNNER = str(Path(__file__).with_name("job_runner.py"))
LIMIT_MS = 100


def rules():
    return Rules.validate(dict(
        targets={"alpha": {"plugin": "flow", "sources": ["source"], "repo": "owner/repo", "check_timeout": 1}},
        executors={"writer": {"use": "writer", "family": "a", "billing": "subscription"},
                   "reviewer": [{"use": "reviewer", "family": "b", "billing": "subscription"}],
                   "acceptor": [{"use": "acceptor", "family": "c", "billing": "subscription"}]},
        limits={"concurrent": 3, "chain_depth": 8}, selector="ordered", channel="channel",
        ask_first=["对外发消息"], skip_ask=[]))


def configure(path, **values):
    (path / "config.json").write_text(json.dumps(values))


def allow(world, *kinds):
    for kind in kinds:
        (world["path"] / f"allow-{kind}").touch()


def forbidden(name):
    def refuse(*args, **kwargs):
        raise AssertionError(f"一拍里不许调用 {name}")
    return refuse


@contextmanager
def no_waiting_or_network(monkeypatch):
    """一拍期间：等子进程、发网络请求的入口一律报错；只允许启动独立进程（Popen 不等）。"""
    with monkeypatch.context() as patch:
        for module, name in ((subprocess, "run"), (subprocess, "check_output"), (subprocess, "call"),
                             (subprocess, "check_call"), (subprocess.Popen, "wait"), (subprocess.Popen, "communicate"),
                             (urllib.request, "urlopen"), (socket, "create_connection"), (socket.socket, "connect")):
            patch.setattr(module, name, forbidden(f"{getattr(module, '__name__', module)}.{name}"))
        yield


def item(log, key):
    return log.get("items", f"alpha:{key}")


def rounds(log, key):
    return [r for r in log.all("rounds") if r["item"] == f"alpha:{key}"]


def shown(log, key):
    """workshop status 里这件活的状态一栏。"""
    for line in status(log).splitlines():
        cells = line.split("\t")
        if cells[0] == f"alpha:{key}":
            return cells[2]
    raise AssertionError(f"状态里没有 alpha:{key}")


def submit(engine, key, advance, **extra):
    row = next(r for r in engine.active() if r["item"] == f"alpha:{key}")
    engine.submit(row["token"], dict(outcome="done", summary=f"完成{row['stage']}", advance=advance, **extra))
    return row


@pytest.fixture
def world(tmp_path, monkeypatch):
    """就地模式把活 7 推到「证据」并交好回执，活 8 刚放行还没派；再换成作业模式的引擎。"""
    monkeypatch.setenv("JOBTEST_DIR", str(tmp_path))
    configure(tmp_path, checks=[["/bin/sleep", "5"]])
    clock = Clock()
    log = Log(tmp_path / "workshop.db")
    inline = Engine(log, rules(), job_plugins.registry(), clock)
    inline.ingest(event("7"))
    inline.tick()
    for stage in ("说清楚", "执行", "证据"):
        submit(inline, "7", stage)
        inline.tick()
    assert item(log, "7")["stage"] == "证据"
    evidence = json.dumps({"schema": "issue-delivery/v1", "head": job_plugins.SHA})
    submit(inline, "7", "审查", evidence=[evidence])
    inline.ingest(event("8", ext_id="2"))
    inline.card(item(log, "7"), "hello", "notice", notice("作业测试的一条通知"))
    (tmp_path / "calls.log").unlink(missing_ok=True)

    def engine():
        jobs = Jobs(tmp_path / "jobs", [sys.executable, RUNNER, str(tmp_path)])
        return Engine(Log(tmp_path / "workshop.db"), rules(), job_plugins.registry(), clock, jobs=jobs)

    try:
        yield dict(path=tmp_path, clock=clock, log=log, engine=engine)
    finally:
        # 断言失败也放行子进程，避免它们等到兜底期限才退出。
        allow({"path": tmp_path}, *job_plugins.config().get("blocked", []))
        log.close()


def tick(world, engine, monkeypatch):
    """跑一拍：期间禁止等子进程和网络；返回墙钟毫秒和心跳。"""
    world["clock"].now += timedelta(seconds=1)
    began = time.monotonic()
    with no_waiting_or_network(monkeypatch):
        engine.tick()
    elapsed = (time.monotonic() - began) * 1000
    beat = json.loads((world["path"] / "heartbeat.json").read_text())
    assert not engine.control("@tick").get("failures"), engine.log.facts()[-1]
    return elapsed, beat


def wait(world, engine, monkeypatch, done, seconds=40):
    beats = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        elapsed, beat = tick(world, engine, monkeypatch)
        beats.append((elapsed, beat))
        if done():
            return beats
        time.sleep(0.01)
    raise AssertionError("等不到预期结果")


def test_slow_check_source_and_channel_do_not_hold_the_tick(world, monkeypatch):
    # 作业保持阻塞，直到已经看到另一件活被派出，不靠固定睡眠猜时序。
    configure(world["path"], blocked=["check", "source", "channel"], checks=[["/bin/sleep", "5"]])
    engine = world["engine"]()
    log = engine.log
    seen = {"label": False, "dispatched_during_check": False}

    def observe():
        checking = bool(job_plugins.calls("assess-start")) and not job_plugins.calls("assess-end")
        if checking and shown(log, "7") == "等检查":
            seen["label"] = True
        started = [r for r in rounds(log, "8") if r.get("started")]
        if checking and started:
            seen["dispatched_during_check"] = True
        if all(seen.values()) and job_plugins.calls("source-start") and job_plugins.calls("send-start"):
            assert not job_plugins.calls("sent") or (world["path"] / "allow-channel").exists()
            allow(world, "check", "source", "channel")
        return any(r["stage"] == "证据" and r["status"] == "ended" for r in rounds(log, "7"))

    beats = wait(world, engine, monkeypatch, observe)
    # 每一拍都在 100 毫秒内返回，心跳每拍都写、时间每拍都变。
    assert all(b["tick_ms"] <= LIMIT_MS for _, b in beats), [b["tick_ms"] for _, b in beats]
    assert all(elapsed <= LIMIT_MS for elapsed, _ in beats), [round(e) for e, _ in beats]
    assert len({b["at"] for _, b in beats}) == len(beats)
    # 慢检查跑着的时候，活 7 显示「等检查」，另一件空闲的活 8 照常被派出。
    assert seen["label"] and seen["dispatched_during_check"]
    # 检查跑完后的下一拍按结果结算：活 7 推进到审查；检查只跑了一次。
    assert item(log, "7")["stage"] == "审查"
    assert len(job_plugins.calls("assess-start")) == 1
    # 慢通道、慢来源也都在后台跑完，没挡住一拍。
    wait(world, engine, monkeypatch, lambda: log.get("cards", "alpha:7:hello")["sent"])
    assert job_plugins.calls("source-start")
    engine.log.close()


@pytest.mark.parametrize("finished_before_restart", [False, True])
def test_restart_recovers_running_or_finished_check_without_rerun(world, monkeypatch, finished_before_restart):
    configure(world["path"], blocked=["check"], checks=[["/bin/sleep", "5"]])
    engine = world["engine"]()
    wait(world, engine, monkeypatch, lambda: bool(job_plugins.calls("assess-start")))
    assert shown(world["log"], "7") == "等检查"
    assert not job_plugins.calls("assess-end")
    # 守护进程重启：丢掉引擎和作业对象，用同一个状态目录重新起。
    engine.log.close()
    del engine
    if finished_before_restart:
        allow(world, "check")
        deadline = time.monotonic() + 20
        while not job_plugins.calls("assess-end") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert job_plugins.calls("assess-end")
    revived = world["engine"]()
    if not finished_before_restart:
        tick(world, revived, monkeypatch)
        assert shown(revived.log, "7") == "等检查"
        assert not job_plugins.calls("assess-end")
        allow(world, "check")
    wait(world, revived, monkeypatch,
         lambda: any(r["stage"] == "证据" and r["status"] == "ended" for r in rounds(revived.log, "7")))
    assert item(revived.log, "7")["stage"] == "审查"
    assert len(job_plugins.calls("assess-start")) == 1
    revived.log.close()


def test_check_timeout_in_job_records_check_failed(world, monkeypatch):
    """真流程在作业里照规则 check_timeout 跑检查：超时记 exit 124、timed_out，这件活检查没过。"""
    monkeypatch.setenv("JOBTEST_REAL_FLOW", "1")
    engine = world["engine"]()
    log = engine.log
    wait(world, engine, monkeypatch, lambda: any(r["stage"] == "证据" and r["status"] == "ended" for r in rounds(log, "7")))
    result = next(f["body"] for f in log.facts("alpha:7") if f["kind"] == "round_result" and f["body"]["stage"] == "证据")
    reason = json.loads(result["reason"])
    assert reason["reason"] == "检查失败，退回执行修正"
    assert reason["runs"][0]["argv"] == ["/bin/sleep", "5"]
    assert reason["runs"][0]["exit_code"] == 124 and reason["runs"][0]["timed_out"]
    assert item(log, "7")["stage"] == "证据" and next(r for r in rounds(log, "7") if r["stage"] == "证据")["passed"] is None
    log.close()


def test_same_call_reuses_result_without_new_job(tmp_path, monkeypatch):
    """同一调用出过结果就复用，不另起作业；出错的结果取一次就清掉，下次重跑。"""
    monkeypatch.setenv("JOBTEST_DIR", str(tmp_path))
    configure(tmp_path, check_seconds=0)
    jobs = Jobs(tmp_path / "jobs", [sys.executable, RUNNER, str(tmp_path)])
    Log(tmp_path / "workshop.db").close()
    stage_item = {"id": "alpha:7", "stage": "证据"}
    args = (stage_item, None, {"name": "alpha"})
    with pytest.raises(Pending):
        jobs.call("flow", "flow", "assess", args, identity=["alpha:7", "证据", "t1"])
    deadline = time.monotonic() + 20
    while True:
        try:
            first = jobs.call("flow", "flow", "assess", args, identity=["alpha:7", "证据", "t1"])
            break
        except Pending:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    assert first.passed and len(job_plugins.calls("assess-start")) == 1
    assert jobs.call("flow", "flow", "assess", args, identity=["alpha:7", "证据", "t1"]) == first
    assert len(list((tmp_path / "jobs").iterdir())) == 1 and len(job_plugins.calls("assess-start")) == 1


def test_slow_process_creation_does_not_hold_tick(world, monkeypatch):
    """操作系统创建进程也会变慢；不能把这段等待留在一拍里。"""
    spawn = os.posix_spawnp
    callers = []
    main_thread = threading.get_ident()

    def slow_spawn(*args, **kwargs):
        callers.append(threading.get_ident())
        time.sleep(.2)
        return spawn(*args, **kwargs)

    monkeypatch.setattr(os, "posix_spawnp", slow_spawn)
    configure(world["path"], check_seconds=0)
    engine = world["engine"]()
    try:
        beats = wait(world, engine, monkeypatch,
                     lambda: any(r["stage"] == "证据" and r["status"] == "ended"
                                 for r in rounds(engine.log, "7")))
        assert callers and main_thread not in callers
        assert all(b["tick_ms"] <= LIMIT_MS and elapsed <= LIMIT_MS for elapsed, b in beats)
        assert item(engine.log, "7")["stage"] == "审查"
    finally:
        engine.jobs.launcher.shutdown(wait=True)
        engine.log.close()


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("child_finished", [False, True])
def test_job_is_recognized_or_cancelled_while_process_is_being_created(tmp_path, monkeypatch, cancel, child_finished):
    monkeypatch.setenv("JOBTEST_DIR", str(tmp_path))
    configure(tmp_path)
    Log(tmp_path / "workshop.db").close()
    entered, release = threading.Event(), threading.Event()
    spawn = os.posix_spawnp

    def held_spawn(*args, **kwargs):
        pid = spawn(*args, **kwargs) if child_finished else None
        entered.set()
        assert release.wait(10)
        return pid if child_finished else spawn(*args, **kwargs)

    monkeypatch.setattr(os, "posix_spawnp", held_spawn)
    jobs = Jobs(tmp_path / "jobs", [sys.executable, RUNNER, str(tmp_path)])
    revived = Jobs(jobs.directory, jobs.command)
    args = [{}, None]
    path = jobs.path("source", "source", "poll", args)
    try:
        with pytest.raises(Pending):
            jobs.call("source", "source", "poll", args)
        assert entered.wait(10)
        assert (path / "pid").read_text() == "-1"
        if child_finished:
            deadline = time.monotonic() + 10
            while not (path / "result.json").exists():
                assert time.monotonic() < deadline
                time.sleep(.01)
        with pytest.raises(Pending):
            revived.call("source", "source", "poll", args)
        assert revived.spawned == 0
        if cancel:
            jobs.kill(path)
        release.set()
        jobs.launcher.shutdown(wait=True)
        if cancel:
            assert not path.exists()
        else:
            deadline = time.monotonic() + 10
            while not (path / "result.json").exists():
                assert time.monotonic() < deadline
                time.sleep(.01)
            assert revived.call("source", "source", "poll", args).cursor == "after-batch"
            assert len(job_plugins.calls("source-start")) == 1
    finally:
        release.set()
        jobs.launcher.shutdown(wait=True)
        jobs.reap()
        for pid in jobs.children:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


def test_background_process_creation_failure_is_retryable(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("spawn unavailable")

    monkeypatch.setattr(os, "posix_spawnp", fail)
    jobs = Jobs(tmp_path / "jobs", [sys.executable])
    with pytest.raises(Pending):
        jobs.call("source", "source", "poll", [])
    jobs.launcher.shutdown(wait=True)
    with pytest.raises(Retryable, match="spawn unavailable"):
        jobs.call("source", "source", "poll", [])


@pytest.mark.parametrize("external", ["external:already-sent", "absent", None])
def test_finished_reconcile_is_consumed_once_and_delivery_continues(world, monkeypatch, external):
    """恢复 sending 卡片：后台对账的三种结果都要交给通道处理，不能立即过期。"""
    configure(world["path"], reconcile_result=external)
    engine = world["engine"]()
    key = "alpha:7:hello"
    engine.fact("card_changed", {"id": key, "sending": True}, "alpha:7")
    path = engine.jobs.path("channel", "channel", "reconcile", [key])

    def channels():
        engine.jobs.reap()
        with no_waiting_or_network(monkeypatch):
            engine.channels()

    def completed(path):
        deadline = time.monotonic() + 10
        while not (path / "result.json").exists():
            assert time.monotonic() < deadline, "后台作业未写回结果"
            time.sleep(.01)

    try:
        channels()
        completed(path)
        # 模拟下一拍甚至重启后才取结果，完成时间已经过去。
        engine.jobs.clock = lambda: time.time() + 60
        channels()
        assert not path.exists(), "对账结果应消费一次后清除"
        assert len(job_plugins.calls("reconcile")) == 1
        card = engine.log.get("cards", key)
        if external is None:
            assert not card["sent"]
            assert engine.control(f"reconcile:{key}")["count"] == 1
            assert not job_plugins.calls("send-start")
            # 不确定的结果不能被缓存；下一次要重新对账。
            channels()
            completed(path)
            channels()
            assert engine.control(f"reconcile:{key}")["count"] == 2
            assert len(job_plugins.calls("reconcile")) == 2
        else:
            if external == "absent":
                completed(engine.jobs.path("channel", "channel", "notify", [key]))
                channels()
                card = engine.log.get("cards", key)
                assert len(job_plugins.calls("sent")) == 1
                assert card["external"] == f"external:{key}"
            else:
                assert card["external"] == external
                assert not job_plugins.calls("send-start")
            assert card["sent"]
            channels()
            assert len(job_plugins.calls("reconcile")) == 1
    finally:
        engine.log.close()


def test_job_that_dies_without_result_is_retryable_and_overdue_job_is_killed(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBTEST_DIR", str(tmp_path))
    dying = Jobs(tmp_path / "jobs", [sys.executable, "-c", "pass"])
    with pytest.raises(Pending):
        dying.call("source", "source", "poll", ({"name": "alpha"}, None))
    deadline = time.monotonic() + 20
    with pytest.raises(Retryable, match="没写结果就退出"):
        while time.monotonic() < deadline:
            try:
                dying.call("source", "source", "poll", ({"name": "alpha"}, None))
            except Pending:
                time.sleep(0.01)
    slow = Jobs(tmp_path / "slow", [sys.executable, "-c", "import time; time.sleep(60)"], deadline=0.5)
    with pytest.raises(Pending):
        slow.call("source", "source", "poll", ({"name": "alpha"}, None))
    time.sleep(1)
    with pytest.raises(Retryable, match="没出结果"):
        slow.call("source", "source", "poll", ({"name": "alpha"}, None))
    assert not list((tmp_path / "slow").iterdir())


def test_workshop_job_command_runs_entry_point_plugin(tmp_path):
    """守护进程用的 workshop job 子命令：按入口点装插件、解码参数、写回结果。"""
    from workshop.core.models import Selection
    Log(tmp_path / "workshop.db").close()
    jobs = Jobs(tmp_path / "jobs", [sys.executable, "-m", "workshop.cli", "--state-dir", str(tmp_path), "job"])
    config = {"use": "writer", "family": "a", "billing": "subscription"}
    args = ({"name": "alpha"}, "执行", 1, [], [{"config": config, "available": True}])
    deadline = time.monotonic() + 20
    while True:
        try:
            choice = jobs.call("selector", "ordered", "select", args)
            break
        except Pending:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    assert isinstance(choice, Selection) and choice.config == config


def completed_item(world):
    """另建一件已收尾的活，和 fixture 里正在推进的两件并存。"""
    engine = Engine(world["log"], rules(), job_plugins.registry(), world["clock"])
    engine.ingest(event("9", ext_id="completed"))
    engine.change(item(world["log"], "9"), state="完成", stage="收尾",
                  workdir="/fake/completed", sessions={"writer": "session:9"})


@pytest.mark.parametrize("restart_at", ["hold-start", "release-start"])
@pytest.mark.parametrize("reason", ["", "仍有未交出的改动"])
def test_release_jobs_do_not_block_and_survive_restart(world, monkeypatch, restart_at, reason):
    configure(world["path"], blocked=["hold", "release"], release_reason=reason)
    if restart_at == "release-start":
        allow(world, "hold")
    completed_item(world)
    engine = world["engine"]()
    beats = wait(world, engine, monkeypatch,
                 lambda: bool(job_plugins.calls(restart_at)) and bool(rounds(engine.log, "8")))
    assert item(engine.log, "9")["workdir"] == "/fake/completed"
    engine.log.close()
    revived = world["engine"]()
    elapsed, beat = tick(world, revived, monkeypatch)
    beats.append((elapsed, beat))
    assert not job_plugins.calls(restart_at.replace("-start", "-end"))
    assert item(revived.log, "9")["workdir"] == "/fake/completed"
    allow(world, "hold", "release")
    beats += wait(world, revived, monkeypatch, lambda: bool(item(revived.log, "9").get("workdir_released")))
    assert all(elapsed <= LIMIT_MS and beat["tick_ms"] <= LIMIT_MS for elapsed, beat in beats), [
        {"wall_ms": round(elapsed, 2), "tick_ms": beat["tick_ms"]} for elapsed, beat in beats]
    assert len(job_plugins.calls("hold-start")) == len(job_plugins.calls("release-start")) == 1
    assert rounds(revived.log, "8")  # 其他活仍能派出。
    row = item(revived.log, "9")
    assert row["workdir"] == ("/fake/completed" if reason else None)
    assert row["workdir_released"] == ("留着：" + reason if reason else "已收回")
    tick(world, revived, monkeypatch)
    cards = [c for c in revived.log.all("cards") if "workdir-kept" in c["id"]]
    assert len(cards) == bool(reason)
    assert len(job_plugins.calls("release-start")) == 1
    revived.log.close()


def test_release_hold_is_rechecked_after_delay_and_new_completion(world, monkeypatch):
    configure(world["path"], hold_reason="还要留")
    completed_item(world)
    engine = world["engine"]()
    wait(world, engine, monkeypatch, lambda: bool(item(engine.log, "9").get("release_hold")))
    assert not job_plugins.calls("release-start")
    configure(world["path"])
    tick(world, engine, monkeypatch)
    assert len(job_plugins.calls("hold-start")) == 1
    world["clock"].now += timedelta(hours=1)
    wait(world, engine, monkeypatch, lambda: item(engine.log, "9").get("workdir_released") == "已收回")
    assert len(job_plugins.calls("hold-start")) == 2
    # 返工后再次收尾，同名目录必须重新判断，不能复用上一次的收回结果。
    engine.change(item(engine.log, "9"), workdir="/fake/completed", workdir_released=None,
                  pending_episode=1)
    wait(world, engine, monkeypatch, lambda: item(engine.log, "9").get("workdir_released") == "已收回")
    assert len(job_plugins.calls("hold-start")) == 3
    assert len(job_plugins.calls("release-start")) == 2
    engine.log.close()


def test_rework_waits_for_active_release_before_launching(world, monkeypatch):
    configure(world["path"], blocked=["release"])
    completed_item(world)
    engine = world["engine"]()
    wait(world, engine, monkeypatch, lambda: bool(job_plugins.calls("release-start")))
    engine.change(item(engine.log, "9"), pending_rework=True)
    tick(world, engine, monkeypatch)
    assert not rounds(engine.log, "9")
    assert not job_plugins.calls("release-end")
    allow(world, "release")
    wait(world, engine, monkeypatch, lambda: any(r.get("started") for r in rounds(engine.log, "9")))
    calls = job_plugins.calls()
    released = next(i for i, row in enumerate(calls) if row[0] == "release-end")
    launched = next(i for i, row in enumerate(calls) if row[0] == "launch" and row[2] == "alpha:9")
    assert released < launched
    assert len(job_plugins.calls("release-start")) == 1
    engine.log.close()
