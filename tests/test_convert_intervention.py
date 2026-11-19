"""#176 存量转换脚本：每类旧记录的去向、一批里的改动、重跑不重复、失败停派发和续跑解开。"""
import importlib.util
import json
from pathlib import Path

import pytest

from workshop.core.log import Log
from workshop.core.rules import Rules
from workshop.reports.report import overview

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "rules/workshop.yaml"


def load_script():
    spec = importlib.util.spec_from_file_location("convert_intervention", ROOT / "scripts/convert_intervention.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


convert = load_script()


class FakeService:
    """介入点服务的 create：同一操作号拿回同一个等待号。"""
    def __init__(self):
        self.created = []

    def create(self, operation, value, config):
        self.created.append((operation, value))
        return "wait:" + operation


QUESTION = {"subject": "要不要继续", "decision": "要不要继续", "context": "合成问卡",
            "choices": [{"key": "yes", "label": "继续"}, {"key": "no", "label": "调整"}], "recommend": "yes"}


def item(log, target, key, state, **fields):
    ident = f"{target}:{key}"
    log.append("event", {"source": "test", "ext_id": ident, "target": target, "object_key": key, "title": "合成",
                         "body": {}, "depth": 0, "action": "ready", "flow": "", "initial_stage": "执行"},
               ident, source="test", ext_id=ident)
    log.append("item_changed", {"stage": "执行", "state": state, **fields}, ident)
    return ident


def card(log, owner, suffix, mode="ask", payload=None, **flags):
    body = {"id": f"{owner}:{suffix}", "item": owner, "mode": mode, "payload": payload or QUESTION, "sent": True,
            "answered": False, "sending": False, "cancelled": False, "key": suffix, **flags}
    log.append("card_created", body, owner)
    return body["id"]


def timer(log, owner, target, kind, who):
    ident = f"{owner}:{kind}:{who}"
    log.append("timer_created", {"id": ident, "item": owner, "target": target, "at": "2099-01-01T00:00:00+00:00",
                                 "about": "到点再看", "kind": kind, "owner": who, "fired": False},
               owner, source="timer-plan", ext_id=ident)
    return ident


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSHOP_CUTOVER_RULES", str(RULES))
    log = Log(tmp_path / "workshop.db")
    live = item(log, "workshop", "live", "等你", approved=["旧批准"], billing_approved=["旧按量"],
                frozen_approved=["docs/x.md"], pending_advance={"to": "交付"})
    ended = item(log, "grant-console", "ended", "完成", approved=["旧批准"], frozen_approved=["docs/y.md"])
    ids = {
        "open_ask": card(log, live, "ask", external="Q-old"),
        "answered": card(log, live, "answered", answered=True),
        "cancelled": card(log, live, "cancelled", cancelled=True),
        "approval": card(log, live, "approval", payload={**QUESTION, "kind": "approval"}),
        "ended_ask": card(log, ended, "ask"),
        "sent_notice": card(log, live, "sent", mode="notice", payload={"subject": "s", "text": "t"}),
        "stale_notice": card(log, live, "stale", mode="notice", payload={"subject": "s2", "text": "t2"}, sent=False),
        "found_notice": card(log, live, "found", mode="notice", payload={"subject": "s3", "text": "t3"}, sent=False, sending=True),
        "lost_notice": card(log, ended, "lost", mode="notice", payload={"subject": "s4", "text": "t4"}, sent=False, sending=True),
        "return_open": timer(log, live, "workshop", "送回", "Kun"),
        "step_agent": timer(log, live, "workshop", "工序", "agent"),
        "step_own": timer(log, live, "workshop", "工序", "车间"),
        "return_ended": timer(log, ended, "grant-console", "送回", "Kun"),
    }
    log.append("control", {"id": "reconcile:" + ids["lost_notice"], "count": 3, "at": "2026-11-15T00:00:00+00:00"})
    (tmp_path / "ask-kun-channel").mkdir()
    (tmp_path / "ask-kun-channel" / "x.json").write_text(json.dumps(
        {"external": "N-found", "payload": {"subject": "s3", "text": "t3"}}))
    yield log, tmp_path, ids, live, ended
    log.close()


def destinations(rows):
    return {(r.category, r.id): r.destination for r in rows}


def test_every_legacy_category_has_its_destination(state):
    log, root, ids, live, ended = state
    got = destinations(convert.plan(log, root, convert.ORDER))
    assert got == {
        ("问卡", ids["open_ask"]): "转等待",
        ("问卡", ids["answered"]): "历史：已答",
        ("问卡", ids["cancelled"]): "历史：已作废",
        ("审批卡", ids["approval"]): "取消：审批卡作废",
        ("问卡", ids["ended_ask"]): "取消：活已结束",
        ("通知", ids["sent_notice"]): "历史：已发",
        ("通知", ids["stale_notice"]): "取消：过时通知",
        ("通知", ids["found_notice"]): "对账：已发",
        ("通知", ids["lost_notice"]): "对账：取消",
        ("核心批准", live): "历史：清掉，关口重查",
        ("billing_approved", live): "历史：清掉，关口重查",
        ("frozen_approved", live): "作废：冻结批准",
        ("pending_advance", live): "历史：清掉，关口重查",
        ("核心批准", ended): "历史：不当授权",
        ("frozen_approved", ended): "历史：冻结批准",
        ("送回定时", ids["return_open"]): "改成到点自动续跑",
        ("非车间工序定时", ids["step_agent"]): "改成到点自动续跑",
        ("送回定时", ids["return_ended"]): "取消：活已结束",
        ("通道对账记录", "reconcile:" + ids["lost_notice"]): "历史：通道已退役",
    }
    # 车间自己的工序不是旧记录，不列。
    assert ids["step_own"] not in {key[1] for key in got}


def test_intervention_deliveries_not_sent_are_kept(tmp_path):
    import sqlite3
    db = tmp_path / "intervention.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE records (id TEXT, kind TEXT, body TEXT)")
        for ident, owner, state in (("d1", "workshop:7", "uncertain"), ("d2", "grant-console:3", "sent"), ("d3", "", "sending")):
            conn.execute("INSERT INTO records VALUES (?, 'delivery', ?)",
                         (ident, json.dumps({"state": state, "payload": {"item": owner}})))
    rows = convert.intervention_rows(db, convert.ORDER)
    assert [(r.id, r.target, r.destination) for r in rows] == [("d1", "workshop", "保留"), ("d3", "workshop", "保留")]


