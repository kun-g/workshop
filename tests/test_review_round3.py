"""复审修改轮 3 的十条回归，包括真实回执与退出探测的竞争。"""
from dataclasses import asdict, replace
from datetime import timedelta
import json
import os
import subprocess

import pytest
import yaml

from workshop.cli import main
from workshop.core.engine import Engine
from workshop.core.log import Log
from workshop.core.models import Answer, stamp
from workshop.core.ports import Retryable
from workshop.core.report import model_metrics, status, usage
from workshop.core.rules import Rules
from workshop.core.view import TABLES
from conftest import current, event, finish, item, reach


def wait_without_card(engine):
    reach(engine, "说清楚")
    finish(engine, leave={"kind": "等你", "about": "等放行"},
           notice=[{"key": "wait", "text": "先说清楚，不用回卡"}])
    assert item(engine)["state"] == "卡住"
    return engine.log.all("rounds")[-1]["token"]


def test_1_stale_running_snapshot_cannot_replace_real_receipt(engine, monkeypatch):
    engine.ingest(event())
    engine.tick()
    stale = current(engine)
    data = {"outcome": "done", "summary": "真实回执", "advance": "说清楚"}
    engine.submit(stale["token"], data)
    monkeypatch.setattr(engine.plugins.get("executor", "writer"), "poll",
                        lambda *args: (_ for _ in ()).throw(AssertionError("已有回执不能再探测")))
    engine.check_round(stale)
    assert engine.log.get("rounds", stale["token"])["receipt"] == data
    assert not engine.control("alpha").get("broken")


def test_1_receipt_arriving_during_poll_wins_over_automatic_failure(engine, monkeypatch):
    engine.ingest(event())
    engine.tick()
    row = current(engine)
    data = {"outcome": "done", "summary": "交完即退出", "advance": "说清楚"}
    executor = engine.plugins.get("executor", "writer")
    def exited(token, started):
        other = Engine(Log(engine.log.path), engine.rules, engine.plugins, engine.clock)
        other.submit(token, data)
        other.log.close()
        executor.live.discard(token)
        return "exited"
    monkeypatch.setattr(executor, "poll", exited)
    engine.tick()
    settled = engine.log.get("rounds", row["token"])
    assert settled["outcome"] == "done" and not settled.get("automatic_failure")
    assert current(engine)["stage"] == "说清楚"
    assert not any(f["kind"] == "plugin_failed" for f in engine.log.facts())


@pytest.mark.parametrize("action", ["ready", "rework", "not-an-action"])
def test_2_receipt_children_cannot_release_or_rework_waiting_item(engine, monkeypatch, action):
    wait_without_card(engine)
    engine.ingest(event("two", ext_id="two"))
    engine.tick()
    child = asdict(replace(event(ext_id="child"), action=action, depth=-9,
                           body={"from_kun": True, "requested_action": "ready"}))
    monkeypatch.setattr(engine.plugins.get("flow", "flow"), "classify",
                        lambda *args: (_ for _ in ()).throw(AssertionError("agent 事件不能 classify")))
    finish(engine, "说清楚", key="two", events=[child])
    assert item(engine)["state"] == "卡住" and not item(engine)["pending_rework"]
    received = [f for f in engine.log.facts("alpha:one") if f["kind"] == "event"][-1]
    assert received["body"]["action"] == "update" and received["body"]["depth"] == 1
    assert received["body"]["ingress"] == "agent"


