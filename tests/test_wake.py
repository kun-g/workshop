from contextlib import contextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import select
import socket
import tempfile
import time

import pytest

from conftest import build, configuration, current, event, hold_for_kun, reach
from workshop import cli
from workshop.core.jobs import Jobs, Pending, execute, write
from workshop.core.log import Log
from workshop.core.models import Poll, stamp
from workshop.core.wake import Mailbox, Wake, ring


@pytest.fixture
def state():
    # macOS 的 Unix 地址长度有限；不用 pytest 的长测试名组成地址。
    with tempfile.TemporaryDirectory(prefix="ws-wake-", dir="/tmp") as directory:
        yield Path(directory)


def test_mailbox_absent_stale_full_and_single_owner(state):
    ring(state)
    with Mailbox(state) as box:
        ring(state, "source:alpha:source")
        assert box.wait(0).messages == {"source:alpha:source"}
        with pytest.raises(BlockingIOError):
            Mailbox(state)
        ring(state, "still-alive")
        assert box.wait(0).messages == {"still-alive"}
        for _ in range(1000):
            ring(state, "full")
        assert "full" in box.wait(0).messages
    assert not (state / "wake.sock").exists()
    # 模拟接收进程崩溃留下的 socket。
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(state / "wake.sock"))
    sock.close()
    ring(state)
    with Mailbox(state) as box:
        ring(state)
        assert box.wait(0).messages == {""}


@pytest.mark.skipif(not hasattr(select, "kqueue"), reason="文件监听需要 kqueue")
def test_files_in_place_replace_delete_recreate_and_missing_parent(state):
    file = state / "answers.json"
    file.write_text("one")
    missing = state / "new" / "answers.json"
    with Mailbox(state, [file, missing]) as box:
        file.write_text("two")
        assert box.wait(0).paths == {file}
        temp = state / "replacement"
        temp.write_text("three")
        temp.replace(file)
        assert box.wait(0).paths == {file}
        file.write_text("four")
        assert box.wait(0).paths == {file}
        file.unlink()
        assert box.wait(0).paths == {file}
        file.write_text("five")
        assert box.wait(0).paths == {file}
        missing.parent.mkdir()
        assert not box.wait(0).paths
        missing.write_text("answer")
        assert box.wait(0).paths == {missing}
        (state / "unrelated").touch()
        assert not box.wait(0).paths


class StoredJobs(Jobs):
    def start(self, path, spec):
        path.mkdir(parents=True, exist_ok=True)
        write(path / "spec.json", spec)
        self.spawned += 1


@contextmanager
def jobs_in(state, **kwargs):
    jobs = StoredJobs(state / "jobs", [], **kwargs)
    try:
        yield jobs
    finally:
        jobs.launcher.shutdown(wait=True)


def completed(jobs, port, name, identity, value=Poll([], None), finished=None):
    from workshop.core.jobs import encode
    path = jobs.path(port, name, "poll", identity)
    path.mkdir(parents=True, exist_ok=True)
    write(path / "result.json", {"ok": encode(value), "finished": jobs.clock() if finished is None else finished})
    return path


def test_named_source_invalidates_only_matching_target_and_source(state):
    engine = build(state)
    with jobs_in(state) as jobs:
        engine.jobs = jobs
        first = completed(jobs, "source", "source", ["alpha"])
        other_target = completed(jobs, "source", "source", ["beta"])
        wait = completed(jobs, "wait", "wait", ["bind:decision", 0])
        engine.awaken(Wake(messages={"source:alpha:source", "source:unknown:source", ""}), {})
        assert (first / "invalidated").exists()
        assert not (other_target / "invalidated").exists()
        assert not (wait / "invalidated").exists()
        with pytest.raises(Pending):
            jobs.call("source", "source", "poll", [{}, None], identity=["alpha"])
        assert (first / "spec.json").exists()
        assert not (first / "result.json").exists()
    engine.log.close()


def test_inflight_invalidation_discards_late_old_result(state):
    with jobs_in(state) as jobs:
        with pytest.raises(Pending):
            jobs.call("source", "source", "poll", [{}, None], identity=["alpha"])
        jobs.invalidate("source", "source", ["alpha"])
        path = completed(jobs, "source", "source", ["alpha"], Poll([], "old"))
        with pytest.raises(Pending):
            jobs.call("source", "source", "poll", [{}, None], identity=["alpha"])
        assert not (path / "result.json").exists()
        completed(jobs, "source", "source", ["alpha"], Poll([], "new"))
        assert jobs.call("source", "source", "poll", [{}, None], identity=["alpha"]).cursor == "new"


def test_deadline_and_expiry_are_exact_and_do_not_keep_old_results_awake(state):
    engine = build(state)
    now = engine.clock()
    with jobs_in(state, clock=lambda: engine.clock().timestamp()) as jobs:
        engine.jobs = jobs
        assert engine.wait_timeout() == 60
        engine.ingest(event())
        item = engine.log.get("items", "alpha:one")
        engine.timer(item, "timer", "+40s", "later")
        engine.fact("control", {"id": "retry:source:alpha:source", "at": stamp(now + timedelta(seconds=30))})
        assert engine.wait_timeout() == 30
        completed(jobs, "source", "source", ["alpha"])
        jobs.call("source", "source", "poll", [{}, None], identity=["alpha"])
        assert engine.wait_timeout() == 20
        engine.clock.now += timedelta(seconds=20)
        assert jobs.wait_timeout() == 0
        with pytest.raises(Pending):
            jobs.call("source", "source", "poll", [{}, None], identity=["alpha"])
        jobs.reap()
        assert engine.wait_timeout() == 10
        # 没有在本拍使用的过期结果，不参与截止时间，避免一直空转。
        completed(jobs, "source", "unused", ["alpha"], finished=0)
        assert jobs.wait_timeout() == 60
        jobs.again = True
        assert engine.wait_timeout() == 0
    engine.log.close()


