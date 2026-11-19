"""执行取舍经等待端口持久保存；不发卡、不把会话放行当答案。"""
from workshop.core.models import After
from workshop.core.ports import WaitRejected, WaitUnavailable


def content(receipt, data, stage):
    value = data.get('decision')
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('decision 必须是对象')
    for field in ('key', 'version', 'question', 'reason', 'recommend'):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError('decision 缺 ' + field)
    if receipt.advance or receipt.later or receipt.outcome == 'failed':
        raise ValueError('取舍不能同时推进、定时或报失败')
    return {**value, 'stage': stage}


def after(io, item, key, target, services, value):
    if services is None:
        raise ValueError('核心未传等待端口句柄')
    provider = target.get('decision_wait_service', 'intervention')
    port, config = services(provider)
    operation = 'decision:' + item['id'] + ':' + key
    try:
        reference = port.create(operation, {'point': 'decision', 'owner': item['id'], 'content': value}, config)
        record = port.read(reference, config)
    except WaitRejected as exc:
        return After(blocked=str(exc), next='修正本活等待请求后恢复')
    except WaitUnavailable as exc:
        return After(pending=True, reason=str(exc), next='原操作号重查等待服务', recheck_after='+1m')
    with io.locked(item, target) as state:
        state.setdefault('decisions', {})[reference] = record
        io.save(item, target, state)
    if record.get('superseded'):
        return After(blocked='问题已更新', next='按新等待号回答')
    if record['answer'] is not None:
        return After()
    return After(pending=True, reason=value['reason'], next=value['question'],
                 wait={'provider': provider, 'ref': reference, 'operation': operation})
