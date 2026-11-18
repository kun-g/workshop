"""workshop why：短名查活、每条事件一行摘要、--raw 原样资料（ws#18）。"""
from dataclasses import replace

import pytest

from workshop.cli import main
from workshop.core.models import Classification, Event
from workshop.reports.report import unfiled, why
from workshop.plugins.issue_delivery import IssueDelivery
from conftest import FakeFlow, build, configuration, finish

USER = {"login": "kun-g", "id": 1042091, "avatar_url": "https://avatars.githubusercontent.com/u/1042091?v=4",
        "html_url": "https://github.com/kun-g", "type": "User", "site_admin": False}
ISSUE = {"number": 5, "title": "示例活", "state": "open", "user": USER,
         "body": "## Kun 的原话\n第一行\n第二行\n\n## 验收标准\n- 一条", "reactions": {"total_count": 0, "+1": 0},
         "labels": [{"name": "Ready for Agent", "color": "0E8A16"}]}
LABEL = {"id": 36002722375, "event": "labeled", "actor": USER, "label": {"name": "Ready for Agent", "color": "0E8A16"},
         "created_at": "2026-11-13T07:13:26Z"}


def rules():
    data = configuration()
    data["targets"] = {"workshop": {"plugin": "flow", "sources": ["source"], "short_name": "ws"},
                       "grant-console": {"plugin": "other", "sources": [], "short_name": "gc"}}
    return data


def github(kind, ext, data, action="update"):
    return Event("github", ext, "workshop", "5", "kun-g/workshop #5",
                 {"kind": kind, "repo": "kun-g/workshop", "number": "5", "data": data}, action=action)


@pytest.fixture
def engine(tmp_path):
    engine = build(tmp_path, rules())
    engine.ingest(github("issue", "issue:5:body", ISSUE))
    engine.ingest(github("label", "label:5:1", LABEL, "ready"))
    engine.tick()
    finish(engine, "说清楚", key="5", target="workshop")
    yield engine
    engine.log.close()


def event_lines(text):
    return [line for line in text.splitlines() if " 事件 " in line]


def test_short_name_and_target_key_give_same_output(engine):
    by_short = why(engine.log, "ws#5", rules())
    assert by_short == why(engine.log, "workshop:5", rules())
    assert by_short.splitlines()[0] == "活 workshop:5"
    # 流程没实现摘要时，核心退回「来源 + 事件种类」，原始资料不打印。
    events = event_lines(by_short)
    assert len(events) == 2
    assert events[0].endswith("事件 github update：issue") and events[1].endswith("事件 github ready：label")
    assert "avatar_url" not in by_short and "第二行" not in by_short
    # 每一段仍都列出：轮次、执行者、模型、用量、回执、关口结论。
    assert "接单 writer 一轮" in by_short and "规则模型=" in by_short
    assert "实际模型=model-writer" in by_short and "token=100 / 0 / 0 / 20" in by_short
    assert "回执 done 完成接单" in by_short and "过关口" in by_short


def test_flow_summary_is_one_line(engine):
    engine.plugins.register("flow", "flow", IssueDelivery(io=object()))
    text = why(engine.log, "ws#5", rules(), engine.plugins)
    events = event_lines(text)
    assert "issue #5（open）正文「示例活」，来自 kun-g" in events[0]
    assert events[1].endswith("事件 github ready：issue #5 打上 Ready for Agent，来自 kun-g")
    assert "avatar_url" not in text and "第二行" not in text


def test_raw_prints_original_material(engine):
    text = why(engine.log, "ws#5", rules(), raw=True)
    assert "avatar_url" in text and "第一行\\n第二行" in text
    assert text.count("原始资料：") == 2


def test_broken_flow_summary_falls_back(engine):
    class Broken:
        def describe(self, event, target):
            raise RuntimeError("插件 bug")
    engine.plugins.register("flow", "flow", Broken())
    assert event_lines(why(engine.log, "ws#5", rules(), engine.plugins))[0].endswith("：issue")


@pytest.mark.parametrize("text", ["zz#5", "ws#999", "workshop:999", "missing"])
def test_unknown_short_name_or_number(engine, text):
    with pytest.raises(ValueError) as error:
        why(engine.log, text, rules())
    assert "找不到活" in str(error.value) and "gc、ws" in str(error.value)


def test_short_name_follows_alias(engine):
    engine.fact("alias_bound", {"id": "workshop:18", "object_key": "5", "item": "workshop:5"}, "workshop:5",
                source="alias", ext_id="test:18")
    assert why(engine.log, "ws#18", rules()) == why(engine.log, "workshop:5", rules())


def test_cli_why_short_name_raw_and_unknown(engine, tmp_path, capsys):
    state = ["--state-dir", str(tmp_path)]
    assert main([*state, "why", "ws#5"]) == 0
    short = capsys.readouterr().out
    assert main([*state, "why", "workshop:5"]) == 0
    assert capsys.readouterr().out == short
    assert all(len(line.splitlines()) == 1 for line in event_lines(short)) and len(event_lines(short)) == 2
    assert "avatar_url" not in short
    assert main([*state, "why", "ws#5", "--raw"]) == 0
    assert "avatar_url" in capsys.readouterr().out
    assert main([*state, "why", "zz#5"]) == 2
    err = capsys.readouterr().err
    assert "找不到活：zz#5" in err and "ws" in err