def run_all(log, root, service, inject=None):
    rules = Rules.load(RULES)
    return [convert.batch(log, rules, root, target, service, inject, out=lambda _: None) for target in convert.ORDER]


def test_batch_moves_everything_off_the_old_paths_and_rerun_writes_nothing(state):
    log, root, ids, live, ended = state
    service = FakeService()
    run_all(log, root, service)
    current = log.get("items", live)
    # 没答的普通问卡变成介入点服务里的等待；活停在等你，只靠等待引用。
    (operation, value), = service.created
    assert operation == f"decision:{live}:after:cutover:{ids['open_ask']}"
    assert value["point"] == "decision" and value["content"]["recommend"] == "yes"
    assert current["state"] == "等你" and current["wait_id"] == operation
    assert log.get("waits", operation)["ref"] == "wait:" + operation
    assert log.get("controls", "@legacy-card:" + ids["open_ask"])["wait_ref"] == "wait:" + operation
    assert log.get("controls", "@legacy-card:Q-old")["wait_ref"] == "wait:" + operation
    # 旧批准都不当授权。
    assert not current.get("approved") and not current.get("billing_approved") and not current.get("frozen_approved")
    assert not current.get("pending_advance")
    # 没发的、没答的卡全部作废；查到外部编号的通知记已发，不重发。
    for key in ("open_ask", "approval", "ended_ask", "stale_notice", "lost_notice"):
        assert log.get("cards", ids[key])["cancelled"], key
    found = log.get("cards", ids["found_notice"])
    assert found["sent"] and found["external"] == "N-found" and not found["sending"]
    # 送回和别人的工序：开着的活改成车间到点续跑，结束的活取消。
    for key in ("return_open", "step_agent", "return_ended"):
        assert log.get("timers", ids[key])["fired"], key
    for key in ("return_open", "step_agent"):
        new = log.get("timers", ids[key] + ":cutover")
        assert new["owner"] == "车间" and new["kind"] == "工序" and not new["fired"]
    assert log.get("timers", ids["return_ended"] + ":cutover") is None
    assert not log.get("timers", ids["step_own"])["fired"]
    # 每条旧记录一条去向事实，对账无剩余。
    done = convert.converted(log)
    assert not [r for r in convert.plan(log, root, convert.ORDER) if r.key not in done]

    facts = log.db.execute("SELECT count(*) FROM facts").fetchone()[0]
    assert run_all(log, root, service) == [0, 0]
    assert log.db.execute("SELECT count(*) FROM facts").fetchone()[0] == facts + 4  # 只多两批的 running/done 标记
    assert len(service.created) == 1


def test_interrupted_batch_resumes_without_repeating(state):
    log, root, ids, live, ended = state
    service = FakeService()
    rules = Rules.load(RULES)
    planned = convert.plan(log, root, ("workshop",))
    inject = convert.injector("workshop:2", "workshop")
    with pytest.raises(convert.Injected):
        convert.batch(log, rules, root, "workshop", service, inject, out=lambda _: None)
    first = convert.converted(log)
    assert len(first) == 2
    convert.batch(log, rules, root, "workshop", service, out=lambda _: None)
    done = convert.converted(log)
    assert set(first) <= set(done) and set(done) == {r.key for r in planned}
    assert all(done[key] == first[key] for key in first)
    assert len({op for op, _ in service.created}) == len(service.created) == 1


