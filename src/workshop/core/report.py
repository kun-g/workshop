"""只读的状态、起因与用量查询。"""
from dataclasses import fields
from datetime import datetime, timedelta
import json
from .jobs import running as running_jobs
from .models import Event, utcnow
from .flows import configured
from .holding import resolve


ENDED = ("完成", "放弃")


def state_since(log, item):
    """这件活进入当前状态的时间（事实里最近一次改状态的时刻）；查不到返回 None，页面写「时长未知」。"""
    found = log.db.execute("SELECT at FROM facts WHERE item=? AND kind='item_changed' AND json_extract(body,'$.state') IS NOT NULL "
                           "ORDER BY seq DESC LIMIT 1", (item,)).fetchone()
    return found[0] if found else None


def origin(item):
    """这件活从哪份来源资料来：标题、链接、简称，以及 Kun 原话的第一句（页面给人回忆背景用）；资料里没有标题和链接的返回 None。"""
    found = [m["body"] for m in item.get("materials") or []
             if isinstance(m.get("body"), dict) and isinstance(m["body"].get("data"), dict)
             and m["body"]["data"].get("title") and m["body"]["data"].get("html_url")]
    if not found:
        return None
    data = found[-1]["data"]
    quotes = [line.lstrip("> ").strip() for line in (data.get("body") or "").splitlines() if line.startswith(">") and line.lstrip("> ").strip()]
    return {"title": data["title"], "url": data["html_url"],
            "ref": f'{str(found[-1].get("repo", "")).split("/")[-1]}#{found[-1].get("number", "")}', "quote": quotes[0] if quotes else ""}


def overview(log, *, everything=False, states=(), targets=(), flows=()):
    """总览要用的数据，文字和网页共用。默认只取没结束的活；states、targets、flows 是筛选，空表示不筛。"""
    count = log.db.execute("SELECT count(*) FROM items").fetchone()[0]
    # 默认不列已结束的活：在库里先筛掉，不逐行解析它们（已结束的占绝大多数，解析一遍要 200 多毫秒）。
    items = log.all("items") if everything or states else log.open_items()
    cards = log.all("cards")
    unreadable = [c for c in cards if c["mode"] == "ask" and not c["answered"]
                  and not c.get("cancelled") and not c.get("rejected") and c.get("read_failures", 0) >= 10]
    blocked_reads = {c["item"] for c in unreadable}
    # 读取故障是卡片的状态，投影到总览；不覆盖活已有的等待/故障，恢复后自然消失。
    read_states = {i["id"] for i in items if i["id"] in blocked_reads and i["state"] == "等你"}
    # 守护进程在后台作业里替这件活跑东西（例如流程跑检查）时，状态显示作业的字样；每拍回查，跑完按结果结算。
    jobs = running_jobs(log.path.parent / "jobs")
    rows = []
    for item in items:
        row = log.get("rounds", item.get("round", ""))
        control = log.get("controls", item["target"]) or {}
        stuck = item["state"] == "卡住" or item["id"] in read_states
        shown = "暂停" if control.get("paused") else jobs.get(item["id"], "卡住" if item["id"] in read_states else item["state"])
        next_step, blocker = item["next"], item["blocked_by"] or "—"
        if item["id"] in read_states:
            next_step, blocker = "项目经理核对卡片读取错误（见卡住清单）", "通道"
        rows.append({"id": item["id"], "title": item.get("title") or item["id"], "target": item["target"],
                     "flow": item.get("flow") or "", "stage": item["stage"], "state": item["state"], "shown": shown,
                     "stuck": stuck, "who": row["executor"] if row else "车间", "next": next_step, "blocker": blocker,
                     "reason": item["blocked_by"], "advice": item["next"],
                     "owner": (item.get("leave") or {}).get("owner") or "", "origin": origin(item)})
    filtered = bool(states or targets or flows)
    picked = [r for r in rows
              if (everything or states or r["state"] not in ENDED)
              and (not states or r["shown"] in states or r["state"] in states)
              and (not targets or r["target"] in targets)
              and (not flows or r["flow"] in flows)]
    for r in picked:
        r["since"] = state_since(log, r["id"])
    ids = {r["id"] for r in picked}
    keep = (lambda card: card["item"] in ids) if filtered else (lambda card: True)
    by_id = {r["id"]: r for r in rows}
    unread = [{"item": c["item"], "stage": by_id.get(c["item"], {}).get("stage", ""),
               "reason": f"{c.get('external') or c['id']} 连续 {c['read_failures']} 次读取失败：{c.get('read_error', '')}"}
              for c in unreadable if keep(c)]
    asks = [{"item": c["item"], "card": c.get("external") or c["id"], "decision": c["payload"]["decision"]}
            for c in cards if c["mode"] == "ask" and not c["answered"] and not c.get("cancelled")
            and not c.get("rejected") and keep(c)]
    uncertain = [{"item": c["item"], "card": c["id"]} for c in cards
                 if c.get("reconcile_failed") and not c["sent"] and not c.get("cancel_done") and keep(c)]
    return {"rows": picked, "total": count, "ended": count - sum(r["state"] not in ENDED for r in rows),
            "hidden": count - len(picked), "everything": everything, "filtered": filtered,
            "unreadable": unread, "asks": asks, "uncertain": uncertain,
            "filters": {"states": list(states), "targets": list(targets), "flows": list(flows)}}