def test_describe_github_kinds():
    flow, target = IssueDelivery(io=object()), {"name": "workshop"}
    event = github("label", "x", LABEL)
    assert flow.describe(event, target) == "issue #5 打上 Ready for Agent，来自 kun-g"
    comment = replace(event, body={**event.body, "kind": "pr-review", "data": {"state": "APPROVED", "user": USER}})
    assert flow.describe(comment, target) == "PR #5 审查 APPROVED，来自 kun-g"
    ci = replace(event, body={**event.body, "kind": "pr-ci", "data": {"sha": "a" * 40, "status": {"state": "success"},
                 "checks": {"check_runs": [{"status": "completed", "conclusion": "success"}, {"status": "queued"}]}}})
    assert flow.describe(ci, target) == "PR #5 CI aaaaaaaaaaaa：pending 1，success 1；提交状态 success"
    assert flow.describe(Event("input", "1", "workshop", "5", "t", "正文"), target) is None


# ws#37：外部入口事件经流程判定后，归到的活的 why 有一行；丢弃的、没判定的进「未归件」。
class Judge(FakeFlow):
    """按消息号给判定；摘要是流程核实上游后的一句话。"""
    verdicts = {
        "om_rework": Classification("rework", "5", summary="飞书 Kun：「ws#5 不对，测试绑定」"),
        "om_drop": Classification(discard=True, summary="发送者不是 Kun"),
        "om_new": Classification(summary="飞书 Kun：「加一个新功能」"),
        "om_plain": Classification("update", "5"),
    }

    def classify(self, event, item, target, find_external=None):
        if event.ext_id == "om_stuck":
            raise RuntimeError("流程判定出错")
        return self.verdicts[event.ext_id]

    def describe(self, event, target):
        return f'自报原话「{event.body["text"]}」'


def external(ext_id, text):
    return Event("feishu", ext_id, "workshop", ext_id, text, {"text": text})


@pytest.fixture
def judged(engine):
    engine.plugins.register("flow", "flow", Judge())
    for ext_id, text in (("om_rework", "ws#5 不对，测试绑定"), ("om_drop", "冒名"), ("om_new", "加一个新功能"), ("om_plain", "补一句")):
        assert engine.ingest(external(ext_id, text), ingress="external")
    engine.tick()
    return engine


def seq_of(engine, ext_id):
    return next(f["seq"] for f in engine.log.facts() if f["kind"] == "event" and f["ext_id"] == ext_id)


def test_classified_event_shows_in_why_of_its_item(judged):
    text = why(judged.log, "ws#5", rules(), judged.plugins)
    filed = [line for line in text.splitlines() if " 归件 " in line]
    assert len(filed) == 2
    at, rest = filed[0].split(" ", 1)
    assert at.startswith("2026-11-13T")  # 时刻
    # 来源、动作、原话摘要和谁说的；指回原事件号。
    assert rest == f'归件 CLI 入口 cli:feishu 返工：飞书 Kun：「ws#5 不对，测试绑定」（原事件 {seq_of(judged, "om_rework")}）'
    # 流程没给摘要（例如旧事实）时，退回流程对事件本身的说明。
    assert filed[1].endswith(f'归件 CLI 入口 cli:feishu 更新：自报原话「补一句」（原事件 {seq_of(judged, "om_plain")}）')
    assert "冒名" not in text and "加一个新功能" not in text


def test_event_that_created_new_item_shows_in_its_why(judged):
    text = why(judged.log, "workshop:om_new", rules(), judged.plugins)
    assert f'归件 CLI 入口 cli:feishu 更新：飞书 Kun：「加一个新功能」（原事件 {seq_of(judged, "om_new")}）' in text


def test_discarded_and_unjudged_events_are_listed_as_unfiled(judged):
    judged.ingest(external("om_stuck", "判不了"), ingress="external")
    judged.tick()
    lines = unfiled(judged.log, rules(), judged.plugins).splitlines()
    assert lines[0] == "未归件"
    assert len(lines) == 3  # 已归到活（含新建的活）的不在这里重复。
    assert lines[1].endswith(f'丢弃 workshop CLI 入口 cli:feishu：自报原话「冒名」；判定：发送者不是 Kun（事件 {seq_of(judged, "om_drop")}）')
    assert lines[2].endswith(f'还没判定 workshop CLI 入口 cli:feishu：自报原话「判不了」（事件 {seq_of(judged, "om_stuck")}）')


def test_unfiled_lists_item_dropped_after_classification(engine):
    engine.plugins.register("flow", "flow", Judge())
    engine.ingest(external("om_new", "加一个新功能"), ingress="external")
    engine.classify_events()  # 只判定不起轮，活还能清掉。
    engine.drop("workshop:om_new")
    assert "归到 workshop:om_new，活已清掉" in unfiled(engine.log, rules(), engine.plugins)


def test_unfiled_empty(engine):
    assert unfiled(engine.log, rules()) == "未归件\n没有"


def test_cli_unfiled_and_argument_check(judged, tmp_path, capsys):
    state = ["--state-dir", str(tmp_path)]
    assert main([*state, "why", "--unfiled"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("未归件\n") and "丢弃 workshop" in out
    assert main([*state, "why"]) == 2
    assert main([*state, "why", "ws#5", "--unfiled"]) == 2
    assert "要么给一件活，要么用 --unfiled" in capsys.readouterr().err
