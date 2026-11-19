"""按执行者配置档对比（核心 R10、R24）：一件活算在跑它执行阶段的配置档头上。

只读：轮次表定配置档、开工时刻、用时和花费；每件活的事实定进交付、退回和收尾后的「不对」。
`--by` 再按活的类型拆开：改了什么、改动多大读活状态文件里合并时记的 PR 概况，交付对象读活本身。
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path


WRITE = "执行"
DELIVERY = "交付"
CLOSING = "收尾"
SWITCHED = "换过执行者"
UNKNOWN = "未知"
# 每件活只取这些种类的这几个字段，不读大块正文。
KINDS = ("item_changed", "round_finished", "gate_blocked", "gate_passed", "program_result",
         "event", "event_classified")
FIELDS = ("stage", "state", "gate_stage", "reason", "action", "pending_classification", "unmatched")
STAGE_KINDS = ("item_changed", "round_finished", "gate_blocked")

# 报告不引用插件代码，只认状态文件里这份记录的格式（issue #300）。
COLUMNS = {"kind": "改了什么", "size": "改动多大", "target": "交付对象"}
KIND_ORDER = ("只改文档", "只改测试和文档", "改了程序")
SIZE_ORDER = ("小", "中", "大")
HEAVIER = {name: weight for weight, name in enumerate(KIND_ORDER)}


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


def item_stats(directory, item_id):
    """读活状态文件里合并时记的 PR 概况；没有文件、没有概况或格式不对都算没有。"""
    path = Path(directory) / (hashlib.sha256(item_id.encode()).hexdigest() + ".json")
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    stats = state.get("pr_stats") if isinstance(state, dict) else None
    return stats if isinstance(stats, dict) else None


def combined(stats):
    """一件活所有 PR 合起来：行数相加，分类取最重的一档；有一个未知就整件未知。"""
    if not isinstance(stats, dict):
        return None
    entries = [value for value in stats.values() if isinstance(value, dict)
               and type(value.get("additions")) is int and type(value.get("deletions")) is int]
    if not entries:
        return None
    kinds = [value.get("kind") for value in entries]
    kind = UNKNOWN if any(kind not in HEAVIER for kind in kinds) else max(kinds, key=HEAVIER.get)
    return {"lines": sum(value["additions"] + value["deletions"] for value in entries), "kind": kind}


def size_label(lines):
    """小（不到 50 行）、中（50 到 300 行）、大（超过 300 行）。"""
    return "小" if lines < 50 else "中" if lines <= 300 else "大"


def buckets(log, by, directory):
    """每件活在这个维度上的档；没有状态文件、没有概况、概况未知的归「未知」，不丢掉。"""
    if by not in COLUMNS:
        raise ValueError(f"--by 取值只能是 kind、size、target，收到 {by}")
    targets = {row["id"]: row.get("target") for row in log.all("items", fields=("id", "target"))}
    result = {}
    for item_id in {r["item"] for r in log.all("rounds") if r.get("stage") == WRITE and r.get("executor")}:
        if by == "target":
            name = targets.get(item_id)
            result[item_id] = name if isinstance(name, str) and name.strip() else UNKNOWN
            continue
        summary = combined(item_stats(directory, item_id) or {})
        if summary is None:
            result[item_id] = UNKNOWN
        elif by == "kind":
            result[item_id] = summary["kind"]
        else:
            result[item_id] = size_label(summary["lines"])
    return result


def bucket_order(by, label):
    """档的顺序固定：改了什么、改动多大按定好的次序，交付对象按名字，「未知」排最后。"""
    if label == UNKNOWN:
        return (1, 0, "")
    if by == "kind":
        return (0, 0, KIND_ORDER.index(label))
    if by == "size":
        return (0, 0, SIZE_ORDER.index(label))
    return (0, 1, label)


def profile_rows(log, start, by=None, buckets=None):
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
        group = key if by is None else (key, (buckets or {}).get(item_id, UNKNOWN))
        groups.setdefault(group, []).append(dict(
            item=item_id, **item_outcome(facts),
            seconds=known_sum([r.get("elapsed_seconds") for r in rows]),
            cost=known_sum([round_cost(r) for r in rows])))
    result = []
    profile_of = lambda group: group[0] if by is not None else group
    bucket_of = lambda group: group[1] if by is not None else None
    normal = [group for group in groups if profile_of(group) != SWITCHED]
    for group in sorted(normal, key=lambda k: (profile_of(k)[0], profile_of(k)[1][0], bucket_order(by, bucket_of(k)))):
        items = groups[group]
        name, version, since = profile_of(group)[0], profile_of(group)[1][0], profile_of(group)[1][1]
        bucket = bucket_of(group)
        decided = [i for i in items if i["first"] is not None]
        seconds = [i["seconds"] for i in items if i["seconds"] is not None]
        costs = [i["cost"] for i in items if i["cost"] is not None]
        result.append(dict(name=name, version=version, since=since, bucket=bucket, items=len(items),
                           first_passed=sum(i["first"] for i in decided), first_total=len(decided),
                           returns=sum(i["returns"] for i in items) / len(items),
                           average_seconds=sum(seconds) / len(seconds) if seconds else None,
                           seconds_missing=len(items) - len(seconds),
                           average_cost=sum(costs) / len(costs) if costs else None,
                           cost_missing=len(items) - len(costs),
                           wrong=sum(i["wrong"] for i in items)))
    for group in sorted((g for g in groups if profile_of(g) == SWITCHED), key=lambda k: bucket_order(by, bucket_of(k))):
        result.append(dict(name=SWITCHED, bucket=bucket_of(group), items=len(groups[group])))
    return result


def render(rows, scope, by=None):
    if not rows:
        return []
    dimension = COLUMNS[by] if by else None
    title = f"按执行者配置档（{scope}）" if not by else f"按执行者配置档 × {dimension}（{scope}）"
    column = "" if not by else f"\t{dimension}"
    lines = [title,
             f"配置档{column}\t件数\t合并前DoD一次过\t每件退回\t每件执行用时\t每件花费USD\t收尾后「不对」"]
    for row in rows:
        cell = "" if not by else f'\t{row["bucket"]}'
        if row["name"] == SWITCHED:
            lines.append(f'{SWITCHED}{cell}\t{row["items"]}\t—\t—\t—\t—\t—')
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
        lines.append(f'{row["name"]} 第{row["version"]}版（{row["since"]} 起）{cell}\t{row["items"]}\t{rate}'
                     f'\t{row["returns"]:.1f}\t{minutes}\t{cost}\t{row["wrong"]}')
    return lines
