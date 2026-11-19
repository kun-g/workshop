"""quota 挑选逻辑：按候选顺序取第一个额度够的，额度读本机 ai_quota 快照库（只读）。"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from conftest import build, configuration, current, event
from workshop.reports.report import why
from workshop.core.rules import Rules
from workshop.plugins.selectors.quota import QuotaSelector
from workshop.registry import Registry

NOW = datetime(2026, 11, 13, 12, 30, tzinfo=timezone.utc)
RULES = Rules.load(Path(__file__).parents[1] / "rules" / "workshop.yaml")


def snapshot(path, readings, collected=NOW):
    """按 ai_quota 的表结构写一份快照：每家一次成功采集，used_percent 是已用百分比。"""
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE payloads (id INTEGER PRIMARY KEY, run_id INTEGER, collected_at TEXT, source TEXT,
                               provider TEXT, account TEXT, plan TEXT, error TEXT, raw TEXT);
        CREATE TABLE readings (id INTEGER PRIMARY KEY, payload_id INTEGER, window_name TEXT, window_minutes INTEGER,
                               used_percent REAL, resets_at TEXT, balance REAL, currency TEXT);""")
    for provider, windows in readings.items():
        # 先写一条更新的失败采集：只认成功的那次。
        cursor = connection.execute("INSERT INTO payloads VALUES (NULL, 1, ?, 'codexbar', ?, NULL, NULL, NULL, '{}')",
                                    (collected.isoformat(), provider))
        for name, used in windows.items():
            connection.execute("INSERT INTO readings VALUES (NULL, ?, ?, 10080, ?, NULL, NULL, NULL)", (cursor.lastrowid, name, used))
        connection.execute("INSERT INTO payloads VALUES (NULL, 2, ?, 'codexbar', ?, NULL, NULL, 'network', '{}')",
                           (collected.isoformat(), provider))
    connection.commit()
    connection.close()
    return path


def target(path, writer=None):
    return {**RULES.target("workshop"), "quota_snapshot": {"path": str(path), "max_age_minutes": 45},
            "selection": {"writer": writer, "reviewed": [], "retry_executor": None, "excluded": []}}


def choose(path, stage, writer=None, candidates=None):
    configs = candidates if candidates is not None else RULES.candidates(stage, "workshop", writer)
    return QuotaSelector(clock=lambda: NOW).select(target(path, writer), stage, 1, [],
                                                   [{"config": c, "available": True} for c in configs])


def test_all_stages_use_quota_and_writer_tries_codex_first():
    for stage in ("接单", "说清楚", "执行", "证据", "审查", "交付", "验收", "收尾"):
        assert RULES.selector(stage, "workshop") == "quota"
        assert RULES.selector(stage, "grant-console") == "quota"
    writer = RULES.candidates("执行", "workshop")
    assert [c["use"] for c in writer] == ["orca-codex", "orca-claude"]
    assert writer[0]["model"] and writer[0]["effort"] and writer[0]["quota"]["min_remaining"] == 15
    assert callable(Registry().get("selector", "quota").select)


def test_grok_bot_at_zero_skips_cursor_grok_for_review(tmp_path):
    path = snapshot(tmp_path / "quota.db", {"cursor": {"Grok Bot": 100.0}, "grok": {"Weekly": 0.0}})
    grok = {"use": "cursor-grok", "family": "grok", "billing": "subscription",
            "quota": {"provider": "cursor", "window": "Grok Bot", "min_remaining": 10, "label": "Cursor Grok Bot 周额度"}}
    claude = {"use": "claude", "family": "claude", "billing": "subscription"}
    choice = choose(path, "审查", "codex", [grok, claude])
    assert choice.config == claude
    assert "Cursor Grok Bot 周额度剩 0%，低于门槛 10%，跳过 cursor-grok" in choice.reason


@pytest.mark.parametrize("used, chosen, text", [
    (50.0, "orca-codex", "Codex 周额度剩 50%（门槛 15%），选 orca-codex"),
    (85.0, "orca-codex", "Codex 周额度剩 15%（门槛 15%），选 orca-codex"),
    (90.0, "orca-claude", "Codex 周额度剩 10%，低于门槛 15%，跳过 orca-codex"),
])
def test_codex_weekly_quota_decides_writer(tmp_path, used, chosen, text):
    path = snapshot(tmp_path / "quota.db", {"codex": {"Weekly": used}})
    choice = choose(path, "执行")
    assert choice.config["use"] == chosen
    assert text in choice.reason


def test_unreadable_snapshot_falls_back_to_claude_with_reason(tmp_path):
    choice = choose(tmp_path / "missing.db", "执行")
    assert choice.config["use"] == "orca-claude"
    assert "额度快照读不到" in choice.reason and "missing.db 不存在" in choice.reason and "跳过 orca-codex" in choice.reason
    broken = tmp_path / "broken.db"
    broken.write_text("不是 sqlite")
    choice = choose(broken, "执行")
    assert choice.config["use"] == "orca-claude" and "额度快照读不到" in choice.reason