def test_2_cli_agent_cannot_forge_source_action_or_depth(engine, tmp_path, monkeypatch):
    token = wait_without_card(engine)
    runtime = engine.runtime(token)
    data = asdict(replace(event(ext_id="agent-cli"), action="ready", depth=0,
                          body={"from_kun": True, "requested_action": "ready"}))
    result = subprocess.run(runtime["command"] + ["event", "--json", json.dumps(data)], cwd=tmp_path,
                            env={**os.environ, **runtime["env"]}, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    engine.tick()
    assert item(engine)["state"] == "卡住" and item(engine)["depth"] == 1
    assert not any(e.get("kind") == "classify" for e in engine.log.all("effects"))


@pytest.mark.parametrize("action", ["ready", "rework"])
def test_2_non_agent_cli_action_is_decided_by_flow(engine, monkeypatch, action):
    wait_without_card(engine)
    monkeypatch.setenv("WORKSHOP_STATE_DIR", str(engine.log.path.parent))
    monkeypatch.delenv("WORKSHOP_ROUND_TOKEN", raising=False)
    engine.plugins.get("flow", "flow").upstream[("cli:input", "human-cli")] = {"sender": "Kun", "action": action}
    body = {"from_kun": True, "requested_action": action}
    data = asdict(replace(event(ext_id="human-cli"), action="wrong-caller-value", depth=99, body=body))
    assert main(["event", "--json", json.dumps(data)]) == 0
    assert item(engine)["state"] == "卡住"  # CLI 只提交，判定发生在 tick。
    engine.tick()
    assert current(engine)["stage"] == ("说清楚" if action == "ready" else "执行")
    received = [f for f in engine.log.facts() if f["kind"] == "event" and f["ext_id"] == "human-cli"][0]
    assert received["body"]["depth"] == 0 and received["body"]["action"] == "update"
    assert any(f["kind"] == "event_classified" and f["body"]["action"] == action for f in engine.log.facts())


def test_2_untrusted_cli_ready_does_not_release_and_source_ready_still_does(engine):
    wait_without_card(engine)
    engine.ingest(event(ext_id="untrusted", action="ready", depth=99), ingress="external")
    engine.tick()
    assert item(engine)["state"] == "卡住" and item(engine)["depth"] == 0
    engine.plugins.get("source", "source").events = [event(ext_id="trusted", action="ready")]
    engine.tick()
    assert current(engine)["stage"] == "说清楚"


def test_2_paused_external_event_keeps_provenance_until_selected(engine):
    wait_without_card(engine)
    engine.pause("alpha")
    engine.plugins.get("flow", "flow").upstream[("cli:input", "paused")] = {"sender": "Kun", "action": "ready"}
    engine.ingest(replace(event(ext_id="paused"), action="ready", depth=99,
                          body={"from_kun": True, "requested_action": "ready"}), ingress="external")
    missed = engine.missed("alpha")
    engine.resume("alpha", [missed[0]["id"]])
    assert item(engine)["state"] == "卡住"
    engine.tick()
    assert current(engine)["stage"] == "说清楚"
    before = {table: engine.log.all(table) for table in TABLES}
    engine.log.rebuild()
    assert before == {table: engine.log.all(table) for table in TABLES}


def test_2_bad_classifier_isolated_and_pending_event_recovers(engine, monkeypatch):
    wait_without_card(engine)
    flow = engine.plugins.get("flow", "flow")
    original = flow.classify
    flow.upstream[("cli:input", "later")] = {"sender": "Kun", "action": "ready"}
    monkeypatch.setattr(flow, "classify", lambda *args: "unsupported")
    engine.ingest(replace(event(ext_id="later"), body={"from_kun": True, "requested_action": "ready"}), ingress="external")
    engine.tick()
    assert item(engine)["state"] == "卡住"
    monkeypatch.setattr(flow, "classify", original)
    engine.resume("alpha", [])
    engine.tick()
    assert current(engine)["stage"] == "说清楚"


def test_3_failed_ticks_do_not_refresh_heartbeat_and_errors_deduplicate(engine, monkeypatch):
    engine.tick()
    path = engine.log.path.parent / "heartbeat.json"
    good = path.read_text()
    original = engine.sources
    monkeypatch.setattr(engine, "sources", lambda: (_ for _ in ()).throw(RuntimeError("重复失败")))
    for _ in range(3):
        engine.clock.now += timedelta(minutes=1)
        engine.tick()
    assert path.read_text() == good
    assert len([f for f in engine.log.facts() if f["kind"] == "tick_failed"]) == 1
    assert engine.control("@tick")["failures"] == 3
    monkeypatch.setattr(engine, "sources", original)
    engine.tick()
    assert json.loads(path.read_text())["last_ok_at"] == stamp(engine.clock())
    monkeypatch.setattr(engine, "sources", lambda: (_ for _ in ()).throw(RuntimeError("重复失败")))
    engine.tick()
    assert len([f for f in engine.log.facts() if f["kind"] == "tick_failed"]) == 2


def test_3_failure_before_first_success_does_not_create_healthy_heartbeat(engine, monkeypatch):
    monkeypatch.setattr(engine, "sources", lambda: (_ for _ in ()).throw(RuntimeError("首轮失败")))
    engine.tick()
    assert not (engine.log.path.parent / "heartbeat.json").exists()


def test_4_broken_flow_is_not_finished_or_blocked_again_each_tick(engine, monkeypatch):
    reach(engine, "执行")
    calls = []
    def broken(*args):
        calls.append(1)
        raise RuntimeError("流程核验崩了")
    monkeypatch.setattr(engine.plugins.get("flow", "flow"), "assess", broken)
    finish(engine, "证据")
    count = len([f for f in engine.log.facts() if f["kind"] == "work_blocked"])
    for _ in range(4):
        engine.tick()
        engine.broken("alpha", "flow", "flow", RuntimeError("同一故障"))
    assert len(calls) == 1
    assert len([f for f in engine.log.facts() if f["kind"] == "work_blocked"]) == count


def test_5_receipt_reads_snapshot_even_with_broken_rule_file(engine, tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(engine.rules.data))
    loaded = Engine(engine.log, Rules.load(path), engine.plugins, engine.clock)
    loaded.ingest(event())
    loaded.tick()
    token = current(loaded)["token"]
    runtime = loaded.runtime(token)
    assert "--rules" not in runtime["command"] and "WORKSHOP_RULES" not in runtime["env"]
    path.write_text("broken: [")
    result = subprocess.run(runtime["command"] + ["--rules", str(path), "receipt", token, "--json",
                            json.dumps({"outcome": "done", "summary": "ok", "advance": "说清楚"})],
                            cwd=tmp_path, env={**os.environ, **runtime["env"], "WORKSHOP_RULES": str(path)}, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert loaded.log.get("rounds", token)["status"] == "finishing"


def test_6_uncertain_delivery_escalates_once_and_is_visible(engine, monkeypatch):
    reach(engine, "执行")
    channel = engine.plugins.get("channel", "channel")
    sends = []
    def lost(key, card):
        sends.append(key)
        raise ConnectionError("回包丢失")
    monkeypatch.setattr(channel, "ask", lost)
    monkeypatch.setattr(channel, "reconcile", lambda *args: None)
    finish(engine, ask=[{"key": "design", "title": "选方案", "options": ["A", "B"]}])
    for _ in range(5):
        engine.channels()
    card = engine.log.all("cards")[0]
    assert len(sends) == 1
    assert len([f for f in engine.log.facts() if f["kind"] == "reconcile_failed"]) == 1
    assert len([c for c in engine.log.all("cards") if c.get("reconciliation_alert")]) == 1
    assert "通道待对账" in status(engine.log) and card["id"] in status(engine.log)
    monkeypatch.setattr(channel, "reconcile", lambda key, card: f"found:{key}")
    engine.channels()
    assert engine.log.get("cards", card["id"])["sent"]
    assert "通道待对账" not in status(engine.log)


def test_6_uncertain_alert_does_not_recursively_create_alerts(engine, monkeypatch):
    reach(engine, "执行")
    channel = engine.plugins.get("channel", "channel")
    monkeypatch.setattr(channel, "ask", lambda *args: (_ for _ in ()).throw(ConnectionError("不确定")))
    monkeypatch.setattr(channel, "notify", lambda *args: (_ for _ in ()).throw(ConnectionError("也不确定")))
    monkeypatch.setattr(channel, "reconcile", lambda *args: None)
    finish(engine, ask=[{"key": "q", "title": "选择", "options": ["A", "B"]}])
    for _ in range(12):
        engine.channels()
    assert len(engine.log.all("cards")) == 2
    assert len([f for f in engine.log.facts() if f["kind"] == "reconcile_failed"]) == 2


def test_7_finish_retryable_backs_off_and_faults_at_three(engine, monkeypatch):
    reach(engine, "执行")
    executor = engine.plugins.get("executor", "writer")
    original = executor.stop
    calls = []
    def temporary(*args):
        calls.append(1)
        raise Retryable("暂时关不掉")
    monkeypatch.setattr(executor, "stop", temporary)
    row = finish(engine, "证据")
    engine.tick()
    assert len(calls) == 1 and engine.occupied() == 1
    for minutes in (5, 10):
        engine.clock.now += timedelta(minutes=minutes)
        engine.tick()
    assert len(calls) == 3 and engine.control("alpha")["broken"]
    engine.clock.now += timedelta(hours=1)
    engine.tick()
    assert len(calls) == 4  # 故障期间仍按退避尝试安全停止。
    monkeypatch.setattr(executor, "stop", original)
    engine.clock.now += timedelta(hours=1)
    engine.resume("alpha", [])
    engine.tick()
    assert current(engine)["stage"] == "证据"
    assert engine.control(f"retry:stop:{row['token']}")["count"] == 0


def approval_wait(engine):
    engine.plugins.get("flow", "flow").actions = ["对外发消息"]
    reach(engine, "审查")
    finish(engine, "交付")
    return next(c for c in engine.log.all("cards") if c.get("actions"))


def test_8_rejection_returns_to_execution_with_choice_feedback(engine):
    card = approval_wait(engine)
    engine.plugins.get("channel", "channel").answers = [Answer(card["id"], "no", False)]
    before = len([r for r in engine.log.all("rounds") if r["stage"] == "审查"])
    engine.tick()
    assert current(engine)["stage"] == "执行"
    assert len([r for r in engine.log.all("rounds") if r["stage"] == "审查"]) == before
    assert "Kun 没批准" in item(engine)["gate_reason"]
    assert any(m.get("body") == "no" and m["choice_label"] == "不批准" for m in item(engine)["materials"])
    assert any("Kun 没批准" in str(m["body"]) for m in item(engine)["materials"])


@pytest.mark.parametrize("choice,claimed,stage", [("yes", False, "交付"), ("no", True, "执行")])
def test_9_core_approval_uses_key_not_channel_boolean(engine, choice, claimed, stage):
    card = approval_wait(engine)
    assert card["core_approval"]
    engine.plugins.get("channel", "channel").answers = [Answer(card["id"], choice, claimed)]
    engine.tick()
    assert current(engine)["stage"] == stage
    assert bool(item(engine)["approved"]) == (choice == "yes")


def test_9_plugin_own_question_retains_its_approval_field(engine):
    reach(engine, "执行")
    finish(engine, ask=[{"key": "q", "title": "自己的问题", "options": ["A", "B"]}])
    card = engine.log.all("cards")[0]
    assert not card.get("core_approval")
    engine.plugins.get("channel", "channel").answers = [Answer(card["id"], "custom", True)]
    engine.tick()
    assert next(f for f in engine.log.facts() if f["kind"] == "answer")["body"]["approved"] is True
    assert not item(engine)["approved"]  # 插件问题没有核心动作授权范围。


def test_10_only_ended_rounds_enter_usage_and_model_groups(engine):
    engine.ingest(event())
    engine.tick()
    assert model_metrics(engine.log) == {}
    assert "今天\t0\t" in usage(engine.log, engine.clock())
    assert "未上报 1 轮" not in usage(engine.log, engine.clock())
    engine.plugins.get("executor", "writer").usage_value = None
    finish(engine, "说清楚")
    assert model_metrics(engine.log)[None]["rounds"] == 1
    assert model_metrics(engine.log)[None]["unreported"] == 1
    assert len(engine.active()) == 1
