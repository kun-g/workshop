"""本机信箱：信只负责叫醒；事实仍从状态库和作业结果读取。"""
from contextlib import suppress
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
import select
import selectors
import socket
import time


def ring(state_dir, what=""):
    """非阻塞、尽力送达。接收方不在或队列满，不影响已经提交的事实。"""
    with suppress(OSError):
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)
            sender.sendto(str(what).encode()[:4096], str(Path(state_dir) / "wake.sock"))


@dataclass
class Wake:
    messages: set[str] = field(default_factory=set)
    paths: set[Path] = field(default_factory=set)
    received_at: float | None = None


class Mailbox:
    """一个状态目录只允许一个接收方；父目录与文件一起盯，支持替换、删除后重建。"""

    def __init__(self, state_dir, paths=()):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "wake.sock"
        self.lock = (self.root / "wake.lock").open("a")
        self.socket = None
        self.queue = None
        self.selector = None
        self.files = {}
        self.paths = {Path(p).expanduser().absolute(): self.signature(Path(p).expanduser().absolute()) for p in paths}
        self.owned = False
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.path.unlink(missing_ok=True)  # 锁已取得，留下的只能是上次接收方的地址。
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.socket.setblocking(False)
            self.socket.bind(str(self.path))
            self.owned = True
            os.chmod(self.path, 0o600)
            if hasattr(select, "kqueue"):
                self.queue = select.kqueue()
                self.queue.control([select.kevent(self.socket.fileno(), filter=select.KQ_FILTER_READ,
                                                 flags=select.KQ_EV_ADD)], 0, 0)
                self.rearm()
            else:
                if self.paths:
                    raise OSError("文件唤醒需要支持 kqueue 的系统")
                self.selector = selectors.DefaultSelector()
                self.selector.register(self.socket, selectors.EVENT_READ)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def signature(path):
        try:
            info = path.stat()
            return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size
        except FileNotFoundError:
            return None

    def rearm(self):
        desired = set(self.paths)
        for path in self.paths:
            parent = path.parent
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            desired.add(parent)
        for path, (fd, identity) in list(self.files.items()):
            signature = self.signature(path)
            if path not in desired or signature is None or signature[:2] != identity:
                os.close(fd)  # 关闭描述符也取消旧 inode 的监听。
                del self.files[path]
        for path in desired - self.files.keys():
            try:
                fd = os.open(path, os.O_RDONLY)
            except FileNotFoundError:
                continue
            try:
                self.queue.control([select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_ATTRIB |
                           select.KQ_NOTE_RENAME | select.KQ_NOTE_DELETE | select.KQ_NOTE_REVOKE)], 0, 0)
                stat = os.fstat(fd)
                self.files[path] = fd, (stat.st_dev, stat.st_ino)
            except BaseException:
                os.close(fd)
                raise

    def changed_paths(self):
        changed = set()
        for path, before in list(self.paths.items()):
            after = self.signature(path)
            if before != after:
                changed.add(path)
                self.paths[path] = after
        return changed

    def wait(self, timeout):
        """到期或收信返回；无关的目录变动不会让调用方空转。"""
        # 构造信箱时从取基线到登记监听之间也可能有写入；先检查，不能漏到下一次心跳。
        received = time.monotonic()
        changed = self.changed_paths()
        if changed:
            if self.queue is not None:
                self.rearm()
            return Wake(paths=changed, received_at=received)
        deadline = time.monotonic() + max(0, min(60, timeout))
        while True:
            remaining = max(0, deadline - time.monotonic())
            events = (self.queue.control(None, 128, remaining) if self.queue is not None
                      else self.selector.select(remaining))
            received = time.monotonic()
            result = Wake(received_at=received if events else None)
            # 有限批次，持续发信也不饿死处理；余信让下一次 wait 立即返回。
            for _ in range(256):
                try:
                    result.messages.add(self.socket.recv(4096).decode(errors="replace"))
                    result.received_at = received
                except BlockingIOError:
                    break
            result.paths = self.changed_paths()
            if result.paths:
                result.received_at = received
            if self.queue is not None:
                self.rearm()
            if result.messages or result.paths or time.monotonic() >= deadline:
                return result

    def close(self):
        for fd, _ in self.files.values():
            os.close(fd)
        self.files.clear()
        if self.queue is not None:
            self.queue.close()
        if self.selector is not None:
            self.selector.close()
        if self.socket is not None:
            self.socket.close()
        if self.owned:
            self.path.unlink(missing_ok=True)
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
