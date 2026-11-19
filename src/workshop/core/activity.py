"""业务运行快照；后台查询和修理会话不算业务。

空闲时也要每拍算一次。作业目录只增不减（结果留一天、被复用还会刷新），
不能每拍把所有 spec.json、result.json 读一遍（2026-11-19 线上 2383 个目录、
234MB，每拍约 540ms）。心跳只做两件事：
- 上一拍还在跑的业务作业（job_ids）逐个核对锁和结果，数量是在跑的作业数；
- 只看目录 mtime 在上一拍之后的作业目录，读的只是新开工、刚完成或刚被改过的。
旧目录只 stat 不读文件。万一没有上一拍的 scanned_at（首次运行或旧版心跳），
退化成全量扫描一次，之后照上面走。
"""
import os
from datetime import datetime
from .jobs import held, read


BUSINESS = {('flow', 'after'), ('flow', 'assess'), ('flow', 'brief'),
            ('flow', 'classify'), ('taskhome', 'open'), ('taskhome', 'update')}

# 判定「目录在这一拍之后可能变过」时往回看几秒。秒级文件系统的时间精度、
# 时钟微调都会让写入的 mtime 略早于扫描开始时刻；多看几秒只是可能多读几个
# 近期目录，不会随历史作业数增长。
LOOKBACK_SECONDS = 5


def stamp_of(result, now, tz):
    """结果里的完成时刻；晚于 now 的不算（时钟回拨时也不把空闲段推到未来）。"""
    end = (result or {}).get('finished')
    if isinstance(end, (int, float)):
        stamp = datetime.fromtimestamp(end, tz).isoformat()
        if stamp <= now:
            return stamp
    return None


def later(latest, stamp):
    if stamp and (not latest or stamp > latest):
        return stamp
    return latest


def business(record):
    return bool(record and record.get('item')
                and (record.get('port'), record.get('method')) in BUSINESS)


def result_business(path, result):
    """结果里带调用身份（现在的作业进程会写）；旧结果回落到读 spec。"""
    if 'port' in result and 'method' in result:
        return business(result)
    return business(read(path / 'spec.json'))


def watermark(previous, now):
    """上一拍的扫描起点；缺失或晚于 now（时钟回拨）时返回 None，只这一拍全量扫。"""
    since = previous.get('scanned_at')
    if not since:
        return None
    try:
        if datetime.fromisoformat(since) > datetime.fromisoformat(now):
            return None
        return datetime.fromisoformat(since).timestamp()
    except (TypeError, ValueError):
        return None


def snapshot(engine, previous, now):
    tokens = [r['token'] for r in engine.active()
              if r['status'] == 'running' and not r.get('stopped') and r.get('receipt') is None]
    jobs = {}
    root = engine.log.path.parent / 'jobs'
    latest = previous.get('last_activity_at')
    tz = datetime.fromisoformat(now).tzinfo
    since = watermark(previous, now)
    if root.is_dir():
        if since is not None:
            # 上一拍在跑的作业这一拍目录可能毫无改动，先照抄再核对：
            # 结果出来会刷新目录 mtime，同时会在这里或下一段被读到。
            names = previous.get('job_ids') or []
            if not isinstance(names, (list, tuple)):
                names = ()
            for name in names:
                path = root / name
                result = read(path / 'result.json')
                if result is None and held(path):
                    jobs[name] = None
                else:
                    latest = later(latest, stamp_of(result, now, tz))
        try:
            with os.scandir(root) as scan:
                entries = list(scan)
        except OSError:
            entries = []
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                changed = entry.stat().st_mtime
            except OSError:
                continue
            name = entry.name
            if name in jobs:
                continue
            if since is not None and changed < since - LOOKBACK_SECONDS:
                continue
            path = root / name
            result = read(path / 'result.json')
            if result is None:
                if business(read(path / 'spec.json')) and held(path):
                    jobs[name] = None
                continue
            if not result_business(path, result):
                continue
            # 即使作业在两拍之间完成，完成时间仍打断空闲段。
            latest = later(latest, stamp_of(result, now, tz))
    # 一轮的最后结算也属于业务；避免极短轮跨两拍被漏掉。
    at = engine.log.db.execute("SELECT max(json_extract(body,'$.ended')) FROM rounds").fetchone()[0]
    if at and at <= now and (not latest or at > latest):
        latest = at
    total = len(tokens) + len(jobs)
    if total:
        latest = now
    if not total and (not previous or previous.get('total', 0) > 0):
        latest = now
    idle_since = None if total else max(filter(None, (previous.get('idle_since'), latest)), default=now)
    return {'agents': len(tokens), 'jobs': len(jobs), 'total': total,
            'round_tokens': tokens, 'job_ids': list(jobs), 'last_activity_at': latest,
            'idle_since': idle_since, 'scanned_at': now}
