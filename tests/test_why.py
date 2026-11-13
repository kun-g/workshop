"""workshop why：短名查活、每条事件一行摘要、--raw 原样资料（ws#18）。"""
from dataclasses import replace

import pytest

from workshop.cli import main
from workshop.core.models import Event
from workshop.core.report import why
from workshop.plugins.issue_delivery import IssueDelivery
from conftest import build, configuration, finish

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
