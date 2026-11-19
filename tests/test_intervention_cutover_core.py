"""#176 介入点存量切换后的核心回归：卡片和通道退役，「等你」只看等待引用，到点车间接着做。"""
from datetime import timedelta
import inspect
import json
from pathlib import Path

import pytest

from conftest import configuration, current, event, finish, item, reach, use_wait, build
from test_issue_delivery import world, message, edit, calls  # noqa: F401  world 是夹具
from workshop.core.models import After, Event, Receipt
from workshop.core.rules import Rules
import workshop.plugins.issue_delivery as issue_delivery


WAIT = {"provider": "wait", "ref": "kun-decision", "operation": "bind:kun-decision"}


def test_wait_backed_waiting_holds_without_cards_and_resumes_on_ready(engine):
    port = use_wait(engine)
    reach(engine, "执行")
    flow = engine.plugins.get("flow", "flow")
    original = flow.after
    flow.after = lambda item, stage, key, target, services=None: (
        After(pending=True, reason="等 Kun 取舍", next="等 Kun 回答", wait=WAIT)
        if "kun-decision" not in port.ready else original(item, stage, key, target, services))
    finish(engine, "证据")
    for _ in range(5):
        engine.clock.now += timedelta(minutes=10)
        engine.tick()
        held = item(engine)
        assert held["state"] == "等你" and held["stage"] == "执行", held
        assert engine.waiting(held) and not engine.active()
    assert engine.log.all("cards") == []
    assert not [f for f in engine.log.facts() if f["kind"] in ("card_created", "plugin_failed")]
    assert port.polls  # 车间在查等待，不是靠卡片
    port.ready["kun-decision"] = "answered-once"
    engine.clock.now += timedelta(minutes=1)
    engine.tick()
    engine.tick()
    assert item(engine)["stage"] == "证据" and current(engine)["stage"] == "证据"
    assert engine.log.get("waits", "bind:kun-decision")["state"] == "acknowledged"
    assert engine.log.all("cards") == []


def test_waiting_leave_without_wait_becomes_stuck(engine):
    reach(engine, "执行")
    finish(engine, leave={"kind": "等你", "about": "要 Kun 选方案"})
    stuck = item(engine)
    assert stuck["state"] == "卡住" and stuck["wait_for_ready"]
    assert stuck["blocked_by"] == "要 Kun 选方案" and not stuck.get("wait_id")
    assert stuck["leave"]["owner"] == "项目经理"
    assert engine.log.all("cards") == []


def test_setting_waiting_without_wait_reference_is_stuck(engine):
    engine.ingest(event())
    engine.change(item(engine), state="等你", next="等回答")
    assert item(engine)["state"] == "卡住"


@pytest.mark.parametrize("field,value", [("ask", [{"key": "q"}]), ("notice", [{"key": "n"}]), ("notice", "完成")])
def test_receipt_rejects_retired_ask_and_notice(engine, field, value):
    with pytest.raises(ValueError, match="ask、notice 已取消"):
        Receipt.parse({"outcome": "done", "summary": "x", field: value}, engine.clock())


def test_receipt_tolerates_empty_retired_fields_and_stored_drop_them(engine):
    parsed = Receipt.parse({"outcome": "done", "summary": "x", "ask": [], "notice": []}, engine.clock())
    assert "ask" not in parsed.json() and "notice" not in parsed.json()
    stored = Receipt.stored({"outcome": "done", "summary": "x", "ask": [], "notice": [], "evidence": ["e"]})
    assert stored == Receipt("done", "x", evidence=["e"])
    assert After.stored({"pending": False, "notice": "定时发版完成"}) == After()
    assert "notice" not in inspect.signature(After).parameters


@pytest.mark.parametrize("change", [{"channel": "ask_kun"}, {"notices": "hold"}, {"notices": "send"}])
def test_rules_reject_retired_channel_and_notices(change):
    with pytest.raises(ValueError, match="已退役"):
        Rules.validate({**configuration(), **change})


