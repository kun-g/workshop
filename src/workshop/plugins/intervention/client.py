"""Unix socket 客户端；超时/空回执都不是完成。"""
import json
import socket
from pathlib import Path
from .store import encode

MAX_BYTES = 2 * 1024 * 1024


def request(endpoint, action, data, timeout=5):
    payload = (encode({'action': action, 'data': data}) + '\n').encode()
    if len(payload) > MAX_BYTES:
        raise ValueError('请求过大')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(Path(endpoint).expanduser()))
        connection.sendall(payload)
        with connection.makefile('rb') as stream:
            raw = stream.readline(MAX_BYTES + 1)
    if not raw.endswith(b'\n') or len(raw) > MAX_BYTES:
        raise ConnectionError('介入点服务未确认完整响应')
    value = json.loads(raw)
    if not isinstance(value, dict) or type(value.get('ok')) is not bool:
        raise ConnectionError('介入点服务响应无效')
    if not value['ok']:
        raise ValueError(value.get('error', '介入点请求被拒绝'))
    return value['result']


class Client:
    """经核心 WaitService 端口使用；不装配其他插件。"""
    def wake_paths(self, config):
        return [str(Path(config['endpoint']).expanduser().with_suffix('.changed'))]

    def invoke(self, action, data, config):
        return request(config['endpoint'], action, data, config.get('timeout', 5))

    def create(self, operation, content, config):
        return self.invoke('create', {**content, 'operation': operation}, config)['ref']

    def read(self, reference, config):
        return self.invoke('get', {'ref': reference}, config)

    def poll(self, reference, config):
        return self.invoke('status', {'ref': reference}, config)

    def acknowledge(self, operation, reference, token, config):
        return self.invoke('ack', {'operation': operation, 'ref': reference, 'token': token}, config)


def make_wait():
    return Client()
