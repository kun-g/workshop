"""只读的状态、起因与用量查询。"""
from dataclasses import fields
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from workshop.core.jobs import running as running_jobs
from workshop.core.models import Event, utcnow
from workshop.core.ports import Flow, Plugins
from workshop.core.flows import configured
from workshop.core.holding import resolve


from .manager_status import read_managers, sections
from . import profiles


ENDED = ("完成", "放弃")


def waiting_records(log, rules, plugins, item_id=None, cache=None):
    """cache 是 {等待号: 上次读到的记录}：读到就更新，读不到就用它顶上并标 stale（上次读到的时间）；没有旧数据才写读取失败。"""
    result = []
    for wait in log.all('waits'):
        if item_id and wait['item'] != item_id:
            continue
        if not item_id and wait['state'] != 'waiting':
            continue
        try:
            config = rules.get('wait_services', {})[wait['provider']]
            record = plugins.get('wait', wait['provider']).read(wait['ref'], config)
            if cache is not None:
                cache[wait['ref']] = {'record': record, 'at': utcnow().isoformat()}
            result.append(shaped(wait, record))
        except Exception as exc:
            old = (cache or {}).get(wait['ref'])
            if old:
                result.append({**shaped(wait, old['record']), 'stale': old['at'], 'stale_error': str(exc)})
            else:
                result.append({**wait, 'question': '等待服务读取失败：' + str(exc), 'recommend': '—', 'reason': '', 'seconds': None})
    if cache is not None and not item_id:  # 不再等的不留旧数据
        live = {w['ref'] for w in log.all('waits') if w['state'] == 'waiting'}
        for ref in [r for r in cache if r not in live]:
            del cache[ref]
    return result


def shaped(wait, record):
    content = record['content']
    seconds = max(0, int((utcnow() - datetime.fromisoformat(record['started_at'])).total_seconds()))
    return {**wait, 'content': content, 'record': record, 'seconds': seconds,
            'question': content.get('question', '等待外部结果'),
            'recommend': content.get('recommend', '—'), 'reason': content.get('reason', '')}


def state_since(log, item):
    """这件活进入当前状态的时间（事实里最近一次改状态的时刻）；查不到返回 None，页面写「时长未知」。"""
    return log.state_since(item)


def origin(item, target, flow: Flow | None):
    """流程提供来源摘要；未实现或读取失败时退回活的登记名，不妨碍查看状态。"""
    describe = getattr(flow, "origin", None)
    if describe and target:
        try:
            result = describe(item, target)
            if isinstance(result, dict) and all(isinstance(result.get(k), str) for k in ("title", "url", "ref", "quote")):
                return result
        except Exception:
            pass
    return None


def overview(log, *, everything=False, states=(), targets=(), flows=(), rules=None, plugins: Plugins | None = None, wait_cache=None):
    """总览要用的数据，文字和网页共用。默认只取没结束的活；states、targets、flows 是筛选，空表示不筛。"""
    if rules is None:
        rules = (log.get("controls", "@rules") or {}).get("data", {})
    configured_targets = rules.get("targets", {})
    count = log.item_count()
    # 默认不列已结束的活：在库里先筛掉，不逐行解析它们（已结束的占绝大多数，解析一遍要 200 多毫秒）。
    items = log.all("items") if everything or states else log.open_items()
    # 守护进程在后台作业里替这件活跑东西（例如流程跑检查）时，状态显示作业的字样；每拍回查，跑完按结果结算。
    jobs = running_jobs(log.path.parent / "jobs")
    rows = []
    for item in items:
        target, flow = flow_for(configured_targets, item["target"], plugins, item.get("flow", ""))
        row = log.get("rounds", item.get("round", ""))
        control = log.get("controls", item["target"]) or {}
        stuck = item["state"] == "卡住"
        shown = "暂停" if control.get("paused") else jobs.get(item["id"], item["state"])
        next_step, blocker = item["next"], item["blocked_by"] or "—"
        rows.append({"id": item["id"], "title": item.get("title") or item["id"], "target": item["target"],
                     "priority": (item.get('relations') or {}).get('priority', 'P1'),
                     "flow": item.get("flow") or "", "stage": item["stage"], "state": item["state"], "shown": shown,
                     "stuck": stuck, "who": who(row, item), "next": next_step, "blocker": blocker,
                     "reason": item["blocked_by"], "advice": item["next"],
                     "retry_at": item.get("plugin_retry_at") or "",
                     "auto_retry": bool(item.get("plugin_blocked") and item.get("plugin_retry_at")),
                     "owner": (item.get("leave") or {}).get("owner") or "", "origin": origin(item, target, flow),
                     "due": (item.get("leave") or {}).get("at") or (item.get("after_wait") or {}).get("at") or "",
                     "about": (item.get("leave") or {}).get("about") or ""})
    filtered = bool(states or targets or flows)
    picked = [r for r in rows
              if (everything or states or r["state"] not in ENDED)
              and (not states or r["shown"] in states or r["state"] in states)
              and (not targets or r["target"] in targets)
              and (not flows or r["flow"] in flows)]
    for r in picked:
        r["since"] = state_since(log, r["id"])
    ids = {r["id"] for r in picked}
    # 交付对象停了新派发并写了原因（例如存量转换没成）：原因原样列出，在跑的和在等的不受影响。
    holds = [{"target": name, "reason": control["reason"]}
             for name in configured_targets
             if (control := log.get("controls", name) or {}).get("paused") and control.get("reason")
             and (not targets or name in targets)]
    isolated_sources = [control for control in log.all('controls', where=('broken', True))
                        if control['id'].startswith('@source:') and (not targets or control['target'] in targets)]
    return {"managers": read_managers(log.path.parent.resolve()), "rows": picked, "isolated_sources": isolated_sources,
            "total": count, "ended": count - sum(r["state"] not in ENDED for r in rows),
            "hidden": count - len(picked), "everything": everything, "filtered": filtered, "holds": holds,
            "waits": [w for w in waiting_records(log, rules, plugins, cache=wait_cache) if w['item'] in ids],
            "filters": {"states": list(states), "targets": list(targets), "flows": list(flows)}}


