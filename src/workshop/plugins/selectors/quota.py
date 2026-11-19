"""按候选顺序取第一个额度够的；额度从本机 ai_quota 快照库只读读取，不调厂商接口。

规则写法：
- 候选上 `quota: {provider, window, min_remaining, pace_margin, label}` 是额度门槛：保底或配速满足一条就放行，
  两个都没写的候选不看额度（判断见 workshop.quota_gate.judge）。
- 候选上 `first_when_writer: [家]` 表示写代码的是这些家时，把它排到最前（审查、验收换家时用）。
- 交付对象上 `quota_snapshot: {path, max_age_minutes}` 指快照库和过期线。
全部候选都被跳过时返回 Selection(None, 理由)，核心记下理由再发「没有可用执行者」卡。
"""
from datetime import datetime, timezone

from workshop.core.models import Selection
from workshop.quota_gate import Snapshot, Unreadable, judge, threshold

__all__ = ["QuotaSelector", "Snapshot", "Unreadable", "threshold", "make_selector"]


class QuotaSelector:
    def __init__(self, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def select(self, target, stage, round_number, history, candidates):
        if not candidates:
            return None
        writer = target.get("selection", {}).get("writer")
        ordered = sorted(candidates, key=lambda c: writer is None or writer not in c["config"].get("first_when_writer", []))
        notes = []
        takeover = target.get("selection", {}).get("takeover")
        if takeover:
            # 阶梯指定了接手者：核心只交来它一个，额度门槛照样看（ws#301）。
            notes.append(f"阶梯指定的接手者 {takeover}，只交它")
        if writer is not None and ordered[0] is not candidates[0] and writer in ordered[0]["config"].get("first_when_writer", []):
            notes.append(f'写代码的是 {writer} 家，{ordered[0]["config"]["use"]} 排第一')
        snapshot = None
        suffix = f"；阶段 {stage}，第 {round_number} 轮"
        for candidate in ordered:
            config = candidate["config"]
            quota = threshold(config)
            if quota is None:
                return Selection(config, "；".join(notes + [f'选 {config["use"]}（无额度门槛）']) + suffix)
            if snapshot is None:
                snapshot = Snapshot(target.get("quota_snapshot") or {}, self.clock())
            try:
                passed, text = judge(quota, snapshot)
            except Unreadable as exc:
                notes.append(f'{exc}，跳过 {config["use"]}')
                continue
            if passed:
                return Selection(config, "；".join(notes + [f'{text}，选 {config["use"]}']) + suffix)
            notes.append(f'{text}，跳过 {config["use"]}')
        # 全被跳过：不选人，但把每个候选为什么被跳过交回核心记下。
        return Selection(None, "；".join(notes) + suffix)


def make_selector():
    return QuotaSelector()
