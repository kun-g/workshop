"""亲眼看卡片的「通过」「不通过」点击：核对原送达和版本后作答，回一条已收到。"""
from .store import encode

NS = 'workshop-visual/v1'
CHOICES = {'pass': '通过', 'fail': '不通过'}


def rejected(reason):
    return {'state': 'rejected', 'accepted': False, 'reason': reason}


def receipt_card(value):
    choice = '通过' if value == '过' else '不通过'
    elements = [] if value == '过' else [{'tag': 'div', 'text': {'tag': 'plain_text', 'content': value}}]
    return {'schema': '2.0', 'header': {'title': {'tag': 'plain_text', 'content': '已收到：' + choice},
            'subtitle': {'tag': 'plain_text', 'content': '亲眼看'}, 'template': 'green' if value == '过' else 'orange'},
            'body': {'elements': elements}}


def click(service, key, data):
    """本人和私聊已由服务入口核对；这里核对按钮绑定的这一次送达、版本和等待。"""
    outer = data['event']
    event = outer.get('event', {})
    action = event.get('action', {})
    value = action.get('value', {})
    if action.get('tag') != 'button' or value.get('ns') != NS or value.get('choice') not in CHOICES:
        return rejected('按钮无效')
    if not isinstance(value.get('permit'), str) or not isinstance(value.get('version'), str):
        return rejected('按钮未绑定送达')
    delivery = next((d for d in service.store.all('delivery') if d.get('permit') == value['permit']), None)
    if (delivery is None or delivery['state'] != 'sent' or delivery['payload'].get('kind') != 'visual'
            or delivery['payload'].get('version') != value['version']
            or delivery['external'] != event.get('context', {}).get('open_message_id')):
        return rejected('按钮未绑定原送达')
    wait = next((w for w in service.store.all('wait') if w['point'] == 'visual'
                 and w['content'].get('delivery') == delivery['id']), None)
    if wait is None or wait.get('superseded') or wait['content'].get('version') != value['version']:
        return rejected('这张卡的版本已过期')
    if value['choice'] == 'pass':
        answer = '过'
    else:
        reason = str((action.get('form_value') or {}).get('reason') or '').strip()
        if not reason:
            return rejected('不通过要在输入框写哪里不对')
        answer = '不对：' + reason
    receipt = service.store.identity('delivery', 'visual-receipt:' + wait['id'])
    answered = wait['answer'] is None
    if not answered:
        # 同一张卡重复点击，或已引用回复过：不重复作答，已收到只回一条。
        passed = wait['answer']['value'] == '过'
        if passed != (answer == '过'):
            return rejected('已收到' + ('通过' if passed else '不通过') + '，不能改')
        answer = wait['answer']['value']
    else:
        reference = (outer.get('header') or {}).get('event_id') or key
        service.answer('visual-click:' + key, {'ref': wait['id'], 'value': answer, 'source': {
            'channel': 'external', 'reference': reference, 'version': value['version'], 'delivery': delivery['id'],
            'evidence': encode({'event': reference, 'operator': event.get('operator', {}).get('open_id'),
                                'chat': event.get('context', {}).get('open_chat_id'),
                                'message': delivery['external'], 'choice': value['choice'], 'text': answer})}})
    if not any(d['id'] == receipt for d in service.store.all('delivery')):
        service.store.put('delivery', receipt, {'id': receipt, 'permit': None, 'state': 'queued', 'external': None,
            'payload': {'kind': 'visual_receipt', 'card': receipt_card(answer), 'wait': wait['id']}}, 'receipt_queued')
    return {'accepted': True, 'answered': answered, 'ref': receipt, 'answer': answer}
