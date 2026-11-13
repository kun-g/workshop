"""拒收与上游作废必须成为事实，不能冒充回答或堵住其他卡。"""
import json
from datetime import timedelta
import pytest

from workshop.core.cards import question, notice
from workshop.core.models import Answer
from workshop.core.ports import ChannelRejected
from workshop.core.report import why
from conftest import event, item, finish, current


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
        raise ChannelRejected("参数校验不通过；尚未发送")

    channel.ask = channel.notify = reject
    for _ in range(5):
        engine.channels()
    assert len(engine.log.all("cards")) == 2
    assert len([f for f in engine.log.facts() if f["kind"] == "card_rejected"]) == 2
    engine.heartbeat()
    heartbeat = json.loads(engine.log.path.with_name("heartbeat.json").read_text())
    assert heartbeat["delivery_failed_count"] >= 1 and heartbeat["delivery_failed_since"]
    assert any(f["kind"] == "delivery_failed" for f in engine.log.facts())


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


def refuse(*args):
    raise ChannelRejected("用法不合规；尚未发送")


@pytest.mark.parametrize("state", ["跑着", "卡住", "完成", "空闲"])
def test_notice_rejection_never_changes_work_state(engine, state):
    engine.ingest(event())
    engine.change(item(engine), state=state)
    before = item(engine)
    cid = engine.card(before, "notice", "notice", notice("已登记"))
    engine.plugins.get("channel", "channel").notify = refuse
    engine.channels()
    assert item(engine) == before
    assert engine.log.get("cards", cid)["rejected"]
    assert not engine.waiting(item(engine))


def test_running_rejection_waits_at_finish_and_release_clears_flag(engine):
    engine.ingest(event())
    engine.tick()
    token = current(engine)["token"]
    cid = engine.card(item(engine), "question", "ask", question("确认方案"))
    engine.plugins.get("channel", "channel").ask = refuse
    engine.channels()
    assert item(engine)["state"] == "跑着" and item(engine)["round"] == token
    assert engine.waiting(item(engine))
    finish(engine, "说清楚")
    assert item(engine)["state"] == "等你" and not engine.active()
    engine.ingest(event(ext_id="ready", action="ready"))
    assert item(engine)["state"] == "空闲" and not item(engine)["wait_for_ready"]
    assert not engine.waiting(item(engine))
    assert not engine.log.get("cards", cid)["awaiting_release"]


def test_release_while_running_does_not_leave_wait_marker(engine):
    engine.ingest(event())
    engine.tick()
    engine.card(item(engine), "question", "ask", question("确认方案"))
    engine.plugins.get("channel", "channel").ask = refuse
    engine.channels()
    engine.ingest(event(ext_id="ready", action="ready"))
    assert item(engine)["state"] == "跑着" and not item(engine)["wait_for_ready"]
    assert not engine.waiting(item(engine))
    finish(engine, "说清楚")
    assert item(engine)["state"] != "等你"


def test_blocked_rejection_updates_saved_state_and_release_before_restore(engine):
    engine.ingest(event())
    engine.block(item(engine), "插件故障", plugin=True)
    engine.card(item(engine), "question", "ask", question("确认方案"))
    engine.plugins.get("channel", "channel").ask = refuse
    engine.channels()
    assert item(engine)["state"] == "卡住" and item(engine)["blocked_by"] == "插件故障"
    assert item(engine)["before_plugin_failure"]["state"] == "等你"
    assert engine.waiting(item(engine))
    engine.ingest(event(ext_id="ready", action="ready"))
    assert item(engine)["state"] == "卡住" and not item(engine)["wait_for_ready"]
    engine.resume("alpha", [], [])
    assert item(engine)["state"] == "空闲" and not engine.waiting(item(engine))


def test_completed_question_rejection_does_not_reopen_or_leave_wait(engine):
    engine.ingest(event())
    engine.change(item(engine), state="完成")
    before = item(engine)
    engine.card(item(engine), "question", "ask", question("确认方案"))
    engine.plugins.get("channel", "channel").ask = refuse
    engine.channels()
    assert item(engine) == before and not engine.waiting(item(engine))


def test_delivery_failure_heartbeat_recovers_after_success(engine):
    engine.ingest(event())
    engine.card(item(engine), "notice", "notice", notice("已登记"))
    channel = engine.plugins.get("channel", "channel")
    channel.down = True
    engine.channels()
    engine.heartbeat()
    data = json.loads(engine.log.path.with_name("heartbeat.json").read_text())
    assert data["delivery_failed_count"] > 0
    assert data["delivery_failed_since"] == engine.clock().isoformat()
    channel.down = False
    engine.channels()
    engine.heartbeat()
    data = json.loads(engine.log.path.with_name("heartbeat.json").read_text())
    assert data["delivery_failed_count"] == 0 and data["delivery_failed_since"] is None
