"""通道的严格卡片格式；限制以 Unicode 字符计。"""
from typing import Literal, TypedDict
import json


QUESTION_TEXT = (("subject", 30), ("decision", 42), ("context", None), ("reason", 60))
CHOICE_TEXT = (("key", 18), ("label", 18), ("effect", 60))
QUESTION_KINDS = ("approval", "reversible")


def question_rules():
    def fields(specs):
        return "、".join(f"{name} 为非空字符串" + (f"且≤{limit}字符" if limit else "（无长度上限）")
                        for name, limit in specs)
    return ("ask 每项的外层 key 为非空字符串且同列表唯一；" + fields(QUESTION_TEXT) +
            "；choices 为 2–3 个 JSON 对象，每项 " + fields(CHOICE_TEXT) +
            "，选项 key 不能重复；recommend 必须等于一个选项的 key；kind 只能为 " +
            "/".join(QUESTION_KINDS) + "；长度按 Unicode 字符计。")


def question_example(key="question", *, context="本轮已有证据，请据此决定是否继续。", approval=False):
    card = {"key": key, **question(context, approval=approval)}
    validate_question(card)
    return "```json\n" + json.dumps({"ask": [card]}, ensure_ascii=False) + "\n```\n"


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
    problems = text_problems(data, QUESTION_TEXT)
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
            problems.extend(text_problems(choice, CHOICE_TEXT, f"choices[{i}]: "))
            key = choice.get("key")
            if isinstance(key, str):
                if key in keys:
                    problems.append(f"choices[{i}]: 选项 key 重复")
                keys.add(key)
    if not isinstance(data.get("recommend"), str) or data["recommend"] not in keys:
        problems.append("recommend: 推荐选项无效")
    if data.get("kind") not in QUESTION_KINDS:
        problems.append("kind: 问题 kind 无效")
    if problems:
        raise ValueError("；".join(problems) + "；完整问卡要求：" + question_rules())


def validate_notice(data):
    problems = text_problems(data, (("subject", 30), ("text", None)))
    if data.get("fyi") is not True:
        problems.append("fyi: 通知必须 fyi=true")
    if problems:
        raise ValueError("；".join(problems))


def question(text, *, approval=False):
    text = text or "请补充下一步安排"
    return dict(subject=text[:30], decision=text[:42], context=text,
                choices=[dict(key="yes", label="批准" if approval else "继续", effect="按当前方案继续"),
                         dict(key="no", label="不批准" if approval else "调整", effect="保留当前阶段并调整方案")],
                recommend="yes", reason="依据已登记的证据和下一步安排", kind="approval" if approval else "reversible")


def notice(text):
    return dict(subject=(text or "车间通知")[:30], text=text, fyi=True)
