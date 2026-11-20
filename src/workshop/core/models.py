"""端口共享的值类型。所有时间为带时区的 ISO 8601，金额为 USD。"""
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

STAGES = ("接单", "说清楚", "执行", "证据", "审查", "交付", "验收", "收尾", "观察")
OUTCOMES = ("done", "failed", "nothing")
DEFAULT_BRIEF_MAX_CHARS = 524288


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


# 合批窗口上限：窗口再加一拍处理也要落在核心 R27 的 1 秒之内。
BATCH_WINDOW_MAX_MS = 500


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
    flow: str = ""
    flow_default: bool = False
    action_id: str = ""
    previous_ids: list[str] = field(default_factory=list)
    # 核心 R33：来源可把合批窗口设为 0（立即取），不设用规则的默认窗口；不可攒批的事件单独一轮。
    batch_window_ms: int | None = None
    batchable: bool = True
    # 核心 R37：状态覆盖类变更（开关、负责人等）带上覆盖的字段和对象版本，版本落后于活上已记的就是陈旧，
    # 只留痕、不回拨、不进批次；增量类变更（新评论等）不填，按自己的标识和到达顺序处理。
    # version 是能按字符串先后比较的版本（如 ISO 时间）。snapshot 表示这是来源当下读到的快照：
    # 和已记的同键撞上时，以它校正活上记的状态（同一秒的重复变化会撞键）。
    version: str | None = None
    overwrite: dict | None = None
    snapshot: bool = False

    @property
    def item_id(self) -> str:
        prefix = f"{self.target}:{self.flow}" if self.flow and not self.flow_default else self.target
        return f"{prefix}:{self.object_key}"

    def validate(self) -> None:
        if not all(isinstance(x, str) and x for x in
                   (self.source, self.ext_id, self.target, self.object_key, self.title)):
            raise ValueError("事件标识与标题不能为空")
        if (not isinstance(self.flow, str) or ":" in self.flow or type(self.flow_default) is not bool
                or not isinstance(self.action_id, str)):
            raise ValueError("事件流程无效")
        if not isinstance(self.previous_ids, list) or not all(isinstance(v, str) and v for v in self.previous_ids):
            raise ValueError("旧事件标识须为字符串列表")
        if type(self.depth) is not int or self.depth < 0 or self.action not in ("update", "rework", "ready"):
            raise ValueError("事件深度或动作无效")
        if (self.batch_window_ms is not None and not (type(self.batch_window_ms) is int and 0 <= self.batch_window_ms <= BATCH_WINDOW_MAX_MS)
                or type(self.batchable) is not bool):
            raise ValueError("合批窗口须为 0 到 500 毫秒的整数，能否攒批须为布尔值")
        if (self.version is not None and (not isinstance(self.version, str) or not self.version)
                or self.overwrite is not None and (not isinstance(self.overwrite, dict) or self.version is None)
                or type(self.snapshot) is not bool):
            raise ValueError("状态覆盖须同时带非空版本串，快照标记须为布尔值")


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
    normalized: bool = False


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Due:
    """一次到点扫描的结论（核心 R34）。

    slot 是不晚于 now 的最近一个槽位，锚定槽位时刻、不用 now：停机后补跑、半程崩溃重扫，算出的都是同一个。
    时刻一律写成当地时间带时区偏移的 ISO 8601，夏令时回拨时两个 01:30 也各有唯一表示，可直接做去重标识。
    """
    slot: str
    due: bool       # 下一次时刻已到（没有记过下一次的算到）
    missed: int     # 从下一次时刻到 slot 共到点几次；没到为 0
    next_at: str    # slot 之后的下一个时刻

    def ext_id(self, prefix=""):
        return f"{prefix}:{self.slot}" if prefix else self.slot


