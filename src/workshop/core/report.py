"""只读的状态、起因与用量查询。"""
from dataclasses import fields
from datetime import datetime, timedelta
import json
import re
from .jobs import running as running_jobs
from .models import Event, utcnow


def status(log):
    items = log.all("items")
    running = sum(i["state"] == "跑着" for i in items)
    waiting = sum(i["state"] == "等你" for i in items)
    blocked = sum(i["state"] == "卡住" for i in items)
    queued = sum(i["state"] == "排队" for i in items)
    lines = [f"活 {len(items)} 件 · 跑着 {running} · 等你 {waiting} · 卡住 {blocked}" + (f" · 排队 {queued}" if queued else ""),
             "活\t阶段\t状态\t谁在推\t下一步\t卡在谁手里"]
    # 守护进程在后台作业里替这件活跑东西（例如流程跑检查）时，状态显示作业的字样；每拍回查，跑完按结果结算。
    jobs = running_jobs(log.path.parent / "jobs")
    for item in items:
        row = log.get("rounds", item.get("round", ""))
        who = row["executor"] if row else "车间"
        control = log.get("controls", item["target"]) or {}
        state = "暂停" if control.get("paused") else jobs.get(item["id"], item["state"])
        lines.append("\t".join((item["id"], item["stage"], state, who, item["next"], item["blocked_by"] or "—")))
    uncertain = [c for c in log.all("cards") if c.get("reconcile_failed") and not c["sent"] and not c.get("cancel_done")]
    for card in uncertain:
        lines.append(f'通道待对账\t{card["item"]}\t{card["id"]}\t连续 3 次不能确定是否已发送，需检查通道')
    return "\n".join(lines)


def resolve(log, targets, text):
    """`<短名>#<对象键>` 或 `<交付对象>:<对象键>` 找到活 id；对象键先查别名。"""
    short = {t["short_name"]: name for name, t in targets.items() if isinstance(t, dict) and t.get("short_name")}
    named = re.fullmatch(r"([^\s:#]+)#(\S+)", text)
    target, key = (short.get(named[1]), named[2]) if named else text.split(":", 1) if ":" in text else (None, None)
    if target and key:
        alias = log.get("aliases", f"{target}:{key}")
        item_id = f'{target}:{alias["object_key"] if alias else key}'
        if log.get("items", item_id):
            return item_id
    raise ValueError(f'找不到活：{text}；可用短名：{"、".join(sorted(short)) or "无"}')


def one_line(text):
    return " ".join(str(text).split())


ACTIONS = {"update": "更新", "rework": "返工", "ready": "放行"}


def flow_for(targets, name, plugins):
    """规则里的交付对象和它的流程；装不上流程时只回交付对象，查看起因不依赖插件。"""
    target = {"name": name, **targets[name]} if name in targets else None
    flow = None
    if plugins and target:
        try:
            flow = plugins.get("flow", target["plugin"])
        except Exception:
            flow = None
    return target, flow


def summarize(body, target, flow):
    """流程给的一句话说明；流程没有说明或出错时退回事件种类。"""
    content = body["body"]
    kind = content.get("kind") if isinstance(content, dict) and isinstance(content.get("kind"), str) else "资料"
    describe = getattr(flow, "describe", None)
    summary = None
    if describe and target:
        try:
            summary = describe(Event(**{f.name: body[f.name] for f in fields(Event) if f.name in body}), target)
        except Exception:  # 摘要只读且可选；插件出错不影响查看起因（R20）。
            summary = None
    return one_line(summary)[:200] if isinstance(summary, str) and summary.strip() else kind


def entry_label(source):
    return "CLI 入口 " if source.startswith("cli:") else ""


def event_line(body, target, flow, raw):
    """一条事件一行：来源、动作和流程给的一句说明；流程没有说明时退回事件种类。"""
    line = f'{entry_label(body["source"])}事件 {body["source"]} {body.get("action", "update")}：{summarize(body, target, flow)}'
    if raw:
        line += "\n    原始资料：" + json.dumps(body["body"], ensure_ascii=False, sort_keys=True)
    return line


