"""知识检查：用的层只有规定，一条规矩只有一份文字，用的层、条目文件、教训三者对得上。

用的层：USE_FILES 里以「- P1 」「- S3 」「- E2 」开头的行，一条一行。
条目文件：docs/knowledge/{principles,standards,practices}/<编号>-<短名>.md，开头几行「- 键：值」。
教训：docs/knowledge/lessons/*.md，开头「- 产出：」列出它促成的编号。
"""
import re
import sys
from pathlib import Path

USE_FILES = ("AGENTS.md", "docs/项目经理.md", "docs/协调对话.md")
KNOWLEDGE = "docs/knowledge"
KIND_DIR = {"P": "principles", "S": "standards", "E": "practices"}
LINE = re.compile(r"^- ([PSE]\d+) (.+)$")
FIELD = re.compile(r"^- (一句话|写在|机制|状态|批准|来源|产出)：(.*)$")
RULE = re.compile(r"\b[PSE]\d+\b")
QUOTE = re.compile(r"[「」“”]")
DATE = re.compile(r"\d{1,2}-\d{1,2}\b|\d{4}-\d{2}-\d{2}|\d+ ?月 ?\d+ ?日")
MAX_CHARS = 80
STATES = ("在用", "机制接管", "废止")


def fields(path):
    return {m[1]: m[2].strip() for line in path.read_text().splitlines() if (m := FIELD.match(line))}


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
            else:
                entries[rule] = (path, fields(path))
    lessons = {p.name: {r for r in RULE.findall(fields(p).get("产出", ""))} for p in sorted((base / "lessons").glob("*.md"))}

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
        if rule not in entries:
            out.append(f"{rule}：没有条目文件（{KNOWLEDGE}/{KIND_DIR[rule[0]]}/{rule}-短名.md）")

    for rule, (path, f) in entries.items():
        name = f"{KIND_DIR[rule[0]]}/{path.name}"
        places = used.get(rule, [])
        files = sorted({n for n, _, _ in places})
        state = f.get("状态", "")
        if state not in STATES:
            out.append(f"{name}：状态只能是 {'、'.join(STATES)}")
        elif state == "在用":
            if not files:
                out.append(f"{name}：在用，但用的层里没有 {rule}")
            if places and f.get("一句话") != places[0][2]:
                out.append(f"{name}：一句话和用的层不一致")
            listed = sorted(w.strip() for w in f.get("写在", "").split("、") if w.strip())
            if files and listed != files:
                out.append(f"{name}：写在「{'、'.join(listed)}」，实际在「{'、'.join(files)}」")
        elif files:
            out.append(f"{name}：{state}，用的层里却还有")
        sources = re.findall(r"\]\(\.\./lessons/([^)]+)\)", f.get("来源", ""))
        if not sources:
            out.append(f"{name}：来源至少链一篇教训")
        for lesson in sources:
            if lesson not in lessons:
                out.append(f"{name}：来源 lessons/{lesson} 不存在")
            elif rule not in lessons[lesson]:
                out.append(f"{name}：来源 lessons/{lesson} 的产出里没写 {rule}")
    for lesson, made in lessons.items():
        for rule in made:
            if rule not in entries:
                out.append(f"lessons/{lesson}：产出 {rule} 没有条目文件")
            elif f"../lessons/{lesson})" not in entries[rule][1].get("来源", ""):
                out.append(f"lessons/{lesson}：产出 {rule}，但 {rule} 的来源里没有这篇")
    return out


if __name__ == "__main__":
    found = problems(Path(__file__).resolve().parents[1])
    for p in found:
        print("知识检查：" + p, file=sys.stderr)
    if found:
        sys.exit(1)
    print("知识检查通过")
