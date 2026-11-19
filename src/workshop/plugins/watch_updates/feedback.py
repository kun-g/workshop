"""复制反馈；只解析数据，不执行正文。"""
import json
import re


SOURCES = ('cli:feishu',)


def read_message(io, target, source, ext_id, body):
    if source == 'cli:feishu':
        return io.message(target, ext_id), {'channel': 'feishu', 'message_id': ext_id}
    return None, {}


def drop_scope(row, target):
    """「不用盯」的范围只认报告快照为这一行登记的选项；旧报告没登记范围，按「只这一处」处理。"""
    if row['choice'] != '不用盯' or target['kind'] != 'instances':
        if 'scope' in row:
            raise ValueError('只有「不用盯」带范围')
        return None
    options = target.get('scopes')
    if options is None:
        if 'scope' in row:
            raise ValueError('这份报告没有登记范围选项')
        return {'id': 'here', 'kind': 'instance', 'label': '只这一处', 'software': target['software'],
                'instances': [m['instance'] for m in target['members']]}
    chosen = row.get('scope', 'here')
    match = [o for o in options if o['id'] == chosen]
    if not isinstance(chosen, str) or len(match) != 1:
        raise ValueError('不用盯的范围不是这份报告为这一行登记的选项')
    return match[0]


def feedback(text, state):
    blocks = re.findall(r'^```json\s*\n(.*?)\n```\s*$', text, re.M | re.S)
    if len(blocks) > 1:
        raise ValueError('反馈只能含一段 JSON')
    value = json.loads(blocks[0] if blocks else text)
    if (not isinstance(value, dict) or 'schema' in value or not isinstance(value.get('report'), str)
            or value['report'] not in state['reports'] or not isinstance(value.get('items'), list)):
        raise ValueError('报告编号或反馈格式无效')
    report = state['reports'][value['report']]
    registry = report.get('feedback_targets', {})
    items, needs, seen, covered = [], [], set(), set()
    for row in value['items']:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] not in registry or row['id'] in seen:
            raise ValueError('反馈目标不存在或重复')
        seen.add(row['id'])
        target = registry[row['id']]
        if row.get('choice') not in target['choices'] or not isinstance(row.get('note', ''), str):
            raise ValueError('反馈选项或理由无效')
        if len(row.get('note', '')) > 4000:
            raise ValueError('反馈理由过长')
        scope = drop_scope(row, target)
        if target['kind'] == 'instances':
            for member in target['members']:
                pair = (member['key'], member['instance'])
                if pair in covered:
                    raise ValueError('反馈目标包含重复部署')
                covered.add(pair)
                items.append({**member, 'decision': row['choice'], 'note': row.get('note', ''), 'target_id': row['id'],
                              **({'scope': scope} if scope else {})})
        elif target['kind'] == 'feature':
            items.append({'key': target['key'], 'instance': target['instance'], 'decision': '',
                          'features': [{'id': target['feature'], 'choice': row['choice']}]})
        elif target['kind'] == 'need':
            needs.append({'id': target['need'], 'choice': row['choice']})
    return {'report': value['report'], 'items': items, 'needs': needs}
