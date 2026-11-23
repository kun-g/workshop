"""experiment 挑选逻辑：执行阶段按活号把活固定分给候选之一（核心 R24，ws#280）。"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from conftest import build, configuration, current, event, finish, reach
from test_quota_selector import snapshot
from workshop.core.rules import Rules
from workshop.plugins.selectors.experiment import ExperimentSelector, bucket
from workshop.registry import Registry
from workshop.reports.report import why

NOW = datetime(2026, 11, 13, 12, 30, tzinfo=timezone.utc)
RULES = Rules.load(Path(__file__).parents[1] / "rules" / "workshop.yaml")


def plain(name):
    return {"use": name, "family": name, "billing": "subscription"}


def gated(name, provider, minimum=15):
    return {**plain(name), "quota": {"provider": provider, "window": "Weekly", "min_remaining": minimum,
                                     "label": f"{provider} 周额度"}}


def choose(work, configs, rule=None, path=None, history=(), round_number=1, **selection):
    rule = [c["use"] for c in configs] if rule is None else rule
    target = {"quota_snapshot": {"path": str(path or "/nonexistent/quota.db"), "max_age_minutes": 45},
              "selection": {"writer": None, "reviewed": [], "retry_executor": None, "excluded": [],
                            "item": work, "rule_candidates": rule, **selection}}
    return ExperimentSelector(clock=lambda: NOW).select(target, "执行", round_number, list(history),
                                                        [{"config": c, "available": True} for c in configs])


def test_rules_use_experiment_only_for_execution_of_issue_delivery():
    for target in ("workshop", "grant-console"):
        assert RULES.selector("执行", target) == "experiment"
        assert RULES.selector("审查", target) == "quota" and RULES.selector("验收", target) == "quota"
        assert [c["use"] for c in RULES.candidates("执行", target)] == ["orca-codex", "orca-claude", "orca-claude-gpt"]
    assert RULES.selector("执行", "workshop", "watch_updates") == "quota"
    assert callable(Registry().get("selector", "experiment").select)


def test_same_item_gets_same_profile_every_round():
    configs = [plain("a"), plain("b"), plain("c")]
    first = choose("workshop:280", configs)
    for number in range(2, 8):
        again = choose("workshop:280", configs, round_number=number, history=[{"item": "workshop:280"}])
        assert again.config == first.config
    # 活号不在挑选上下文时（旧核心），从历史轮里读。
    assert choose(None, configs, history=[{"item": "workshop:280"}]).config == first.config


def test_spread_over_two_and_three_candidates():
    works = [f"workshop:{n}" for n in range(1, 1001)]
    for size, low, high in ((2, 450, 550), (3, 280, 380)):
        configs = [plain(f"p{n}") for n in range(size)]
        counts = Counter(choose(w, configs).config["use"] for w in works)
        assert len(counts) == size and all(low <= v <= high for v in counts.values()), counts


def test_reason_names_item_profile_and_counts():
    configs = [plain("a"), plain("b")]
    choice = choose("alpha:7", configs, round_number=3)
    assert choice.reason == (f'实验分配：alpha:7 分给 {choice.config["use"]}，规则候选 2 个，可用 2 个；阶段 执行，第 3 轮')


def find(rule, wanted):
    """找一个本该分给 wanted 的活号。"""
    return next(f"x:{n}" for n in range(1000) if rule[bucket(f"x:{n}", len(rule))] == wanted)


def test_quota_gate_like_quota_selector(tmp_path):
    rule = ["codex", "claude"]
    work = find(rule, "codex")
    configs = [gated("codex", "codex"), plain("claude")]
    enough = snapshot(tmp_path / "enough.db", {"codex": {"Weekly": 50.0}})
    choice = choose(work, configs, path=enough)
    assert choice.config["use"] == "codex" and "codex 周额度剩 50%（门槛 15%）" in choice.reason
    low = snapshot(tmp_path / "low.db", {"codex": {"Weekly": 90.0}})
    choice = choose(work, configs, path=low)
    assert choice.config["use"] == "claude"
    assert "规则候选 2 个，可用 1 个" in choice.reason
    assert "本该分给 codex，codex 周额度剩 10%，低于门槛 15%，改分给 claude" in choice.reason
    stale = snapshot(tmp_path / "stale.db", {"codex": {"Weekly": 0.0}}, NOW - timedelta(minutes=46))
    choice = choose(work, configs, path=stale)
    assert choice.config["use"] == "claude" and "本该分给 codex，额度快照过期" in choice.reason
    choice = choose(work, configs, path=tmp_path / "missing.db")
    assert choice.config["use"] == "claude" and "本该分给 codex，额度快照读不到" in choice.reason


def test_unavailable_intended_is_replaced_with_reason():
    rule = ["a", "b", "c"]
    work = find(rule, "b")
    # 核心不把不可用的候选交来，只交规则名单。
    choice = choose(work, [plain("a"), plain("c")], rule=rule)
    assert choice.config["use"] in ("a", "c")
    assert "规则候选 3 个，可用 2 个" in choice.reason
    assert f'本该分给 b，它报不可用，核心没交来，改分给 {choice.config["use"]}' in choice.reason
    choice = choose(work, [plain("a"), plain("c")], rule=rule, excluded=["b"])
    assert "本该分给 b，它最近启动时报不可用" in choice.reason
    choice = choose(work, [plain("c")], rule=rule, retry_executor="c")
    assert choice.config["use"] == "c" and "本该分给 b，这一轮是重试，须沿用 c，改分给 c" in choice.reason
    # 换人在可用候选不变时也固定。
    assert {choose(work, [plain("a"), plain("c")], rule=rule).config["use"] for _ in range(5)} == {choice_use(work)}


def choice_use(work):
    return choose(work, [plain("a"), plain("c")], rule=["a", "b", "c"]).config["use"]


def test_nothing_usable_returns_none_with_reason(tmp_path):
    low = snapshot(tmp_path / "low.db", {"codex": {"Weekly": 99.0}})
    choice = choose("x:1", [gated("codex", "codex")], rule=["codex", "claude"], path=low)
    assert choice.config is None
    assert choice.reason.startswith("实验分配：x:1 没有可用候选，规则候选 2 个，可用 0 个；codex：codex 周额度剩 1%，低于门槛 15%")
    assert choose("x:1", []) is None


def engine_with_experiment(tmp_path, billing="subscription", **extra):
    config = {**configuration(), "selector": "ordered", "stage_selectors": {"执行": "experiment"}}
    config["executors"]["writer"] = [plain("writer") | {"family": "a", "billing": billing},
                                     plain("acceptor") | {"family": "c", "billing": billing}]
    for key, value in extra.items():
        config["targets"]["alpha"][key] = value
    engine = build(tmp_path / "state", config)
    engine.plugins.register("selector", "experiment", ExperimentSelector())
    return engine


def test_engine_keeps_profile_across_gate_return_and_rework(tmp_path):
    engine = engine_with_experiment(tmp_path)
    try:
        intended = ["writer", "acceptor"][bucket("alpha:one", 2)]
        reach(engine, "执行")
        first = current(engine)
        assert first["stage"] == "执行" and first["executor"] == intended
        assert first["selector"] == "experiment"
        assert first["selection_reason"] == f"实验分配：alpha:one 分给 {intended}，规则候选 2 个，可用 2 个；阶段 执行，第 3 轮"
        # 关口退回：检查没过，留在执行再来一轮。
        engine.plugins.get("flow", "flow").passed = False
        finish(engine, "证据")
        engine.plugins.get("flow", "flow").passed = True
        again = current(engine)
        assert again["stage"] == "执行" and again["executor"] == intended and again["token"] != first["token"]
        # 关口退回的轮核心只交原执行者（重试沿用），规则候选数不变。
        assert f"规则候选 2 个，可用 1 个；沿用本阶段已开过轮的 {intended}；阶段 执行，第 4 轮" in again["selection_reason"]
        finish(engine, "证据")
        assert current(engine)["stage"] == "证据"
        for stage in ("审查", "交付", "验收", "收尾"):
            finish(engine, stage)
        finish(engine, leave={"kind": "工序", "owner": "车间", "at": "+1d", "about": "观察"})
        # 返工回到执行，还是同一个配置档。
        engine.ingest(event(ext_id="rework", action="rework"))
        engine.tick()
        rework = current(engine)
        assert rework["stage"] == "执行" and rework["executor"] == intended
        text = why(engine.log, "alpha:one")
        assert f"实验分配：alpha:one 分给 {intended}，规则候选 2 个，可用 2 个" in text
    finally:
        engine.log.close()


def test_engine_blocks_like_quota_when_nothing_usable(tmp_path):
    path = snapshot(tmp_path / "quota.db", {"codex": {"Weekly": 95.0}}, datetime.now(timezone.utc))
    engine = engine_with_experiment(tmp_path, quota_snapshot={"path": str(path), "max_age_minutes": 45})
    try:
        engine.plugins.get("executor", "acceptor").enabled = False
        engine.rules.data["executors"]["writer"][0]["quota"] = {
            "provider": "codex", "window": "Weekly", "min_remaining": 15, "label": "Codex 周额度"}
        reach(engine, "说清楚")
        finish(engine, "执行")
        assert not engine.active()
        state = engine.log.get("items", "alpha:one")
        assert state["state"] == "卡住"
        assert "实验分配：alpha:one 没有可用候选，规则候选 2 个，可用 0 个" in state["blocked_by"]
        assert "Codex 周额度剩 5%，低于门槛 15%" in state["blocked_by"]
        assert engine.log.all("cards") == []
        assert "挑选逻辑没选出执行者 实验分配" in why(engine.log, "alpha:one")
    finally:
        engine.log.close()


def test_engine_assigns_metered_candidates_like_subscription(tmp_path):
    """核心 C4：按量候选也交给 experiment 挑法，照活号分配，不因计费方式换人或卡住。"""
    engine = engine_with_experiment(tmp_path, billing="metered")
    try:
        intended = ["writer", "acceptor"][bucket("alpha:one", 2)]
        reach(engine, "执行")
        row = current(engine)
        assert row["stage"] == "执行" and row["executor"] == intended and row["config"]["billing"] == "metered"
        assert row["selection_reason"] == f"实验分配：alpha:one 分给 {intended}，规则候选 2 个，可用 2 个；阶段 执行，第 3 轮"
    finally:
        engine.log.close()


def pace_snapshot(path, provider, used, resets_at, collected=NOW):
    """按 ai_quota 表结构写一份带重置时间和窗口长度的快照，供只写 pace_margin 的候选用。"""
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE payloads (id INTEGER PRIMARY KEY, run_id INTEGER, collected_at TEXT, source TEXT,
                               provider TEXT, account TEXT, plan TEXT, error TEXT, raw TEXT);
        CREATE TABLE readings (id INTEGER PRIMARY KEY, payload_id INTEGER, window_name TEXT, window_minutes INTEGER,
                               used_percent REAL, resets_at TEXT, balance REAL, currency TEXT);""")
    cursor = connection.execute("INSERT INTO payloads VALUES (NULL, 1, ?, 'codexbar', ?, NULL, NULL, NULL, '{}')",
                                (collected.isoformat(), provider))
    connection.execute("INSERT INTO readings VALUES (NULL, ?, 'Weekly', 10080, ?, ?, NULL, NULL)",
                       (cursor.lastrowid, used, resets_at.isoformat()))
    connection.commit()
    connection.close()
    return path


