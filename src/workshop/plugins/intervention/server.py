"""仅本机用户可访问；一个服务串行提交请求，独立安装可启动。"""
import fcntl
import json
import os
from pathlib import Path
import socketserver
import stat
from .client import MAX_BYTES
from .service import Service
from .store import encode


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(10)
        try:
            raw = self.rfile.readline(MAX_BYTES + 1)
            if not raw.endswith(b'\n') or len(raw) > MAX_BYTES:
                raise ValueError('请求过大或未结束')
            message = json.loads(raw)
            result = self.server.service.call(message['action'], message.get('data', {}))
            # 状态点击只读；亲眼看点击作答了要叫醒车间。
            if message['action'] not in ('get', 'status', 'ping', 'status_click') or isinstance(result, dict) and result.get('answered'):
                self.server.changed.touch()
            response = {'ok': True, 'result': result}
        except (ValueError, KeyError, TypeError, OSError) as exc:
            response = {'ok': False, 'error': str(exc)}
        try:
            self.wfile.write((encode(response) + '\n').encode())
        except OSError:
            pass  # 客户端退出不回滚已提交的操作；以同一操作号重试。


def serve(state, rules, endpoint, ready_fd=None):
    state, endpoint = Path(state).resolve(), Path(endpoint).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    endpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # socket 父目录须由安装者准备为私有目录；不更改共享目录权限。
    if endpoint.parent.stat().st_uid != os.getuid() or endpoint.parent.stat().st_mode & 0o077:
        raise ValueError('socket 父目录必须归当前用户且权限为 0700')
    with (state / 'instance.lock').open('a') as state_lock, endpoint.with_suffix('.lock').open('a') as lock:
        fcntl.flock(state_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if endpoint.exists() and not stat.S_ISSOCK(endpoint.lstat().st_mode):
            raise ValueError('拒绝覆盖非 socket 文件')
        endpoint.unlink(missing_ok=True)
        service = Service(state, rules)
        try:
            with socketserver.UnixStreamServer(str(endpoint), Handler) as server:
                endpoint.chmod(0o600)
                server.service = service
                server.changed = endpoint.with_suffix(".changed")
                server.changed.touch()
                if ready_fd is not None:
                    os.write(ready_fd, b'ready\n')
                    os.close(ready_fd)
                print(encode({'ready': str(endpoint), 'pid': os.getpid()}), flush=True)
                server.serve_forever()
        finally:
            service.store.close()
            endpoint.unlink(missing_ok=True)
