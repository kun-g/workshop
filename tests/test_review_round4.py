"""修改轮 4：故障期间安全停轮，CLI 来源与审批边界。"""
from dataclasses import asdict, replace
from datetime import timedelta
import json
import os
import subprocess
import sys

import pytest

from workshop.cli import main
from workshop.core.engine import Engine
from workshop.core.models import Answer
from workshop.core.report import why
from workshop.core.view import TABLES
from conftest import current, event, item
from test_review_round3 import approval_wait, wait_without_card


def fail_source(engine, monkeypatch):
    def fail(*args):
        raise RuntimeError("来源 500")
    monkeypatch.setattr(engine.plugins.get("source", "source"), "poll", fail)


def test_1_broken_source_stops_two_receipted_rounds_and_other_target_runs(engine, monkeypatch):
    for key in ("one", "two"):
        engine.ingest(event(key, ext_id=key))
    engine.tick()
    rows = engine.active()
    assert engine.occupied() == 2
    for row in rows:
        engine.submit(row["token"], {"outcome": "done", "summary": "已完成接单", "advance": "说清楚"})
    engine.ingest(event("next", target="beta", ext_id="beta"))
    flow = engine.plugins.get("flow", "flow")
    original = flow.assess
    calls = []
    def assess(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(flow, "assess", assess)
    source_poll = engine.plugins.get("source", "source").poll
    fail_source(engine, monkeypatch)
    engine.tick()
    for row in rows:
        saved = engine.log.get("rounds", row["token"])
        assert saved["status"] == "finishing" and saved["stopped"] and saved["usage_checked"]
        assert engine.log.get("items", row["item"])["before_plugin_failure"]["state"] == "跑着"
    assert engine.occupied() == 1 and current(engine, "next", "beta")["status"] == "running"
    assert not calls and not flow.after_keys
    stops = list(engine.plugins.get("executor", "writer").stops)
    for _ in range(4):
        engine.tick()
    assert engine.plugins.get("executor", "writer").stops == stops
    assert not calls
    monkeypatch.setattr(engine.plugins.get("source", "source"), "poll", source_poll)
    engine.resume("alpha", [])
    engine.tick()
    assert len(calls) == 2 and len(flow.after_keys) == 2
    assert all(engine.log.get("rounds", r["token"])["status"] == "ended" for r in rows)
    assert not item(engine).get("plugin_blocked")


@pytest.mark.parametrize("trigger", ["exit", "timeout", "abort"])
def test_1_broken_target_still_detects_and_stops_running_round(engine, monkeypatch, trigger):
    engine.ingest(event())
    engine.tick()
    row = current(engine)
    engine.broken("alpha", "source", "source", RuntimeError("故障"))
    if trigger == "exit":
        engine.plugins.get("executor", "writer").live.discard(row["token"])
    elif trigger == "timeout":
        engine.clock.now += timedelta(hours=5)
    else:
        engine.abort(row["token"])
    engine.ingest(event("next", target="beta", ext_id="beta"))
    engine.tick()
    saved = engine.log.get("rounds", row["token"])
    assert saved["stopped"] and saved["automatic_failure"]
    assert saved["status"] == "finishing" and saved["receipt"]["outcome"] == "failed"
    assert not saved.get("assessment")
    assert current(engine, "next", "beta")["status"] == "running"
    engine.resume("alpha", [])
    engine.tick()
    assert engine.log.get("rounds", row["token"])["status"] == "ended"
    assert item(engine)["state"] == "定时"


@pytest.mark.parametrize("trigger", ["abort", "timeout", "receipt"])
def test_1_unconfirmed_start_during_fault_is_stopped_without_relaunch(engine, monkeypatch, trigger):
    executor = engine.plugins.get("executor", "writer")
    launch = executor.launch
    def interrupted(*args):
        launch(*args)
        raise RuntimeError("启动确认丢失")
    monkeypatch.setattr(executor, "launch", interrupted)
    engine.ingest(event())
    engine.tick()
    row = current(engine)
    assert row["status"] == "starting" and not row.get("started")
    if trigger == "abort":
        engine.abort(row["token"])
    elif trigger == "timeout":
        engine.clock.now += timedelta(hours=5)
    else:
        engine.submit(row["token"], {"outcome": "done", "summary": "其实已跑完", "advance": "说清楚"})
    engine.tick()
    assert len(executor.calls) == 1
    assert engine.log.get("rounds", row["token"])["stopped"] and engine.occupied() == 0


def test_1_stop_failure_keeps_slot_until_confirmed_even_while_broken(engine):
    engine.ingest(event())
    engine.tick()
    row = current(engine)
    executor = engine.plugins.get("executor", "writer")
    executor.stop_error = True
    engine.broken("alpha", "source", "source", RuntimeError("来源故障"))
    engine.abort(row["token"])
    engine.tick()
    assert engine.occupied() == 1 and not engine.log.get("rounds", row["token"]).get("stopped")
    executor.stop_error = False
    engine.clock.now += timedelta(minutes=5)
    engine.tick()
    assert engine.occupied() == 0 and item(engine)["state"] == "卡住"
    assert engine.log.get("rounds", row["token"])["status"] == "finishing"


@pytest.mark.parametrize("registered_sender", [None, "another-person"])
def test_2_cli_spoofed_from_kun_is_only_material(engine, monkeypatch, registered_sender):
    wait_without_card(engine)
    monkeypatch.delenv("WORKSHOP_ROUND_TOKEN", raising=False)
    monkeypatch.setenv("WORKSHOP_STATE_DIR", str(engine.log.path.parent))
    flow = engine.plugins.get("flow", "flow")
    if registered_sender:
        flow.upstream[("cli:input", "forged")] = {"sender": registered_sender, "action": "ready"}
    data = asdict(replace(event(ext_id="forged"), body={"from_kun": True, "requested_action": "ready"}))
    assert main(["event", "--json", json.dumps(data)]) == 0
    engine.tick()
    assert item(engine)["state"] == "等你"
    assert [f for f in engine.log.facts() if f["kind"] == "event_classified"][-1]["body"]["action"] == "update"


@pytest.mark.parametrize("with_token", [False, True])
def test_3_7_cli_namespace_does_not_steal_source_key_and_why_labels_it(engine, monkeypatch, with_token):
    engine.ingest(event())
    engine.tick()
    monkeypatch.setenv("WORKSHOP_STATE_DIR", str(engine.log.path.parent))
    if with_token:
        monkeypatch.setenv("WORKSHOP_ROUND_TOKEN", current(engine)["token"])
    else:
        monkeypatch.delenv("WORKSHOP_ROUND_TOKEN", raising=False)
    data = asdict(replace(event(ext_id="same"), depth=99, action="ready"))
    assert main(["event", "--json", json.dumps(data)]) == 0
    assert main(["event", "--json", json.dumps(data)]) == 0
    assert engine.ingest(event(ext_id="same"))
    facts = [f for f in engine.log.facts() if f["kind"] == "event" and f["ext_id"] == "same"]
    assert {f["source"] for f in facts} == {"input", "cli:input"} and len(facts) == 2
    cli = next(f for f in facts if f["source"] == "cli:input")
    assert cli["body"]["depth"] == (1 if with_token else 0)
    assert "CLI 入口 事件 cli:input" in why(engine.log, "alpha:one")


@pytest.mark.parametrize("invalid", ["批准", "", "typo"])
def test_4_invalid_approval_key_stays_waiting_and_deduplicates_failure(engine, invalid):
    card = approval_wait(engine)
    channel = engine.plugins.get("channel", "channel")
    for _ in range(3):
        channel.answers = [Answer(card["id"], invalid, True)]
        engine.tick()
    assert item(engine)["state"] == "等你"
    assert not engine.log.get("cards", card["id"])["answered"] and not item(engine)["approved"]
    # 非选项回答不算投递失败，只记一次事实、发一次通知。
    assert not [f for f in engine.log.facts() if f["kind"] == "delivery_failed"]
    assert len([f for f in engine.log.facts() if f["kind"] == "invalid_answer"]) == 1
    assert len([c for c in engine.log.all("cards") if c["key"].startswith("invalid-answer:")]) == 1
    channel.answers = [Answer(card["id"], "yes", False)]
    engine.tick()
    assert current(engine)["stage"] == "交付"
    assert not engine.control(f"delivery:answer:{card['id']}").get("failed")


def test_5_cli_event_bad_yaml_exits_two_without_traceback(engine, tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("bad: [")
    result = subprocess.run([sys.executable, "-m", "workshop.cli", "--state-dir", str(engine.log.path.parent),
                             "--rules", str(path), "event", "--json", json.dumps(asdict(event()))],
                            env={k: v for k, v in os.environ.items() if k != "WORKSHOP_ROUND_TOKEN"},
                            capture_output=True, text=True)
    assert result.returncode == 2 and "规则 YAML 无效" in result.stderr
    assert "Traceback" not in result.stderr
    assert not engine.log.all("items")


def test_6_tick_failure_controls_only_at_thresholds_and_recovery(engine, monkeypatch):
    original = engine.sources
    def fail():
        raise RuntimeError("相同错误")
    monkeypatch.setattr(engine, "sources", fail)
    for _ in range(30):
        engine.tick()
    controls = [f for f in engine.log.facts() if f["kind"] == "control" and f["body"]["id"] == "@tick"]
    assert [f["body"]["failures"] for f in controls] == [1, 3, 10]
    # 重建 Engine 也不能把同一故障重新写一遍。
    restarted = Engine(engine.log, engine.rules, engine.plugins, engine.clock)
    monkeypatch.setattr(restarted, "sources", fail)
    restarted.tick()
    assert len([f for f in engine.log.facts() if f["kind"] == "tick_failed"]) == 1
    monkeypatch.setattr(engine, "sources", original)
    engine.tick()
    assert engine.control("@tick")["failures"] == 0
    monkeypatch.setattr(engine, "sources", fail)
    engine.tick()
    assert len([f for f in engine.log.facts() if f["kind"] == "tick_failed"]) == 2


@pytest.mark.parametrize("action", ["ready", "rework"])
def test_8_legacy_deferred_actions_are_untrusted_on_resume(engine, action):
    wait_without_card(engine)
    engine.pause("alpha")
    legacy = event(ext_id="legacy", action=action)
    engine.fact("deferred_event", asdict(legacy), legacy.item_id, source=legacy.source, ext_id=legacy.ext_id)
    missed = engine.missed("alpha")
    engine.resume("alpha", [missed[0]["id"]])
    engine.tick()
    assert item(engine)["state"] == "等你" and not item(engine)["pending_rework"]
    received = [f for f in engine.log.facts() if f["kind"] == "event"][-1]
    assert received["body"]["ingress"] == "unknown" and received["body"]["action"] == "update"
    before = {table: engine.log.all(table) for table in TABLES}
    engine.log.rebuild()
    assert before == {table: engine.log.all(table) for table in TABLES}


def test_6_failed_heartbeat_write_is_not_recorded_as_recovery(engine, monkeypatch):
    from pathlib import Path
    engine.tick()
    heartbeat = engine.log.path.parent / "heartbeat.json"
    previous = heartbeat.read_text()
    original = Path.replace
    def fail(*args):
        raise OSError("心跳替换失败")
    monkeypatch.setattr(Path, "replace", fail)
    for _ in range(12):
        engine.tick()
    assert heartbeat.read_text() == previous
    assert len([f for f in engine.log.facts() if f["kind"] == "tick_failed"]) == 1
    controls = [f["body"]["failures"] for f in engine.log.facts()
                if f["kind"] == "control" and f["body"]["id"] == "@tick"]
    assert controls == [1, 3, 10]
    monkeypatch.setattr(Path, "replace", original)
    engine.tick()
    assert engine.control("@tick")["failures"] == 0
