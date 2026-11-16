"""知识检查：用的层只有规定，一条规矩只有一份文字，用的层、条目文件、教训三者对得上。

用的层：USE_FILES 里以「- P1 」「- S3 」「- E2 」开头的行，一条一行。
条目文件：docs/knowledge/{principles,standards,practices}/<编号>-<短名>.md，front matter 有
  id、summary、written_in、mechanism、status、approved、sources。
教训：docs/knowledge/lessons/*.md，front matter 有 date、refs、produces。
"""
import re
import sys
from pathlib import Path

import yaml

USE_FILES = ("AGENTS.md", "docs/项目经理.md", "docs/协调对话.md")
KNOWLEDGE = "docs/knowledge"
KIND_DIR = {"P": "principles", "S": "standards", "E": "practices"}
LINE = re.compile(r"^- ([PSE]\d+) (.+)$")
QUOTE = re.compile(r"[「」“”]")
DATE = re.compile(r"\d{1,2}-\d{1,2}\b|\d{4}-\d{2}-\d{2}|\d+ ?月 ?\d+ ?日")
MAX_CHARS = 80
STATES = ("在用", "机制接管", "废止")


ENTRY_KEYS = ("id", "summary", "written_in", "mechanism", "status", "approved", "sources")
LESSON_KEYS = ("date", "refs", "produces")


def front_matter(path, keys, out):
    """读文件开头 --- 之间的 YAML；缺字段或格式不对记进 out，返回 None。"""
    text = path.read_text()
    name = f"{path.parent.name}/{path.name}"
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        out.append(f"{name}：开头要有 --- 包起来的 front matter")
        return None
    try:
        data = yaml.safe_load(text[4 : text.index("\n---\n", 4)]) or {}
    except yaml.YAMLError as e:
        out.append(f"{name}：front matter 不是合法 YAML（{e.__class__.__name__}）")
        return None
    missing = [k for k in keys if k not in data]
    if missing:
        out.append(f"{name}：front matter 缺 {'、'.join(missing)}")
        return None
    for k in ("written_in", "sources", "produces"):
        if k in data and not isinstance(data[k], list):
            out.append(f"{name}：{k} 要写成列表")
            return None
    return data


def use_lines(root):
    found = {}
    for name in USE_FILES:
        path = root / name
        if path.exists():
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if m := LINE.match(line):
                    found.setdefault(m[1], []).append((name, number, m[2]))
    return found


def problems(root):
    root = Path(root)
    base = root / KNOWLEDGE
    out = []
    used = use_lines(root)
    entries = {}
    for kind, folder in KIND_DIR.items():
        for path in sorted((base / folder).glob("*.md")):
            rule = path.name.split("-", 1)[0]
            if not re.fullmatch(kind + r"\d+", rule):
                out.append(f"{folder}/{path.name}：文件名要以 {kind}<序号>- 开头")
            elif rule in entries:
                out.append(f"{rule}：有两个条目文件")
            elif (f := front_matter(path, ENTRY_KEYS, out)) is not None:
                if f["id"] != rule:
                    out.append(f"{folder}/{path.name}：id 写的是 {f['id']}，文件名是 {rule}")
                entries[rule] = (path, f)
    lessons = {}
    for path in sorted((base / "lessons").glob("*.md")):
        if (f := front_matter(path, LESSON_KEYS, out)) is not None:
            lessons[path.stem] = set(f["produces"])

    for rule, places in used.items():
        for name, number, text in places:
            where = f"{name}:{number} {rule}"
            if QUOTE.search(text):
                out.append(f"{where}：用的层不放原话（「」或引号）")
            if DATE.search(text):
                out.append(f"{where}：用的层不放日期")
            if len(text) > MAX_CHARS:
                out.append(f"{where}：超过 {MAX_CHARS} 字（{len(text)}），细节放进条目文件")
        if len({text for _, _, text in places}) > 1:
            out.append(f"{rule}：几处文字不一致：" + "；".join(f"{n}:{i}" for n, i, _ in places))
        if rule not in entries and not any(p.name.startswith(rule + "-") for p in (base / KIND_DIR[rule[0]]).glob("*.md")):
            out.append(f"{rule}：没有条目文件（{KNOWLEDGE}/{KIND_DIR[rule[0]]}/{rule}-短名.md）")

    for rule, (path, f) in entries.items():
        name = f"{KIND_DIR[rule[0]]}/{path.name}"
        places = used.get(rule, [])
        files = sorted({n for n, _, _ in places})
        state = f["status"]
        if state not in STATES:
            out.append(f"{name}：状态只能是 {'、'.join(STATES)}")
        elif state == "在用":
            if not files:
                out.append(f"{name}：在用，但用的层里没有 {rule}")
            if places and f["summary"] != places[0][2]:
                out.append(f"{name}：summary 和用的层不一致")
            listed = sorted(f["written_in"])
            if files and listed != files:
                out.append(f"{name}：written_in 写的是「{'、'.join(listed)}」，实际在「{'、'.join(files)}」")
        elif files:
            out.append(f"{name}：{state}，用的层里却还有")
        if not f["sources"]:
            out.append(f"{name}：sources 至少列一篇教训")
        for lesson in f["sources"]:
            if lesson not in lessons:
                out.append(f"{name}：sources 里的 lessons/{lesson}.md 不存在")
            elif rule not in lessons[lesson]:
                out.append(f"{name}：sources 里的 lessons/{lesson}.md，produces 没写 {rule}")
    for lesson, made in lessons.items():
        for rule in made:
            if rule not in entries:
                out.append(f"lessons/{lesson}.md：produces {rule}，没有条目文件")
            elif lesson not in entries[rule][1]["sources"]:
                out.append(f"lessons/{lesson}.md：produces {rule}，但 {rule} 的 sources 里没有这篇")
    return out


if __name__ == "__main__":
    found = problems(Path(__file__).resolve().parents[1])
    for p in found:
        print("知识检查：" + p, file=sys.stderr)
    if found:
        sys.exit(1)
    print("知识检查通过")