def test_failed_batch_holds_new_dispatch_and_resume_releases_it(state):
    log, root, ids, live, ended = state
    rules = Rules.load(RULES)
    service = FakeService()
    convert.batch(log, rules, root, "workshop", service, out=lambda _: None)
    with pytest.raises(convert.Injected) as failure:
        convert.batch(log, rules, root, "grant-console", service, convert.injector("grant-console:1", "grant-console"),
                      out=lambda _: None)
    reason = convert.fail(log, "grant-console", failure.value)
    control = log.get("controls", "grant-console")
    assert control["paused"] and control["hold"] == "cutover"
    assert log.get("controls", "@cutover:grant-console")["state"] == "failed"
    holds = overview(log, rules=rules.data)["holds"]
    assert holds == [{"target": "grant-console", "reason": reason}]
    # workshop 那批不受影响。
    assert not (log.get("controls", "workshop") or {}).get("paused")

    convert.batch(log, rules, root, "grant-console", service, out=lambda _: None)
    control = log.get("controls", "grant-console")
    assert not control.get("paused") and control.get("hold") is None
    assert log.get("controls", "@cutover:grant-console")["state"] == "done"
    assert overview(log, rules=rules.data)["holds"] == []


def test_hold_does_not_take_over_a_pause_kun_made(state):
    log, root, ids, live, ended = state
    log.append("control", {"id": "grant-console", "paused": True, "reason": "Kun 停的"})
    convert.hold(log, "grant-console", "存量转换没成")
    assert log.get("controls", "grant-console")["reason"] == "Kun 停的"
    assert convert.release(log, "grant-console") == []
    assert log.get("controls", "grant-console")["paused"]


def test_cutover_runs_in_order_then_does_nothing(state, monkeypatch, capsys):
    log, root, ids, live, ended = state
    service = FakeService()
    monkeypatch.setattr(convert, "make_wait", lambda: service)
    argv = ["--state-dir", str(root), "--intervention-db", str(root / "none.db"), "--rules", str(RULES)]
    assert convert.main(argv + ["reconcile"]) == 1
    assert convert.main(argv + ["cutover"]) == 0
    text = capsys.readouterr().out
    assert text.index("workshop：") < text.index("grant-console：")
    assert len(list((root / "cutover-backups").iterdir())) == 1
    assert convert.main(argv + ["cutover"]) == 0
    assert "已转完" in capsys.readouterr().out
    assert len(list((root / "cutover-backups").iterdir())) == 1
    assert convert.main(argv + ["reconcile"]) == 0


def test_cutover_failure_holds_the_rest_and_still_lets_the_release_go_on(state, monkeypatch, capsys):
    log, root, ids, live, ended = state
    monkeypatch.setattr(convert, "make_wait", lambda: FakeService())
    argv = ["--state-dir", str(root), "--intervention-db", str(root / "none.db"), "--rules", str(RULES)]
    assert convert.main(argv + ["--inject-failure", "workshop:1", "cutover"]) == 0
    out = capsys.readouterr().out
    assert "存量转换没成" in out and "前一批 workshop 没转成，本批没跑" in out
    for target in convert.ORDER:
        assert log.get("controls", target)["hold"] == "cutover"


def test_group_message_table_lists_every_group_message_and_switchboard_delivery(tmp_path):
    import sqlite3
    log = Log(tmp_path / "workshop.db")
    try:
        owner = item(log, "grant-console", "109", "完成")
        event = {"source": "cli:feishu", "ext_id": "om_1", "target": "grant-console", "object_key": "109",
                 "title": "gc#109 不对", "body": {"chat_id": "oc_group", "text": "gc#109 不对"}, "depth": 0, "action": "update", "flow": ""}
        log.append("event", event, owner, source="cli:feishu", ext_id="om_1")
        seq = log.db.execute("SELECT seq FROM facts WHERE source='cli:feishu'").fetchone()[0]
        log.append("event_classified", {"id": f"classify:{seq}", "action": "rework", "event_seq": seq}, owner)
        board = tmp_path / "core.sqlite3"
        with sqlite3.connect(board) as db:
            db.execute("CREATE TABLE actions (key TEXT, task TEXT, claimed_by TEXT, claimed_at TEXT, result TEXT, data TEXT)")
            for key, result in (("workshop:feishu:om_1", json.dumps({"status": "done"})), ("workshop:feishu:om_2", None)):
                db.execute("INSERT INTO actions VALUES (?, NULL, '', NULL, ?, ?)",
                           (key, result, json.dumps({"kind": "workshop", "args": {"message": key[-4:], "chat": "oc_group"}})))
        lines = convert.group_messages(log, board)
    finally:
        log.close()
    assert lines[1].split("\t") == ["车间", "om_1", "oc_group", "grant-console", "rework", owner, "执行/完成", "已结束，不再处理"]
    assert lines[2].split("\t")[-1] == "已投完" and lines[3].split("\t")[-1].startswith("没投完")
    assert lines[-1] == "车间收下的群消息 1 条；接线台未完成的车间投递 1 条"
