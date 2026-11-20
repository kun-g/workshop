"""核心 R33：同一件活上还没处理的事件攒成一批跑一轮。

全部用假时钟，不用 sleep（E8）；窗口用默认 500 毫秒（规则不写 batch_window_ms）。
流程把每一轮判成「等资料」：来源有新事件或还有没处理的事件，活回到空闲再起一轮，
这正是「用户连发几条、执行者被叫起来几次」的路径。
"""
from datetime import timedelta

import pytest

from conftest import Clock, build, configuration, event
from workshop.core.models import Evidence
from workshop.core.ports import InputBlocked
from workshop.core.rules import Rules

T0 = Clock().now


def engine_at(path):
    config = configuration()
    config["limits"] = {k: v for k, v in config["limits"].items() if k != "batch_window_ms"}
    engine = build(path, config)
    assert engine.batch_window() == 500

    def assess(item, receipt, target):
        if receipt.outcome == "failed":
            return Evidence()
        return Evidence(awaits_update=True, reason="等来源补充")

    engine.plugins.get("flow", "flow").assess = assess
    return engine


@pytest.fixture
def engine(tmp_path):
    engine = engine_at(tmp_path)
    yield engine
    engine.log.close()


def at(engine, ms):
    engine.clock.now = T0 + timedelta(milliseconds=ms)


def put(engine, name, key="one", action="update", **extra):
    """写一条来源事件并跑一拍；返回它的事实序号。活还不存在时先放行建活。"""
    if engine.log.get("items", f"alpha:{key}") is None:
        action = "ready"
    assert engine.ingest(event(key=key, ext_id=name, action=action, **extra))
    seq = engine.log.db.execute("SELECT max(seq) FROM facts WHERE kind='event' AND json_extract(body, '$.ext_id')=?",
                                (name,)).fetchone()[0]
    engine.tick()
    return seq


def rounds(engine, key="one"):
    return [row for row in engine.log.all("rounds") if row["item"] == f"alpha:{key}"]


def batches(engine, key="one"):
    return [row["batch"] for row in rounds(engine, key)]


def running(engine, key="one"):
    return [row for row in engine.active() if row["item"] == f"alpha:{key}"]


def done(engine, key="one", outcome="done"):
    """当前这一轮交回执并结算。"""
    [row] = running(engine, key)
    assert engine.submit(row["token"], {"outcome": outcome, "summary": "看过这一批"})
    engine.tick()
    return row


def restart(engine, path):
    """守护进程被强杀后重新拉起：同一份状态库，新的进程内状态；原执行者会话也已不在。"""
    now = engine.clock.now
    engine.log.close()
    fresh = engine_at(path)
    fresh.clock.now = now
    return fresh


def item(engine, key="one"):
    return engine.log.get("items", f"alpha:{key}")


def watermark(engine, key="one"):
    return item(engine, key).get("watermark", 0)


def test_fixed_window_from_first_event_is_not_extended(engine):
    e1 = put(engine, "e1")
    at(engine, 300)
    e2 = put(engine, "e2")
    at(engine, 499)
    engine.tick()
    assert batches(engine) == []
    at(engine, 500)
    engine.tick()
    assert batches(engine) == [[e1, e2]]
    row = running(engine)[0]
    assert row["at"] == "2026-11-13T08:00:00.500000+00:00"
    assert engine.log.brief(row)["batch"] == [e1, e2]
    at(engine, 550)
    done(engine)
    assert item(engine)["state"] == "等资料"
    at(engine, 600)
    e3 = put(engine, "e3")
    at(engine, 900)
    e4 = put(engine, "e4")
    at(engine, 1099)
    engine.tick()
    assert batches(engine) == [[e1, e2]]
    at(engine, 1100)
    engine.tick()
    assert batches(engine) == [[e1, e2], [e3, e4]]
    assert running(engine)[0]["at"] == "2026-11-13T08:00:01.100000+00:00"