def original_event(log, seq):
    row = log.db.execute("SELECT body FROM facts WHERE seq=? AND kind='event'", (seq,)).fetchone()
    return json.loads(row[0]) if row else None


def classified_line(log, body, target, flow):
    """外部入口事件经流程判定归到这件活：动作、来源，以及流程核实后的谁说的和原话摘要。"""
    event = body.get("event") or original_event(log, body.get("event_seq")) or {}
    source = event.get("source", "未知来源")
    summary = body.get("summary")
    if isinstance(summary, str) and summary.strip():
        summary = one_line(summary)[:200]
    elif "body" in event:
        summary = summarize(event, target, flow)
    else:
        summary = "原事件不在事实库"
    action = ACTIONS.get(body.get("action"), body.get("action"))
    return f'归件 {entry_label(source)}{source} {action}：{summary}（原事件 {body.get("event_seq")}）'


def why(log, item_id, rules=None, plugins=None, raw=False):
    """rules 是规则数据（含 targets）；plugins 可选，用来取流程的事件摘要。"""
    targets = (rules or {}).get("targets") or {}
    item_id = resolve(log, targets, item_id)
    item = log.get("items", item_id)
    target, flow = flow_for(targets, item["target"], plugins)
    lines = [f"活 {item_id}"]
    for fact in log.facts(item_id):
        body = fact["body"]
        kind = fact["kind"]
        if kind == "event":
            lines.append(f'{fact["at"]} {event_line(body, target, flow, raw)}')
            continue
        if kind == "event_classified":
            lines.append(f'{fact["at"]} {classified_line(log, body, target, flow)}')
            continue
        if kind == "round_reserved":
            detail = (f'{body["stage"]} {body["executor"]} 一轮 {body["token"]} 起因：事实 {body["cause"]} '
                      f'规则模型={display(body.get("requested", {}).get("model"))} 强度={display(body.get("requested", {}).get("effort"))} '
                      f'选择理由：{body.get("selection_reason", "未知")}')
        elif kind == "round_changed" and body.get("status") == "ended":
            row = log.get("rounds", body["token"])
            value = row.get("usage") or {}
            tokens = " / ".join(display(value.get(k)) for k in TOKEN_FIELDS)
            detail = (f'轮结束 {body["token"]} 结果={row.get("outcome", "未知")} '
                      f'判过={display(row.get("passed"))} 实际模型={display(value.get("model_actual"))} '
                      f'耗时={display(row.get("elapsed_seconds"))}秒 token={tokens} '
                      f'API折合USD={display(value.get("cost_usd"))} 计费={display(value.get("billing"))}')
            for entry in entries(row):
                if entry["scope"] == "children":
                    child_tokens = " / ".join(display(entry.get(k)) for k in TOKEN_FIELDS)
                    detail += f'；子 agent 模型={display(entry.get("model_actual"))} token={child_tokens} API折合USD={display(entry.get("cost_usd"))}'
            if not row.get("children_reported"):
                detail += "；子 agent 未计"
        elif kind == "round_changed" and body.get("receipt_rejected"):
            detail = f'回执被拒 {body["token"]}：{body["receipt_rejected"]}'
        elif kind == "receipt":
            receipt = body["receipt"]
            detail = f'回执 {receipt["outcome"]} {receipt["summary"]}'
        elif kind in ("gate_blocked", "gate_passed"):
            detail = f'{"关口拦住" if kind == "gate_blocked" else "过关口"} {body.get("gate_reason", "")}'
        elif kind == "selection_failed":
            detail = f'挑选逻辑起轮失败 {body["reason"]}'
        elif kind == "selection_none":
            detail = f'挑选逻辑没选出执行者 {body["reason"]}'
        elif kind in ("card_rejected", "card_cancelled_external"):
            detail = f'卡片{ "拒收" if kind == "card_rejected" else "作废"} {body["id"]}：{body["reason"]}'
        elif kind == "invalid_answer":
            detail = f'非选项回答 卡片 {body["card"]}「{body["title"]}」收到「{body["value"]}」，仍等原卡'
        elif kind == "answer":
            detail = f'回答 {body["value"]}'
            via = body.get("via")
            if isinstance(via, dict) and via.get("channel") == "session":
                detail += f'（会话 {via.get("session")} 转录，Kun 原话：「{via.get("quote")}」）'
        elif kind == "notice_held":
            detail = f'通知没发 {body.get("stage", "")} {body["key"]}「{body["subject"] or body.get("text", "")}」：{body["reason"]}'
        elif kind == "work_blocked":
            detail = f'卡住 {body["blocked_by"]}'
        else:
            continue
        lines.append(f'{fact["at"]} {one_line(detail)}')
    return "\n".join(lines)


