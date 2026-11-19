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
        issue = None
        if saved:
            contract = saved['contract']
        else:
            issue = flow._issue(item, target)
            contract = dod.visual_contract(issue.get('body') or '')
        if not all(contract.values()):
            return After(blocked='存量亲眼看约定缺看什么、在哪看或怎么回', next='在原 issue 补齐送达约定')
        if not supported(contract):
            return After(blocked='亲眼看约定尚无对应通道支持',
                         next='实现约定的地点与回法后续接；当前只支持飞书私聊点卡片上的通过或不通过，或引用回复过或不对')
        if saved and saved.get('delivery'):
            # 已排队的送达只按原号推进；标题等后来变了也不换内容。
            delivery = saved['delivery']
        else:
            permit = port.invoke('permit', {'operation': operation + ':permit', 'point': 'visual',
                'owner': item['id'], 'scope': 'contract:' + item['id'] + ':' + version, 'basis': contract}, config)['ref']
            issue = issue or flow._issue(item, target)
            payload = {'kind': 'visual', 'item': item['id'], 'version': version,
                       'card': card(issue, target, item['id'], version, state.get('pr'), contract, permit)}
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


def supported(contract):
    """飞书私聊里点卡片按钮，或引用卡片回过或不对。"""
    response = contract['response']
    return ('飞书' in contract['location'] and '私聊' in contract['location']
            and ('通过' in response and '不通过' in response or all(w in response for w in ('引用', '过', '不对'))))


def plain(text):
    return {'tag': 'plain_text', 'content': str(text)}


# 按钮点击经工作台转发到介入点服务；ns 与服务端 visual_click.NS 是同一份契约。
CLICK_NS = 'workshop-visual/v1'
FALLBACK = '按钮没反应时，在 Ash 私聊里引用这张卡回「过」，或「不对：哪里不对」。'


def card(issue, target, item, version, pr, contract, permit):
    """首屏只放人要看的：issue 标题和链接、合并它的 PR、三段约定和两个按钮；对账号码进折叠详情。

    飞书卡片标题不支持链接，链接放在标题下第一行。按钮绑定这一次许可（即这一次送达）和版本；
    送达号由送达命令写进详情。
    """
    number = issue.get('number')
    url = issue.get('html_url') or 'https://github.com/' + target['repo'] + '/issues/' + str(number)
    first = '[issue #' + str(number) + '](' + url + ')'
    if pr:
        first += '　版本：[PR #' + str(pr) + '](https://github.com/' + target['repo'] + '/pull/' + str(pr) + ')'
    response = contract['response'] if '引用' in contract['response'] else contract['response'] + '\n' + FALLBACK
    elements = [{'tag': 'markdown', 'content': first}]
    for label, text in (('看什么', contract['contract']), ('在哪看', contract['location']), ('怎么回', response)):
        # 原文的换行和编号照留，不并成一段。
        elements += [{'tag': 'markdown', 'content': '**' + label + '**'}, {'tag': 'div', 'text': plain(text)}]

    def button(text, kind, choice, **extra):
        return {'tag': 'button', 'text': plain(text), 'type': kind, 'width': 'fill', **extra, 'behaviors': [{'type': 'callback',
                'value': {'ns': CLICK_NS, 'permit': permit, 'version': version, 'choice': choice}}]}
    # 表单写法照 oh-my-assistant 的 ask-kun 卡（线上在用）：输入框必填，空着提交不了。
    elements += [button('通过', 'primary_filled', 'pass'),
                 {'tag': 'form', 'name': 'visual', 'elements': [
                     {'tag': 'input', 'name': 'reason', 'required': True, 'input_type': 'multiline_text',
                      'max_length': 1000, 'width': 'fill', 'placeholder': plain('不通过时写哪里不对')},
                     button('不通过', 'danger', 'fail', name='fail', form_action_type='submit')]},
                 {'tag': 'collapsible_panel', 'expanded': False, 'header': {'title': plain('详情')},
                  'elements': [{'tag': 'div', 'text': plain('活：' + item + '\n合并提交：' + version)}]}]
    return {'schema': '2.0', 'header': {'title': plain(issue.get('title') or item), 'subtitle': plain('亲眼看'),
            'template': 'blue'}, 'body': {'elements': elements}}


def answer(state):
    entry = state.get('visual_deliveries', {}).get(state.get('merge'), {})
    record = entry.get('answer')
    return record['value'] if record else None
