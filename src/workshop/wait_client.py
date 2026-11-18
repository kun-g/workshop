"""Unix socket 客户端；超时/空回执都不是完成。"""
import json
import socket
from pathlib import Path
def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

MAX_BYTES = 2 * 1024 * 1024


class Rejected(ValueError):
    """服务明确返回拒绝，区别于客户端自身编码错误。"""


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
        raise Rejected(value.get('error', '介入点请求被拒绝'))
    return value['result']


class Client:
    """经核心 WaitService 端口使用；不装配其他插件。"""
    def validate(self, config):
        if not isinstance(config.get('endpoint'), str) or not config['endpoint'].strip():
            raise ValueError('endpoint 必须是非空本机地址')
        if type(config.get('timeout', 5)) not in (int, float) or not 0 < config.get('timeout', 5) <= 30:
            raise ValueError('timeout 必须大于 0 且不超过 30 秒')
        if set(config) - {'endpoint', 'timeout'}:
            raise ValueError('未知字段：' + ', '.join(sorted(set(config) - {'endpoint', 'timeout'})))

    def wake_paths(self, config):
        return [str(Path(config['endpoint']).expanduser().with_suffix('.changed'))]

    def invoke(self, action, data, config):
        # 独立服务和 CLI 的 request 不依赖核心；装进车间的端口才映射异常。
        from workshop.core.ports import WaitRejected, WaitUnavailable
        try:
            return request(config['endpoint'], action, data, config.get('timeout', 5))
        except Rejected as exc:
            raise WaitRejected(str(exc)) from exc
        except OSError as exc:
            raise WaitUnavailable(str(exc)) from exc

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
