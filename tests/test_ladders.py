"""阶梯按执行者类别分：部署目录 ladders 按名字写几条，换人只在当前执行者所在的那条里往上找（核心 R14，ws#318）。"""
import pytest
import yaml

from conftest import Evidence, current, finish, item, reach
from test_deployment import RULES, deployment, run_cli
from test_ladder import build, executor, fail_rounds, gate_rounds, handovers, ladder_config
from workshop.core.rules import Rules
from workshop.deployment import assemble
from workshop.reports.report import why

# 字典顺序把「写文章」放前面：把几条阶梯摊平成一条的做法会从 weak 往上找到「写代码」里的 mid。
APART = {"写文章": [{"members": ["weak"]}],
         "写代码": [{"members": ["mid"]}, {"members": ["top"]}]}
ABOVE = {"写文章": [{"members": ["weak"]}, {"members": ["weak2"]}],
         "写代码": [{"members": ["mid"]}, {"members": ["top"]}]}


def config(ladders):
    data = ladder_config(ladder=None)
    data["ladders"] = ladders
    return data


# ---------- 部署目录：ladders 的写法和加载时校验 ----------

def test_deployment_ladders_load_by_name(tmp_path):
    root = deployment(tmp_path, settings={"ladders": {
        "写代码": [{"members": ["orca-codex", "codex"]},
                {"members": ["orca-claude", "orca-claude-gpt"], "takeover": ["orca-claude"]}],
        "写文章": [{"members": ["cursor-grok"]}]}})
    data = assemble(RULES, root).rules.data
    assert data["ladders"] == {
        "写代码": [{"members": ["orca-codex", "codex"], "takeover": ["orca-codex", "codex"]},
                {"members": ["orca-claude", "orca-claude-gpt"], "takeover": ["orca-claude"]}],
        "写文章": [{"members": ["cursor-grok"], "takeover": ["cursor-grok"]}]}
    assert "ladder" not in data
    # 规范化后的形式再过一遍校验结果不变。
    assert Rules.validate(data).data["ladders"] == data["ladders"]


@pytest.mark.parametrize("ladders,message", [
    ({"写代码": [{"members": ["orca-codex", "codex"]}], "写文章": [{"members": ["codex"]}]},
     "阶梯 写文章 第 1 级：执行者 codex 已在阶梯 写代码 第 1 级"),
    ({"写代码": [{"members": ["orca-codex"]}, {"members": ["codex", "orca-codex"]}]},
     "阶梯 写代码 第 2 级：执行者 orca-codex 已在阶梯 写代码 第 1 级"),
    ({"写代码": [{"members": ["nobody"]}]}, "阶梯 写代码 第 1 级：执行者 nobody 没有定义"),
    ({"写代码": {"members": ["orca-codex"]}}, "阶梯 写代码 必须是列表"),
    ({"": [{"members": ["orca-codex"]}]}, "ladders 的阶梯名必须是非空字符串"),
    ([{"members": ["orca-codex"]}], "ladders 必须是对象"),
])
def test_bad_ladders_name_the_ladder_the_level_and_the_executor(tmp_path, ladders, message):
    root = deployment(tmp_path, settings={"ladders": ladders})
    with pytest.raises(ValueError, match=message) as caught:
        assemble(RULES, root)
    assert "deployment.yaml" in str(caught.value)


def test_ladder_and_ladders_together_is_an_error(tmp_path):
    root = deployment(tmp_path, settings={"ladder": [{"members": ["orca-codex"]}],
                                          "ladders": {"写代码": [{"members": ["codex"]}]}})
    with pytest.raises(ValueError, match="ladder 和 ladders 只能写一个") as caught:
        assemble(RULES, root)
    assert "deployment.yaml" in str(caught.value)
    data = config({"写代码": [{"members": ["mid"]}]})
    data["ladder"] = [{"members": ["weak"]}]
    with pytest.raises(ValueError, match="ladder 和 ladders 只能写一个"):
        Rules.validate(data)


def test_deployment_ladders_replace_a_ladder_from_the_repository_rules(tmp_path):
    rules = yaml.safe_load(RULES.read_text())
    rules["ladder"] = [{"members": ["orca-codex"]}]
    path = tmp_path / "workshop.yaml"
    path.write_text(yaml.safe_dump(rules, allow_unicode=True))
    root = deployment(tmp_path, settings={"ladders": {"写代码": [{"members": ["codex"]}]}})
    data = assemble(path, root).rules.data
    assert "ladder" not in data and data["ladders"] == {"写代码": [{"members": ["codex"], "takeover": ["codex"]}]}


# ---------- 换人只在当前执行者所在的那条阶梯里找 ----------

def unable(engine, monkeypatch):
    """流程判这个执行者做不下来了，退回执行。"""
    flow = engine.plugins.get("flow", "flow")
    monkeypatch.setattr(flow, "assess", lambda *args: Evidence(
        reason="做不下来", unable=True, blocked="两段 DoD 退回到上限", next="项目经理看", handoff="执行"))
    finish(engine, "证据")


