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
    if not requested:
        complete = current == stages[-1] and receipt.outcome == "done" and evidence.passed
        return Decision(current, complete, "完成" if complete else "未提议下一阶段", complete=complete)
    index = stages.index(current)
    if index + 1 >= len(stages) or requested != stages[index + 1]:
        return Decision(current, False, "不能越过流程阶段和关口")
    checks = evidence.checks or item.get("checks", [])
    if requested == "执行" and not (checks and all(
            isinstance(argv, list) and argv and all(isinstance(v, str) and v.strip() for v in argv)
            for argv in checks)):
        return Decision(current, False, "说清楚阶段尚未写出能跑的检查")
    if current in ("证据", "审查", "验收") and not evidence.passed:
        return Decision("执行", True, "证据未通过，退回执行")
    if requested == "交付" or ("交付" not in stages and requested == "验收"):
        pending = tuple(sorted(set(evidence.actions) & set(rules.data["ask_first"])
                               - set(rules.data["skip_ask"]) - set(item.get("approved", []))))
        if pending:
            return Decision(current, False, "交付动作等待授权", pending)
    return Decision(requested, True)
