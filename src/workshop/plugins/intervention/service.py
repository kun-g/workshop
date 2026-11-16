"""等待、回答和送达的权威记录。许可目录只从唯一规则文件加载。"""
import json
from pathlib import Path
import subprocess
import yaml
from .store import Store, encode, required


class Service:
    def __init__(self, state, rules):
        self.store = Store(Path(state) / 'intervention.db')
        self.rules = Path(rules)

    def policy(self, point):
        config = yaml.safe_load(self.rules.read_text()).get('intervention', {})
        points = config.get('points', {})
        if point not in points:
            raise ValueError('介入点未登记')
        return points[point], config

    def call(self, action, data):
        if not isinstance(data, dict):
            raise ValueError('data 必须是对象')
        fields = {
            'ping': set(), 'get': {'ref'}, 'status': {'ref'}, 'dispatch': {'ref'},
            'create': {'operation', 'point', 'owner', 'content'},
            'answer': {'operation', 'ref', 'value', 'source'},
            'ack': {'operation', 'ref', 'token'},
            'permit': {'operation', 'point', 'scope', 'owner', 'basis'},
            'enqueue': {'operation', 'permit', 'payload'},
            'cancel': {'operation', 'ref', 'reason'},
        }
        if action not in fields or set(data) != fields[action]:
            raise ValueError('操作字段不完整或有未知字段')
        if action == 'ping':
            return {'protocol': 1}
        if action == 'get':
            return self.store.get(data['ref'])
        if action == 'status':
            wait = self.store.get(data['ref'], 'wait')
            return {'ref': wait['id'], 'ready': wait['answer'] is not None, 'token': wait['token']}
        if action == 'dispatch':
            return self.dispatch(data['ref'])
        handlers = {'create': self.create, 'answer': self.answer, 'ack': self.ack,
                    'permit': self.permit, 'enqueue': self.enqueue, 'cancel': self.cancel}
        if action not in handlers:
            raise ValueError('未知操作')
        key = required(data.get('operation'), 'operation')
        payload = {k: v for k, v in data.items() if k != 'operation'}
        return self.store.operation(key, action, payload, lambda: handlers[action](key, payload))

    def create(self, key, data):
        point = required(data.get('point'), 'point')
        policy, _ = self.policy(point)
        if policy.get('wait') is not True:
            raise ValueError('该介入点不接受等待')
        required(data.get('owner'), 'owner')
        if not isinstance(data.get('content'), dict) or not data['content']:
            raise ValueError('等待内容不能为空')
        ref = self.store.identity('wait', key)
        record = {'id': ref, **data, 'answer': None, 'token': None, 'acknowledged': False}
        self.store.put('wait', ref, record, 'created')
        return {'ref': ref}

    def answer(self, key, data):
        record = self.store.get(data['ref'], 'wait')
        source = data.get('source', {})
        if not isinstance(source, dict):
            raise ValueError('回答须注明来源')
        channel = source.get('channel')
        if channel == 'session':
            required(source.get('session'), 'session')
            required(source.get('quote'), 'quote')
        elif channel == 'external':
            required(source.get('reference'), 'reference')
            required(source.get('evidence'), 'evidence')
        else:
            raise ValueError('回答须来自会话或经回查的外部记录')
        if 'value' not in data or data['value'] is None or data['value'] in ('', [], {}):
            raise ValueError('空回答不能解除等待')
        answer = {'value': data['value'], 'source': source}
        if record['answer'] is not None:
            if record['answer'] != answer:
                raise ValueError('已有回答；新问题须创建新的等待')
        else:
            record.update(answer=answer, token=self.store.identity('result', key))
            self.store.put('wait', record['id'], record, 'answered')
        return {'ref': record['id'], 'token': record['token']}

    def ack(self, key, data):
        record = self.store.get(data['ref'], 'wait')
        if not record['token'] or data.get('token') != record['token']:
            raise ValueError('未完成或结果标识不符')
        if not record['acknowledged']:
            record['acknowledged'] = True
            self.store.put('wait', record['id'], record, 'acknowledged')
        return {'ref': record['id'], 'token': record['token']}

    def permit(self, key, data):
        policy, _ = self.policy(data.get('point'))
        if policy.get('send') is not True:
            raise ValueError('该介入点禁止推送')
        scope = required(data.get('scope'), 'scope')
        if not any(scope.startswith(p) and scope != p for p in policy.get('scope_prefixes', [])):
            raise ValueError('送达去重范围不符合规则')
        basis = data.get('basis')
        if not isinstance(basis, dict):
            raise ValueError('许可缺依据')
        for field in policy.get('required_basis', []):
            required(basis.get(field), field)
        required(data.get('owner'), 'owner')
        # 范围由规则约束：空闲段共用一次，其他按事项/约定对象一次。
        ref = self.store.identity('permit', data['point'] + ':' + scope)
        try:
            record = self.store.get(ref, 'permit')
        except ValueError:
            record = {'id': ref, **data, 'cancelled': False}
            self.store.put('permit', ref, record, 'permitted')
        return {'ref': ref, 'cancelled': record['cancelled']}

    def enqueue(self, key, data):
        permit = self.store.get(data['permit'], 'permit')
        policy, _ = self.policy(permit['point'])
        if permit['cancelled'] or policy.get('send') is not True:
            raise ValueError('许可已取消或停用')
        if not isinstance(data.get('payload'), dict) or not data['payload']:
            raise ValueError('送达内容不能为空')
        ref = self.store.identity('delivery', permit['id'])
        try:
            old = self.store.get(ref, 'delivery')
        except ValueError:
            old = None
        if old:
            if old['payload'] != data['payload']:
                raise ValueError('同一许可不能更改已排队内容')
            return {'ref': ref}
        self.store.put('delivery', ref, {'id': ref, **data, 'state': 'queued', 'external': None}, 'queued')
        return {'ref': ref}

    def cancel(self, key, data):
        record = self.store.get(data['ref'], 'permit')
        required(data.get('reason'), 'reason')
        if not record['cancelled']:
            record.update(cancelled=True, reason=data['reason'])
            self.store.put('permit', record['id'], record, 'cancelled')
        return {'ref': record['id']}

    def transport(self, config, action, record):
        argv = config.get('transport')
        if not isinstance(argv, list) or not argv or not all(isinstance(s, str) and s for s in argv):
            raise ValueError('送达通道尚未配置')
        result = subprocess.run(argv, input=encode({'action': action, 'key': record['id'], 'payload': record['payload']}),
                                capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise ValueError('送达通道未确认完成')
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get('state') not in ('sent', 'absent', 'unknown'):
            raise ValueError('送达回执无效')
        if value['state'] == 'sent':
            required(value.get('external'), 'external')
        return value

    def dispatch(self, ref):
        record = self.store.get(ref, 'delivery')
        if record['state'] in ('sent', 'cancelled'):
            return record
        permit = self.store.get(record['permit'], 'permit')
        policy, config = self.policy(permit['point'])
        try:
            if record['state'] in ('sending', 'uncertain'):
                # 发送后的崩溃窗口必须先对账；即使许可取消，也保留已送达的真实结果。
                result = self.transport(config, 'reconcile', record)
                if result['state'] == 'sent':
                    return self.delivery_state(record, 'sent', result['external'])
                if result['state'] != 'absent':
                    return self.delivery_state(record, 'uncertain')
            if permit['cancelled'] or policy.get('send') is not True:
                return self.delivery_state(record, 'cancelled')
            self.delivery_state(record, 'sending')  # 提交发送意图，才调用通道。
            result = self.transport(config, 'send', record)
            return self.delivery_state(record, 'sent', result['external']) if result['state'] == 'sent' else self.delivery_state(record, 'uncertain')
        except (ValueError, OSError, subprocess.SubprocessError):
            return self.delivery_state(record, 'uncertain')

    def delivery_state(self, record, state, external=None):
        record = {**record, 'state': state, 'external': external}
        with self.store.transaction():
            self.store.put('delivery', record['id'], record, state)
        return record