def test_full_batch_starts_without_waiting_and_rest_goes_next(engine):
    seqs = [put(engine, f"e{n}") for n in range(1, 26)]
    assert batches(engine) == [seqs[:20]]
    assert item(engine)["state"] == "跑着"
    # 第一轮的说明不带第 21 到 25 条的正文：它们进下一轮。
    brief = engine.log.brief(running(engine)[0])
    assert {m["seq"] for m in brief["materials"]} & set(seqs) == set(seqs[:20])
    done(engine)
    assert batches(engine) == [seqs[:20], seqs[20:]]
    done(engine)
    assert len(rounds(engine)) == 2 and item(engine)["queue"] == []


def test_window_zero_on_another_item_is_not_blocked(engine):
    a = put(engine, "a1", key="a")
    at(engine, 50)
    b = put(engine, "b1", key="b", batch_window_ms=0)
    assert batches(engine, "b") == [[b]]
    assert running(engine, "b")[0]["at"] == "2026-11-13T08:00:00.050000+00:00"
    assert batches(engine, "a") == []
    at(engine, 500)
    engine.tick()
    assert batches(engine, "a") == [[a]]


def test_events_during_a_running_round_become_one_batch(engine):
    e1 = put(engine, "e1")
    at(engine, 300)
    e2 = put(engine, "e2")
    at(engine, 500)
    engine.tick()
    later = []
    for ms, name in ((600, "e3"), (2000, "e4"), (3500, "e5")):
        at(engine, ms)
        later.append(put(engine, name))
        assert len(engine.active()) == 1  # t=5s 前没有第二个并发轮。
    at(engine, 4999)
    engine.tick()
    assert batches(engine) == [[e1, e2]]
    at(engine, 5000)
    done(engine)
    assert batches(engine) == [[e1, e2], later]
    assert running(engine)[0]["at"] == "2026-11-13T08:00:05+00:00"
    done(engine)
    at(engine, 60_000)
    engine.tick()
    assert batches(engine) == [[e1, e2], later]


@pytest.mark.parametrize("executor_survives", [True, False])
def test_kill_during_running_round_keeps_the_in_flight_boundary(tmp_path, executor_survives):
    engine = engine_at(tmp_path)
    e1 = put(engine, "e1")
    at(engine, 300)
    e2 = put(engine, "e2")
    at(engine, 500)
    engine.tick()
    later = []
    for ms, name in ((600, "e3"), (2000, "e4"), (3500, "e5")):
        at(engine, ms)
        later.append(put(engine, name))
    [row] = running(engine)
    old = engine.plugins.get("executor", row["executor"])
    at(engine, 4000)
    engine = restart(engine, tmp_path)
    if executor_survives:
        # 只有守护进程被杀：执行者会话还在跑，新进程认回它，到 t=5s 交回执。
        engine.plugins.register("executor", row["executor"], old)
        engine.tick()
        assert batches(engine) == [[e1, e2]]
        at(engine, 5000)
        done(engine)
    else:
        # 会话也没了：新进程判这一轮失败，按退避重试；重跑的仍是在途那一批。
        engine.tick()
        assert rounds(engine)[0]["outcome"] == "failed"
        assert item(engine)["inflight"] == [e1, e2] and watermark(engine) < e1
        at(engine, 4000 + 300_000)
        engine.tick()
        assert batches(engine) == [[e1, e2], [e1, e2]]
        done(engine)
    assert batches(engine)[-1] == later
    done(engine)
    assert item(engine)["queue"] == [] and watermark(engine) == later[-1]
    engine.log.close()


def test_limit_applies_after_a_running_round(engine):
    e1 = put(engine, "e1", batch_window_ms=0)
    assert batches(engine) == [[e1]]
    seqs = [put(engine, f"x{n}") for n in range(1, 26)]
    done(engine)
    assert batches(engine) == [[e1], seqs[:20]]
    done(engine)
    assert batches(engine) == [[e1], seqs[:20], seqs[20:]]


