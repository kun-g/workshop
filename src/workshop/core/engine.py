"""一轮一轮推进：外部副作用有持久标识；视图只经事实演进。"""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
import fcntl
import json
from pathlib import Path
import uuid

from .gates import decide, validate_stages
from .log import Log
from .models import Brief, Event, Receipt, Started, due, stamp, utcnow
from .ports import Unavailable


class Engine:
    def __init__(self, log: Log, rules, plugins, clock=utcnow):
        self.log, self.rules, self.plugins, self.clock = log, rules, plugins, clock

    @contextmanager
    def exclusive(self):
        with self.log.path.with_suffix(".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def fact(self, kind, body, item=None, **kwargs):
        return self.log.append(kind, body, item, at=stamp(self.clock()), **kwargs)

    def change(self, item, kind="item_changed", **changes):
        self.fact(kind, changes, item["id"])

    def control(self, target):
        return self.log.get("controls", target) or {}

    def ingest(self, event: Event) -> bool:
        event.validate()
        self.rules.target(event.target)
        with self.log.transaction():
            paused = self.control(event.target).get("paused", False)
            kind = "deferred_event" if paused else "event"
            added = self.fact(kind, asdict(event), event.item_id,
                              source=event.source, ext_id=event.ext_id)
            if added and not paused:
                item = self.log.get("items", event.item_id)
                if event.depth > self.rules.data["limits"]["chain_depth"]:
                    self.block(item, "事件连锁超过上限")
                elif self.control(event.target).get("broken"):
                    self.block(item, "流程插件等待修复")
            return added

    def pause(self, target):
        self.rules.target(target)
        with self.exclusive():
            self.fact("control", {"id": target, "paused": True})

    def missed(self, target):
        return [e for e in self.log.all("deferred") if not e["resolved"] and e["event"]["target"] == target]

    def resume(self, target, selected: list[str], expected: list[str] | None = None):
        self.rules.target(target)
        with self.exclusive(), self.log.transaction():
            missed = self.missed(target)
            if expected is not None and set(expected) != {e["id"] for e in missed}:
                raise ValueError("遗漏清单已变化，请重新查看后选择")
            if not set(selected).issubset({e["id"] for e in missed}):
                raise ValueError("选择中有未知事件；请重新查看遗漏清单")
            self.fact("control", {"id": target, "paused": False, "broken": False})
            for entry in missed:
                replay = entry["id"] in selected
                self.fact("deferred_resolved", {"id": entry["id"], "replayed": replay})
                if replay:
                    event = Event(**entry["event"])
                    self.fact("event", asdict(event), event.item_id,
                              source="resume", ext_id=entry["id"])
                    replayed = self.log.get("items", event.item_id)
                    if event.depth > self.rules.data["limits"]["chain_depth"]:
                        self.block(replayed, "事件连锁超过上限")
                    elif event.source == "timer":
                        timer = self.log.get("timers", event.ext_id)
                        if timer:
                            self.apply_timer(timer, replayed)
            # 恢复只解除插件故障；重试耗尽和连锁上限须由新决定处理。
            for item in self.log.all("items"):
                if item["target"] == target and item.get("plugin_blocked"):
                    self.change(item, state="空闲", blocked_by="", plugin_blocked=False, next="重试原轮")

    def submit(self, token: str, data: dict) -> bool:
        receipt = Receipt.parse(data, self.clock())
        for event in receipt.events:
            self.rules.target(event["target"])
        with self.log.transaction():
            row = self.log.get("rounds", token)
            if not row:
                raise ValueError("未知轮次令牌")
            if row.get("receipt") is not None:
                if row["receipt"] != data:
                    raise ValueError("该轮已交不同回执")
                return False
            if row["status"] not in ("starting", "running"):
                raise ValueError("该轮不能再交回执")
            return self.fact("receipt", {"token": token, "receipt": data}, row["item"],
                             source="receipt", ext_id=token)

    def card(self, item, key, mode, payload, **extra):
        card_id = f'{item["id"]}:{key}'
        if not self.log.get("cards", card_id):
            self.fact("card_created", dict(id=card_id, item=item["id"], mode=mode,
                      payload=payload, sent=False, answered=False, **extra), item["id"])
        return card_id

    def timer(self, item, key, at, about, kind="工序", owner="车间", base=None):
        timer_id = f'{item["id"]}:{key}'
        self.fact("timer_created", dict(id=timer_id, item=item["id"], target=item["target"],
                  at=due(at, base or self.clock()), about=about, kind=kind, owner=owner, fired=False), item["id"],
                  source="timer-plan", ext_id=timer_id)

    def block(self, item, reason, plugin=False):
        self.change(item, "work_blocked", state="卡住", next="等你介入", blocked_by=reason,
                    plugin_blocked=plugin, leave={"kind": "等你", "about": reason})

    def broken(self, target, port, name, exc):
        # 流程插件共享的所有类一起隔离；其他端口按本次受影响的交付对象隔离。
        targets = [target]
        if port == "flow":
            targets = [t for t, rule in self.rules.data["targets"].items() if rule["plugin"] == name]
        with self.log.transaction():
            for affected in targets:
                if not self.control(affected).get("broken"):
                    self.fact("plugin_failed", {"target": affected, "port": port, "name": name,
                                                 "error": repr(exc)})
                    self.fact("control", {"id": affected, "broken": True})
                    pseudo = {"id": f"@{affected}"}
                    self.card(pseudo, f"plugin:{port}:{name}", "notice", {"text": f"{affected} 插件故障：{exc}"})
                for item in self.log.all("items"):
                    if item["target"] == affected and item["state"] != "完成":
                        self.block(item, f"{port}/{name}: {exc}", plugin=True)

    def waiting(self, item):
        return any(c["item"] == item["id"] and c["mode"] == "ask" and not c["answered"]
                   for c in self.log.all("cards"))

    def sources(self):
        for target, rule in self.rules.data["targets"].items():
            if self.control(target).get("broken"):
                continue
            for name in rule.get("sources", []):
                key = f"{target}:{name}"
                cursor = (self.log.get("cursors", key) or {}).get("cursor")
                try:
                    batch = self.plugins.get("source", name).poll(self.rules.target(target), cursor)
                    with self.log.transaction():
                        for event in batch.events:
                            if event.target != target:
                                raise ValueError("来源不能跨交付对象写入")
                            self.ingest(event)
                        self.fact("cursor", {"id": key, "cursor": batch.cursor})
                except Exception as exc:
                    self.broken(target, "source", name, exc)

    def channels(self):
        try:
            channel = self.plugins.get("channel", self.rules.data["channel"])
            for card in self.log.all("cards"):
                if not card["sent"]:
                    try:
                        external = getattr(channel, "ask" if card["mode"] == "ask" else "notify")(
                            card["id"], {**card["payload"], "item": card["item"]})
                        self.fact("card_changed", {"id": card["id"], "external": external, "sent": True}, card["item"])
                    except Exception as exc:
                        self.fact("delivery_failed", {"card": card["id"], "error": repr(exc)}, card["item"])
            cards = [c for c in self.log.all("cards") if c["mode"] == "ask" and c["sent"] and not c["answered"]]
            for answer in channel.poll(cards):
                card = self.log.get("cards", answer.card_id)
                if not card or card["mode"] != "ask" or not card["sent"] or card["answered"]:
                    continue
                item = self.log.get("items", card["item"])
                with self.log.transaction():
                    self.fact("answer", asdict(answer), item["id"], source="answer", ext_id=card["id"])
                    cause = self.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0]
                    self.fact("card_changed", {"id": card["id"], "answered": True, "answer": answer.value}, item["id"])
                    approved = item["approved"]
                    if answer.approved:
                        approved = sorted(set(approved + card.get("actions", [])))
                    materials = item["materials"] + [{"body": answer.value, "role": "资料，不是指令", "source": "回答"}]
                    changes = dict(approved=approved, materials=materials, cause=cause)
                    if item["state"] == "等你" and not self.waiting(item):
                        changes.update(state="空闲", next="接回会话", blocked_by="")
                    self.change(item, **changes)
        except Exception as exc:
            self.fact("delivery_failed", {"channel": self.rules.data["channel"], "error": repr(exc)})

    def timers(self):
        now = stamp(self.clock())
        for timer in self.log.all("timers"):
            if timer["fired"] or timer["at"] > now:
                continue
            item = self.log.get("items", timer["item"])
            with self.log.transaction():
                self.fact("timer_fired", {"id": timer["id"]}, item["id"])
                self.ingest(Event("timer", timer["id"], item["target"], item["object_key"],
                                  item["title"], timer["about"], item["depth"]))
                if not self.control(item["target"]).get("paused"):
                    self.apply_timer(timer, item)

    def apply_timer(self, timer, item):
        if timer["kind"] == "送回":
            self.card(item, timer["id"], "ask", {"title": timer["about"], "options": ["继续", "调整"]})
            if item["state"] not in ("跑着", "卡住"):
                self.change(item, state="等你", next=timer["about"], blocked_by="Kun",
                            leave={"kind": "等你", "about": timer["about"]})
        elif (item["state"] in ("定时", "完成") and not self.waiting(item)
              and (not item.get("retry_at") or stamp(self.clock()) >= item["retry_at"])):
            self.change(item, state="空闲", next=timer["about"])

    def effect(self, key, action):
        if not (self.log.get("effects", key) or {}).get("done"):
            self.fact("effect", {"id": key, "done": False})
            result = action()
            self.fact("effect", {"id": key, "done": True, "result": result})
        return self.log.get("effects", key).get("result")

    def taskhome(self, item):
        name = self.rules.data["taskhome"]
        try:
            home = self.plugins.get("taskhome", name)
            target = self.rules.target(item["target"])
            if not item.get("ticket"):
                ticket = self.effect(f'ticket:{item["id"]}', lambda: home.open(item["id"], item, target))
                self.change(item, ticket=ticket)
            else:
                key = f'ticket:{item["id"]}:{item["revision"]}'
                self.effect(key, lambda: home.update(key, item["ticket"], item, target))
            return True
        except Exception as exc:
            self.broken(item["target"], "taskhome", name, exc)
            return False

    def active(self):
        return [r for r in self.log.all("rounds") if r["status"] != "ended"]

    def occupied(self):
        return sum(not row.get("stopped") for row in self.active())

    def launch(self, row):
        item = self.log.get("items", row["item"])
        executor = self.plugins.get("executor", row["executor"])
        brief = Brief(**row["brief"])
        if row["session"]:
            started = executor.resume(row["token"], row["session"], brief, item, row["config"])
            if started.session != row["session"]:
                raise ValueError("执行者没有接回原会话")
        else:
            started = executor.launch(row["token"], brief, item, row["config"])
        if not started.session or not started.handle:
            raise ValueError("执行者必须返回会话和运行标识")
        with self.log.transaction():
            latest = self.log.get("rounds", row["token"])
            self.fact("round_changed", {"token": row["token"], "started": asdict(started),
                       "status": "finishing" if latest.get("receipt") is not None else "running"}, item["id"])
            sessions = dict(item["sessions"])
            sessions[row["executor"]] = started.session
            changes = dict(session=started.session, sessions=sessions, worktree=started.worktree)
            if row["stage"] == "执行":
                changes["writer"] = row["config"]["family"]
            self.change(item, **changes)

    def reserve(self, item, unavailable=()):
        if item["depth"] > self.rules.data["limits"]["chain_depth"]:
            self.block(item, "事件连锁超过上限")
            return
        target = self.rules.target(item["target"])
        port, name = "flow", target["plugin"]
        try:
            flow = self.plugins.get(port, name)
            stages = flow.stages(target)
            validate_stages(stages)
            instructions = flow.brief(item, item["stage"], target)
            chosen = None
            for config in self.rules.candidates(item["stage"], item["writer"]):
                if config["use"] in unavailable:
                    continue
                port, name = "executor", config["use"]
                executor = self.plugins.get(port, name)
                try:
                    if executor.available(config):
                        chosen = config
                        break
                except Unavailable:
                    continue
            if chosen is None:
                self.card(item, f'capacity:{item["revision"]}', "ask", {"title": "现有订阅没有可用执行者", "options": ["恢复后继续"]})
                self.change(item, state="等你", next="等现有订阅恢复", blocked_by="Kun",
                            leave={"kind": "等你", "about": "执行者不可用"})
                return
            token = str(uuid.uuid4())
            brief = Brief(instructions, item["materials"])
            row = dict(token=token, item=item["id"], stage=item["stage"], executor=chosen["use"],
                       config=chosen, brief=asdict(brief), session=item["sessions"].get(chosen["use"]),
                       status="starting", at=stamp(self.clock()), cause=item["cause"], receipt=None, usage=None)
            with self.log.transaction():
                latest = self.log.get("items", item["id"])
                if (self.control(item["target"]).get("paused") or latest["state"] != "空闲"
                        or any(r["item"] == item["id"] for r in self.active())
                        or self.occupied() >= self.rules.data["limits"]["concurrent"]):
                    return
                self.fact("round_reserved", row, item["id"])
            try:
                self.launch(row)
            except Unavailable:
                # 端口保证这种错误没有启动副作用，才允许释放并换下一家。
                with self.log.transaction():
                    self.fact("round_changed", {"token": token, "status": "ended", "outcome": "unavailable",
                              "ended": stamp(self.clock())}, item["id"])
                    self.change(item, state="空闲", round=None, next="选择下一执行者")
                self.reserve(self.log.get("items", item["id"]), (*unavailable, chosen["use"]))
        except Exception as exc:
            self.broken(item["target"], port, name, exc)

    def finish(self, row):
        item = self.log.get("items", row["item"])
        token = row["token"]
        port, name = "executor", row["executor"]
        try:
            executor = self.plugins.get(port, name)
            started = Started(**row["started"])
            if not row.get("stopped"):
                executor.stop(token, started)
                self.fact("round_changed", {"token": token, "stopped": True}, item["id"])
            usage = row.get("usage")
            if not row.get("usage_checked"):
                try:
                    value = executor.usage(token, started)
                    if value is not None:
                        value.validate()
                        usage = asdict(value)
                except Exception as exc:
                    self.fact("usage_failed", {"token": token, "error": repr(exc)}, item["id"])
                self.fact("round_changed", {"token": token, "usage": usage, "usage_checked": True}, item["id"])
            received_at = datetime.fromisoformat(row["received"])
            receipt = Receipt.parse(row["receipt"], received_at)
            target = self.rules.target(item["target"])
            port, name = "flow", target["plugin"]
            flow = self.plugins.get(port, name)
            evidence = flow.assess(item, receipt, target)
            evidence.validate()
            stages = flow.stages(target)
            decision = decide(item, receipt, evidence, stages, self.rules)
            # 收尾也必须登记后面的工序、送回或等你；缺失就留在本阶段。
            missing_leave = decision.complete and not (receipt.leave or receipt.ask or receipt.later)
            if receipt.outcome == "done" and decision.allowed and not missing_leave and not receipt.ask:
                key = f"after:{token}"
                self.effect(key, lambda: flow.after(item, row["stage"], key, target))
            with self.log.transaction():
                for ask in receipt.ask:
                    self.card(item, f'{token}:{ask["key"]}', "ask", ask)
                for notice in receipt.notice:
                    self.card(item, f'notice:{notice["key"]}', "notice", notice)
                for later in receipt.later:
                    self.timer(item, f'{token}:{later["key"]}', later["at"], later["about"], base=received_at)
                if receipt.leave and receipt.leave["kind"] in ("工序", "送回"):
                    leave = receipt.leave
                    self.timer(item, f"leave:{token}", leave["at"], leave["about"], leave["kind"], leave.get("owner", "Kun"), base=received_at)
                changes = dict(failures=0, round=None, retry_at=None, cause=max(item["cause"], row["receipt_seq"]))
                if evidence.checks:
                    changes["checks"] = evidence.checks
                if receipt.outcome == "failed":
                    failures = item["failures"] + 1
                    changes["failures"] = failures
                    if failures == 3:
                        self.card(item, "failing", "notice", {"text": f'{item["title"]} 连续 3 次没跑成'})
                    if failures >= 10:
                        changes.update(state="卡住", next="等你介入", blocked_by="连续 10 次没跑成",
                                       leave={"kind": "等你", "about": "连续失败"})
                    else:
                        at = stamp(self.clock() + timedelta(seconds=min(300 * 2 ** (failures - 1), 3600)))
                        self.timer(item, f"retry:{token}", at, "重试这一轮")
                        changes.update(state="定时", next=at, retry_at=at, leave={"kind": "工序", "owner": "车间", "at": at, "about": "重试"})
                elif receipt.outcome == "done" and decision.allowed and not missing_leave and not receipt.ask:
                    changes.update(stage=decision.stage, state="完成" if decision.complete else "空闲",
                                   next="已收尾" if decision.complete else decision.stage, blocked_by="",
                                   leave=receipt.leave or {"kind": "工序", "at": stamp(self.clock()), "owner": "车间", "about": decision.stage})
                    if decision.stage == "执行" and item["stage"] != "说清楚":
                        changes["approved"] = []
                    self.fact("gate_passed", {"gate_reason": decision.reason}, item["id"])
                elif decision.needs_approval and receipt.outcome == "done":
                    self.card(item, f"approval:{token}", "ask",
                              {"title": decision.reason, "options": ["批准", "不批准"]}, actions=list(decision.needs_approval))
                    changes.update(state="等你", next=decision.reason, blocked_by="Kun", leave={"kind": "等你", "about": decision.reason})
                    self.fact("gate_blocked", {"gate_reason": decision.reason}, item["id"])
                elif receipt.ask or receipt.later or receipt.leave:
                    leave = receipt.leave
                    if not leave and receipt.later:
                        first = receipt.later[0]
                        leave = {"kind": "工序", "owner": "车间", "at": due(first["at"], received_at), "about": first["about"]}
                    changes.update(state="定时", next="等约好的时间", leave=leave)
                else:
                    reason = "停下必须登记工序、送回或等你" if missing_leave else decision.reason
                    self.card(item, f"gate:{token}", "ask", {"title": reason, "options": ["补充后继续"]})
                    changes.update(state="等你", next=reason, blocked_by="Kun", leave={"kind": "等你", "about": reason})
                    self.fact("gate_blocked", {"gate_reason": reason}, item["id"])
                if self.waiting(item) and changes["state"] != "卡住":
                    changes.update(state="等你", next="等回答", blocked_by="Kun", leave={"kind": "等你", "about": "等回答"})
                elif receipt.later and changes["state"] == "空闲":
                    changes.update(state="定时", next="等约好的时间")
                self.fact("round_changed", {"token": token, "status": "ended", "ended": stamp(self.clock()),
                           "outcome": receipt.outcome, "summary": receipt.summary}, item["id"])
                self.change(item, "round_finished", **changes)
                for index, data in enumerate(receipt.events):
                    child = Event(**{**data, "source": f"round:{token}", "ext_id": str(index), "depth": item["depth"] + 1})
                    self.ingest(child)
        except Exception as exc:
            self.broken(item["target"], port, name, exc)

    def tick(self):
        with self.exclusive():
            heartbeat = self.log.path.parent / "heartbeat.json"
            temporary = heartbeat.with_suffix(".tmp")
            temporary.write_text(json.dumps({"at": stamp(self.clock()), "active": self.occupied()}))
            temporary.replace(heartbeat)
            self.sources()
            self.channels()
            self.timers()
            for row in self.active():
                item = self.log.get("items", row["item"])
                if self.control(item["target"]).get("broken"):
                    continue
                if not row.get("started"):
                    try:
                        self.launch(row)
                    except Exception as exc:
                        self.broken(item["target"], "executor", row["executor"], exc)
                elif row["status"] == "finishing":
                    self.finish(row)
            for item in self.log.all("items"):
                control = self.control(item["target"])
                if control.get("paused") or control.get("broken"):
                    continue
                if (item["pending_rework"] and item["depth"] <= self.rules.data["limits"]["chain_depth"]
                        and not any(r["item"] == item["id"] for r in self.active())):
                    self.change(item, stage="执行" if item["checks"] else "说清楚", state="空闲",
                                pending_rework=False, approved=[], next="按新事件返工")
                    item = self.log.get("items", item["id"])
                if not self.taskhome(item):
                    continue
                item = self.log.get("items", item["id"])
                if item["state"] == "空闲" and self.occupied() < self.rules.data["limits"]["concurrent"]:
                    self.reserve(item)
            self.channels()