def test_repository_rules_have_no_retired_keys():
    rules = Rules.load(Path(__file__).resolve().parents[1] / "rules/workshop.yaml").data
    assert "channel" not in rules and "notices" not in rules
    for target in rules["targets"].values():
        for key in ("ask_kun_state", "feishu_chat_id", "feishu_profile", "kun_feishu_open_id"):
            assert key not in target


def test_send_back_defaults_to_workshop_and_continues_without_card(engine):
    reach(engine, "执行")
    finish(engine, leave={"kind": "送回", "at": "+1h", "about": "看看外部结果"})
    (timer,) = [t for t in engine.log.all("timers") if t["kind"] == "送回"]
    assert timer["owner"] == "车间"
    assert item(engine)["state"] == "定时" and not engine.active()
    engine.clock.now += timedelta(minutes=59)
    engine.tick()
    assert item(engine)["state"] == "定时"
    engine.clock.now += timedelta(minutes=2)
    engine.tick()
    assert engine.log.get("timers", timer["id"])["fired"]
    assert item(engine)["state"] != "定时" and current(engine)["stage"] == "执行"
    assert engine.log.all("cards") == []


def issue_engine(world):
    cfg = configuration()
    cfg.pop("taskhome")
    cfg["targets"]["alpha"] = {**world["target"], "plugin": "issue_delivery", "sources": []}
    engine = build(world["root"] / "feishu-stopped", cfg)
    engine.plugins.register("flow", "issue_delivery", world["flow"])
    return engine


@pytest.mark.parametrize("text,parent", [("改一下错误提示", None), ("@Ash ws#7 不对，重做", None), ("不对", "om_card")])
def test_feishu_group_message_neither_creates_nor_advances_items(world, text, parent):
    engine = issue_engine(world)
    try:
        engine.ingest(Event("github", "start", "alpha", "7", "test", {}, action="ready"))
        before = item(engine, "7")
        # 上游回查得到的确实是 Kun 在交付对象群里说的话：入口已停，照样不建活、不推进。
        edit(world, message=message(text, parent=parent))
        engine.ingest(Event("feishu", "om_real", "alpha", "om_real", "x",
                            {"from_kun": True, "text": text}), ingress="external")
        engine.classify_events()
        assert engine.log.get("items", "alpha:om_real") is None
        after = item(engine, "7")
        assert not after["pending_rework"]
        assert (after["stage"], after["state"]) == (before["stage"], before["state"])
        discarded = [f for f in engine.log.facts() if f["kind"] == "event_discarded"]
        assert len(discarded) == 1
        assert not [c for c in calls(world) if c[0] == "lark-cli"]  # 不回查飞书，也不开 issue
        assert not [c for c in calls(world) if "--input" in c]
    finally:
        engine.log.close()


def test_issue_delivery_does_not_read_ask_kun_state(world, monkeypatch):
    package = Path(issue_delivery.__file__).parent
    for path in package.rglob("*.py"):
        source = path.read_text()
        assert "ask_kun_state" not in source and "ask-kun/asks" not in source, path
    # 旧规则里的问卡状态文件还在、回复也确实指向那张卡：既不读它，也不按它返工。
    target = {**world["target"], "ask_kun_state": str(world["root"] / "asks.json")}
    (world["root"] / "asks.json").write_text(json.dumps({"asks": {"Q1": {"id": "Q1", "message_id": "om_card"}}}))
    edit(world, message=message("不对", parent="om_card"))
    opened = []
    real_open = Path.open
    def watch_open(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", watch_open)
    reply = Event("cli:feishu", "om_real", "alpha", "om_real", "x", {"from_kun": True, "text": "不对"})
    result = world["flow"].classify(reply, None, target, lambda **_: {"item": {"object_key": "7"}})
    assert result.discard and result.action != "rework"
    assert not [p for p in opened if p.endswith("asks.json")]