def status(log, **options):
    view = overview(log, **options)
    rows = view["rows"]
    count = lambda state: sum(r["state"] == state for r in rows)
    reads = sum(r["stuck"] and r["state"] == "等你" for r in rows)
    head = f"活 {len(rows)} 件 · 跑着 {count('跑着')} · 等你 {count('等你') - reads} · 卡住 {count('卡住') + reads}"
    head += f" · 排队 {count('排队')}" if count("排队") else ""
    head += f" · 定时 {count('定时')}" if count("定时") else ""
    for waiting in ("等前置", "等子单"):
        head += f" · {waiting} {count(waiting)}" if count(waiting) else ""
    if view["hidden"]:
        head += f"\n另有 {view['hidden']} 件没列出" + ("（筛掉的）" if view["filtered"] else "（已结束；workshop status --all 看全部）")
    lines = [head, "活\t阶段\t状态\t谁在推\t下一步\t卡在谁手里"]
    for r in rows:
        lines.append("\t".join((r["id"], r["stage"], r["shown"], r["who"], r["next"], r["blocker"])))
    lines.append("卡住清单（项目经理处理）\n活\t阶段\t原因\t建议动作")
    for r in rows:
        if r["state"] == "卡住":
            lines.append("\t".join((r["id"], r["stage"], one_line(r["reason"]), one_line(r["advice"]))))
    for card in view["unreadable"]:
        lines.append("\t".join((card["item"], card["stage"], one_line(card["reason"]),
                                "项目经理核对通道读取权限和状态；恢复后自动收取原卡回答")))
    lines.append("等回答的问卡")
    for card in view["asks"]:
        lines.append(f'{card["item"]}\t{card["card"]}\t{card["decision"]}')
    for card in view["uncertain"]:
        lines.append(f'通道待对账\t{card["item"]}\t{card["card"]}\t连续 3 次不能确定是否已发送，需检查通道')
    return "\n".join(lines)


def one_line(text):
    return " ".join(str(text).split())


ACTIONS = {"update": "更新", "rework": "返工", "ready": "放行"}


def flow_for(targets, name, plugins, selected=""):
    """规则里的交付对象和它的流程；装不上流程时只回交付对象，查看起因不依赖插件。"""
    try:
        target = configured(name, targets[name], selected) if name in targets else None
    except (ValueError, KeyError):
        target = {"name": name, **targets[name]}  # 历史仍可查，即使当前规则已移除该流程。
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
    via = body.get('body')
    if body['source'] == 'session' and body.get('ingress') == 'source' and isinstance(via, dict):
        line += f'（channel=session，会话 {one_line(via.get("session"))}，Kun 原话：「{one_line(via.get("quote"))}」）'
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
    target, flow = flow_for(targets, item["target"], plugins, item.get("flow", ""))
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
        elif kind == "program_result":
            detail = f'程序完成 {body["stage"]}：{body.get("reason", "")}'
        elif kind == "program_after":
            result = body["result"]
            detail = f'程序阶段 {body["stage"]} after：{json.dumps(result, ensure_ascii=False)}'
        elif kind == "round_result" and body.get("reason"):
            detail = f'流程核对 {body["stage"]}：{body["reason"]}'
        elif kind in ("gate_blocked", "gate_passed"):
            detail = f'{"关口拦住" if kind == "gate_blocked" else "过关口"} {body.get("gate_reason", "")}'
        elif kind == "selection_failed":
            detail = f'挑选逻辑起轮失败 {body["reason"]}'
        elif kind == "selection_none":
            detail = f'挑选逻辑没选出执行者 {body["reason"]}'
        elif kind in ("card_rejected", "card_cancelled_external"):
            detail = f'卡片{ "拒收" if kind == "card_rejected" else "作废"} {body["id"]}：{body["reason"]}'
        elif kind == "card_changed" and body.get("read_failures") == 10:
            card = log.get("cards", body["id"]) or {}
            detail = f'卡片 {card.get("external") or body["id"]} 连续 10 次读取失败：{body.get("read_error", "")}'
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
        elif kind == "recovery_requested":
            detail = f'恢复 {body["mode"]} 从 {body["from_state"]}/{body["stage"]}：{body["reason"]}；Kun 原话：{body["quote"]}；计划：{json.dumps(body["plan"], ensure_ascii=False)}'
        elif kind == "recovery_applied":
            detail = f'已消费恢复机会 {body["id"]}'
        elif kind == "item_changed" and "relations" in body:
            snapshot = body["relations"]
            labels = (("prerequisites", "前置"), ("members", "子单"),
                      ("collections", "所属总单"), ("related", "相关"))
            parts = [label + "=" + "、".join(n["name"] + ("（已完成）" if n["complete"] else "（未完成）")
                                                for n in snapshot.get(key, []))
                     for key, label in labels if snapshot.get(key)]
            detail = "关系快照：" + ("；".join(parts) or "没有关系")
            if snapshot.get("error"):
                detail = "关系未确认：" + snapshot["error"]
        elif kind == "item_changed" and any(k in body for k in ("relation_hold", "aggregate")):
            detail = f'关系准入：{body.get("state", "")} {body.get("next", "")}'
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
        if fact["kind"] != "event" or (body.get("ingress") != "external" and not body.get("unmatched")):
            continue
        target, flow = flow_for(targets, body["target"], plugins, body.get("flow", ""))
        outcome = outcomes.get(fact["seq"])
        if body.get("unmatched"):
            state = "没有流程匹配"
        elif outcome and outcome["kind"] == "event_classified":
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