def unfiled(log, rules=None, plugins=None):
    """未归件：外部入口事件里判为丢弃的，和还没判定完的；归到活（含新建的活）的在那件活的 why 里。"""
    targets = (rules or {}).get("targets") or {}
    outcomes = {}
    for fact in log.facts():
        if fact["kind"] in ("event_classified", "event_discarded"):
            outcomes[fact["body"].get("event_seq")] = fact
    lines = ["未归件"]
    for fact in log.facts():
        body = fact["body"]
        if fact["kind"] != "event" or body.get("ingress") != "external":
            continue
        target, flow = flow_for(targets, body["target"], plugins)
        outcome = outcomes.get(fact["seq"])
        if outcome and outcome["kind"] == "event_classified":
            if log.get("items", outcome["item"]):
                continue
            state = f'归到 {outcome["item"]}，活已清掉'
        elif outcome:
            state = "丢弃"
        else:
            state = "还没判定"
        note = (outcome or {}).get("body", {}).get("summary")
        reason = f'；判定：{one_line(note)[:200]}' if isinstance(note, str) and note.strip() else ""
        lines.append(f'{fact["at"]} {state} {body["target"]} {entry_label(body["source"])}{body["source"]}：'
                     f'{summarize(body, target, flow)}{reason}（事件 {fact["seq"]}）')
    if len(lines) == 1:
        lines.append("没有")
    return "\n".join(lines)


TOKEN_FIELDS = ("input", "cache_read", "cache_write", "output")


def display(value):
    return "未知" if value is None else str(value)


def entries(row):
    return row.get("usage_by_model") or [{"scope": "main", **(row.get("usage") or {})}]


def sum_known(values):
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def total(rows, field):
    return sum_known([entry.get(field) for row in rows for entry in entries(row)])


def incomplete(value):
    return any(value.get(k) is None for k in (*TOKEN_FIELDS, "model_actual", "cost_usd", "billing"))


