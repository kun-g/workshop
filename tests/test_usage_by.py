"""workshop usage --by：按活的类型拆开执行者配置档的对比表（issue #300）。

PR 概况是插件在合并时写进活状态文件的；这里按同一份格式写替身，只验证报告的读法和排版。
"""
from datetime import timedelta
import hashlib
import json
from pathlib import Path

from test_profile_usage import Book, CODEX, CLAUDE, DAY, section
from workshop.reports.profiles import combined
from workshop.reports.report import usage


def write_stats(directory, item_id, stats):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(item_id.encode()).hexdigest() + ".json")
    path.write_text(json.dumps({"pr_stats": stats}, ensure_ascii=False))


def pr(files, additions, deletions, kind):
    return {"files": files, "additions": additions, "deletions": deletions, "kind": kind}


def cells(text):
    return [line.split("\t") for line in section(text)[2:]]


def test_by_kind_adds_a_column_and_orders_buckets_unknown_last(tmp_path):
    book = Book(tmp_path)
    stats_dir = tmp_path / "issue_delivery"
    for number, kind in enumerate(["只改文档", "只改文档", "只改测试和文档", "改了程序"], 1):
        book.item(f"a:{number}")
        book.run(f"a:{number}", "orca-codex", CODEX, 10)
        write_stats(stats_dir, f"a:{number}", {"11": pr(2, 20, 2, kind)})
    book.item("a:5")
    book.run("a:5", "orca-codex", CODEX, 10)  # 没有状态文件
    text = usage(book.log, DAY + timedelta(days=1), since="2026-11-20", by="kind", pr_stats_dir=stats_dir)
    block = section(text)
    assert block[0] == "按执行者配置档 × 改了什么（2026-11-20 起开工的活）"
    assert block[1] == "配置档\t改了什么\t件数\t合并前DoD一次过\t每件退回\t每件执行用时\t每件花费USD\t收尾后「不对」"
    assert [(line[0].split(" ")[0], line[1], line[2]) for line in cells(text)] == [
        ("orca-codex", "只改文档", "2"), ("orca-codex", "只改测试和文档", "1"),
        ("orca-codex", "改了程序", "1"), ("orca-codex", "未知", "1")]


def test_by_size_splits_at_fifty_and_three_hundred_lines(tmp_path):
    book = Book(tmp_path)
    stats_dir = tmp_path / "issue_delivery"
    for number, (additions, deletions) in enumerate([(30, 19), (40, 10), (250, 50), (300, 1)], 1):
        book.item(f"a:{number}")
        book.run(f"a:{number}", "orca-codex", CODEX, 10)
        write_stats(stats_dir, f"a:{number}", {"11": pr(1, additions, deletions, "改了程序")})
    text = usage(book.log, DAY, since="2026-11-20", by="size", pr_stats_dir=stats_dir)
    assert section(text)[1] == "配置档\t改动多大\t件数\t合并前DoD一次过\t每件退回\t每件执行用时\t每件花费USD\t收尾后「不对」"
    assert [(line[0].split(" ")[0], line[1], line[2]) for line in cells(text)] == [
        ("orca-codex", "小", "1"), ("orca-codex", "中", "2"), ("orca-codex", "大", "1")]


def test_by_target_reads_the_item_and_puts_unknown_last(tmp_path):
    book = Book(tmp_path)
    book.item("a:1", target="workshop")
    book.run("a:1", "orca-codex", CODEX, 10)
    book.item("a:2", target="alpha")
    book.run("a:2", "orca-codex", CODEX, 10)
    book.item("a:3", target="grant-console")
    book.run("a:3", "orca-codex", CODEX, 10)
    book.item("a:4", target=None)
    book.run("a:4", "orca-codex", CODEX, 10)
    text = usage(book.log, DAY, since="2026-11-20", by="target", pr_stats_dir=tmp_path / "none")
    assert [(line[0].split(" ")[0], line[1], line[2]) for line in cells(text)] == [
        ("orca-codex", "alpha", "1"), ("orca-codex", "grant-console", "1"),
        ("orca-codex", "workshop", "1"), ("orca-codex", "未知", "1")]


