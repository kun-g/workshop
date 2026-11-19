"""额度门槛按配速（ws#298）：剩余不低于保底，或不低于「匀速线 − pace_margin」，满足一条就放行。"""
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest
import yaml

from conftest import build, configuration, current, event
from test_executor_profiles import config_with, profile
from test_experiment_selector import find
from workshop.plugins.selectors.experiment import ExperimentSelector
from workshop.plugins.selectors.quota import QuotaSelector
from workshop.quota_gate import Snapshot, Unreadable, judge, threshold
from workshop.registry import load_rules
from workshop.reports.report import why

NOW = datetime(2026, 11, 13, 12, 30, tzinfo=timezone.utc)
WEEK = 10080


def snapshot(path, used, resets=None, minutes=WEEK, collected=NOW, provider="codex"):
    """按 ai_quota 的表结构写一份快照：codex 的 Weekly 窗口，已用 used%，带重置时间和窗口长度。"""
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE payloads (id INTEGER PRIMARY KEY, run_id INTEGER, collected_at TEXT, source TEXT,
                               provider TEXT, account TEXT, plan TEXT, error TEXT, raw TEXT);
        CREATE TABLE readings (id INTEGER PRIMARY KEY, payload_id INTEGER, window_name TEXT, window_minutes INTEGER,
                               used_percent REAL, resets_at TEXT, balance REAL, currency TEXT);""")
    cursor = connection.execute("INSERT INTO payloads VALUES (NULL, 1, ?, 'magpie', ?, NULL, NULL, NULL, '{}')",
                                (collected.isoformat(), provider))
    resets = resets.isoformat() if isinstance(resets, datetime) else resets
    connection.execute("INSERT INTO readings VALUES (NULL, ?, 'Weekly', ?, ?, ?, NULL, NULL)",
                       (cursor.lastrowid, minutes, used, resets))
    connection.execute("INSERT INTO readings VALUES (NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL)", (cursor.lastrowid,))
    connection.commit()
    connection.close()
    return path


def gate(minimum=None, margin=None):
    quota = {"provider": "codex", "window": "Weekly", "label": "Codex 周额度"}
    if minimum is not None:
        quota["min_remaining"] = minimum
    if margin is not None:
        quota["pace_margin"] = margin
    return quota


def decide(path, quota, now=NOW):
    return judge(quota, Snapshot({"path": str(path), "max_age_minutes": 45}, now))


def until(pace):
    """离重置还剩多久，匀速线才是 pace%。"""
    return NOW + timedelta(minutes=WEEK * pace / 100)


@pytest.mark.parametrize("left, minimum, margin, passed, text", [
    # 只写保底：和原来逐字相同。
    (20, 15, None, True, "Codex 周额度剩 20%（门槛 15%）"),
    (15, 15, None, True, "Codex 周额度剩 15%（门槛 15%）"),
    (10, 15, None, False, "Codex 周额度剩 10%，低于门槛 15%"),
    # 只写配速：匀速线 50。
    (60, None, 0, True, "Codex 周额度剩 60%（匀速线 50%，余量 0），按配速放行"),
    (50, None, 0, True, "Codex 周额度剩 50%（匀速线 50%，余量 0），按配速放行"),
    (40, None, 0, False, "Codex 周额度剩 40%（匀速线 50%，余量 0），低于匀速线减余量 50%"),
    (40, None, 10, True, "Codex 周额度剩 40%（匀速线 50%，余量 10），按配速放行"),
    (55, None, -10, False, "Codex 周额度剩 55%（匀速线 50%，余量 -10），低于匀速线减余量 60%"),
    (60, None, -10, True, "Codex 周额度剩 60%（匀速线 50%，余量 -10），按配速放行"),
    # 两个都写：保底 / 配速各自满足或不满足的四种组合。
    (55, 15, 0, True, "Codex 周额度剩 55%（门槛 15%，匀速线 50%，余量 0），按保底放行"),
    (20, 15, 0, True, "Codex 周额度剩 20%（门槛 15%，匀速线 50%，余量 0），按保底放行"),
    (55, 60, 0, True, "Codex 周额度剩 55%（门槛 60%，匀速线 50%，余量 0），按配速放行"),
    (10, 15, 0, False, "Codex 周额度剩 10%（门槛 15%，匀速线 50%，余量 0），低于门槛 15%，也低于匀速线减余量 50%"),
    # 恰好相等：两条各自的边界都算放行。
    (15, 15, -40, True, "Codex 周额度剩 15%（门槛 15%，匀速线 50%，余量 -40），按保底放行"),
    (40, 60, 10, True, "Codex 周额度剩 40%（门槛 60%，匀速线 50%，余量 10），按配速放行"),
    (39.5, 60, 10, False, "Codex 周额度剩 39.5%（门槛 60%，匀速线 50%，余量 10），低于门槛 60%，也低于匀速线减余量 40%"),
])
def test_floor_or_pace_either_passes(tmp_path, left, minimum, margin, passed, text):
    path = snapshot(tmp_path / "quota.db", 100 - left, until(50))
    assert decide(path, gate(minimum, margin)) == (passed, text)


@pytest.mark.parametrize("resets, pace", [
    (NOW + timedelta(minutes=WEEK - 1), 99.99),   # 窗口刚开始
    (NOW + timedelta(minutes=WEEK / 2), 50),      # 过半
    (NOW + timedelta(minutes=10), 0.1),           # 临近重置
    (NOW + timedelta(minutes=2 * WEEK), 100),     # 算出来超过 100 的限制到 100
])
def test_pace_line_follows_selection_time(tmp_path, resets, pace):
    path = snapshot(tmp_path / "quota.db", 100.0, resets)
    passed, text = decide(path, gate(margin=0))
    assert f"匀速线 {pace:g}%" in text and passed is (pace == 0)


def test_now_is_selection_time_not_collection_time(tmp_path):
    # 快照 30 分钟前采的；匀速线按挑选时的现在算，是 50 而不是 50.3。
    path = snapshot(tmp_path / "quota.db", 50.0, until(50), collected=NOW - timedelta(minutes=30))
    assert decide(path, gate(margin=0)) == (True, "Codex 周额度剩 50%（匀速线 50%，余量 0），按配速放行")
    later = NOW + timedelta(minutes=WEEK / 1000)
    assert decide(path, gate(margin=0), later) == (True, "Codex 周额度剩 50%（匀速线 49.9%，余量 0），按配速放行")


UNKNOWN = [
    (None, WEEK, "没有重置时间"),
    (until(50), None, "没有窗口长度"),
    ("2026-11-17T00:00:00", WEEK, "重置时间不是带时区的时间（2026-11-17T00:00:00）"),
    ("下周一", WEEK, "重置时间不是带时区的时间（下周一）"),
    (until(50), 0, "窗口长度不是正数（0）"),
    (until(50), -5, "窗口长度不是正数（-5）"),
    (NOW - timedelta(minutes=1), WEEK, "重置时间已过，等下一次采集"),
    (NOW, WEEK, "重置时间已过，等下一次采集"),
]


@pytest.mark.parametrize("resets, minutes, why_not", UNKNOWN)
def test_unknown_pace_counts_as_not_met(tmp_path, resets, minutes, why_not):
    path = snapshot(tmp_path / "quota.db", 30.0, resets, minutes)
    # 没写保底：跳过并写明原因。
    assert decide(path, gate(margin=100)) == (False, f"Codex 周额度剩 70%，配速算不了（{why_not}），没写保底")
    # 写了保底：只看保底。
    assert decide(path, gate(60, 100)) == (True, f"Codex 周额度剩 70%（门槛 60%，配速算不了：{why_not}），按保底放行")
    assert decide(path, gate(80, 100)) == (False, f"Codex 周额度剩 70%（门槛 80%，配速算不了：{why_not}），低于门槛 80%")


def test_stale_snapshot_is_checked_first(tmp_path):
    path = snapshot(tmp_path / "quota.db", 0.0, until(50), collected=NOW - timedelta(minutes=46))
    with pytest.raises(Unreadable, match="额度快照过期"):
        decide(path, gate(margin=100))
    with pytest.raises(Unreadable, match="额度快照过期"):
        decide(snapshot(tmp_path / "gone.db", 0.0, NOW - timedelta(days=1), collected=NOW - timedelta(minutes=46)), gate(15, 0))


def test_threshold_needs_either_number():
    base = {"use": "x", "quota": {"provider": "codex", "window": "Weekly"}}
    assert threshold(base) is None
    assert threshold({**base, "quota": gate(margin=0)})["pace_margin"] == 0
    assert threshold({**base, "quota": gate(15)})["min_remaining"] == 15


BAD_MARGINS = ["5", True, False, 100.5, -101, float("nan"), [0]]


@pytest.mark.parametrize("margin", BAD_MARGINS)
def test_bad_pace_margin_rejected_at_selection(tmp_path, margin):
    bad = {"use": "orca-x", "family": "x", "billing": "subscription", "quota": {**gate(15), "pace_margin": margin}}
    path = snapshot(tmp_path / "quota.db", 0.0, until(50))
    with pytest.raises(ValueError, match="orca-x 的 quota.pace_margin"):
        pick("quota", path, [bad])
    with pytest.raises(ValueError, match="orca-x 的 quota.pace_margin"):
        pick("experiment", path, [bad])


@pytest.mark.parametrize("margin", BAD_MARGINS)
def test_bad_pace_margin_rejected_at_rule_load(tmp_path, margin):
    name = "orca-bad-pace"
    config = config_with({name: profile("codex", quota={"provider": "codex", "window": "Weekly", "pace_margin": margin})})
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match=f"配置档 {name} 的 quota.pace_margin"):
        load_rules(path)


def test_rule_load_accepts_pace_margin(tmp_path):
    for quota in (gate(margin=-20.5), gate(15, 100), gate(margin=-100)):
        config = config_with({"orca-pace": profile("codex", quota={k: v for k, v in quota.items() if k != "label"})})
        path = tmp_path / "rules.yaml"
        path.write_text(yaml.safe_dump(config))
        assert load_rules(path).data["executor_profiles"]["orca-pace"]["quota"] == {k: v for k, v in quota.items() if k != "label"}


def plain(name):
    return {"use": name, "family": name, "billing": "subscription"}


def pick(kind, path, configs):
    rule = [c["use"] for c in configs]
    work = find(rule, configs[0]["use"])
    target = {"quota_snapshot": {"path": str(path), "max_age_minutes": 45},
              "selection": {"writer": None, "reviewed": [], "retry_executor": None, "excluded": [],
                            "item": work, "rule_candidates": rule}}
    selector = (QuotaSelector if kind == "quota" else ExperimentSelector)(clock=lambda: NOW)
    return selector.select(target, "执行", 1, [], [{"config": c, "available": True} for c in configs])


@pytest.mark.parametrize("kind", ["quota", "experiment"])
@pytest.mark.parametrize("quota, chosen, text", [
    # 剩 11%，匀速线 9%：只写配速放行。
    (gate(margin=0), "codex", "Codex 周额度剩 11%（匀速线 9%，余量 0），按配速放行"),
    # 只写保底：照旧被 15% 挡住。
    (gate(15), "claude", "Codex 周额度剩 11%，低于门槛 15%"),
    # 两个都写：保底不够、配速够。
    (gate(15, 0), "codex", "Codex 周额度剩 11%（门槛 15%，匀速线 9%，余量 0），按配速放行"),
    # 只写配速但余量为负：不放行。
    (gate(margin=-5), "claude", "Codex 周额度剩 11%（匀速线 9%，余量 -5），低于匀速线减余量 14%"),
    # 都没写：不看额度。
    ({"provider": "codex", "window": "Weekly"}, "codex", None),
])
def test_both_selectors_use_the_shared_judgment(tmp_path, kind, quota, chosen, text):
    path = snapshot(tmp_path / "quota.db", 89.0, until(9))
    choice = pick(kind, path, [{**plain("codex"), "quota": quota}, plain("claude")])
    assert choice.config["use"] == chosen
    if text is None:
        assert "周额度" not in choice.reason
    elif kind == "quota" and chosen == "codex":
        assert choice.reason == f"{text}，选 codex；阶段 执行，第 1 轮"
    elif kind == "quota":
        assert choice.reason == f"{text}，跳过 codex；选 claude（无额度门槛）；阶段 执行，第 1 轮"
    elif chosen == "codex":
        assert choice.reason.endswith(f"可用 2 个；{text}；阶段 执行，第 1 轮")
    else:
        assert f"本该分给 codex，{text}，改分给 claude" in choice.reason


@pytest.mark.parametrize("kind", ["quota", "experiment"])
def test_pace_only_candidate_skipped_when_snapshot_missing(tmp_path, kind):
    """只写 pace_margin 的候选也看额度：快照读不到就跳过。"""
    choice = pick(kind, tmp_path / "missing.db", [{**plain("codex"), "quota": gate(margin=0)}, plain("claude")])
    assert choice.config["use"] == "claude" and "额度快照读不到" in choice.reason


def test_pace_reason_is_shown_by_why(tmp_path):
    now = datetime.now(timezone.utc)
    path = snapshot(tmp_path / "quota.db", 89.0, now + timedelta(minutes=WEEK * 0.09 + 30), collected=now)
    config = {**configuration(), "selector": "quota"}
    config["targets"]["alpha"]["quota_snapshot"] = {"path": str(path), "max_age_minutes": 45}
    config["executors"]["writer"] = [
        {"use": "writer", "family": "a", "billing": "subscription", "quota": gate(15, 0)},
        {"use": "acceptor", "family": "c", "billing": "subscription"}]
    engine = build(tmp_path / "state", config)
    try:
        engine.plugins.register("selector", "quota", QuotaSelector())
        engine.ingest(event())
        engine.tick()
        assert current(engine)["executor"] == "writer"
        text = why(engine.log, "alpha:one")
        assert "Codex 周额度剩 11%（门槛 15%，匀速线 9." in text and "余量 0），按配速放行，选 writer" in text
    finally:
        engine.log.close()
