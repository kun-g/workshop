"""交付对象隔离期间，已 abort 的失败轮能结束，活能放弃（核心 R15、R20）。"""
import pytest

from workshop.core.holding import abandon
from conftest import current, event, finish, item, reach


def isolate(engine):
    engine.broken("alpha", "flow", "flow", RuntimeError("坏了"))
    assert engine.flow_broken("alpha")


def forbid_flow(engine):
    flow = engine.plugins.get("flow", "flow")
    for name in ("assess", "stages"):
        setattr(flow, name, lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("隔离期间不得调流程插件")))


def test_aborted_round_ends_during_isolation_and_item_can_be_abandoned(engine):
    reach(engine, "执行")
    row = current(engine)
    stage = item(engine)["stage"]
    engine.abort(row["token"])
    isolate(engine)
    forbid_flow(engine)
    rounds = len(engine.log.all("rounds"))
    engine.tick()
    ended = engine.log.get("rounds", row["token"])
    assert ended["status"] == "ended" and ended["outcome"] == "failed"
    assert not engine.active()
    assert len(engine.log.all("rounds")) == rounds  # 不派新轮
    assert item(engine)["stage"] == stage and item(engine)["round"] is None  # 不推进
    assert item(engine)["state"] == "卡住"  # 仍卡在插件故障上
    engine.tick()
    assert not engine.active()
    abandon(engine, "alpha:one", "Kun 说撤")
    assert item(engine)["state"] == "放弃"


def test_recover_still_refused_during_isolation(engine):
    reach(engine, "执行")
    engine.abort(current(engine)["token"])
    isolate(engine)
    engine.tick()
    assert not engine.active()
    with pytest.raises(ValueError, match="插件尚在隔离，先修复插件"):
        engine.recover("alpha:one", "wait", "试试")


def test_unaborted_round_waits_for_isolation_to_clear(engine):
    reach(engine, "执行")
    row = current(engine)
    engine.submit(row["token"], {"outcome": "failed", "summary": "不成"})
    isolate(engine)
    forbid_flow(engine)
    engine.tick()
    assert engine.log.get("rounds", row["token"])["status"] == "finishing"
    with pytest.raises(ValueError, match="还有一轮没结束"):
        abandon(engine, "alpha:one", "撤")
    engine.resume("alpha", [])
    flow = engine.plugins.get("flow", "flow")
    del flow.assess, flow.stages
    engine.tick()
    assert engine.log.get("rounds", row["token"])["status"] == "ended"


def test_aborted_round_resumes_normally_after_isolation(engine):
    reach(engine, "执行")
    row = current(engine)
    engine.abort(row["token"])
    isolate(engine)
    engine.tick()
    assert engine.log.get("rounds", row["token"])["status"] == "ended"
    engine.resume("alpha", [])
    assert not item(engine)["plugin_blocked"]
    assert item(engine)["state"] == "定时" and item(engine)["failures"] == 1
