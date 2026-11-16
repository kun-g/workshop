import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_knowledge", Path(__file__).resolve().parents[1] / "scripts/check_knowledge.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def entry(rule, text, where="AGENTS.md", state="在用", sources=("a.md",)):
    links = "、".join(f"[x](../lessons/{s})" for s in sources)
    return f"# {rule}\n\n- 一句话：{text}\n- 写在：{where}\n- 机制：只有文字\n- 状态：{state}\n- 批准：—\n- 来源：{links}\n"


def make(tmp_path, agents, entries, lessons, pm=""):
    k = tmp_path / "docs/knowledge"
    for d in ("lessons", "principles", "standards", "practices"):
        (k / d).mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text(agents)
    (tmp_path / "docs/项目经理.md").write_text(pm)
    for name, body in entries.items():
        (k / name).write_text(body)
    for name, made in lessons.items():
        (k / "lessons" / name).write_text(f"# x\n\n- 产出：{made}\n")
    return check.problems(tmp_path)


def test_two_lessons_one_practice(tmp_path):
    found = make(tmp_path, "- E1 先看第一屏。\n",
                 {"practices/E1-看第一屏.md": entry("E1", "先看第一屏。", sources=("a.md", "b.md"))},
                 {"a.md": "E1", "b.md": "E1"})
    assert found == []


def test_quote_date_length(tmp_path):
    rules = {"S1": "Kun 说「别这样」。", "S2": "11-15 那次之后不再这样。", "S3": "长" * 81}
    found = make(tmp_path, "".join(f"- {r} {t}\n" for r, t in rules.items()),
                 {f"standards/{r}-x.md": entry(r, t) for r, t in rules.items()}, {"a.md": "S1、S2、S3"})
    assert any("原话" in p for p in found) and any("日期" in p for p in found) and any("超过" in p for p in found)


def test_same_rule_same_text(tmp_path):
    found = make(tmp_path, "- S1 主检出只读。\n", {"standards/S1-x.md": entry("S1", "主检出只读。", "AGENTS.md、docs/项目经理.md")},
                 {"a.md": "S1"}, pm="- S1 主检出只读，别 pull。\n")
    assert any("不一致" in p for p in found)


def test_missing_entry_and_unused_entry(tmp_path):
    found = make(tmp_path, "- S1 主检出只读。\n", {"standards/S2-x.md": entry("S2", "别的。")}, {"a.md": "S2"})
    assert any("S1：没有条目文件" in p for p in found)
    assert any("在用，但用的层里没有 S2" in p for p in found)


def test_entry_must_match_use_layer(tmp_path):
    found = make(tmp_path, "- S1 主检出只读。\n", {"standards/S1-x.md": entry("S1", "主检出别动。", "docs/项目经理.md")}, {"a.md": "S1"})
    assert any("一句话和用的层不一致" in p for p in found) and any("实际在" in p for p in found)


def test_lesson_links_both_ways(tmp_path):
    found = make(tmp_path, "- S1 主检出只读。\n", {"standards/S1-x.md": entry("S1", "主检出只读。", sources=("a.md", "c.md"))},
                 {"a.md": "", "b.md": "S1"})
    assert any("a.md 的产出里没写 S1" in p for p in found)
    assert any("c.md 不存在" in p for p in found)
    assert any("b.md：产出 S1，但 S1 的来源里没有这篇" in p for p in found)


def test_mechanism_took_over(tmp_path):
    assert make(tmp_path, "", {"standards/S1-x.md": entry("S1", "主检出只读。", "—", state="机制接管")}, {"a.md": "S1"}) == []
