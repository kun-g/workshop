"""只读的状态、起因与用量查询。"""
from datetime import datetime, timedelta
from .models import utcnow


def status(log):
    items = log.all("items")
    running = sum(i["state"] == "跑着" for i in items)
    waiting = sum(i["state"] == "等你" for i in items)
    blocked = sum(i["state"] == "卡住" for i in items)
    lines = [f"活 {len(items)} 件 · 跑着 {running} · 等你 {waiting} · 卡住 {blocked}",
             "活\t阶段\t状态\t谁在推\t下一步\t卡在谁手里"]
    for item in items:
        row = log.get("rounds", item.get("round", ""))
        who = row["executor"] if row else "车间"
        control = log.get("controls", item["target"]) or {}
        state = "暂停" if control.get("paused") else item["state"]
        lines.append("\t".join((item["id"], item["stage"], state, who, item["next"], item["blocked_by"] or "—")))
    return "\n".join(lines)


def why(log, item_id):
    if not log.get("items", item_id):
        raise ValueError(f"找不到活：{item_id}")
    lines = [f"活 {item_id}"]
    for fact in log.facts(item_id):
        body = fact["body"]
        kind = fact["kind"]
        if kind == "event":
            detail = f'事件 {fact["source"]} {body["body"]}'
        elif kind == "round_reserved":
            detail = f'{body["stage"]} {body["executor"]} 一轮 {body["token"]} 起因：事实 {body["cause"]}'
        elif kind == "receipt":
            receipt = body["receipt"]
            detail = f'回执 {receipt["outcome"]} {receipt["summary"]}'
        elif kind in ("gate_blocked", "gate_passed"):
            detail = f'{"关口拦住" if kind == "gate_blocked" else "过关口"} {body.get("gate_reason", "")}'
        elif kind == "answer":
            detail = f'回答 {body["value"]}'
        elif kind == "work_blocked":
            detail = f'卡住 {body["blocked_by"]}'
        else:
            continue
        lines.append(f'{fact["at"]} {detail}')
    return "\n".join(lines)


def usage(log, now=None):
    now = (now or utcnow()).astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = log.all("rounds")

    def summarize(label, selected):
        known = [r["usage"] for r in selected if r.get("usage") is not None]
        return (f'{label}\t{len(selected)}\t{sum(u["tokens_in"] for u in known)} / '
                f'{sum(u["tokens_out"] for u in known)}\t${sum(u["cost"] for u in known):.4f}'
                f'\t没上报 {len(selected) - len(known)} 轮')

    lines = ["期间/活\t轮数\t输入/输出 token\t折合 USD\t未上报",
             summarize("今天", [r for r in rows if datetime.fromisoformat(r["at"]) >= today]),
             summarize("最近 7 天", [r for r in rows if datetime.fromisoformat(r["at"]) >= now - timedelta(days=7)])]
    for item_id in dict.fromkeys(r["item"] for r in rows):
        lines.append(summarize(f"按活 {item_id}", [r for r in rows if r["item"] == item_id]))
    return "\n".join(lines)
