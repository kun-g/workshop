"""额度门槛：从本机 ai_quota 快照库只读读取剩余百分比，quota 和 experiment 两种挑法共用。

候选上 `quota: {provider, window, min_remaining, pace_margin, label}` 是额度门槛：
剩余不低于 min_remaining（保底），或不低于「匀速线 − pace_margin」（配速），满足写了的任意一条就放行；
两个都没写的候选不看额度。匀速线 = 100 × 距重置还剩的时间 ÷ 窗口长度，取快照里该窗口的重置时间和窗口长度、挑选时的现在算。
交付对象上 `quota_snapshot: {path, max_age_minutes}` 指快照库和过期线。
个人账号的快照不管谁采的（PERSONAL_SOURCES 里的来源都认），取该家按采集时间最新的一次成功采集；
团队池（omc）和别的来源不当个人账号的额度读。
"""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


DEFAULT_PATH = "~/.local/state/ai_quota/quota.db"
DEFAULT_MAX_AGE_MINUTES = 45
PERSONAL_SOURCES = ("codexbar", "magpie")


class Unreadable(Exception):
    """快照读不到、缺窗口或过期；有门槛的候选因此跳过。"""


class PaceUnknown(Exception):
    """配速这一条算不了：缺重置时间或窗口长度、格式不对、或重置时间已过。按不满足处理。"""


class Snapshot:
    def __init__(self, settings, now):
        self.path = Path(str(settings.get("path", DEFAULT_PATH))).expanduser()
        self.max_age = settings.get("max_age_minutes", DEFAULT_MAX_AGE_MINUTES)
        if type(self.max_age) not in (int, float) or self.max_age <= 0:
            raise ValueError("quota_snapshot.max_age_minutes 必须是正数")
        self.now = now
        self.cache = {}
        self.pace_inputs = {}

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

    def pace(self, provider, window):
        """返回该窗口的匀速线（0 到 100）；算不了时抛 PaceUnknown 写明原因。先调 remaining，快照过期的检查在那里。"""
        if provider not in self.cache:
            self.cache[provider] = self._latest(provider)
        resets, minutes = self.pace_inputs.get(provider, {}).get(window, (None, None))
        if resets is None or resets == "":
            raise PaceUnknown("没有重置时间")
        if minutes is None:
            raise PaceUnknown("没有窗口长度")
        try:
            reset_at = datetime.fromisoformat(resets) if isinstance(resets, str) else None
        except ValueError:
            reset_at = None
        if reset_at is None or reset_at.tzinfo is None:
            raise PaceUnknown(f"重置时间不是带时区的时间（{resets}）")
        if type(minutes) not in (int, float) or not minutes > 0:
            raise PaceUnknown(f"窗口长度不是正数（{minutes}）")
        left = (reset_at - self.now).total_seconds() / 60
        if left <= 0:
            raise PaceUnknown("重置时间已过，等下一次采集")
        return round(min(100.0, max(0.0, 100 * left / minutes)), 2)

    def _latest(self, provider):
        if not self.path.is_file():
            raise Unreadable(f"额度快照读不到（{self.path} 不存在）")
        try:
            connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
            try:
                # 每个来源各取最近一次成功采集，再按采集时间比谁更新。
                payloads = [row for source in PERSONAL_SOURCES for row in [connection.execute(
                    "SELECT id, collected_at FROM payloads WHERE provider = ? AND source = ? AND error IS NULL "
                    "ORDER BY id DESC LIMIT 1", (provider, source)).fetchone()] if row]
                payload = max(((_collected_at(row[1]), row[0]) for row in payloads), default=None)
                readings = connection.execute(
                    "SELECT window_name, used_percent FROM readings WHERE payload_id = ? AND window_name IS NOT NULL",
                    (payload[1],)).fetchall() if payload else []
                # 配速另外取同一次采集的重置时间和窗口长度，不动上面取剩余百分比的读法。
                self.pace_inputs[provider] = {name: (resets, minutes) for name, resets, minutes in connection.execute(
                    "SELECT window_name, resets_at, window_minutes FROM readings WHERE payload_id = ? AND window_name IS NOT NULL",
                    (payload[1],)).fetchall()} if payload else {}
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise Unreadable(f"额度快照读不到（{exc}）") from exc
        if not payload:
            raise Unreadable(f"额度快照里没有 {provider} 的成功采集")
        return payload[0], {name: used for name, used in readings}


def _collected_at(text):
    try:
        collected = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise Unreadable(f"额度快照时间无法解析（{text}）") from exc
    if collected.tzinfo is None:
        collected = collected.replace(tzinfo=timezone.utc)
    return collected

def threshold(config):
    """候选的额度门槛；min_remaining 和 pace_margin 都没写时为 None（不看额度）。写错抛 ValueError，带执行者名字。"""
    quota = config.get("quota")
    if quota is None:
        return None
    if not isinstance(quota, dict) or not all(isinstance(quota.get(k), str) and quota[k] for k in ("provider", "window")):
        raise ValueError(f'{config["use"]} 的 quota 必须写 provider 和 window')
    minimum = quota.get("min_remaining")
    margin = quota.get("pace_margin")
    if minimum is None and margin is None:
        return None
    if minimum is not None and (type(minimum) not in (int, float) or not 0 <= minimum <= 100):
        raise ValueError(f'{config["use"]} 的 quota.min_remaining 必须在 0 到 100 之间')
    if margin is not None and (type(margin) not in (int, float) or not -100 <= margin <= 100):
        raise ValueError(f'{config["use"]} 的 quota.pace_margin 必须是 -100 到 100 之间的数')
    return quota


def judge(quota, snapshot):
    """按门槛判断放不放行，返回 (放行, 理由正文)；快照读不到或过期时抛 Unreadable。两种挑法共用，前后缀各自加。

    只写 min_remaining 时理由与原来逐字相同：「X剩 L%（门槛 M%）」或「X剩 L%，低于门槛 M%」。
    """
    name = label(quota)
    left = snapshot.remaining(quota["provider"], quota["window"])
    minimum = quota.get("min_remaining")
    margin = quota.get("pace_margin")
    if margin is None:
        if left >= minimum:
            return True, f"{name}剩 {left:g}%（门槛 {minimum:g}%）"
        return False, f"{name}剩 {left:g}%，低于门槛 {minimum:g}%"
    floor = f"门槛 {minimum:g}%，" if minimum is not None else ""
    try:
        pace = snapshot.pace(quota["provider"], quota["window"])
    except PaceUnknown as exc:
        if minimum is None:
            return False, f"{name}剩 {left:g}%，配速算不了（{exc}），没写保底"
        numbers = f"{name}剩 {left:g}%（门槛 {minimum:g}%，配速算不了：{exc}）"
        if left >= minimum:
            return True, numbers + "，按保底放行"
        return False, numbers + f"，低于门槛 {minimum:g}%"
    line = round(pace - margin, 2)
    numbers = f"{name}剩 {left:g}%（{floor}匀速线 {pace:g}%，余量 {margin:g}）"
    if minimum is not None and left >= minimum:
        return True, numbers + "，按保底放行"
    if left >= line:
        return True, numbers + "，按配速放行"
    below = f"低于门槛 {minimum:g}%，也" if minimum is not None else ""
    return False, numbers + f"，{below}低于匀速线减余量 {line:g}%"


def label(quota):
    return quota.get("label") or f'{quota["provider"]} {quota["window"]}'
