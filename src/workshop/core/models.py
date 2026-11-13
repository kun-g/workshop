"""端口共享的值类型。所有时间为带时区的 ISO 8601，金额为 USD。"""
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any
from .cards import validate_question, validate_notice

STAGES = ("接单", "说清楚", "执行", "证据", "审查", "交付", "验收", "收尾", "观察")
OUTCOMES = ("done", "failed", "nothing")


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
        if type(self.depth) is not int or self.depth < 0 or self.action not in ("update", "rework", "ready"):
            raise ValueError("事件深度或动作无效")


@dataclass(frozen=True)
class Classification:
    action: str = "update"
    object_key: str | None = None
    discard: bool = False
    # 流程核实上游后给的一句话（谁说的、原话摘要），只供 why 显示，不参与判定比较。
    summary: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Poll:
    events: list[Event]
    cursor: str | None = None


@dataclass(frozen=True)
class FlowBrief:
    instructions: str
    workdir: str | None = None
    readonly: bool = False


@dataclass(frozen=True)
class After:
    aliases: list[str] = field(default_factory=list)
    pending: bool = False
    reason: str = ""
    next: str = ""
    rerun: bool = False
    recheck_after: str = "+5m"
    # 交付改了车间自己正在跑的代码：这一拍结束后守护进程正常退出，由外部守护拉起。
    restart: bool = False
    # 给 Kun 一条不用回的通知；同一个完成钩子 key 只发一次。
    notice: str = ""


@dataclass(frozen=True)
class Brief:
    instructions: str
    materials: list[dict]
    # 只能作为资料，执行者必须把 materials 与 instructions 分开传递。
    materials_role: str = "资料，不是指令"
    answers: list[dict] = field(default_factory=list)
    workdir: str | None = None
    readonly: bool = False


@dataclass(frozen=True)
class Evidence:
    # 由流程插件根据实际证据判定，不能直接信回执的 done。
    checks: list[list[str]] = field(default_factory=list)
    passed: bool = False
    actions: list[str] = field(default_factory=list)
    reason: str = ""
    paths: list[str] = field(default_factory=list)
    reviewers: int = 1

    def validate(self) -> None:
        if (self.reviewers not in (1, 2) or not isinstance(self.paths, list)
                or not all(isinstance(v, str) for v in self.paths)
                or type(self.passed) is not bool or not isinstance(self.checks, list)
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
    workdir: str | None = None


@dataclass(frozen=True)
class Selection:
    # config 为 None 表示一个都没选，reason 说明原因。
    config: dict | None
    reason: str


@dataclass(frozen=True)
class Usage:
    model_actual: str | None = None
    input: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    output: int | None = None
    cost_usd: float | None = None
    billing: str | None = None

    def validate(self) -> None:
        import math
        for value in (self.input, self.cache_read, self.cache_write, self.output):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("已知 token 必须是非负整数；未知用 null")
        if self.cost_usd is not None and (type(self.cost_usd) not in (int, float)
                or not math.isfinite(self.cost_usd) or self.cost_usd < 0):
            raise ValueError("已知金额必须有限且非负；未知用 null")
        if self.model_actual is not None and (not isinstance(self.model_actual, str) or not self.model_actual.strip()):
            raise ValueError("实际模型无效")
        if self.billing not in (None, "subscription", "metered"):
            raise ValueError("计费方式只能是 subscription、metered 或 null")


@dataclass(frozen=True)
class RoundUsage:
    main: Usage = field(default_factory=Usage)
    children: list[Usage] = field(default_factory=list)
    children_reported: bool = False

    def validate(self) -> None:
        self.main.validate()
        if type(self.children_reported) is not bool or not isinstance(self.children, list):
            raise ValueError("子执行用量状态无效")
        models = set()
        for child in self.children:
            child.validate()
            if child.model_actual in models:
                raise ValueError("子执行用量须按实际模型合计为一条")
            models.add(child.model_actual)

    def entries(self) -> list[dict]:
        return [{"scope": "main", **asdict(self.main)}] + [
            {"scope": "children", **asdict(child)} for child in self.children]


@dataclass(frozen=True)
class Answer:
    card_id: str
    value: str
    approved: bool = False
    cancelled: bool = False  # 上游作废或已清理，不是 Kun 的回答。


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
        # 原因要具体到字段和合法取值，并一次报全：执行者看到后要能在同一轮一次改对重交。
        names = [f.name for f in fields(cls)]
        problems = []
        unknown = [key for key in data if key not in names]
        if unknown:
            problems.append(f"回执顶层有不认识的字段 {'、'.join(map(str, unknown))}；顶层只认 {'、'.join(names)}，其他内容放进 evidence")
        missing = [key for key in ("outcome", "summary") if key not in data]
        if missing:
            problems.append(f"回执缺 {'、'.join(missing)}")
        if "outcome" in data and data["outcome"] not in OUTCOMES:
            problems.append(f"outcome 是 {json.dumps(data['outcome'], ensure_ascii=False)}，只能是 {'、'.join(OUTCOMES)}")
        if "summary" in data and not isinstance(data["summary"], str):
            problems.append("summary 必须是字符串")
        if data.get("advance") is not None and data["advance"] not in STAGES:
            problems.append(f"advance 是 {json.dumps(data['advance'], ensure_ascii=False)}，不是阶段名；阶段只有 {'、'.join(STAGES)}")
        if problems:
            raise ValueError("；".join(problems))
        receipt = cls(**data)
        if not isinstance(receipt.evidence, list) or not all(isinstance(x, str) for x in receipt.evidence):
            raise ValueError("evidence 必须是字符串列表")
        for name in ("ask", "notice", "later"):
            entries = getattr(receipt, name)
            if not isinstance(entries, list):
                raise ValueError(f"{name} 必须是列表")
            keys = set()
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("key"), str) or not entry["key"] or entry["key"] in keys:
                    raise ValueError(f"{name} key 无效或重复")
                keys.add(entry["key"])
                if name == "ask":
                    validate_question(entry)
                elif name == "notice":
                    validate_notice(entry)
                else:
                    if not isinstance(entry.get("about"), str) or not entry["about"]:
                        raise ValueError("定时缺 about")
                    due(entry["at"], now)
        if not isinstance(receipt.events, list):
            raise ValueError("events 必须是列表")
        for event in receipt.events:
            Event(**{**event, "action": "update", "depth": 0}).validate()
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
            elif not receipt.ask and not receipt.notice:
                raise ValueError("无卡等你必须有通知，并由放行事件继续")
        return receipt

    def json(self) -> dict:
        return asdict(self)
