"""核心 R27：事件进车间 1 秒内开始处理，一拍 ≤100 毫秒。

11-16 11:45 的 2.3 秒有三处来源，各用闸门复现、不用 sleep（E8）：
- 一拍里别的步骤慢：外部事件的归件排在这些步骤之前；
- 每 10 分钟一次的作业目录清理在一拍里删上千个目录：清理移出主线程；
- 对账不确定的卡每拍起一个作业、作业完成又叫醒主循环，守护进程一刻不停：告警后按间隔再对账。
"""
from datetime import timedelta
import threading
import time

from conftest import build, event, reach
from workshop.core import jobs as job_module
from workshop.core.jobs import Jobs, write
from workshop.core.cards import question


GATE_SECONDS = 10  # 只作兜底：闸门没放开时测试失败而不是挂住。


def test_external_event_is_classified_before_slow_steps(tmp_path):
    """来源读得再慢，也在归件之后；归件不等它。"""
    engine = build(tmp_path)
    engine.ingest(event())
    order = []
    flow = engine.plugins.get("flow", "flow")
    source = engine.plugins.get("source", "source")
    classify, poll = flow.classify, source.poll
    gate = threading.Event()

    def slow_poll(target, cursor):
        order.append("sources")
        assert gate.wait(GATE_SECONDS), "测试闸门没有放开"
        return poll(target, cursor)

    def watched(event, item, target, find_external=None):
        order.append("classify")
        # 归件开始时慢来源还没被调用：它在后面，不挡在事件前面。
        assert "sources" not in order
        gate.set()
        return classify(event, item, target, find_external)

    source.poll = slow_poll
    flow.classify = watched
    engine.ingest(event(ext_id="external-1", action="update"), ingress="external")
    engine.tick()
    assert order[:2] == ["classify", "sources"]
    assert any(f["kind"] == "event_classified" for f in engine.log.facts())
    engine.log.close()


class StoredJobs(Jobs):
    def start(self, path, spec):
        path.mkdir(parents=True, exist_ok=True)
        write(path / "spec.json", spec)
        self.spawned += 1


def test_job_sweep_runs_outside_the_tick(tmp_path, monkeypatch):
    """清理卡在闸门上时，这一拍照样结束，事件照样归件；清理放开后旧目录才删。"""
    now = [1_000_000.0]
    engine = build(tmp_path)
    jobs = StoredJobs(tmp_path / "jobs", [], clock=lambda: now[0])
    engine.jobs = jobs
    stale = tmp_path / "jobs" / "stale"
    stale.mkdir(parents=True)
    write(stale / "result.json", {"ok": None, "finished": 0})
    import os
    os.utime(stale / "result.json", (0, 0))
    entered, gate = threading.Event(), threading.Event()
    sweep = jobs.sweep

    def gated(at):
        entered.set()
        assert gate.wait(GATE_SECONDS), "测试闸门没有放开"
        sweep(at)

    monkeypatch.setattr(jobs, "sweep", gated)
    try:
        engine.ingest(event(ext_id="external-2", action="update"), ingress="external")
        engine.tick()
        assert entered.wait(GATE_SECONDS), "清理没有起"
        # 清理还停在闸门上，这一拍已经结束：归件作业已登记，心跳已写。
        assert not gate.is_set()
        assert any(p.name != "stale" for p in (tmp_path / "jobs").iterdir())
        assert (tmp_path / "heartbeat.json").exists()
        assert stale.exists()
        # 上一次没清完，下一拍不再起第二个。
        now[0] += job_module.SWEEP_SECONDS + 1
        engine.tick()
        gate.set()
        jobs.sweeper.join(GATE_SECONDS)
        assert not jobs.sweeper.is_alive()
        assert not stale.exists()
    finally:
        gate.set()
        jobs.launcher.shutdown(wait=True)
        engine.log.close()


def test_uncertain_reconcile_does_not_keep_the_loop_awake(tmp_path, monkeypatch):
    """对账结果取一次就清；结果为「不确定」时，告警后不再每拍起作业，下一次唤醒按间隔算。"""
    engine = build(tmp_path)
    reach(engine, "执行")
    channel = engine.plugins.get("channel", "channel")
    monkeypatch.setattr(channel, "ask", lambda *a: (_ for _ in ()).throw(ConnectionError("回包丢失")))
    calls = []
    monkeypatch.setattr(channel, "reconcile", lambda key, card: calls.append(key))
    card = engine.card({"id": "alpha:one"}, "q", "ask", question("选用哪个方案"))
    engine.fact("card_changed", {"id": card, "sending": True}, "alpha:one")
    for _ in range(3):
        engine.channels()
    assert len(calls) == 3
    assert engine.log.get("cards", card)["reconcile_failed"]
    # 已告警：同一时刻再跑多少拍都不再对账。
    for _ in range(20):
        engine.channels()
    assert len(calls) == 3
    # 不起作业，就不会有作业完成把主循环叫醒；下一次唤醒落在约好的对账时刻。
    assert 59 <= engine.wait_timeout() <= 60
    engine.clock.now += timedelta(seconds=60)
    engine.channels()
    assert len(calls) == 4
    engine.log.close()


def test_slow_tick_still_leaves_event_handling_within_one_second(tmp_path):
    """假时钟下复现旧的慢拍：来源一步占 2.3 秒，事件仍在这一拍开头就开始归件。"""
    clock = [0.0]
    engine = build(tmp_path)
    engine.ingest(event())
    flow = engine.plugins.get("flow", "flow")
    source = engine.plugins.get("source", "source")
    classify, poll = flow.classify, source.poll
    started = {}

    def slow_poll(target, cursor):
        clock[0] += 2.3  # 旧慢拍的量级，由假时钟推进，不真等。
        return poll(target, cursor)

    def watched(event, item, target, find_external=None):
        started.setdefault(event.ext_id, clock[0])
        return classify(event, item, target, find_external)

    source.poll = slow_poll
    flow.classify = watched
    recorded = clock[0]
    engine.ingest(event(ext_id="external-3", action="update"), ingress="external")
    engine.tick()
    assert started["external-3"] - recorded <= 1.0
    assert clock[0] - recorded >= 2.3  # 这一拍确实慢，慢在归件之后。
    engine.log.close()