def who(row, item):
    """谁在推：有在跑的轮写执行者；没有时写车间，阶梯指定了接手者就一并写明（ws#301）。"""
    if row:
        return row["executor"]
    return f'车间（阶梯指定 {item["takeover"]}）' if item.get("takeover") else "车间"


def status(log, **options):
    view = overview(log, **options)
    rows = view["rows"]
    count = lambda state: sum(r["state"] == state for r in rows)
    head = f"活 {len(rows)} 件 · 跑着 {count('跑着')} · 等你 {count('等你')} · 卡住 {count('卡住')}"
    head += f" · 排队 {count('排队')}" if count("排队") else ""
    head += f" · 定时 {count('定时')}" if count("定时") else ""
    for waiting in ("等前置", "等子单", "等资料"):
        head += f" · {waiting} {count(waiting)}" if count(waiting) else ""
    if view["hidden"]:
        head += f"\n另有 {view['hidden']} 件没列出" + ("（筛掉的）" if view["filtered"] else "（已结束；workshop status --all 看全部）")
    lines = [head]
    for hold in view["holds"]:
        lines.append(f'{hold["target"]} 停了新派发：{one_line(hold["reason"])}')
    lines.append("活\t阶段\t状态\t谁在推\t下一步\t卡在谁手里\t优先级")
    for r in rows:
        lines.append("\t".join((r["id"], r["stage"], r["shown"], r["who"], r["next"], r["blocker"], r['priority'])))
    lines.append("卡住\n活\t阶段\t原因\t建议动作")
    for r in rows:
        if r["state"] == "卡住":
            advice = one_line(r["advice"]) + retry_note(r)
            lines.append("\t".join((r["id"], r["stage"], one_line(r["reason"]), advice)))
    lines.extend(source_isolation_line(c) for c in view['isolated_sources'])
    for heading, snapshot in sections(view).items():
        lines.extend([heading, *snapshot['lines']])
        if heading == '等你':
            lines.append('详情（等待号、版本、原因、推荐、等了多久）')
        if snapshot['details']:
            lines.extend(['详情', *snapshot['details']])
    return "\n".join(lines)


def one_line(text):
    return " ".join(str(text).split())


def source_isolation_line(control):
    next_step = f"下次自动重试 {control['retry_at']}" if control['auto_retry'] else '等项目经理'
    return (f"{control['target']} 来源 {control['plugin']} 自 {control['since']} 隔离，"
            f"{next_step}。最后错误：{one_line(control['reason'])}")


def retry_note(row):
    """自动重试的到点时间，本机时区；没有自动重试的计划就返回空串。"""
    if not row.get("auto_retry") or not row.get("retry_at"):
        return ""
    try:
        when = datetime.fromisoformat(row["retry_at"]).astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        when = row["retry_at"]
    return f"；车间自动重试，下次 {when}"


ACTIONS = {"update": "更新", "rework": "返工", "ready": "放行"}


def flow_for(targets, name, plugins: Plugins, selected=""):
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


def summarize(body, target, flow: Flow | None):
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
    return log.event_at(seq)


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
    for wait in waiting_records(log, rules or {}, plugins, item_id):
        lines.append('等待记录 ' + json.dumps(wait.get('record', wait), ensure_ascii=False))
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
        elif kind == "executor_handover":
            skipped = "、".join(f"第 {n} 级" for n in body["skipped"]) or "无"
            # 旧事实没有 ladder 字段；旧写法的阶梯名为空：都照原来的样子显示。
            named = f'阶梯 {body["ladder"]} 换人' if body.get("ladder") else '阶梯换人'
            detail = (f'{named} {body["from"]}（第 {body["from_level"]} 级）→ {body["to"]}（第 {body["to_level"]} 级），'
                      f'原因 {body["cause"]}：{body["reason"]}；跳过：{skipped}')
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


def usage(log, now=None, since=None, by=None, pr_stats_dir=None):
    """since 是 YYYY-MM-DD：「按执行者配置档」只算这一天（本地时间）及以后开工的活；不给时算最近 7 天开工的。

    by 是 kind、size、target 之一：这一段按活的类型再拆开。PR 概况直接读状态目录下的
    issue_delivery 状态文件（issue #300），不引用插件代码；pr_stats_dir 可以另指。
    """
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
    # 订阅和按量的轮分开算（核心 C4）；计费方式取执行者报的，没报的归「未知」。
    billing = lambda r: (r.get("usage") or {}).get("billing")
    for kind in dict.fromkeys(billing(r) for r in rows):
        label = {"subscription": "订阅", "metered": "按量"}.get(kind, display(kind))
        lines.append(summarize(f"按计费 {label}", [r for r in rows if billing(r) == kind]))
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
    if since is None:
        start = now - timedelta(days=7)
        scope = f"最近 7 天开工的活：{start:%Y-%m-%d %H:%M} 起"
    else:
        try:
            day = date.fromisoformat(since)
        except ValueError:
            raise ValueError(f"--since 要写成 YYYY-MM-DD，收到 {since}") from None
        start = datetime.combine(day, datetime.min.time()).astimezone()
        scope = f"{day.isoformat()} 起开工的活"
    buckets = profiles.buckets(log, by, pr_stats_dir or Path(log.path).parent / "issue_delivery") if by else None
    lines += profiles.render(profiles.profile_rows(log, start, by, buckets),
                             scope, by)
    return "\n".join(lines)