def test_optional_paths_and_wait_invalidation(state):
    engine = build(state)
    engine.ingest(event())
    port, _ = hold_for_kun(engine)
    port.wake_paths = lambda config: [str(state / "answers.json")]
    source = engine.plugins.get("source", "source")
    source.wake_paths = lambda: [str(state / "source.json")]
    paths = engine.wake_paths()
    assert paths[state / "answers.json"] == {("wait", "wait", ())}
    assert paths[state / "source.json"] == {("source", "source", ("alpha",))}
    with jobs_in(state) as jobs:
        engine.jobs = jobs
        cached = completed(jobs, "wait", "wait", ["bind:decision", 0])
        engine.awaken(Wake(paths={state / "answers.json"}), paths)
        assert (cached / "invalidated").exists()
    port.wake_paths = None
    source.wake_paths = None
    assert engine.wake_paths() == {}
    engine.log.close()


def test_cli_rings_only_after_successful_event_and_receipt_commit(state, monkeypatch):
    engine = build(state)
    reach(engine, "执行")
    row = current(engine)
    monkeypatch.setattr(cli, "Registry", lambda: engine.plugins)
    monkeypatch.delenv("WORKSHOP_ROUND_TOKEN", raising=False)
    snapshots = []
    def check_commit(directory, what=""):
        # 独立连接看得到，证明事务已经提交。
        log = Log(state / "workshop.db")
        snapshots.append(log.facts())
        log.close()
    monkeypatch.setattr(cli, "ring", check_commit)
    prefix = ["--state-dir", str(state)]
    assert cli.main([*prefix, "event", "--json", json.dumps(dict(source="test", ext_id="wake", target="alpha", object_key="two", title="test", body="data"))]) == 0
    assert any(f["ext_id"] == "wake" for f in snapshots[-1])
    assert cli.main([*prefix, "receipt", row["token"], "--json", json.dumps({"outcome": "nothing", "summary": "test"})]) == 0
    assert any(f["kind"] == "receipt" and f["body"]["token"] == row["token"] for f in snapshots[-1])
    before = len(snapshots)
    assert cli.main([*prefix, "receipt", "missing", "--json", '{}']) == 2
    assert len(snapshots) == before
    engine.log.close()


def test_result_writer_rings_after_atomic_publish(state):
    engine = build(state)
    directory = state / "jobs" / "example"
    directory.mkdir(parents=True)
    write(directory / "spec.json", {"port": "source", "name": "source", "method": "poll", "args": [{}, None]})
    with Mailbox(state) as box:
        execute(directory, engine.plugins)
        assert box.wait(0).messages == {""}
        assert json.loads((directory / "result.json").read_text())["ok"]
    engine.log.close()


def test_heartbeat_measures_receive_to_tick_start(state):
    engine = build(state)
    engine.tick(time.monotonic())
    beat = json.loads((state / "heartbeat.json").read_text())
    assert beat["wake_ms"] >= 0
    assert beat["tick_ms"] >= 0
    engine.log.close()


def test_immediate_error_wake_does_not_spin_retries(state):
    from workshop.core.ports import Retryable
    now = [100.0]
    with jobs_in(state, clock=lambda: now[0]) as jobs:
        path = jobs.path("wait", "wait", "poll", ["poll"])
        for delay in (1, 2, 4):
            path.mkdir(parents=True, exist_ok=True)
            write(path / "result.json", {"error": {"kind": "Retryable", "message": "offline"}, "finished": now[0]})
            with pytest.raises(Retryable, match="offline"):
                jobs.call("wait", "wait", "poll", [[]], identity=["poll"])
            jobs.reap()
            with pytest.raises(Pending):
                jobs.call("wait", "wait", "poll", [[]], identity=["poll"])
            assert not path.exists()
            assert jobs.wait_timeout() == delay
            now[0] += delay
        completed(jobs, "wait", "wait", ["poll"], [])
        assert jobs.call("wait", "wait", "poll", [[]], identity=["poll"]) == []
        assert not jobs.retries


@pytest.mark.skipif(not hasattr(select, "kqueue"), reason="文件监听需要 kqueue")
def test_change_between_snapshot_and_registration_is_not_lost(state, monkeypatch):
    file = state / "answer"
    file.write_text("before")
    rearm = Mailbox.rearm
    def changed_before_registration(box):
        file.write_text("after")
        rearm(box)
    monkeypatch.setattr(Mailbox, "rearm", changed_before_registration)
    with Mailbox(state, [file]) as box:
        # 把内核等待替换成拒绝阻塞的替身，丢失启动窗口的实现会在这里失败。
        queue = box.queue
        class NoWait:
            def control(self, *args):
                pytest.fail("已经变更的文件不应进入内核等待")
            def close(self):
                pass
        monkeypatch.setattr(box, "rearm", lambda: None)
        box.queue = NoWait()
        try:
            assert box.wait(60).paths == {file}
        finally:
            box.queue = queue