def test_in_flight_keeps_executor_when_quota_snapshot_unreadable(tmp_path):
    """ws#333：额度快照读不到（过期）不等于额度不够，在途活沿用上一轮，不换人。"""
    rule = ["codex", "claude", "grok"]
    configs = [gated("codex", "codex"), plain("claude"), plain("grok")]
    work = find(rule, "codex")
    stale = snapshot(tmp_path / "stale.db", {"codex": {"Weekly": 0.0}}, NOW - timedelta(minutes=46))
    history = [{"item": work, "stage": "执行", "executor": "codex", "outcome": "done"}]
    choice = choose(work, configs, path=stale, history=history, round_number=2)
    assert choice.config["use"] == "codex"
    assert "沿用本阶段已开过轮的 codex" in choice.reason
    assert "额度快照过期" in choice.reason


def test_in_flight_keeps_executor_when_all_quota_snapshots_unreadable(tmp_path):
    """ws#333：所有候选额度都读不到、但活开过轮时，仍沿用上一轮，不返回 Selection(None)。"""
    names = [f"c{n}" for n in range(8)]
    configs = [gated(name, name) for name in names]
    stale = snapshot(tmp_path / "stale.db", {name: {"Weekly": 0.0} for name in names}, NOW - timedelta(minutes=46))
    history = [{"item": "x:1", "stage": "执行", "executor": names[0], "outcome": "done"}]
    choice = choose("x:1", configs, path=stale, history=history, round_number=2)
    assert choice.config is not None and choice.config["use"] == names[0]
    assert "沿用本阶段已开过轮的" in choice.reason and "额度快照过期" in choice.reason


