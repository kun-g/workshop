"""停机窗口迁移；先备份，追加事实，保留历史和旧键别名。"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
from pathlib import Path
import shutil
import sqlite3
import re

from workshop.core.log import Log
from workshop.core.models import Event
from .checks import destination, hydrate
from .io import IO, write
from .model import digest


def migrate(database, target, *, io=None):
    io = io or IO()
    database = Path(database).resolve()
    if not database.is_file():
        raise ValueError('状态库不存在')
    prefix = target['name'] + ':watch_updates:'
    with database.with_suffix('.lock').open('a') as handle, io.locked(target):
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        log = Log(database)
        try:
            state = io.state(target)
            old = [i for i in log.all('items') if i['id'].startswith(prefix)
                   and i['state'] not in ('完成', '放弃') and not i.get('migrated_to')
                   and not re.fullmatch(r'check(?::\d+)?', i['object_key'])
                   and not i['object_key'].startswith('upgrade:')]
            if not old:
                if not state.get('checks_migrated'):
                    state['checks_migrated'] = True
                    io.save(target, state)
                return {'status': 'unchanged', 'migrated': 0}
            ids = {i['id'] for i in old}
            if any(r['item'] in ids for r in log.all('rounds', unless=('status', 'ended'))) or any(i.get('program') for i in old):
                raise ValueError('旧活仍有未结算轮次或程序作业；须先结算，再在停机窗口迁移')
            if any(c.get('item') in ids and c.get('mode') == 'ask' and not c.get('answered')
                   and not c.get('cancelled') for c in log.all('cards')):
                raise ValueError('旧活仍有未回答问卡，不能绕过回答迁移')
            key, _ = destination(state)
            item_id = prefix + key
            now = datetime.now(timezone.utc).isoformat()
            backup = database.parent / ('watch-updates-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            backup.mkdir(mode=0o700)
            with sqlite3.connect(backup / database.name) as copy:
                log.db.backup(copy)
            if io.state_file(target).is_file():
                shutil.copy2(io.state_file(target), backup / 'state.json')
            check = state.setdefault('checks', {}).setdefault(key, {'entries': {}, 'failures': {}})
            # 写文件在数据库事务之前；中途退出后可重复 hydrate，事实的 ext_id 也固定去重。
            transfers = []
            for item in old:
                materials = deepcopy(item.get('materials', []))
                for m in materials:
                    if m.get('source') == 'watch_updates' and isinstance(m.get('body'), dict):
                        m['body'].setdefault('entry_key', item['object_key'])
                hydrate(state, {'object_key': key, 'materials': materials})
                current = state['versions'].get(item['object_key'])
                if current:
                    current['check'] = key
                check.setdefault('migrated', {})[item['id']] = {
                    'stage': item['stage'], 'state': item['state'], 'blocked_by': item.get('blocked_by'),
                    'next': item.get('next'), 'history': list(log.full_item(item).get('history', [])),
                    'version': deepcopy(current), 'materials': materials}
                transfers.append((item, materials))
            for report in state['reports'].values():
                if prefix + report.get('anchor', '') in ids or any(prefix + v in ids for v in report.get('versions', [])):
                    report['previous_anchor'] = report['anchor']
                    report['anchor'] = key
            # 核心事实与文件不能同一事务提交；标记只在事实提交成功后写。
            io.save(target, state)
            with log.transaction():
                event = Event('watch_updates', 'consolidation:check', target['name'], key, '检查更新',
                              {'kind': 'migration', 'backup': str(backup)}, action='ready', flow='watch_updates')
                log.append('event', asdict(event), item_id, source='watch-updates-migration', ext_id='create:' + item_id)
                for item, materials in transfers:
                    old_id = item['id']
                    for n, material in enumerate(materials):
                        body = material.get('body')
                        if material.get('source') == 'watch_updates' and isinstance(body, dict) and body.get('kind') in ('version', 'reconcile', 'failure'):
                            record_id = digest(['migrated', old_id, n])
                            write(io.file(target, 'state_dir') / 'events' / (record_id + '.json'), body)
                            body = {'kind': body['kind'], 'entry_key': body['entry_key'], 'record_id': record_id}
                        event = Event(material.get('source') or 'watch_updates', material.get('ext_id') or f'migrated:{old_id}:{n}', target['name'],
                                      key, '迁入 ' + item['title'], body, flow='watch_updates')
                        log.append('event', asdict(event), item_id, source=event.source, ext_id=f'migrated:{old_id}:{n}')
                    archive = io.file(target, 'state_dir') / 'migration' / (digest(old_id) + '.json')
                    write(archive, check['migrated'][old_id])
                    summary = {k: check['migrated'][old_id][k] for k in ('stage', 'state', 'blocked_by', 'next')}
                    snapshot = Event('watch_updates_migration', 'snapshot:' + old_id, target['name'], key,
                                     '存量迁移 ' + old_id, {'old_item': old_id, **summary, 'archive': str(archive)}, flow='watch_updates')
                    log.append('event', asdict(snapshot), item_id,
                               source=snapshot.source, ext_id=snapshot.ext_id)
                    log.append('item_changed', {'state': '完成', 'migrated_to': item_id, 'pending_rework': False,
                               'after_wait': None, 'pending_advance': None, 'retry_at': None, 'serial_hold': False,
                               'blocked_by': '', 'next': '已并入 ' + item_id,
                               'leave': {'kind': '工序', 'owner': '车间', 'at': now, 'about': '继续 ' + item_id}}, old_id)
                    log.append('alias_bound', {'id': old_id, 'item': item_id, 'object_key': key}, item_id,
                               source='watch-updates-migration', ext_id='alias:' + old_id)
                for timer in log.all('timers', unless=('fired', True)):
                    if timer['item'] in ids:
                        log.append('timer_fired', {'id': timer['id']}, timer['item'])
                for effect in log.all('effects', unless=('done', True)):
                    if effect.get('item') in ids:
                        log.append('effect', {'id': effect['id'], 'done': True, 'migrated_to': item_id}, effect['item'])
                log.append('item_changed', {'stage': '执行', 'state': '空闲', 'pending_rework': False,
                           'after_wait': None, 'pending_advance': None, 'next': '继续迁入的检查项', 'blocked_by': ''}, item_id)
            state['checks_migrated'] = True
            io.save(target, state)
            return {'status': 'migrated', 'migrated': len(old), 'item': item_id,
                    'backup': str(backup), 'old_items': sorted(ids)}
        finally:
            log.close()
