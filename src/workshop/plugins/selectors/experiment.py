"""实验分配：按活号把每件活固定分给阶段候选里的一个，用来比不同执行者做活的效果。

- 可用候选 = 核心交来的候选（已去掉不可用、按量计费的）− 额度不够的（额度门槛和 quota 挑法同一套，见 workshop.quota_gate）。
- 本该分给谁 = 规则候选[哈希(活号) 对规则候选数取模]；它可用就分给它，同一件活每一轮都算出同一个。
- 它这一轮不可用时，分给 可用候选[哈希(活号) 对可用候选数取模]，理由写明本该分给谁、为什么换。
- 一个可用的都没有时返回 Selection(None, 理由)，核心照 quota 挑法的情形记卡住。
活号和规则候选名单从核心交来的挑选上下文 target["selection"] 的 item、rule_candidates 读。
"""
from datetime import datetime, timezone
import hashlib

from workshop.core.models import Selection
from workshop.quota_gate import Snapshot, Unreadable, label, threshold


def bucket(item, size):
    """活号的稳定哈希对 size 取模；不用 Python 的 hash()，它每个进程加盐。"""
    return int.from_bytes(hashlib.sha256(item.encode("utf-8")).digest()[:8], "big") % size


class ExperimentSelector:
    def __init__(self, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def select(self, target, stage, round_number, history, candidates):
        if not candidates:
            return None
        selection = target.get("selection", {})
        suffix = f"；阶段 {stage}，第 {round_number} 轮"
        item = selection.get("item") or next((row.get("item") for row in history if row.get("item")), None)
        if not isinstance(item, str) or not item:
            return Selection(None, "实验分配拿不到活号，不分配" + suffix)
        usable, skipped, notes = [], {}, []
        snapshot = None
        for candidate in candidates:
            config = candidate["config"]
            quota = threshold(config)
            if quota is None:
                usable.append(config)
                continue
            if snapshot is None:
                snapshot = Snapshot(target.get("quota_snapshot") or {}, self.clock())
            try:
                left = snapshot.remaining(quota["provider"], quota["window"])
            except Unreadable as exc:
                skipped[config["use"]] = str(exc)
                continue
            if left >= quota["min_remaining"]:
                usable.append(config)
                notes.append(f'{label(quota)}剩 {left:g}%（门槛 {quota["min_remaining"]:g}%）')
            else:
                skipped[config["use"]] = f'{label(quota)}剩 {left:g}%，低于门槛 {quota["min_remaining"]:g}%'
        rule = list(dict.fromkeys(selection.get("rule_candidates") or [c["config"]["use"] for c in candidates]))
        counts = f"规则候选 {len(rule)} 个，可用 {len(usable)} 个"
        if not usable:
            reasons = [f"{name}：{why}" for name, why in skipped.items()]
            return Selection(None, "；".join([f"实验分配：{item} 没有可用候选，{counts}"] + reasons) + suffix)
        names = [config["use"] for config in usable]
        intended = rule[bucket(item, len(rule))]
        if intended in names:
            chosen = usable[names.index(intended)]
        else:
            chosen = usable[bucket(item, len(usable))]
            notes.insert(0, f'本该分给 {intended}，{self.why_not(intended, skipped, selection)}，改分给 {chosen["use"]}')
        return Selection(chosen, "；".join([f'实验分配：{item} 分给 {chosen["use"]}，{counts}'] + notes) + suffix)

    @staticmethod
    def why_not(name, skipped, selection):
        if name in skipped:
            return skipped[name]
        retry = selection.get("retry_executor")
        if retry and retry != name:
            return f"这一轮是重试，须沿用 {retry}"
        if name in (selection.get("excluded") or []):
            return "它最近启动时报不可用"
        return "它报不可用或按量计费，核心没交来"


def make_selector():
    return ExperimentSelector()
