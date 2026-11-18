"""规则里的冻结路径（碰到停下问 Kun）和串行路径（碰到的活一件一件过执行）。"""
from copy import deepcopy

import pytest

from workshop.core.models import Answer, Evidence
from workshop.core.rules import Rules
from conftest import FakeFlow, build, configuration, current, event, finish, item


class PathFlow(FakeFlow):
    """按活和阶段给改动路径：说清楚交预计路径，之后交实际改动。"""

    def __init__(self):
        super().__init__()
        self.paths = {}

    def assess(self, item, receipt, target):
        paths = self.paths.get((item["id"], item["stage"]), self.paths.get(item["id"], []))
        return Evidence(self.checks, self.passed, self.actions, paths=list(paths))


@pytest.fixture
def make(tmp_path):
    engines = []

    def factory(**rule):
        config = configuration()
        config["targets"]["alpha"].update(rule)
        engine = build(tmp_path / str(len(engines)), config)
        engine.plugins.register("flow", "flow", PathFlow())
        engines.append(engine)
        return engine

    yield factory
    for engine in engines:
        engine.log.close()


def flow(engine):
    return engine.plugins.get("flow", "flow")


def start(engine, key):
    engine.ingest(event(key, ext_id=key))
    engine.tick()


def walk(engine, key, stages):
    for stage in stages:
        finish(engine, stage, key=key)


def close(engine, key):
    """收尾完成：交收尾回执并登记后面的事。"""
    finish(engine, key=key, leave={"kind": "工序", "owner": "车间", "at": "+7d", "about": "复看"})





# ---------- 串行路径 ----------

def test_second_item_runs_when_first_enters_acceptance(make):
    engine = make(serial_paths=["cards/"])
    flow(engine).paths = {"alpha:one": ["cards/x.py"], "alpha:two": ["cards/"]}
    start(engine, "one")
    walk(engine, "one", ["说清楚", "执行"])
    assert current(engine, "one")["stage"] == "执行"
    assert item(engine, "one")["serial_hold"]

    start(engine, "two")
    walk(engine, "two", ["说清楚", "执行"])
    queued = item(engine, "two")
    assert queued["stage"] == "执行" and queued["state"] == "排队"
    assert queued["blocked_by"] == "alpha:one" and "alpha:one" in queued["next"]
    assert not [r for r in engine.active() if r["item"] == "alpha:two"]
    assert queued["leave"]["kind"] == "工序"

    # 前一件退回执行期间仍占着。
    walk(engine, "one", ["证据", "审查"])
    finish(engine, "执行", key="one")
    assert current(engine, "one")["stage"] == "执行"
    assert item(engine, "two")["state"] == "排队"

    for stage in ("证据", "审查", "交付"):
        finish(engine, stage, key="one")
        assert item(engine, "one")["serial_hold"]
        assert item(engine, "two")["state"] == "排队"
        assert "收尾" not in item(engine, "two")["next"]
        assert not [r for r in engine.active() if r["item"] == "alpha:two"]
    finish(engine, "验收", key="one")
    engine.tick()
    assert not item(engine, "one")["serial_hold"]
    running = current(engine, "two")
    assert running["stage"] == "执行"
    assert item(engine, "two")["serial_hold"] and item(engine, "two")["blocked_by"] == ""
    # 后件占位不能把前件的验收或收尾反过来排队。
    assert current(engine, "one")["stage"] == "验收"
    finish(engine, "收尾", key="one")
    assert current(engine, "one")["stage"] == "收尾"
    close(engine, "one")
    assert item(engine, "one")["state"] == "完成"