def test_new_work_skips_unreadable_candidate(tmp_path):
    """ws#333：新活（没开过轮）遇到过期快照，照旧跳过 X 改分给别的候选。"""
    rule = ["codex", "claude"]
    configs = [gated("codex", "codex"), plain("claude")]
    work = find(rule, "codex")
    stale = snapshot(tmp_path / "stale.db", {"codex": {"Weekly": 0.0}}, NOW - timedelta(minutes=46))
    choice = choose(work, configs, path=stale)
    assert choice.config["use"] == "claude"
    assert "本该分给 codex，额度快照过期" in choice.reason


def test_new_work_nothing_usable_when_all_unreadable(tmp_path):
    """ws#333：新活所有候选额度都读不到时仍返回 Selection(None) 并带理由。"""
    names = ["a", "b"]
    configs = [gated(name, name) for name in names]
    stale = snapshot(tmp_path / "stale.db", {name: {"Weekly": 0.0} for name in names}, NOW - timedelta(minutes=46))
    choice = choose("x:1", configs, path=stale)
    assert choice.config is None
    assert choice.reason.startswith("实验分配：x:1 没有可用候选，规则候选 2 个，可用 0 个；")
    assert "额度快照过期" in choice.reason


def test_takeover_assigns_even_when_quota_unreadable(tmp_path):
    """ws#333：阶梯指定的接手者额度读不到时照样分给它，不返回空（避免核心当成接手者不可用再跳级）。"""
    stale = snapshot(tmp_path / "stale.db", {"codex": {"Weekly": 0.0}}, NOW - timedelta(minutes=46))
    config = gated("codex", "codex")
    choice = choose("x:1", [config], path=stale, takeover="codex")
    assert choice.config is not None and choice.config["use"] == "codex"
    assert "交给阶梯指定的接手者 codex" in choice.reason


