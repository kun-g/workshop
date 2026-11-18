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


def touched(paths, prefixes):
    """前缀匹配，同 two_reviewers：路径以规则里某个前缀开头。"""
    return sorted({path for path in paths for prefix in prefixes if path.startswith(prefix)})


def overlaps(paths, prefixes):
    """登记的路径可以是目录前缀：和规则前缀互为前缀就算碰到。"""
    return any(path.startswith(prefix) or prefix.startswith(path) for path in paths for prefix in prefixes)


def validate_stages(stages):
    short = list(stages) == ["证据", "交付", "验收", "收尾"]
    if (len(set(stages)) != len(stages) or any(s not in STAGES for s in stages)
            or list(stages) != sorted(stages, key=STAGES.index)
            or not (short or {"接单", "执行", "收尾"}.issubset(stages))):
        raise ValueError("阶段必须有序且包含四道关口需要的阶段")


def initial_stage(target):
    stages = target.get('stages')
    if stages is None:
        return '接单'
    validate_stages(stages)
    return stages[0]


def allowed_advances(item: dict, stages) -> list[str]:
    """本阶段回执 advance 能填的阶段：下一段；证据到收尾可退回执行；到过验收的执行可直接回验收。"""
    current = item["stage"]
    # 下一段按全局顺序取规则里还在的那一个（当前阶段被规则去掉时同样认它之后的第一个），和关口判定一致。
    following = next((s for s in stages if STAGES.index(s) > STAGES.index(current)), None)
    allowed = [following] if following else []
    if current in ("证据", "审查", "交付", "验收", "收尾") and "执行" not in allowed:
        allowed.append("执行")
    if (current == "执行" and "验收" in stages and following != "验收"
            and any(entry.get("stage") == "验收" for entry in item.get("history", []))):
        allowed.append("验收")
    return allowed


def advance_problem(item: dict, advance, stages) -> str:
    """advance 不合法时给出具体原因（填了什么、能填什么）；合法或没填时返回空串。"""
    if advance is None or advance in allowed_advances(item, stages):
        return ""
    allowed = allowed_advances(item, stages)
    choices = "、".join(f"「{s}」" for s in allowed) or "（没有下一段，不填）"
    return f"advance 是「{advance}」；{item['stage']}之后只能是{choices}，不推进就不填"


def decide(item: dict, receipt: Receipt, evidence: Evidence, stages, rules: Rules) -> Decision:
    validate_stages(stages)
    unknown = set(evidence.actions) - set(rules.data["ask_first"]) - set(rules.data["skip_ask"])
    if unknown:
        raise ValueError(f"未登记的动作类别：{sorted(unknown)}")
    current = item["stage"]
    requested = receipt.advance
    if current == "验收" and not evidence.passed:
        return Decision("执行", True, "验收不过，退回执行")
    if requested == "执行" and current in ("证据", "审查", "交付", "验收", "收尾"):
        return Decision("执行", True, "退回执行")
    if requested in ("交付", "验收"):
        required = set(evidence.actions) & set(rules.data["ask_first"]) - set(rules.data["skip_ask"])
        verified = {r['action'] for r in evidence.authorized_actions
                    if r.get('request') and r.get('result') == 'approved' and r.get('applied') is True
                    and r.get('target') and r['target'] == evidence.action_targets.get(r['action'])}
        pending = tuple(sorted(required - verified))
        if pending:
            return Decision(current, False, "动作等待授权中心核实原申请和实际生效：" + "、".join(pending), pending)
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
        return Decision(current, False, "不能越过流程阶段和关口：" + advance_problem(item, requested, stages))
    checks = evidence.checks or item.get("checks", [])
    if requested == "执行" and not (checks and all(
            isinstance(argv, list) and argv and all(isinstance(v, str) and v.strip() for v in argv)
            for argv in checks)):
        return Decision(current, False, "还没有登记能跑的检查")
    if not evidence.passed:
        return Decision(current, False, evidence.reason or "本阶段证据未通过")
    if requested != "交付" and not ("交付" not in stages and requested == "验收"):
        return Decision(requested, True)
    return Decision(requested, True)