def test_round_only_marks_the_events_it_took(engine):
    e1 = put(engine, "e1", batch_window_ms=0)
    e2 = put(engine, "e2")
    assert batches(engine) == [[e1]]
    assert [e["seq"] for e in item(engine)["queue"]] == [e1, e2]
    done(engine)
    assert [e["seq"] for e in item(engine)["queue"]] == [e2]
    assert watermark(engine) == e1
    assert batches(engine) == [[e1], [e2]]
    done(engine)
    assert item(engine)["queue"] == [] and watermark(engine) == e2


def test_unbatchable_event_runs_alone(engine):
    e10 = put(engine, "e10")
    e11 = put(engine, "e11", batchable=False)
    e12 = put(engine, "e12")
    # 后面是不可攒批的：前面这一批不会再变，不等窗口。
    assert batches(engine) == [[e10]]
    while running(engine):
        done(engine)
    assert batches(engine) == [[e10], [e11], [e12]]
    taken = [seq for batch in batches(engine) for seq in batch]
    assert sorted(taken) == [e10, e11, e12] and len(taken) == len(set(taken))
    assert item(engine)["queue"] == []


def test_unbatchable_event_waits_until_it_is_the_smallest(engine):
    e1 = put(engine, "e1", batchable=False)
    assert batches(engine) == [[e1]]
    e2 = put(engine, "e2")
    e3 = put(engine, "e3", batchable=False)
    done(engine)
    assert batches(engine) == [[e1], [e2]]
    done(engine)
    assert batches(engine) == [[e1], [e2], [e3]]


def test_skipped_event_never_batched_and_watermark_moves_contiguously(engine):
    e14 = put(engine, "e14")
    e15 = put(engine, "e15")
    engine.skip_event("alpha:one", e15, "陈旧：只留痕")
    assert watermark(engine) < e14
    at(engine, 500)
    engine.tick()
    assert batches(engine) == [[e14]]
    assert watermark(engine) < e14
    assert e15 not in {m["seq"] for m in engine.log.brief(running(engine)[0])["materials"]}
    done(engine)
    assert watermark(engine) == e15 and item(engine)["queue"] == []
    assert all(e15 not in batch for batch in batches(engine))
    with pytest.raises(ValueError):
        engine.skip_event("alpha:one", e15, "已经不在队列里")


def test_skip_does_not_pass_unconsumed_event(engine):
    e1 = put(engine, "e1")
    e2 = put(engine, "e2")
    e3 = put(engine, "e3")
    engine.skip_event("alpha:one", e2, "陈旧")
    assert watermark(engine) < e1
    at(engine, 500)
    engine.tick()
    assert batches(engine) == [[e1, e3]]
    done(engine)
    assert watermark(engine) == e3


def test_failure_before_start_leaves_watermark(engine, monkeypatch):
    executor = engine.plugins.get("executor", "writer")

    def blocked(*args):
        raise InputBlocked("派活输入阻塞：测试")

    monkeypatch.setattr(executor, "launch", blocked)
    e1 = put(engine, "e1", batch_window_ms=0)
    assert item(engine)["state"] == "卡住"
    assert watermark(engine) == 0 and [e["seq"] for e in item(engine)["queue"]] == [e1]
    monkeypatch.undo()
    # 放行把活放回空闲；重来的仍是原来那一批，后到的放行事件留给下一轮。
    e2 = put(engine, "e2", action="ready", batch_window_ms=0)
    assert batches(engine) == [[e1], [e1]]
    done(engine)
    assert batches(engine) == [[e1], [e1], [e2]]


def test_unavailable_executor_leaves_watermark(engine):
    engine.plugins.get("executor", "writer").enabled = False
    e1 = put(engine, "e1", batch_window_ms=0)
    assert item(engine)["state"] == "卡住" and batches(engine) == []
    assert watermark(engine) == 0 and [e["seq"] for e in item(engine)["queue"]] == [e1]