@pytest.mark.parametrize("occupied", [False, True])
@pytest.mark.parametrize("legacy_hold", [False, True])
def test_acceptance_rejection_reacquires_or_queues(make, occupied, legacy_hold):
    engine = make(serial_paths=["cards/"])
    flow(engine).paths = {f"alpha:{k}": ["cards/x.py"] for k in ("one", "two")}
    start(engine, "one")
    walk(engine, "one", ["说清楚", "执行", "证据", "审查", "交付", "验收"])
    if occupied:
        start(engine, "two")
        walk(engine, "two", ["说清楚", "执行"])
        assert current(engine, "two")["stage"] == "执行"
    if legacy_hold:
        # 模拟旧版本留下的事实，不能经新的状态变更方法提前清掉。
        engine.fact("item_changed", {"serial_hold": True}, "alpha:one")
    flow(engine).passed = False
    finish(engine, "执行", key="one")
    flow(engine).passed = True
    if occupied:
        queued = item(engine, "one")
        assert queued["stage"] == "执行" and queued["state"] == "排队"
        assert not queued["serial_hold"]
        assert queued["blocked_by"] == "alpha:two" and "alpha:two" in queued["next"]
        assert not [r for r in engine.active() if r["item"] == "alpha:one"]
        walk(engine, "two", ["证据", "审查", "交付", "验收"])
        engine.tick()
    assert current(engine, "one")["stage"] == "执行"
    assert item(engine, "one")["serial_hold"] and not item(engine, "one")["blocked_by"]


@pytest.mark.parametrize("stage", ["说清楚", "验收", "收尾", "观察"])
@pytest.mark.parametrize("state", ["空闲", "排队"])
def test_old_hold_outside_execution_to_delivery_is_released(make, stage, state):
    engine = make(serial_paths=["cards/"])
    flow(engine).path = (*flow(engine).path, "观察")
    flow(engine).paths = {f"alpha:{k}": ["cards/x.py"] for k in ("one", "two")}
    start(engine, "one")
    walk(engine, "one", ["说清楚", "执行"])
    # 先结束活动轮，再放入旧版本的停留/排队状态。
    engine.abort(current(engine, "one")["token"])
    engine.tick()
    engine.fact("item_changed", {"stage": stage, "state": state, "serial_hold": True,
                "blocked_by": "alpha:two" if state == "排队" else "",
                "next": "排队：等 alpha:two 收尾让出串行路径" if state == "排队" else stage}, "alpha:one")
    start(engine, "two")
    walk(engine, "two", ["说清楚", "执行"])
    assert current(engine, "two")["stage"] == "执行"
    assert current(engine, "one")["stage"] == stage
    assert not item(engine, "one")["serial_hold"]
    assert not item(engine, "one")["blocked_by"]
    assert "收尾让出" not in item(engine, "one")["next"]


def test_undeclared_paths_queue_before_evidence(make):
    engine = make(serial_paths=["cards/"])
    flow(engine).paths = {"alpha:one": ["cards/x.py"], ("alpha:three", "执行"): ["cards/y.py", "src/z.py"]}
    start(engine, "one")
    walk(engine, "one", ["说清楚", "执行"])
    start(engine, "three")
    walk(engine, "three", ["说清楚", "执行"])
    assert current(engine, "three")["stage"] == "执行"  # 说清楚没登记，照常进执行。
    finish(engine, "证据", key="three")
    held = item(engine, "three")
    assert held["stage"] == "证据" and held["state"] == "排队" and held["blocked_by"] == "alpha:one"
    assert "cards/y.py" in held["paths"]
    assert not [r for r in engine.active() if r["item"] == "alpha:three"]

    walk(engine, "one", ["证据", "审查", "交付", "验收"])
    assert current(engine, "three")["stage"] == "证据"


def test_undeclared_paths_pass_when_nobody_holds(make):
    engine = make(serial_paths=["cards/"])
    flow(engine).paths = {("alpha:three", "执行"): ["cards/y.py"]}
    start(engine, "three")
    walk(engine, "three", ["说清楚", "执行", "证据"])
    assert current(engine, "three")["stage"] == "证据"
    assert item(engine, "three")["serial_hold"]


