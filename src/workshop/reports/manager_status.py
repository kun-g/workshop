"""三个可点击栏目共用同一份只读投影；编号和长证据归入详情。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

HEADINGS = ('等你', '项目经理处理中', '处理不了')


def read_managers(directory):
    path = Path(directory) / 'managers.db'
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        return [json.loads(r[0]) for r in db.execute('SELECT body FROM concerns ORDER BY rowid')]


def sections(view):
    result = {h: {'section': h, 'lines': [], 'details': []} for h in HEADINGS}
    names = {r['id']: r['title'][:32] for r in view['rows']}
    for wait in view.get('waits', []):
        row = result['等你']
        row['lines'].append(names.get(wait['item'], '待处理事项') + '：' + wait['question'] + '；推荐：' + wait['recommend'])
        row['details'].append(json.dumps(wait, ensure_ascii=False, sort_keys=True))
    for m in view.get('managers', []):
        if not m['active'] or m['state'] == 'resolved':
            continue
        heading = '处理不了' if m['state'] in ('blocked', 'unavailable') else '项目经理处理中'
        fact = m['facts'][-1]['value'] if m['facts'] else {}
        title = fact.get('title') or ('全局空闲' if m['id'] == 'idle' else '车间运行故障')
        conclusion = m.get('conclusion') or {}
        reason = conclusion.get('reason') or fact.get('reason') or fact.get('text') or '正在核对原因'
        action = conclusion.get('action')
        result[heading]['lines'].append(str(title)[:32] + '：' + str(reason) + ('；' + action if action else ''))
        result[heading]['details'].append(json.dumps(m, ensure_ascii=False, sort_keys=True))
    for row in result.values():
        if not row['lines']:
            row['lines'] = ['没有']
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    request = json.load(sys.stdin)
    section = request['section']
    if section not in HEADINGS:
        raise ValueError('未知栏目')
    try:
        state = args.state_dir.expanduser().resolve()
        heartbeat = json.loads((state / 'heartbeat.json').read_text())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat['last_ok_at'])).total_seconds()
        if not -60 <= age <= 180:
            raise ValueError('守护进程心跳过期')
        pid = heartbeat['process']['pid']
        if type(pid) is not int or pid <= 0:
            raise ValueError('守护进程身份缺失')
        os.kill(pid, 0)
        from workshop.core.log import Log
        from workshop.registry import Registry
        from .report import overview
        log = Log.readonly(state / 'workshop.db')
        try:
            registry = Registry()
            saved = (log.get('controls', '@rules') or {}).get('data', {})
            snapshots = {}
            for name, config in saved.get('wait_services', {}).items():
                records = request.get('waits')
                if records is None:
                    endpoint = Path(config['endpoint']).expanduser().resolve()
                    with closing(sqlite3.connect((endpoint.parent.parent / 'intervention.db').as_uri() + '?mode=ro', uri=True)) as db:
                        records = [json.loads(r[0]) for r in db.execute("SELECT body FROM records WHERE kind='wait'")]
                class Snapshot:
                    def __init__(self, records):
                        self.records = {r['id']: r for r in records}
                    def read(self, ref, config):
                        return self.records[ref]
                snapshots[name] = Snapshot(records)
            class SnapshotPlugins:
                def get(self, port, name):
                    return snapshots[name] if port == 'wait' else registry.get(port, name)
            snapshot = sections(overview(log, plugins=SnapshotPlugins()))[section]
        finally:
            log.close()
    except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
        reason = ('守护进程未运行或状态文件不存在' if isinstance(exc, (FileNotFoundError, ProcessLookupError))
                  else '没有读取状态的权限' if isinstance(exc, PermissionError)
                  else '守护进程心跳过期' if str(exc) == '守护进程心跳过期' else '状态记录无法读取')
        snapshot = {'section': section, 'lines': ['读不到车间状态：' + reason], 'details': [type(exc).__name__ + ': ' + str(exc)]}
    print(json.dumps(snapshot, ensure_ascii=False))


if __name__ == '__main__':
    main()