def test_failure_after_start_retries_same_batch(engine):
    e1 = put(engine, "e1")
    e2 = put(engine, "e2")
    at(engine, 500)
    engine.tick()
    e3 = put(engine, "e3")
    done(engine, outcome="failed")
    assert item(engine)["state"] == "定时" and watermark(engine) < e1
    at(engine, 500 + 300_000)
    engine.tick()
    assert batches(engine) == [[e1, e2], [e1, e2]]
    done(engine)
    assert batches(engine) == [[e1, e2], [e1, e2], [e3]]
    done(engine)
    taken = [seq for batch in batches(engine)[1:] for seq in batch]
    assert taken == [e1, e2, e3] and item(engine)["queue"] == []


@pytest.mark.parametrize("batchable", [True, False])
def test_restart_reruns_exactly_the_in_flight_batch(tmp_path, batchable):
    engine = engine_at(tmp_path)
    e10 = put(engine, "e10")
    e11 = put(engine, "e11")
    at(engine, 500)
    engine.tick()
    assert batches(engine) == [[e10, e11]]
    e12 = put(engine, "e12", batchable=batchable)
    e13 = put(engine, "e13") if not batchable else None
    engine = restart(engine, tmp_path)
    engine.tick()
    at(engine, 500 + 300_000)
    engine.tick()
    assert batches(engine) == [[e10, e11], [e10, e11]]
    done(engine)
    assert batches(engine)[2] == [e12]
    if not batchable:
        done(engine)
        assert batches(engine)[3] == [e13]
    engine.log.close()


def test_restart_after_completion_fact_does_not_rerun(tmp_path):
    engine = engine_at(tmp_path)
    e10 = put(engine, "e10")
    e11 = put(engine, "e11")
    at(engine, 500)
    engine.tick()
    e12 = put(engine, "e12")
    [row] = running(engine)
    engine.submit(row["token"], {"outcome": "done", "summary": "看过这一批"})
    # 只结算这一轮（写下完成事实），随即被强杀：还没来得及起下一轮。
    engine.finish(engine.log.get("rounds", row["token"]))
    assert any(f["kind"] == "batch_done" for f in engine.log.facts())
    assert batches(engine) == [[e10, e11]] and not running(engine)
    engine = restart(engine, tmp_path)
    engine.tick()
    assert batches(engine) == [[e10, e11], [e12]]
    engine.log.close()


def test_rework_events_after_in_flight_batch_still_get_their_round(engine):
    e1 = put(engine, "e1")
    at(engine, 500)
    engine.tick()
    e2 = put(engine, "e2", action="rework")
    done(engine, outcome="failed")
    # 返工先把活拉回执行；在途那一批原样重跑，返工事件自己的那一批随后跟上，不落空。
    assert batches(engine) == [[e1], [e1]]
    done(engine)
    assert batches(engine) == [[e1], [e1], [e2]] and not item(engine)["pending_rework"]


def test_window_wakes_the_main_loop(engine):
    put(engine, "e1")
    at(engine, 100)
    engine.tick_started = engine.clock()
    assert engine.wait_timeout() == pytest.approx(0.4)


def test_rules_and_events_reject_bad_windows():
    config = configuration()
    for bad in (-1, 501, 0.5, "500"):
        config["limits"]["batch_window_ms"] = bad
        with pytest.raises(ValueError):
            Rules.validate(config)
    for bad in ({"batch_window_ms": 501}, {"batch_window_ms": -1}, {"batchable": "no"}):
        with pytest.raises(ValueError):
            event(**bad).validate()


def test_replay_matches_live_view(engine):
    put(engine, "e1")
    put(engine, "e2", batchable=False)
    at(engine, 500)
    engine.tick()
    done(engine)
    put(engine, "e3")
    live = item(engine)
    engine.log.rebuild()
    assert item(engine) == live
