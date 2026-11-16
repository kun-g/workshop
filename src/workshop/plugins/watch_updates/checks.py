"""检查活的归属；软件版本键仍只标识一条判断，不再标识一件活。"""
from copy import deepcopy
from pathlib import Path
import re
from .io import read


def destination(state):
    checks = state.get('checks', {})
    active = next((k for k, v in checks.items() if not v.get('closed')), None)
    if active:
        return active, True
    return ('check' if not checks else 'check:' + str(len(checks) + 1)), False


def hydrate(state, item, records=None):
    """在副本上也能调用；读取/判定不会提前消费资料。"""
    key = item['object_key']
    check = state.setdefault('checks', {}).setdefault(key, {'entries': {}, 'failures': {}})
    for material in item.get('materials', []):
        data = material.get('body')
        if material.get('source') != 'watch_updates' or not isinstance(data, dict):
            continue
        kind = data.get('kind')
        if kind not in ('version', 'reconcile', 'failure'):
            continue
        entry = data.get('entry_key')
        if not entry:
            continue
        if data.get('record_id'):
            record_id = data['record_id']
            if records is None or not re.fullmatch(r'[a-f0-9]{24}', record_id):
                raise ValueError('检查项记录标识无效')
            stored = read(Path(records) / (record_id + '.json'))
            if stored.get('kind') != kind or stored.get('entry_key') != entry:
                raise ValueError('检查项记录与来源事件不匹配')
            data = stored
        if entry not in check['entries']:
            check.pop('closed', None)
        check['entries'].setdefault(entry, deepcopy(data))
        if kind == 'version':
            state['versions'].setdefault(entry, {**deepcopy(data), 'status': 'judging', 'feedback': {}})
            state['versions'][entry]['check'] = key
            software = state['versions'][entry]['software']['id']
            for failure in check['failures'].values():
                if failure.get('software_id') == software:
                    failure['resolved'] = True
        elif kind == 'failure':
            check['failures'].setdefault(entry, deepcopy(data))
    return check


def candidate(state, check):
    for key, data in check['entries'].items():
        if data.get('blocked'):
            continue
        if data['kind'] == 'reconcile' and not state['weeks'].get(key, {}).get('reconciled'):
            return key, data
    for key, data in check['entries'].items():
        if data['kind'] != 'version' or data.get('blocked'):
            continue
        current = state['versions'][key]
        from .model import current_judgment
        if current.get('status') != 'done' and (not current_judgment(current) or current.get('needs_judgment')):
            return key, data
    return None


def incoming_feedback(item, state):
    from .feedback import SOURCES
    return any(m.get('source') in SOURCES and m.get('ext_id') and
               m['source'] + ':' + m['ext_id'] not in state['consumed'] for m in item.get('materials', []))
