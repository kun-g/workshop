"""登记核对与两段 DoD：接单时核对 issue 的几节，进交付时核对 PR 正文的合并前 DoD 证据，上线后取上线后 DoD 的检查。

只看文字结构，判得出过或不过；DoD 写得对不对由写 issue 的会话负责（docs/issue写法.md）。
"""
import json
import re

from .requirements import change_documents, changed_requirement, check_references, section

UNCLEAR = re.compile(r"待定|TODO|待补充")
FENCE = re.compile(r"^\s*(```|~~~)")


def _validate(value):
    if not isinstance(value, list) or not value or not all(
            isinstance(argv, list) and argv and all(isinstance(v, str) and v.strip() for v in argv) for argv in value):
        raise ValueError("检查须为非空 argv 列表")
    return value


def checks_in(text):
    """一段文字里全部 workshop-checks 代码块的检查，按出现顺序去重。"""
    result = []
    for block in re.findall(r"```workshop-checks[ \t]*\n(.*?)\n```", text or "", re.S):
        for argv in _validate(json.loads(block)):
            if argv not in result:
                result.append(argv)
    return result


def items(text):
    """顶层列表项（- * + 或 1. 1)）：第一行原文（压成单空格）和其后到下一项之前的内容；代码块里的行不算列表项。"""
    entries, current, fenced = [], None, False
    for line in (text or "").splitlines():
        if FENCE.match(line):
            fenced = not fenced
        match = None if fenced else re.match(r"^(?:[-*+]|\d+[.)])\s+(.*\S)\s*$", line)
        if match and not FENCE.match(line):
            current = {"text": " ".join(match[1].split()), "body": []}
            entries.append(current)
        elif current is not None:
            current["body"].append(line)
    return [{"text": e["text"], "body": "\n".join(e["body"])} for e in entries]


def _first_line(text):
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")


def _quote(text):
    """原话节里有引用：去掉「来源：运行现象」这类说明后还有字。"""
    lines = [line for line in (text or "").splitlines() if line.strip() and not line.strip().startswith("来源：")]
    return bool(lines)


def registration(body, target, documents=None):
    """接单核对：返回 (缺的项, 能跑的检查)。documents 为 None 时不核对依据里的编号（调用方另取文档）。"""
    body = body or ""
    problems = []
    checks = []
    runnable = section(body, "能跑的检查")
    try:
        checks = checks_in(runnable)
    except (ValueError, TypeError) as exc:
        problems.append(f"「能跑的检查」格式不对：{exc}")
    if not checks and not problems:
        problems.append("缺「能跑的检查」节或其中的 workshop-checks 代码块")
    required = target.get("required_checks", [["scripts/check.sh"]])
    missing = [argv for argv in required if checks and argv not in checks]
    if missing:
        problems.append("「能跑的检查」缺规则要求的 " + "、".join(" ".join(a) for a in missing))
    dod = section(body, "合并前 DoD")
    if not items(dod):
        problems.append("缺「合并前 DoD」节，或节里没有逐项列出的条件")
    elif UNCLEAR.search(dod):
        problems.append("「合并前 DoD」还有待定项")
    actions = section(body, "不可逆或对外的动作")
    if not actions:
        problems.append("缺「不可逆或对外的动作」节（没有就写「无」）")
    visual = section(body, "要不要亲眼看")
    if visual and not re.match(r"(?:是|否)(?:\s|$)", visual):
        # 没写这一节按「否」：核心 R2、R3 只要求检查、DoD 和不可逆动作三节。
        problems.append("「要不要亲眼看」第一行须写「是」或「否」")
    if _first_line(visual) == '是':
        for name, value in visual_contract(body).items():
            if not value:
                problems.append('亲眼看缺「' + {'contract': '看什么：', 'location': '在哪看：', 'response': '怎么回：'}[name] + '」段')
    after = section(body, "上线后 DoD")
    if after:
        try:
            if not checks_in(after):
                problems.append("「上线后 DoD」须给每项写 workshop-checks 代码块")
        except (ValueError, TypeError) as exc:
            problems.append(f"「上线后 DoD」的检查格式不对：{exc}")
    basis = section(body, "依据")
    if not basis:
        problems.append("缺「依据」节")
    elif changed_requirement(basis) is not None:
        if not changed_requirement(basis):
            problems.append("依据：需求变更没写需求原文")
        elif not _quote(section(body, "Kun 的原话")):
            problems.append("需求变更：「Kun 的原话」节里没有原话")
        elif documents is not None:
            try:
                change_documents(basis, documents, target)
            except ValueError as exc:
                problems.append(str(exc))
    elif documents is not None:
        try:
            check_references(basis, documents)
        except ValueError as exc:
            problems.append(str(exc))
    return problems, checks


def post_checks(body):
    """上线后 DoD 的检查；没写这一节为空。"""
    return checks_in(section(body or "", "上线后 DoD"))


def visual(body):
    return _first_line(section(body or "", "要不要亲眼看")).startswith("是")


def evidence_problems(pr_body, issue_body, checks):
    """PR 正文的「合并前 DoD」节逐项对上 issue 的合并前 DoD，每项附检查名或原样输出。"""
    wanted = items(section(issue_body or "", "合并前 DoD"))
    if not wanted:
        # 旧写法的在途 issue（说清楚轮核过、没有合并前 DoD 节）：没有可对的项，只靠重跑检查。
        return []
    written = items(section(pr_body or "", "合并前 DoD"))
    if not written:
        return ["PR 正文缺「合并前 DoD」节，或节里没有逐项列出"]
    names = {" ".join(argv) for argv in checks}
    problems = []
    for want in wanted:
        match = next((w for w in written if w["text"].startswith(want["text"])), None)
        if match is None:
            problems.append(f"PR 的合并前 DoD 少了一项：{want['text']}")
            continue
        extra = match["text"][len(want["text"]):] + "\n" + match["body"]
        fenced = re.search(r"```[^\n]*\n(.*?)\n\s*```", extra, re.S)
        named = any(code.strip() in names for code in re.findall(r"`([^`\n]+)`", extra))
        if not (fenced and fenced[1].strip()) and not named:
            problems.append(f"PR 的合并前 DoD 这一项没附证据（登记的检查名或原样输出代码块）：{want['text']}")
    return problems


def visual_contract(body):
    text = section(body, '要不要亲眼看')
    result = {}
    for key, label in (('contract', '看什么'), ('location', '在哪看'), ('response', '怎么回')):
        match = re.search(r'^' + label + r'：(.*?)(?=^(?:看什么|在哪看|怎么回)：|\Z)', text, re.M | re.S)
        result[key] = match[1].strip() if match else ''
    return result