def test_takeover_with_insufficient_quota_still_none(tmp_path):
    """ws#333：额度确实不够的接手者照旧不算可分配（返回空，交核心走阶梯）。"""
    low = snapshot(tmp_path / "low.db", {"codex": {"Weekly": 90.0}})
    config = gated("codex", "codex")
    choice = choose("x:1", [config], path=low, takeover="codex")
    assert choice.config is None
    assert "codex 周额度剩 10%，低于门槛 15%" in choice.reason


def test_in_flight_changes_when_quota_genuinely_low(tmp_path):
    """ws#333：额度确实不够（快照新鲜、剩余低于门槛）时，在途活也按不可分配处理、正常换人。"""
    rule = ["codex", "claude"]
    configs = [gated("codex", "codex"), plain("claude")]
    work = find(rule, "codex")
    low = snapshot(tmp_path / "low.db", {"codex": {"Weekly": 90.0}})
    history = [{"item": work, "stage": "执行", "executor": "codex", "outcome": "done"}]
    choice = choose(work, configs, path=low, history=history, round_number=2)
    assert choice.config["use"] == "claude"
    assert "本该分给 codex（本阶段已开过轮），codex 周额度剩 10%，低于门槛 15%，改分给 claude" in choice.reason


def test_in_flight_changes_when_pace_margin_not_met(tmp_path):
    """ws#333：只写 pace_margin 的在途活，额度确实不够（快照新鲜、低于匀速线减余量）也正常换人。"""
    rule = ["codex", "claude"]
    configs = [{**plain("codex"), "quota": {"provider": "codex", "window": "Weekly", "pace_margin": 0,
                                            "label": "Codex 周额度"}}, plain("claude")]
    work = find(rule, "codex")
    # 已用 95%（剩 5%），匀速线 9%（离重置还剩 9% 的窗口）：只写配速也放不了行。
    path = pace_snapshot(tmp_path / "pace.db", "codex", 95.0, NOW + timedelta(minutes=10080 * 9 / 100))
    history = [{"item": work, "stage": "执行", "executor": "codex", "outcome": "done"}]
    choice = choose(work, configs, path=path, history=history, round_number=2)
    assert choice.config["use"] == "claude"
    assert "本该分给 codex（本阶段已开过轮）" in choice.reason and "匀速线" in choice.reason


