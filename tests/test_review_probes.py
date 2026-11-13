"""由换家审查探针 A–L 转为修复后的断言，并覆盖修改清单 0–26。"""
from dataclasses import asdict
from datetime import timedelta
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from workshop.cli import main
from workshop.core.cards import question, notice, validate_question
from workshop.core.engine import Engine
from workshop.core.log import Log
from workshop.core.models import Answer, Event, Evidence, stamp
from workshop.core.ports import Retryable
from workshop.core.rules import Rules
from conftest import build, current, event, finish, item, reach


def ask(key="d", text="选哪个"):
    return {"key": key, **question(text)}


def test_0_taskhome_optional_and_default_unconfigured(engine):
    config = deepcopy(engine.rules.data)
    config.pop("taskhome")
    engine.rules = Rules.validate(config)
    reach(engine, "收尾")
    finish(engine, leave={"kind": "工序", "owner": "车间", "at": "+1d", "about": "观察"})
    assert item(engine)["state"] == "完成" and not item(engine)["ticket"]
    assert "taskhome" not in Rules.load(Path("rules/workshop.yaml")).data


def test_A_unknown_action_category_fails_closed(engine):
    reach(engine, "审查")
    engine.plugins.get("flow", "flow").actions = ["对外发送消息"]
    finish(engine, "交付")
    assert item(engine)["stage"] == "审查" and item(engine)["state"] == "卡住"
    assert len([f for f in engine.log.facts() if f["kind"] == "plugin_failed"]) == 1


def crash_and_repair(engine):
    flow = engine.plugins.get("flow", "flow")
    engine.ingest(event("two", ext_id="9"))
    flow.crash = True
    engine.tick()
    flow.crash = False
    engine.resume("alpha", [])
    engine.tick()


def test_B_resume_restores_waiting_not_running(engine):
    reach(engine, "执行")
    finish(engine, ask=[ask()])
    crash_and_repair(engine)
    assert item(engine)["state"] == "等你"
    assert not any(r["item"] == "alpha:one" for r in engine.active())


def test_C_answer_keeps_concurrent_event_and_monotonic_cause(engine):
    reach(engine, "执行")
    finish(engine, ask=[ask()])
    card = engine.log.all("cards")[0]
    channel = engine.plugins.get("channel", "channel")
    def poll(cards):
        other = Engine(Log(engine.log.path), engine.rules, engine.plugins, engine.clock)
        other.ingest(event(ext_id="late"))
        other.log.close()
        return [Answer(card["id"], "继续")]
    channel.poll = poll
    engine.channels()
    seq = next(f["seq"] for f in engine.log.facts() if f["kind"] == "event" and f["ext_id"] == "late")
    assert seq in [m.get("seq") for m in item(engine)["materials"]]
    engine.change(item(engine), cause=1)
    assert item(engine)["cause"] >= seq


def test_D_other_owner_gets_reminder_not_execution(engine):
    reach(engine, "收尾")
    finish(engine, leave={"kind": "工序", "owner": "Kun", "at": "+1d", "about": "Kun 自己处理"})
    engine.clock.now += timedelta(days=1, minutes=1)
    engine.tick()
    engine.tick()
    assert not engine.active()
    assert item(engine)["state"] == "完成"
    assert len([c for c in engine.log.all("cards") if c["mode"] == "notice"]) == 1


def test_E_nothing_does_not_send_empty_card(engine):
    reach(engine, "执行")
    engine.submit(current(engine)["token"], {"outcome": "nothing", "summary": "没有新结果", "advance": "证据"})
    engine.tick()
    assert not engine.log.all("cards")
    assert item(engine)["stage"] == "执行" and item(engine)["state"] == "定时"


def test_F_dead_rounds_release_slots_and_retry(engine):
    for n in range(3):
        engine.ingest(event(str(n), ext_id=str(n)))
    engine.tick()
    engine.plugins.get("executor", "writer").live.clear()
    engine.tick()
    assert item(engine, "0")["failures"] == 1
    assert item(engine, "1")["state"] == "定时"
    assert current(engine, "2")["status"] == "running"


def test_G_exhausted_item_stays_exhausted_after_plugin_repair(engine):
    reach(engine, "执行")
    for n in range(10):
        engine.submit(current(engine)["token"], {"outcome": "failed", "summary": "不成"})
        engine.tick()
        if n < 9:
            engine.clock.now += timedelta(hours=1)
            engine.tick()
    assert item(engine)["state"] == "卡住"
    crash_and_repair(engine)
    assert item(engine)["state"] == "卡住" and item(engine)["failures"] == 10
    assert not any(r["item"] == "alpha:one" for r in engine.active())