def test_items_off_serial_paths_run_side_by_side(make):
    engine = make(serial_paths=["cards/"])
    flow(engine).paths = {"alpha:one": ["cards/x.py"], "alpha:two": ["src/y.py"]}
    for key in ("one", "two"):
        start(engine, key)
        walk(engine, key, ["说清楚", "执行"])
    assert current(engine, "one")["stage"] == "执行"
    assert current(engine, "two")["stage"] == "执行"
    assert not item(engine, "two").get("serial_hold")


def test_without_serial_rule_both_run(make):
    engine = make()
    flow(engine).paths = {"alpha:one": ["cards/x.py"], "alpha:two": ["cards/x.py"]}
    for key in ("one", "two"):
        start(engine, key)
        walk(engine, key, ["说清楚", "执行"])
    assert current(engine, "two")["stage"] == "执行"


def test_three_items_released_one_by_one_in_order(make):
    engine = make(serial_paths=["cards/"])
    config = deepcopy(engine.rules.data)
    config["limits"]["concurrent"] = 3
    engine.rules = Rules.validate(config)
    flow(engine).paths = {f"alpha:{k}": ["cards/x.py"] for k in ("one", "two", "three")}
    for key in ("one", "two", "three"):
        start(engine, key)
        walk(engine, key, ["说清楚", "执行"])
    assert item(engine, "two")["state"] == item(engine, "three")["state"] == "排队"
    walk(engine, "one", ["证据", "审查", "交付", "验收"])
    engine.tick()
    assert current(engine, "two")["stage"] == "执行"
    three = item(engine, "three")
    assert three["state"] == "排队" and three["blocked_by"] == "alpha:two"


def test_reaching_complete_again_after_rework_queues_behind_new_holder(make):
    engine = make(serial_paths=["cards/"])
    flow(engine).paths = {"alpha:one": ["cards/x.py"], "alpha:two": ["cards/x.py"]}
    start(engine, "one")
    walk(engine, "one", ["说清楚", "执行", "证据", "审查", "交付", "验收", "收尾"])
    close(engine, "one")
    start(engine, "two")
    walk(engine, "two", ["说清楚", "执行"])
    assert current(engine, "two")["stage"] == "执行"
    # 已收尾的活又被返工，回到执行时排在当前占着的那件后面。
    engine.ingest(event("one", ext_id="again", action="rework"))
    engine.tick()
    again = item(engine, "one")
    assert again["stage"] == "执行" and again["state"] == "排队" and again["blocked_by"] == "alpha:two"


# ---------- 冻结路径 ----------

def reach_evidence(engine, paths):
    flow(engine).paths = {("alpha:one", "证据"): paths, ("alpha:one", "审查"): paths}
    start(engine, "one")
    walk(engine, "one", ["说清楚", "执行", "证据"])
    finish(engine, "审查", key="one")


def test_legacy_frozen_paths_do_not_create_gate(make):
    engine = make(frozen_paths=["deploy/", "docs/ci.md"])
    reach_evidence(engine, ["deploy/a.yaml", "docs/ci.md"])
    assert current(engine, "one")["stage"] == "审查"
    assert not engine.log.all("cards")











def test_untouched_frozen_paths_pass(make):
    engine = make(frozen_paths=["deploy/"])
    reach_evidence(engine, ["src/b.py", "deployment.md"])
    assert current(engine, "one")["stage"] == "审查"
    assert not engine.log.all("cards")





def test_serial_paths_must_be_string_lists(make):
    for value in ('deploy/', ['']):
        config = configuration()
        config["targets"]["alpha"]["serial_paths"] = value
        with pytest.raises(ValueError, match="serial_paths"):
            Rules.validate(config)


def test_repository_rules_keep_serial_paths_without_frozen_gate():
    from pathlib import Path
    target = Rules.load(Path("rules/workshop.yaml")).target("grant-console")
    assert 'frozen_paths' not in target
    assert any("lark_receiver" in p for p in target["serial_paths"])


