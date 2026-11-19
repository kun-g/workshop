"""workshop usage 按执行者配置档对比（核心 R10，#281）：事实逐条写进真实状态库，再读报告。"""
from datetime import datetime, timedelta, timezone

from workshop.core.log import Log
from workshop.reports.profiles import profile_rows
from workshop.reports.report import usage


DAY = datetime(2026, 11, 20, 4, tzinfo=timezone.utc)  # 各时区都还是 11-20 当天
CLAUDE = {"driver": "claude", "model": "claude-opus-5-5", "effort": "high", "billing": "subscription"}
CODEX = {"driver": "codex", "model": "gpt-6-astra", "effort": "high", "billing": "subscription"}


class Book:
    """按时间顺序往状态库写一件件活的事实。"""

    def __init__(self, tmp_path):
        self.log = Log(tmp_path / "workshop.db")
        self.at = DAY
        self.count = 0

    def tick(self, minutes=1):
        self.at += timedelta(minutes=minutes)
        return self.at.isoformat()

    def fact(self, kind, body, item, minutes=1):
        self.count += 1
        self.log.append(kind, body, item, ext_id=f"{kind}:{self.count}", at=self.tick(minutes))

    def item(self, item):
        self.fact("event", {"target": "alpha", "object_key": item, "title": item, "depth": 0,
                            "action": "ready", "body": {}, "ext_id": f"{item}:open"}, item)

    def run(self, item, executor, config, minutes, cost=1.0, ended=True, children=True):
        token = f"{item}:{self.count}"
        self.fact("round_reserved", dict(token=token, item=item, stage="执行", executor=executor,
                                         config={**config, "use": executor}, status="starting", at=self.tick(0),
                                         usage={"cost_usd": None}, usage_by_model=[], children_reported=False,
                                         passed=None, elapsed_seconds=None), item, minutes=0)
        if not ended:
            return
        self.at += timedelta(minutes=minutes)
        self.fact("round_changed", {"token": token, "status": "ended", "ended": self.at.isoformat(), "outcome": "done",
                                    "children_reported": children,
                                    "usage": {"cost_usd": cost},
                                    "usage_by_model": [{"scope": "main", "cost_usd": cost}]}, item, minutes=0)

    def stage(self, item, stage, gate=True, **extra):
        if gate:
            self.fact("gate_passed", {"gate_reason": ""}, item)
        self.fact("round_finished", {"stage": stage, "state": "空闲", "next": stage, **extra}, item)

    def delivery(self, item, reason):
        self.fact("program_result", {"token": f"program:{self.count}", "stage": "交付", "outcome": "done",
                                     "summary": "程序完成", "evidence": [], "reason": reason}, item)

    def done(self, item):
        self.stage(item, "收尾")
        self.fact("item_changed", {"state": "完成", "next": "已收尾"}, item)

    def rework(self, item):
        self.fact("event", {"target": "alpha", "object_key": item, "title": item, "depth": 0,
                            "action": "rework", "body": {"text": "不对"}, "ext_id": f"{item}:{self.count}"}, item)
        self.stage(item, "执行", gate=False)


PASSED = '{"reason": "合并前 DoD 已过：检查在干净副本重跑通过，PR 逐项附了证据", "head": "abc", "runs": []}'


def section(text):
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("按执行者配置档"))
    return lines[start:]


def row(text, label):
    found = [line.split("\t") for line in section(text) if line.startswith(label)]
    assert len(found) == 1, found
    return found[0]


