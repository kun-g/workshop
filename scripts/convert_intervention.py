"""介入点存量转换（#176）：按交付对象分批，先预览、再执行、最后对账。

一次性迁移，放 scripts/、不进守护进程常驻代码（判据与界限 M7、协作方式 H4）。
每条旧记录的去向写成一条 `legacy_converted` 事实（source=cutover，ext_id=类别:原编号），和它带来的改动在同一事务里；
同一批再跑一次结果一样，中途中断后续跑只补没写的，不重做已写的。

旧记录和去向（介入点 R1–R13，issue #176 合并前 DoD 第 2 项）：
- 没答的普通问卡 → 介入点服务里的等待（保留问题和旧卡号的对应），不补发、不替 Kun 填答案；
- 没答的审批卡、核心批准 approved、pending_advance、billing_approved → 只留作历史，不当授权，关口重查；
- 冻结批准 frozen_approved → 作废，其它仍有效的关口重查；
- 送回定时、agent 约的非车间工序 → 活没结束的改成到点车间自动续跑，活已结束的取消（不再到点找 Kun）；
- 没发的通知 → 取消；结果不明的按原标识对账，查不到外部编号的不重发；
- 已发、已答、已作废的卡 → 历史，已结束；
- 介入点服务里的亲眼看、盯更新报告、求助 → 保留，结果不明的由服务按原送达号对账。

用法（--state-dir 默认 ~/.local/state/workshop）：
  convert_intervention.py inventory [--target T]   转换前清单
  convert_intervention.py preview   [--target T]   预览：还没转的每条旧记录要去哪
  convert_intervention.py run --target T            备份后执行这一批
  convert_intervention.py reconcile [--target T]   对账表：每条旧记录一行去向；有没转的退出码 1
  convert_intervention.py cutover                   上线时用：备份，先 workshop 后 grant-console，计时
  convert_intervention.py groups                    飞书群消息核对表（只读）
"""
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import sqlite3
import sys
import time

from workshop.core.log import Log
from workshop.core.models import stamp
from workshop.core.rules import Rules
from workshop.wait_client import make_wait

ORDER = ("workshop", "grant-console")
SOURCE = "cutover"
ENDED = ("完成", "放弃")


class Injected(RuntimeError):
    """隔离回放用的人为失败。"""


@dataclass
class Row:
    category: str
    id: str
    target: str
    item: str
    destination: str
    reason: str
    detail: dict = field(default_factory=dict)

    @property
    def key(self):
        return f"{self.category}:{self.id}"


def resident(value):
    """发版准备命令在临时 HOME 里跑；~ 按当前系统用户解析，读写的仍是现役状态。"""
    value = str(value)
    if value.startswith("~/"):
        return Path(pwd.getpwuid(os.getuid()).pw_dir) / value[2:]
    return Path(value)


def now():
    return stamp(datetime.now(timezone.utc))


def target_of(item_id):
    # 插件故障通知挂在 @<交付对象> 上。
    return item_id.split(":", 1)[0].removeprefix("@")


def converted(log):
    rows = log.db.execute("SELECT ext_id, body FROM facts WHERE source=? AND kind='legacy_converted'", (SOURCE,))
    return {ext_id: json.loads(body) for ext_id, body in rows}


