"""执行者阶梯：做不下来的活逐级换更强的执行者接着做，到顶仍不成才卡住（核心 R14、R16，ws#301）。

规则数据里的 `ladder` 是一个列表，每级 {members, takeover}；takeover 不写就是 members。
没有这一项时当作没配，一切照旧。只管写代码的轮：审查、验收不受指定的影响。
"""
from .jobs import Pending
from .models import stamp
from .ports import Retryable, Unavailable

# 换人的原因：一轮连续跑不成、同一关口连续没过、被指定的执行者挑选时没挑上、流程说这个执行者做不下来了。
CAUSES = ("failures", "gate", "quota", "returns")
FAILURE_LIMIT = 10
GATE_LIMIT, TAKEOVER_GATE_LIMIT = 3, 2
FIELDS = ("members", "takeover")


def validate(value, defined=None):
    """校验并补全阶梯；出错写明第几级、哪个执行者。defined 给了时，点到的执行者必须在里面。"""
    if not isinstance(value, list):
        raise ValueError("ladder 必须是列表")
    seen, result = {}, []
    for number, level in enumerate(value, 1):
        where = f"阶梯第 {number} 级"
        if not isinstance(level, dict):
            raise ValueError(f"{where}：必须是对象，写 members 和可选的 takeover")
        extra = sorted(str(k) for k in level if k not in FIELDS)
        if extra:
            raise ValueError(f"{where}：不认的字段 {'、'.join(extra)}；只认 members、takeover")
        members = level.get("members")
        if not names(members):
            raise ValueError(f"{where}：members 必须是不重复的执行者名列表")
        takeover = level.get("takeover", members)
        if not names(takeover):
            raise ValueError(f"{where}：takeover 必须是不重复的执行者名列表")
        for name in members:
            if defined is not None and name not in defined:
                raise ValueError(f"{where}：执行者 {name} 没有定义")
            if name in seen:
                raise ValueError(f"{where}：执行者 {name} 已在第 {seen[name]} 级")
            seen[name] = number
        for name in takeover:
            if name not in members:
                raise ValueError(f"{where}：接手者 {name} 不在本级 members 里")
        result.append({"members": list(members), "takeover": list(takeover)})
    return result


def names(value):
    return (isinstance(value, list) and bool(value) and all(isinstance(n, str) and n for n in value)
            and len(set(value)) == len(value))


def levels(data):
    """规则数据里的阶梯；没配时是空列表。"""
    return [{"members": list(level["members"]), "takeover": list(level.get("takeover", level["members"]))}
            for level in data.get("ladder") or []]


def position(table, name):
    """执行者在第几级（从 0 数）；不在阶梯里时 None。"""
    return next((index for index, level in enumerate(table) if name in level["members"]), None)


def writing(stage):
    """写代码的轮：审查、验收以外的阶段，和规则里 writer 角色的阶段一致。"""
    return stage not in ("审查", "验收")


def gate_limit(item):
    """同一关口连续没过的上限：起点执行者 3 次，阶梯指定的接手者 2 次。"""
    return TAKEOVER_GATE_LIMIT if item.get("takeover") else GATE_LIMIT


def find(engine, item, current, excluded=()):
    """从 current 所在级往上，取第一个 takeover 里有可用执行者的级。

    「可用」只看可用性检查和最近启动时报过不可用的记录；可用性检查可能异步，必须在写状态的事务之外调。
    找不到（不在阶梯里、已在最高级、上面都不可用）返回 None。
    """
    table = levels(engine.rules.data)
    start = position(table, current)
    if start is None:
        return None
    excluded = set(excluded) | set(engine.recently_unavailable(item))
    results, waiting = {}, None
    for level in table[start + 1:]:
        for name in level["takeover"]:
            if name in excluded or name in results:
                results.setdefault(name, False)
                continue
            try:
                engine.plugins.get("executor", name)
                results[name] = bool(engine.call("executor", name, "available", engine.rules.executor(name)))
            except (Unavailable, Retryable, LookupError):
                results[name] = False
            except Pending as exc:
                # 各级一起查，不一家一家排队等。
                waiting = waiting or exc
    if waiting:
        raise waiting
    skipped = []
    for index in range(start + 1, len(table)):
        chosen = next((n for n in table[index]["takeover"] if results.get(n)), None)
        if chosen:
            return {"from": current, "from_level": start + 1, "to": chosen, "to_level": index + 1, "skipped": skipped}
        skipped.append(index + 1)
    return None


def handover(engine, item, found, cause, reason):
    """记一条换人事实，返回活上要改的字段：指定接手者、两种计数归零、换人次数加一，接手者开新会话。"""
    if cause not in CAUSES:
        raise ValueError(f"换人原因只能是 {'、'.join(CAUSES)}")
    record = {**found, "cause": cause, "reason": reason}
    engine.fact("executor_handover", {key: record[key] for key in
                                      ("from", "from_level", "to", "to_level", "cause", "reason", "skipped")}, item["id"])
    about = f'阶梯换人：交给第 {found["to_level"]} 级的 {found["to"]} 接着做'
    # 同一个工作区和分支（活上的 workdir 不动），但不接回接手者以前在这件活上的会话。
    sessions = {name: session for name, session in item.get("sessions", {}).items() if name != found["to"]}
    return dict(takeover=found["to"], handovers=item.get("handovers", 0) + 1, failures=0, gate_count=0,
                retry_executor=None, retry_at=None, sessions=sessions, state="空闲", next=about, blocked_by="",
                leave={"kind": "工序", "owner": "车间", "at": stamp(engine.clock()), "about": about})
