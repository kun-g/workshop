"""按候选顺序取第一个额度够的；额度从本机 ai_quota 快照库只读读取，不调厂商接口。

规则写法：
- 候选上 `quota: {provider, window, min_remaining, label}` 是额度门槛；没写或没写 min_remaining 的候选不看额度。
- 候选上 `first_when_writer: [家]` 表示写代码的是这些家时，把它排到最前（审查、验收换家时用）。
- 交付对象上 `quota_snapshot: {path, max_age_minutes}` 指快照库和过期线。
全部候选都被跳过时返回 Selection(None, 理由)，核心记下理由再发「没有可用执行者」卡。
"""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from workshop.core.models import Selection

DEFAULT_PATH = "~/.local/state/ai_quota/quota.db"
DEFAULT_MAX_AGE_MINUTES = 45


class Unreadable(Exception):
    """快照读不到、缺窗口或过期；有门槛的候选因此跳过。"""


class Snapshot:
    def __init__(self, settings, now):
        self.path = Path(str(settings.get("path", DEFAULT_PATH))).expanduser()
        self.max_age = settings.get("max_age_minutes", DEFAULT_MAX_AGE_MINUTES)
        if type(self.max_age) not in (int, float) or self.max_age <= 0:
            raise ValueError("quota_snapshot.max_age_minutes 必须是正数")
        self.now = now
        self.cache = {}

    def remaining(self, provider, window):
        """返回该家该窗口的剩余百分比；读不到时抛 Unreadable 并写明原因。"""
        if provider not in self.cache:
            self.cache[provider] = self._latest(provider)
        collected, windows = self.cache[provider]
        age = (self.now - collected).total_seconds() / 60
        if age > self.max_age:
            raise Unreadable(f"额度快照过期（{provider} 最近一次成功采集在 {age:.0f} 分钟前，超过 {self.max_age:g} 分钟）")
        if window not in windows or windows[window] is None:
            raise Unreadable(f"额度快照里没有 {provider} 的 {window} 窗口")
        return round(100 - windows[window], 2)

    def _latest(self, provider):
        if not self.path.is_file():
            raise Unreadable(f"额度快照读不到（{self.path} 不存在）")
        try:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
            try:
                payload = connection.execute(
                    "SELECT id, collected_at FROM payloads WHERE provider = ? AND source = 'codexbar' AND error IS NULL "
                    "ORDER BY id DESC LIMIT 1", (provider,)).fetchone()
                readings = connection.execute(
                    "SELECT window_name, used_percent FROM readings WHERE payload_id = ? AND window_name IS NOT NULL",
                    (payload[0],)).fetchall() if payload else []
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise Unreadable(f"额度快照读不到（{exc}）") from exc
        if not payload:
            raise Unreadable(f"额度快照里没有 {provider} 的成功采集")
        try:
            collected = datetime.fromisoformat(payload[1])
        except ValueError as exc:
            raise Unreadable(f"额度快照时间无法解析（{payload[1]}）") from exc
        if collected.tzinfo is None:
            collected = collected.replace(tzinfo=timezone.utc)
        return collected, {name: used for name, used in readings}


def threshold(config):
    quota = config.get("quota")
    if quota is None:
        return None
    if not isinstance(quota, dict) or not all(isinstance(quota.get(k), str) and quota[k] for k in ("provider", "window")):
        raise ValueError(f'{config["use"]} 的 quota 必须写 provider 和 window')
    minimum = quota.get("min_remaining")
    if minimum is None:
        return None
    if type(minimum) not in (int, float) or not 0 <= minimum <= 100:
        raise ValueError(f'{config["use"]} 的 quota.min_remaining 必须在 0 到 100 之间')
    return quota


class QuotaSelector:
    def __init__(self, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def select(self, target, stage, round_number, history, candidates):
        if not candidates:
            return None
        writer = target.get("selection", {}).get("writer")
        ordered = sorted(candidates, key=lambda c: writer is None or writer not in c["config"].get("first_when_writer", []))
        notes = []
        if writer is not None and ordered[0] is not candidates[0] and writer in ordered[0]["config"].get("first_when_writer", []):
            notes.append(f'写代码的是 {writer} 家，{ordered[0]["config"]["use"]} 排第一')
        snapshot = None
        suffix = f"；阶段 {stage}，第 {round_number} 轮"
        for candidate in ordered:
            config = candidate["config"]
            quota = threshold(config)
            if quota is None:
                return Selection(config, "；".join(notes + [f'选 {config["use"]}（无额度门槛）']) + suffix)
            label = quota.get("label") or f'{quota["provider"]} {quota["window"]}'
            if snapshot is None:
                snapshot = Snapshot(target.get("quota_snapshot") or {}, self.clock())
            try:
                left = snapshot.remaining(quota["provider"], quota["window"])
            except Unreadable as exc:
                notes.append(f'{exc}，跳过 {config["use"]}')
                continue
            if left >= quota["min_remaining"]:
                return Selection(config, "；".join(notes + [f'{label}剩 {left:g}%（门槛 {quota["min_remaining"]:g}%），选 {config["use"]}']) + suffix)
            notes.append(f'{label}剩 {left:g}%，低于门槛 {quota["min_remaining"]:g}%，跳过 {config["use"]}')
        # 全被跳过：不选人，但把每个候选为什么被跳过交回核心记下。
        return Selection(None, "；".join(notes) + suffix)


def make_selector():
    return QuotaSelector()
