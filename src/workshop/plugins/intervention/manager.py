"""项目经理结论与按范围唯一送达；点击读取经配置固定的只读命令。"""
import json
import re
import subprocess
from .store import required

SECTIONS = ('等你', '项目经理处理中', '处理不了')


def plain(text):
    return {'tag': 'plain_text', 'content': str(text)}


def detail(title, text):
    return {'tag': 'collapsible_panel', 'expanded': False,
            'header': {'title': plain(title)}, 'elements': [{'tag': 'div', 'text': plain(text)}]}


def help_card(conclusion, delivery):
    limits = {'action': 20, 'reason': 50, 'evidence': 300}
    for key, limit in limits.items():
        text = required(conclusion.get(key), key)
        if len(text) > limit or key != 'evidence' and '\n' in text:
            raise ValueError(key + ' 超过卡片上限')
    section = conclusion['status_location']
    if section not in SECTIONS:
        raise ValueError('未知 status 栏目')
    # 首屏不呈现路径、机器编号或内部状态；完整证据只进折叠。
    front = conclusion['action'] + conclusion['reason']
    if re.search(r'(?:/Users/|/tmp/|workshop:|delivery:|#[0-9]+|\b(?:queued|running|unavailable)\b)', front):
        raise ValueError('首屏包含内部标识')
    return {'schema': '2.0', 'header': {'title': plain(conclusion['action']),
            'subtitle': plain('车间程序：项目经理起不来' if conclusion.get('unavailable') else '项目经理处理不了'),
            'template': 'orange'}, 'body': {'elements': [
                {'tag': 'div', 'text': plain(conclusion['reason'])},
                detail('项目经理查了什么', conclusion['evidence']),
                {'tag': 'button', 'text': plain(section), 'type': 'primary',
                 'behaviors': [{'type': 'callback', 'value': {'ns': 'workshop-status/v1',
                                  'delivery': delivery, 'section': section}}]}]}}


def update(service, key, data):
    row = data['record']
    for field in ('id', 'episode'):
        required(row.get(field), field)
    if type(row.get('revision')) is not int or type(row.get('active')) is not bool:
        raise ValueError('事项版本或状态无效')
    ref = service.store.identity('manager', row['id'])
    old = next((r for r in service.store.all('manager') if r['id'] == row['id']), None)
    if old and row['revision'] <= old['revision']:
        return {'ref': ref}
    service.store.put('manager', ref, row, 'manager_updated')
    # 只撤销尚未确定发送的队列；sending/uncertain 由 dispatch 先对账。
    for delivery in service.store.all('delivery'):
        if delivery['payload'].get('kind') != 'manager_help' or delivery['state'] in ('sent', 'cancelled'):
            continue
        members = delivery['payload']['members']
        current = {r['episode']: r for r in service.store.all('manager')}
        primary = delivery['payload'].get('primary')
        if (primary and (not current.get(primary, {}).get('active') or current[primary].get('state') == 'resolved')
                or all(not current.get(e, {}).get('active') or current[e].get('state') == 'resolved' for e in members)):
            service.cancel(key, {'ref': delivery['permit'], 'reason': '发送前事项已解决'})
    return {'ref': ref}


def queue(service, row):
    key, ref = row['episode'], service.store.identity('manager', row['id'])
    conclusion = row.get('conclusion') or {}
    if not row['active'] or row['state'] not in ('blocked', 'unavailable') or conclusion.get('result') != 'blocked':
        return {'ref': ref}
    business = row.get('business', {})
    if business.get('total') is None:
        return {'ref': ref}  # 没有可信业务快照，不能选择额度归属。
    scope = 'incident:' + row['episode'] if business['total'] > 0 else 'idle:' + required(business.get('idle_since'), 'idle_since')
    permit = service.permit(key, {'point': 'manager_help', 'scope': scope, 'owner': row['id'], 'basis': conclusion})
    delivery_id = service.store.identity('delivery', permit['ref'])
    if any(r['id'] == delivery_id for r in service.store.all('delivery')) or permit['cancelled']:
        return {'ref': ref}  # 同范围后续结论只更新状态。
    known = [r for r in service.store.all('manager') if r['active'] and r['state'] in ('blocked', 'unavailable')
             and (scope.startswith('idle:') and r.get('business', {}).get('idle_since') == business.get('idle_since') or r['episode'] == row['episode'])]
    combined = dict(conclusion)
    combined['evidence'] = '；'.join((r.get('conclusion') or {}).get('action', '') + '：' + (r.get('conclusion') or {}).get('evidence', '') for r in known)[:300]
    card = help_card(combined, delivery_id)
    service.enqueue(key, {'permit': permit['ref'], 'payload': {'kind': 'manager_help', 'card': card,
                          'section': combined['status_location'], 'primary': row['episode'], 'members': [r['episode'] for r in known]}})
    return {'ref': ref}