def test_each_column_follows_the_table(tmp_path):
    book = Book(tmp_path)
    # 一号：一次过、收尾后被回「不对」；返工换回执行不算退回。
    book.item("a:1")
    book.run("a:1", "orca-claude", CLAUDE, 30, cost=2.0)
    book.stage("a:1", "交付")
    book.delivery("a:1", PASSED)
    book.done("a:1")
    book.rework("a:1")
    # 二号：合并前 DoD 没过退回一次，再进交付过了。
    book.item("a:2")
    book.run("a:2", "orca-claude", CLAUDE, 10, cost=1.0)
    book.stage("a:2", "交付")
    book.delivery("a:2", "合并前 DoD没过，退回执行：PR 的合并前 DoD 少了一项")
    book.stage("a:2", "执行")
    book.run("a:2", "orca-claude", CLAUDE, 20, cost=3.0)
    book.stage("a:2", "交付")
    book.delivery("a:2", PASSED)
    book.done("a:2")
    # 三号：合并前 DoD 过了、上线后 DoD 退回：一次过，退回一次；花费拿不到。
    book.item("a:3")
    book.run("a:3", "orca-claude", CLAUDE, 15, cost=None)
    book.stage("a:3", "交付")
    book.delivery("a:3", PASSED)
    book.delivery("a:3", "上线后 DoD没过，退回执行：上线后检查没过")
    book.stage("a:3", "执行")
    # 四号：合并失败（记在合并前 DoD 下）退回：没过。
    book.item("a:4")
    book.run("a:4", "orca-claude", CLAUDE, 25, cost=2.0)
    book.stage("a:4", "交付")
    book.delivery("a:4", PASSED)
    book.delivery("a:4", "合并前 DoD没过，退回执行：合并被拒绝")
    book.stage("a:4", "执行")

    rows = profile_rows(book.log, DAY)
    assert len(rows) == 1
    stats = rows[0]
    assert (stats["name"], stats["version"], stats["items"]) == ("orca-claude", 1, 4)
    assert (stats["first_passed"], stats["first_total"]) == (2, 4)
    assert stats["returns"] == 3 / 4
    assert stats["average_seconds"] == (30 + 30 + 15 + 25) * 60 / 4
    assert (stats["average_cost"], stats["cost_missing"]) == ((2.0 + 4.0 + 2.0) / 3, 1)
    assert stats["wrong"] == 1

    text = usage(book.log, DAY + timedelta(days=1), since="2026-11-20")
    assert section(text)[0] == "按执行者配置档（2026-11-20 起开工的活）"
    assert section(text)[1] == "配置档\t件数\t合并前DoD一次过\t每件退回\t每件执行用时\t每件花费USD\t收尾后「不对」"
    assert row(text, "orca-claude")[1:] == ["4", "50%（2/4）", "0.8", "25 分", "2.67（缺 1）", "1"]


def test_rework_and_recovery_out_of_delivery_are_not_returns(tmp_path):
    book = Book(tmp_path)
    book.item("a:1")
    book.run("a:1", "orca-codex", CODEX, 10)
    book.stage("a:1", "交付")
    book.delivery("a:1", PASSED)
    book.rework("a:1")  # 交付里回「不对」：返工不是交付退回，也不是收尾后
    book.run("a:1", "orca-codex", CODEX, 10)
    book.stage("a:1", "交付")
    book.delivery("a:1", PASSED)
    book.done("a:1")
    stats = profile_rows(book.log, DAY)[0]
    assert (stats["returns"], stats["wrong"]) == (0, 0)
    assert (stats["first_passed"], stats["first_total"]) == (1, 1)


def test_delivery_run_by_an_agent_does_not_count_as_entering_delivery(tmp_path):
    """旧流程的交付是 agent 一轮，没有交付的程序结果：一次过算不出，交回执行也不算退回。"""
    book = Book(tmp_path)
    book.item("a:1")
    book.run("a:1", "orca-codex", CODEX, 10)
    book.stage("a:1", "交付")
    book.stage("a:1", "执行")
    stats = profile_rows(book.log, DAY)[0]
    assert (stats["first_total"], stats["returns"]) == (0, 0)
    assert row(usage(book.log, DAY, since="2026-11-20"), "orca-codex")[2] == "未知"


def test_item_switching_profiles_goes_to_its_own_row(tmp_path):
    book = Book(tmp_path)
    book.item("a:1")
    book.run("a:1", "orca-codex", CODEX, 10)
    book.item("a:2")
    book.run("a:2", "orca-codex", CODEX, 10)
    book.run("a:2", "orca-claude", CLAUDE, 10)
    rows = profile_rows(book.log, DAY)
    assert [(r["name"], r["items"]) for r in rows] == [("orca-codex", 1), ("换过执行者", 1)]
    text = usage(book.log, DAY, since="2026-11-20")
    assert row(text, "换过执行者")[1:] == ["1", "—", "—", "—", "—", "—"]
    assert row(text, "orca-codex")[1] == "1"
    assert not any(line.startswith("orca-claude") for line in section(text))