def test_stale_or_missing_window_skips_threshold(tmp_path):
    stale = snapshot(tmp_path / "stale.db", {"codex": {"Weekly": 0.0}}, NOW - timedelta(minutes=46))
    choice = choose(stale, "执行")
    assert choice.config["use"] == "orca-claude" and "额度快照过期" in choice.reason and "超过 45 分钟" in choice.reason
    fresh = snapshot(tmp_path / "fresh.db", {"codex": {"Weekly": 0.0}}, NOW - timedelta(minutes=44))
    assert choose(fresh, "执行").config["use"] == "orca-codex"
    other = snapshot(tmp_path / "other.db", {"claude": {"Weekly": 0.0}})
    choice = choose(other, "执行")
    assert choice.config["use"] == "orca-claude" and "没有 codex 的成功采集" in choice.reason


def test_snapshot_is_read_only(tmp_path):
    path = snapshot(tmp_path / "quota.db", {"codex": {"Weekly": 50.0}})
    before = path.read_bytes()
    choose(path, "执行")
    assert path.read_bytes() == before and not Path(str(path) + "-journal").exists()


def test_review_and_acceptance_switch_family_after_codex_writer(tmp_path):
    path = tmp_path / "missing.db"
    review = choose(path, "审查", "codex")
    assert review.config["use"] == "claude"
    assert "写代码的是 codex 家，claude 排第一" in review.reason
    assert choose(path, "验收", "codex").config["use"] == "claude"
    # 写代码的是 claude 家时照旧：codex 打头。
    assert choose(path, "审查", "claude").config["use"] == "codex"
    assert choose(path, "验收", "claude").config["use"] == "codex"


def test_nothing_chosen_when_every_candidate_lacks_quota(tmp_path):
    path = snapshot(tmp_path / "quota.db", {"codex": {"Weekly": 99.0}})
    only = [c for c in RULES.candidates("执行", "workshop") if c["use"] == "orca-codex"]
    choice = choose(path, "执行", candidates=only)
    assert choice.config is None
    assert "Codex 周额度剩 1%，低于门槛 15%，跳过 orca-codex" in choice.reason
    assert choose(path, "执行", candidates=[]) is None


def test_invalid_threshold_is_rule_error(tmp_path):
    bad = {"use": "x", "family": "x", "billing": "subscription", "quota": {"provider": "codex", "window": "Weekly", "min_remaining": 150}}
    with pytest.raises(ValueError, match="0 到 100"):
        choose(tmp_path / "missing.db", "执行", candidates=[bad])


def test_reason_is_recorded_on_the_round_and_shown_by_why(tmp_path):
    path = snapshot(tmp_path / "quota.db", {"codex": {"Weekly": 30.0}}, datetime.now(timezone.utc))
    config = {**configuration(), "selector": "quota"}
    config["targets"]["alpha"]["quota_snapshot"] = {"path": str(path), "max_age_minutes": 45}
    config["executors"]["writer"] = [
        {"use": "writer", "family": "a", "billing": "subscription",
         "quota": {"provider": "codex", "window": "Weekly", "min_remaining": 15, "label": "Codex 周额度"}},
        {"use": "acceptor", "family": "c", "billing": "subscription"}]
    engine = build(tmp_path / "state", config)
    try:
        engine.plugins.register("selector", "quota", QuotaSelector())
        engine.ingest(event())
        engine.tick()
        row = current(engine)
        assert row["executor"] == "writer"
        assert row["selection_reason"].startswith("Codex 周额度剩 70%（门槛 15%），选 writer")
        assert "Codex 周额度剩 70%" in why(engine.log, "alpha:one")
    finally:
        engine.log.close()


def test_reason_kept_when_every_candidate_is_skipped(tmp_path):
    """审查的必须改：Claude 不可用、Codex 剩 10% 时，why 和卡片要写出额度和门槛，不只是通用提示。"""
    path = snapshot(tmp_path / "quota.db", {"codex": {"Weekly": 90.0}}, datetime.now(timezone.utc))
    config = {**configuration(), "selector": "quota"}
    config["targets"]["alpha"]["quota_snapshot"] = {"path": str(path), "max_age_minutes": 45}
    config["executors"]["writer"] = [
        {"use": "writer", "family": "a", "billing": "subscription",
         "quota": {"provider": "codex", "window": "Weekly", "min_remaining": 15, "label": "Codex 周额度"}},
        {"use": "acceptor", "family": "c", "billing": "subscription"}]
    engine = build(tmp_path / "state", config)
    try:
        engine.plugins.register("selector", "quota", QuotaSelector())
        engine.plugins.get("executor", "acceptor").enabled = False
        engine.ingest(event())
        engine.tick()
        assert not engine.active()
        text = "Codex 周额度剩 10%，低于门槛 15%，跳过 writer"
        assert "挑选逻辑没选出执行者 " + text in why(engine.log, "alpha:one")
        assert engine.log.all("cards") == []
        assert text in engine.log.get("items", "alpha:one")["blocked_by"]
        assert engine.log.get("items", "alpha:one")["state"] == "卡住"
    finally:
        engine.log.close()