# ---------- 放弃让出串行路径 ----------

def test_abandoned_holder_releases_next(make):
    engine = make(serial_paths=["cards/"])
    flow(engine).paths = {"alpha:one": ["cards/x.py"], "alpha:two": ["cards/x.py"]}
    start(engine, "one")
    walk(engine, "one", ["说清楚", "执行"])
    start(engine, "two")
    walk(engine, "two", ["说清楚", "执行"])
    assert item(engine, "two")["state"] == "排队"
    with pytest.raises(ValueError, match="abort"):
        engine.abandon("alpha:one", "不做了")  # 还有一轮在跑。
    engine.abort(current(engine, "one")["token"])
    engine.tick()
    with pytest.raises(ValueError, match="原因"):
        engine.abandon("alpha:one", " ")
    engine.abandon("alpha:one", "需求取消")
    gone = item(engine, "one")
    assert gone["state"] == "放弃" and "需求取消" in gone["next"] and not gone["serial_hold"]
    assert gone["leave"]["kind"] == "等你"
    assert any(f["kind"] == "item_abandoned" for f in engine.log.facts("alpha:one"))
    engine.tick()
    assert current(engine, "two")["stage"] == "执行"
    assert not [r for r in engine.active() if r["item"] == "alpha:one"]
    with pytest.raises(ValueError, match="放弃"):
        engine.abandon("alpha:one", "再放弃一次")

    # 放弃的活被返工，回到执行，排在现在占着的那件后面。
    engine.ingest(event("one", ext_id="again", action="rework"))
    engine.tick()
    back = item(engine, "one")
    assert back["stage"] == "执行" and back["state"] == "排队" and back["blocked_by"] == "alpha:two"


def test_abandon_cancels_open_questions(make):
    engine = make()
    start(engine, 'one')
    finish(engine, key='one', ask=[{'key':'q', 'title':'选择方案', 'options':['A','B']}])
    engine.abandon('alpha:one', '需求取消')
    assert engine.log.all('cards')[0]['cancelled']
    engine.tick()
    assert item(engine, 'one')['state'] == '放弃' and not engine.active()


def test_cli_abandon(make, tmp_path, capsys):
    from workshop.cli import main
    engine = make(serial_paths=["cards/"])
    start(engine, "one")
    engine.abort(current(engine, "one")["token"])
    engine.tick()
    state_dir = engine.log.path.parent
    rules = tmp_path / "rules.yaml"
    import yaml
    rules.write_text(yaml.safe_dump(engine.rules.data, allow_unicode=True))
    main(["--state-dir", str(state_dir), "--rules", str(rules), "abandon", "alpha:one", "--reason", "重复的活"])
    assert "已放弃 alpha:one" in capsys.readouterr().out
    engine.log.db.close()
    from workshop.core.log import Log
    engine.log = Log(state_dir / "workshop.db")
    assert item(engine, "one")["state"] == "放弃"


def test_repository_rules_serialize_card_callback():
    from pathlib import Path
    target = Rules.load(Path("rules/workshop.yaml")).target("grant-console")
    assert "services/devops-console/grant_console/app.py" in target["serial_paths"]


def stopped(engine, key="one"):
    start(engine, key)
    engine.abort(current(engine, key)["token"])
    engine.tick()


def test_abandon_drops_rework_received_before(make):
    engine = make()
    stopped(engine)
    engine.ingest(event("one", ext_id="before", action="rework"))  # 收到了，还没处理。
    assert item(engine, "one")["pending_rework"]
    engine.abandon("alpha:one", "不做了")
    engine.tick()
    assert item(engine, "one")["state"] == "放弃" and not engine.active()
    engine.ingest(event("one", ext_id="after", action="rework"))
    engine.tick()
    assert item(engine, "one")["state"] == "跑着"  # 放弃之后的新返工才让它回来。