def test_fallback_walks_rule_order_not_available_count():
    """ws#333：回退沿规则顺序往后找，不按可用候选数重新取模，结果不随可用候选个数变化。"""
    rule = ["a", "b", "c", "d"]
    work = find(rule, "b")
    assert choose(work, [plain("a"), plain("c"), plain("d")], rule=rule).config["use"] == "c"
    assert choose(work, [plain("a"), plain("d")], rule=rule).config["use"] == "d"
    # 把 a 从可用候选里拿掉，结果仍是 c、d。
    assert choose(work, [plain("c"), plain("d")], rule=rule).config["use"] == "c"
    assert choose(work, [plain("d")], rule=rule).config["use"] == "d"


def test_fallback_result_stable_across_rounds(tmp_path):
    """ws#333：可用集合不变时，回退结果在同一件活多轮之间固定。"""
    rule = ["codex", "claude", "grok"]
    configs = [gated("codex", "codex"), plain("claude"), plain("grok")]
    work = find(rule, "codex")
    stale = snapshot(tmp_path / "stale.db", {"codex": {"Weekly": 0.0}}, NOW - timedelta(minutes=46))
    assert {choose(work, configs, path=stale).config["use"] for _ in range(5)} == {"claude"}


def test_all_available_distribution_unchanged():
    """ws#333：全部可用时分配不变——8 个候选各占 9.5%~15.5%，且每个活号分到的人与改动前逐个相同。"""
    configs = [plain(f"c{n}") for n in range(8)]
    works = [f"workshop:{n}" for n in range(1, 1001)]
    counts = Counter(choose(w, configs).config["use"] for w in works)
    assert len(counts) == 8 and all(95 <= v <= 155 for v in counts.values()), counts
    for w in works:
        assert choose(w, configs).config["use"] == configs[bucket(w, 8)]["use"]


def test_no_assignable_candidate_lists_each_reason(tmp_path):
    """ws#333：没有可分配候选时行为不变——返回空并逐个列出候选为什么不可分配。"""
    low = snapshot(tmp_path / "low.db", {"codex": {"Weekly": 90.0}, "claude": {"Weekly": 95.0}})
    configs = [gated("codex", "codex"), gated("claude", "claude")]
    choice = choose("x:1", configs, path=low)
    assert choice.config is None
    assert choice.reason.startswith("实验分配：x:1 没有可用候选，规则候选 2 个，可用 0 个；")
    assert "codex：codex 周额度剩 10%，低于门槛 15%" in choice.reason
    assert "claude：claude 周额度剩 5%，低于门槛 15%" in choice.reason
