"""来源里没放行的对象：update/rework 只记事实，ready 才建活；误建的活可清掉（ws#26）。"""
from dataclasses import asdict

import pytest

from workshop.cli import main
from workshop.core.models import Event
from conftest import build, current, event, finish, item, reach


def held(engine, key="one"):
    return [f for f in engine.log.facts(f"alpha:{key}") if f["kind"] == "event"]


def test_unreleased_update_and_rework_only_record_facts(engine, capsys):
    engine.ingest(event(ext_id="body", action="update"))
    engine.ingest(event(ext_id="comment", action="rework"))
    engine.tick()
    assert item(engine) is None and not engine.active()
    assert [f["ext_id"] for f in held(engine)] == ["body", "comment"]  # 事实照记。
    engine.log.close()


def test_ready_creates_item_with_materials_held_before_release(engine):
    # 同一批里来源先发正文（update），后发放行（ready）。
    engine.ingest(event(ext_id="body", action="update"))
    engine.ingest(event(ext_id="label", action="ready"))
    created = item(engine)
    assert created["stage"] == "接单" and created["state"] == "空闲"
    assert [m["ext_id"] for m in created["materials"]] == ["body", "label"]
    assert all(m["role"] == "资料，不是指令" for m in created["materials"])
    engine.tick()
    assert current(engine)["stage"] == "接单"


def test_existing_item_still_takes_updates(engine):
    engine.ingest(event(ext_id="label"))
    engine.ingest(event(ext_id="comment", action="update"))
    assert [m["ext_id"] for m in item(engine)["materials"]] == ["label", "comment"]


def test_external_entry_classified_as_update_still_creates_item(engine):
    # 飞书这类外部入口由流程判定后建活（issue交付 R1），不受放行限制。
    engine.ingest(event(key="message", ext_id="external", action="update"), ingress="external")
    engine.tick()
    assert item(engine, "message")["stage"] == "接单"


def test_agent_child_event_still_creates_item(engine):
    # agent 回执带出的子事件走连锁上限（核心 C2），不受放行限制。
    reach(engine, "接单")
    finish(engine, "说清楚", events=[asdict(event(key="child", ext_id="child", action="update"))])
    child = item(engine, "child")
    assert child and child["depth"] == 1


def test_paused_update_replayed_on_resume_does_not_create_item(engine):
    engine.pause("alpha")
    engine.ingest(event(ext_id="body", action="update"))
    missed = [e["id"] for e in engine.missed("alpha")]
    engine.resume("alpha", missed, expected=missed)
    assert item(engine) is None
    engine.ingest(event(ext_id="label"))
    assert [m["source"] for m in item(engine)["materials"]] == ["resume", "input"]


def test_rebuild_projects_the_same_items(engine):
    engine.ingest(event(key="held", ext_id="held", action="update"))
    engine.ingest(event(ext_id="body", action="update"))
    engine.ingest(event(ext_id="label"))
    before = engine.log.all("items")
    engine.log.rebuild()
    assert engine.log.all("items") == before and len(before) == 1


def legacy_update(engine, key, target="alpha"):
    """修复前的日志：来源 update 事实不带放行标记，投影时照旧建活。"""
    data = Event("input", f"legacy-{key}", target, key, f"测试活 {key}", "外部正文")
    engine.fact("event", {**asdict(data), "ingress": "source", "pending_classification": False},
                data.item_id, source=data.source, ext_id=data.ext_id)


def test_drop_removes_misbuilt_item_without_rounds(tmp_path, capsys, monkeypatch):
    # 照现场：误建的 beta:132 所在交付对象暂停着，别的交付对象的活在跑。
    engine = build(tmp_path)
    engine.pause("beta")
    legacy_update(engine, "132", "beta")
    reach(engine, "说清楚")
    assert engine.log.get("items", "beta:132")["stage"] == "接单"
    engine.log.close()
    monkeypatch.setenv("WORKSHOP_STATE_DIR", str(tmp_path))
    assert main(["drop", "beta:132"]) == 0
    assert "已清掉 beta:132" in capsys.readouterr().out
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "beta:132" not in out and "alpha:one" in out

    engine = build(tmp_path)
    try:
        engine.resume("beta", [], expected=[])
        engine.tick()
        assert all(r["item"] != "beta:132" for r in engine.log.all("rounds"))
        assert item(engine)["stage"] == "说清楚"  # 别的活不受影响。
        engine.ingest(event(key="132", target="beta", ext_id="after-drop", action="update"))
        engine.tick()
        assert engine.log.get("items", "beta:132") is None
        assert all(r["item"] != "beta:132" for r in engine.log.all("rounds"))
        engine.ingest(event(key="132", target="beta", ext_id="ready"))
        # 再放行重新建活，只带清掉之后的资料。
        assert [m["ext_id"] for m in engine.log.get("items", "beta:132")["materials"]] == ["after-drop", "ready"]
        before = engine.log.all("items")
        engine.log.rebuild()
        assert engine.log.all("items") == before
    finally:
        engine.log.close()


def test_drop_refuses_item_that_has_run(engine):
    reach(engine, "接单")
    with pytest.raises(ValueError, match="不能清掉"):
        engine.drop("alpha:one")
    with pytest.raises(ValueError, match="找不到活"):
        engine.drop("alpha:missing")
    assert item(engine)["stage"] == "接单"
