"""不透明等待引用：先保存暂停位置；完成信号仅唤醒原阶段重新核验。"""
from datetime import timedelta
from .models import stamp
from .jobs import Pending


def descriptor(value):
    if (not isinstance(value, dict) or set(value) != {'provider', 'ref', 'operation'}
            or not all(isinstance(v, str) and v.strip() for v in value.values())):
        raise ValueError('等待须有 provider/ref/operation 三个非空字符串')
    return value


def active(engine, item):
    if not item.get('wait_id'):
        return False
    row = engine.log.get('waits', item['wait_id'])
    return bool(row and row['state'] == 'waiting')


def bind(engine, item, value):
    value = descriptor(value)
    if value['provider'] not in engine.rules.data.get('wait_services', {}):
        raise ValueError('等待端口未登记')
    key = value['operation']
    expected = {**value, 'item': item['id'], 'stage': item['stage']}
    with engine.log.transaction():
        old = engine.log.get('waits', key)
        if old:
            if any(old[k] != v for k, v in expected.items()) or old['state'] != 'waiting':
                raise ValueError('等待操作号已使用或参数冲突')
            return key
        current = engine.log.get('items', item['id'])
        if current is None or current['stage'] != item['stage'] or current['state'] in ('完成', '放弃'):
            raise ValueError('暂停位置已改变')
        if active(engine, current):
            raise ValueError('本活已有等待')
        if any(w['provider'] == value['provider'] and w['ref'] == value['ref'] for w in engine.log.all('waits')):
            raise ValueError('等待引用已绑定，不能跨活或跨阶段复用')
        engine.fact('wait_bound', {'id': key, **expected, 'state': 'waiting', 'token': None,
                    'attempt': 0, 'next_at': stamp(engine.clock())}, item['id'],
                    source='wait-bind', ext_id=key)
    return key


def pending(engine, item, value, hook):
    key = bind(engine, item, value)
    return dict(state='等你', wait_id=key, next='等待外部结果', blocked_by='',
                after_wait={**hook, 'at': stamp(engine.clock())},
                after_pending_count=item.get('after_pending_count', 0) + 1,
                leave={'kind': '等你', 'about': '等待外部结果'})


def reconcile(engine):
    first = not getattr(engine, 'waits_started', False)
    engine.waits_started = True
    for row in engine.log.all('waits'):
        if row['state'] not in ('waiting', 'resumed') or (not first and row['next_at'] > stamp(engine.clock())):
            continue
        item = engine.log.get('items', row['item'])
        if not item:
            continue
        if row['state'] == 'waiting':
            if item['state'] in ('完成', '放弃') or item.get('wait_id') != row['id'] or item['stage'] != row['stage']:
                engine.fact('wait_changed', {'id': row['id'], 'state': 'superseded'}, row['item'])
                continue
            if engine.control(item['target']).get('paused') or engine.flow_broken(item['target'], item.get('flow', '')):
                continue
        config = engine.rules.data.get('wait_services', {}).get(row['provider'])
        try:
            if config is None:
                raise ValueError('等待端口未配置')
            if row['state'] == 'resumed':
                result = engine.call('wait', row['provider'], 'acknowledge', 'ack:' + row['id'], row['ref'], row['token'], config,
                                     identity=[row['id'], 'ack', row['attempt']], item=row['item'])
                if not isinstance(result, dict) or result.get('ref') != row['ref'] or result.get('token') != row['token']:
                    raise ValueError('确认回执不匹配')
                engine.fact('wait_changed', {'id': row['id'], 'state': 'acknowledged'}, row['item'])
                continue
            result = engine.call('wait', row['provider'], 'poll', row['ref'], config,
                                 identity=[row['id'], row['attempt']], item=row['item'])
            if (not isinstance(result, dict) or result.get('ref') != row['ref']
                    or type(result.get('ready')) is not bool):
                raise ValueError('等待结果无效')
            if result['ready']:
                token = result.get('token')
                if not isinstance(token, str) or not token.strip():
                    raise ValueError('完成信号缺稳定标识')
                with engine.log.transaction():
                    # 状态和恢复事实同事务；重复信号不能重复恢复，阶段不在这里推进。
                    latest = engine.log.get('items', row['item'])
                    if latest.get('wait_id') != row['id'] or latest['stage'] != row['stage']:
                        continue
                    engine.fact('wait_resumed', {'id': row['id'], 'token': token, 'next_at': stamp(engine.clock())},
                                row['item'], source='wait-resume', ext_id=row['id'])
                continue
            retry(engine, row)
        except Pending:
            continue
        except Exception as exc:
            # 查询失败只保留暂停，不隔离别的活，不把空结果或超时解释成批准。
            retry(engine, row, type(exc).__name__)


def retry(engine, row, error=''):
    engine.fact('wait_changed', {'id': row['id'], 'attempt': row['attempt'] + 1, 'error': error,
                'next_at': stamp(engine.clock() + timedelta(seconds=5 if error else 60))}, row['item'])
