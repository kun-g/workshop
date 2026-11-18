"""一轮一轮推进：外部副作用有持久标识；视图只经事实演进。"""
from contextlib import contextmanager
import fcntl

from .flows import default_flow, entries
from .log import Log
from .models import stamp, utcnow
from .records import defer_items, materialize
from .stages import serial_stage
from . import asking, beat, faults, holding, homes, hooks, intake, picking, rounds, timing, waking, workdirs, waits


def finder(log, target):
    """给流程判定用的只读查找：按外部编号找卡，或按对象键（先解析别名）找同目标的活。"""
    scope = target if isinstance(target, dict) else {"target": target}
    target = scope["target"]
    prefix = target if scope.get("flow_default", True) else f'{target}:{scope["flow"]}'

    def resolve(object_key):
        alias = log.get("aliases", f"{prefix}:{object_key}")
        return alias["object_key"] if alias else object_key

    def find(external=None, *, object_key=None, flow=None):
        if flow is not None:
            selected = scope.get("flows", {}).get(flow)
            if selected is None:
                raise ValueError(f"查找未配置的流程：{flow}")
            return finder(log, selected)(external, object_key=object_key)
        if object_key is not None:
            found = log.get("items", f"{prefix}:{resolve(object_key)}")
            return {"item": log.full_item(found)} if found else None
        matches = [c for c in log.all("cards") if c.get("external") == external]
        if len(matches) != 1:
            return None
        item = log.get("items", matches[0]["item"])
        return {"card": matches[0], "item": log.full_item(item)} if item and item["target"] == target and item["id"] == f'{prefix}:{item["object_key"]}' else None
    return find


