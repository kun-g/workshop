"""知识检查：用的层只有规定，一条规矩只有一份文字，编号和台账对得上。

用的层：USE_FILES 里以「- P1 」「- S3 」「- E2 」开头的行，一条一行。
台账：docs/knowledge/ledger.md 的表格，一条规矩一行。
"""
import re
import sys
from pathlib import Path

USE_FILES = ("AGENTS.md", "docs/项目经理.md", "docs/协调对话.md")
LEDGER = "docs/knowledge/ledger.md"
LINE = re.compile(r"^- ([PSE]\d+) (.+)$")
QUOTE = re.compile(r"[「」“”]")
DATE = re.compile(r"\d{1,2}-\d{1,2}\b|\d{4}-\d{2}-\d{2}|\d+ ?月 ?\d+ ?日")
MAX_CHARS = 80
STATES = ("在用", "机制接管", "废止")


def use_lines(root):
    found = {}
    for name in USE_FILES:
        path = root / name
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if m := LINE.match(line):
                found.setdefault(m[1], []).append((name, number, m[2]))
    return found


def ledger_rows(root):
    rows = {}
    lines = (root / LEDGER).read_text().splitlines()
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 7 and re.fullmatch(r"[PSE]\d+", cells[0]):
            rows[cells[0]] = dict(zip(("id", "name", "where", "mechanism", "source", "approval", "state"), cells))
    return rows


def problems(root):
    root = Path(root)
    out = []
    used = use_lines(root)
    rows = ledger_rows(root)
    for rule, places in used.items():
        for name, number, text in places:
            where = f"{name}:{number} {rule}"
            if QUOTE.search(text):
                out.append(f"{where}：用的层不放原话（「」或引号）")
            if DATE.search(text):
                out.append(f"{where}：用的层不放日期")
            if len(text) > MAX_CHARS:
                out.append(f"{where}：超过 {MAX_CHARS} 字（{len(text)}），细节放进来历层")
        if len({text for _, _, text in places}) > 1:
            out.append(f"{rule}：几处文字不一致：" + "；".join(f"{n}:{i}" for n, i, _ in places))
        if rule not in rows:
            out.append(f"{rule}：台账里没有")
    for rule, row in rows.items():
        files = sorted({n for n, _, _ in used.get(rule, [])})
        listed = sorted(w.strip() for w in row["where"].split("、") if w.strip() and w.strip() != "—")
        if row["state"] not in STATES:
            out.append(f"台账 {rule}：状态只能是 {'、'.join(STATES)}")
        elif row["state"] == "在用" and not files:
            out.append(f"台账 {rule}：在用，但用的层里没有这一条")
        elif row["state"] != "在用" and files:
            out.append(f"台账 {rule}：{row['state']}，用的层里却还有")
        if row["state"] == "在用" and files != listed:
            out.append(f"台账 {rule}：写在「{'、'.join(listed)}」，实际在「{'、'.join(files)}」")
        links = re.findall(r"\]\((lessons/[^)]+)\)", row["source"])
        if not links:
            out.append(f"台账 {rule}：来源至少链一篇教训")
        for link in links:
            if not (root / "docs/knowledge" / link).exists():
                out.append(f"台账 {rule}：来源 {link} 不存在")
    return out


if __name__ == "__main__":
    found = problems(Path(__file__).resolve().parents[1])
    for p in found:
        print("知识检查：" + p, file=sys.stderr)
    if found:
        sys.exit(1)
    print("知识检查通过")
