"""按执行者配置档对比（核心 R10、R24）：一件活算在跑它执行阶段的配置档头上。

只读：轮次表定配置档、开工时刻、用时和花费；每件活的事实定进交付、退回和收尾后的「不对」。
"""
from datetime import datetime
import json


WRITE = "执行"
DELIVERY = "交付"
CLOSING = "收尾"
SWITCHED = "换过执行者"
# 每件活只取这些种类的这几个字段，不读大块正文。
KINDS = ("item_changed", "round_finished", "gate_blocked", "gate_passed", "program_result",
         "event", "event_classified")
FIELDS = ("stage", "state", "gate_stage", "reason", "action", "pending_classification", "unmatched")
STAGE_KINDS = ("item_changed", "round_finished", "gate_blocked")


def versions(rounds):
    """同一个名字的配置档，启动设置一变就是新一版；按第一次用到的时刻编号，记从哪天起。"""
    result = {}
    for row in sorted(rounds, key=lambda r: r["at"]):
        name, content = row["executor"], json.dumps(row.get("config") or {}, sort_keys=True, ensure_ascii=False)
        seen = result.setdefault(name, {})
        if content not in seen:
            seen[content] = (len(seen) + 1, datetime.fromisoformat(row["at"]).astimezone().date().isoformat())
    return result


def item_outcome(facts):
    """一件活在交付和收尾上的结果。

    交付只认程序步做的（有交付的程序结果）；旧流程由 agent 跑的交付不算进过交付。
    退回：交付程序步把活交回执行（合并前 DoD、合并失败、上线后 DoD 都走这条），返工、恢复不算。
    一次过：第一次进交付，离开前没因合并前 DoD 或合并失败退回；还没离开交付的，看过没过合并前 DoD。
    收尾后「不对」：活完成以后收到返工。
    """
    stage = None
    first = None  # True 一次过，False 没过，None 还看不出
    judged = by_program = passed_check = completed = wrong = gated = False
    returns = 0
    reason = ""
    for fact in facts:
        kind, body = fact["kind"], fact["body"]
        if kind == "gate_passed":
            # 过关口紧接着换阶段是程序步交接；返工、恢复换阶段前没有过关口。旧事实没记 gate_stage，只看先后。
            gated = True
            continue
        if kind == "program_result" and body.get("stage") == DELIVERY and stage == DELIVERY:
            by_program = True
            reason = body.get("reason") or ""
            passed_check = passed_check or "合并前 DoD 已过" in reason
        elif kind in ("event", "event_classified"):
            if (body.get("action") == "rework" and completed
                    and not body.get("pending_classification") and not body.get("unmatched")):
                wrong = True
        if kind not in STAGE_KINDS:
            continue
        new = body.get("stage", stage)
        if new != stage:
            if stage == DELIVERY and by_program:
                returned = gated and new == WRITE
                returns += returned
                if not judged:
                    if returned:
                        # 上线后 DoD 退回说明合并前 DoD 已过；合并前 DoD、合并失败退回都算没过。
                        first = reason.startswith("上线后 DoD")
                    else:
                        # 进收尾算过；返工、恢复离开的，看离开前过没过合并前 DoD。
                        first = True if new == CLOSING or passed_check else None
                    judged = True
            if new == DELIVERY:
                by_program = passed_check = False
                reason = ""
            stage = new
        if body.get("state") == "完成":
            completed = True
        gated = False
    if stage == DELIVERY and by_program and not judged and passed_check:
        first = True
    return dict(first=first, returns=returns, wrong=wrong)


def round_cost(row):
    """一轮的 API 折合花费（含子 agent）；没结束、子 agent 没读到或任何一段缺花费都算未知。"""
    if row.get("status") != "ended" or not row.get("children_reported"):
        return None
    values = [entry.get("cost_usd") for entry in (row.get("usage_by_model") or [row.get("usage") or {}])]
    return None if not values or any(v is None for v in values) else sum(values)


def known_sum(values):
    return None if any(v is None for v in values) else sum(values)


def profile_rows(log, start):
    """start 是带时区的时刻；只算这之后（含）开工的活。没有活时返回空列表。"""
    writes = [r for r in log.all("rounds") if r.get("stage") == WRITE and r.get("executor")]
    numbered = versions(writes)
    by_item = {}
    for row in writes:
        by_item.setdefault(row["item"], []).append(row)
    groups = {}
    for item_id, rows in by_item.items():
        if min(datetime.fromisoformat(r["at"]) for r in rows) < start:
            continue
        keys = {(r["executor"], numbered[r["executor"]][json.dumps(r.get("config") or {}, sort_keys=True, ensure_ascii=False)])
                for r in rows}
        key = keys.pop() if len(keys) == 1 else SWITCHED
        facts = log.selected_facts(KINDS, FIELDS, item=item_id)
        groups.setdefault(key, []).append(dict(
            item=item_id, **item_outcome(facts),
            seconds=known_sum([r.get("elapsed_seconds") for r in rows]),
            cost=known_sum([round_cost(r) for r in rows])))
    result = []
    for key in sorted((k for k in groups if k != SWITCHED), key=lambda k: (k[0], k[1][0])):
        items = groups[key]
        decided = [i for i in items if i["first"] is not None]
        seconds = [i["seconds"] for i in items if i["seconds"] is not None]
        costs = [i["cost"] for i in items if i["cost"] is not None]
        result.append(dict(name=key[0], version=key[1][0], since=key[1][1], items=len(items),
                           first_passed=sum(i["first"] for i in decided), first_total=len(decided),
                           returns=sum(i["returns"] for i in items) / len(items),
                           average_seconds=sum(seconds) / len(seconds) if seconds else None,
                           seconds_missing=len(items) - len(seconds),
                           average_cost=sum(costs) / len(costs) if costs else None,
                           cost_missing=len(items) - len(costs),
                           wrong=sum(i["wrong"] for i in items)))
    if SWITCHED in groups:
        result.append(dict(name=SWITCHED, items=len(groups[SWITCHED])))
    return result


def render(rows, scope):
    if not rows:
        return []
    lines = [f"按执行者配置档（{scope}）",
             "配置档\t件数\t合并前DoD一次过\t每件退回\t每件执行用时\t每件花费USD\t收尾后「不对」"]
    for row in rows:
        if row["name"] == SWITCHED:
            lines.append(f'{SWITCHED}\t{row["items"]}\t—\t—\t—\t—\t—')
            continue
        total = row["first_total"]
        rate = f'{row["first_passed"] / total:.0%}（{row["first_passed"]}/{total}）' if total else "未知"
        minutes = row["average_seconds"]
        minutes = f"{minutes / 60:.0f} 分" if minutes is not None else "未知"
        cost = row["average_cost"]
        cost = f"{cost:.2f}" if cost is not None else "未知"
        if row["seconds_missing"]:
            minutes += f'（缺 {row["seconds_missing"]}）'
        if row["cost_missing"]:
            cost += f'（缺 {row["cost_missing"]}）'
        lines.append(f'{row["name"]} 第{row["version"]}版（{row["since"]} 起）\t{row["items"]}\t{rate}'
                     f'\t{row["returns"]:.1f}\t{minutes}\t{cost}\t{row["wrong"]}')
    return lines
