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


def validate_question(data):
    for name, limit in (("subject", 30), ("decision", 42), ("context", None), ("reason", 60)):
        text_field(data, name, limit)
    choices = data.get("choices")
    if not isinstance(choices, list) or not 2 <= len(choices) <= 3:
        raise ValueError("问题须有 2–3 个选项")
    keys = set()
    for choice in choices:
        for name, limit in (("key", 18), ("label", 18), ("effect", 60)):
            text_field(choice, name, limit)
        if choice["key"] in keys:
            raise ValueError("选项 key 重复")
        keys.add(choice["key"])
    if data.get("recommend") not in keys or data.get("kind") not in ("approval", "reversible"):
        raise ValueError("推荐选项或问题 kind 无效")


def validate_notice(data):
    text_field(data, "subject", 30)
    text_field(data, "text")
    if data.get("fyi") is not True:
        raise ValueError("通知必须 fyi=true")


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