@dataclass(frozen=True)
class Cadence:
    """周期：每 N 分钟（沿 UTC epoch 切）、每天或每周的当地时刻。定时发版和用户周期任务共用这一份扫描。"""
    every: str                  # minutes、daily、weekly
    minutes: int = 0
    at: str = ""                # 当地时刻 HH:MM
    weekday: int = 0            # 每周：0 是周一
    zone: str = "Asia/Shanghai"

    def __post_init__(self):
        if self.every not in ("minutes", "daily", "weekly"):
            raise ValueError("周期只支持 minutes、daily、weekly")
        if self.every == "minutes":
            if type(self.minutes) is not int or self.minutes <= 0:
                raise ValueError("每 N 分钟的 N 须为正整数")
        elif not isinstance(self.at, str) or not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", self.at):
            raise ValueError("时刻须为 HH:MM（00:00 到 23:59）")
        if type(self.weekday) is not int or not 0 <= self.weekday <= 6:
            raise ValueError("星期须为 0（周一）到 6（周日）")
        try:
            if not isinstance(self.zone, str) or not self.zone:
                raise ValueError(self.zone)
            ZoneInfo(self.zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"未知时区：{self.zone!r}") from exc

    def tz(self):
        return ZoneInfo(self.zone)

    def key(self, moment: datetime) -> str:
        return moment.astimezone(self.tz()).isoformat()

    def local(self, day) -> datetime:
        """当地某天的这个时刻，换成 UTC。回拨重复的取第一次；跳时跳过的取跳过之后的第一个时刻。"""
        hour, minute = map(int, self.at.split(":"))
        wall = datetime(day.year, day.month, day.day, hour, minute)
        zone = self.tz()
        first = wall.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
        if first.astimezone(zone).replace(tzinfo=None) == wall:
            return first
        moment = min(first, wall.replace(tzinfo=zone, fold=1).astimezone(timezone.utc))
        while moment.astimezone(zone).replace(tzinfo=None) < wall:
            moment += timedelta(minutes=1)
        return moment

    def nearby(self, moment: datetime):
        day = moment.astimezone(self.tz()).date()
        days = (day + timedelta(days=n) for n in range(-8, 9))
        return [self.local(d) for d in days if self.every == "daily" or d.weekday() == self.weekday]

    def floor(self, moment: datetime) -> datetime:
        """不晚于 moment 的最近一个槽位。"""
        if self.every == "minutes":
            elapsed = (moment.astimezone(timezone.utc) - EPOCH) // timedelta(minutes=1)
            return EPOCH + timedelta(minutes=elapsed // self.minutes * self.minutes)
        return max(slot for slot in self.nearby(moment) if slot <= moment)

    def after(self, moment: datetime) -> datetime:
        """晚于 moment 的第一个槽位。"""
        if self.every == "minutes":
            return self.floor(moment) + timedelta(minutes=self.minutes)
        return min(slot for slot in self.nearby(moment) if slot > moment)

    def scan(self, now: datetime, next_at=None, *, last=None) -> Due:
        """扫到点、算下一次：next_at 是记下的下一次时刻；只记了上一次槽位的传 last。读不出的时刻当没记过。"""
        def moment(value):
            try:
                parsed = datetime.fromisoformat(value) if isinstance(value, str) and value else None
            except ValueError:
                return None
            return parsed if parsed and parsed.tzinfo else None
        start = moment(next_at)
        if start is None and moment(last) is not None:
            start = self.after(moment(last))
        slot = self.floor(now)
        due = start is None or start <= now
        if not due:
            missed = 0
        elif start is None or start > slot:
            missed = 1
        elif self.every == "minutes":
            missed = (slot - start) // timedelta(minutes=self.minutes) + 1
        else:
            days = (slot.astimezone(self.tz()).date() - start.astimezone(self.tz()).date()).days
            missed = days // (1 if self.every == "daily" else 7) + 1
        return Due(self.key(slot), due, missed, self.key(self.after(slot)))


@dataclass(frozen=True)
class Relations:
    """来源确认的完整关系快照；错误不等于没有关系。标识可跨交付对象。"""
    prerequisites: list[dict] = field(default_factory=list)
    members: list[dict] = field(default_factory=list)
    collections: list[dict] = field(default_factory=list)
    related: list[dict] = field(default_factory=list)
    converge: str = ""
    error: str = ""
    priority: str = "P1"

    def validate(self):
        if self.priority not in ("P0", "P1", "P2"):
            raise ValueError("优先级只能是 P0/P1/P2")
        if not isinstance(self.error, str) or not isinstance(self.converge, str):
            raise ValueError("关系的错误和汇合阶段须为字符串")
        for group in (self.prerequisites, self.members, self.collections, self.related):
            if not isinstance(group, list):
                raise ValueError("关系须为列表")
            seen = set()
            for entry in group:
                if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                        or not entry["id"] or entry["id"] in seen
                        or not isinstance(entry.get("name"), str) or not entry["name"]
                        or type(entry.get("complete")) is not bool):
                    raise ValueError("关系对象缺少唯一标识、显示名或完成状态")
                seen.add(entry["id"])
        if self.members and self.converge not in STAGES:
            raise ValueError("总单须指定合法的汇合阶段")


