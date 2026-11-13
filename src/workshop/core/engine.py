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
import time

from .gates import advance_problem, decide, overlaps, validate_stages
from .models import STAGES as ALL_STAGES
from .log import Log
from .models import Brief, Event, Receipt, STAGES, Started, due, stamp, utcnow
from .ports import Unavailable, Retryable, ChannelRejected
from .cards import frozen_question, question, notice, validate_question, validate_notice
from .models import After, Answer, FlowBrief, Classification, Evidence, Release, Usage, RoundUsage
from .jobs import Pending


def gate(ingress):
    """来源轮询的事件要放行（ready）才能建活；外部入口经流程判定、agent 连锁事件不受此限。
    标记写进事实，旧事实没有标记，重建视图时照旧投影。"""
    return {"release_gate": True} if ingress == "source" else {}


def finder(log, target):
    """给流程判定用的只读查找：按外部编号找卡，或按对象键（先解析别名）找同目标的活。"""
    def resolve(object_key):
        alias = log.get("aliases", f"{target}:{object_key}")
        return alias["object_key"] if alias else object_key

    def find(external=None, *, object_key=None):
        if object_key is not None:
            found = log.get("items", f"{target}:{resolve(object_key)}")
            return {"item": found} if found else None
        matches = [c for c in log.all("cards") if c.get("external") == external]
        if len(matches) != 1:
            return None
        item = log.get("items", matches[0]["item"])
        return {"card": matches[0], "item": item} if item and item["target"] == target else None
    return find


