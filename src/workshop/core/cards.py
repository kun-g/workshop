"""通道的严格卡片格式；限制以 Unicode 字符计。"""
from typing import Literal, TypedDict


class Choice(TypedDict):
    key: str
    label: str
    effect: str


class Question(TypedDict):
    subject: str
    decision: str
    context: str
    choices: list[Choice]
    recommend: str
    reason: str
    kind: Literal["approval", "reversible"]


class Notice(TypedDict):
    subject: str
    text: str
    fyi: bool


def text_field(data, name, limit=None):
    value = data.get(name)
    if not isinstance(value, str) or not value.strip() or (limit and len(value) > limit):
        raise ValueError(f"卡片 {name} 为空或超长")


def text_problems(data, specs, prefix=""):
    problems = []
    for name, limit in specs:
        try:
            text_field(data, name, limit)
        except ValueError as exc:
            problems.append(f"{prefix}{exc}")
    return problems


def validate_question(data):
    problems = text_problems(data, (("subject", 30), ("decision", 42), ("context", None), ("reason", 60)))
    choices = data.get("choices")
    keys = set()
    if not isinstance(choices, list):
        problems.append("choices: 问题须有 2–3 个选项")
    else:
        if not 2 <= len(choices) <= 3:
            problems.append("choices: 问题须有 2–3 个选项")
        for i, choice in enumerate(choices):
            if not isinstance(choice, dict):
                problems.append(f"choices[{i}] 必须是 JSON 对象")
                continue
            problems.extend(text_problems(choice, (("key", 18), ("label", 18), ("effect", 60)), f"choices[{i}]: "))
            key = choice.get("key")
            if isinstance(key, str):
                if key in keys:
                    problems.append(f"choices[{i}]: 选项 key 重复")
                keys.add(key)
        if not isinstance(data.get("recommend"), str) or data["recommend"] not in keys:
            problems.append("recommend: 推荐选项无效")
    if data.get("kind") not in ("approval", "reversible"):
        problems.append("kind: 问题 kind 无效")
    if problems:
        raise ValueError("；".join(problems))


def validate_notice(data):
    problems = text_problems(data, (("subject", 30), ("text", None)))
    if data.get("fyi") is not True:
        problems.append("fyi: 通知必须 fyi=true")
    if problems:
        raise ValueError("；".join(problems))


def frozen_question(item, files, actions=()):
    """改动碰到冻结路径：列出文件，问 Kun 放不放行；推荐不放行，冻结本来就是默认不让改。"""
    lines = "\n".join(f"- {f}" for f in files)
    extra = f"\n同时要授权的交付动作：{'、'.join(actions)}；放行一并授权。" if actions else ""
    context = (f'{item["id"]}（{item.get("title", "")}）的改动碰到规则里的冻结路径，下面这些文件要你单独放行：\n'
               f"{lines}{extra}\n放行：照常往下走，这些文件以后不再问；不放行：退回执行。")
    return dict(subject="改动碰到冻结路径，放行吗", decision="放行这件活对冻结路径的改动吗？", context=context,
                choices=[dict(key="yes", label="放行", effect="照常往下走；卡上这些文件以后不再问"),
                         dict(key="no", label="不放行", effect="退回执行，改掉对冻结路径的改动")],
                recommend="no", reason="规则把这些路径列为冻结，改动要你单独授权", kind="approval")


def question(text, *, approval=False):
    text = text or "请补充下一步安排"
    return dict(subject=text[:30], decision=text[:42], context=text,
                choices=[dict(key="yes", label="批准" if approval else "继续", effect="按当前方案继续"),
                         dict(key="no", label="不批准" if approval else "调整", effect="保留当前阶段并调整方案")],
                recommend="yes", reason="依据已登记的证据和下一步安排", kind="approval" if approval else "reversible")


def notice(text):
    return dict(subject=(text or "车间通知")[:30], text=text, fyi=True)
