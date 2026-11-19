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


def heading_of(ident):
    """没有标题的事项，按 id 的形状给前缀：交付送达看对象和编号，主循环单列，认不出的显示 id 本身。"""
    parts = str(ident).split(':')
    if ident == 'idle':
        return '全局空闲'
    if ident == 'system:tick':
        return '主循环'
    if parts[:2] == ['fault', 'delivery'] and len(parts) >= 5:
        return parts[2] + ':' + parts[3] + ' 送达'
    if parts[:3] == ['fault', 'delivery', 'channel'] and len(parts) == 4:
        return parts[3] + ' 送达通道'
    return str(ident)


STATES = {'blocked': '处理不了', 'unavailable': '项目经理起不来', 'active': '处理中'}


def stale_time(text):
    """旧数据是什么时候读到的，本机时区，写到分钟。"""
    return datetime.fromisoformat(text).astimezone().strftime('%m-%d %H:%M')


def auto_retry_note(fact):
    """插件瞬时故障的自动重试说明：事项首行要能直接看出下次几点，不用人手工 resume。"""
    if not isinstance(fact, dict):
        return ''
    control = fact.get('control')
    if isinstance(control, dict) and control.get('broken') and control.get('auto_retry') and control.get('retry_at'):
        return '插件故障，车间自动重试中，下次 ' + stale_time(control['retry_at'])
    return ''


def readable(record):
    """详情给人看：每项一行「名称：内容」，空的不显示；不出原始 JSON。等你的记录和项目经理事项各取各的字段。"""
    if 'question' in record:
        content = record.get('content') or {}
        pairs = [('等什么', record.get('question')), ('原因', record.get('reason')), ('推荐', record.get('recommend')),
                 ('版本', content.get('version')), ('旧数据', record.get('stale') and stale_time(record['stale']) + ' 读到；现在读不到：' + record.get('stale_error', '')), ('等待号', record.get('ref')), ('活', record.get('item'))]
    else:
        conclusion = record.get('conclusion') or {}
        fact = record['facts'][-1]['value'] if record.get('facts') else {}
        pairs = [('原因', conclusion.get('reason') or fact.get('reason') or fact.get('text')), ('要做的', conclusion.get('action')),
                 ('自动重试', auto_retry_note(fact).removeprefix('插件故障，车间自动重试中，') or None),
                 ('项目经理查了什么', conclusion.get('evidence')), ('状态', STATES.get(record.get('state'), record.get('state'))),
                 ('尝试次数', len(record.get('attempts') or []) or None), ('事项', record.get('id'))]
    return [(k, str(v)) for k, v in pairs if v]


def sections(view):
    result = {h: {'section': h, 'lines': [], 'details': []} for h in HEADINGS}
    names = {r['id']: r['title'][:32] for r in view['rows']}
    for wait in view.get('waits', []):
        row = result['等你']
        old = '（读不到，显示的是 ' + stale_time(wait['stale']) + ' 的旧数据）' if wait.get('stale') else ''
        row['lines'].append(names.get(wait['item'], '待处理事项') + '：' + wait['question'] + '；推荐：' + wait['recommend'] + old)
        row['details'].append(json.dumps(wait, ensure_ascii=False, sort_keys=True))
    for m in view.get('managers', []):
        if not m['active'] or m['state'] == 'resolved':
            continue
        heading = '处理不了' if m['state'] in ('blocked', 'unavailable') else '项目经理处理中'
        fact = m['facts'][-1]['value'] if m['facts'] else {}
        title = fact.get('title') or heading_of(m['id'])
        conclusion = m.get('conclusion') or {}
        reason = conclusion.get('reason') or fact.get('reason') or fact.get('text') or '正在核对原因'
        retry = auto_retry_note(fact)
        action = conclusion.get('action')
        if retry:
            # 车间自己会到点重试：事项首行直说下次几点，不再显示「手工 resume」这类旧动作。
            reason, action = retry, None
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