class Engine:
    def __init__(self, log: Log, rules, plugins, clock=utcnow, jobs=None):
        """jobs 为空时插件调用就地进行（测试和单次命令）；守护进程传入作业，一拍里不等任何外部调用。"""
        self.log, self.rules, self.plugins, self.clock, self.jobs = log, rules, plugins, clock, jobs
        self.restart_requested = False
        self.tick_failures = self.control("@tick").get("failures", 0)
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

    def call(self, port, name, method, *args, identity=None, item=None, lookup=None):
        """调一次插件。有作业时：结果已出就返回，没出就登记或认回作业并抛 Pending，下一拍再来取。"""
        if self.jobs is None:
            if lookup is not None:
                args = (*args, finder(self.log, lookup))
            return getattr(self.plugins.get(port, name), method)(*args)
        return self.jobs.call(port, name, method, args, identity=identity, item=item, lookup=lookup)

    def resolve_key(self, target, object_key):
        alias = self.log.get("aliases", f"{target}:{object_key}")
        return alias["object_key"] if alias else object_key

    def find_external(self, target, external):
        return finder(self.log, target)(external)

    def after(self, item, stage, key, target, *, recheck=False):
        def invoke():
            # 同一完成钩子每次回查是一次新调用；回查次数进身份，免得认回上一次的结果。
            result = self.call("flow", target["plugin"], "after", item, stage, key, target,
                               identity=[key, item.get("after_pending_count", 0)], item=item["id"])
            return asdict(result) if isinstance(result, After) else result
        if recheck and (self.log.get("effects", key) or {}).get("done"):
            self.fact("effect", {"id": key, "done": False})
        value = self.effect(key, invoke)
        result = After(**value) if isinstance(value, dict) else After(aliases=value or [])
        if (type(result.pending) is not bool or type(result.rerun) is not bool or not isinstance(result.reason, str)
                or not isinstance(result.next, str) or result.pending and (not result.reason or not result.next)):
            raise ValueError("仍留本阶段须有原因和下一步")
        if type(result.restart) is not bool or not isinstance(result.notice, str):
            raise ValueError("重启标记须是布尔值，通知须是字符串")
        if result.notice.strip():
            self.card(item, f"after-notice:{key}", "notice", notice(result.notice))
        if result.restart:
            # 只在内存里记；run 循环在这一拍结束后退出，外部守护拉起新代码。
            self.restart_requested = True
        if result.pending and not result.rerun:
            due(result.recheck_after, self.clock())
        aliases = result.aliases
        if not isinstance(aliases, list) or not all(isinstance(a, str) and a for a in aliases):
            raise ValueError("流程返回的别名必须是非空字符串列表")
        with self.log.transaction():
            for alias in aliases:
                identity = f'{item["target"]}:{alias}'
                existing = self.log.get("aliases", identity) or self.log.get("items", identity)
                if existing and existing["object_key"] != item["object_key"]:
                    raise ValueError(f"别名冲突：{identity}")
                self.fact("alias_bound", {"id": identity, "object_key": item["object_key"],
                          "item": item["id"]}, item["id"], source="alias", ext_id=f"{key}:{alias}")
        return result

    def pending_changes(self, item, result, target, hook):
        count = item.get("after_pending_count", 0) + 1
        changes = dict(next=result.next, gate_reason=result.reason, after_pending_count=count, after_wait=None)
        if count >= target.get("pending_limit", 5):
            self.card(item, f"pending:{item['stage']}:{item.get('pending_episode', 0)}", "notice",
                      notice(result.reason + "；" + result.next))
            changes.update(state="卡住", blocked_by="阶段等待达到上限", leave={"kind": "等你", "about": result.reason})
        elif result.rerun:
            changes.update(state="空闲", leave={"kind": "工序", "owner": "车间", "at": stamp(self.clock()), "about": result.next})
        else:
            # 取插件建议与核心指数退避较晚者，等待不占执行者。
            at = max(due(result.recheck_after, self.clock()),
                     stamp(self.clock() + timedelta(seconds=min(300 * 2 ** (count - 1), 3600))))
            self.timer(item, f"{hook['key']}:{count}", at, result.next)
            changes.update(state="定时", after_wait={**hook, "at": at},
                           leave={"kind": "工序", "owner": "车间", "at": at, "about": result.next})
        return changes

    def recheck_after(self, item):
        hook = item.get("after_wait")
        if not hook or item["state"] != "空闲" or stamp(self.clock()) < hook["at"] or not self.retry_due(hook["key"]):
            return
        target = self.rules.target(item["target"])
        try:
            result = self.after({**item, "result": hook["receipt"]}, item["stage"], hook["key"], target, recheck=True)
            with self.log.transaction():
                if result.pending:
                    self.change(item, **self.pending_changes(item, result, target, hook))
                else:
                    self.change(item, stage=hook["to"], state="完成" if hook["complete"] else "空闲",
                                next=hook["to"], after_wait=None, after_pending_count=0,
                                pending_episode=item.get("pending_episode", 0) + 1, blocked_by="",
                                leave=hook["receipt"].get("leave") or {"kind": "工序", "owner": "车间", "at": stamp(self.clock()), "about": hook["to"]})
            self.retry_clear(hook["key"])
        except Retryable as exc:
            self.retry_failure(item["target"], "flow", target["plugin"], hook["key"], exc)
        except Exception as exc:
            self.broken(item["target"], "flow", target["plugin"], exc)

    def cleanup_flows(self):
        keep = [r["brief"].get("workdir") for r in self.active() if not r.get("stopped")]
        for name in self.rules.data["targets"]:
            target = self.rules.target(name)
            if self.control(name).get("broken"):
                continue
            try:
                if getattr(self.plugins.get("flow", target["plugin"]), "cleanup", None):
                    self.call("flow", target["plugin"], "cleanup", target, keep)
            except Pending:
                pass  # 清理不挡别的事，作业自己跑完。
            except Exception as exc:
                self.broken(name, "flow", target["plugin"], exc)

    def release_workdirs(self):
        """收尾完成的活：流程说不用留了，就请为它开过工作目录的执行者收回；返工时执行者重新开。

        流程说还要留（例如还有没关的提交请求），约一小时后再问；执行者说不能收（有没交出去的改动），
        通知 Kun 一次，不再重试。没有任何执行者认领的目录不动。
        """
        running = {r["item"] for r in self.active()}
        now = stamp(self.clock())
        for item in self.log.all("items"):
            if (item["state"] != "完成" or not item.get("workdir") or item.get("workdir_released")
                    or item["id"] in running or (item.get("release_at") or "") > now):
                continue
            control = self.control(item["target"])
            if control.get("paused") or control.get("broken") or item["target"] not in self.rules.data["targets"]:
                continue
            target = self.rules.target(item["target"])
            later = stamp(self.clock() + timedelta(hours=1))
            # 同一次收回跨拍、跨重启复用结果；延期重试和返工后的收尾必须重新判断。
            identity = [item["id"], item.get("pending_episode", 0), item["workdir"], item.get("release_at")]
            try:
                hold = getattr(self.plugins.get("flow", target["plugin"]), "hold_workdir", None)
                reason = self.call("flow", target["plugin"], "hold_workdir", item, target,
                                   identity=identity, item=item["id"]) if hold else ""
                if not isinstance(reason, str):
                    raise ValueError("流程留工作目录的原因必须是字符串")
            except Pending:
                continue
            except Retryable:
                self.change(item, release_at=later)
                continue
            except Exception as exc:
                self.broken(item["target"], "flow", target["plugin"], exc)
                continue
            if reason:
                self.change(item, release_at=later, release_hold=reason)
                continue
            outcome = "无执行者认领"
            for name in sorted(item.get("sessions") or {}):
                try:
                    release = getattr(self.plugins.get("executor", name), "release", None)
                    result = self.call("executor", name, "release", item, target,
                                       identity=identity, item=item["id"]) if release else None
                    if result is not None and (not isinstance(result, Release) or not result.released and not result.reason):
                        raise ValueError("执行者收回结果无效；留着时必须写原因")
                except Pending:
                    outcome = None
                    break
                except Retryable:
                    outcome = None
                    self.change(item, release_at=later)
                    break
                except Exception as exc:
                    outcome = None
                    self.broken(item["target"], "executor", name, exc)
                    break
                if result is None:
                    continue
                if result.released:
                    outcome = "已收回"
                    self.change(item, workdir=None, workdir_released=outcome, release_hold="", release_at=None)
                else:
                    outcome = "留着：" + result.reason
                    with self.log.transaction():
                        self.card(item, f'workdir-kept:{item.get("pending_episode", 0)}', "notice",
                                  notice(f'{item["id"]}（{item.get("title", "")}）已收尾，工作目录没收回：{result.reason}'))
                        self.change(item, workdir_released=outcome, release_hold="", release_at=None)
                break
            if outcome == "无执行者认领":
                self.change(item, workdir_released=outcome, release_hold="", release_at=None)

    def ingest(self, event: Event, round_token: str | None = None, *, ingress: str = "source") -> bool:
        if ingress == "external":
            event = replace(event, source=f"cli:{event.source}")
        if round_token:
            row = self.log.get("rounds", round_token)
            if not row:
                raise ValueError("未知事件来源轮次令牌")
            parent = self.log.get("items", row["item"])
            event = replace(event, depth=parent["depth"] + 1, action="update")
            ingress = "agent"
        elif ingress == "external":
            event = replace(event, depth=0, action="update")
        elif ingress != "source":
            raise ValueError("事件入口必须是可信来源、外部入口或带轮次令牌的 agent")
        event.validate()
        event = replace(event, object_key=self.resolve_key(event.target, event.object_key))
        self.rules.target(event.target)
        with self.log.transaction():
            paused = self.control(event.target).get("paused", False)
            kind = "deferred_event" if paused else "event"
            added = self.fact(kind, {**asdict(event), "ingress": ingress, "pending_classification": ingress == "external",
                                     **gate(ingress)}, event.item_id, source=event.source, ext_id=event.ext_id)
            if added and not paused:
                if ingress == "external":
                    self.classification_intent(event)
                    return added
                item = self.log.get("items", event.item_id)
                if item is None:
                    return added  # 没放行的对象只记了事实。
                if event.depth > self.rules.data["limits"]["chain_depth"]:
                    self.block(item, "事件连锁超过上限")
                elif self.control(event.target).get("broken"):
                    self.block(item, "流程插件等待修复", plugin=True)
            return added

    def classification_intent(self, event, received_seq=None):
        """received_seq：原来收到时的事实序号；暂停后补放的事件带原序号，放弃前收到的旧返工照样作废。"""
        seq = self.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0]
        self.fact("effect", {"id": f"classify:{seq}", "kind": "classify", "done": False,
                             "event": asdict(event), "item": event.item_id, "event_seq": seq,
                             "received_seq": received_seq or seq, "defer_creation": True}, event.item_id)

    def classify_events(self):
        for entry in self.log.all("effects"):
            if entry.get("kind") != "classify" or entry["done"]:
                continue
            event = Event(**entry["event"])
            control = self.control(event.target)
            if control.get("paused") or control.get("broken") or not self.retry_due(entry["id"]):
                continue
            target = self.rules.target(event.target)
            try:
                item = self.log.get("items", event.item_id)
                result = self.call("flow", target["plugin"], "classify", event, item, target,
                                   identity=[entry["id"], asdict(event)], lookup=event.target)
                decision = Classification(result) if isinstance(result, str) else result
                if not isinstance(decision, Classification):
                    raise ValueError("流程返回了未知判定")
                if type(decision.discard) is not bool:
                    raise ValueError("丢弃标记必须是布尔值")
                if decision.summary is not None and not isinstance(decision.summary, str):
                    raise ValueError("判定摘要必须是字符串")
                note = {"summary": decision.summary.strip()} if decision.summary and decision.summary.strip() else {}
                if decision.discard:
                    self.fact("event_discarded", {"id": entry["id"], "event_seq": entry["event_seq"], **note})
                    self.retry_clear(entry["id"])
                    continue
                action = decision.action
                if decision.object_key is not None:
                    key = self.resolve_key(event.target, decision.object_key)
                    if not isinstance(key, str) or not key or not self.log.get("items", f"{event.target}:{key}"):
                        raise ValueError("可信归件只能指向已有活")
                    event = replace(event, object_key=key)
                else:
                    event = replace(event, object_key=self.resolve_key(event.target, event.object_key))
                if action not in ("update", "ready", "rework"):
                    raise ValueError("流程返回了未知事件动作")
                action = self.after_abandon(event.item_id, entry.get("received_seq", entry["event_seq"]), action)
                with self.log.transaction():
                    self.fact("event_classified", {"id": entry["id"], "action": action,
                              "event_seq": entry["event_seq"], **note,
                              **({"event": {**asdict(event), "action": action}} if entry.get("defer_creation") else {})}, event.item_id)
                self.retry_clear(entry["id"])
            except Pending:
                continue
            except Retryable as exc:
                self.retry_failure(event.target, "flow", target["plugin"], entry["id"], exc)
            except Exception as exc:
                self.broken(event.target, "flow", target["plugin"], exc)

    def after_abandon(self, item_id, seq, action):
        """放弃之前收到的事件，晚到的分类或补放只当资料：返工、放行降为普通更新。"""
        item = self.log.get("items", item_id)
        if action in ("rework", "ready") and item and seq < item.get("abandoned_seq", 0):
            return "update"
        return action

    def drop(self, item_id):
        """清掉一件还没跑过轮次的活；之后同一对象要再放行才重新建活。"""
        with self.log.transaction():
            item = self.log.get("items", item_id)
            if not item:
                raise ValueError(f"找不到活：{item_id}")
            rounds = [r for r in self.log.all("rounds") if r.get("item") == item_id]
            cards = [c for c in self.log.all("cards") if c.get("item") == item_id]
            timers = [t for t in self.log.all("timers") if t.get("item") == item_id and not t.get("fired")]
            effects = [e for e in self.log.all("effects") if e.get("item") == item_id and not e.get("done")]
            aliases = [a for a in self.log.all("aliases") if a.get("item") == item_id]
            if item["state"] != "空闲" or item.get("history") or item.get("sessions") or rounds or cards or timers or effects or aliases:
                raise ValueError(f"{item_id} 已经跑过轮次或有未了的卡、定时、别名，不能清掉")
            self.fact("item_dropped", {"id": item_id}, item_id)

    def abandon(self, item_id, reason):
        """Kun 决定不做了：停在当前阶段，状态「放弃」，取消没答的问题，让出串行路径。
        记录都留着；要再做就返工（可信的「不对」），回到执行重新排队。"""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("放弃要写原因")
        with self.log.transaction():
            item = self.log.get("items", item_id)
            if not item:
                raise ValueError(f"找不到活：{item_id}")
            if item["state"] in ("完成", "放弃"):
                raise ValueError(f"{item_id} 已经{item['state']}，不用放弃")
            if any(r["item"] == item_id for r in self.active()):
                raise ValueError(f"{item_id} 还有一轮没结束，先用 abort 结束那一轮再放弃")
            self.fact("item_abandoned", {"id": item_id, "reason": reason.strip()}, item_id)
            self.cancel_questions(item)
            # 放弃前收到的返工、插件故障的恢复快照都作废：只有放弃之后的新返工才让它回来。
            self.change(item, state="放弃", next=f"已放弃：{reason.strip()}", blocked_by="", serial_hold=False,
                        pending_advance=None, after_wait=None, wait_for_ready=False, retry_at=None,
                        pending_rework=False, plugin_blocked=False, before_plugin_failure=None,
                        leave={"kind": "等你", "about": "已放弃；要再做就返工"})

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
                    body = dict(entry["event"])
                    ingress = body.pop("ingress", "unknown")
                    body.pop("pending_classification", None)
                    body.pop("release_gate", None)
                    event = Event(**body)
                    if ingress == "unknown":
                        event = replace(event, action="update")
                    event = replace(event, object_key=self.resolve_key(event.target, event.object_key))
                    event = replace(event, action=self.after_abandon(event.item_id, int(entry["id"]), event.action))
                    self.fact("event", {**asdict(event), "ingress": ingress, "pending_classification": ingress == "external",
                                        **gate(ingress)}, event.item_id, source="resume", ext_id=entry["id"])
                    if ingress == "external":
                        self.classification_intent(event, int(entry["id"]))
                        continue
                    replayed = self.log.get("items", event.item_id)
                    if replayed is None:
                        continue  # 没放行的对象只记了事实。
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
        try:
            try:
                receipt = Receipt.parse(data, self.clock())
            except ValueError as exc:
                # advance 填错也并进同一次拒绝，执行者一次改全。
                advance = data.get("advance") if isinstance(data, dict) else None
                extra = self.advance_error(token, advance) if isinstance(advance, str) else ""
                if extra and extra not in str(exc):
                    raise ValueError(f"{exc}；{extra}") from exc
                raise
            for event in receipt.events:
                self.rules.target(event["target"])
            extra = self.advance_error(token, receipt.advance)
            if extra:
                raise ValueError(extra)
        except (ValueError, TypeError, KeyError) as exc:
            # 原因记在这一轮上：执行者没改对就退出时，自动失败的结果里仍看得到。
            self.receipt_rejected(token, exc)
            raise
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

    def advance_error(self, token, advance) -> str:
        """交回执时就按本阶段核对 advance，执行者能在同一轮看到原因改对重交；没问题返回空串。"""
        row = self.log.get("rounds", token) if isinstance(token, str) else None
        if not row or advance is None:
            return ""
        item = self.log.get("items", row["item"])
        if not item or item["stage"] != row["stage"]:
            return ""
        try:
            target = self.rules.target(item["target"])
            stages = self.plugins.get("flow", target["plugin"]).stages(target)
            validate_stages(stages)
        except Exception:
            return ""  # 读不到阶段时留给收轮时的关口判定。
        return advance_problem(item, advance, stages)

    @staticmethod
    def failure_reason(row, receipt):
        """没跑成的原因；这一轮自己交的回执被拒过，具体字段原因一并带上（代交的摘要里常没有）。"""
        if receipt.outcome != "failed":
            return None
        rejected = row.get("receipt_rejected")
        if rejected and rejected not in receipt.summary:
            return f"{receipt.summary}；这一轮交的回执被拒过：{rejected}"
        return receipt.summary

    def receipt_rejected(self, token, exc):
        row = self.log.get("rounds", token) if isinstance(token, str) else None
        if not row or row.get("receipt") is not None or row["status"] not in ("starting", "running"):
            return
        with self.log.transaction():
            self.fact("round_changed", {"token": token, "receipt_rejected": str(exc)}, row["item"])

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

    def receipt_notices(self, item, stage, token, receipt, evidence, closing):
        """执行者回执里的通知只在 Kun 需要知道时发：这一轮留下等你，或这件活完成。

        完成通知按 Evidence.done_key 只发一张，不看执行者用什么 key；其余的不发卡，记 notice_held 供 why 查看。
        """
        waiting = bool(receipt.leave and receipt.leave.get("kind") == "等你")
        done = f'notice:完成:{evidence.done_key}' if evidence.done_key else "notice:完成"
        for entry in receipt.notice:
            if waiting:
                self.card(item, f'notice:{entry["key"]}', "notice", entry)
                continue
            if closing and not self.log.get("cards", f'{item["id"]}:{done}'):
                self.card(item, done, "notice", entry)
                continue
            reason = (f'这次完成已发过通知（{evidence.done_key or "这件活"}）' if closing
                      else "进度通知不发：只在留下等你或完成时发")
            self.fact("notice_held", {"token": token, "stage": stage, "key": entry["key"],
                      "subject": entry["subject"], "text": entry["text"], "reason": reason}, item["id"])

    def timer(self, item, key, at, about, kind="工序", owner="车间", base=None):
        timer_id = f'{item["id"]}:{key}'
        self.fact("timer_created", dict(id=timer_id, item=item["id"], target=item["target"],
                  at=due(at, base or self.clock()), about=about, kind=kind, owner=owner, fired=False), item["id"],
                  source="timer-plan", ext_id=timer_id)

    def block(self, item, reason, plugin=False):
        with self.log.transaction():
            item = self.log.get("items", item["id"])
            if plugin and item.get("plugin_blocked"):
                return
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
                    if item["target"] == affected and item["state"] not in ("完成", "放弃") and not item.get("plugin_blocked"):
                        self.block(item, f"{port}/{name}: {exc}", plugin=True)

    def waiting(self, item):
        return any(c["item"] == item["id"] and c["mode"] == "ask" and not c["answered"]
                   and (c.get("awaiting_release") or not c.get("cancelled") and not c.get("rejected"))
                   for c in self.log.all("cards"))

    def sources(self):
        for target, rule in self.rules.data["targets"].items():
            if self.control(target).get("broken"):
                continue
            # 先结算已经开始的完成钩子，再读新变化，避免别名尚未绑定时另开活。
            if any(r["target"]["name"] == target and r["status"] == "finishing"
                   and self.log.get("effects", f'after:{r["token"]}') for r in self.active()):
                continue
            for name in rule.get("sources", []):
                key = f"{target}:{name}"
                cursor = (self.log.get("cursors", key) or {}).get("cursor")
                if not self.retry_due(f"source:{key}"):
                    continue
                try:
                    # 身份只认交付对象：游标每次都可能变，带进身份就成了一出结果马上再拉；拉的间隔由新鲜期管。
                    batch = self.call("source", name, "poll", self.rules.target(target), cursor, identity=[target])
                    with self.log.transaction():
                        for event in batch.events:
                            if event.target != target:
                                raise ValueError("来源不能跨交付对象写入")
                            self.ingest(event)
                        if batch.cursor != cursor:
                            self.fact("cursor", {"id": key, "cursor": batch.cursor})
                    self.retry_clear(f"source:{key}")
                except Pending:
                    continue
                except Retryable as exc:
                    self.retry_failure(target, "source", name, f"source:{key}", exc)
                except Exception as exc:
                    self.broken(target, "source", name, exc)

    def delivery_error(self, key, exc, item=None):
        state = self.control(f"delivery:{key}")
        if not state.get("failed"):
            with self.log.transaction():
                self.fact("delivery_failed", {"key": key, "error": repr(exc)}, item)
                self.fact("control", {"id": f"delivery:{key}", "failed": True, "since": stamp(self.clock())})

    def delivery_ok(self, key):
        if self.control(f"delivery:{key}").get("failed"):
            self.fact("control", {"id": f"delivery:{key}", "failed": False})

    def invalid_answer(self, card, value):
        """审批卡收到非选项回答：通道没坏，记事实并通知 Kun 一次，活继续等原卡。"""
        if card.get("invalid_answer") != value:
            self.fact("invalid_answer", {"card": card["id"], "value": value, "title": card["payload"]["decision"]}, card["item"])
            self.fact("card_changed", {"id": card["id"], "invalid_answer": value}, card["item"])
        if card.get("invalid_answer_noticed"):
            return
        item = self.log.get("items", card["item"])
        name = (item or {}).get("title") or card["item"]
        text = (f"「{name}」的卡片「{card['payload']['decision']}」收到「{value}」，不是卡上的选项，活还在等这张卡。"
                "请在卡片上点按钮重新回答；这条不用回。")
        self.card({"id": card["item"]}, f"invalid-answer:{card['key']}", "notice", notice(text))
        self.fact("card_changed", {"id": card["id"], "invalid_answer_noticed": True}, card["item"])

    def reconcile_uncertain(self, card):
        key = f"reconcile:{card['id']}"
        count = self.control(key).get("count", 0)
        if count >= 3:
            return
        with self.log.transaction():
            self.fact("control", {"id": key, "count": count + 1})
            if count + 1 == 3:
                self.fact("reconcile_failed", {"card": card["id"], "reason": "连续 3 次无法确定是否已发送"}, card["item"])
                incident = self.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0]
                self.fact("card_changed", {"id": card["id"], "reconcile_failed": True}, card["item"])
                # 告警自身若也无法对账，保留状态，不递归制造更多告警。
                if not card.get("reconciliation_alert"):
                    self.card({"id": card["item"]}, f"reconcile:{incident}", "notice",
                              notice(f"卡片 {card['id']} 连续 3 次对账不确定，需要检查通道"), reconciliation_alert=True)

    def reconcile_clear(self, card):
        key = f"reconcile:{card['id']}"
        if self.control(key).get("count"):
            self.fact("control", {"id": key, "count": 0})
        if card.get("reconcile_failed"):
            self.fact("card_changed", {"id": card["id"], "reconcile_failed": False}, card["item"])

    def undeliverable(self, card, reason, *, cancelled=False):
        """已知终态单独落事实；不冒充回答，不再发送或对账。"""
        with self.log.transaction():
            current = self.log.get("cards", card["id"])
            if current.get("rejected") or current.get("cancelled"):
                return
            self.fact("card_cancelled_external" if cancelled else "card_rejected",
                      {"id": card["id"], "reason": reason}, card["item"])
            item = self.log.get("items", card["item"])
            wait = (card["mode"] == "ask" and item and item["state"] != "完成"
                    and (item.get("before_plugin_failure") or {}).get("state") != "完成")
            self.fact("card_changed", {"id": card["id"], "sending": False,
                      "rejected": not cancelled, "cancelled": cancelled,
                      "cancel_done": cancelled, "reconcile_failed": False,
                      "awaiting_release": bool(wait)}, card["item"])
            self.reconcile_clear(card)
            self.delivery_error(card["id"], RuntimeError(reason), card["item"])
            if wait:
                pending = item.get("pending_advance")
                if pending and pending.get("card") == card["id"]:
                    pending = None
                waiting = dict(state="等你", next="卡片无法继续，需要检查后放行", blocked_by="Kun",
                               leave={"kind": "等你", "about": "卡片无法继续，需要检查后放行"})
                changes = dict(wait_for_ready=True, pending_advance=pending)
                if item["state"] == "卡住":
                    saved = item.get("before_plugin_failure")
                    if saved and saved.get("state") not in ("跑着", "完成"):
                        changes["before_plugin_failure"] = {**saved, **waiting}
                elif item["state"] != "跑着":
                    changes.update(waiting)
                self.change(item, **changes)
            if not card.get("rejection_alert"):
                self.card({"id": card["item"]}, f"undeliverable:{card['id']}", "notice",
                          notice(f"车间有一张卡发不出去，原因已记录，请看 workshop why {card['item']}"),
                          rejection_alert=True, rejection_of=card["id"])

    def channels(self):
        name = self.rules.data["channel"]
        try:
            self.plugins.get("channel", name)
            for card in self.log.all("cards"):
                try:
                    if card.get("rejected"):
                        continue
                    if card.get("cancelled"):
                        if not card.get("cancel_done"):
                            self.call("channel", name, "cancel", card["id"], card.get("external"), identity=["cancel", card["id"]])
                            self.fact("card_changed", {"id": card["id"], "cancel_done": True}, card["item"])
                    elif not card["sent"]:
                        external = None
                        method = "ask" if card["mode"] == "ask" else "notify"
                        sending = [card["id"]]
                        # 发送作业还在跑或已出结果，就认它，不另去对账，免得把自己的发送算成不确定。
                        if card.get("sending") and not (self.jobs and self.jobs.exists("channel", name, method, sending)):
                            external = self.call("channel", name, "reconcile", card["id"], card, identity=[card["id"]])
                            if external is None:
                                self.reconcile_uncertain(card)
                                continue
                            self.reconcile_clear(card)
                        if external in (None, "absent"):
                            if not card.get("sending"):
                                self.fact("card_changed", {"id": card["id"], "sending": True}, card["item"])
                            external = self.call("channel", name, method, card["id"], card["payload"], identity=sending)
                        if not isinstance(external, str) or not external:
                            raise ValueError("通道没有返回外部编号")
                        self.fact("card_changed", {"id": card["id"], "external": external, "sent": True, "reconcile_failed": False}, card["item"])
                    self.delivery_ok(card["id"])
                    if card.get("rejection_of"):
                        self.delivery_ok(card["rejection_of"])
                except Pending:
                    continue
                except ChannelRejected as exc:
                    self.undeliverable(card, str(exc))
                except Exception as exc:
                    self.delivery_error(card["id"], exc, card["item"])
            cards = [c for c in self.log.all("cards") if c["mode"] == "ask" and c["sent"]
                     and not c["answered"] and not c.get("cancelled")]
            try:
                answers = self.call("channel", name, "poll", cards, identity=["poll"])
            except Pending:
                answers = []
            for answer in answers:
                with self.log.transaction():
                    card = self.log.get("cards", answer.card_id)
                    if not card or card["mode"] != "ask" or not card["sent"] or card["answered"] or card.get("cancelled"):
                        continue
                    if answer.cancelled:
                        self.undeliverable(card, "上游卡片已作废或已清理", cancelled=True)
                        continue
                    # 旧版本把非选项回答记成投递失败；收到任何回答都先清掉那条，免得看门程序误报通道故障。
                    self.delivery_ok(f"answer:{card['id']}")
                    if card["payload"]["kind"] == "approval" and answer.value not in [c["key"] for c in card["payload"]["choices"]]:
                        self.invalid_answer(card, answer.value)
                        continue
                    self.record_answer(card, answer)
            self.delivery_ok(f"channel:{name}")
        except Exception as exc:
            self.delivery_error(f"channel:{name}", exc)

    def cancel_questions(self, item):
        for card in self.log.all("cards"):
            if card["item"] == item["id"] and card.get("awaiting_release"):
                self.fact("card_changed", {"id": card["id"], "awaiting_release": False}, item["id"])
            if card["item"] == item["id"] and card["mode"] == "ask" and not card["answered"] and not card.get("cancelled") and not card.get("rejected"):
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

    def record_answer(self, card, answer, provenance=None):
        """记下 Kun 对一张卡的回答。provenance 记回答从哪个渠道来（飞书卡片不写；会话转录写会话号和原话）。"""
        approved = answer.value == "yes" if card.get("core_approval") else answer.approved
        choice_label = next((c["label"] for c in card["payload"]["choices"] if c["key"] == answer.value), answer.value)
        # 回答单独追加，由 evolve 在最新资料上追加，不回写旧资料快照。
        self.fact("answer", {**asdict(answer), "approved": approved, "choice_label": choice_label, "key": card["payload"].get("key", card["key"]),
                  "title": card["payload"]["decision"], "actions": card.get("actions", []), "billing_configs": card.get("billing_configs", []),
                  "frozen_files": card.get("frozen_files", []), **({"via": provenance} if provenance else {})},
                  card["item"], source="answer", ext_id=card["id"])
        self.fact("card_changed", {"id": card["id"], "answered": True, "answer": answer.value, "choice_label": choice_label, "approved": approved}, card["item"])
        item = self.log.get("items", card["item"])
        if card.get("billing_configs") and not approved:
            self.change(item, state="等你", wait_for_ready=True, next="按量计费未获批准，等待放行", blocked_by="Kun",
                        leave={"kind": "等你", "about": "调整执行者或授权后放行"})
        elif item["state"] == "等你" and not item.get("wait_for_ready") and not self.waiting(item):
            self.change(item, state="空闲", next="按回答继续", blocked_by="")

    def apply_timer(self, timer, item):
        if item["state"] == "放弃":
            return  # 放弃前约的工序和送回不再打扰；到点只记事实。
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
        entry = self.log.get("effects", key)
        if not (entry or {}).get("done"):
            if entry is None or "done" not in entry:
                # 作业没出结果时每拍都会回到这里；意图只记一次。
                self.fact("effect", {"id": key, "done": False})
            result = action()
            self.fact("effect", {"id": key, "done": True, "result": result})
        return self.log.get("effects", key).get("result")

    def taskhome(self, item):
        name = self.rules.data.get("taskhome")
        if not name:
            return True
        try:
            self.plugins.get("taskhome", name)
            target = self.rules.target(item["target"])
            if not item.get("ticket"):
                key = f'ticket:{item["id"]}'
                ticket = self.effect(key, lambda: self.call("taskhome", name, "open", item["id"], item, target, identity=[key]))
                self.change(item, ticket=ticket)
            else:
                key = f'ticket:{item["id"]}:{item["revision"]}'
                self.effect(key, lambda: self.call("taskhome", name, "update", key, item["ticket"], item, target, identity=[key]))
            return True
        except Pending:
            return False
        except Exception as exc:
            self.broken(item["target"], "taskhome", name, exc)
            return False

    def active(self):
        return self.log.all("rounds", unless=("status", "ended"))

    def occupied(self):
        return sum(not row.get("stopped") for row in self.active())

    def launch(self, row):
        item = self.log.get("items", row["item"])
        self.plugins.get("executor", row["executor"])
        brief = Brief(**row["brief"])
        if row["session"]:
            started = self.call("executor", row["executor"], "resume", row["token"], row["session"], brief, item,
                                row["config"], row["target"], identity=[row["token"]], item=item["id"])
            if started.session != row["session"]:
                raise ValueError("执行者没有接回原会话")
        else:
            started = self.call("executor", row["executor"], "launch", row["token"], brief, item, row["config"],
                                row["target"], identity=[row["token"]], item=item["id"])
        if not started.session or not started.handle:
            raise ValueError("执行者必须返回会话和运行标识")
        with self.log.transaction():
            latest = self.log.get("rounds", row["token"])
            self.fact("round_changed", {"token": row["token"], "started": asdict(started),
                       "status": "finishing" if latest.get("receipt") is not None else "running"}, item["id"])
            item = self.log.get("items", item["id"])
            sessions = dict(item["sessions"])
            sessions[row["executor"]] = started.session
            # 新一轮可能重新开了工作目录；收尾后要重新判断收不收。
            changes = dict(session=started.session, sessions=sessions, workdir=started.workdir,
                           workdir_released=None, release_at=None, release_hold="")
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
        unavailable = tuple(dict.fromkeys((*unavailable, *self.recently_unavailable(item))))
        try:
            flow = self.plugins.get(port, name)
            stages = flow.stages(target)
            validate_stages(stages)
            if item["stage"] not in stages:
                following = next(s for s in stages if ALL_STAGES.index(s) > ALL_STAGES.index(item["stage"]))
                self.change(item, stage=following, state="空闲", next=following, failures=0, retry_at=None,
                            retry_executor=None, review_required=1, reviews=[],
                            gate_reason=f'规则已去掉「{item["stage"]}」阶段，直接进{following}')
                return
            content = self.call(port, name, "brief", item, item["stage"], target, item=item["id"])
            content = FlowBrief(content) if isinstance(content, str) else content
            if (not isinstance(content, FlowBrief) or not isinstance(content.instructions, str)
                    or type(content.readonly) is not bool
                    or content.workdir is not None and not Path(content.workdir).is_absolute()):
                raise ValueError("派活说明或目录无效")
            instructions = content.instructions
            candidates = []
            metered = []
            waiting = None
            for config in self.rules.candidates(item["stage"], item["target"], item["writer"]):
                if (config["use"] in unavailable or item.get("retry_executor") and config["use"] != item["retry_executor"]
                        or item["stage"] == "审查" and config["family"] in item.get("reviews", [])):
                    continue
                port, name = "executor", config["use"]
                self.plugins.get(port, name)
                try:
                    available = self.call(port, name, "available", config)
                except Unavailable:
                    available = False
                except Pending as exc:
                    # 各家的可用性一起查，不一家一家排队等。
                    waiting = waiting or exc
                    continue
                if not available:
                    continue
                billing_key = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
                if config["billing"] == "metered" and billing_key not in item.get("billing_approved", []):
                    metered.append((billing_key, config))
                    continue
                candidates.append({"config": config, "available": True})
            if waiting:
                raise waiting
            if not candidates and metered:
                context = "没有可用的订阅候选；是否授权本件活使用这些按量计费候选：" + json.dumps([c for _, c in metered], ensure_ascii=False)
                payload = question("是否批准本件活使用按量计费执行者", approval=True)
                payload["context"] = context
                self.card(item, f'billing:{item["revision"]}', "ask", payload, billing_configs=[k for k, _ in metered], core_approval=True)
                self.change(item, state="等你", next="按量计费先授权", blocked_by="Kun", leave={"kind": "等你", "about": "按量计费授权"})
                return
            history = self.log.all("rounds", where=("item", item["id"]))
            selection_target = {**target, "selection": {"writer": item["writer"], "reviewed": item.get("reviews", []),
                                "retry_executor": item.get("retry_executor"), "excluded": list(unavailable)}}
            selector_name = self.rules.selector(item["stage"], item["target"])
            port, name = "selector", selector_name
            self.plugins.get(port, name)
            choice = self.call(port, name, "select", selection_target, item["stage"], len(history) + 1, history, candidates)
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
                # 挑选逻辑可用 Selection(None, 理由) 说明为什么一个都没选；理由进事实和卡片，why 能看到。
                why_none = choice.reason.strip() if choice is not None and isinstance(choice.reason, str) else ""
                payload = question("现有订阅没有可用执行者")
                if why_none:
                    self.fact("selection_none", {"reason": why_none, "selector": selector_name}, item["id"])
                    payload["context"] = "挑选逻辑没有选出执行者：" + why_none
                self.card(item, f'capacity:{item["revision"]}', "ask", payload)
                self.change(item, state="等你", next="等现有订阅恢复", blocked_by="Kun",
                            leave={"kind": "等你", "about": "执行者不可用" + (f"：{why_none}" if why_none else "")})
                return
            port, name = "executor", chosen["use"]
            token = str(uuid.uuid4())
            brief = Brief(instructions, [m for m in item["materials"] if m["role"] != "Kun 的回答"],
                          answers=[m for m in item["materials"] if m["role"] == "Kun 的回答"],
                          workdir=content.workdir, readonly=content.readonly)
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
                self.release_unavailable(row)
                self.reserve(self.log.get("items", item["id"]), (*unavailable, chosen["use"]))
        except Retryable as exc:
            self.start_retry(item, locals().get("row"), port, name, exc)
        except Exception as exc:
            self.broken(item["target"], port, name, exc)

    def recently_unavailable(self, item, minutes=10):
        """这件活最近几轮启动时说没法用的执行者；启动作业晚一拍才出结果，重新挑时要避开它们。"""
        since = stamp(self.clock() - timedelta(minutes=minutes))
        names = []
        for row in reversed(self.log.all("rounds", where=("item", item["id"]))):
            if row.get("outcome") != "unavailable" or row["stage"] != item["stage"] or (row.get("ended") or "") < since:
                break
            names.append(row["executor"])
        return names

    def release_unavailable(self, row):
        """启动说没法用：端口保证没有启动副作用，结束这一轮、放开活，换下一家。"""
        item = self.log.get("items", row["item"])
        with self.log.transaction():
            self.fact("round_changed", {"token": row["token"], "status": "ended", "outcome": "unavailable",
                      "ended": stamp(self.clock())}, item["id"])
            self.change(item, state="空闲", round=None, next="选择下一执行者")

    def serial_holder(self, item):
        """同一交付对象里占着串行路径的另一件活：进了执行、没收尾完成、没放弃、不在排队。"""
        start = STAGES.index("执行")
        return next((other for other in self.log.all("items")
                     if other["id"] != item["id"] and other["target"] == item["target"] and other.get("serial_hold")
                     and other["state"] not in ("完成", "放弃", "排队") and STAGES.index(other["stage"]) >= start), None)

    def serial_queue(self, item):
        """规则 serial_paths：碰到的活同一时间只放一件过执行，其余停在当前阶段前排队，前一件收尾完成或放弃后自动放下一件。
        按说清楚时登记的路径加之后实际改动判；占位从进执行起，退回执行期间仍占着。"""
        prefixes = self.rules.target(item["target"]).get("serial_paths", [])
        needed = (prefixes and STAGES.index(item["stage"]) >= STAGES.index("执行")
                  and overlaps(item.get("paths", []), prefixes))
        if not needed:
            if item["state"] == "排队":
                self.change(item, state="空闲", next=item["stage"], blocked_by="", serial_hold=False)
            return self.log.get("items", item["id"])
        holder = self.serial_holder(item)
        if holder:
            about = f'排队：等 {holder["id"]} 收尾让出串行路径'
            if item["state"] != "排队" or item.get("blocked_by") != holder["id"] or item.get("serial_hold"):
                self.change(item, state="排队", next=about, blocked_by=holder["id"], serial_hold=False,
                            leave={"kind": "工序", "owner": "车间", "at": stamp(self.clock()), "about": about})
        elif item["state"] == "排队" or not item.get("serial_hold"):
            changes = dict(serial_hold=True)
            if item["state"] == "排队":
                changes.update(state="空闲", next=item["stage"], blocked_by="",
                               leave={"kind": "工序", "owner": "车间", "at": stamp(self.clock()), "about": item["stage"]})
            self.change(item, **changes)
        return self.log.get("items", item["id"])

    def runtime(self, token):
        state_dir = str(self.log.path.parent.resolve())
        command = [sys.executable, "-m", "workshop.cli", "--state-dir", state_dir]
        environment = {"WORKSHOP_STATE_DIR": state_dir, "WORKSHOP_ROUND_TOKEN": token}
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

    def answer_here(self, ref, value, quote, session):
        """Kun 在会话里回答一张卡（渠道由 Kun 选）：按卡号或飞书 Q 号找卡，记下会话号和 Kun 的原话，和卡片回答同样生效。"""
        if not quote.strip() or not session.strip():
            raise ValueError("要写 Kun 的原话和会话号")
        with self.log.transaction():
            cards = [c for c in self.log.all("cards") if c["mode"] == "ask" and ref in (c["id"], c.get("external"))]
            if len(cards) != 1:
                raise ValueError(f"找不到唯一一张问卡：{ref}")
            card = cards[0]
            if card["answered"] or card.get("cancelled"):
                raise ValueError("这张卡已经答过或作废")
            keys = [c["key"] for c in card["payload"]["choices"]]
            if keys and value not in keys:
                raise ValueError(f"回答须是选项之一：{', '.join(keys)}")
            payload = card["payload"]
            # 批准与否和卡片通道同一判法：只有审批卡答 yes 才算批准。
            approved = payload["kind"] == "approval" and value == "yes" and "yes" in keys
            self.record_answer(card, Answer(card["id"], value, approved),
                               provenance={"channel": "session", "session": session, "quote": quote})
        return card

    def abort(self, token, reason="人手动结束这一轮"):
        with self.log.transaction():
            row = self.log.get("rounds", token)
            if not row or row["status"] == "ended":
                raise ValueError("没有可结束的轮")
            self.fact("round_changed", {"token": token, "abort_requested": reason}, row["item"])

    def automatic_failure(self, token, reason):
        with self.log.transaction():
            latest = self.log.get("rounds", token)
            if not latest or latest.get("receipt") is not None or latest["status"] not in ("starting", "running"):
                return
            if latest.get("receipt_rejected"):
                reason += f'；最后一次交的回执被拒：{latest["receipt_rejected"]}'
            if self.submit(token, {"outcome": "failed", "summary": reason}):
                self.fact("round_changed", {"token": token, "automatic_failure": True}, latest["item"])

    def check_round(self, row):
        with self.log.transaction():
            row = self.log.get("rounds", row["token"])
            if not row or row.get("receipt") is not None or row["status"] != "running":
                return
        reason = row.get("abort_requested")
        if not reason and row.get("deadline", "9999") <= stamp(self.clock()):
            reason = "这一轮超过规则期限"
        if not reason:
            try:
                state = self.call("executor", row["executor"], "poll", row["token"],
                                  Started(**row["started"]) if row.get("started") else None, identity=[row["token"]])
                if state not in ("running", "exited"):
                    raise ValueError("执行者返回了未知存活状态")
                if state == "exited":
                    reason = "执行者已退出但没有交回执"
            except Pending:
                return
            except Retryable as exc:
                self.delivery_error(f"poll:{row['token']}", exc, row["item"])
                return
        if reason:
            # poll 期间可能刚交回执；自动失败与真实回执由同一个写事务仲裁。
            self.automatic_failure(row["token"], reason)

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

    def settle_executor(self, row):
        if row.get("stopped") and row.get("usage_checked"):
            return True
        if not self.retry_due(f"stop:{row['token']}"):
            return False
        item = self.log.get("items", row["item"])
        token = row["token"]
        port, name = "executor", row["executor"]
        try:
            self.plugins.get(port, name)
            started = Started(**row["started"]) if row.get("started") else None
            if not row.get("stopped"):
                self.call(port, name, "stop", token, started, identity=[token], item=item["id"])
                self.fact("round_changed", {"token": token, "stopped": True}, item["id"])
            if not row.get("usage_checked"):
                value = None
                try:
                    value = self.call(port, name, "usage", token, started, row["at"], identity=[token, row["at"]], item=item["id"])
                    if value is not None:
                        value.validate()
                except Exception as exc:
                    value = None
                    self.fact("usage_failed", {"token": token, "error": repr(exc)}, item["id"])
                measured = value or RoundUsage()
                self.fact("round_changed", {"token": token, "usage": asdict(measured.main),
                          "usage_by_model": measured.entries(), "children_reported": measured.children_reported,
                          "usage_checked": True}, item["id"])
            self.retry_clear(f"stop:{token}")
            return True
        except Retryable as exc:
            self.retry_failure(item["target"], port, name, f"stop:{token}", exc)
        except Exception as exc:
            # 关不掉不能假装释放占位；仍按退避重试停止，但立即隔离业务。
            self.retry_failure(item["target"], port, name, f"stop:{token}", exc)
            self.broken(item["target"], port, name, exc)
        return False

    def finish(self, row):
        if not self.settle_executor(row):
            return
        item = self.log.get("items", row["item"])
        if self.control(item["target"]).get("broken") or not self.retry_due(f"finish:{row['token']}"):
            return
        self.cleanup_flows()
        token = row["token"]
        port, name = "flow", self.rules.target(item["target"])["plugin"]
        try:
            received_at = datetime.fromisoformat(row["received"])
            receipt = Receipt.parse(row["receipt"], received_at)
            target = self.rules.target(item["target"])
            port, name = "flow", target["plugin"]
            flow = self.plugins.get(port, name)
            evidence = (Evidence(**row["assessment"]) if row.get("assessment") else
                        Evidence() if row.get("automatic_failure") else
                        self.call(port, name, "assess", item, receipt, target,
                                  identity=[item["id"], item["stage"], token, row["receipt"]], item=item["id"]))
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
            after_result = After()
            if advancing:
                key = f"after:{token}"
                after_result = self.after({**item, "result": receipt.json()}, row["stage"], key, target)
            with self.log.transaction():
                item = self.log.get("items", item["id"])
                # 本轮结果写完整原因：交过被拒的回执时，具体字段和合法取值一并记下（代交的摘要里常没有）。
                summary = self.failure_reason(row, receipt) or receipt.summary
                self.fact("round_result", {"token": token, "stage": row["stage"], "outcome": receipt.outcome,
                          "summary": summary, "evidence": receipt.evidence, "reason": evidence.reason},
                          item["id"], source="result", ext_id=token)
                for ask in receipt.ask:
                    self.card(item, f'{token}:{ask["key"]}', "ask", ask)
                completed = advancing and (decision.complete or (
                    bool(evidence.done_key) and STAGES.index(decision.stage) > STAGES.index(row["stage"])))
                self.receipt_notices(item, row["stage"], token, receipt, evidence, completed)
                for later in receipt.later:
                    self.timer(item, f'{token}:{later["key"]}', later["at"], later["about"], base=received_at)
                if receipt.leave and receipt.leave["kind"] in ("工序", "送回"):
                    leave = receipt.leave
                    self.timer(item, f"leave:{token}", leave["at"], leave["about"], leave["kind"], leave.get("owner", "Kun"), base=received_at)
                changes = dict(failures=0, round=None, retry_at=None, cause=max(item["cause"], row["receipt_seq"]),
                               retry_executor=None, review_required=required, reviews=reviews,
                               # 没跑成的原因交给流程写进下一轮说明；跑成了就清掉。
                               last_failure=summary if receipt.outcome == "failed" else None)
                if evidence.checks:
                    changes["checks"] = evidence.checks
                if evidence.paths:
                    # 说清楚时登记的预计路径和之后实际改动的并集，串行路径排队按它判。
                    changes["paths"] = sorted(set(item.get("paths", [])) | set(evidence.paths))
                if after_result.pending:
                    changes.update(self.pending_changes(item, after_result, target,
                        {"key": key, "receipt": receipt.json(), "to": decision.stage, "complete": decision.complete}))
                    if after_result.rerun:
                        changes.update(reviews=item.get("reviews", []), retry_executor=row["executor"])
                elif rejected:
                    changes.update(stage="执行", state="空闲", next="验收不过，退回执行", approved=[], reviews=[],
                                   after_pending_count=0, pending_episode=item.get("pending_episode", 0) + 1,
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
                    changes.update(after_pending_count=0, pending_episode=item.get("pending_episode", 0) + 1)
                    changes.update(stage=decision.stage, state="完成" if decision.complete else "空闲",
                                   next="已收尾" if decision.complete else decision.stage, blocked_by="", gate_count=0,
                                   leave=receipt.leave or {"kind": "工序", "at": stamp(self.clock()), "owner": "车间", "about": decision.stage})
                    if decision.stage == "执行" and item["stage"] != "说清楚":
                        changes.update(approved=[], reviews=[], review_required=1)
                    self.fact("gate_passed", {"gate_reason": decision.reason}, item["id"])
                elif (decision.needs_approval or decision.frozen) and receipt.outcome == "done" and not receipt.ask:
                    payload = (frozen_question(item, decision.frozen, decision.needs_approval) if decision.frozen
                               else question(decision.reason, approval=True))
                    card_id = self.card(item, f"approval:{token}", "ask", payload, actions=list(decision.needs_approval),
                                        frozen_files=list(decision.frozen), core_approval=True)
                    changes.update(state="等你", next=decision.reason, blocked_by="Kun", leave={"kind": "等你", "about": decision.reason},
                                   pending_advance={"token": token, "card": card_id, "from": item["stage"], "to": receipt.advance,
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
                           "outcome": receipt.outcome, "summary": summary,
                           "passed": evidence.passed if row["stage"] in ("审查", "验收") and receipt.outcome != "nothing" and not row.get("automatic_failure") else None}, item["id"])
                if item.get("plugin_blocked") and self.control(item["target"]).get("broken"):
                    changes["before_plugin_failure"] = {k: changes.get(k, item.get(k)) for k in ("state", "next", "blocked_by", "leave")}
                    changes.update(state="卡住", next="修复插件后继续", blocked_by=item["blocked_by"])
                self.change(item, "round_finished", **changes)
                for index, data in enumerate(receipt.events):
                    self.ingest(Event(**{**data, "source": f"round:{token}", "ext_id": str(index)}), round_token=token)
            self.retry_clear(f"finish:{token}")
        except Retryable as exc:
            self.retry_failure(item["target"], port, name, f"finish:{token}", exc)
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
            with self.log.transaction():
                card = self.log.get("cards", pending["card"])
                label = card.get("choice_label", card.get("answer", "未批准"))
                reason = f"Kun 没批准，选择了：{label}（{card.get('answer', '未知')}）"
                self.fact("core_feedback", {"body": reason, "card": card["id"], "choice": card.get("answer")}, item["id"])
                self.change(item, stage="执行", state="空闲", next=reason, pending_advance=None, reviews=[],
                            review_required=1, approved=[], retry_executor=None, gate_count=0, gate_reason=reason,
                            leave={"kind": "工序", "owner": "车间", "at": stamp(self.clock()), "about": "按拒绝意见修改"})
            return
        target = self.rules.target(item["target"])
        try:
            key = f"after:{pending['token']}"
            with self.log.transaction():
                latest = self.log.get("items", item["id"])
                if latest["pending_rework"] or latest["stage"] != pending["from"]:
                    return
                if not self.log.get("effects", key):
                    self.fact("effect", {"id": key, "done": False})
            result = self.after({**item, "result": receipt.json()}, pending["from"], key, target)
            with self.log.transaction():
                latest = self.log.get("items", item["id"])
                if latest["pending_rework"]:
                    return
                changes = (self.pending_changes(latest, result, target, {"key": key, "receipt": receipt.json(), "to": decision.stage, "complete": decision.complete}) if result.pending else
                           dict(state="空闲", next=decision.stage, gate_reason="已按原证据授权推进",
                                after_pending_count=0, pending_episode=latest.get("pending_episode", 0) + 1))
                self.change(latest, "gate_passed", stage=pending["from"] if result.pending else decision.stage,
                            pending_advance=None, gate_count=0, **changes)
        except Exception as exc:
            self.broken(item["target"], "flow", target["plugin"], exc)

    def tick(self):
        began = time.monotonic()
        try:
            self._tick(began)
        except Exception as exc:
            try:
                with self.log.transaction():
                    state = self.control("@tick")
                    errors = [f["body"]["error"] for f in self.log.facts()
                              if f["kind"] == "tick_failed" and f["seq"] >= state.get("since", 0)] if state.get("failures") else []
                    if repr(exc) not in errors:
                        self.fact("tick_failed", {"error": repr(exc)})
                    self.tick_failures = min(10, max(self.tick_failures, state["failures"]) + 1) if state.get("failures") else 1
                    if self.tick_failures in (1, 3, 10) and self.tick_failures != state.get("failures"):
                        since = state.get("since") if state.get("failures") else self.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0]
                        self.fact("control", {"id": "@tick", "since": since, "failures": self.tick_failures})
            except Exception as recording_error:
                print(f"车间 tick 失败且暂时无法记录：{exc!r}; {recording_error!r}", file=sys.stderr)

    def heartbeat(self, began=None):
        now = stamp(self.clock())
        heartbeat = self.log.path.parent / "heartbeat.json"
        temporary = heartbeat.with_suffix(".tmp")
        failures = [c for c in self.log.all("controls") if c["id"].startswith("delivery:") and c.get("failed")]
        for failure in failures:
            if not failure.get("since"):
                failure["since"] = now
                self.fact("control", {"id": failure["id"], "since": now})
        # tick_ms：这一拍从进入到写心跳前的耗时；一拍只读写事实库和本地文件，应是毫秒级。
        elapsed = round((time.monotonic() - began) * 1000) if began is not None else None
        temporary.write_text(json.dumps({"at": now, "last_ok_at": now, "tick_ms": elapsed, "active": self.occupied(),
            "delivery_failed_count": len(failures),
            "delivery_failed_since": min((c.get("since", now) for c in failures), default=None)}))
        with self.log.transaction():
            if self.control("@tick").get("failures"):
                self.fact("control", {"id": "@tick", "failures": 0, "since": None})
            temporary.replace(heartbeat)
        self.tick_failures = 0

    def _tick(self, began=None):
        with self.exclusive():
            if self.jobs:
                self.jobs.reap()
            if not getattr(self, "cleaned_flows", False):
                self.cleanup_flows()
                self.cleaned_flows = True
            self.sources()
            self.classify_events()
            self.channels()
            self.timers()
            for row in self.active():
                item = self.log.get("items", row["item"])
                try:
                    expired = row.get("deadline", "9999") <= stamp(self.clock())
                    if row["status"] == "starting" and not row.get("started") and not row.get("abort_requested") and not expired:
                        if self.control(item["target"]).get("broken"):
                            continue
                        try:
                            self.launch(row)
                        except Unavailable:
                            # 启动作业晚于预订出结果：放开这一轮，避开最近没法用的执行者另挑。
                            self.release_unavailable(row)
                            self.reserve(self.log.get("items", item["id"]))
                            continue
                        row = self.log.get("rounds", row["token"])
                    if row["status"] == "starting" and (row.get("abort_requested") or expired):
                        self.automatic_failure(row["token"], row.get("abort_requested") or "这一轮超过规则期限")
                    else:
                        self.check_round(row)
                    row = self.log.get("rounds", row["token"])
                    if row["status"] == "finishing":
                        self.finish(row)
                except Pending:
                    continue  # 这一轮在等作业出结果，下一拍再看。
                except Retryable as exc:
                    self.start_retry(item, row, "executor", row["executor"], exc)
                except Exception as exc:
                    self.broken(item["target"], "executor", row["executor"], exc)
            for item in self.log.all("items"):
                try:
                    self.advance(item)
                except Pending:
                    continue  # 这件活在等作业出结果，下一拍再看；别的活照常推进。
            self.release_workdirs()
            self.channels()
            self.heartbeat(began)

    def advance(self, item):
        control = self.control(item["target"])
        if control.get("paused") or control.get("broken"):
            return
        if (item["pending_rework"] and item["depth"] <= self.rules.data["limits"]["chain_depth"]
                and not any(r["item"] == item["id"] for r in self.active())):
            if self.jobs and item.get("workdir") and not item.get("workdir_released"):
                identity = [item["id"], item.get("pending_episode", 0), item["workdir"], item.get("release_at")]
                if any(self.jobs.exists("executor", name, "release", identity)
                       for name in item.get("sessions") or {}):
                    # 正在收回的目录不能同时给新一轮用；本拍末尾先结算收回结果。
                    return
            with self.log.transaction():
                self.cancel_questions(item)
                self.change(item, stage="执行" if item["checks"] else "说清楚", state="空闲",
                            **({} if item["checks"] else {"serial_hold": False}),
                            pending_rework=False, approved=[], pending_advance=None, reviews=[], review_required=1,
                            after_pending_count=0, after_wait=None, pending_episode=item.get("pending_episode", 0) + 1,
                            wait_for_ready=False, retry_executor=None, gate_count=0, next="按新事件返工")
            item = self.log.get("items", item["id"])
        self.recheck_after(item)
        item = self.log.get("items", item["id"])
        self.complete_approval(item)
        item = self.log.get("items", item["id"])
        if not self.taskhome(item):
            return
        item = self.log.get("items", item["id"])
        if item["state"] in ("空闲", "排队") and not item.get("after_wait"):
            item = self.serial_queue(item)
        if item["state"] == "空闲" and not item.get("after_wait") and self.occupied() < self.rules.data["limits"]["concurrent"]:
            self.reserve(item)
