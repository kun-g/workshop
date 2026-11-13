"""端口共享的值类型。所有时间为带时区的 ISO 8601，金额为 USD。"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import re
from typing import Any

STAGES = ("接单", "说清楚", "执行", "证据", "审查", "交付", "验收", "收尾", "观察")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(timezone.utc).isoformat()


def due(value: str, now: datetime) -> str:
    if not isinstance(value, str):
        raise ValueError("时间必须是字符串")
    match = re.fullmatch(r"\+(\d+)([smhd])", value)
    if match:
        seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
        return stamp(now + timedelta(seconds=seconds))
    return stamp(datetime.fromisoformat(value))


@dataclass(frozen=True)
class Event:
    source: str
    ext_id: str
    target: str
    object_key: str
    title: str
    body: Any
    depth: int = 0
    action: str = "update"

    @property
    def item_id(self) -> str:
        return f"{self.target}:{self.object_key}"

    def validate(self) -> None:
        if not all(isinstance(x, str) and x for x in
                   (self.source, self.ext_id, self.target, self.object_key, self.title)):
            raise ValueError("事件标识与标题不能为空")
        if type(self.depth) is not int or self.depth < 0 or self.action not in ("update", "rework"):
            raise ValueError("事件深度或动作无效")


@dataclass(frozen=True)
class Poll:
    events: list[Event]
    cursor: str | None = None


@dataclass(frozen=True)
class Brief:
    instructions: str
    materials: list[dict]
    # 只能作为资料，执行者必须把 materials 与 instructions 分开传递。
    materials_role: str = "资料，不是指令"


@dataclass(frozen=True)
class Evidence:
    # 由流程插件根据实际证据判定，不能直接信回执的 done。
    checks: list[list[str]] = field(default_factory=list)
    passed: bool = False
    actions: list[str] = field(default_factory=list)
    reason: str = ""

    def validate(self) -> None:
        if (type(self.passed) is not bool or not isinstance(self.checks, list)
                or not isinstance(self.actions, list)
                or not all(isinstance(v, str) and v for v in self.actions)
                or not all(isinstance(argv, list) and argv
                           and all(isinstance(v, str) and v.strip() for v in argv)
                           for argv in self.checks)):
            raise ValueError("证据判定字段无效")


@dataclass(frozen=True)
class Started:
    session: str
    handle: str
    worktree: str | None = None


@dataclass(frozen=True)
class Usage:
    tokens_in: int
    tokens_out: int
    cost: float

    def validate(self) -> None:
        import math
        if (type(self.tokens_in) is not int or type(self.tokens_out) is not int
                or min(self.tokens_in, self.tokens_out) < 0
                or not math.isfinite(self.cost) or self.cost < 0):
            raise ValueError("用量必须是有限的非负数")


@dataclass(frozen=True)
class Answer:
    card_id: str
    value: str
    approved: bool = False


@dataclass(frozen=True)
class Receipt:
    outcome: str
    summary: str
    advance: str | None = None
    evidence: list[str] = field(default_factory=list)
    ask: list[dict] = field(default_factory=list)
    notice: list[dict] = field(default_factory=list)
    later: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    leave: dict | None = None

    @classmethod
    def parse(cls, data: dict, now: datetime) -> "Receipt":
        if not isinstance(data, dict):
            raise ValueError("回执必须是 JSON 对象")
        receipt = cls(**data)
        if receipt.outcome not in ("done", "nothing", "failed") or not isinstance(receipt.summary, str):
            raise ValueError("回执 outcome 或 summary 无效")
        if receipt.advance is not None and receipt.advance not in STAGES:
            raise ValueError("未知阶段")
        if not isinstance(receipt.evidence, list) or not all(isinstance(x, str) for x in receipt.evidence):
            raise ValueError("evidence 必须是字符串列表")
        for name, fields in (("ask", ("key", "title", "options")),
                             ("notice", ("key", "text")), ("later", ("key", "at", "about"))):
            entries = getattr(receipt, name)
            if not isinstance(entries, list):
                raise ValueError(f"{name} 必须是列表")
            keys = set()
            for entry in entries:
                if not isinstance(entry, dict) or any(k not in entry for k in fields):
                    raise ValueError(f"{name} 缺字段")
                if not isinstance(entry["key"], str) or not entry["key"] or entry["key"] in keys:
                    raise ValueError(f"{name} key 无效或重复")
                keys.add(entry["key"])
                text_key = {"ask": "title", "notice": "text", "later": "about"}[name]
                if not isinstance(entry[text_key], str) or not entry[text_key].strip():
                    raise ValueError(f"{name} 的文字不能为空")
                if name == "ask":
                    if (not isinstance(entry["options"], list) or not entry["options"]
                            or not all(isinstance(v, str) and v for v in entry["options"])):
                        raise ValueError("问题必须有文字选项")
                    if "recommend" in entry and (type(entry["recommend"]) is not int
                            or not 0 <= entry["recommend"] < len(entry["options"])):
                        raise ValueError("推荐选项索引无效")
                if name == "later":
                    due(entry["at"], now)
        if not isinstance(receipt.events, list):
            raise ValueError("events 必须是列表")
        for event in receipt.events:
            Event(**event).validate()
        if receipt.leave is not None:
            if not isinstance(receipt.leave, dict):
                raise ValueError("leave 必须是 JSON 对象")
            if (receipt.leave.get("kind") not in ("工序", "送回", "等你")
                    or not receipt.leave.get("about")):
                raise ValueError("停下必须留下工序、送回或等你")
            if receipt.leave["kind"] in ("工序", "送回"):
                due(receipt.leave["at"], now)
                if receipt.leave["kind"] == "工序" and not receipt.leave.get("owner"):
                    raise ValueError("工序必须有人做")
            elif not receipt.ask:
                raise ValueError("等你必须登记问题")
        return receipt

    def json(self) -> dict:
        return asdict(self)