def model_metrics(log):
    rows = [r for r in log.all("rounds") if r["status"] == "ended"]
    first = {}
    for row in rows:
        if row["stage"] in ("审查", "验收"):
            key = (row["item"], row["stage"], row["config"]["family"])
            first.setdefault(key, row["token"])
    groups = {}
    for row in rows:
        for value in entries(row):
            model = value.get("model_actual")
            groups.setdefault(model, []).append({**row, "usage": value, "scope": value["scope"]})
    result = {}
    for model, selected in groups.items():
        unique = {r["token"]: r for r in selected}
        durations = [r["elapsed_seconds"] for r in unique.values() if r.get("elapsed_seconds") is not None]
        cache_rows = [r for r in selected if all(r["usage"].get(k) is not None for k in ("input", "cache_read", "cache_write"))]
        denominator = sum(sum(r["usage"][k] for k in ("input", "cache_read", "cache_write")) for r in cache_rows)
        rates = {}
        for stage in ("审查", "验收"):
            attempts = [r for r in selected if r["stage"] == stage and r["scope"] == "main"
                        and first.get((r["item"], stage, r["config"]["family"])) == r["token"]]
            known = [r for r in attempts if r.get("passed") is not None and r.get("outcome") is not None]
            rates[stage] = {"passed": sum(r["passed"] and r["outcome"] == "done" for r in known),
                            "total": len(known), "missing": len(attempts) - len(known)}
        unreported = {r["token"] for r in selected if incomplete(r["usage"]) or r.get("elapsed_seconds") is None
                      or r["scope"] == "main" and r["stage"] in ("审查", "验收") and r.get("passed") is None}
        cache_missing = {r["token"] for r in selected if any(r["usage"].get(k) is None for k in ("input", "cache_read", "cache_write"))}
        result[model] = dict(rounds=len(unique), tokens={k: sum_known([r["usage"].get(k) for r in selected]) for k in TOKEN_FIELDS},
                            cost_usd=sum_known([r["usage"].get("cost_usd") for r in selected]),
                            average_seconds=sum(durations) / len(durations) if durations else None,
                            duration_missing=len(unique) - len(durations),
                            cache_hit_rate=sum(r["usage"]["cache_read"] for r in cache_rows) / denominator if denominator else None,
                            cache_missing=len(cache_missing), rates=rates, unreported=len(unreported),
                            children_unreported=sum(not r.get("children_reported") for r in unique.values()))
    return result


def usage(log, now=None):
    now = (now or utcnow()).astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = [r for r in log.all("rounds") if r["status"] == "ended"]

    def summarize(label, selected):
        tokens = " / ".join(display(total(selected, k)) for k in TOKEN_FIELDS)
        cost = total(selected, "cost_usd")
        money = f"${cost:.4f}" if cost is not None else "未知"
        missing = sum(any(incomplete(entry) for entry in entries(r)) for r in selected)
        children_missing = sum(not r.get("children_reported") for r in selected)
        return f"{label}\t{len(selected)}\t{tokens}\t{money}\t未上报 {missing} 轮；子 agent 未计 {children_missing} 轮"

    lines = ["期间/活\t轮数\t输入/缓存读/缓存写/输出 token\tAPI 折合 USD\t未上报",
             summarize("今天", [r for r in rows if datetime.fromisoformat(r["at"]) >= today]),
             summarize("最近 7 天", [r for r in rows if datetime.fromisoformat(r["at"]) >= now - timedelta(days=7)])]
    for item_id in dict.fromkeys(r["item"] for r in rows):
        lines.append(summarize(f"按活 {item_id}", [r for r in rows if r["item"] == item_id]))
    lines.append("按实际模型\t轮数\t审查一次过\t验收一次过\t平均秒\t输入/缓存读/缓存写/输出 token\tAPI 折合 USD\t缓存命中率\t未上报")
    for model, stats in model_metrics(log).items():
        def rate(stage):
            value = stats["rates"][stage]
            return f'{value["passed"]}/{value["total"]}（缺 {value["missing"]}）' if value["total"] else f'未知（缺 {value["missing"]}）'
        tokens = " / ".join(display(stats["tokens"][k]) for k in TOKEN_FIELDS)
        hit = stats["cache_hit_rate"]
        hit = f"{hit:.1%}" if hit is not None else "未知"
        cost = stats["cost_usd"]
        cost = f"${cost:.4f}" if cost is not None else "未知"
        average = stats["average_seconds"]
        average = f"{average:.1f}" if average is not None else "未知"
        lines.append(f'{display(model)}\t{stats["rounds"]}\t{rate("审查")}\t{rate("验收")}\t{average}（缺 {stats["duration_missing"]}）'
                     f'\t{tokens}\t{cost}\t{hit}（缺 {stats["cache_missing"]}）\t未上报 {stats["unreported"]} 轮；子 agent 未计 {stats["children_unreported"]} 轮')
    return "\n".join(lines)
