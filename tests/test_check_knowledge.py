import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("check_knowledge", Path(__file__).resolve().parents[1] / "scripts/check_knowledge.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def entry(rule, text, where=("AGENTS.md",), state="在用", sources=("a",)):
    return (f"---\nid: {rule}\nsummary: {text}\nwritten_in: [{', '.join(where)}]\nmechanism: 只有文字\n"
            f"status: {state}\napproved: —\nsources: [{', '.join(sources)}]\n---\n\n# {rule}\n")


def make(tmp_path, agents, entries, lessons, pm=""):
    k = tmp_path / "docs/knowledge"
    for d in ("lessons", "principles", "standards", "experiences"):
        (k / d).mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text(agents)
    (tmp_path / "docs/项目经理.md").write_text(pm)
    for name, body in entries.items():
        (k / name).write_text(body)
    for name, made in lessons.items():
        body = made if made.startswith("---") else "---\nrefs: x\n---\n\n# x\n"
        (k / "lessons" / f"{name}.md").write_text(body)
    return check.problems(tmp_path)


def test_two_lessons_one_experience(tmp_path):
    found = make(tmp_path, "- E1 先看第一屏。\n",
                 {"experiences/E1-看第一屏.md": entry("E1", "先看第一屏。", sources=("a", "b"))},
                 {"a": "E1", "b": "E1"})
    assert found == []


def test_quote_date_length(tmp_path):
    rules = {"S1": "Kun 说「别这样」。", "S2": "11-15 那次之后不再这样。", "S3": "长" * 81}
    found = make(tmp_path, "".join(f"- {r} {t}\n" for r, t in rules.items()),
                 {f"standards/{r}-x.md": entry(r, t) for r, t in rules.items()}, {"a": "S1, S2, S3"})
    assert any("原话" in p for p in found) and any("日期" in p for p in found) and any("超过" in p for p in found)


def test_same_rule_same_text(tmp_path):
    found = make(tmp_path, "- S1 主检出只读。\n", {"standards/S1-x.md": entry("S1", "主检出只读。", ("AGENTS.md", "docs/项目经理.md"))},
                 {"a": "S1"}, pm="- S1 主检出只读，别 pull。\n")
    assert any("不一致" in p for p in found)


def test_missing_entry_and_unused_entry(tmp_path):
    found = make(tmp_path, "- S1 主检出只读。\n", {"standards/S2-x.md": entry("S2", "别的。")}, {"a": "S2"})
    assert any("S1：没有条目文件" in p for p in found)
    assert any("在用，但用的层里没有 S2" in p for p in found)


def test_entry_must_match_use_layer(tmp_path):
    found = make(tmp_path, "- S1 主检出只读。\n", {"standards/S1-x.md": entry("S1", "主检出别动。", ("docs/项目经理.md",))}, {"a": "S1"})
    assert any("summary 和用的层不一致" in p for p in found) and any("实际在" in p for p in found)


def test_sources_point_down(tmp_path):
    agents = "- P1 车间自己运转。\n- S1 主检出只读。\n- E1 先看第一屏。\n"
    found = make(tmp_path, agents, {"principles/P1-x.md": entry("P1", "车间自己运转。", sources=("S1", "a")),
                                    "standards/S1-x.md": entry("S1", "主检出只读。", sources=("E1", "P1", "c", "E9")),
                                    "experiences/E1-x.md": entry("E1", "先看第一屏。")}, {"a": ""})
    assert not any(p.startswith("principles/") or p.startswith("experiences/") for p in found)
    assert any("sources 只能引下级，P1 不比 S1 低" in p for p in found)
    assert any("c.md 不存在" in p for p in found)
    assert any("E9 没有条目文件" in p for p in found)


def test_mechanism_took_over(tmp_path):
    assert make(tmp_path, "", {"standards/S1-x.md": entry("S1", "主检出只读。", (), state="机制接管")}, {"a": "S1"}) == []


def test_front_matter_required(tmp_path):
    found = make(tmp_path, "- S1 主检出只读。\n- S2 别的。\n- S3 再一条。\n",
                 {"standards/S1-x.md": "# S1\n\n- 一句话：主检出只读。\n",
                  "standards/S2-x.md": "---\nid: S2\nsummary: 别的。\n---\n",
                  "standards/S3-x.md": entry("S9", "再一条。")}, {"a": "S3"})
    assert any("S1-x.md：开头要有" in p for p in found)
    assert any("S2-x.md：front matter 缺 written_in" in p for p in found)
    assert any("S3-x.md：id 写的是 S9" in p for p in found)
    assert not any("S1：没有条目文件" in p for p in found)


def problem(status, body="## 经过\n- 11-20 22:29 ws#310 推不上去\n"):
    return f"---\nrefs: ws#310\nstatus: {status}\n---\n\n# 令牌过期\n\n{body}"


def test_problem_status_values(tmp_path):
    good = {f"p{i}": problem(s) for i, s in enumerate(["未处理", "手工干预", "已解决", "不做", "已开单 ws#324", "已开单 ws#325、gc#216"])}
    assert make(tmp_path, "", {}, good) == []


def test_problem_status_rejects_free_text_and_missing_section(tmp_path):
    found = make(tmp_path, "", {}, {"a": problem("未处理（建议不做）"), "b": problem("已开单 #324"),
                                    "c": problem("未处理", body="只有一段话\n")})
    assert any("a.md：status 只能是" in p for p in found)
    assert any("b.md：status 只能是" in p for p in found)
    assert any("c.md：问题记录要有「## 经过」" in p for p in found)


def test_plain_lesson_needs_no_status(tmp_path):
    assert make(tmp_path, "", {}, {"a": "---\nrefs: x\n---\n\n# 一件事\n"}) == []


def test_open_problems_skips_closed(tmp_path):
    make(tmp_path, "", {}, {"20261120-a": problem("未处理"), "20261120-b": problem("已解决"),
                            "20261120-c": problem("已开单 ws#324"), "20261120-d": "---\nrefs: x\n---\n\n# 教训\n"})
    assert [(stem, status) for stem, status, _ in check.open_problems(tmp_path)] == [
        ("20261120-a", "未处理"), ("20261120-c", "已开单 ws#324")]