def test_abandon_survives_plugin_recovery(make):
    engine = make()
    stopped(engine)
    engine.plugins.get("flow", "flow").crash = True
    engine.broken("alpha", "flow", "flow", RuntimeError("插件坏了"))
    assert item(engine, "one")["state"] == "卡住" and item(engine, "one")["plugin_blocked"]
    engine.abandon("alpha:one", "不做了")
    engine.plugins.get("flow", "flow").crash = False
    engine.resume("alpha", [])
    engine.tick()
    assert item(engine, "one")["state"] == "放弃" and not engine.active()
    engine.broken("alpha", "flow", "flow", RuntimeError("又坏了"))
    assert item(engine, "one")["state"] == "放弃"


def test_old_timers_do_not_bother_after_abandon(make):
    engine = make()
    stopped(engine)
    engine.timer(item(engine, "one"), "send-back", "+1h", "一小时后问一下", kind="送回", owner="Kun")
    engine.timer(item(engine, "one"), "other", "+1h", "别人的工序", owner="别人")
    engine.abandon("alpha:one", "不做了")
    from datetime import timedelta
    engine.clock.now += timedelta(hours=2)
    engine.tick()
    cards = [c for c in engine.log.all("cards") if c["item"] == "alpha:one" and ("send-back" in c["id"] or "other" in c["id"])]
    assert not cards and item(engine, "one")["state"] == "放弃"


def kun_says(engine, ext_id, action="rework"):
    flow(engine).upstream[("cli:input", ext_id)] = {"sender": "Kun", "action": action}
    engine.ingest(event("one", ext_id=ext_id, action="update"), ingress="external")


def test_external_rework_received_before_abandon_does_not_revive(make):
    engine = make()
    stopped(engine)
    kun_says(engine, "before")  # 外部入口先记下，等分类。
    assert [e for e in engine.log.all("effects") if e.get("kind") == "classify" and not e["done"]]
    engine.abandon("alpha:one", "不做了")
    engine.tick()
    engine.tick()
    gone = item(engine, "one")
    assert gone["state"] == "放弃" and not gone["pending_rework"] and not engine.active()
    assert any(m.get("ext_id") == "before" for m in gone["materials"])  # 仍记成资料。

    kun_says(engine, "after")  # 放弃之后的新返工照常生效。
    engine.tick()
    engine.tick()
    assert item(engine, "one")["state"] == "跑着"
    before = {t: engine.log.all(t) for t in ("items", "effects")}
    engine.log.rebuild()
    assert before == {t: engine.log.all(t) for t in ("items", "effects")}


def test_paused_rework_replayed_after_abandon_does_not_revive(make):
    engine = make()
    stopped(engine)
    engine.pause("alpha")
    engine.ingest(event("one", ext_id="held", action="rework"))
    engine.abandon("alpha:one", "不做了")
    missed = [e["id"] for e in engine.missed("alpha")]
    engine.resume("alpha", missed, expected=missed)
    engine.tick()
    assert item(engine, "one")["state"] == "放弃" and not engine.active()


def test_paused_external_rework_replayed_after_abandon_does_not_revive(make):
    engine = make()
    stopped(engine)
    engine.pause("alpha")
    kun_says(engine, "held")  # 暂停期间从外部入口进来，先记成漏掉的事件。
    engine.abandon("alpha:one", "不做了")
    missed = [e["id"] for e in engine.missed("alpha")]
    assert missed
    engine.resume("alpha", missed, expected=missed)
    engine.tick()
    engine.tick()
    gone = item(engine, "one")
    assert gone["state"] == "放弃" and not gone["pending_rework"] and not engine.active()
    entry = next(e for e in engine.log.all("effects") if e.get("kind") == "classify" and e["event"]["ext_id"] == "held")
    assert entry["received_seq"] == int(missed[0]) and entry["action"] == "update"

    kun_says(engine, "fresh")  # 放弃之后的新返工照常生效。
    engine.tick()
    engine.tick()
    assert item(engine, "one")["state"] == "跑着"
