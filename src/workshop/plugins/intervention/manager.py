"""项目经理结论与按范围唯一送达；点击读取经配置固定的只读命令。"""
import json
import os
import re
import subprocess
import time
from urllib.parse import quote
from .store import required

SECTIONS = ('等你', '项目经理处理中', '处理不了')
# 一次 flush 只推进这么多条、最多花这么久；剩下的下一拍接着发。
FLUSH_LIMIT = 10
FLUSH_SECONDS = 20
FLUSH_CURSOR = 'flush-cursor.json'


def plain(text):
    return {'tag': 'plain_text', 'content': str(text)}


def detail(title, text):
    return {'tag': 'collapsible_panel', 'expanded': False,
            'header': {'title': plain(title)}, 'elements': [{'tag': 'div', 'text': plain(text)}]}


def clip(text, limit):
    """超长时在句子边界截断，没有边界才硬截并加省略号；不留半句。"""
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    stop = max(cut.rfind('。'), cut.rfind('；'))
    return cut[:stop + 1] if stop >= limit // 2 else cut + '…'


def evidence_panel(evidence):
    """每件事一段：事项名加粗，查到的写在下面；折叠头带箭头，看得出能点开。"""
    blocks = []
    for line in evidence.split('\n'):
        name, _, text = line.partition('：')
        blocks.append({'tag': 'markdown', 'content': f'**{name}**\n{text}' if text else line})
    return {'tag': 'collapsible_panel', 'expanded': False,
            'header': {'title': plain('项目经理查了什么'), 'icon': {'tag': 'standard_icon', 'token': 'down-small-ccm_outlined', 'size': '16px 16px'},
                       'icon_position': 'right', 'icon_expanded_angle': -180},
            'elements': blocks}