def test_H_transient_start_failure_does_not_stop_other_work(engine, monkeypatch):
    writer = engine.plugins.get("executor", "writer")
    original = writer.launch
    def launch(token, brief, it, config, target):
        if it["id"] == "alpha:one":
            raise Retryable("暂时起不来，确认没有进程")
        return original(token, brief, it, config, target)
    monkeypatch.setattr(writer, "launch", launch)
    engine.ingest(event())
    engine.ingest(event("two", ext_id="2"))
    engine.tick()
    assert item(engine)["state"] == "定时"
    assert current(engine, "two")["status"] == "running"
    assert not engine.control("alpha").get("broken")


def test_I_failed_acceptance_returns_to_execution(engine):
    reach(engine, "验收")
    engine.plugins.get("flow", "flow").passed = False
    engine.submit(current(engine)["token"], {"outcome": "failed", "summary": "验收步骤第 2 步不过"})
    engine.tick()
    assert current(engine)["stage"] == "执行"
    assert item(engine)["failures"] == 0


def test_J_answers_have_questions_and_are_separate_from_external_text(engine):
    reach(engine, "执行")
    finish(engine, ask=[ask("a", "用哪个库"), ask("b", "要不要迁移")])
    cards = engine.log.all("cards")
    engine.plugins.get("channel", "channel").answers = [Answer(cards[0]["id"], "乙"), Answer(cards[1]["id"], "甲")]
    engine.tick()
    brief = current(engine)["brief"]
    assert [a["title"] for a in brief["answers"]] == ["用哪个库", "要不要迁移"]
    assert [a["key"] for a in brief["answers"]] == ["a", "b"]
    assert all(a["role"] == "Kun 的回答" and a["card"] for a in brief["answers"])
    assert not any(m.get("source") == "回答" for m in brief["materials"])


def test_K_approval_uses_saved_evidence_without_repeating_review(engine):
    flow = engine.plugins.get("flow", "flow")
    flow.actions = ["对外发消息"]
    reach(engine, "审查")
    finish(engine, "交付")
    count = len(engine.log.all("rounds"))
    card = next(c for c in engine.log.all("cards") if c.get("actions"))
    flow.assess = lambda *args: (_ for _ in ()).throw(AssertionError("不得重新核验旧轮"))
    engine.plugins.get("channel", "channel").answers = [Answer(card["id"], "yes", True)]
    engine.tick()
    assert current(engine)["stage"] == "交付"
    assert len(engine.log.all("rounds")) == count + 1


def test_L_each_plugin_incident_notifies_once(engine):
    flow = engine.plugins.get("flow", "flow")
    engine.ingest(event())
    flow.crash = True
    engine.tick()
    flow.crash = False
    engine.resume("alpha", [])
    engine.tick()
    engine.ingest(event("two", ext_id="2"))
    flow.crash = True
    engine.tick()
    engine.tick()
    assert len([c for c in engine.log.all("cards") if c["mode"] == "notice"]) == 2


def test_1_timeout_and_abort_are_failed_rounds(engine):
    engine.ingest(event())
    engine.tick()
    token = current(engine)["token"]
    engine.clock.now += timedelta(hours=3)
    engine.tick()
    assert engine.log.get("rounds", token)["outcome"] == "failed"
    assert item(engine)["state"] == "定时"
    engine.clock.now += timedelta(minutes=5)
    engine.tick()
    token = current(engine)["token"]
    engine.abort(token)
    engine.tick()
    assert engine.log.get("rounds", token)["outcome"] == "failed"
    assert not engine.active()


def test_4_repair_restores_timer(engine):
    reach(engine, "执行")
    finish(engine, later=[{"key": "later", "at": "+1h", "about": "看结果"}])
    crash_and_repair(engine)
    assert item(engine)["state"] == "定时"


def test_5_cli_requires_seen_and_rejects_stale_list(engine, monkeypatch, capsys):
    monkeypatch.setenv("WORKSHOP_STATE_DIR", str(engine.log.path.parent))
    engine.pause("alpha")
    engine.ingest(event())
    first = engine.missed("alpha")[0]["id"]
    engine.ingest(event("two", ext_id="2"))
    assert main(["resume", "alpha", "--select", "all"]) == 2
    assert main(["resume", "alpha", "--select", "all", "--seen", first]) == 2
    assert engine.control("alpha")["paused"] and len(engine.missed("alpha")) == 2


