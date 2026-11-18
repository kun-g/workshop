"""业务运行快照；后台查询和修理会话不算业务。"""
from datetime import datetime
from .jobs import held, read


BUSINESS = {('flow', 'after'), ('flow', 'assess'), ('flow', 'brief'),
            ('flow', 'classify'), ('taskhome', 'open'), ('taskhome', 'update')}


def snapshot(engine, previous, now):
    tokens = [r['token'] for r in engine.active()
              if r['status'] == 'running' and not r.get('stopped') and r.get('receipt') is None]
    jobs = []
    root = engine.log.path.parent / 'jobs'
    latest = previous.get('last_activity_at')
    if root.exists():
        for path in root.iterdir():
            spec = read(path / 'spec.json')
            if not spec or not spec.get('item') or (spec.get('port'), spec.get('method')) not in BUSINESS:
                continue
            result = read(path / 'result.json')
            if result is None and held(path):
                jobs.append(path.name)
            # 即使作业在两拍之间完成，完成时间仍打断空闲段。
            end = (result or {}).get('finished')
            if isinstance(end, (int, float)):
                stamp = datetime.fromtimestamp(end, datetime.fromisoformat(now).tzinfo).isoformat()
                if stamp <= now and (not latest or stamp > latest):
                    latest = stamp
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
            'round_tokens': tokens, 'job_ids': jobs, 'last_activity_at': latest,
            'idle_since': idle_since}
