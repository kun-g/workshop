"""PR 概况的分类和补历史脚本（issue #300）：只用假的 GitHub 响应，不联网。"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from workshop.core.log import Log
from workshop.plugins.issue_delivery import stats as prstats

from conftest import in_process_commands

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/pm/backfill_pr_stats.py"


def load_script():
    spec = importlib.util.spec_from_file_location("backfill_pr_stats", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def entry(name, additions, deletions, previous=None):
    value = {"filename": name, "additions": additions, "deletions": deletions}
    if previous:
        value["previous_filename"] = previous
    return value


def test_classify_three_kinds_and_renames():
    assert prstats.classify([entry("docs/a.md", 3, 1), entry("README.md", 1, 0)], 2)["kind"] == "只改文档"
    assert prstats.classify([entry("tests/test_a.py", 3, 1)], 1)["kind"] == "只改测试和文档"
    assert prstats.classify([entry("docs/a.md", 1, 0), entry("tests/test_a.py", 2, 0)], 2)["kind"] == "只改测试和文档"
    assert prstats.classify([entry("src/a.py", 3, 1), entry("docs/a.md", 1, 0)], 2)["kind"] == "改了程序"
    assert prstats.classify([entry("docs/new.py", 1, 1, "docs/old.py")], 1)["kind"] == "只改文档"
    assert prstats.classify([entry("src/new.py", 1, 1, "docs/old.py")], 1)["kind"] == "改了程序"
    assert prstats.classify([entry("src/new.py", 1, 1, "tests/old.py")], 1)["kind"] == "改了程序"


def test_classify_unknown_when_count_mismatch_or_over_limit():
    summary = prstats.classify([entry("docs/a.md", 2, 1)], 2)
    assert summary == {"files": 1, "additions": 2, "deletions": 1, "kind": "未知"}
    assert prstats.classify([entry("docs/a.md", 2, 1)], None)["kind"] == "未知"
    many = [entry("docs/a.md", 1, 0) for _ in range(prstats.FILE_LIMIT + 1)]
    assert prstats.classify(many, len(many))["kind"] == "未知"


def test_combine_adds_lines_and_takes_the_heaviest_kind():
    stats = {"11": {"files": 1, "additions": 10, "deletions": 5, "kind": "只改文档"},
             "12": {"files": 1, "additions": 2, "deletions": 1, "kind": "只改测试和文档"}}
    assert prstats.combine(stats) == {"lines": 18, "kind": "只改测试和文档"}
    stats["13"] = {"files": 1, "additions": 1, "deletions": 0, "kind": "改了程序"}
    assert prstats.combine(stats)["kind"] == "改了程序"
    stats["14"] = {"files": 1, "additions": 1, "deletions": 0, "kind": "未知"}
    assert prstats.combine(stats)["kind"] == "未知"
    assert prstats.combine({}) is None


@pytest.fixture
def bench(tmp_path, monkeypatch):
    commands = tmp_path / "bin"
    commands.mkdir()
    script = Path(__file__).parent / "fixtures/issue_delivery/fake_command.py"
    for name in ("gh", "git"):
        file = commands / name
        file.write_text(script.read_text())
        file.chmod(0o755)
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.environ["PATH"])
    in_process_commands(monkeypatch, commands)
    store = tmp_path / "remote.json"
    store.write_text(json.dumps({
        "pr": {"number": 11, "changed_files": 2, "merged": True, "state": "closed",
               "merge_commit_sha": "a" * 40, "head": {"sha": "a" * 40}, "body": ""},
        "files": [{"filename": "docs/a.md", "additions": 3, "deletions": 1},
                  {"filename": "docs/b.md", "additions": 2, "deletions": 0}]}))
    monkeypatch.setenv("WORKSHOP_FAKE_STATE", str(store))
    db = tmp_path / "workshop.db"
    log = Log(db)
    rules = {"targets": {"alpha": {"name": "alpha", "repo": "kun-g/workshop"}}}
    log.db.execute("INSERT INTO controls VALUES ('@rules', ?)", (json.dumps({"data": rules}),))
    for item, number in (("alpha:7", 7), ("alpha:8", 8)):
        log.append("event", {"target": "alpha", "object_key": str(number), "title": item, "depth": 0,
                             "action": "ready", "body": {}, "ext_id": item}, item)
    log.close()
    directory = tmp_path / "issue_delivery"
    directory.mkdir()
    state_path(directory, "alpha:7").write_text(json.dumps({"effects": {}, "issue": 7, "pr": 11, "merge": "a" * 40}))
    state_path(directory, "alpha:8").write_text(json.dumps({
        "effects": {}, "issue": 8, "pr": 12, "merge": "b" * 40,
        "pr_stats": {"12": {"files": 1, "additions": 1, "deletions": 0, "kind": "只改文档"}}}))
    return dict(db=db, directory=directory, script=load_script())


def state_path(directory, item_id):
    return directory / (hashlib.sha256(item_id.encode()).hexdigest() + ".json")


def test_print_lists_missing_pr_and_the_fixed_total_line(bench, capsys):
    bench["script"].main(["--state-db", str(bench["db"]), "--issue-delivery-state-dir", str(bench["directory"])])
    out = capsys.readouterr().out.splitlines()
    assert out == ["alpha:7\t#11\t文件 2、+5/-1、只改文档",
                   "合计：补 1 件；只改文档 1；只改测试和文档 0；改了程序 0；未知 0"]
    assert "pr_stats" not in json.loads(state_path(bench["directory"], "alpha:7").read_text())


def test_apply_writes_once_and_a_rerun_finds_nothing_to_add(bench, capsys):
    args = ["--apply", "--state-db", str(bench["db"]), "--issue-delivery-state-dir", str(bench["directory"])]
    bench["script"].main(args)
    recorded = json.loads(state_path(bench["directory"], "alpha:7").read_text())["pr_stats"]
    assert recorded == {"11": {"files": 2, "additions": 5, "deletions": 1, "kind": "只改文档"}}
    assert json.loads(state_path(bench["directory"], "alpha:8").read_text())["pr_stats"] == {
        "12": {"files": 1, "additions": 1, "deletions": 0, "kind": "只改文档"}}
    capsys.readouterr()
    bench["script"].main(args)
    assert capsys.readouterr().out.splitlines() == ["合计：补 0 件；只改文档 0；只改测试和文档 0；改了程序 0；未知 0"]


def test_apply_needs_both_paths_and_refuses_production(bench):
    with pytest.raises(SystemExit) as missing:
        bench["script"].main(["--apply", "--state-db", str(bench["db"])])
    assert "--issue-delivery-state-dir" in str(missing.value)
    with pytest.raises(SystemExit) as refused:
        bench["script"].main(["--apply", "--state-db", str(bench["db"]),
                              "--issue-delivery-state-dir", str(bench["script"].DEFAULT_DIRECTORY)])
    assert "生产目录" in str(refused.value)
    with pytest.raises(SystemExit) as refused_db:
        bench["script"].main(["--apply", "--state-db", str(bench["script"].DEFAULT_DB),
                              "--issue-delivery-state-dir", str(bench["directory"])])
    assert "生产目录" in str(refused_db.value)