def test_changed_profile_content_splits_rows_by_version(tmp_path):
    book = Book(tmp_path)
    book.item("a:1")
    book.run("a:1", "orca-codex", CODEX, 10)
    book.at += timedelta(days=2)
    book.item("a:2")
    book.run("a:2", "orca-codex", {**CODEX, "prompt": "只写测试先行的代码"}, 20)
    book.item("a:3")
    book.run("a:3", "orca-codex", {**CODEX, "prompt": "只写测试先行的代码"}, 40)
    # 键的顺序不同不算改过。
    book.item("a:4")
    book.run("a:4", "orca-codex", dict(reversed(list(CODEX.items()))), 10)
    text = usage(book.log, DAY + timedelta(days=3), since="2026-11-20")
    first = DAY.astimezone().date().isoformat()
    later = (DAY + timedelta(days=2)).astimezone().date().isoformat()
    assert row(text, f"orca-codex 第1版（{first} 起）")[1:2] == ["2"]
    assert row(text, f"orca-codex 第2版（{later} 起）")[1:5] == ["2", "未知", "0.0", "30 分"]


def test_since_and_default_window_filter_by_start_of_work(tmp_path):
    book = Book(tmp_path)
    book.item("a:old")
    book.run("a:old", "orca-claude", CLAUDE, 10)
    book.at += timedelta(days=10)
    # 开工早的活后来又跑执行，也不算进窗口。
    book.run("a:old", "orca-claude", CLAUDE, 10)
    book.item("a:new")
    book.run("a:new", "orca-codex", CODEX, 10)
    now = book.at + timedelta(hours=1)
    text = usage(book.log, now)
    assert section(text)[0] == f"按执行者配置档（最近 7 天开工的活：{(now.astimezone() - timedelta(days=7)):%Y-%m-%d %H:%M} 起）"
    assert [line.split("\t")[0].split(" ")[0] for line in section(text)[2:]] == ["orca-codex"]
    text = usage(book.log, now, since="2026-11-20")
    assert section(text)[0] == "按执行者配置档（2026-11-20 起开工的活）"
    assert [line.split("\t")[0].split(" ")[0] for line in section(text)[2:]] == ["orca-claude", "orca-codex"]


def test_unknown_time_and_cost_stay_unknown_and_do_not_pull_average(tmp_path):
    book = Book(tmp_path)
    book.item("a:1")
    book.run("a:1", "orca-codex", CODEX, 60, cost=3.0)
    book.item("a:2")
    book.run("a:2", "orca-codex", CODEX, 30, cost=1.0, children=False)  # 子 agent 没读到：花费不全
    book.item("a:3")
    book.run("a:3", "orca-codex", CODEX, 0, ended=False)  # 还在跑：用时、花费都未知
    assert row(usage(book.log, book.at, since="2026-11-20"), "orca-codex")[4:6] == ["45 分（缺 1）", "3.00（缺 2）"]
    book2 = Book(tmp_path / "only-unknown")
    book2.item("a:1")
    book2.run("a:1", "orca-codex", CODEX, 0, ended=False)
    assert row(usage(book2.log, book2.at, since="2026-11-20"), "orca-codex")[4:6] == ["未知（缺 1）", "未知（缺 1）"]


def test_no_items_means_no_section_and_bad_date_is_refused(tmp_path):
    book = Book(tmp_path)
    book.item("a:1")
    book.run("a:1", "orca-codex", CODEX, 10)
    assert "按执行者配置档" not in usage(book.log, DAY + timedelta(days=1), since="2026-11-21")
    assert "按执行者配置档" not in usage(Log(tmp_path / "empty.db"), DAY)
    try:
        usage(book.log, DAY, since="11-20")
    except ValueError as exc:
        assert "YYYY-MM-DD" in str(exc)
    else:
        raise AssertionError("日期写错应报错")