class Engine:

    def __init__(self, log: Log, rules, plugins, clock=utcnow, jobs=None):
        """jobs 为空时插件调用就地进行（测试和单次命令）；守护进程传入作业，一拍里不等任何外部调用。"""
        if hasattr(plugins, "configure"):
            rules = plugins.configure(rules)
        self.log, self.rules, self.plugins, self.clock, self.jobs = log, rules, plugins, clock, jobs
        self.restart_requested = False
        self.channel_read_tokens = {}
        self.tick_failures = self.control("@tick").get("failures", 0)
        saved = self.log.get("controls", "@rules") or {}
        pinned = dict(self.control("@flow-defaults").get("values", {}))
        targets = {}
        for name, config in rules.data['targets'].items():
            old = saved.get('data', {}).get('targets', {}).get(name, {})
            chosen = pinned.get(name) or (default_flow(old) if old else default_flow(config))
            if chosen not in entries(config):
                raise ValueError(f'{name} 兼容流程 {chosen} 不能移除或换名')
            if config.get('default_flow', chosen) != chosen:
                raise ValueError(f'{name} default_flow 不能改变已有活的归属')
            pinned[name] = chosen
            targets[name] = {**config, 'default_flow': chosen} if 'flows' in config else config
        if pinned != self.control('@flow-defaults').get('values'):
            self.fact('control', {'id': '@flow-defaults', 'values': pinned})
        rules = type(rules)({**rules.data, 'targets': targets}, rules.path)
        self.rules = rules

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
        # 离开占位区间立即让出；从区间外返工也不能继承旧版本残留的占位。
        if "stage" in changes and (not serial_stage(item["stage"]) or not serial_stage(changes["stage"])):
            changes["serial_hold"] = False
        # 等回答必须有本活仍有效的问题。通知、已作废的卡不能充当问题。
        if changes.get("state") == "等你" and not self.answerable({**item, **changes}):
            reason = changes.get("gate_reason") or (changes.get("leave") or {}).get("about") or changes.get("next") or "缺少有效问卡"
            changes.update(state="卡住", blocked_by=reason,
                           next="项目经理处理阻塞；需要 Kun 判断时提交带推荐的问卡")
        if changes.get("state") == "卡住" and (changes.get("leave") or {}).get("kind") == "等你":
            changes["leave"] = self.blocked_leave(changes.get("next") or "项目经理处理阻塞")
        self.fact(kind, changes, item["id"])

    def blocked_leave(self, action):
        return {"kind": "工序", "owner": "项目经理", "at": stamp(self.clock()), "about": action}

    def control(self, target):
        return self.log.get("controls", target) or {}

    def call(self, port, name, method, *args, identity=None, item=None, lookup=None):
        """调一次插件。有作业时：结果已出就返回，没出就登记或认回作业并抛 Pending，下一拍再来取。"""
        args = defer_items(args, self.log.path.resolve())
        if self.jobs is None:
            args = materialize(args)
            if lookup is not None:
                args = (*args, finder(self.log, lookup))
            return getattr(self.plugins.get(port, name), method)(*args)
        return self.jobs.call(port, name, method, args, identity=identity, item=item, lookup=lookup)

    def item_target(self, item):
        return self.rules.target(item['target'], item.get('flow', ''))

    def scope(self, target, flow=''):
        config = self.rules.target(target, flow)
        return {'target': target, 'flow': config['flow'], 'flow_default': config['flow_default']}

    def prefix(self, target, flow=''):
        scope = self.scope(target, flow)
        return target if scope['flow_default'] else f"{target}:{scope['flow']}"

    def flow_broken(self, target, flow=''):
        config = self.rules.target(target, flow)
        return bool(self.control(target).get('broken') or
                    self.control(f"@flow:{target}:{config['plugin']}").get('broken'))

    def resolve_key(self, target, object_key, flow=''):
        alias = self.log.get("aliases", f"{self.prefix(target, flow)}:{object_key}")
        return alias["object_key"] if alias else object_key

    def find_external(self, target, external):
        return finder(self.log, target)(external)

    def after(self, *args, **kwargs):
        return hooks.after(self, *args, **kwargs)

    def pending_changes(self, *args, **kwargs):
        return hooks.pending_changes(self, *args, **kwargs)

    def recheck_after(self, *args, **kwargs):
        return hooks.recheck_after(self, *args, **kwargs)

    def cleanup_flows(self, *args, **kwargs):
        return workdirs.cleanup_flows(self, *args, **kwargs)

    def release_workdirs(self, *args, **kwargs):
        return workdirs.release_workdirs(self, *args, **kwargs)

    def ingest(self, *args, **kwargs):
        return intake.ingest(self, *args, **kwargs)

    def record_event(self, *args, **kwargs):
        return intake.record_event(self, *args, **kwargs)

    def classification_intent(self, *args, **kwargs):
        return intake.classification_intent(self, *args, **kwargs)

    def classify_events(self, *args, **kwargs):
        return intake.classify_events(self, *args, **kwargs)

    def after_abandon(self, *args, **kwargs):
        return intake.after_abandon(self, *args, **kwargs)

    def drop(self, *args, **kwargs):
        return holding.drop(self, *args, **kwargs)

    def abandon(self, *args, **kwargs):
        return holding.abandon(self, *args, **kwargs)

    def recover(self, *args, **kwargs):
        return holding.recover(self, *args, **kwargs)

    def pause(self, *args, **kwargs):
        return holding.pause(self, *args, **kwargs)

    def missed(self, *args, **kwargs):
        return holding.missed(self, *args, **kwargs)

    def resume(self, *args, **kwargs):
        return holding.resume(self, *args, **kwargs)

    def restore_target(self, *args, **kwargs):
        return holding.restore_target(self, *args, **kwargs)

    def submit(self, *args, **kwargs):
        return intake.submit(self, *args, **kwargs)

    def advance_error(self, *args, **kwargs):
        return intake.advance_error(self, *args, **kwargs)

    @staticmethod
    def failure_reason(*args, **kwargs):
        return intake.failure_reason(*args, **kwargs)

    def receipt_rejected(self, *args, **kwargs):
        return intake.receipt_rejected(self, *args, **kwargs)

    def card(self, *args, **kwargs):
        return asking.card(self, *args, **kwargs)

    def receipt_notices(self, *args, **kwargs):
        return asking.receipt_notices(self, *args, **kwargs)

    def timer(self, *args, **kwargs):
        return timing.timer(self, *args, **kwargs)

    def block(self, *args, **kwargs):
        return faults.block(self, *args, **kwargs)

    def broken(self, *args, **kwargs):
        return faults.broken(self, *args, **kwargs)

    def waiting(self, item):
        # 拒收/作废后的待放行也阻止自动执行，但它不属于「等你」。
        return self.answerable(item) or any(c["item"] == item["id"] and c.get("awaiting_release")
                                          for c in self.log.all("cards"))

    def answerable(self, item):
        return waits.active(self, item) or any(c["item"] == item["id"] and c["mode"] == "ask" and not c["answered"]
                   and not c.get("cancelled") and not c.get("rejected")
                   for c in self.log.all("cards"))

    def sources(self, *args, **kwargs):
        return intake.sources(self, *args, **kwargs)

    def source_retry(self, *args, **kwargs):
        return intake.source_retry(self, *args, **kwargs)

    def delivery_error(self, *args, **kwargs):
        return faults.delivery_error(self, *args, **kwargs)

    def delivery_ok(self, *args, **kwargs):
        return faults.delivery_ok(self, *args, **kwargs)

    def invalid_answer(self, *args, **kwargs):
        return asking.invalid_answer(self, *args, **kwargs)

    def reconcile_uncertain(self, *args, **kwargs):
        return asking.reconcile_uncertain(self, *args, **kwargs)

    def reconcile_clear(self, *args, **kwargs):
        return asking.reconcile_clear(self, *args, **kwargs)

    def undeliverable(self, *args, **kwargs):
        return asking.undeliverable(self, *args, **kwargs)

    def channels(self, *args, **kwargs):
        return asking.channels(self, *args, **kwargs)

    def channel_reads(self, *args, **kwargs):
        return asking.channel_reads(self, *args, **kwargs)

    def cancel_questions(self, *args, **kwargs):
        return asking.cancel_questions(self, *args, **kwargs)

    def timers(self, *args, **kwargs):
        return timing.timers(self, *args, **kwargs)

    def record_answer(self, *args, **kwargs):
        return asking.record_answer(self, *args, **kwargs)

    def apply_timer(self, *args, **kwargs):
        return timing.apply_timer(self, *args, **kwargs)

    def effect(self, *args, **kwargs):
        return hooks.effect(self, *args, **kwargs)

    def taskhome(self, *args, **kwargs):
        return homes.taskhome(self, *args, **kwargs)

    def active(self):
        return self.log.all("rounds", unless=("status", "ended"))

    def occupied(self):
        return sum(not row.get("stopped") for row in self.active())

    def launch(self, *args, **kwargs):
        return picking.launch(self, *args, **kwargs)

    def reserve(self, *args, **kwargs):
        return picking.reserve(self, *args, **kwargs)

    def start_program(self, *args, **kwargs):
        return picking.start_program(self, *args, **kwargs)

    def recently_unavailable(self, *args, **kwargs):
        return picking.recently_unavailable(self, *args, **kwargs)

    def release_unavailable(self, *args, **kwargs):
        return picking.release_unavailable(self, *args, **kwargs)

    def serial_holder(self, *args, **kwargs):
        return picking.serial_holder(self, *args, **kwargs)

    def serial_queue(self, *args, **kwargs):
        return picking.serial_queue(self, *args, **kwargs)

    def runtime(self, *args, **kwargs):
        return picking.runtime(self, *args, **kwargs)

    def retry_due(self, *args, **kwargs):
        return faults.retry_due(self, *args, **kwargs)

    def retry_clear(self, *args, **kwargs):
        return faults.retry_clear(self, *args, **kwargs)

    def retry_failure(self, *args, **kwargs):
        return faults.retry_failure(self, *args, **kwargs)

    def start_retry(self, *args, **kwargs):
        return faults.start_retry(self, *args, **kwargs)

    def answer_here(self, *args, **kwargs):
        return asking.answer_here(self, *args, **kwargs)

    def release_here(self, *args, **kwargs):
        return holding.release_here(self, *args, **kwargs)

    def abort(self, *args, **kwargs):
        return rounds.abort(self, *args, **kwargs)

    def automatic_failure(self, *args, **kwargs):
        return rounds.automatic_failure(self, *args, **kwargs)

    def check_round(self, *args, **kwargs):
        return rounds.check_round(self, *args, **kwargs)

    def required_reviews(self, *args, **kwargs):
        return rounds.required_reviews(self, *args, **kwargs)

    def failure_changes(self, *args, **kwargs):
        return rounds.failure_changes(self, *args, **kwargs)

    def gate_retry(self, *args, **kwargs):
        return rounds.gate_retry(self, *args, **kwargs)

    def settle_executor(self, *args, **kwargs):
        return rounds.settle_executor(self, *args, **kwargs)

    def finish(self, *args, **kwargs):
        return rounds.finish(self, *args, **kwargs)

    def complete_approval(self, *args, **kwargs):
        return rounds.complete_approval(self, *args, **kwargs)

    def wake_paths(self, *args, **kwargs):
        return waking.wake_paths(self, *args, **kwargs)

    def awaken(self, *args, **kwargs):
        return waking.awaken(self, *args, **kwargs)

    def wait_timeout(self, *args, **kwargs):
        return waking.wait_timeout(self, *args, **kwargs)

    def tick(self, received_at=None):
        beat.tick(self, received_at)

    def heartbeat(self, *args, **kwargs):
        return beat.heartbeat(self, *args, **kwargs)

    def _tick(self, began=None):
        beat.one(self, began)

    def advance(self, item):
        beat.advance(self, item)

