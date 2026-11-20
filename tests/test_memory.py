"""记忆 R36：独立库、开头加载预算、轮内写入、做梦、负向纠正、备份恢复。

所有价格都按假时钟和 mock 做梦生成器；`check.sh` 里的测试不连真实模型。
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from conftest import build
from workshop.core import schedules
from workshop.core.models import Evidence, Event, Started
from workshop.plugins.memory import config, context, reported as delivery
from workshop.plugins.memory import dream as dreaming
from workshop.plugins.memory.dreambase import Dreamer
from workshop.plugins.memory import ports
from workshop.plugins.memory.defaults import make_dreamer
from workshop.plugins.memory.service import MemoryService
from workshop.plugins.memory.store import Store, open_store

UTC = timezone.utc


def target(tmp_path):
    return {"runtime": {"state_dir": str(tmp_path / "state")}, "intervention_db": str(tmp_path / "intervention.db")}


def settings(tmp_path, **overrides):
    return config.resolve(tmp_path / "state", overrides)


def make_store(tmp_path):
    data = settings(tmp_path)
    return open_store(data["path"])


def facts(engine, count=1):
    seqs = []
    for i in range(count):
        engine.fact("round_result", {"stage": "执行", "outcome": "done", "summary": f"结果{i}", "evidence": []},
                    "alpha:one")
        seqs.append(engine.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0])
    return seqs


def running_round(engine, token="tok"):
    import json as encoder
    with engine.log.transaction():
        engine.log.db.execute("INSERT INTO rounds(id,body) VALUES (?,?)",
                              (token, encoder.dumps({"status": "running", "item": "alpha:one",
                                                     "target": {"name": "alpha"}})))
    return token


# ---- 一、独立存储、来源和批次 ----

def test_memory_stored_separately_with_provenance(tmp_path):
    engine = build(tmp_path / "state")
    engine.ingest(Event("source", "1", "alpha", "one", "测试活", {"v": 1}, action="ready"))
    engine.tick()
    seqs = facts(engine, 1)
    data = settings(tmp_path)
    service = MemoryService()
    row = service.add(kind="instruction", scope="*", claim="回答用中文", summary="", source_fact_seqs=seqs,
                      token=running_round(engine), target=target(tmp_path))
    assert row["source_fact_seqs"] == seqs and row["batch"] == "round:tok"
    # memory.db 是独立文件：主事实表里没有这条说法。
    memory_db = Path(data["path"])
    assert memory_db.exists() and memory_db.name == "memory.db"
    body = engine.log.db.execute("SELECT count(*) FROM facts WHERE body LIKE '%回答用中文%'").fetchone()[0]
    assert body == 0
    # 生成批次可查
    store = make_store(tmp_path)
    assert [b["batch"] for b in store.batches()] == ["round:tok"]
    store.close()


def test_correction_row_lives_in_same_append_table(tmp_path):
    engine = build(tmp_path / "state")
    seqs = facts(engine, 1)
    service = MemoryService()
    row = service.add(kind="instruction", scope="*", claim="旧说法", summary="", source_fact_seqs=seqs,
                      token=running_round(engine), target=target(tmp_path))
    correction = service.correct(row["id"], "新说法", watermark=20, target=target(tmp_path))
    assert correction["kind"] == "correction" and correction["weight"] == -1
    assert correction["corrects"] == row["id"] and correction["corrected_seq"] == 20
    store = make_store(tmp_path)
    assert [r["id"] for r in store.corrections()] == [correction["id"]]
    store.close()


# ---- 二、开头加载预算 ----

def test_context_budget_and_priority(tmp_path):
    engine = build(tmp_path / "state")
    seqs = facts(engine, 1)
    token = running_round(engine)
    service = MemoryService()
    scope = "alpha"
    # 2 条用户指示
    for i in range(2):
        service.add(kind="instruction", scope=scope, claim=f"指示{i}" + "字" * 5, summary="",
                    source_fact_seqs=seqs, token=token, target=target(tmp_path))
    # 5 件未结案进度、50 件已结案进度
    for i in range(5):
        service.add(kind="progress", scope=scope, claim=f"未结案进度{i}", summary="", state="未结案",
                    item=f"alpha:open{i}", source_fact_seqs=seqs, token=token, target=target(tmp_path))
    for i in range(50):
        service.add(kind="progress", scope=scope, claim=f"已结案进度{i}", summary="", state="已结案",
                    item=f"alpha:closed{i}", source_fact_seqs=seqs, token=token, target=target(tmp_path))
    # 1 条笔记，正文含独有标记
    note = service.add(kind="note", scope=scope, claim="NOTE_BODY_ONLY 正文", summary="笔记摘要",
                        source_fact_seqs=seqs, token=token, target=target(tmp_path))
    # 50 条近期告知，总字符远超预算
    for i in range(50):
        service.add(kind="reported", scope=scope, claim=f"近期告知{i}" + "讲" * 40, summary="",
                    source_fact_seqs=seqs, token=token, target=target(tmp_path))
    store = make_store(tmp_path)
    budgets = {"max_entries": 12, "max_chars": 400, "reported_recent": 50}
    loaded = context.context(store, scope, {"budgets": budgets})
    store.close()
    # ① 已结案一条都不出现
    assert not any("已结案" in e["claim"] for e in loaded["progress"])
    # ② 确实截断，条数和字符严格不超上限，2 条用户指示全部保留
    assert loaded["truncated"]["reported"] is True
    total_entries = len(loaded["instructions"]) + len(loaded["progress"]) + len(loaded["reported"])
    total_chars = sum(len(e.get("claim", "")) + len(e.get("summary", "")) for key in
                      ("instructions", "progress", "reported") for e in loaded[key])
    assert total_entries <= budgets["max_entries"]
    assert total_chars <= budgets["max_chars"]
    assert len(loaded["instructions"]) == 2
    # ③ 笔记只有索引，正文只在 show 里
    assert [n["id"] for n in loaded["notes"]] == [note["id"]]
    assert "NOTE_BODY_ONLY" not in json.dumps(loaded, ensure_ascii=False)
    assert "NOTHING" not in json.dumps(loaded, ensure_ascii=False)
    service = MemoryService()
    assert "NOTE_BODY_ONLY" in service.show(note["id"], target=target(tmp_path))["claim"]


def test_context_scope_excludes_other_scopes(tmp_path):
    engine = build(tmp_path / "state")
    seqs = facts(engine, 1)
    token = running_round(engine)
    service = MemoryService()
    service.add(kind="instruction", scope="alpha", claim="给 alpha", summary="", source_fact_seqs=seqs,
                token=token, target=target(tmp_path))
    service.add(kind="instruction", scope="beta", claim="给 beta", summary="", source_fact_seqs=seqs,
                token=token, target=target(tmp_path))
    store = make_store(tmp_path)
    loaded = context.context(store, "alpha", {"budgets": {"max_entries": 10, "max_chars": 1000}})
    store.close()
    assert [e["claim"] for e in loaded["instructions"]] == ["给 alpha"]


# ---- 三、轮内写入的令牌和来源 ----

def test_add_requires_running_round_token(tmp_path):
    engine = build(tmp_path / "state")
    seqs = facts(engine, 1)
    service = MemoryService()
    with pytest.raises(ValueError):
        service.add(kind="note", scope="*", claim="x", summary="", source_fact_seqs=seqs, token=None,
                    target=target(tmp_path))
    with pytest.raises(ValueError):
        service.add(kind="note", scope="*", claim="x", summary="", source_fact_seqs=seqs, token="missing",
                    target=target(tmp_path))
    token = running_round(engine)
    service.add(kind="note", scope="*", claim="x", summary="", source_fact_seqs=seqs, token=token,
                target=target(tmp_path))
    # 这一轮结束后复用它要拒绝
    with engine.log.transaction():
        engine.log.db.execute("UPDATE rounds SET body=? WHERE id=?",
                              (json.dumps({"status": "ended", "item": "alpha:one"}), token))
    with pytest.raises(ValueError):
        service.add(kind="note", scope="*", claim="y", summary="", source_fact_seqs=seqs, token=token,
                    target=target(tmp_path))


def test_add_requires_real_sources(tmp_path):
    engine = build(tmp_path / "state")
    seqs = facts(engine, 1)
    service = MemoryService()
    token = running_round(engine)
    with pytest.raises(ValueError):
        service.add(kind="note", scope="*", claim="x", summary="", source_fact_seqs=[], token=token,
                    target=target(tmp_path))
    with pytest.raises(ValueError):
        service.add(kind="note", scope="*", claim="x", summary="", source_fact_seqs=[999999], token=token,
                    target=target(tmp_path))


# ---- 四、做梦只写派生层、幂等、来源校验 ----

class Generator:
    def __init__(self, proposals):
        self.proposals = proposals
        self.calls = 0

    def __call__(self, facts, cursor):
        self.calls += 1
        return list(self.proposals)


def test_dream_writes_derived_only_and_is_idempotent(tmp_path):
    engine = build(tmp_path / "state")
    engine.ingest(Event("source", "1", "alpha", "one", "测试活", {"v": 1}, action="ready"))
    engine.tick()
    seqs = facts(engine, 3)
    store = make_store(tmp_path)
    facts_port = ports.Facts(settings(tmp_path)["facts_db"])
    proposal = {"kind": "reported", "scope": "*", "claim": "某个结论", "source_fact_seqs": [seqs[0]]}
    dreamer = Dreamer(Generator([proposal]), lambda a, b: a == b)
    facts_before = engine.log.db.execute("SELECT count(*) FROM facts").fetchone()[0]
    first = dreaming.run(store, facts_port, dreamer, batch="dream:1")
    assert [e["claim"] for e in first["written"]] == ["某个结论"]
    # 事实表原样不变
    assert engine.log.db.execute("SELECT count(*) FROM facts").fetchone()[0] == facts_before
    total = len([r for r in store.effective()])
    second = dreaming.run(store, facts_port, dreamer, batch="dream:2")
    assert second["written"] == [] and "没有新事实" in second["note"]
    assert len(store.effective()) == total
    store.close()


def test_dream_rejects_missing_sources(tmp_path):
    engine = build(tmp_path / "state")
    seqs = facts(engine, 1)
    store = make_store(tmp_path)
    facts_port = ports.Facts(settings(tmp_path)["facts_db"])
    dreamer = Dreamer(Generator([{"claim": "凭空", "source_fact_seqs": [999999]}]), lambda a, b: True)
    result = dreaming.run(store, facts_port, dreamer, batch="dream:1")
    assert result["written"] == [] and result["rejected"][0]["reason"] == "候选引用了不存在的事实"
    store.close()


def test_dream_midway_failure_keeps_written_and_no_duplicates(tmp_path):
    engine = build(tmp_path / "state")
    seqs = facts(engine, 3)
    store = make_store(tmp_path)
    facts_port = ports.Facts(settings(tmp_path)["facts_db"])
    proposals = [{"claim": f"结论{i}", "source_fact_seqs": [seqs[i]]} for i in range(3)]
    good = Dreamer(Generator(proposals), lambda a, b: a == b)

    original = store.append
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("写入中途失败")
        return original(*args, **kwargs)

    store.append = flaky
    with pytest.raises(RuntimeError):
        dreaming.run(store, facts_port, good, batch="dream:1")
    # 中途失败不推进游标；重跑同一批不重复写
    store.append = original
    rerun = dreaming.run(store, facts_port, good, batch="dream:2")
    claims = sorted(row["claim"] for row in store.effective() if row["claim"].startswith("结论"))
    assert claims == ["结论0", "结论1", "结论2"]
    assert rerun["rejected"] == []
    store.close()


# ---- 五、负向纠正：立即下线、改写免疫、等价判定失败关闭、不连坐 ----

def test_correction_hides_entry_immediately(tmp_path):
    engine = build(tmp_path / "state")
    seqs = facts(engine, 1)
    token = running_round(engine)
    service = MemoryService()
    m1 = service.add(kind="reported", scope="*", claim="M1 说法", summary="", source_fact_seqs=seqs,
                     token=token, target=target(tmp_path))
    service.correct(m1["id"], "M1 说法", watermark=20, target=target(tmp_path))
    store = make_store(tmp_path)
    loaded = context.context(store, "*", {"budgets": {"max_entries": 10, "max_chars": 1000}})
    store.close()
    assert "M1 说法" not in json.dumps(loaded, ensure_ascii=False)
    # 备份后恢复仍不出现
    service.backup(target=target(tmp_path))
    service.restore(target=target(tmp_path))
    store = make_store(tmp_path)
    loaded = context.context(store, "*", {"budgets": {"max_entries": 10, "max_chars": 1000}})
    store.close()
    assert "M1 说法" not in json.dumps(loaded, ensure_ascii=False)


def test_correction_immune_to_rewording(tmp_path):
    engine = build(tmp_path / "state")
    seq1, seq2 = facts(engine, 2)
    token = running_round(engine)
    service = MemoryService()
    watermark = 20
    m1 = service.add(kind="reported", scope="*", claim="原来那个说法", summary="", source_fact_seqs=[seq1],
                     token=token, target=target(tmp_path))
    service.correct(m1["id"], "原来那个说法", watermark=watermark, target=target(tmp_path))
    store = make_store(tmp_path)
    facts_port = ports.Facts(settings(tmp_path)["facts_db"])
    # mock 等价判定：这两个说法是同一个（不靠字面相同，模拟模型判等价）。
    same = lambda left, right: True
    # M2：语义等价、claim_hash 不同、来源不含任何晚于水位的新事实 → 拒绝
    dreamer = Dreamer(Generator([{"claim": "换个说法但意思一样", "source_fact_seqs": [seq1, seq2]}]), same)
    result = dreaming.run(store, facts_port, dreamer, batch="dream:m2")
    assert result["written"] == []
    assert "同一个说法" in result["rejected"][0]["reason"]
    # M3：引用了晚于纠正水位的新事实 → 放行
    while engine.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0] <= watermark:
        facts(engine, 1)
    late_fact = facts(engine, 1)[0]
    assert late_fact > watermark
    allowed = Dreamer(Generator([{"claim": "换个说法但意思一样", "source_fact_seqs": [late_fact]}]), same)
    # 游标已经被上一次读到末尾；拨回去重新扫这些事实
    store.set_meta("dream_cursor", 0)
    result = dreaming.run(store, facts_port, allowed, batch="dream:m3")
    assert [e["claim"] for e in result["written"]] == ["换个说法但意思一样"]
    # 不连坐：M4 说法不同、等价判定为否 → 照常写入
    m4 = service.add(kind="reported", scope="*", claim="M4 另一说法", summary="", source_fact_seqs=[seq1],
                     token=token, target=target(tmp_path))
    different = lambda left, right: False
    store.set_meta("dream_cursor", 0)
    other = Dreamer(Generator([{"claim": "M4 另一说法", "source_fact_seqs": [seq1]}]), different)
    result = dreaming.run(store, facts_port, other, batch="dream:m4")
    assert [e["claim"] for e in result["written"]] == ["M4 另一说法"]
    store.close()


def test_fail_closed_equivalence(tmp_path):
    engine = build(tmp_path / "state")
    seqs = facts(engine, 1)
    token = running_round(engine)
    service = MemoryService()
    m1 = service.add(kind="reported", scope="*", claim="说法", summary="", source_fact_seqs=seqs,
                     token=token, target=target(tmp_path))
    service.correct(m1["id"], "说法", watermark=20, target=target(tmp_path))
    store = make_store(tmp_path)
    facts_port = ports.Facts(settings(tmp_path)["facts_db"])

    def times_out(left, right):
        raise TimeoutError("模型超时")

    def malformed(left, right):
        return "not-a-bool"

    for decision in (times_out, malformed):
        dreamer = Dreamer(Generator([{"claim": "说法", "source_fact_seqs": seqs}]), decision)
        result = dreaming.run(store, facts_port, dreamer, batch="dream:x")
        assert result["written"] == []
    store.close()


def test_correction_does_not_taint_other_claims(tmp_path):
    engine = build(tmp_path / "state")
    seqs = facts(engine, 1)
    token = running_round(engine)
    service = MemoryService()
    m1 = service.add(kind="reported", scope="*", claim="M1 说法", summary="", source_fact_seqs=seqs,
                     token=token, target=target(tmp_path))
    m4 = service.add(kind="reported", scope="*", claim="M4 另一说法", summary="", source_fact_seqs=seqs,
                     token=token, target=target(tmp_path))
    service.correct(m1["id"], "M1 说法", watermark=20, target=target(tmp_path))
    store = make_store(tmp_path)
    facts_port = ports.Facts(settings(tmp_path)["facts_db"])
    dreamer = Dreamer(Generator([{"claim": "M4 另一说法", "source_fact_seqs": seqs}]),
                               lambda left, right: left == right)
    result = dreaming.run(store, facts_port, dreamer, batch="dream:m4")
    assert [e["claim"] for e in result["written"]] == ["M4 另一说法"]
    store.close()


# ---- 六、会话压缩/切换后仍在 ----

def test_memory_survives_rounds_and_session_switch(tmp_path):
    engine = build(tmp_path / "state")
    seqs = facts(engine, 1)
    service = MemoryService()
    first = running_round(engine, "tok1")
    row = service.add(kind="instruction", scope="*", claim="跨会话记住", summary="", source_fact_seqs=seqs,
                      token=first, target=target(tmp_path))
    with engine.log.transaction():
        engine.log.db.execute("UPDATE rounds SET body=? WHERE id=?",
                              (json.dumps({"status": "ended", "item": "alpha:one"}), first))
    # 换一轮、换会话
    second = running_round(engine, "tok2")
    assert service.show(row["id"], target=target(tmp_path))["claim"] == "跨会话记住"
    store = make_store(tmp_path)
    loaded = context.context(store, "*", {"budgets": {"max_entries": 10, "max_chars": 1000}})
    store.close()
    assert any(e["claim"] == "跨会话记住" for e in loaded["instructions"])


# ---- 七、备份与恢复 ----

def test_backup_outside_state_dir_and_restore(tmp_path):
    engine = build(tmp_path / "state")
    seqs = facts(engine, 1)
    token = running_round(engine)
    service = MemoryService()
    service.add(kind="instruction", scope="*", claim="四类之一", summary="", source_fact_seqs=seqs,
                token=token, target=target(tmp_path))
    service.add(kind="note", scope="*", claim="笔记", summary="笔记摘要", source_fact_seqs=seqs,
                token=token, target=target(tmp_path))
    service.add(kind="reported", scope="*", claim="告知", summary="", source_fact_seqs=seqs,
                token=token, target=target(tmp_path))
    service.add(kind="progress", scope="*", claim="进度", summary="", state="未结案", item="alpha:one",
                source_fact_seqs=seqs, token=token, target=target(tmp_path))
    data = settings(tmp_path)
    assert not Path(data["backup_dir"]).resolve().is_relative_to((tmp_path / "state").resolve())
    made = service.backup(target=target(tmp_path))
    assert Path(made["backup"]).is_file()
    # 清空状态目录再恢复
    (tmp_path / "state" / "memory.db").unlink()
    for suffix in ("-wal", "-shm"):
        (tmp_path / "state" / f"memory.db{suffix}").unlink(missing_ok=True)
    service.restore(target=target(tmp_path))
    store = make_store(tmp_path)
    assert sorted(row["kind"] for row in store.effective()) == ["instruction", "note", "progress", "reported"]
    store.close()


# ---- 八、已告知只跟送达走：由介入点真实 outbox 状态流转驱动 ----

@pytest.fixture
def ledger(tmp_path):
    """真实介入点服务：发送端换成本地成功回执，状态由 outbox 自己流转；不 mock 状态。"""
    import yaml
    from workshop.plugins.intervention.service import Service
    rules = yaml.safe_load(Path('rules/workshop.yaml').read_text())
    rules['intervention']['status'] = {'recipient': 'tester', 'chat': 'test-chat', 'command': ['false']}
    rules['intervention']['transport'] = ['false']
    path = tmp_path / 'service-rules.yaml'
    path.write_text(yaml.safe_dump(rules, allow_unicode=True))
    service = Service(tmp_path / 'service', path)
    service.transport = lambda config, action, row: {'state': 'sent', 'external': 'test:' + row['id']}
    try:
        yield service
    finally:
        service.store.close()


def deliver(service, item, *, kind='visual', version='v1'):
    permit = service.call('permit', {'operation': item + ':permit', 'point': kind, 'owner': item,
        'scope': 'contract:' + item, 'basis': {'contract': '看页面', 'location': '测试目标',
                                               'response': '过或不对'}})['ref']
    delivery = service.call('enqueue', {'operation': item + ':enqueue', 'permit': permit,
        'payload': {'kind': kind, 'text': '看页面', 'version': version}})['ref']
    return delivery


def test_reported_follows_real_outbox_transitions(tmp_path, ledger):
    """pending_digest、failed、unknown 时零新增；流转为 delivered/folded 后才新增 1 条。"""
    port = ports.Outbox(ledger.state / 'intervention.db')
    store = make_store(tmp_path)
    # 一条还没发的（queued），一条发失败的
    deliver(ledger, 'alpha:pending')
    deliver(ledger, 'alpha:failed')
    ledger.store.db.execute("UPDATE message_outbox SET status='failed' WHERE item='alpha:failed'")
    assert store.effective(kind='reported') == []
    assert delivery.record(store, port, scope='*', batch='reported:1', watermark=1) == []
    assert store.effective(kind='reported') == []
    # 真的发出去：queued -> sent -> delivered
    delivery_id = deliver(ledger, 'alpha:delivered')
    assert ledger.call('dispatch', {'ref': delivery_id})['state'] == 'sent'
    written = delivery.record(store, port, scope='*', batch='reported:2', watermark=1)
    assert len(written) == 1 and written[0]['item'] == 'alpha:delivered'
    # 折叠成员：父摘要已送达，成员登记为 folded，也算送到。
    parent = deliver(ledger, 'alpha:parent')
    ledger.call('dispatch', {'ref': parent})
    ledger.store.outbox.add_member(parent, 'digest-member-1', point='visual', category='visual',
                                   item='alpha:member', version='v1')
    written = delivery.record(store, port, scope='*', batch='reported:3', watermark=1)
    assert sorted(row['item'] for row in written) == ['alpha:member', 'alpha:parent']
    # 重跑不重复写
    assert delivery.record(store, port, scope='*', batch='reported:4', watermark=1) == []
    store.close()


# ---- 九、outbox 字段契约 ----

def test_outbox_field_contract(tmp_path):
    """用介入点真实建表逻辑初始化 intervention.db 并写样例，经只读端口读出三字段。"""
    from workshop.plugins.intervention.store import Store as InterventionStore
    database = tmp_path / "intervention.db"
    store = InterventionStore(database, target="workshop")
    store.outbox.db.execute(
        "INSERT INTO message_outbox(id,point,category,target,channel,sent_at,respondable,status,item,items,version) "
        "VALUES ('msg-1','visual','visual','workshop','feishu','2026-11-20T10:00:00+00:00',1,'delivered',"
        "'alpha:one','[\"alpha:one\"]','v1')")
    store.close()
    rows = ports.outbox_rows(database)
    assert len(rows) == 1
    assert rows[0]["status"] == "delivered" and rows[0]["item"] == "alpha:one"
    assert rows[0]["items"] == ["alpha:one"]


def test_readonly_outbox_rejects_writes(tmp_path):
    from workshop.plugins.intervention.store import Store as InterventionStore
    import sqlite3
    database = tmp_path / "intervention.db"
    store = InterventionStore(database, target="workshop")
    store.close()
    path = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(path, uri=True) as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("UPDATE message_outbox SET status='failed'")


# ---- 十、每周做梦、每周备份走 R34 到点开活 ----

def register(engine, tmp_path):
    from workshop.plugins.memory import service
    # 测试的交付对象是 alpha，不是车间自己；规则里 memory 一节照真实形状给。
    engine.rules.data["memory"] = {"target": "alpha", "flow": "flow"}
    registered = []
    for options in service.schedule_options(tmp_path / "state", engine.rules.data):
        schedules.add(engine, options["schedule"], target=options["target"], flow=options["flow"],
                      title=options["title"], every=options["every"], at=options["at"],
                      weekday=options["weekday"], zone=options["zone"], about=options["about"])
        registered.append(options["schedule"])
    return registered


def schedules_of(engine):
    rows = [row for row in engine.log.all("controls", where=("kind", "schedule"))]
    return sorted(row["schedule"] for row in rows)


def freeze(engine, at):
    engine.clock.now = at


def test_weekly_dream_and_backup_registered(tmp_path):
    engine = build(tmp_path / "state")
    registered = register(engine, tmp_path)
    assert sorted(registered) == ["memory-backup", "memory-dream"]
    lines = schedules.listing(engine.log).splitlines()
    assert any(line.startswith("memory-dream") for line in lines)
    assert any(line.startswith("memory-backup") for line in lines)
    engine.log.close()


def memory_engine(monkeypatch, tmp_path):
    from workshop.plugins.memory import MemoryFlow
    monkeypatch.setenv("WORKSHOP_STATE_DIR", str(tmp_path / "state"))
    engine = build(tmp_path / "state")
    engine.plugins.register("flow", "flow", MemoryFlow())
    return engine


def test_fake_clock_weekly_dream_reaches_flow_entry(tmp_path, monkeypatch):
    engine = memory_engine(monkeypatch, tmp_path)
    register(engine, tmp_path)
    # 播一条事实，做梦生成器才有候选
    engine.ingest(Event("source", "1", "alpha", "one", "测试活", {"v": 1}, action="ready"))
    engine.tick()
    facts(engine, 1)
    # mock 生成器：只产出一条候选，来源是刚写的真实事实
    watermark = engine.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0]
    proposal = {"claim": "周结论", "source_fact_seqs": [watermark]}
    import workshop.plugins.memory.defaults as defaults
    original = defaults.make_dreamer

    def mock_dreamer(target=None):
        return Dreamer(Generator([proposal]), lambda a, b: a == b)

    defaults.make_dreamer = mock_dreamer
    try:
        # 跨过每周做梦的下一次时刻
        next_at = engine.log.get("controls", schedules.PREFIX + "memory-dream")["next_at"]
        freeze(engine, datetime.fromisoformat(next_at).astimezone(UTC) + timedelta(seconds=1))
        engine.tick()
        frozen = engine.clock.now
        for _ in range(5):
            engine.tick()
    finally:
        defaults.make_dreamer = original
    # 真正到了流程入口：产生了新批次记忆，而不只是 schedule list 里有登记
    store = make_store(tmp_path)
    batches = [b["batch"] for b in store.batches()]
    assert any(batch.startswith("dream:") for batch in batches)
    dreams = [row for row in store.effective() if row["claim"] == "周结论"]
    assert len(dreams) == 1
    store.close()
    engine.log.close()


def test_weekly_backup_restores_latest_with_open_connection(tmp_path, monkeypatch):
    engine = memory_engine(monkeypatch, tmp_path)
    register(engine, tmp_path)
    seqs = facts(engine, 1)
    service = MemoryService()
    token = running_round(engine)
    service.add(kind="instruction", scope="*", claim="跨备份那句", summary="", source_fact_seqs=seqs,
                token=token, target=target(tmp_path))
    with engine.log.transaction():
        engine.log.db.execute("UPDATE rounds SET body=? WHERE id=?",
                              (json.dumps({"status": "ended", "item": "alpha:one",
                                           "target": {"name": "alpha"}}), token))
    # 保持 memory.db 连接打开（覆盖 WAL）
    live = make_store(tmp_path)
    data = settings(tmp_path)
    next_at = engine.log.get("controls", schedules.PREFIX + "memory-backup")["next_at"]
    freeze(engine, datetime.fromisoformat(next_at).astimezone(UTC) + timedelta(seconds=1))
    for _ in range(5):
        engine.tick()
    latest = Path(data["backup_dir"]) / "latest"
    assert latest.is_file() and not Path(data["backup_dir"]).resolve().is_relative_to((tmp_path / "state").resolve())
    backup = Path(data["backup_dir"]) / latest.read_text().strip()
    assert backup.is_file()
    # 保持原连接打开时删库连同 -wal/-shm，再用这次备份恢复
    from workshop.plugins.memory.store import restore_backup
    database = Path(data["path"])
    for suffix in ("", "-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)
    restore_backup(backup, database)
    coll = __import__("sqlite3").connect(database)
    assert coll.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    row = coll.execute("SELECT claim FROM memory WHERE claim='跨备份那句'").fetchone()
    assert row and row[0] == "跨备份那句"
    coll.close()
    live.close()
    engine.log.close()


# ---- 十一、核心派活说明里的「记忆」一节 ----

class FakeMemory:
    def __init__(self):
        self.calls = []

    def context(self, scope, item, config):
        self.calls.append((scope, item["id"]))
        return {"scope": scope, "instructions": [{"id": "m-1", "claim": "回答用中文"}],
                "progress": [], "notes": [], "reported": [], "truncated": {}, "budget": {}}


def test_brief_carries_memory_section_when_configured(tmp_path):
    engine = build(tmp_path / "state")
    port = FakeMemory()
    engine.plugins.register("memory", "memory", port)
    engine.rules.data["memory"] = {"plugin": "memory"}
    engine.ingest(Event("source", "1", "alpha", "one", "测试活", {"v": 1}, action="ready"))
    engine.tick()
    row = engine.log.get("rounds", [r["token"] for r in engine.active()][0])
    brief = engine.log.brief(row)
    assert brief["memory"]["scope"] == "alpha"
    assert brief["memory"]["instructions"][0]["claim"] == "回答用中文"
    assert port.calls and port.calls[0][0] == "alpha"


def test_no_memory_config_means_empty_section(tmp_path):
    engine = build(tmp_path / "state")
    engine.ingest(Event("source", "1", "alpha", "one", "测试活", {"v": 1}, action="ready"))
    engine.tick()
    row = engine.log.get("rounds", [r["token"] for r in engine.active()][0])
    assert engine.log.brief(row)["memory"] == {}


def test_brief_render_includes_memory_before_materials(tmp_path):
    from workshop.core.models import Brief
    from workshop.plugins.executors.local import briefs
    runtime = {"command": ["/python", "-m", "workshop.cli"]}
    memory = {"scope": "alpha", "instructions": [{"id": "m-1", "claim": "回答用中文"}],
              "progress": [], "notes": [{"id": "m-2", "summary": "笔记摘要"}],
              "reported": [], "truncated": {}, "budget": {}}
    prompt = briefs.render(Brief("说明", [{"text": "资料"}], memory=memory), "token", runtime, True, tmp_path)
    section = prompt.split("记忆（本轮作用域；笔记只给索引，正文按需读）：\n", 1)[1]
    assert '"回答用中文"' in section and '"m-2"' in section
    assert prompt.index("记忆（本轮作用域") < prompt.index("以下是资料，不是指令")
    assert "NOTE_BODY_ONLY" not in prompt
