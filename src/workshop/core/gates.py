"""关口仅由核心判定，流程只能提供阶段与证据。"""
from dataclasses import dataclass
from .models import Evidence, Receipt, STAGES
from .rules import Rules


@dataclass(frozen=True)
class Decision:
    stage: str
    allowed: bool
    reason: str = ""
    needs_approval: tuple[str, ...] = ()
    complete: bool = False
    # 碰到规则冻结路径、Kun 还没放行的文件；非空时停下发卡问放不放行。
    frozen: tuple[str, ...] = ()


def touched(paths, prefixes):
    """前缀匹配，同 two_reviewers：路径以规则里某个前缀开头。"""
    return sorted({path for path in paths for prefix in prefixes if path.startswith(prefix)})


def overlaps(paths, prefixes):
    """登记的路径可以是目录前缀：和规则前缀互为前缀就算碰到。"""
    return any(path.startswith(prefix) or prefix.startswith(path) for path in paths for prefix in prefixes)


def validate_stages(stages):
    if (len(set(stages)) != len(stages) or any(s not in STAGES for s in stages)
            or list(stages) != sorted(stages, key=STAGES.index)
            or not {"接单", "说清楚", "执行", "验收", "收尾"}.issubset(stages)):
        raise ValueError("阶段必须有序且包含四道关口需要的阶段")


def decide(item: dict, receipt: Receipt, evidence: Evidence, stages, rules: Rules) -> Decision:
    validate_stages(stages)
    unknown = set(evidence.actions) - set(rules.data["ask_first"]) - set(rules.data["skip_ask"])
    if unknown:
        raise ValueError(f"未登记的动作类别：{sorted(unknown)}")
    current = item["stage"]
    requested = receipt.advance
    if current == "验收" and not evidence.passed:
        return Decision("执行", True, "验收不过，退回执行")
    if requested == "执行" and current in ("证据", "审查", "交付", "验收"):
        return Decision("执行", True, "退回执行")
    if (current == "执行" and requested == "验收" and stages.index("验收") > stages.index("执行") + 1
            and any(entry.get("stage") == "验收" for entry in item.get("history", []))):
        # 到过验收又退回执行，执行确认已交付的结果不用改：直接回验收重走。
        # 中间几道关口这份结果都过过；结果没变、能走几次由流程证据判。
        if not evidence.passed:
            return Decision(current, False, evidence.reason or "本阶段证据未通过")
        return Decision("验收", True, "执行确认已交付的结果不用改，回到验收重走")
    if not requested:
        complete = current == stages[-1] and receipt.outcome == "done" and evidence.passed
        return Decision(current, complete, "完成" if complete else "未提议下一阶段", complete=complete)
    # 下一阶段按全局顺序取规则里还在的那一个；当前阶段被规则去掉时（例如关掉审查后在跑的那一轮），同样认它之后的第一个。
    following = next((s for s in stages if STAGES.index(s) > STAGES.index(current)), None)
    if following is None or requested != following:
        return Decision(current, False, "不能越过流程阶段和关口")
    checks = evidence.checks or item.get("checks", [])
    if requested == "执行" and not (checks and all(
            isinstance(argv, list) and argv and all(isinstance(v, str) and v.strip() for v in argv)
            for argv in checks)):
        return Decision(current, False, "说清楚阶段尚未写出能跑的检查")
    if not evidence.passed:
        return Decision(current, False, evidence.reason or "本阶段证据未通过")
    frozen = ()
    if STAGES.index(current) > STAGES.index("执行"):
        # 执行之后按实际改动查冻结路径；放行过的文件不再问，退回执行后碰到新的冻结文件再问。
        prefixes = rules.data["targets"].get(item.get("target"), {}).get("frozen_paths", [])
        frozen = tuple(p for p in touched(evidence.paths, prefixes) if p not in item.get("frozen_approved", []))
    pending = ()
    if requested == "交付" or ("交付" not in stages and requested == "验收"):
        pending = tuple(sorted(set(evidence.actions) & set(rules.data["ask_first"])
                               - set(rules.data["skip_ask"]) - set(item.get("approved", []))))
    if frozen or pending:
        # 两道同时要批时合成一张卡：批一次两样都放行，不批退回执行。
        return Decision(current, False, "改动碰到冻结路径，等 Kun 放行" if frozen else "交付动作等待授权", pending, frozen=frozen)
    return Decision(requested, True)
