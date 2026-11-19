"""轮询失败标记：启动时偶发不记；轮询成功或这一轮结束就清；存量自清（介入点 R11）。"""
from datetime import timedelta

import pytest

from conftest import current, event
from workshop.core.managers import Store, observe_failures
from workshop.core.ports import Retryable


def started(engine):
    engine.ingest(event())
    engine.tick()
    row = current(engine)
    assert row["status"] == "running"
    return row


def flaky(engine, monkeypatch, times):
    """接下来 times 次 poll 报看不清，之后照常。"""
    writer = engine.plugins.get("executor", "writer")
    original = writer.poll
    left = [times]

    def poll(token, started):
        if left[0]:
            left[0] -= 1
            raise Retryable("启动结果或进程状态无法确认；保留占位")
        return original(token, started)
    monkeypatch.setattr(writer, "poll", poll)


def marker(engine, token):
    return bool(engine.control(f"delivery:poll:{token}").get("failed"))


def concern(engine, tmp_path, token):
    store = Store(tmp_path / "managers")
    try:
        observe_failures(store, engine.log, "core", engine.clock().isoformat(), engine.rules)
        rows = {r["id"]: r for r in store.all()}
        row = rows.get(f"fault:delivery:poll:{token}")
        return row and row["active"]
    finally:
        store.close()


def test_one_retryable_then_success_records_nothing(engine, monkeypatch):
    row = started(engine)
    flaky(engine, monkeypatch, 2)
    engine.tick()
    engine.tick()
    assert not marker(engine, row["token"])
    engine.tick()  # 成功一次，重新计数
    assert engine.log.get("rounds", row["token"])["poll_retries"] == 0
    flaky(engine, monkeypatch, 2)
    engine.tick()
    engine.tick()
    assert not marker(engine, row["token"])


def test_three_consecutive_retryable_records_marker_then_success_clears(engine, monkeypatch, tmp_path):
    row = started(engine)
    flaky(engine, monkeypatch, 5)
    for _ in range(3):
        engine.tick()
    assert marker(engine, row["token"])
    assert concern(engine, tmp_path, row["token"])
    engine.tick()
    engine.tick()
    # 计数封顶，不每拍写一条事实。
    assert engine.log.get("rounds", row["token"])["poll_retries"] == 3
    engine.tick()  # 第 6 次 poll 返回 running
    assert not marker(engine, row["token"])
    assert not concern(engine, tmp_path, row["token"])


def end_by_receipt(engine, row):
    engine.submit(row["token"], {"outcome": "done", "summary": "完成", "advance": "说清楚"})


def end_by_abort(engine, row):
    engine.abort(row["token"])


def end_by_deadline(engine, row):
    engine.clock.now += timedelta(days=1)


def end_by_exit(engine, row):
    engine.plugins.get("executor", "writer").live.discard(row["token"])


@pytest.mark.parametrize("end", [end_by_receipt, end_by_abort, end_by_deadline, end_by_exit])
def test_round_end_clears_marker_and_resolves_concern(engine, monkeypatch, tmp_path, end):
    row = started(engine)
    flaky(engine, monkeypatch, 3)
    for _ in range(3):
        engine.tick()
    assert marker(engine, row["token"]) and concern(engine, tmp_path, row["token"])
    end(engine, row)
    engine.tick()
    assert engine.log.get("rounds", row["token"])["status"] == "ended"
    assert not marker(engine, row["token"])
    assert not concern(engine, tmp_path, row["token"])


def test_leftover_marker_of_ended_round_clears_on_next_tick(engine, tmp_path):
    row = started(engine)
    end_by_receipt(engine, row)
    engine.tick()
    assert engine.log.get("rounds", row["token"])["status"] == "ended"
    # 旧代码留下的残留：已结束的轮和已不存在的轮各一条。
    for token in (row["token"], "gone"):
        engine.fact("control", {"id": f"delivery:poll:{token}", "failed": True, "since": "2026-11-19T03:10:20+00:00"})
    assert concern(engine, tmp_path, row["token"]) and concern(engine, tmp_path, "gone")
    engine.tick()
    assert not marker(engine, row["token"]) and not marker(engine, "gone")
    assert not concern(engine, tmp_path, row["token"]) and not concern(engine, tmp_path, "gone")


def test_marker_of_running_round_is_kept(engine, monkeypatch):
    row = started(engine)
    flaky(engine, monkeypatch, 10)
    for _ in range(3):
        engine.tick()
    engine.tick()
    assert marker(engine, row["token"])
