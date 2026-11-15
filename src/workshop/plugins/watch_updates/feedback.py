"""复制反馈与会话代录；只解析数据，不执行正文。"""
import json
import re


SOURCES = ('cli:feishu', 'cli:session')


def read_message(io, target, source, ext_id, body):
    if source == 'cli:feishu':
        return io.message(target, ext_id), {'channel': 'feishu', 'message_id': ext_id}
    if source == 'cli:session' and isinstance(body, dict) and body.get('channel') == 'session':
        if all(isinstance(body.get(k), str) and body[k].strip() for k in ('session', 'quote')):
            # 原话可以是自然语言；会话把选择另填为结构化资料，不能覆盖原话。
            text = json.dumps(body['feedback'], ensure_ascii=False) if isinstance(body.get('feedback'), dict) else body['quote']
            return text, {'channel': 'session', 'session': body['session'], 'recorded': True, 'quote': body['quote']}
    return None, {}


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
        if target['kind'] == 'instances':
            for member in target['members']:
                pair = (member['key'], member['instance'])
                if pair in covered:
                    raise ValueError('反馈目标包含重复部署')
                covered.add(pair)
                items.append({**member, 'decision': row['choice'], 'note': row.get('note', ''), 'target_id': row['id']})
        elif target['kind'] == 'feature':
            items.append({'key': target['key'], 'instance': target['instance'], 'decision': '',
                          'features': [{'id': target['feature'], 'choice': row['choice']}]})
        elif target['kind'] == 'need':
            needs.append({'id': target['need'], 'choice': row['choice']})
    return {'report': value['report'], 'items': items, 'needs': needs}
