"""一轮一轮推进：外部副作用有持久标识；视图只经事实演进。"""
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import fcntl
import json
from pathlib import Path
import uuid
import sys
import hashlib

from .gates import decide, validate_stages
from .log import Log
from .models import Brief, Event, Receipt, Started, due, stamp, utcnow
from .ports import Unavailable, Retryable
from .cards import question, notice, validate_question, validate_notice
from .models import Evidence, Usage, RoundUsage


class Engine:
    def __init__(self, log: Log, rules, plugins, clock=utcnow):
        self.log, self.rules, self.plugins, self.clock = log, rules, plugins, clock
        saved = self.log.get("controls", "@rules") or {}
        if saved.get("data") != rules.data or saved.get("path") != rules.path:
            self.fact("control", {"id": "@rules", "data": rules.data, "path": rules.path})

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

    def ingest(self, event: Event, round_token: str | None = None) -> bool:
        if round_token:
            row = self.log.get("rounds", round_token)
            if not row:
                raise ValueError("未知事件来源轮次令牌")
            origin = self.log.get("items", row["item"])
            event = replace(event, depth=origin["depth"] + 1)
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
                    self.block(item, "流程插件等待修复", plugin=True)
            return added

    def pause(self, target):
        self.rules.target(target)
        self.fact("control", {"id": target, "paused": True})

    def missed(self, target):
        return [e for e in self.log.all("deferred") if not e["resolved"] and e["event"]["target"] == target]

    def resume(self, target, selected: list[str], expected: list[str] | None = None):
        self.rules.target(target)
        with self.log.transaction():
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
                    restored = dict(item["before_plugin_failure"])
                    if restored["state"] == "等你" and not item.get("wait_for_ready") and not self.waiting(item):
                        restored.update(state="空闲", next="按已收到的回答继续", blocked_by="")
                    self.change(item, **restored, plugin_blocked=False, before_plugin_failure=None)

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
        if mode == "ask":
            validate_question(payload)
        else:
            validate_notice(payload)
        card_id = f'{item["id"]}:{key}'
        if not self.log.get("cards", card_id):
            self.fact("card_created", dict(id=card_id, item=item["id"], mode=mode,
                      payload=payload, sent=False, answered=False, sending=False, cancelled=False, key=key, **extra), item["id"])
        return card_id

    def timer(self, item, key, at, about, kind="工序", owner="车间", base=None):
        timer_id = f'{item["id"]}:{key}'
        self.fact("timer_created", dict(id=timer_id, item=item["id"], target=item["target"],
                  at=due(at, base or self.clock()), about=about, kind=kind, owner=owner, fired=False), item["id"],
                  source="timer-plan", ext_id=timer_id)

    def block(self, item, reason, plugin=False):
        with self.log.transaction():
            item = self.log.get("items", item["id"])
            saved = item.get("before_plugin_failure")
            if plugin and not item.get("plugin_blocked"):
                saved = {k: item.get(k) for k in ("state", "next", "blocked_by", "leave")}
            self.change(item, "work_blocked", state="卡住", next="等你介入", blocked_by=reason,
                        plugin_blocked=plugin, before_plugin_failure=saved, leave={"kind": "等你", "about": reason})

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
                    incident = self.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0]
                    self.fact("control", {"id": affected, "broken": True})
                    pseudo = {"id": f"@{affected}"}
                    self.card(pseudo, f"plugin:{port}:{name}:{incident}", "notice", notice(f"{affected} 插件故障：{exc}"))
                for item in self.log.all("items"):
                    if item["target"] == affected and item["state"] != "完成":
                        self.block(item, f"{port}/{name}: {exc}", plugin=True)

    def waiting(self, item):
        return any(c["item"] == item["id"] and c["mode"] == "ask" and not c["answered"] and not c.get("cancelled")
                   for c in self.log.all("cards"))

    def sources(self):
        for target, rule in self.rules.data["targets"].items():
            if self.control(target).get("broken"):
                continue
            for name in rule.get("sources", []):
                key = f"{target}:{name}"
                cursor = (self.log.get("cursors", key) or {}).get("cursor")
                if not self.retry_due(f"source:{key}"):
                    continue
                try:
                    batch = self.plugins.get("source", name).poll(self.rules.target(target), cursor)
                    with self.log.transaction():
                        for event in batch.events:
                            if event.target != target:
                                raise ValueError("来源不能跨交付对象写入")
                            self.ingest(event)
                        self.fact("cursor", {"id": key, "cursor": batch.cursor})
                    self.retry_clear(f"source:{key}")
                except Retryable as exc:
                    self.retry_failure(target, "source", name, f"source:{key}", exc)
                except Exception as exc:
                    self.broken(target, "source", name, exc)

    def delivery_error(self, key, exc, item=None):
        state = self.control(f"delivery:{key}")
        if not state.get("failed"):
            with self.log.transaction():
                self.fact("delivery_failed", {"key": key, "error": repr(exc)}, item)
                self.fact("control", {"id": f"delivery:{key}", "failed": True})

    def delivery_ok(self, key):
        if self.control(f"delivery:{key}").get("failed"):
            self.fact("control", {"id": f"delivery:{key}", "failed": False})

    def channels(self):
        name = self.rules.data["channel"]
        try:
            channel = self.plugins.get("channel", name)
            for card in self.log.all("cards"):
                try:
                    if card.get("cancelled"):
                        if not card.get("cancel_done"):
                            channel.cancel(card["id"], card.get("external"))
                            self.fact("card_changed", {"id": card["id"], "cancel_done": True}, card["item"])
                    elif not card["sent"]:
                        external = None
                        if card.get("sending"):
                            external = channel.reconcile(card["id"], card)
                            if external is None:
                                continue
                        if external in (None, "absent"):
                            self.fact("card_changed", {"id": card["id"], "sending": True}, card["item"])
                            external = getattr(channel, "ask" if card["mode"] == "ask" else "notify")(
                                card["id"], card["payload"])
                        if not isinstance(external, str) or not external:
                            raise ValueError("通道没有返回外部编号")
                        self.fact("card_changed", {"id": card["id"], "external": external, "sent": True}, card["item"])
                    self.delivery_ok(card["id"])
                except Exception as exc:
                    self.delivery_error(card["id"], exc, card["item"])
            cards = [c for c in self.log.all("cards") if c["mode"] == "ask" and c["sent"]
                     and not c["answered"] and not c.get("cancelled")]
            for answer in channel.poll(cards):
                with self.log.transaction():
                    card = self.log.get("cards", answer.card_id)
                    if not card or card["mode"] != "ask" or not card["sent"] or card["answered"] or card.get("cancelled"):
                        continue
                    # 回答单独追加，由 evolve 在最新资料上追加，不回写旧资料快照。
                    self.fact("answer", {**asdict(answer), "key": card["payload"].get("key", card["key"]),
                              "title": card["payload"]["decision"], "actions": card.get("actions", []), "billing_configs": card.get("billing_configs", [])},
                              card["item"], source="answer", ext_id=card["id"])
                    self.fact("card_changed", {"id": card["id"], "answered": True, "answer": answer.value}, card["item"])
                    item = self.log.get("items", card["item"])
                    if card.get("billing_configs") and not answer.approved:
                        self.change(item, state="等你", wait_for_ready=True, next="按量计费未获批准，等待放行", blocked_by="Kun",
                                    leave={"kind": "等你", "about": "调整执行者或授权后放行"})
                    elif item["state"] == "等你" and not self.waiting(item):
                        self.change(item, state="空闲", next="按回答继续", blocked_by="")
            self.delivery_ok(f"channel:{name}")
        except Exception as exc:
            self.delivery_error(f"channel:{name}", exc)

    def cancel_questions(self, item):
        for card in self.log.all("cards"):
            if card["item"] == item["id"] and card["mode"] == "ask" and not card["answered"] and not card.get("cancelled"):
                self.fact("card_changed", {"id": card["id"], "cancelled": True}, item["id"])

    def timers(self):
        now = stamp(self.clock())
        for timer in self.log.all("timers"):
            if timer["fired"] or timer["at"] > now:
                continue
            item = self.log.get("items", timer["item"])
            if self.control(item["target"]).get("broken"):
                continue
            with self.log.transaction():
                self.fact("timer_fired", {"id": timer["id"]}, item["id"])
                self.ingest(Event("timer", timer["id"], item["target"], item["object_key"],
                                  item["title"], timer["about"], item["depth"]))
                if not self.control(item["target"]).get("paused"):
                    self.apply_timer(timer, item)

    def apply_timer(self, timer, item):
        if timer["kind"] == "送回":
            self.card(item, timer["id"], "ask", question(timer["about"]))
            if item["state"] not in ("跑着", "卡住"):
                self.change(item, state="等你", next=timer["about"], blocked_by="Kun",
                            leave={"kind": "等你", "about": timer["about"]})
        elif timer["owner"] != "车间":
            self.card(item, timer["id"], "notice", notice(f'{timer["owner"]} 的工序到点：{timer["about"]}'))
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
        name = self.rules.data.get("taskhome")
        if not name:
            return True
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
            started = executor.resume(row["token"], row["session"], brief, item, row["config"], row["target"])
            if started.session != row["session"]:
                raise ValueError("执行者没有接回原会话")
        else:
            started = executor.launch(row["token"], brief, item, row["config"], row["target"])
        if not started.session or not started.handle:
            raise ValueError("执行者必须返回会话和运行标识")
        with self.log.transaction():
            latest = self.log.get("rounds", row["token"])
            self.fact("round_changed", {"token": row["token"], "started": asdict(started),
                       "status": "finishing" if latest.get("receipt") is not None else "running"}, item["id"])
            item = self.log.get("items", item["id"])
            sessions = dict(item["sessions"])
            sessions[row["executor"]] = started.session
            changes = dict(session=started.session, sessions=sessions, workdir=started.workdir)
            if row["stage"] == "执行":
                changes["writer"] = row["config"]["family"]
            self.change(item, **changes)
        self.retry_clear(f"start:{item['target']}:{row['executor']}")

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
            candidates = []
            metered = []
            for config in self.rules.candidates(item["stage"], item["target"], item["writer"]):
                if (config["use"] in unavailable or item.get("retry_executor") and config["use"] != item["retry_executor"]
                        or item["stage"] == "审查" and config["family"] in item.get("reviews", [])):
                    continue
                port, name = "executor", config["use"]
                executor = self.plugins.get(port, name)
                try:
                    available = executor.available(config)
                except Unavailable:
                    available = False
                if not available:
                    continue
                billing_key = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
                if config["billing"] == "metered" and billing_key not in item.get("billing_approved", []):
                    metered.append((billing_key, config))
                    continue
                candidates.append({"config": config, "available": True})
            if not candidates and metered:
                context = "没有可用的订阅候选；是否授权本件活使用这些按量计费候选：" + json.dumps([c for _, c in metered], ensure_ascii=False)
                payload = question("是否批准本件活使用按量计费执行者", approval=True)
                payload["context"] = context
                self.card(item, f'billing:{item["revision"]}', "ask", payload, billing_configs=[k for k, _ in metered])
                self.change(item, state="等你", next="按量计费先授权", blocked_by="Kun", leave={"kind": "等你", "about": "按量计费授权"})
                return
            history = [r for r in self.log.all("rounds") if r["item"] == item["id"]]
            selection_target = {**target, "selection": {"writer": item["writer"], "reviewed": item.get("reviews", []),
                                "retry_executor": item.get("retry_executor"), "excluded": list(unavailable)}}
            selector_name = self.rules.selector(item["stage"], item["target"])
            port, name = "selector", selector_name
            choice = self.plugins.get(port, name).select(selection_target, item["stage"], len(history) + 1, history, candidates)
            chosen = choice.config if choice is not None else None
            if chosen is not None:
                # 校验输出权限边界，不在核心另写挑选策略。
                if (not any(c["config"] == chosen and c["available"] for c in candidates)
                        or not isinstance(choice.reason, str) or not choice.reason.strip()
                        or chosen["use"] in unavailable
                        or item.get("retry_executor") and chosen["use"] != item["retry_executor"]
                        or item["stage"] in ("审查", "验收") and chosen["family"] == item["writer"]
                        or item["stage"] == "审查" and chosen["family"] in item.get("reviews", [])):
                    reason = "挑选逻辑返回了规则不允许的候选或缺少理由"
                    self.fact("selection_failed", {"reason": reason, "selector": selector_name}, item["id"])
                    raise Retryable(reason)
            if chosen is None:
                self.card(item, f'capacity:{item["revision"]}', "ask", question("现有订阅没有可用执行者"))
                self.change(item, state="等你", next="等现有订阅恢复", blocked_by="Kun",
                            leave={"kind": "等你", "about": "执行者不可用"})
                return
            port, name = "executor", chosen["use"]
            token = str(uuid.uuid4())
            brief = Brief(instructions, [m for m in item["materials"] if m["role"] != "Kun 的回答"],
                          answers=[m for m in item["materials"] if m["role"] == "Kun 的回答"])
            target = {**target, "runtime": self.runtime(token)}
            row = dict(token=token, item=item["id"], stage=item["stage"], executor=chosen["use"],
                       config=chosen, requested={"executor": chosen["use"], "model": chosen.get("model"), "effort": chosen.get("effort")}, selector=selector_name, selection_reason=choice.reason, target=target, deadline=stamp(self.clock() + timedelta(seconds=self.rules.data["limits"].get("round_timeout_seconds", 7200))), brief=asdict(brief), session=item["sessions"].get(chosen["use"]),
                       status="starting", at=stamp(self.clock()), cause=item["cause"], receipt=None, usage=asdict(Usage()), usage_by_model=RoundUsage().entries(), children_reported=False, passed=None, elapsed_seconds=None)
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
        except Retryable as exc:
            self.start_retry(item, locals().get("row"), port, name, exc)
        except Exception as exc:
            self.broken(item["target"], port, name, exc)

    def runtime(self, token):
        state_dir = str(self.log.path.parent.resolve())
        command = [sys.executable, "-m", "workshop.cli", "--state-dir", state_dir]
        environment = {"WORKSHOP_STATE_DIR": state_dir, "WORKSHOP_ROUND_TOKEN": token}
        if self.rules.path:
            command += ["--rules", self.rules.path]
            environment["WORKSHOP_RULES"] = self.rules.path
        return {"state_dir": state_dir, "rules_path": self.rules.path, "command": command, "env": environment}

    def retry_due(self, key):
        return (self.control(f"retry:{key}").get("at") or "") <= stamp(self.clock())

    def retry_clear(self, key):
        if self.control(f"retry:{key}").get("count"):
            self.fact("control", {"id": f"retry:{key}", "count": 0, "at": None})

    def retry_failure(self, target, port, name, key, exc):
        count = self.control(f"retry:{key}").get("count", 0) + 1
        at = stamp(self.clock() + timedelta(seconds=min(300 * 2 ** (count - 1), 3600)))
        self.fact("plugin_retry", {"target": target, "port": port, "name": name, "count": count, "error": str(exc)})
        self.fact("control", {"id": f"retry:{key}", "count": count, "at": at})
        if count >= 3:
            self.broken(target, port, name, exc)
        return at

    def start_retry(self, item, row, port, name, exc):
        with self.log.transaction():
            if row and row["status"] == "starting":
                self.fact("round_changed", {"token": row["token"], "status": "ended", "stopped": True,
                          "outcome": "failed", "summary": str(exc), "ended": stamp(self.clock())}, item["id"])
                self.fact("round_result", {"token": row["token"], "stage": row["stage"], "outcome": "failed",
                          "summary": str(exc), "evidence": [], "reason": "暂时启动失败"}, item["id"])
            # 先登记工序，再隔离，故障解除时仍保持这条退避安排。
            count = self.control(f"retry:start:{item['target']}:{name}").get("count", 0) + 1
            at = stamp(self.clock() + timedelta(seconds=min(300 * 2 ** (count - 1), 3600)))
            self.timer(item, f"start:{uuid.uuid4()}", at, "重试启动")
            self.change(item, state="定时", round=None, next=at, retry_at=at,
                        leave={"kind": "工序", "owner": "车间", "at": at, "about": "重试启动"})
            self.retry_failure(item["target"], port, name, f"start:{item['target']}:{name}", exc)

    def abort(self, token, reason="人手动结束这一轮"):
        with self.log.transaction():
            row = self.log.get("rounds", token)
            if not row or row["status"] == "ended":
                raise ValueError("没有可结束的轮")
            self.fact("round_changed", {"token": token, "abort_requested": reason}, row["item"])

    def check_round(self, row):
        if row["status"] == "finishing":
            return
        reason = row.get("abort_requested")
        if not reason and row.get("deadline", "9999") <= stamp(self.clock()):
            reason = "这一轮超过规则期限"
        if not reason:
            try:
                state = self.plugins.get("executor", row["executor"]).poll(row["token"], Started(**row["started"]) if row.get("started") else None)
                if state not in ("running", "exited"):
                    raise ValueError("执行者返回了未知存活状态")
                if state == "exited":
                    reason = "执行者已退出但没有交回执"
            except Retryable as exc:
                self.delivery_error(f"poll:{row['token']}", exc, row["item"])
                return
        if reason:
            self.submit(row["token"], {"outcome": "failed", "summary": reason})
            self.fact("round_changed", {"token": row["token"], "automatic_failure": True}, row["item"])

    def required_reviews(self, item, evidence, target):
        matched = any(path.startswith(prefix) for path in evidence.paths for prefix in target.get("two_reviewers", []))
        return max(item.get("review_required", 1), evidence.reviewers, 2 if matched else 1)

    def failure_changes(self, item, row):
        failures = item["failures"] + 1
        episode = item.get("failure_episode") if item["failures"] else row["token"]
        changes = {"failures": failures, "failure_episode": episode}
        if failures == 3:
            self.card(item, f"failing:{episode}", "notice", notice(f'{item["title"]} 连续 3 次没跑成'))
        if failures >= 10:
            changes.update(state="卡住", next="等你介入", blocked_by="连续 10 次没跑成",
                           leave={"kind": "等你", "about": "连续失败"})
        else:
            at = stamp(self.clock() + timedelta(seconds=min(300 * 2 ** (failures - 1), 3600)))
            self.timer(item, f"retry:{row['token']}", at, "重试这一轮")
            changes.update(state="定时", next=at, retry_at=at,
                           leave={"kind": "工序", "owner": "车间", "at": at, "about": "重试"})
        return changes

    def gate_retry(self, item, row, reason):
        reason = reason or "请补充阶段结果与下一步"
        signature = f'{item["stage"]}:{row["receipt"].get("advance")}:{reason}'
        count = item.get("gate_count", 0) + 1 if item.get("gate_signature") == signature else 1
        changes = dict(gate_signature=signature, gate_count=count, gate_reason=reason,
                       retry_executor=row["executor"], state="空闲", next=reason,
                       leave={"kind": "工序", "owner": "车间", "at": stamp(self.clock()), "about": reason})
        self.fact("gate_blocked", {"gate_reason": reason}, item["id"])
        if count >= 3:
            self.card(item, f"gate:{row['token']}", "ask", question(reason))
            changes.update(state="等你", next=reason, blocked_by="Kun", leave={"kind": "等你", "about": reason})
        return changes

    def finish(self, row):
        item = self.log.get("items", row["item"])
        token = row["token"]
        port, name = "executor", row["executor"]
        try:
            executor = self.plugins.get(port, name)
            started = Started(**row["started"]) if row.get("started") else None
            if not row.get("stopped"):
                executor.stop(token, started)
                self.fact("round_changed", {"token": token, "stopped": True}, item["id"])
            if not row.get("usage_checked"):
                value = None
                try:
                    value = executor.usage(token, started, row["at"])
                    if value is not None:
                        value.validate()
                except Exception as exc:
                    value = None
                    self.fact("usage_failed", {"token": token, "error": repr(exc)}, item["id"])
                measured = value or RoundUsage()
                self.fact("round_changed", {"token": token, "usage": asdict(measured.main),
                          "usage_by_model": measured.entries(), "children_reported": measured.children_reported,
                          "usage_checked": True}, item["id"])
            received_at = datetime.fromisoformat(row["received"])
            receipt = Receipt.parse(row["receipt"], received_at)
            target = self.rules.target(item["target"])
            port, name = "flow", target["plugin"]
            flow = self.plugins.get(port, name)
            evidence = (Evidence(**row["assessment"]) if row.get("assessment") else
                        Evidence() if row.get("automatic_failure") else flow.assess(item, receipt, target))
            evidence.validate()
            stages = flow.stages(target)
            decision = decide(item, receipt, evidence, stages, self.rules)
            self.fact("round_changed", {"token": token, "assessment": asdict(evidence)}, item["id"])
            required = self.required_reviews(item, evidence, target)
            reviews = list(item.get("reviews", []))
            reviewing = item["stage"] == "审查" and evidence.passed and receipt.outcome == "done" and not receipt.ask
            if reviewing and row["config"]["family"] not in reviews:
                reviews.append(row["config"]["family"])
            another_review = reviewing and len(reviews) < required
            rejected = item["stage"] == "验收" and not evidence.passed and not row.get("automatic_failure")
            wait_ready = receipt.leave and receipt.leave["kind"] == "等你" and not receipt.ask
            missing_leave = decision.complete and not (receipt.leave or receipt.ask or receipt.later)
            advancing = (receipt.outcome == "done" and decision.allowed and not missing_leave
                         and not receipt.ask and not wait_ready and not another_review and not rejected)
            if advancing:
                key = f"after:{token}"
                self.effect(key, lambda: flow.after(item, row["stage"], key, target))
            with self.log.transaction():
                item = self.log.get("items", item["id"])
                self.fact("round_result", {"token": token, "stage": row["stage"], "outcome": receipt.outcome,
                          "summary": receipt.summary, "evidence": receipt.evidence, "reason": evidence.reason},
                          item["id"], source="result", ext_id=token)
                for ask in receipt.ask:
                    self.card(item, f'{token}:{ask["key"]}', "ask", ask)
                for entry in receipt.notice:
                    self.card(item, f'notice:{entry["key"]}', "notice", entry)
                for later in receipt.later:
                    self.timer(item, f'{token}:{later["key"]}', later["at"], later["about"], base=received_at)
                if receipt.leave and receipt.leave["kind"] in ("工序", "送回"):
                    leave = receipt.leave
                    self.timer(item, f"leave:{token}", leave["at"], leave["about"], leave["kind"], leave.get("owner", "Kun"), base=received_at)
                changes = dict(failures=0, round=None, retry_at=None, cause=max(item["cause"], row["receipt_seq"]),
                               retry_executor=None, review_required=required, reviews=reviews)
                if evidence.checks:
                    changes["checks"] = evidence.checks
                if rejected:
                    changes.update(stage="执行", state="空闲", next="验收不过，退回执行", approved=[], reviews=[],
                                   review_required=1, pending_advance=None, gate_count=0,
                                   leave={"kind": "工序", "owner": "车间", "at": stamp(self.clock()), "about": "修正验收问题"})
                elif receipt.outcome == "failed":
                    changes.update(self.failure_changes(item, row))
                elif wait_ready:
                    changes.update(state="等你", wait_for_ready=True, next="等待放行事件", blocked_by="Kun", leave=receipt.leave)
                elif another_review:
                    changes.update(stage="审查", state="空闲", next="另一家审查", gate_count=0,
                                   leave={"kind": "工序", "owner": "车间", "at": stamp(self.clock()), "about": "另一家审查"})
                elif advancing:
                    changes.update(stage=decision.stage, state="完成" if decision.complete else "空闲",
                                   next="已收尾" if decision.complete else decision.stage, blocked_by="", gate_count=0,
                                   leave=receipt.leave or {"kind": "工序", "at": stamp(self.clock()), "owner": "车间", "about": decision.stage})
                    if decision.stage == "执行" and item["stage"] != "说清楚":
                        changes.update(approved=[], reviews=[], review_required=1)
                    self.fact("gate_passed", {"gate_reason": decision.reason}, item["id"])
                elif decision.needs_approval and receipt.outcome == "done" and not receipt.ask:
                    self.card(item, f"approval:{token}", "ask", question(decision.reason, approval=True), actions=list(decision.needs_approval))
                    changes.update(state="等你", next=decision.reason, blocked_by="Kun", leave={"kind": "等你", "about": decision.reason},
                                   pending_advance={"token": token, "from": item["stage"], "to": receipt.advance,
                                                    "evidence": asdict(evidence), "receipt": row["receipt"], "stages": list(stages)})
                    self.fact("gate_blocked", {"gate_reason": decision.reason}, item["id"])
                elif receipt.ask or receipt.later or receipt.leave:
                    leave = receipt.leave
                    if not leave and receipt.later:
                        first = receipt.later[0]
                        leave = {"kind": "工序", "owner": "车间", "at": due(first["at"], received_at), "about": first["about"]}
                    changes.update(state="定时", next="等约好的时间", leave=leave)
                elif receipt.outcome == "nothing":
                    at = stamp(self.clock() + timedelta(minutes=5))
                    self.timer(item, f"nothing:{token}", at, "没有新结果，稍后再看")
                    changes.update(state="定时", next=at, leave={"kind": "工序", "owner": "车间", "at": at, "about": "稍后再看"})
                else:
                    reason = "停下必须登记工序、送回或等你" if missing_leave else decision.reason
                    changes.update(self.gate_retry(item, row, reason))
                if self.waiting(item) and changes["state"] != "卡住":
                    changes.update(state="等你", next="等回答", blocked_by="Kun", leave={"kind": "等你", "about": "等回答"})
                elif receipt.later and changes["state"] == "空闲":
                    changes.update(state="定时", next="等约好的时间")
                self.fact("round_changed", {"token": token, "status": "ended", "ended": stamp(self.clock()),
                           "outcome": receipt.outcome, "summary": receipt.summary,
                           "passed": evidence.passed if row["stage"] in ("审查", "验收") and receipt.outcome != "nothing" and not row.get("automatic_failure") else None}, item["id"])
                if item.get("plugin_blocked") and self.control(item["target"]).get("broken"):
                    changes["before_plugin_failure"] = {k: changes.get(k, item.get(k)) for k in ("state", "next", "blocked_by", "leave")}
                    changes.update(state="卡住", next="修复插件后继续", blocked_by=item["blocked_by"])
                self.change(item, "round_finished", **changes)
                for index, data in enumerate(receipt.events):
                    self.ingest(Event(**{**data, "source": f"round:{token}", "ext_id": str(index), "depth": item["depth"] + 1}))
        except Retryable as exc:
            self.delivery_error(f"finish:{token}", exc, item["id"])
        except Exception as exc:
            self.broken(item["target"], port, name, exc)

    def complete_approval(self, item):
        pending = item.get("pending_advance")
        if not pending or self.waiting(item) or item["state"] != "空闲":
            return
        receipt = Receipt.parse(pending["receipt"], self.clock())
        evidence = Evidence(**pending["evidence"])
        decision = decide(item, receipt, evidence, pending["stages"], self.rules)
        if not decision.allowed:
            self.change(item, pending_advance=None, reviews=[], gate_reason="授权未获批准，按回答调整")
            return
        target = self.rules.target(item["target"])
        try:
            flow = self.plugins.get("flow", target["plugin"])
            key = f"after:{pending['token']}"
            with self.log.transaction():
                latest = self.log.get("items", item["id"])
                if latest["pending_rework"] or latest["stage"] != pending["from"]:
                    return
                if not self.log.get("effects", key):
                    self.fact("effect", {"id": key, "done": False})
            self.effect(key, lambda: flow.after(item, pending["from"], key, target))
            with self.log.transaction():
                latest = self.log.get("items", item["id"])
                if latest["pending_rework"]:
                    return
                self.change(latest, "gate_passed", stage=decision.stage, state="空闲", next=decision.stage,
                            pending_advance=None, gate_count=0, gate_reason="已按原证据授权推进")
        except Exception as exc:
            self.broken(item["target"], "flow", target["plugin"], exc)

    def tick(self):
        try:
            self._tick()
        except Exception as exc:
            try:
                self.fact("tick_failed", {"error": repr(exc)})
            except Exception as recording_error:
                print(f"车间 tick 失败且暂时无法记录：{exc!r}; {recording_error!r}", file=sys.stderr)

    def _tick(self):
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
                try:
                    expired = row.get("deadline", "9999") <= stamp(self.clock())
                    if not row.get("started") and not row.get("abort_requested") and not expired:
                        if self.control(item["target"]).get("broken"):
                            continue
                        self.launch(row)
                        row = self.log.get("rounds", row["token"])
                    self.check_round(row)
                    row = self.log.get("rounds", row["token"])
                    if row["status"] == "finishing":
                        self.finish(row)
                except Retryable as exc:
                    self.start_retry(item, row, "executor", row["executor"], exc)
                except Exception as exc:
                    self.broken(item["target"], "executor", row["executor"], exc)
            for item in self.log.all("items"):
                control = self.control(item["target"])
                if control.get("paused") or control.get("broken"):
                    continue
                if (item["pending_rework"] and item["depth"] <= self.rules.data["limits"]["chain_depth"]
                        and not any(r["item"] == item["id"] for r in self.active())):
                    with self.log.transaction():
                        self.cancel_questions(item)
                        self.change(item, stage="执行" if item["checks"] else "说清楚", state="空闲",
                                    pending_rework=False, approved=[], pending_advance=None, reviews=[], review_required=1,
                                    wait_for_ready=False, retry_executor=None, gate_count=0, next="按新事件返工")
                    item = self.log.get("items", item["id"])
                self.complete_approval(item)
                item = self.log.get("items", item["id"])
                if not self.taskhome(item):
                    continue
                item = self.log.get("items", item["id"])
                if item["state"] == "空闲" and self.occupied() < self.rules.data["limits"]["concurrent"]:
                    self.reserve(item)
            self.channels()