def by_failures(engine, monkeypatch):
    finish(engine, "执行")
    fail_rounds(engine, 10)


def by_gate(engine, monkeypatch):
    finish(engine, "执行")
    gate_rounds(engine, 3)


def by_returns(engine, monkeypatch):
    finish(engine, "执行")
    unable(engine, monkeypatch)


def by_quota(engine, monkeypatch):
    # 活已指定给「写文章」第 1 级的 weak，挑选时它不可用。
    engine.change(engine.log.get("items", "alpha:one"), takeover="weak")
    executor(engine, "weak").enabled = False
    finish(engine, "执行")


CAUSES = {"failures": by_failures, "gate": by_gate, "returns": by_returns, "quota": by_quota}
STUCK = {"failures": "连续 10 次没跑成", "returns": "两段 DoD 退回到上限", "quota": "执行者不可用"}


@pytest.mark.parametrize("cause", CAUSES)
def test_no_takeover_above_in_its_own_ladder_blocks_even_if_another_ladder_has_one(tmp_path, monkeypatch, cause):
    engine = build(tmp_path / "state", config(APART))
    try:
        reach(engine, "说清楚")
        CAUSES[cause](engine, monkeypatch)
        state = item(engine)
        assert state["state"] == "卡住"
        assert state["blocked_by"] == STUCK.get(cause, state.get("gate_reason"))
        assert handovers(engine) == []
        assert executor(engine, "mid").calls == [] and executor(engine, "top").calls == []
    finally:
        engine.log.close()


@pytest.mark.parametrize("cause", CAUSES)
def test_takeover_above_in_its_own_ladder_is_chosen(tmp_path, monkeypatch, cause):
    engine = build(tmp_path / "state", config(ABOVE))
    try:
        reach(engine, "说清楚")
        CAUSES[cause](engine, monkeypatch)
        state = item(engine)
        assert state["state"] != "卡住" and state["takeover"] == "weak2"
        assert current(engine)["executor"] == "weak2"
        [record] = handovers(engine)
        assert record["cause"] == cause and record["ladder"] == "写文章"
        assert (record["from"], record["from_level"], record["to"], record["to_level"]) == ("weak", 1, "weak2", 2)
    finally:
        engine.log.close()


# ---------- 换人事实记阶梯名；why 写出来 ----------

def test_handover_fact_records_the_ladder_name_and_why_shows_it(tmp_path):
    engine = build(tmp_path / "state", config(ABOVE))
    try:
        reach(engine, "说清楚")
        by_gate(engine, None)
        [record] = handovers(engine)
        assert set(record) == {"ladder", "from", "from_level", "to", "to_level", "cause", "reason", "skipped"}
        text = why(engine.log, "alpha:one")
        assert f"阶梯 写文章 换人 weak（第 1 级）→ weak2（第 2 级），原因 gate：{record['reason']}；跳过：无" in text
    finally:
        engine.log.close()


def test_old_ladder_records_an_empty_ladder_name_and_old_facts_still_show(tmp_path):
    engine = build(tmp_path / "state", ladder_config())
    try:
        reach(engine, "说清楚")
        by_gate(engine, None)
        [record] = handovers(engine)
        assert record["ladder"] == ""
        text = why(engine.log, "alpha:one")
        assert f"阶梯换人 weak（第 1 级）→ mid（第 2 级），原因 gate：{record['reason']}；跳过：无" in text
        # 上线前记的旧事实没有 ladder 字段：照旧显示，不报错。
        old = {k: v for k, v in record.items() if k != "ladder"}
        engine.log.append("executor_handover", old, "alpha:one")
        assert why(engine.log, "alpha:one").count("阶梯换人 weak（第 1 级）→ mid（第 2 级）") == 2
    finally:
        engine.log.close()


# ---------- executors check 按阶梯名分段打印 ----------

def test_executors_check_prints_one_section_per_ladder(tmp_path, capsys):
    root = deployment(tmp_path, settings={"ladders": {
        "写代码": [{"members": ["orca-codex", "codex"]},
                {"members": ["orca-claude", "orca-claude-gpt"], "takeover": ["orca-claude"]}],
        "写文章": [{"members": ["cursor-grok"]}]}})
    code, out, err = run_cli(capsys, "--state-dir", str(tmp_path / "state"), "executors", "check", str(root))
    assert code == 0, err
    assert out.splitlines()[-5:] == ["阶梯 写代码（级号、成员、接手者）",
                                     "1\torca-codex,codex\torca-codex,codex",
                                     "2\torca-claude,orca-claude-gpt\torca-claude",
                                     "阶梯 写文章（级号、成员、接手者）",
                                     "1\tcursor-grok\tcursor-grok"]