@dataclass(frozen=True)
class FlowBrief:
    instructions: str
    workdir: str | None = None
    readonly: bool = False
    # 流程补充的外部文字只能作为资料，不能成为说明或人的回答。
    materials: list[dict] = field(default_factory=list)


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
    # 流程已幂等消费的恢复计划，核心追加事实供查询。
    recovery_id: str = ""
    # 回查已经确定不能继续时，留下原因，不再自动等待或推进。
    blocked: str = ""
    # 流程已持久创建的引用；与 pending=True 一起返回，恢复后重查完成钩子。
    wait: dict | None = None
    # 同目标的其它活完成时提前回查；只唤醒，仍由流程重新核验。
    wake_on_completed: bool = False

    @classmethod
    def stored(cls, data: dict) -> "After":
        """读回已存的结果：旧版本存下的 notice 已取消（介入点 R13），去掉。"""
        return cls(**{k: v for k, v in data.items() if k != "notice"})


@dataclass(frozen=True)
class Release:
    """执行者收回一件已收尾的活的工作目录的结果；released=False 时 reason 写为什么留着。"""
    released: bool
    reason: str = ""


@dataclass(frozen=True)
class Brief:
    instructions: str
    materials: list[dict]
    # 只能作为资料，执行者必须把 materials 与 instructions 分开传递。
    materials_role: str = "资料，不是指令"
    answers: list[dict] = field(default_factory=list)
    workdir: str | None = None
    readonly: bool = False
    # 核心 R33：这一轮带上的事件序号，按到达顺序；正文在 materials 里按 seq 对应。
    batch: list[int] = field(default_factory=list)
    # 记忆 R36：本轮开头按作用域加载的固定部分（用户指示、未结案进度、笔记索引、近期告知）。
    memory: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Evidence:
    # 由流程插件根据实际证据判定，不能直接信回执的 done。
    checks: list[list[str]] = field(default_factory=list)
    passed: bool = False
    actions: list[str] = field(default_factory=list)
    reason: str = ""
    paths: list[str] = field(default_factory=list)
    reviewers: int = 1
    # 流程确认结果已完成时给出的标识；判过并向前推进就可发完成通知，同一标识只发一张。
    # 空时只在最后阶段完成后发，一件活只发一张。
    done_key: str = ""
    blocked: str = ""
    next: str = ""
    # 无下一步的回执，经流程核实仍需等待；到点调用 after，完成后重跑本阶段。
    recheck_after: str = ""
    superseded: bool = False  # 来源事实已变；结束旧轮并重新安排，不算验收拒绝。
    # 交接由流程按外部看得到的状态判定：非空时代替回执的 advance（只能是下一阶段或退回执行）。
    handoff: str = ""
    # 没过且要等来源资料更新：活停在本阶段「等资料」，不派轮、不发卡；来源有新事件时下一拍重核。
    awaits_update: bool = False
    # 流程核实本轮需创建外部等待；调用 after 取得引用，回答后仍回本阶段。
    pause: bool = False
    action_targets: dict[str, str] = field(default_factory=dict)
    authorized_actions: list[dict] = field(default_factory=list)
    # 流程判定当前执行者做不下来了：和 blocked 一起给（原因和建议动作照 blocked），handoff 是接手者接着做的阶段。
    # 阶梯上有可换的，核心换人、不记卡住；没有可换的，按 blocked 记卡住。旧记录没有这一项，读回是 False。
    unable: bool = False

    def validate(self) -> None:
        if not isinstance(self.action_targets, dict) or not all(
                isinstance(k, str) and k and isinstance(v, str) and v for k, v in self.action_targets.items()):
            raise ValueError("动作目标须为非空字符串映射")
        if not isinstance(self.authorized_actions, list):
            raise ValueError("授权核实须为列表")
        for record in self.authorized_actions:
            if (not isinstance(record, dict) or not all(isinstance(record.get(k), str) and record[k]
                    for k in ('action', 'target', 'request', 'result')) or type(record.get('applied')) is not bool):
                raise ValueError("授权核实缺动作、目标、原申请、结果或生效状态")
        if type(self.unable) is not bool or self.unable and (
                self.passed or self.pause or not self.blocked or not self.handoff):
            raise ValueError("做不下来须同时给阻塞和交接阶段，不能同时通过或暂停")
        if type(self.pause) is not bool or self.pause and (
                self.passed or self.blocked or self.handoff or self.recheck_after or self.awaits_update):
            raise ValueError("暂停不能同时通过、阻塞、交接或另设等待")
        if not isinstance(self.handoff, str) or self.handoff and self.handoff not in STAGES:
            raise ValueError("交接阶段须为合法阶段名")
        if type(self.awaits_update) is not bool or self.awaits_update and (
                self.passed or self.blocked or self.recheck_after or self.handoff or not self.reason.strip()):
            raise ValueError("等资料须有原因，不能同时通过、阻塞、定时回查或交接")
        if type(self.superseded) is not bool or self.superseded and (
                self.passed or self.blocked or self.recheck_after or not self.reason):
            raise ValueError("过期证据须有原因，不能同时通过、阻塞或定时回查")
        if (not isinstance(self.recheck_after, str) or self.recheck_after and
                (self.blocked or self.passed or not isinstance(self.reason, str) or not self.reason.strip()
                 or not isinstance(self.next, str) or not self.next.strip())):
            raise ValueError("定时回查须有原因和下一步，不能同时通过或阻塞")
        if self.recheck_after:
            due(self.recheck_after, utcnow())
        if not isinstance(self.blocked, str) or not isinstance(self.next, str) or self.blocked and not self.next.strip():
            raise ValueError("阻塞必须有原因和建议动作")
        if (self.reviewers not in (1, 2) or not isinstance(self.paths, list)
                or not isinstance(self.done_key, str)
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


# 介入点迁移（#176）前的回执有 ask、notice 两个字段；已存的旧回执里它们是空列表，读回时去掉。
RETIRED_FIELDS = ("ask", "notice")
RETIRED_HINT = "ask、notice 已取消：要 Kun 取舍按流程说明登记等待，进度和结果不发通知"


class ReceiptError(ValueError):
    def __init__(self, problems):
        super().__init__("；".join(problems))


@dataclass(frozen=True)
class Receipt:
    outcome: str
    summary: str
    advance: str | None = None
    evidence: list[str] = field(default_factory=list)
    later: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    leave: dict | None = None

    @classmethod
    def stored(cls, data: dict) -> "Receipt":
        """读回已经收下的回执：旧版本存下的 ask、notice 不再有去处，去掉。"""
        return cls(**{k: v for k, v in data.items() if k not in RETIRED_FIELDS})

    @classmethod
    def parse(cls, data: dict, now: datetime) -> "Receipt":
        if not isinstance(data, dict):
            raise ValueError("回执必须是 JSON 对象")
        # 原因要具体到字段和合法取值，并一次报全：执行者看到后要能在同一轮一次改对重交。
        names = [f.name for f in fields(cls)]
        problems = []
        retired = [key for key in RETIRED_FIELDS if data.get(key)]
        if retired:
            problems.append(RETIRED_HINT)
        data = {k: v for k, v in data.items() if k not in RETIRED_FIELDS}
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
        # 校验原始字段，最后才构造对象；顶层缺字段不妨碍收集其余错误。
        def check(path, operation):
            try:
                operation()
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                problems.extend(f"{path}: {part}" for part in str(exc).split("；"))

        evidence = data.get("evidence", [])
        if not isinstance(evidence, list):
            problems.append("evidence 必须是字符串列表")
        else:
            for i, value in enumerate(evidence):
                if not isinstance(value, str):
                    problems.append(f"evidence[{i}] 必须是字符串")
        for name in ("later",):
            entries = data.get(name, [])
            if not isinstance(entries, list):
                problems.append(f"{name} 必须是列表")
                continue
            keys = set()
            for i, entry in enumerate(entries):
                path = f"{name}[{i}]"
                if not isinstance(entry, dict):
                    problems.append(f"{path} 必须是 JSON 对象")
                    continue
                key = entry.get("key")
                if not isinstance(key, str) or not key.strip() or key in keys:
                    problems.append(f"{path}: {name} key 无效或重复")
                else:
                    keys.add(key)
                if not isinstance(entry.get("about"), str) or not entry["about"]:
                    problems.append(f"{path}.about: 定时缺 about")
                check(f"{path}.at", lambda: due(entry.get("at"), now))
        events = data.get("events", [])
        if not isinstance(events, list):
            problems.append("events 必须是列表")
        else:
            for i, event in enumerate(events):
                if not isinstance(event, dict):
                    problems.append(f"events[{i}] 必须是 JSON 对象")
                    continue
                check(f"events[{i}]", lambda: Event(**{**event, "action": "update", "depth": 0}).validate())
        leave = data.get("leave")
        if leave is not None:
            if not isinstance(leave, dict):
                problems.append("leave 必须是 JSON 对象")
            else:
                if leave.get("kind") not in ("工序", "送回", "等你") or not leave.get("about"):
                    problems.append("leave.kind/about: 停下必须留下工序、送回或等你")
                if leave.get("kind") in ("工序", "送回"):
                    check("leave.at", lambda: due(leave.get("at"), now))
                    if leave["kind"] == "工序" and not leave.get("owner"):
                        problems.append("leave.owner: 工序必须有人做")
                # 无卡等待内容由流程核实并交等待服务，核心不解释业务问题。
        if problems:
            raise ReceiptError(problems)
        return cls(**data)

    @staticmethod
    def format_help(evidence: str = "证据字符串") -> str:
        """通用回执协议；流程可提供一条自己的证据示例。"""
        minimal = {"outcome": "done", "summary": "本轮结果", "evidence": [evidence]}
        return (
            "回执字段类型：outcome、summary 必填且为字符串；outcome 只能是 done、failed、nothing。"
            "其余可省略：advance 为阶段字符串或 null（取值按本轮说明）；evidence 为字符串列表；"
            "later、events 为对象列表；leave 为对象或 null。"
            "later 每项须有非空字符串 key，同一列表内不重复，还须有字符串 at、about；"
            "events 每项须有 source/ext_id/target/object_key/title 字符串及 body。"
            "leave 须有 kind（工序/送回/等你）、about；工序/送回还须有 at，工序还须有 owner；到点车间接着做，不找 Kun。"
            "回执不发卡、不发通知：要 Kun 取舍按流程说明登记等待，进度和结果 Kun 在 workshop status 里看。\n"
            "示例只展示格式；summary、evidence 须替换为本轮实际结果，证据内容按流程说明填写。\n"
            "最小回执 JSON 示例：\n```json\n" + json.dumps(minimal, ensure_ascii=False) + "\n```\n"
        )

    def json(self) -> dict:
        return asdict(self)
