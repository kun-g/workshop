"""上线后人验（issue交付 R14）：交付完成后经介入点服务登记一个等待，列出各项原文；只登记、不发消息。

回答「过」才收尾关单；回答别的，活带着回答原文退回执行（不算进两段 DoD 的退回上限）。
等待和回答都绑定合并提交：修复后另开 PR、新的合并提交重新登记。
"""
from workshop.core.models import After
from workshop.core.ports import WaitUnavailable, WaitRejected
from . import dod

POINT = 'human_check'
PASS = '过'


def after(flow, item, target, services):
    state = flow.io.read(item, target)
    if not state.get('human'):
        return None
    version = state.get('merge')
    if not version or not flow._delivery_done(target, state, version):
        return After(blocked='上线后人验缺已交付版本', next='先通过两段 DoD 并上线')
    if services is None:
        return After(blocked='上线后人验缺等待端口', next='接入介入点服务')
    port, config = services('intervention')
    operation = 'human:' + item['id'] + ':' + version
    saved = state.get('human_checks', {}).get(version)
    if saved:
        # 同一合并提交只按第一次读到的各项登记；issue 后来改了也不换问题。
        entries = saved['items']
    else:
        entries = dod.human_items(flow._issue(item, target).get('body') or '')
        if not entries:
            return After(blocked='上线后人验没有列出的项', next='在原 issue 的「上线后人验」节逐项写看什么、怎么判')
        with flow.io.locked(item, target) as state:
            state.setdefault('human_checks', {})[version] = {'items': entries}
            flow.io.save(item, target, state)
    # 写全长提交号：answer-wait --version 要和等待内容里的版本逐字相同。
    question = '上线后人验（合并提交 ' + version + '）：\n' + '\n'.join(
        f'{number}. {text}' for number, text in enumerate(entries, 1))
    try:
        reference = port.create(operation, {'point': POINT, 'owner': item['id'], 'content': {
            'key': POINT, 'stage': '收尾', 'version': version, 'items': entries, 'question': question,
            'reason': '上线后要人看或等外部条件才能验的项，检查跑不出来',
            'recommend': '逐项看过后回「过」；不过回「不对：哪里不对」'}}, config)
        record = port.read(reference, config)
    except WaitUnavailable as exc:
        return After(pending=True, reason=str(exc), next='按原操作号回查介入点服务', recheck_after='+1m')
    except WaitRejected as exc:
        return After(blocked=str(exc), next='核对当前交付版本的上线后人验等待')
    with flow.io.locked(item, target) as state:
        state['human_checks'][version].update(wait=reference, answer=record['answer'])
        flow.io.save(item, target, state)
    if record['answer'] is not None:
        if passed(record['answer']['value']):
            return None
        return After(pending=True, rerun=True, reason='上线后人验未通过', next='按回答原文退回执行')
    return After(pending=True, reason='等你上线后人验：' + '；'.join(entries),
                 next='逐项看过后回「过」；不过回「不对：哪里不对」',
                 wait={'provider': 'intervention', 'ref': reference, 'operation': operation})


def passed(value):
    return isinstance(value, str) and value.strip() == PASS


def answer(state):
    """当前合并提交的人验回答原文；没登记或没答为 None。"""
    record = state.get('human_checks', {}).get(state.get('merge'), {}).get('answer')
    return record['value'] if record else None