def test_multiple_prs_add_lines_and_keep_the_heaviest_kind(tmp_path):
    book = Book(tmp_path)
    stats_dir = tmp_path / "issue_delivery"
    book.item("a:1")
    book.run("a:1", "orca-codex", CODEX, 10)
    write_stats(stats_dir, "a:1", {"11": pr(3, 20, 5, "只改文档"), "12": pr(2, 10, 0, "改了程序")})
    text = usage(book.log, DAY, since="2026-11-20", by="kind", pr_stats_dir=stats_dir)
    assert [(line[0].split(" ")[0], line[1], line[2]) for line in cells(text)] == [("orca-codex", "改了程序", "1")]
    # 35 行算小：两个 PR 的行数相加，不是只看一个。
    text = usage(book.log, DAY, since="2026-11-20", by="size", pr_stats_dir=stats_dir)
    assert [line[1] for line in cells(text)] == ["小"]


def test_switched_executor_row_is_split_by_the_same_dimension(tmp_path):
    book = Book(tmp_path)
    stats_dir = tmp_path / "issue_delivery"
    book.item("a:1")
    book.run("a:1", "orca-codex", CODEX, 10)
    write_stats(stats_dir, "a:1", {"11": pr(1, 10, 0, "只改文档")})
    book.item("a:2")
    book.run("a:2", "orca-codex", CODEX, 10)
    book.run("a:2", "orca-claude", CLAUDE, 10)
    write_stats(stats_dir, "a:2", {"12": pr(1, 100, 0, "改了程序")})
    text = usage(book.log, DAY, since="2026-11-20", by="kind", pr_stats_dir=stats_dir)
    assert [(line[0], line[1], line[2:]) for line in cells(text)] == [
        ("orca-codex 第1版（2026-11-20 起）", "只改文档", ["1", "未知", "0.0", "10 分", "1.00", "0"]),
        ("换过执行者", "改了程序", ["1", "—", "—", "—", "—", "—"])]


def test_without_by_the_table_is_unchanged(tmp_path):
    book = Book(tmp_path)
    book.item("a:1")
    book.run("a:1", "orca-codex", CODEX, 10)
    stats_dir = tmp_path / "issue_delivery"
    write_stats(stats_dir, "a:1", {"11": pr(1, 10, 0, "只改文档")})
    assert usage(book.log, DAY, since="2026-11-20", pr_stats_dir=stats_dir) == usage(book.log, DAY, since="2026-11-20")
    assert section(usage(book.log, DAY, since="2026-11-20"))[1] == (
        "配置档\t件数\t合并前DoD一次过\t每件退回\t每件执行用时\t每件花费USD\t收尾后「不对」")


def test_unknown_and_bad_by_values_are_refused(tmp_path):
    book = Book(tmp_path)
    book.item("a:1")
    book.run("a:1", "orca-codex", CODEX, 10)
    try:
        usage(book.log, DAY, since="2026-11-20", by="colour")
    except ValueError as exc:
        assert "kind" in str(exc) and "size" in str(exc) and "target" in str(exc)
    else:
        raise AssertionError("未知的 --by 应报错")


def test_combine_marks_whole_item_unknown_when_a_pr_is_unknown(tmp_path):
    assert combined({"1": pr(1, 10, 0, "只改文档"), "2": pr(1, 10, 0, "未知")}) == {"lines": 20, "kind": "未知"}
    assert combined({"1": pr(1, 10, 0, "只改文档"), "2": pr(1, 10, 0, "只改测试和文档")}) == {
        "lines": 20, "kind": "只改测试和文档"}
    assert combined({}) is None and combined(None) is None