def flush(service):
    errors = []
    for row in service.store.all('manager'):
        try:
            with service.store.transaction():
                queue(service, row)
        except (ValueError, KeyError, TypeError) as exc:
            error = {'id': row['id'], 'revision': row.get('revision'), 'reason': str(exc)}
            errors.append(error)
            with service.store.transaction():
                service.store.put('manager_error', service.store.identity('manager_error', row['id']), error, 'queue_failed')
    return {'errors': errors, 'deliveries': [service.dispatch(r['id']) for r in service.store.all('delivery')
                          if r['payload'].get('kind') in ('manager_help', 'status_reply') and r['state'] not in ('sent', 'cancelled')]}


def reply_card(snapshot, key):
    return {'schema': '2.0', 'header': {'title': plain(snapshot['section']), 'subtitle': plain('车间状态')},
            'body': {'elements': [*({'tag': 'div', 'text': plain(line)} for line in snapshot['lines']),
                                 detail('详情', '\n'.join(snapshot.get('details', [])) + '\n[送达 ' + key + ']')]}}


def click(service, key, data):
    config = service.configuration().get('status', {})
    event = data['event']['event']
    if event.get('operator', {}).get('open_id') != config.get('recipient') or event.get('context', {}).get('open_chat_id') != config.get('chat'):
        return {'state': 'rejected', 'accepted': False, 'reason': '只接受指定本人私聊点击'}
    action = event.get('action', {})
    value = action.get('value', {})
    if action.get('tag') != 'button' or value.get('ns') != 'workshop-status/v1' or value.get('section') not in SECTIONS:
        return {'state': 'rejected', 'accepted': False, 'reason': '按钮无效'}
    try:
        original = service.store.get(value.get('delivery'), 'delivery')
    except ValueError:
        return {'state': 'rejected', 'accepted': False, 'reason': '原送达不存在'}
    if (original['state'] != 'sent' or original['payload'].get('kind') != 'manager_help'
            or original['payload']['section'] != value['section']
            or original['external'] != event.get('context', {}).get('open_message_id')):
        return {'state': 'rejected', 'accepted': False, 'reason': '按钮未绑定原送达'}
    try:
        result = subprocess.run(config['command'], input=json.dumps({'section': value['section'], 'waits': service.store.all('wait')}),
                                capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError('只读状态命令失败')
        snapshot = json.loads(result.stdout)
        if snapshot['section'] != value['section'] or not isinstance(snapshot['lines'], list):
            raise ValueError('只读状态返回无效')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        reason = '状态读取命令超时' if isinstance(exc, subprocess.TimeoutExpired) else '状态读取命令未能完成'
        snapshot = {'section': value['section'], 'lines': ['读不到车间状态：' + reason],
                    'details': [type(exc).__name__ + ': ' + str(exc)]}
    ref = service.store.identity('delivery', key)
    # 用户主动读取的响应不创建求助许可，不消耗自动推送额度。
    service.store.put('delivery', ref, {'id': ref, 'permit': None, 'state': 'queued', 'external': None,
        'payload': {'kind': 'status_reply', 'card': reply_card(snapshot, ref), 'snapshot': snapshot}}, 'reply_queued')
    return {'accepted': True, 'ref': ref, 'snapshot': snapshot}
