"""issue #5：审批卡收到非选项回答时通知 Kun 一次，不算通道故障。"""
import json

from workshop.core.models import Answer
from workshop.reports.report import why
from conftest import current, item
from test_review_round3 import approval_wait


def heartbeat(engine):
    return json.loads((engine.log.path.parent / "heartbeat.json").read_text())


def invalid_notices(engine):
    return [c for c in engine.log.all("cards") if c["key"].startswith("invalid-answer:")]


def test_invalid_answer_notifies_once_and_keeps_waiting(engine):
    card = approval_wait(engine)
    assert [c["key"] for c in card["payload"]["choices"]] == ["yes", "no"]
    channel = engine.plugins.get("channel", "channel")
    sent = []
    notify = channel.notify
    channel.notify = lambda key, payload: sent.append((key, payload)) or notify(key, payload)

    channel.answers = [Answer(card["id"], "批准", True)]
    engine.tick()
    engine.tick()
    assert len(sent) == 1
    text = sent[0][1]["text"]
    assert item(engine)["title"] in text and card["payload"]["decision"] in text and "「批准」" in text
    assert "点按钮重新回答" in text and sent[0][1]["fyi"] is True
    assert heartbeat(engine)["delivery_failed_count"] == 0 and heartbeat(engine)["delivery_failed_since"] is None
    assert not [f for f in engine.log.facts() if f["kind"] == "delivery_failed"]
    assert "非选项回答" in why(engine.log, item(engine)["id"]) and "「批准」" in why(engine.log, item(engine)["id"])

    channel.answers = [Answer(card["id"], "批准", True)]
    engine.tick()
    engine.tick()
    assert len(sent) == 1 and len(invalid_notices(engine)) == 1
    assert len([f for f in engine.log.facts() if f["kind"] == "invalid_answer"]) == 1
    assert item(engine)["state"] == "等你" and not engine.log.get("cards", card["id"])["answered"]

    channel.answers = [Answer(card["id"], "yes", False)]
    engine.tick()
    assert current(engine)["stage"] == "审查"
    assert heartbeat(engine)["delivery_failed_count"] == 0


def test_invalid_answer_notice_failure_is_recorded_as_delivery_failure(engine):
    card = approval_wait(engine)
    channel = engine.plugins.get("channel", "channel")
    def fail(key, payload):
        raise ConnectionError("通知发不出去")
    channel.notify = fail
    channel.answers = [Answer(card["id"], "批准", True)]
    engine.tick()
    engine.tick()
    notice_card = invalid_notices(engine)[0]
    failures = [f for f in engine.log.facts() if f["kind"] == "delivery_failed"]
    assert [f["body"]["key"] for f in failures] == [notice_card["id"]]
    assert engine.control(f"delivery:{notice_card['id']}")["failed"]
    assert heartbeat(engine)["delivery_failed_count"] == 1
