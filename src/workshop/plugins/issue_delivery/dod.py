"""登记核对与两段 DoD：接单时核对 issue 的几节，进交付时核对 PR 正文的合并前 DoD 证据，上线后取上线后 DoD 的检查。

只看文字结构，判得出过或不过；DoD 写得对不对由写 issue 的会话负责（docs/issue写法.md）。
"""
import json
import re

from .requirements import change_documents, changed_requirement, requirement_basis, section

UNCLEAR = re.compile(r"待定|TODO|待补充")
FENCE = re.compile(r"^\s*(```|~~~)")
DEFAULT_CHECKS = [["scripts/check.sh"]]
# workshop status、why 和 check-issue 报同一句。
CONSTRAINT = "「约束」节须单写「无」或至少列一条"


def _validate(value):
    if not isinstance(value, list) or not value or not all(
            isinstance(argv, list) and argv and all(isinstance(v, str) and v.strip() for v in argv) for argv in value):
        raise ValueError("检查须为非空 argv 列表")
    return value


def required_checks(target):
    """这件活要跑的检查：交付对象规则的 required_checks，没登记按 scripts/check.sh。

    不从 issue 里读，也不用活上以前存下的检查；规则加载时已核对格式（validate）。
    """
    value = target.get("required_checks")
    if value is None:
        return [list(argv) for argv in DEFAULT_CHECKS]
    try:
        return [list(argv) for argv in _validate(value)]
    except ValueError as exc:
        raise ValueError(f'{target.get("name", "交付对象")} 的 required_checks 格式不对：{exc}') from None


def constraint_written(body):
    """「约束」节去掉空行后第一行单独是「无」，或者节里至少有一条列表项。"""
    text = section(body or "", "约束")
    return _first_line(text) == "无" or bool(items(text))


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
    """接单核对（核心 R2、R3，issue交付 R2、R10）：返回 (缺的项, 这件活要跑的检查)。

    只看合并前 DoD、约束、要不要亲眼看、上线后 DoD 和需求变更；旧写法的「依据」「能跑的检查」
    「不可逆或对外的动作」三节不再要求（兼容：写了也不报错，「能跑的检查」不生效）。
    documents 为 None 时不核对需求变更指明的文档（调用方另取文档）。
    """
    body = body or ""
    problems = []
    dod = section(body, "合并前 DoD")
    if not items(dod):
        problems.append("缺「合并前 DoD」节，或节里没有逐项列出的条件")
    elif UNCLEAR.search(dod):
        problems.append("「合并前 DoD」还有待定项")
    if not constraint_written(body):
        problems.append(CONSTRAINT)
    visual = section(body, "要不要亲眼看")
    if visual and not re.match(r"(?:是|否)(?:\s|$)", visual):
        # 没写这一节按「否」。
        problems.append("「要不要亲眼看」第一行须写「是」或「否」")
    if _first_line(visual) == '是':
        for name, value in visual_contract(body).items():
            if not value:
                problems.append('亲眼看缺「' + {'contract': '看什么：', 'location': '在哪看：', 'response': '怎么回：'}[name] + '」段')
        if not visual_confirmed(body):
            # 介入点 R9、issue交付 R7：要 Kun 验收的，开单前和他确认过，原话带会话号留在这里。
            problems.append('亲眼看缺「Kun 确认：」段（Kun 对看什么的原话，> 引用，带会话号）')
    after = section(body, "上线后 DoD")
    if after:
        try:
            if not checks_in(after):
                problems.append("「上线后 DoD」须给每项写 workshop-checks 代码块")
        except (ValueError, TypeError) as exc:
            problems.append(f"「上线后 DoD」的检查格式不对：{exc}")
    # 只列已有需求编号的不核对编号存不存在；需求变更要有原文、原话和登记的文档。
    basis = requirement_basis(body)
    if changed_requirement(basis) is not None:
        if not changed_requirement(basis):
            problems.append("需求变更没写需求原文")
        elif not _quote(section(body, "Kun 的原话")):
            problems.append("需求变更：「Kun 的原话」节里没有原话")
        elif documents is not None:
            try:
                change_documents(basis, documents, target)
            except ValueError as exc:
                problems.append(str(exc))
    return problems, required_checks(target)


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
        match = re.search(r'^' + label + r'：(.*?)(?=^(?:看什么|在哪看|怎么回|Kun 确认)：|\Z)', text, re.M | re.S)
        result[key] = match[1].strip() if match else ''
    return result


def visual_confirmed(body):
    """「Kun 确认：」段里至少一行 > 引用的原话，并写明出自哪个会话（会话号）。"""
    text = section(body, '要不要亲眼看')
    match = re.search(r'^Kun 确认：(.*?)(?=^(?:看什么|在哪看|怎么回)：|\Z)', text, re.M | re.S)
    if not match:
        return False
    return (bool(re.search(r'^\s*>\s*\S', match[1], re.M))
            and bool(re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', match[1])))
