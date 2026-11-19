"""已退役的送达键：规则里没有的通道或交付对象失败不再登记事项，标记和心跳一起清掉。"""
import json
from types import SimpleNamespace

from workshop.core.faults import delivery_retired
from workshop.core.managers import Store, observe_failures
from workshop.reports.manager_status import heading_of


def concerns(engine, tmp_path, retire=False):
    store = Store(tmp_path / "managers")
    try:
        if retire:
            store.observe("fault:delivery:channel:ask_kun", {"reason": "旧标记"}, "core",
                          engine.clock().isoformat())
        observe_failures(store, engine.log, "core", engine.clock().isoformat(), engine.rules)
        return {row["id"]: row for row in store.all()}
    finally:
        store.close()


def test_retired_channel_failure_registers_no_concern_and_resolves_old_one(engine, tmp_path):
    engine.delivery_error("channel:ask_kun", RuntimeError("旧通道已退役"))
    rows = concerns(engine, tmp_path, retire=True)
    assert not rows["fault:delivery:channel:ask_kun"]["active"]


def test_failure_of_delivery_still_in_rules_still_registers(engine, tmp_path):
    engine.delivery_error("alpha:one:token:card", RuntimeError("送达未确认"))
    assert concerns(engine, tmp_path)["fault:delivery:alpha:one:token:card"]["active"]


def test_round_token_failure_still_registers(engine, tmp_path):
    engine.delivery_error("poll:token-1", RuntimeError("轮询看不清"))
    assert concerns(engine, tmp_path)["fault:delivery:poll:token-1"]["active"]


def test_tick_clears_retired_marker_so_heartbeat_stops_counting(engine):
    engine.fact("control", {"id": "delivery:channel:ask_kun", "failed": True,
                            "since": "2026-11-19T08:10:53+00:00"})
    engine.tick()
    assert not engine.control("delivery:channel:ask_kun").get("failed")
    heartbeat = json.loads((engine.log.path.parent / "heartbeat.json").read_text())
    assert heartbeat["delivery_failed_count"] == 0 and heartbeat["delivery_failed_since"] is None


def test_retired_judgement_follows_current_rules():
    rules = SimpleNamespace(data={"channel": ["lark"], "targets": {"alpha": {}}})
    assert not delivery_retired(rules, "channel:lark")
    assert delivery_retired(rules, "channel:ask_kun")
    assert not delivery_retired(rules, "alpha:one:token:card")
    assert delivery_retired(rules, "gone:one:token:card")
    # 现行规则不再收 channel，等于一个通道都没有。
    current = SimpleNamespace(data={"targets": {"alpha": {}}})
    assert delivery_retired(current, "channel:ask_kun")


def test_heading_of_channel_fault_is_readable():
    assert heading_of("fault:delivery:channel:ask_kun") == "ask_kun 送达通道"
    assert heading_of("fault:delivery:grant-console:170:requirement-change") == "grant-console:170 送达"
    assert heading_of("fault:delivery:poll:token") == "fault:delivery:poll:token"
