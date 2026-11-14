"""执行者回执里的通知只在 Kun 需要知道时发（ws#46）：留下等你，或这件活完成；完成通知按完成标识只发一张。"""
import pytest

from workshop.core.models import Evidence, Receipt
from workshop.core.report import why
from conftest import current, finish, item, reach


def notices(engine):
    return [c for c in engine.log.all("cards") if c["mode"] == "notice"]


def sent(engine):
    return [card for mode, card in engine.plugins.get("channel", "channel").messages.values() if mode == "notice"]


def test_progress_notices_are_held_and_visible_in_why(engine):
    reach(engine, "接单")
    finish(engine, "说清楚", notice=[{"key": "intake", "text": "ws#41 已接单"}])
    finish(engine, "执行", notice=[{"key": "clear", "text": "ws#32 已说清楚，进入执行"}])
    finish(engine, "证据", notice=[{"key": "opened", "text": "PR #147 已开"}])
    engine.tick()
    assert item(engine)["stage"] == "证据"
    assert notices(engine) == [] and sent(engine) == []
    text = why(engine.log, "alpha:one")
    for subject in ("ws#41 已接单", "ws#32 已说清楚，进入执行", "PR #147 已开"):
        assert f"「{subject}」" in text
    assert text.count("进度通知不发") == 3


def test_notice_without_question_blocks_and_is_held(engine):
    reach(engine, "说清楚")
    finish(engine, notice=[{"key": "stuck", "text": "卡在验收标准，先聊清范围"}],
           leave={"kind": "等你", "about": "等 Kun 改好同一 issue 并重打 Ready for Agent"})
    engine.tick()
    assert item(engine)["state"] == "卡住"
    assert notices(engine) == []
    assert sent(engine) == []


def test_executor_failure_without_question_is_blocked(engine):
    reach(engine, "执行")
    row = current(engine)
    engine.submit(row["token"], {"outcome": "nothing", "summary": "验收拿不到提交",
                                 "notice": [{"key": "env", "subject": "ws#9 卡在环境", "text": "验收拿不到提交", "fyi": True}],
                                 "leave": {"kind": "等你", "about": "验收拿不到提交"}})
    engine.tick()
    assert notices(engine) == []
    assert item(engine)["state"] == "卡住"


def test_completion_notice_is_sent_once_whatever_the_keys(engine):
    flow = engine.plugins.get("flow", "flow")
    flow.assess = lambda item, receipt, target: Evidence(flow.checks, flow.passed, flow.actions, done_key="abc123")
    reach(engine, "验收")
    finish(engine, "收尾", notice=[{"key": "acceptance-ws37", "text": "ws#37 已验收"},
                                  {"key": "another", "text": "ws#37 已验收（同轮重复）"}])
    assert [c["payload"]["text"] for c in notices(engine)] == ["ws#37 已验收"]
    finish(engine, leave={"kind": "送回", "at": "+7d", "about": "复看"},
           notice=[{"key": "accepted", "text": "ws#37 已验收，不对就在群里回一句「ws#37 不对」"},
                   {"key": "accepted-again", "text": "ws#37 已验收（重复）"}])
    engine.tick()
    assert item(engine)["state"] == "完成"
    cards = notices(engine)
    assert len(cards) == 1 and cards[0]["key"] == "notice:完成:abc123"
    assert [c["text"] for c in sent(engine)] == ["ws#37 已验收"]
    text = why(engine.log, "alpha:one")
    assert "「ws#37 已验收（同轮重复）」：这次完成已发过通知（abc123）" in text
    assert "「ws#37 已验收（重复）」：这次完成已发过通知（abc123）" in text


def test_same_done_key_never_sends_a_second_completion_card(engine):
    reach(engine, "收尾")
    work = item(engine)
    first = Receipt("done", "收尾", notice=[{"key": "accepted", "subject": "已验收", "text": "第一次", "fyi": True}])
    again = Receipt("done", "收尾", notice=[{"key": "accepted-2", "subject": "已验收", "text": "重走验收后", "fyi": True}])
    with engine.log.transaction():
        engine.receipt_notices(work, "收尾", "t1", first, Evidence(done_key="abc123"), True)
        engine.receipt_notices(work, "收尾", "t2", again, Evidence(done_key="abc123"), True)
    assert [c["payload"]["text"] for c in notices(engine)] == ["第一次"]
    with engine.log.transaction():
        engine.receipt_notices(work, "收尾", "t3", again, Evidence(done_key="def456"), True)
    assert [c["payload"]["text"] for c in notices(engine)] == ["第一次", "重走验收后"]


@pytest.mark.parametrize('passed,outcome,advance', [
    (False, 'done', '收尾'),
    (True, 'nothing', '收尾'),
    (True, 'done', None),
    (True, 'done', '执行'),
])
def test_completion_key_does_not_bypass_the_gate(engine, passed, outcome, advance):
    reach(engine, '验收')
    flow = engine.plugins.get('flow', 'flow')
    flow.assess = lambda item, receipt, target: Evidence(
        flow.checks, passed, flow.actions, done_key='abc123')
    engine.submit(current(engine)['token'], {
        'outcome': outcome, 'summary': '尚未通过并向前推进', 'advance': advance,
        'notice': [{'key': 'early', 'subject': '过早的完成通知', 'text': '不能发', 'fyi': True}],
    })
    engine.tick()
    assert notices(engine) == [] and sent(engine) == []
    assert '过早的完成通知' in why(engine.log, 'alpha:one')


def test_rules_hold_keeps_every_notice_off_the_channel_but_asks_still_go(tmp_path):
    from conftest import build, configuration
    from workshop.core.cards import notice
    config = configuration()
    config["notices"] = "hold"
    engine = build(tmp_path, config)
    try:
        reach(engine, "说清楚")
        finish(engine, notice=[{"key": "stuck", "text": "卡在验收标准"}],
               leave={"kind": "等你", "about": "等 Kun"})
        engine.card({"id": "alpha:one"}, "plugin:flow:x:1", "notice", notice("alpha 插件故障：boom"))
        engine.tick()
        assert notices(engine) == [] and sent(engine) == []
        text = why(engine.log, "alpha:one")
        assert "「卡在验收标准」" in text and "插件故障" in text and "notices: hold" in text
    finally:
        engine.log.close()


def test_rules_reject_unknown_notices_value():
    from conftest import configuration
    from workshop.core.rules import Rules
    config = configuration()
    config["notices"] = "quiet"
    with pytest.raises(ValueError):
        Rules.validate(config)