def help_card(conclusion, delivery, page=None):
    """page 是本机状态页地址：有就让按钮在电脑上直接打开对应的栏；没有才退回回调按钮。"""
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
                evidence_panel(conclusion['evidence']),
                {'tag': 'button', 'text': plain('看' + section + '栏'), 'type': 'primary',
                 'behaviors': [{'type': 'open_url', 'default_url': page + '#' + quote(section)} if page else
                               {'type': 'callback', 'value': {'ns': 'workshop-status/v1',
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


def pushed(service):
    """求助卡带过的事：没撤销的 manager_help 送达里的成员（项目经理事项的 episode）。"""
    return {episode for r in service.store.all('delivery')
            if r['payload'].get('kind') == 'manager_help' and r['state'] != 'cancelled'
            for episode in r['payload'].get('members', [])}


def queue(service, row):
    key, ref = row['episode'], service.store.identity('manager', row['id'])
    conclusion = row.get('conclusion') or {}
    if not row['active'] or row['state'] not in ('blocked', 'unavailable') or conclusion.get('result') != 'blocked':
        return {'ref': ref}
    business = service.business()
    shared = business is not None
    if not shared:
        business = row.get('business', {})
    if business.get('total') is None:
        return {'ref': ref}  # 没有可信业务快照，不能选择额度归属。
    done = pushed(service)
    if row['episode'] in done:
        return {'ref': ref}  # 推过的事后来的结论只更新状态。
    scope = 'incident:' + row['episode'] if business['total'] > 0 else 'idle:' + required(business.get('idle_since'), 'idle_since')
    # 同一张卡只并同一栏、没推过的事项：等你的活在「等你」栏，不混进「处理不了」的卡。
    # 全局业务状态是当下这一刻的空闲段，所有事项都在同一段里；旧记录没有全局状态时才逐条比。
    known = [r for r in service.store.all('manager') if r['active'] and r['state'] in ('blocked', 'unavailable')
             and r['episode'] not in done
             and (r.get('conclusion') or {}).get('status_location') == conclusion.get('status_location')
             and (scope.startswith('idle:') and (shared or r.get('business', {}).get('idle_since') == business.get('idle_since'))
                  or r['episode'] == row['episode'])]
    permit = service.permit(key, {'point': 'manager_help', 'scope': scope, 'owner': row['id'], 'basis': conclusion})
    delivery_id = service.store.identity('delivery', permit['ref'])
    if any(r['id'] == delivery_id for r in service.store.all('delivery')) or permit['cancelled']:
        return {'ref': ref}  # 同范围只推一次；之后的结论只更新状态。
    combined = dict(conclusion)
    lines = [(r.get('conclusion') or {}).get('action', '') + '：' + (r.get('conclusion') or {}).get('evidence', '') for r in known]
    combined['evidence'] = '\n'.join(clip(line, (300 - len(lines) + 1) // len(lines)) for line in lines)
    card = help_card(combined, delivery_id, service.status_page())
    service.enqueue(key, {'permit': permit['ref'], 'payload': {'kind': 'manager_help', 'card': card,
                          'section': combined['status_location'], 'primary': row['episode'], 'members': [r['episode'] for r in known]}})
    return {'ref': ref}


def cursor(service):
    """上一拍试到的最后一条送达；下一拍从它的下一条开始，走到尾再回头。"""
    try:
        value = json.loads((service.state / FLUSH_CURSOR).read_text())
    except (OSError, ValueError):
        return None
    return value.get('after') if isinstance(value, dict) else None


def save_cursor(service, after):
    path = service.state / FLUSH_CURSOR
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'after': after}))
    os.replace(temporary, path)


def flush(service, limit=FLUSH_LIMIT, seconds=FLUSH_SECONDS, clock=time.monotonic):
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
    rows = service.store.all('delivery')
    def sendable(row):
        return row['payload'].get('kind') in ('manager_help', 'status_reply') and row['state'] not in ('sent', 'cancelled')
    pending = [r for r in rows if sendable(r)]
    # 从持久游标的下一条开始轮转：队头一条一直不确定时，后面的送达下一拍也能轮到。
    after = cursor(service)
    start = 0
    if after is not None:
        found = next((i for i, r in enumerate(rows) if r['id'] == after), None)
        start = found + 1 if found is not None else 0
    started = clock()
    deliveries, last = [], None
    for record in rows[start:] + rows[:start]:
        # 一条慢送达仍可能用满通道自己的超时，但一次 flush 不会连着跑几分钟。
        if len(deliveries) >= limit or clock() - started >= seconds:
            break
        if not sendable(record):
            continue
        deliveries.append(service.dispatch(record['id']))
        last = record['id']
    if last is not None:
        save_cursor(service, last)
    return {'errors': errors, 'deliveries': deliveries, 'remaining': len(pending) - len(deliveries)}


def reply_card(snapshot, key):
    return {'schema': '2.0', 'header': {'title': plain(snapshot['section']), 'subtitle': plain('车间状态')},
            'body': {'elements': [*({'tag': 'div', 'text': plain(line)} for line in snapshot['lines']),
                                 detail('详情', '\n'.join(snapshot.get('details', [])) + '\n[送达 ' + key + ']')]}}


def rejected(reason):
    return {'state': 'rejected', 'accepted': False, 'reason': reason}


def bound(service, data):
    """核对按钮绑定的原送达；合格返回 (value, delivery)，不合格返回 (None, 拒绝结果)。"""
    config = service.configuration().get('status', {})
    event = data['event']['event']
    if event.get('operator', {}).get('open_id') != config.get('recipient') or event.get('context', {}).get('open_chat_id') != config.get('chat'):
        return None, rejected('只接受指定本人私聊点击')
    action = event.get('action', {})
    value = action.get('value', {})
    if action.get('tag') != 'button' or value.get('ns') != 'workshop-status/v1' or value.get('section') not in SECTIONS:
        return None, rejected('按钮无效')
    try:
        original = service.store.get(value.get('delivery'), 'delivery')
    except ValueError:
        return None, rejected('原送达不存在')
    if (original['state'] != 'sent' or original['payload'].get('kind') != 'manager_help'
            or original['payload']['section'] != value['section']
            or original['external'] != event.get('context', {}).get('open_message_id')):
        return None, rejected('按钮未绑定原送达')
    return value, original


def prepare(service, data):
    """在操作事务之外跑只读状态命令；慢命令不占着写锁，重复点击直接取原结果。"""
    config = service.configuration().get('status', {})
    value, original = bound(service, data)
    if value is None:
        return {'rejected': original}
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
    return {'snapshot': snapshot}


def click(service, key, data, prepared):
    value, original = bound(service, data)
    if value is None:
        return original
    if prepared.get('rejected'):
        return prepared['rejected']
    snapshot = prepared['snapshot']
    service.outbox.clicked(original['id'], service.store.timestamp())
    ref = service.store.identity('delivery', key)
    # 用户主动读取的响应不创建求助许可，不消耗自动推送额度。
    service.store.put('delivery', ref, {'id': ref, 'permit': None, 'state': 'queued', 'external': None,
        'payload': {'kind': 'status_reply', 'card': reply_card(snapshot, ref), 'snapshot': snapshot}}, 'reply_queued')
    return {'accepted': True, 'ref': ref, 'snapshot': snapshot}
