"""拒收与上游作废必须成为事实，不能冒充回答或堵住其他卡。"""
from workshop.core.cards import question
from workshop.core.models import Answer
from workshop.core.ports import ChannelRejected
from workshop.core.report import why
from conftest import event, item


def waiting_card(engine, key="first"):
    engine.ingest(event())
    current = item(engine)
    engine.change(current, state="等你")
    return engine.card(current, key, "ask", question("批准操作", approval=True), core_approval=True)


def test_rejected_card_records_reason_waits_and_notifies_once(engine):
    cid = waiting_card(engine)
    channel = engine.plugins.get("channel", "channel")
    attempts = []

    def reject(key, payload):
        attempts.append(key)
        raise ChannelRejected("上游 stderr：不合规，尚未发送")

    channel.ask = reject
    for _ in range(4):
        engine.channels()
    card = engine.log.get("cards", cid)
    assert card["rejected"] and not card["sending"] and not card["sent"]
    assert attempts == [cid]
    assert item(engine)["state"] == "等你" and item(engine)["wait_for_ready"]
    assert "上游 stderr：不合规，尚未发送" in why(engine.log, "alpha:one")
    notices = [v for v in channel.messages.values() if v[0] == "notice"]
    assert len(notices) == 1
    assert notices[0][1]["text"] == "车间有一张卡发不出去，原因已记录，请看 workshop why alpha:one"
    assert not [f for f in engine.log.facts() if f["kind"] == "reconcile_failed"]
    before = engine.log.all("cards")
    engine.log.rebuild()
    assert engine.log.all("cards") == before


def test_rejection_notice_itself_rejected_does_not_recurse(engine):
    waiting_card(engine)
    channel = engine.plugins.get("channel", "channel")

    def reject(*args):
        raise ChannelRejected("命令不存在")

    channel.ask = channel.notify = reject
    for _ in range(5):
        engine.channels()
    assert len(engine.log.all("cards")) == 2
    assert len([f for f in engine.log.facts() if f["kind"] == "card_rejected"]) == 2


def test_cancelled_external_card_is_not_answer_and_does_not_authorize(engine):
    cid = waiting_card(engine)
    engine.channels()
    channel = engine.plugins.get("channel", "channel")
    engine.change(item(engine), pending_advance={"card": cid})
    channel.answers = [Answer(cid, "yes", True, cancelled=True)]
    engine.channels()
    engine.channels()
    card = engine.log.get("cards", cid)
    assert card["cancelled"] and card["cancel_done"] and not card["answered"]
    assert not item(engine)["approved"]
    assert item(engine)["pending_advance"] is None
    assert item(engine)["state"] == "等你" and item(engine)["wait_for_ready"]
    assert "作废" in why(engine.log, "alpha:one")
    assert not [f for f in engine.log.facts() if f["kind"] == "answer"]
    assert len([c for c in engine.log.all("cards") if c["mode"] == "notice"]) == 1


def test_other_card_answer_does_not_clear_undeliverable_wait(engine):
    cid = waiting_card(engine)
    other = engine.card(item(engine), "second", "ask", question("确认方案"))
    engine.channels()
    channel = engine.plugins.get("channel", "channel")
    channel.answers = [Answer(cid, "", cancelled=True), Answer(other, "yes")]
    engine.channels()
    assert engine.log.get("cards", other)["answered"]
    assert item(engine)["state"] == "等你" and item(engine)["wait_for_ready"]
