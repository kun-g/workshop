"""执行者阶梯：做不下来的活逐级换更强的执行者接着做，到顶才卡住（核心 R14，ws#301）。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import Clock, FakeExecutor, FakeFlow, FakeHome, FakeSource, configuration, current, event, finish, item, reach
from test_deployment import RULES, deployment, run_cli
from test_quota_selector import snapshot
from workshop.core.engine import Engine
from workshop.core.log import Log
from workshop.core.rules import Rules
from workshop.deployment import assemble
from workshop.plugins.selectors.experiment import ExperimentSelector
from workshop.plugins.selectors.ordered import OrderedSelector
from workshop.plugins.selectors.quota import QuotaSelector
from workshop.registry import Registry
from workshop.reports.report import status, why

NAMES = ("weak", "weak2", "mid", "mid2", "top", "rev", "acc")
LADDER = [{"members": ["weak", "weak2"]},
          {"members": ["mid", "mid2"], "takeover": ["mid"]},
          {"members": ["top"]}]
CODEX_QUOTA = {"provider": "codex", "window": "Weekly", "min_remaining": 15, "label": "Codex 周额度"}


class FakeDriver:
    family = "fake"

    def validate(self, name, profile):
        pass


def ladder_config(ladder=LADDER, writers=("weak", "weak2", "mid", "mid2"), **profiles):
    """写代码候选不含 top：它只在阶梯里接手。每个执行者自成一家，审查、验收换家时不撞。"""
    config = configuration()
    config["executor_profiles"] = {name: {"driver": "fake", "transport": "headless", "billing": "subscription",
                                          "family": name, **profiles.get(name, {})} for name in NAMES}
    config["executors"] = {"writer": [{"use": n} for n in writers], "reviewer": [{"use": "rev"}],
                           "acceptor": [{"use": "acc"}]}
    if ladder is not None:
        config["ladder"] = ladder
    return config


def build(path, config, selector="ordered", **target):
    path.mkdir(parents=True, exist_ok=True)
    registry = Registry()
    registry.register("driver", "fake", FakeDriver())
    registry.register("selector", "ordered", OrderedSelector())
    registry.register("selector", "quota", QuotaSelector())
    registry.register("selector", "experiment", ExperimentSelector())
    registry.register("flow", "flow", FakeFlow())
    registry.register("flow", "other", FakeFlow())
    for name in NAMES:
        registry.register("executor", name, FakeExecutor(name))
    registry.register("taskhome", "home", FakeHome())
    registry.register("source", "source", FakeSource())
    config["stage_selectors"] = {"执行": selector}
    config["targets"]["alpha"].update(target)
    return Engine(Log(path / "workshop.db"), Rules.validate(config), registry, Clock())


@pytest.fixture
def engine(tmp_path):
    engine = build(tmp_path / "state", ladder_config())
    yield engine
    engine.log.close()


def executor(engine, name):
    return engine.plugins.get("executor", name)


def handovers(engine, key="alpha:one"):
    return [f["body"] for f in engine.log.facts(key) if f["kind"] == "executor_handover"]


def fail_rounds(engine, count):
    """连续 count 轮没跑成；每次失败后拨到重试时间。"""
    for _ in range(count):
        engine.submit(current(engine)["token"], {"outcome": "failed", "summary": "不成"})
        engine.tick()
        if item(engine)["state"] == "定时":
            engine.clock.now += timedelta(hours=1)
            engine.tick()


def gate_rounds(engine, count):
    """同一个关口连续没过 count 次：检查没过，回执照样要推进到证据。"""
    engine.plugins.get("flow", "flow").passed = False
    for _ in range(count):
        finish(engine, "证据")


# ---------- 部署目录：ladder 的写法和加载时校验 ----------

def test_deployment_ladder_loads_with_takeover_defaulting_to_members(tmp_path):
    root = deployment(tmp_path, settings={"ladder": [
        {"members": ["orca-codex", "codex"]},
        {"members": ["orca-claude", "orca-claude-gpt"], "takeover": ["orca-claude"]},
        # 接手的执行者不要求在写代码的候选里。
        {"members": ["cursor-grok"]}]})
    data = assemble(RULES, root).rules.data
    assert data["ladder"] == [
        {"members": ["orca-codex", "codex"], "takeover": ["orca-codex", "codex"]},
        {"members": ["orca-claude", "orca-claude-gpt"], "takeover": ["orca-claude"]},
        {"members": ["cursor-grok"], "takeover": ["cursor-grok"]}]
    assert "cursor-grok" not in [c["use"] for c in assemble(RULES, root).rules.candidates("执行", "workshop")]


def test_without_ladder_the_rules_have_no_ladder(tmp_path):
    root = deployment(tmp_path, settings={"orca": True})
    assert "ladder" not in assemble(RULES, root).rules.data
    assert assemble(RULES, root).rules.data == assemble(RULES, "none").rules.data


@pytest.mark.parametrize("ladder,message", [
    ([{"members": ["orca-codex"]}, {"members": ["nobody"]}], "阶梯第 2 级：执行者 nobody 没有定义"),
    ([{"members": ["orca-codex"]}, {"members": ["codex", "orca-codex"]}], "阶梯第 2 级：执行者 orca-codex 已在第 1 级"),
    ([{"members": ["orca-codex", "codex"]}, {"members": ["orca-claude"], "takeover": ["codex"]}],
     "阶梯第 2 级：接手者 codex 不在本级 members 里"),
    ([{"members": []}], "阶梯第 1 级：members 必须是不重复的执行者名列表"),
    ([{"members": ["orca-codex"], "extra": 1}], "阶梯第 1 级：不认的字段 extra"),
    ({"members": ["orca-codex"]}, "ladder 必须是列表"),
])
def test_bad_ladder_names_the_level_the_executor_and_the_file(tmp_path, ladder, message):
    root = deployment(tmp_path, settings={"ladder": ladder})
    with pytest.raises(ValueError, match=message) as caught:
        assemble(RULES, root)
    assert "deployment.yaml" in str(caught.value)


def test_executors_check_prints_the_ladder_last(tmp_path, capsys):
    root = deployment(tmp_path, settings={"ladder": [
        {"members": ["orca-codex", "codex"]},
        {"members": ["orca-claude", "orca-claude-gpt"], "takeover": ["orca-claude"]}]})
    code, out, err = run_cli(capsys, "--state-dir", str(tmp_path / "state"), "executors", "check", str(root))
    assert code == 0, err
    lines = out.splitlines()
    assert lines[-3:] == ["阶梯（级号、成员、接手者）",
                          "1\torca-codex,codex\torca-codex,codex",
                          "2\torca-claude,orca-claude-gpt\torca-claude"]
    code, out, err = run_cli(capsys, "--state-dir", str(tmp_path / "state"), "executors", "check",
                             str(deployment(tmp_path, name="plain")))
    assert code == 0 and "阶梯" not in out


# ---------- 当前执行者 ----------

def test_item_records_current_executor_when_a_round_starts_but_not_for_program_stages(engine, monkeypatch):
    engine.ingest(event())
    engine.tick()
    assert item(engine)["executor"] == "weak"
    flow = engine.plugins.get("flow", "flow")
    monkeypatch.setattr(flow, "automatic", lambda i, s, t: s == "说清楚", raising=False)
    for name in ("weak", "weak2", "mid", "mid2"):
        executor(engine, name).enabled = False
    finish(engine, "说清楚")
    engine.tick()
    # 说清楚由程序做完、推进到执行；执行一轮起不来。程序阶段不改记录。
    assert item(engine)["stage"] == "执行" and item(engine)["state"] == "卡住"
    assert item(engine)["executor"] == "weak"
    executor(engine, "weak2").enabled = True
    engine.ingest(event("two", ext_id="2"))
    engine.tick()
    assert current(engine, "two")["executor"] == "weak2" and item(engine, "two")["executor"] == "weak2"


# ---------- 到上限换人 ----------

def test_ten_failures_hand_over_to_the_next_level_instead_of_blocking(engine):
    reach(engine, "执行")
    fail_rounds(engine, 10)
    state = item(engine)
    assert state["state"] != "卡住"
    assert state["takeover"] == "mid" and state["handovers"] == 1
    assert state["failures"] == 0 and state.get("gate_count", 0) == 0
    assert current(engine)["executor"] == "mid"
    assert handovers(engine) == [{"ladder": "", "from": "weak", "from_level": 1, "to": "mid", "to_level": 2, "cause": "failures",
                                  "reason": "连续 10 次没跑成", "skipped": []}]
    # 接手的执行者也是 10 次。
    fail_rounds(engine, 9)
    assert item(engine)["takeover"] == "mid" and item(engine)["failures"] == 9
    fail_rounds(engine, 1)
    assert item(engine)["takeover"] == "top" and item(engine)["handovers"] == 2


def test_gate_limit_hands_over_starting_three_then_two_for_takeovers(engine):
    reach(engine, "执行")
    gate_rounds(engine, 2)
    assert item(engine).get("takeover") is None and current(engine)["executor"] == "weak"
    gate_rounds(engine, 1)
    state = item(engine)
    assert state["state"] != "卡住" and state["takeover"] == "mid" and state["gate_count"] == 0
    assert current(engine)["executor"] == "mid"
    [first] = handovers(engine)
    assert first["cause"] == "gate" and first["from"] == "weak" and first["to"] == "mid"
    assert first["reason"] == state["gate_reason"]
    # 接手的上限是 2 次。
    gate_rounds(engine, 1)
    assert item(engine)["takeover"] == "mid"
    gate_rounds(engine, 1)
    assert item(engine)["takeover"] == "top" and current(engine)["executor"] == "top"
    assert [h["to_level"] for h in handovers(engine)] == [2, 3]


def test_unavailable_level_is_skipped(engine):
    executor(engine, "mid").enabled = False
    reach(engine, "执行")
    gate_rounds(engine, 3)
    assert item(engine)["takeover"] == "top" and current(engine)["executor"] == "top"
    [record] = handovers(engine)
    assert record["from_level"] == 1 and record["to_level"] == 3 and record["skipped"] == [2]


def test_recently_unavailable_takeover_is_skipped(engine, monkeypatch):
    """最近启动时报过不可用的执行者，这一刻不算可用。"""
    reach(engine, "执行")
    gate_rounds(engine, 2)
    monkeypatch.setattr(engine, "recently_unavailable", lambda work, minutes=10: ["mid"])
    gate_rounds(engine, 1)
    assert item(engine)["takeover"] == "top" and current(engine)["executor"] == "top"
    assert handovers(engine)[0]["skipped"] == [2]


def test_takeover_unavailable_at_launch_moves_up(engine, monkeypatch):
    """被指定的执行者启动时报不可用：不卡住，再往上换一级。"""
    from workshop.core.ports import Unavailable

    def refuse(*args):
        raise Unavailable("mid 起不来")
    reach(engine, "执行")
    monkeypatch.setattr(executor(engine, "mid"), "launch", refuse)
    gate_rounds(engine, 3)
    assert item(engine)["takeover"] == "top" and current(engine)["executor"] == "top"
    assert [(h["from"], h["to"], h["cause"]) for h in handovers(engine)] == [("weak", "mid", "gate"), ("mid", "top", "quota")]


# ---------- 指定以后的挑选 ----------

@pytest.mark.parametrize("selector,text", [
    ("ordered", "选阶梯指定的接手者 mid；阶段 执行，第 6 轮"),
    ("quota", "阶梯指定的接手者 mid，只交它；选 mid（无额度门槛）；阶段 执行，第 6 轮"),
    ("experiment", "实验分配：alpha:one 交给阶梯指定的接手者 mid，不按活号分配；阶段 执行，第 6 轮"),
])
def test_selection_reason_says_ladder_takeover_for_each_selector(tmp_path, selector, text):
    engine = build(tmp_path / "state", ladder_config(), selector)
    try:
        reach(engine, "执行")
        gate_rounds(engine, 3)
        row = current(engine)
        assert row["executor"] == "mid" and row["selection_reason"] == text
        assert row["selector"] == selector
    finally:
        engine.log.close()


def test_takeover_outside_writer_candidates_gets_writing_rounds_and_review_is_unaffected(tmp_path):
    engine = build(tmp_path / "state", ladder_config(ladder=[{"members": ["weak", "weak2"]}, {"members": ["top"]}]))
    try:
        reach(engine, "执行")
        gate_rounds(engine, 3)
        assert current(engine)["executor"] == "top"
        engine.plugins.get("flow", "flow").passed = True
        finish(engine, "证据")
        # 证据也是写代码的角色：仍只派给被指定的执行者。
        assert current(engine)["stage"] == "证据" and current(engine)["executor"] == "top"
        finish(engine, "审查")
        assert current(engine)["stage"] == "审查" and current(engine)["executor"] == "rev"
        finish(engine, "交付")
        assert current(engine)["executor"] == "top"
        finish(engine, "验收")
        assert current(engine)["stage"] == "验收" and current(engine)["executor"] == "acc"
        finish(engine, "收尾")
        finish(engine, leave={"kind": "工序", "owner": "车间", "at": "+1d", "about": "观察"})
        assert item(engine)["takeover"] == "top"
        # 返工回到执行：指定还在，仍交给 top。
        engine.ingest(event(ext_id="rework", action="rework"))
        engine.tick()
        assert current(engine)["stage"] == "执行" and current(engine)["executor"] == "top"
        assert item(engine)["takeover"] == "top"
    finally:
        engine.log.close()


def test_takeover_failing_quota_moves_up_and_blocks_at_the_top(tmp_path):
    path = snapshot(tmp_path / "quota.db", {"codex": {"Weekly": 95.0}}, datetime.now(timezone.utc))
    quota = {"quota_snapshot": {"path": str(path), "max_age_minutes": 45}}
    engine = build(tmp_path / "state", ladder_config(mid={"quota": CODEX_QUOTA}), "quota", **quota)
    try:
        reach(engine, "执行")
        gate_rounds(engine, 3)
        # mid 额度门槛没过：不卡住，改指定给 top 再挑一次。
        assert item(engine)["state"] == "跑着" and item(engine)["takeover"] == "top"
        assert current(engine)["executor"] == "top"
        gate, quota_record = handovers(engine)
        assert gate["to"] == "mid" and quota_record["cause"] == "quota"
        assert quota_record["from"] == "mid" and quota_record["to"] == "top"
        assert "Codex 周额度剩 5%，低于门槛 15%" in quota_record["reason"]
    finally:
        engine.log.close()
    engine = build(tmp_path / "again", ladder_config(mid={"quota": CODEX_QUOTA}, top={"quota": CODEX_QUOTA}),
                   "quota", **quota)
    try:
        reach(engine, "执行")
        gate_rounds(engine, 3)
        state = item(engine)
        assert state["state"] == "卡住" and state["takeover"] == "top"
        assert state["blocked_by"].startswith("执行者不可用：")
        assert state["next"] == "项目经理核对执行者和订阅恢复后重试"
        assert [h["cause"] for h in handovers(engine)] == ["gate", "quota"]
    finally:
        engine.log.close()


# ---------- 找不到接手者：和现在逐项相同 ----------

def stuck_after_failures(engine):
    reach(engine, "执行")
    fail_rounds(engine, 10)
    state = item(engine)
    assert state["state"] == "卡住" and state["blocked_by"] == "连续 10 次没跑成"
    assert state["next"] == "项目经理处理失败原因后恢复"


def stuck_after_gate(engine):
    reach(engine, "执行")
    gate_rounds(engine, 3)
    state = item(engine)
    assert state["state"] == "卡住" and state["blocked_by"] == state["gate_reason"]
    assert state["next"] == "项目经理修正关口失败原因后恢复"


def stuck_without_executor(engine):
    reach(engine, "说清楚")
    for name in ("weak", "weak2", "mid", "mid2"):
        executor(engine, name).enabled = False
    finish(engine, "执行")
    state = item(engine)
    assert state["state"] == "卡住" and state["blocked_by"] == "执行者不可用"
    assert state["next"] == "项目经理核对执行者和订阅恢复后重试"


@pytest.mark.parametrize("ladder,disabled", [
    ([{"members": ["weak2"]}, {"members": ["mid"]}], ()),                     # 执行者不在阶梯里
    ([{"members": ["mid"]}, {"members": ["weak", "weak2"]}], ()),             # 已在最高级
    (LADDER, ("mid", "top")),                                                 # 上面全不可用
    (None, ()),                                                               # 没配阶梯
])
@pytest.mark.parametrize("scenario", [stuck_after_failures, stuck_after_gate, stuck_without_executor])
def test_no_takeover_keeps_todays_behaviour(tmp_path, ladder, disabled, scenario):
    engine = build(tmp_path / "state", ladder_config(ladder))
    try:
        for name in disabled:
            executor(engine, name).enabled = False
        scenario(engine)
        assert handovers(engine) == [] and item(engine).get("takeover") is None
    finally:
        engine.log.close()


# ---------- 接手者的工作区和会话 ----------

def test_takeover_uses_the_same_workdir_with_a_fresh_session(engine):
    reach(engine, "执行")
    before = item(engine)
    # 假设 mid 早先在这件活上留过会话：接手时也不接回，开新会话。
    engine.change(engine.log.get("items", "alpha:one"), sessions={**before["sessions"], "mid": "session:old"})
    gate_rounds(engine, 3)
    calls = executor(engine, "mid").calls
    assert [c[0] for c in calls] == ["launch"]
    launched = calls[0][3]
    assert launched["workdir"] == before["workdir"] == "/fake/workdir"
    state = item(engine)
    assert state["sessions"]["mid"] == "session:mid:alpha:one" != state["sessions"]["weak"]
    # 接手者自己的下一轮接回它自己的会话。
    gate_rounds(engine, 1)
    assert [c[0] for c in executor(engine, "mid").calls] == ["launch", "resume"]


# ---------- 换人记录、why、status ----------

def test_handover_fact_fields_and_why_and_status_lines(engine):
    executor(engine, "mid").enabled = False
    reach(engine, "执行")
    gate_rounds(engine, 3)
    [record] = handovers(engine)
    assert set(record) == {"ladder", "from", "from_level", "to", "to_level", "cause", "reason", "skipped"}
    assert record["ladder"] == ""
    text = why(engine.log, "alpha:one")
    assert f"阶梯换人 weak（第 1 级）→ top（第 3 级），原因 gate：{record['reason']}；跳过：第 2 级" in text
    engine.submit(current(engine)["token"], {"outcome": "failed", "summary": "不成"})
    engine.tick()
    lines = status(engine.log).splitlines()
    row = next(line for line in lines if line.startswith("alpha:one\t"))
    assert row.split("\t")[3] == "车间（阶梯指定 top）"


# ---------- 项目经理恢复 ----------

def test_recover_keeps_the_takeover_and_counts_again(engine):
    reach(engine, "执行")
    gate_rounds(engine, 3 + 2 + 2)
    state = item(engine)
    assert state["state"] == "卡住" and state["takeover"] == "top" and state["gate_count"] == 2
    engine.recover("alpha:one", "rework", "核对过关口原因", "再试")
    engine.tick()
    state = item(engine)
    assert state["takeover"] == "top" and state["gate_count"] == 0 and state["failures"] == 0
    assert current(engine)["executor"] == "top"
    gate_rounds(engine, 1)
    assert item(engine)["state"] != "卡住"
    gate_rounds(engine, 1)
    assert item(engine)["state"] == "卡住" and item(engine)["takeover"] == "top"
