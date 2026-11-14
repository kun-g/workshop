"""发版专用文件与进程边界；不访问核心状态库，不引用其他插件。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from workshop.core.ports import Retryable
from workshop.core.wake import ring
from workshop.github_identity import run as authenticated_run


def read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)


class IO:
    def __init__(self, state_dir=None):
        self.state_dir = Path(state_dir) if state_dir else None

    def root(self, target):
        return self.state_dir or Path(target.get('scheduled_release_state_dir', '~/.local/state/workshop')).expanduser().resolve()

    @contextmanager
    def locked(self, target):
        root = self.root(target)
        root.mkdir(parents=True, exist_ok=True)
        with (root / 'scheduled-release.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Retryable('另一次发版作业正在写入，稍后重试') from exc
            yield root

    def now(self):
        return datetime.now(timezone.utc).isoformat()

    def command(self, argv, *, cwd=None, timeout=30, isolated=False):
        env = None
        temporary = None
        if isolated:
            # 检查和依赖准备没有部署身份；GitHub 凭据仅用于取目标提交。
            env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'TMPDIR') if k in os.environ}
            temporary = tempfile.TemporaryDirectory(prefix='workshop-release-home-')
            home = Path(temporary.name)
            env.update(HOME=str(home), GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
                       GIT_TERMINAL_PROMPT='0', GH_CONFIG_DIR=str(home / 'gh'),
                       AWS_CONFIG_FILE='/dev/null', AWS_SHARED_CREDENTIALS_FILE='/dev/null', KUBECONFIG='/dev/null',
                       GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.hooksPath', GIT_CONFIG_VALUE_0='/dev/null')
        try:
            result = authenticated_run(argv, cwd=cwd, env=env, timeout=timeout,
                                       capture_output=True, text=True, errors='replace')
            return dict(argv=list(argv), exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        except subprocess.TimeoutExpired as exc:
            def text(value):
                return value.decode(errors='replace') if isinstance(value, bytes) else value or ''
            return dict(argv=list(argv), exit_code=124, stdout=text(exc.stdout), stderr=text(exc.stderr) + '\n命令超时')
        except OSError as exc:
            return dict(argv=list(argv), exit_code=127, stdout='', stderr=str(exc))
        finally:
            if temporary is not None:
                temporary.cleanup()

    def git(self, checkout, *args):
        result = self.command(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(checkout), *args])
        if result['exit_code']:
            raise Retryable('git 未完成：' + result['stderr'][-500:])
        return result['stdout'].strip()

    @staticmethod
    def process(pid):
        if pid <= 1:
            return None
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                                env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, capture_output=True, text=True, timeout=5)
        start = result.stdout.strip()
        return {'pid': pid, 'start': start} if result.returncode == 0 and start else None

    def daemon(self):
        # 生产 after 在 Jobs.spawn 的直接子进程中。--once 不具备重启契约，拒绝上线。
        if len(sys.argv) < 3 or sys.argv[-2] != 'job':
            raise Retryable('定时发版须由守护进程的作业运行，不能用 run --once 上线')
        pid = os.getppid()
        identity = self.process(pid)
        if identity is None or os.getppid() != pid:
            raise Retryable('发版作业已失去父守护进程，不能确认重启')
        return identity

    def heartbeat_ready(self, root, daemon):
        # 旧父已退出后，主动唤醒新父写一拍心跳；不能接受旧进程退出前的最后心跳。
        began = self.now()
        ring(root)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            heartbeat = read(root / 'heartbeat.json')
            if heartbeat.get('last_ok_at', '') >= began:
                return self.daemon() == daemon
            time.sleep(.05)
        return False
