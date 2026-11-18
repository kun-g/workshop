"""固定交付版本的亲眼看：许可、一次送达和回答均经等待端口。"""
from workshop.core.models import After
from workshop.core.ports import WaitUnavailable, WaitRejected
from . import dod


def after(flow, item, target, services):
    state = flow.io.read(item, target)
    if not state.get('visual'):
        return None
    version = state.get('merge')
    if not version or not flow._delivery_done(target, state, version):
        return After(blocked='亲眼看缺已交付版本', next='先通过两段 DoD')
    if services is None:
        return After(blocked='亲眼看缺等待端口', next='接入介入点服务')
    port, config = services('intervention')
    operation = 'visual:' + item['id'] + ':' + version
    try:
        saved = state.get('visual_deliveries', {}).get(version)
        if saved:
            contract = saved['contract']
        else:
            contract = dod.visual_contract(flow._issue(item, target).get('body') or '')
        if not all(contract.values()):
            return After(blocked='存量亲眼看约定缺看什么、在哪看或怎么回', next='在原 issue 补齐送达约定')
        if ('飞书' not in contract['location'] or '私聊' not in contract['location']
                or not all(word in contract['response'] for word in ('引用', '过', '不对'))):
            return After(blocked='亲眼看约定尚无对应通道支持',
                         next='实现约定的地点与回法后续接；当前只支持飞书私聊引用回复过或不对')
        permit = port.invoke('permit', {'operation': operation + ':permit', 'point': 'visual',
            'owner': item['id'], 'scope': 'contract:' + item['id'] + ':' + version, 'basis': contract}, config)['ref']
        payload = {'kind': 'visual', 'item': item['id'], 'version': version,
                   'text': f"{item['id']} · 合并提交 {version}\n看什么：{contract['contract']}\n在哪看：{contract['location']}\n怎么回：{contract['response']}"}
        delivery = port.invoke('enqueue', {'operation': operation + ':enqueue', 'permit': permit, 'payload': payload}, config)['ref']
        with flow.io.locked(item, target) as state:
            state.setdefault('visual_deliveries', {})[version] = {'contract': contract, 'delivery': delivery, 'permit': permit}
            flow.io.save(item, target, state)
        sent = port.invoke('dispatch', {'ref': delivery}, config)
        if sent['state'] != 'sent':
            return After(pending=True, reason='亲眼看送达结果未确认：' + delivery,
                         next='按原送达号对账，不盲目重发', recheck_after='+5m')
        reference = port.create(operation + ':wait', {'point': 'visual', 'owner': item['id'],
            'content': {'key': 'visual', 'stage': '收尾', 'version': version,
                        'delivery': delivery, 'response': contract['response'],
                        'question': contract['contract'], 'recommend': '按约定查看后回复'}}, config)
        record = port.read(reference, config)
        with flow.io.locked(item, target) as state:
            state['visual_deliveries'][version].update(external=sent['external'], wait=reference, answer=record['answer'])
            flow.io.save(item, target, state)
        if record['answer'] is not None:
            answer = record['answer']['value']
            if answer == '过' and '过' in contract['response']:
                return None
            return After(pending=True, rerun=True, reason='亲眼看未通过', next='按当前版本的反馈退回执行')
        return After(pending=True, aliases=[sent['external']], reason='等待当前交付版本的亲眼看回复',
                     next=contract['response'], wait={'provider': 'intervention', 'ref': reference, 'operation': operation})
    except WaitUnavailable as exc:
        return After(pending=True, reason=str(exc), next='按原送达号回查介入点服务', recheck_after='+1m')
    except WaitRejected as exc:
        return After(blocked=str(exc), next='核对当前交付版本的送达约定')


def answer(state):
    entry = state.get('visual_deliveries', {}).get(state.get('merge'), {})
    record = entry.get('answer')
    return record['value'] if record else None