def card_version(card):
    return hashlib.sha256(json.dumps(card["payload"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def channel_records(state_dir):
    """旧 ask-kun 通道落盘的发送意图；只读，按正文对账结果不明的卡。"""
    records = []
    for path in sorted((state_dir / "ask-kun-channel").glob("*.json")):
        try:
            records.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return records


def classify_card(card, item, records):
    """返回 (去向, 理由, 外部编号)；外部编号只在按原标识对账查到时有。"""
    item_state = (item or {}).get("state", "")
    payload = card.get("payload") or {}
    if card.get("mode") == "ask":
        if card.get("answered"):
            return "历史：已答", "已答的问卡只留作历史；回答不当授权中心的授权", None
        if card.get("cancelled") or card.get("rejected"):
            return "历史：已作废", "已作废或拒收的问卡只留作历史", None
        if payload.get("kind") == "approval":
            return "取消：审批卡作废", "旧审批卡不当授权；要授权的动作走授权中心原申请，关口重查", None
        if item is None or item_state in ENDED:
            return "取消：活已结束", f"活已{item_state or '不在'}，没答的问卡作废，不补发", None
        return "转等待", "没答的普通问卡转成介入点服务里的等待，不补发、不替 Kun 填答案", None
    if card.get("sent"):
        return "历史：已发", "已发的通知只留作历史", None
    if card.get("cancelled"):
        return "历史：已作废", "已作废的通知只留作历史", None
    if card.get("sending") or card.get("reconcile_failed"):
        external = card.get("external") or next(
            (r.get("external") for r in records if r.get("external") and (r.get("payload") or {}).get("subject") == payload.get("subject")
             and (r.get("payload") or {}).get("text") == payload.get("text")), None)
        if external:
            return "对账：已发", f"按原标识对账查到外部编号 {external}，记已发，不重发", external
        return "对账：取消", "按原标识对账查不到外部编号，结果不明，不重发，取消（介入点 R13 不再发通知）", None
    return "取消：过时通知", "没发的通用通知取消；进度和结果只在 workshop status 里看（介入点 R13）", None


def plan(log, state_dir, targets):
    """每条旧记录一行；已转的也列，用于对账。"""
    items = {i["id"]: i for i in log.all("items")}
    records = channel_records(state_dir)
    rows = []
    for card in log.all("cards"):
        target = target_of(card["item"])
        if target not in targets:
            continue
        item = items.get(card["item"])
        destination, reason, external = classify_card(card, item, records)
        category = "问卡" if card.get("mode") == "ask" else "通知"
        if category == "问卡" and (card.get("payload") or {}).get("kind") == "approval":
            category = "审批卡"
        rows.append(Row(category, card["id"], target, card["item"], destination, reason,
                        {"external": card.get("external") or external, "subject": (card.get("payload") or {}).get("subject", "")}))
    for item in items.values():
        if item["target"] not in targets:
            continue
        ended = item["state"] in ENDED
        for name, category in (("approved", "核心批准"), ("billing_approved", "billing_approved"),
                               ("frozen_approved", "frozen_approved")):
            value = item.get(name)
            if not value:
                continue
            if category == "frozen_approved":
                destination = "作废：冻结批准" if not ended else "历史：冻结批准"
                reason = "冻结路径关口已取消（介入点 R8）；批准作废，其它仍有效的关口重查"
            else:
                destination = "历史：不当授权" if ended else "历史：清掉，关口重查"
                reason = "旧核心批准只留作历史，不当授权中心的授权（介入点 R8）"
            rows.append(Row(category, item["id"], item["target"], item["id"], destination, reason, {"value": value}))
        if item.get("pending_advance"):
            rows.append(Row("pending_advance", item["id"], item["target"], item["id"],
                            "历史：清掉，关口重查" if not ended else "历史：活已结束",
                            "旧的待批推进不当授权；关口按授权中心原申请重查", {"value": item["pending_advance"]}))
    for timer in log.db.execute("SELECT body FROM timers WHERE json_extract(body,'$.fired') IS NOT 1"):
        timer = json.loads(timer[0])
        if timer["target"] not in targets:
            continue
        if not (timer["kind"] == "送回" or timer["kind"] == "工序" and timer["owner"] not in ("车间", "项目经理")):
            continue
        item = items.get(timer["item"])
        category = "送回定时" if timer["kind"] == "送回" else "非车间工序定时"
        if item is None or item["state"] in ENDED:
            destination, reason = "取消：活已结束", "活已结束，到点不再找 Kun（介入点 R10、R13）"
        else:
            destination, reason = "改成到点自动续跑", "到点由车间接着做，不找 Kun（介入点 R10）"
        rows.append(Row(category, timer["id"], timer["target"], timer["item"], destination, reason,
                        {"at": timer["at"], "about": timer["about"], "owner": timer["owner"]}))
    for control in log.all("controls"):
        if control.get("id", "").startswith("reconcile:") and (control.get("count") or control.get("at")):
            card_id = control["id"].removeprefix("reconcile:")
            target = target_of(card_id)
            if target in targets:
                rows.append(Row("通道对账记录", control["id"], target, card_id.rsplit(":", 1)[0],
                                "历史：通道已退役", "旧通道的对账计数清掉；卡片去向见同号的卡片行", {}))
    rows.sort(key=lambda r: (ORDER.index(r.target) if r.target in ORDER else 9, r.category, r.id))
    return rows


def intervention_rows(service_db, targets):
    """介入点服务里的送达：约定的亲眼看、盯更新报告、求助按已定规则保留，不在这里改。"""
    if not service_db or not Path(service_db).exists():
        return []
    db = sqlite3.connect(f"file:{service_db}?mode=ro", uri=True)
    rows = []
    try:
        for ident, body in db.execute("SELECT id, body FROM records WHERE kind='delivery'"):
            body = json.loads(body)
            owner = ((body.get("payload") or {}).get("item") or "")
            target = target_of(owner) if owner else "workshop"
            if target not in targets or body.get("state") in ("sent", "cancelled"):
                continue
            rows.append(Row("介入点送达", ident, target, owner, "保留", "约定的送达按原送达号由介入点服务对账，不补发、不重发",
                            {"state": body.get("state")}))
    finally:
        db.close()
    return rows


def wait_for(card, item):
    """旧问卡转等待的内容；和 issue 交付登记取舍时的请求逐字一致，原阶段恢复时同一操作号认回同一个等待。"""
    payload = card["payload"]
    key = "cutover:" + card["id"]
    choices = "、".join(f'{c.get("key")}={c.get("label")}' for c in payload.get("choices", []))
    value = {"key": card.get("key") or key, "version": card_version(card),
             "question": payload.get("decision") or payload.get("subject"),
             "reason": (payload.get("context") or payload.get("decision")) + (f"；旧卡选项：{choices}" if choices else ""),
             "recommend": payload.get("recommend") or "（旧卡没有推荐）"}
    hook_key = "after:" + key
    operation = "decision:" + item["id"] + ":" + hook_key
    receipt = {"outcome": "nothing", "summary": "旧问卡转成等待（#176）", "advance": None,
               "evidence": [json.dumps({"schema": "issue-delivery/v1", "decision": value}, ensure_ascii=False)],
               "later": [], "events": [], "leave": {"kind": "等你", "about": value["question"]}}
    return {**value, "stage": item["stage"]}, hook_key, operation, receipt


def apply(log, row, rules, service, inject):
    """一条旧记录的去向和它带来的改动同一事务；已写过就跳过。"""
    if inject is not None:
        inject()
    at = now()
    ref = None
    if row.destination == "转等待":
        card = log.get("cards", row.id)
        item = log.get("items", row.item)
        value, hook_key, operation, receipt = wait_for(card, item)
        provider = "intervention"
        config = dict(rules.data.get("wait_services", {}).get(provider) or {})
        if not config:
            raise RuntimeError("规则没有配 wait_services.intervention，问卡转不成等待")
        config["endpoint"] = str(resident(config["endpoint"]))
        # 稳定操作号：中断后重跑拿回同一个等待号。
        ref = service.create(operation, {"point": "decision", "owner": item["id"], "content": value}, config)
        row.detail.update(wait_ref=ref, version=value["version"], legacy_external=card.get("external"))
    with log.transaction():
        if log.db.execute("SELECT 1 FROM facts WHERE source=? AND ext_id=?", (SOURCE, row.key)).fetchone():
            return False
        if row.category in ("问卡", "审批卡", "通知") and not row.destination.startswith("历史"):
            card = log.get("cards", row.id)
            changes = {"id": row.id, "cancelled": True, "cancel_done": True, "awaiting_release": False,
                       "reconcile_failed": False, "converted": row.destination}
            if row.destination == "对账：已发":
                changes.update(cancelled=False, sent=True, sending=False, external=row.detail["external"])
            log.append("card_changed", changes, row.item, at=at)
            if row.destination in ("转等待", "取消：审批卡作废"):
                item = log.get("items", row.item)
                if row.destination == "转等待":
                    value, hook_key, operation, receipt = wait_for(card, item)
                    log.append("wait_bound", {"id": operation, "provider": "intervention", "ref": ref, "operation": operation,
                                              "item": item["id"], "stage": item["stage"], "state": "waiting", "token": None,
                                              "attempt": 0, "next_at": at}, item["id"], source="wait-bind", ext_id=operation, at=at)
                    log.append("item_changed", {"state": "等你", "next": "等回答", "blocked_by": "", "wait_for_ready": False,
                                                "after_wait": {"key": hook_key, "receipt": receipt, "to": item["stage"],
                                                               "complete": False, "at": at},
                                                "leave": {"kind": "等你", "about": value["question"]}}, item["id"], at=at)
                    log.append("control", {"id": "@legacy-card:" + row.id, "wait_ref": ref}, at=at)
                    if card.get("external"):
                        log.append("control", {"id": "@legacy-card:" + card["external"], "wait_ref": ref}, at=at)
                elif item and item["state"] in ("等你", "卡住") and not item.get("wait_id"):
                    log.append("item_changed", {"state": "空闲", "next": "旧审批卡作废，关口按授权中心重查", "blocked_by": "",
                                                "wait_for_ready": False}, item["id"], at=at)
        elif row.category in ("核心批准", "billing_approved", "frozen_approved", "pending_advance") and "清掉" in row.destination or row.destination == "作废：冻结批准":
            item = log.get("items", row.item)
            field = {"核心批准": "approved", "billing_approved": "billing_approved", "frozen_approved": "frozen_approved",
                     "pending_advance": "pending_advance"}[row.category]
            changes = {field: None if field == "pending_advance" else []}
            if item["state"] in ("等你", "卡住") and not item.get("wait_id") and item.get("pending_advance"):
                changes.update(state="空闲", next="旧批准不当授权，关口重查", blocked_by="", wait_for_ready=False)
            log.append("item_changed", changes, row.item, at=at)
        elif row.category in ("送回定时", "非车间工序定时"):
            timer = log.get("timers", row.id)
            log.append("timer_fired", {"id": row.id}, row.item, at=at)
            if row.destination == "改成到点自动续跑":
                new = row.id + ":cutover"
                log.append("timer_created", {**timer, "id": new, "kind": "工序", "owner": "车间", "fired": False,
                                             "about": timer["about"], "converted_from": row.id},
                           row.item, source="timer-plan", ext_id=new, at=at)
                row.detail["timer"] = new
        elif row.category == "通道对账记录":
            log.append("control", {"id": row.id, "count": 0, "at": None}, at=at)
        log.append("legacy_converted", {"category": row.category, "id": row.id, "target": row.target, "item": row.item,
                                        "destination": row.destination, "reason": row.reason, "detail": row.detail},
                   row.item, source=SOURCE, ext_id=row.key, at=at)
    return True


def backup(state_dir, directory, label):
    """sqlite 在线备份：守护进程还在写也拿到一致的副本。"""
    directory.mkdir(parents=True, exist_ok=True)
    stamp_ = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    outputs = []
    for name, path in (("workshop", state_dir / "workshop.db"),):
        out = directory / f"{name}.db.before-cutover-{label}-{stamp_}"
        source = sqlite3.connect(path)
        target = sqlite3.connect(out)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        outputs.append(out)
    return outputs


def hold(log, target, reason):
    """这一批没转成：停新派发，在跑的轮、程序作业和等待都保留，status 写明原因。"""
    current = log.get("controls", target) or {}
    if current.get("paused") and current.get("hold") != "cutover":
        return  # Kun 自己暂停的不覆盖；原因记在 @cutover 里。
    log.append("control", {"id": target, "paused": True, "hold": "cutover", "reason": reason})


def release(log, target):
    """修好后续跑成功：只解开本脚本停的那一下；暂停期间攒下的事件全部补上，和没停过一样。"""
    current = log.get("controls", target) or {}
    if current.get("hold") != "cutover":
        return []
    missed = [e for e in log.all("deferred") if not e["resolved"] and e["event"]["target"] == target]
    from workshop.core.engine import Engine  # 补事件沿用核心的恢复路径，不另写一份。
    from workshop.registry import Registry, load_rules
    saved = log.get("controls", "@rules")
    engine = Engine(log, load_rules(Path(os.environ["WORKSHOP_CUTOVER_RULES"])), Registry())
    engine.resume(target, [e["id"] for e in missed], [e["id"] for e in missed])
    if saved and log.get("controls", "@rules") != saved:
        # 建引擎会把发版检出里的规则记成「当前规则」；守护进程重启时自己记，这里还原，不留临时路径。
        log.append("control", saved)
    log.append("control", {"id": target, "hold": None, "reason": None})
    return [e["id"] for e in missed]


def batch(log, rules, state_dir, target, service, inject=None, out=print):
    rows = [r for r in plan(log, state_dir, (target,))]
    done = converted(log)
    todo = [r for r in rows if r.key not in done]
    log.append("control", {"id": "@cutover:" + target, "state": "running", "at": now(), "error": None})
    written = 0
    for row in todo:
        if apply(log, row, rules, service, inject):
            written += 1
    left = [r for r in plan(log, state_dir, (target,)) if r.key not in converted(log)]
    if left:
        raise RuntimeError(f"{target} 还有 {len(left)} 条旧记录没转")
    log.append("control", {"id": "@cutover:" + target, "state": "done", "at": now(), "error": None,
                           "records": len(rows), "written": written})
    resumed = release(log, target)
    out(f"{target}：旧记录 {len(rows)} 条，本次写 {written} 条，跳过已转 {len(rows) - len(todo)} 条"
        + (f"；解开停派发，补上暂停期间的事件 {len(resumed)} 件" if resumed else ""))
    return written


def fail(log, target, exc):
    reason = f"存量转换没成：{exc}；修好后重跑 scripts/convert_intervention.py run --target {target}"
    log.append("control", {"id": "@cutover:" + target, "state": "failed", "at": now(), "error": str(exc)})
    hold(log, target, reason)
    return reason


def injector(spec, target):
    """只给隔离回放用：<交付对象>:<写几条后>[:kill]。默认抛错（失败安全态）；kill 是进程被杀（中断续跑）。"""
    if not spec:
        return None
    name, count, *mode = spec.split(":")
    if name != target:
        return None
    left = [int(count)]

    def inject():
        if left[0] <= 0:
            if mode == ["kill"]:
                print(f"隔离回放：写了 {count} 条后进程被杀（--inject-failure {spec}）", flush=True)
                os.kill(os.getpid(), signal.SIGKILL)
            raise Injected(f"隔离回放人为失败（--inject-failure {spec}）")
        left[0] -= 1
    return inject


def table(rows, done):
    lines = ["交付对象\t类别\t原编号\t活\t去向\t理由"]
    for r in rows:
        mark = "" if r.key in done or r.category == "介入点送达" else "（未转）"
        lines.append(f"{r.target}\t{r.category}\t{r.id}\t{r.item}\t{r.destination}{mark}\t{r.reason}")
    return lines


def summary(rows, done=None):
    counts = {}
    for r in rows:
        key = (r.target, r.category, r.destination + ("" if done is None or r.key in done or r.category == "介入点送达" else "（未转）"))
        counts[key] = counts.get(key, 0) + 1
    return [f"{t}\t{c}\t{d}\t{n}" for (t, c, d), n in sorted(counts.items(), key=lambda kv: (ORDER.index(kv[0][0]) if kv[0][0] in ORDER else 9, kv[0]))]


def group_messages(log, switchboard_db):
    """飞书群入口停用（#176 DoD 12）的核对表：进过车间的每条群消息、它当时的处理和那件活的现状；接线台那头的车间投递。只读。"""
    lines = ["来源\t消息号\t群\t交付对象\t当时处理\t活\t活的现状\t去向"]
    classified = {}
    for owner, body in log.db.execute("SELECT item, body FROM facts WHERE kind='event_classified'"):
        body = json.loads(body)
        classified[body.get("event_seq")] = {**body, "item": owner}
    count = 0
    for seq, item, body in log.db.execute("SELECT seq, item, body FROM facts WHERE kind='event' AND source='cli:feishu' ORDER BY seq"):
        body = json.loads(body)
        result = classified.get(seq, {})
        owner = result.get("item") or item
        current = log.get("items", owner) if owner else None
        state = f'{current["stage"]}/{current["state"]}' if current else "—"
        ended = current is None or current["state"] in ENDED
        lines.append("\t".join(["车间", body["ext_id"], (body.get("body") or {}).get("chat_id", ""), body["target"],
                                 result.get("action", "未归类"), owner or "—", state,
                                 "已结束，不再处理" if ended else "活还开着：按普通活走，群消息不再推进它"]))
        count += 1
    pending = 0
    if switchboard_db and Path(switchboard_db).exists():
        db = sqlite3.connect(f"file:{switchboard_db}?mode=ro", uri=True)
        try:
            for key, result, data in db.execute("SELECT key, result, data FROM actions WHERE json_extract(data,'$.kind')='workshop' ORDER BY key"):
                status = (json.loads(result) or {}).get("status") if result else "待处理"
                pending += status != "done"
                args = (json.loads(data).get("args") or {})
                lines.append("\t".join(["接线台", args.get("message", key), args.get("chat", ""), "—", f"车间投递 {status}", "—", "—",
                                         "已投完" if status == "done" else "没投完：接线台改版后不再投，作废"]))
        finally:
            db.close()
    lines.append(f"车间收下的群消息 {count} 条；接线台未完成的车间投递 {pending} 条")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", default="~/.local/state/workshop")
    parser.add_argument("--intervention-db", default="~/.local/state/workshop-intervention/intervention.db")
    parser.add_argument("--rules", default=str(Path(__file__).resolve().parents[1] / "rules/workshop.yaml"))
    parser.add_argument("--backup-dir", help="默认在状态目录的 cutover-backups/")
    parser.add_argument("--inject-failure", help="只给隔离回放用：<交付对象>:<写几条后失败>")
    parser.add_argument("--switchboard-db", default="~/.local/state/switchboard/core.sqlite3", help="groups 用：接线台状态库，只读")
    parser.add_argument("command", choices=("inventory", "preview", "run", "reconcile", "cutover", "groups"))
    parser.add_argument("--target", action="append", help="交付对象；可重复；默认按 workshop、grant-console 顺序全部")
    parser.add_argument("--rows", action="store_true", help="逐条列出，不只列合计")
    args = parser.parse_args(argv)
    state_dir = resident(args.state_dir).resolve()
    os.environ["WORKSHOP_CUTOVER_RULES"] = str(Path(args.rules).expanduser().resolve())
    rules = Rules.load(Path(os.environ["WORKSHOP_CUTOVER_RULES"]))
    targets = tuple(args.target or [t for t in ORDER if t in rules.data["targets"]])
    service_db = resident(args.intervention_db)
    if args.command == "groups":
        log = Log.readonly(state_dir / "workshop.db")
        try:
            print("\n".join(group_messages(log, resident(args.switchboard_db))))
            return 0
        finally:
            log.close()
    log = Log(state_dir / "workshop.db")
    try:
        if args.command in ("inventory", "preview", "reconcile"):
            done = converted(log)
            rows = plan(log, state_dir, targets) + intervention_rows(service_db, targets)
            if args.command == "reconcile":
                # 已转的按写下的去向列（定时转完就不再是待触发，plan 里看不到了）；没转的按当前计划列。
                listed = {r.key for r in rows}
                rows += [Row(body["category"], body["id"], body["target"], body["item"], body["destination"],
                             body["reason"], body.get("detail", {}))
                         for key, body in done.items() if key not in listed and body["target"] in targets]
                rows = [Row(done[r.key]["category"], r.id, r.target, r.item, done[r.key]["destination"], done[r.key]["reason"],
                            done[r.key].get("detail", {})) if r.key in done else r for r in rows]
                rows.sort(key=lambda r: (ORDER.index(r.target) if r.target in ORDER else 9, r.category, r.id))
            if args.command == "preview":
                rows = [r for r in rows if r.key not in done]
            print(f"{args.command}：状态目录 {state_dir}，交付对象 {'、'.join(targets)}，共 {len(rows)} 条")
            print("交付对象\t类别\t去向\t条数")
            print("\n".join(summary(rows, done if args.command == "reconcile" else None)))
            if args.rows or args.command == "reconcile":
                print("\n".join(table(rows, done)))
            for target in targets:
                state = (log.get("controls", "@cutover:" + target) or {})
                if state:
                    print(f"{target} 转换状态：{state.get('state')}" + (f"（{state.get('error')}）" if state.get("error") else ""))
            if args.command == "reconcile":
                left = [r for r in rows if r.key not in done and r.category != "介入点送达"]
                print(f"未转 {len(left)} 条")
                return 1 if left else 0
            return 0
        service = make_wait()
        backups = resident(args.backup_dir) if args.backup_dir else state_dir / "cutover-backups"
        if args.command == "run":
            if len(targets) != 1:
                parser.error("run 一次只跑一个交付对象")
            target = targets[0]
            started = time.monotonic()
            print("备份：" + "、".join(str(p) for p in backup(state_dir, backups, target)))
            try:
                batch(log, rules, state_dir, target, service, injector(args.inject_failure, target))
            except Exception as exc:
                print(fail(log, target, exc))
                return 1
            finally:
                print(f"耗时 {time.monotonic() - started:.2f} 秒")
            return 0
        # cutover：前一批没成，后一批不跑；没跑的同样停新派发，写明原因。
        started = time.monotonic()
        done = converted(log)
        if all((log.get("controls", "@cutover:" + t) or {}).get("state") == "done" for t in targets) and not [
                r for r in plan(log, state_dir, targets) if r.key not in done]:
            # 以后每次发版都会走到这里：已转完就不再备份、不再写。
            print(f"已转完：{'、'.join(targets)} 没有剩下的旧记录（{time.monotonic() - started:.2f} 秒）")
            return 0
        print("备份：" + "、".join(str(p) for p in backup(state_dir, backups, "all")) + f"（{time.monotonic() - started:.2f} 秒）")
        failed = None
        for target in targets:
            if failed:
                print(fail(log, target, f"前一批 {failed} 没转成，本批没跑"))
                continue
            try:
                batch(log, rules, state_dir, target, service, injector(args.inject_failure, target))
            except Exception as exc:
                print(fail(log, target, exc))
                failed = target
        print(f"整段转换耗时 {time.monotonic() - started:.2f} 秒")
        # 没转成的对象已停新派发、写明原因；发版照常继续，另一个对象照常（#176 合并前 DoD 第 10 项）。
        return 0
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