def test_8_failure_notice_has_new_episode(engine):
    reach(engine, "执行")
    def three():
        for n in range(3):
            engine.submit(current(engine)["token"], {"outcome": "failed", "summary": "失败"})
            engine.tick()
            engine.clock.now += timedelta(hours=1)
            engine.tick()
    three()
    finish(engine, "证据")
    three()
    assert len([c for c in engine.log.all("cards") if ":failing:" in c["id"]]) == 2


def test_9_executor_receives_target_and_absolute_runtime_cli(engine, tmp_path):
    engine.ingest(event())
    engine.tick()
    row = current(engine)
    runtime = row["target"]["runtime"]
    assert row["target"]["name"] == "alpha"
    assert Path(runtime["state_dir"]).is_absolute()
    result = subprocess.run(runtime["command"] + ["receipt", row["token"], "--json", json.dumps({"outcome": "done", "summary": "ok", "advance": "说清楚"})],
                            cwd=tmp_path, env={**os.environ, **runtime["env"]}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert engine.log.get("rounds", row["token"])["status"] == "finishing"


def test_10_core_and_receipt_cards_have_strict_format(engine):
    reach(engine, "执行")
    finish(engine, ask=[ask()])
    for card in engine.log.all("cards"):
        validate_question(card["payload"])
    with pytest.raises(ValueError):
        engine.submit("missing", {"outcome": "done", "summary": "x", "ask": [dict(key="k", title="old", options=["A"])]})


def test_11_next_round_sees_previous_conclusions(engine):
    reach(engine, "审查")
    flow = engine.plugins.get("flow", "flow")
    flow.passed = False
    original = flow.assess
    flow.assess = lambda *args: Evidence(checks=flow.checks, passed=False, reason="必须改：缺校验")
    finish(engine, "执行", evidence=["check:failure.txt"])
    history = item(engine)["history"][-1]
    assert history["reason"] == "必须改：缺校验" and history["evidence"] == ["check:failure.txt"]
    assert current(engine)["stage"] == "执行"
    assert engine.plugins.get("executor", "writer").calls[-1][-1]["history"][-1] == history


def test_13_stage_executor_override(engine):
    config = deepcopy(engine.rules.data)
    config["stage_executors"] = {"证据": {"use": "acceptor", "family": "c", "billing": "subscription"}}
    engine.rules = Rules.validate(config)
    reach(engine, "证据")
    assert current(engine)["executor"] == "acceptor"


@pytest.mark.parametrize("by_paths", [True, False])
def test_14_two_distinct_reviewers_must_pass(engine, by_paths):
    config = deepcopy(engine.rules.data)
    config["executors"]["reviewer"].append({"use": "acceptor", "family": "c", "billing": "subscription"})
    config["targets"]["alpha"]["two_reviewers"] = ["deploy/"]
    engine.rules = Rules.validate(config)
    flow = engine.plugins.get("flow", "flow")
    flow.assess = lambda *args: Evidence(checks=flow.checks, passed=True,
                                         paths=["deploy/service.yaml"] if by_paths else [], reviewers=1 if by_paths else 2)
    reach(engine, "审查")
    finish(engine, "交付")
    assert item(engine)["stage"] == "审查"
    assert current(engine)["executor"] == "acceptor"
    finish(engine, "交付")
    assert item(engine)["stage"] == "交付" and item(engine)["reviews"] == ["b", "c"]


def test_16_gate_retries_same_executor_then_escalates(engine):
    engine.plugins.get("flow", "flow").checks = []
    reach(engine, "说清楚")
    for n in range(3):
        finish(engine, "执行")
        if n < 2:
            assert current(engine)["executor"] == "writer" and not engine.log.all("cards")
            assert "能跑的检查" in item(engine)["gate_reason"]
    assert item(engine)["state"] == "等你" and len(engine.log.all("cards")) == 1


def test_17_transient_source_backoff_then_fault(engine, monkeypatch):
    source = engine.plugins.get("source", "source")
    calls = []
    def poll(*args):
        calls.append(1)
        raise Retryable("抖动")
    monkeypatch.setattr(source, "poll", poll)
    engine.tick()
    engine.tick()
    assert len(calls) == 1 and not engine.control("alpha").get("broken")
    engine.clock.now += timedelta(minutes=5)
    engine.tick()
    engine.clock.now += timedelta(minutes=10)
    engine.tick()
    assert len(calls) == 3 and engine.control("alpha")["broken"]


def test_18_cli_event_infers_chain_depth(engine, tmp_path):
    engine.ingest(event(depth=8))
    engine.tick()
    row = current(engine)
    runtime = row["target"]["runtime"]
    result = subprocess.run(runtime["command"] + ["event", "--json", json.dumps(asdict(event("child", ext_id="c", depth=0)))],
                            cwd=tmp_path, env={**os.environ, **runtime["env"]}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert item(engine, "child")["depth"] == 9 and item(engine, "child")["state"] == "卡住"


def test_19_send_crash_reconciles_instead_of_sending_twice(engine, monkeypatch):
    reach(engine, "执行")
    channel = engine.plugins.get("channel", "channel")
    original = channel.ask
    calls = []
    def ask_then_crash(key, card):
        calls.append(key)
        original(key, card)
        raise KeyboardInterrupt()
    monkeypatch.setattr(channel, "ask", ask_then_crash)
    with pytest.raises(KeyboardInterrupt):
        finish(engine, ask=[ask()])
    assert engine.log.all("cards")[0]["sending"]
    monkeypatch.setattr(channel, "ask", original)
    engine.tick()
    assert len(calls) == 1 and engine.log.all("cards")[0]["sent"]


def test_20_delivery_failure_dedup_and_new_incident(engine):
    reach(engine, "执行")
    channel = engine.plugins.get("channel", "channel")
    channel.down = True
    finish(engine, ask=[ask()])
    engine.tick()
    engine.tick()
    card = engine.log.all("cards")[0]
    assert len([f for f in engine.log.facts() if f["kind"] == "delivery_failed" and f["body"]["key"] == card["id"]]) == 1
    channel.down = False
    engine.tick()
    engine.cancel_questions(item(engine))
    channel.down = True
    engine.tick()
    engine.tick()
    assert len([f for f in engine.log.facts() if f["kind"] == "delivery_failed" and f["body"]["key"] == card["id"]]) == 2


def test_21_pause_resume_do_not_take_tick_lock(engine, monkeypatch):
    monkeypatch.setattr(engine, "exclusive", lambda: (_ for _ in ()).throw(AssertionError("不得拿 tick 锁")))
    engine.pause("alpha")
    engine.resume("alpha", [], expected=[])
    assert not engine.control("alpha")["paused"]


def test_22_usage_receives_round_start_time(engine):
    reach(engine, "执行")
    row = current(engine)
    engine.clock.now += timedelta(minutes=1)
    finish(engine, "证据")
    assert engine.plugins.get("executor", "writer").since == row["at"]


def test_23_rework_cancels_old_questions_and_ignores_late_answer(engine):
    reach(engine, "执行")
    finish(engine, ask=[ask()])
    card = engine.log.all("cards")[0]
    engine.ingest(event(ext_id="back", action="rework"))
    engine.tick()
    assert engine.log.get("cards", card["id"])["cancel_done"]
    engine.plugins.get("channel", "channel").answers = [Answer(card["id"], "过期答案", True)]
    engine.tick()
    assert not any(m.get("body") == "过期答案" for m in item(engine)["materials"])
    assert current(engine)["stage"] == "执行"


def test_24_tick_exception_is_recorded_and_next_tick_runs(engine, monkeypatch):
    original = engine._tick
    monkeypatch.setattr(engine, "_tick", lambda: (_ for _ in ()).throw(RuntimeError("意外错误")))
    engine.tick()
    assert any(f["kind"] == "tick_failed" for f in engine.log.facts())
    monkeypatch.setattr(engine, "_tick", original)
    engine.ingest(event())
    engine.tick()
    assert current(engine)


def test_26_no_card_wait_only_explicit_ready_event_releases(engine):
    reach(engine, "说清楚")
    finish(engine, leave={"kind": "等你", "about": "需要聊清楚验收标准"},
           notice=[{"key": "clarify", **notice("验收标准没说清，先等放行，不用回卡")}])
    assert item(engine)["state"] == "等你" and not engine.active()
    assert len(engine.log.all("cards")) == 1 and engine.log.all("cards")[0]["mode"] == "notice"
    engine.ingest(event(ext_id="comment", action="update"))
    engine.tick()
    assert item(engine)["state"] == "等你"
    engine.ingest(event(ext_id="ready", action="ready"))
    engine.tick()
    assert current(engine)["stage"] == "说清楚" and len(engine.log.all("items")) == 1


def test_4_timer_due_during_fault_runs_after_repair(engine):
    reach(engine, "执行")
    finish(engine, later=[{"key": "later", "at": "+1m", "about": "查看"}])
    engine.broken("alpha", "source", "source", RuntimeError("坏了"))
    engine.clock.now += timedelta(minutes=2)
    engine.tick()
    assert not engine.log.all("timers")[0]["fired"]
    engine.resume("alpha", [])
    engine.tick()
    assert current(engine)["stage"] == "执行"


def test_4_answer_during_fault_is_used_after_repair(engine):
    reach(engine, "执行")
    finish(engine, ask=[ask()])
    card = engine.log.all("cards")[0]
    engine.broken("alpha", "source", "source", RuntimeError("坏了"))
    engine.plugins.get("channel", "channel").answers = [Answer(card["id"], "继续")]
    engine.tick()
    engine.resume("alpha", [])
    engine.tick()
    assert current(engine)["stage"] == "执行"


def test_19_uncertain_reconciliation_never_resends(engine, monkeypatch):
    reach(engine, "执行")
    channel = engine.plugins.get("channel", "channel")
    channel.down = True
    finish(engine, ask=[ask()])
    channel.down = False
    monkeypatch.setattr(channel, "reconcile", lambda *args: None)
    monkeypatch.setattr(channel, "ask", lambda *args: (_ for _ in ()).throw(AssertionError("不得盲目重发")))
    engine.tick()
    assert engine.log.all("cards")[0]["sending"] and not engine.log.all("cards")[0]["sent"]


@pytest.mark.parametrize("term", ["issues", "Issue", "PRs", "merged", "pull_request", "worktree", "my_issue_id", "NewIssue"])
def test_25_core_term_check_rejects_variants(tmp_path, term):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "check_boundaries.py").write_text(Path("scripts/check_boundaries.py").read_text())
    core = tmp_path / "src/workshop/core"
    core.mkdir(parents=True)
    (core / "sample.py").write_text(f'value = "{term}"\n')
    result = subprocess.run([sys.executable, str(scripts / "check_boundaries.py")], capture_output=True, text=True)
    assert result.returncode != 0 and "R22" in result.stderr


def test_26_ready_during_fault_retains_original_stage(engine):
    reach(engine, "说清楚")
    finish(engine, leave={"kind": "等你", "about": "先聊清楚"}, notice=[{"key": "wait", **notice("等待放行")}])
    engine.broken("alpha", "source", "source", RuntimeError("坏了"))
    engine.ingest(event(ext_id="ready", action="ready"))
    engine.resume("alpha", [])
    engine.tick()
    assert current(engine)["stage"] == "说清楚"


def test_17_three_transient_launch_failures_escalate(engine, monkeypatch):
    writer = engine.plugins.get("executor", "writer")
    monkeypatch.setattr(writer, "launch", lambda *args: (_ for _ in ()).throw(Retryable("确认没起")))
    engine.ingest(event())
    for n in range(3):
        engine.tick()
        engine.clock.now += timedelta(hours=1)
    assert engine.control("alpha")["broken"] and item(engine)["state"] == "卡住"
    assert not engine.occupied()


def test_14_first_review_with_question_is_not_counted_as_passed(engine):
    config = deepcopy(engine.rules.data)
    config["executors"]["reviewer"].append({"use": "acceptor", "family": "c", "billing": "subscription"})
    engine.rules = Rules.validate(config)
    reach(engine, "审查")
    original_session = current(engine)["started"]["session"]
    finish(engine, "交付", ask=[ask()])
    card = engine.log.all("cards")[0]
    engine.plugins.get("channel", "channel").answers = [Answer(card["id"], "继续")]
    engine.tick()
    assert not item(engine)["reviews"]
    assert current(engine)["started"]["session"] == original_session


def test_1_abort_after_launch_crash_does_not_launch_again(engine, monkeypatch):
    engine.ingest(event())
    original = engine.fact
    def crash(kind, body, *args, **kwargs):
        if kind == "round_changed" and body.get("started"):
            raise KeyboardInterrupt()
        return original(kind, body, *args, **kwargs)
    monkeypatch.setattr(engine, "fact", crash)
    with pytest.raises(KeyboardInterrupt):
        engine.tick()
    token = current(engine)["token"]
    monkeypatch.setattr(engine, "fact", original)
    engine.abort(token)
    engine.tick()
    assert engine.log.get("rounds", token)["status"] == "ended"
    executor = engine.plugins.get("executor", "writer")
    assert not executor.live and len(executor.calls) == 1


def test_dead_acceptor_is_runtime_failure_not_fabricated_evidence(engine):
    reach(engine, "验收")
    row = current(engine)
    engine.plugins.get("executor", "acceptor").live.clear()
    engine.plugins.get("flow", "flow").assess = lambda *args: (_ for _ in ()).throw(AssertionError("没有证据不能核验"))
    engine.tick()
    assert item(engine)["stage"] == "验收" and item(engine)["state"] == "定时"
    assert engine.log.get("rounds", row["token"])["passed"] is None
