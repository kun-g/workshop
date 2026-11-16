import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_knowledge", Path(__file__).resolve().parents[1] / "scripts/check_knowledge.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

HEAD = "| 编号 | 短名 | 写在 | 机制 | 来源 | 批准 | 状态 |\n|---|---|---|---|---|---|---|\n"


def make(tmp_path, agents, pm="", ledger=HEAD, lessons=("a.md",)):
    (tmp_path / "docs/knowledge/lessons").mkdir(parents=True)
    for name in lessons:
        (tmp_path / "docs/knowledge/lessons" / name).write_text("x")
    (tmp_path / "AGENTS.md").write_text(agents)
    (tmp_path / "docs/项目经理.md").write_text(pm)
    (tmp_path / "docs/knowledge/ledger.md").write_text(ledger)
    return check.problems(tmp_path)


def row(rule, where, state="在用", source="[a](lessons/a.md)"):
    return f"| {rule} | 名 | {where} | 只有文字 | {source} | Kun | {state} |\n"


def test_clean(tmp_path):
    assert make(tmp_path, "- S1 主检出只读。\n", ledger=HEAD + row("S1", "AGENTS.md")) == []


def test_quote_date_length(tmp_path):
    found = make(tmp_path, "- S1 Kun 说「别这样」。\n- S2 11-15 那次之后不再这样。\n- S3 " + "长" * 81 + "\n",
                 ledger=HEAD + row("S1", "AGENTS.md") + row("S2", "AGENTS.md") + row("S3", "AGENTS.md"))
    assert any("原话" in p for p in found) and any("日期" in p for p in found) and any("超过" in p for p in found)


def test_same_rule_same_text(tmp_path):
    found = make(tmp_path, "- S1 主检出只读。\n", pm="- S1 主检出只读，别 pull。\n",
                 ledger=HEAD + row("S1", "AGENTS.md、docs/项目经理.md"))
    assert any("不一致" in p for p in found)


def test_ledger_both_ways(tmp_path):
    found = make(tmp_path, "- S1 主检出只读。\n", ledger=HEAD + row("S2", "AGENTS.md"))
    assert "S1：台账里没有" in found
    assert any("S2：在用，但用的层里没有" in p for p in found)


def test_where_and_source(tmp_path):
    found = make(tmp_path, "- S1 主检出只读。\n", ledger=HEAD + row("S1", "docs/项目经理.md", source="[b](lessons/b.md)"))
    assert any("实际在" in p for p in found) and any("不存在" in p for p in found)


def test_mechanism_took_over(tmp_path):
    assert make(tmp_path, "", ledger=HEAD + row("S1", "—", state="机制接管")) == []
