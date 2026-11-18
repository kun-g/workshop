"""作业：把一次插件调用放到独立进程里跑，一拍只登记、启动、读结果，不等它。

每个作业一个目录（状态目录下 jobs/<编号>）：spec.json 是调用说明，pid 是进程号，
lock 由作业进程从启动起一直持有（父进程先锁好再交给它），result.json 是结果。
认回：有结果就用；没结果但锁还被持有就是还在跑；锁空了又没结果，是进程没写结果就退出了。
守护进程重启后照目录认回，不重跑、不丢结果。

同一调用（端口、插件、方法、身份）只有一个作业。结果怎么复用按调用种类定：
- 默认：结果一直可用，只要还有人来取；一天没人取就清掉。
- 新鲜期 N 秒：完成 N 秒内可复用，过期后重跑（轮询、可用性这类会变的查询）。
- 新鲜期 0：取一次就清掉。
- 出错的结果取一次就清掉，下次重跑；错误类型照原样抛回，由核心照原来的退避和隔离规则处理。
"""
from workshop.core.ports import Plugins
from contextlib import suppress
from dataclasses import fields, is_dataclass
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
from queue import SimpleQueue, Empty
import shutil
import signal
from subprocess import DEVNULL, Popen
import sys
import threading
import time
import traceback

from .models import After, Answer, Brief, ChannelPoll, Classification, Event, Evidence, FlowBrief, Poll, Receipt, Relations, Release, RoundUsage, Selection, Started, Usage
from .ports import ChannelRejected, Retryable, Unavailable, TargetBlocked, InputBlocked
from .wake import ring
from .records import StoredBrief, RoundHistory, ResultHistory, materialize


class Pending(BaseException):
    """作业还没出结果。不是故障：调用方跳过这件事，下一拍再来取。

    继承 BaseException，免得被各处 `except Exception` 当成插件故障隔离。"""

    def __init__(self, key, label=None, item=None):
        super().__init__(key)
        self.key, self.label, self.item = key, label, item


class JobFailed(Exception):
    """作业里的插件调用抛了端口没约定的异常；repr 保留原来的样子。"""

    def __init__(self, text, message):
        super().__init__(message)
        self.text = text

    def __repr__(self):
        return self.text


# 结果新鲜期（秒）；不在表里的结果一直可用。
FRESH = {
    ("flow", "relations"): 5,
    ("source", "poll"): 20,
    ("channel", "poll"): 20,
    ("channel", "reconcile"): 0,
    ("executor", "poll"): 30,
    ("executor", "available"): 60,
    ("selector", "select"): 60,
}

# 活在等这些作业时，状态里显示的字样。
LABELS = {
    ("flow", "assess"): "等检查",
    ("flow", "after"): "等检查",
    ("flow", "brief"): "等检查",
}

ERRORS = {"Retryable": Retryable, "Unavailable": Unavailable, "ChannelRejected": ChannelRejected,
          "TargetBlocked": TargetBlocked, "InputBlocked": InputBlocked}
TYPES = {cls.__name__: cls for cls in (After, Answer, Brief, ChannelPoll, Classification, Event, Evidence, FlowBrief, Poll,
                                        Receipt, Relations, Release, RoundUsage, Selection, Started, Usage, StoredBrief, RoundHistory, ResultHistory)}
KEEP_SECONDS = 86400
SWEEP_SECONDS = 600
# 一拍最多登记几个新作业；进程创建交给后台，避免系统调用拖住一拍。
SPAWNS_PER_TICK = 4


def encode(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {"__dataclass__": type(value).__name__,
                "fields": {f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"作业参数或结果不能序列化：{type(value).__name__}")


def decode(value):
    if isinstance(value, dict):
        if set(value) == {"__dataclass__", "fields"} and value["__dataclass__"] in TYPES:
            return TYPES[value["__dataclass__"]](**{k: decode(v) for k, v in value["fields"].items()})
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def digest(*parts):
    text = json.dumps(encode(list(parts)), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def write(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(temporary, path)


def read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def held(path):
    """作业进程还持有锁：还在跑。"""
    try:
        handle = os.open(path / "lock", os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(handle)
    return False


def running(directory):
    """状态目录下正在跑的作业：{活 id: 字样}；只读文件，给状态查询用。"""
    labels = {}
    root = Path(directory)
    if not root.is_dir():
        return labels
    for path in root.iterdir():
        # 跑完的作业留一天待取，spec 可能很大；先用结果文件和锁筛掉，只读在跑的 spec。
        if (path / "result.json").exists() or not held(path):
            continue
        spec = read(path / "spec.json")
        if spec and spec.get("item") and spec.get("label"):
            labels[spec["item"]] = spec["label"]
    return labels


class Jobs:
    """command 是启动作业进程的 argv 前缀，后面接作业目录；进程里调用 execute。"""

    def __init__(self, directory, command, *, deadline=7200, clock=time.time):
        self.directory = Path(directory)
        self.command = list(command)
        self.deadline = deadline
        self.clock = clock
        self.children = {}
        self.spawned = 0
        self.swept = 0.0
        self.sweeper = None
        self.due = []
        self.again = False
        self.retries = {}
        self.launcher = ThreadPoolExecutor(max_workers=1, thread_name_prefix="workshop-start")
        # Jobs 在进入主循环前构造；先起好线程，一拍内只入队，不等线程启动。
        self.launcher.submit(lambda: None).result()
        self.launched = SimpleQueue()

    def path(self, port, name, method, identity):
        return self.directory / digest(port, name, method, identity)

    def exists(self, port, name, method, identity):
        return (self.path(port, name, method, identity) / "spec.json").exists()

    def call(self, port, name, method, args, *, identity=None, item=None, lookup=None):
        identity = list(args) if identity is None else identity
        path = self.path(port, name, method, identity)
        label = LABELS.get((port, method))
        attempts, retry_at = self.retries.get(path, (0, 0))
        if retry_at > self.clock():
            self.due.append(retry_at)
            raise Pending(path.name, label, item)
        # 极快的子进程可能在启动线程公布 pid 前写完结果；此时不能消费并删目录。
        starting = read(path / "pid") == -1 and held(path)
        result = None if starting else read(path / "result.json")
        if result is not None and (path / "invalidated").exists():
            shutil.rmtree(path, ignore_errors=True)
            result = None
        if result is not None:
            fresh = FRESH.get((port, method))
            if "error" in result:
                # 完成信立即叫醒主循环；没有上层退避的调用也不能因此连续重启失败作业。
                retry_at = self.clock() + min(60, 2 ** min(attempts, 6))
                self.retries[path] = attempts + 1, retry_at
                self.due.append(retry_at)
                shutil.rmtree(path, ignore_errors=True)
                error = result["error"]
                kind = ERRORS.get(error.get("kind"))
                if kind:
                    raise kind(error.get("message", ""))
                raise JobFailed(error.get("repr", "作业失败"), error.get("message", ""))
            # 0 表示只消费一次，不是完成后立即过期。
            if fresh is not None and fresh > 0 and self.clock() - result.get("finished", 0) >= fresh:
                shutil.rmtree(path, ignore_errors=True)
            else:
                if fresh == 0:
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    # 后台清理可能刚删掉一天没人取的旧结果；这次照常用读到的内容。
                    with suppress(FileNotFoundError):
                        os.utime(path / "result.json")
                if fresh is not None and fresh > 0:
                    self.due.append(result.get("finished", 0) + fresh)
                self.retries.pop(path, None)
                return decode(result["ok"])
        elif (path / "pid").exists():
            if held(path):
                spec = read(path / "spec.json") or {}
                self.due.append(spec.get("deadline", self.clock() + self.deadline))
                if self.clock() >= spec.get("deadline", float("inf")):
                    self.kill(path)
                    raise Retryable(f"作业超过 {self.deadline} 秒没出结果，已结束：{port}/{name}.{method}")
                raise Pending(path.name, label, item)
            if (path / "result.json").exists():
                # 读结果和查锁之间刚跑完。
                return self.call(port, name, method, args, identity=identity, item=item, lookup=lookup)
            shutil.rmtree(path, ignore_errors=True)
            raise Retryable(f"作业进程没写结果就退出了：{port}/{name}.{method}")
        if self.spawned >= SPAWNS_PER_TICK:
            self.again = True
            raise Pending(path.name, label, item)
        self.start(path, {"port": port, "name": name, "method": method, "args": encode(list(args)),
                          "item": item, "label": label, "lookup": lookup,
                          "started": self.clock(), "deadline": self.clock() + self.deadline})
        raise Pending(path.name, label, item)

    def invalidate(self, port, name, identity):
        """在途调用不终止；它的结果回来后丢掉一次，再查变化后的状态。"""
        path = self.path(port, name, "poll", identity)
        if path.is_dir():
            (path / "invalidated").touch()

    def wait_timeout(self):
        return 0 if self.again else max(0, min([60, *(at - self.clock() for at in self.due)]))

    def start(self, path, spec):
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True)
        write(path / "spec.json", spec)
        lock = os.open(path / "lock", os.O_CREAT | os.O_RDWR, 0o600)
        output = os.open(path / "output.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # -1 表示已登记但尚未创建进程。锁从入队到作业退出一直持有，
            # 重启时也能区分仍在启动与启动者已退出，不能把未写 pid 当成未登记。
            write(path / "pid", -1)
            self.launcher.submit(self.spawn, path, lock, output)
        except BaseException:
            os.close(lock)
            os.close(output)
            raise
        self.due.append(spec["deadline"])
        self.spawned += 1

    def spawn(self, path, lock, output):
        """只在启动线程里创建独立进程；插件本身不在线程里运行。"""
        pid = None
        try:
            if (path / "cancelled").exists():
                return
            # os.posix_spawnp 创建期间持有 GIL，放在线程中仍会卡住主循环。
            # Popen 的独立会话路径在父进程读 exec 确认管道时释放 GIL。
            # 只传本作业的锁；子进程活着，锁就一直被持有。
            child = Popen([*self.command, str(path)], stdin=DEVNULL, stdout=output, stderr=output,
                          start_new_session=True, pass_fds=(lock,))
            pid = child.pid
            self.launched.put(child)
            write(path / "pid", pid)
            if (path / "cancelled").exists():
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except Exception as exc:
            if pid is not None:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            write(path / "result.json", {"error": {"kind": "Retryable", "message": f"作业启动失败：{exc!r}"},
                                         "finished": self.clock()})
        finally:
            os.close(lock)
            os.close(output)
            if (path / "cancelled").exists():
                shutil.rmtree(path, ignore_errors=True)
            ring(self.directory.parent)

    def kill(self, path):
        try:
            # 先标取消，再读 pid，避免启动线程刚写完 pid 并退出时漏掉取消。
            (path / "cancelled").touch()
            pid = int((path / "pid").read_text())
            if pid <= 0:
                # 不能给占位值发进程组信号。启动线程看到后取消排队，
                # 或在正在进行的进程创建返回后立即结束它，再清理目录。
                return
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass
        shutil.rmtree(path, ignore_errors=True)

    def reap(self):
        """每拍开头调用：收掉已退出的作业进程（WNOHANG，不等），重置这一拍的启动数，
        定期清掉一天没人取的结果和没启动成的目录。"""
        self.spawned = 0
        self.due = []
        self.again = False
        while True:
            try:
                child = self.launched.get_nowait()
                self.children[child.pid] = child
            except Empty:
                break
        for pid, child in list(self.children.items()):
            if child.poll() is not None:  # 内部 waitpid(WNOHANG)，不等。
                del self.children[pid]
        now = self.clock()
        if now - self.swept < SWEEP_SECONDS or not self.directory.is_dir():
            return
        self.swept = now
        # 上千个目录逐个 stat、删除要一两百毫秒，曾占满整拍；放到单独线程，一拍不等（核心 R27）。
        # 不用启动线程：清理慢时不能拖住新作业的进程创建。上一次还没清完就不再起。
        if self.sweeper is None or not self.sweeper.is_alive():
            self.sweeper = threading.Thread(target=self.sweep, args=(now,), name="workshop-sweep", daemon=True)
            self.sweeper.start()

    def sweep(self, now):
        """清掉一天没人取的结果和没启动成的目录。和主线程并发也安全：
        主线程刚取用的旧结果被删，下次只是重跑；新登记的目录不满 60 秒且有结果或 pid，不会被删。"""
        for path in self.directory.iterdir():
            try:
                result = path / "result.json"
                if result.exists():
                    if now - result.stat().st_mtime > KEEP_SECONDS:
                        shutil.rmtree(path, ignore_errors=True)
                elif not (path / "pid").exists() and now - path.stat().st_mtime > 60:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue


def execute(path, plugins: Plugins, lookup=None):
    """作业进程里跑：按 spec 调插件，把结果或错误原子写进 result.json。"""
    path = Path(path)
    spec = json.loads((path / "spec.json").read_text())
    try:
        plugin = plugins.get(spec["port"], spec["name"])
        args = materialize(decode(spec["args"]))
        if spec.get("lookup"):
            args.append(lookup(spec["lookup"]))
        result = {"ok": encode(getattr(plugin, spec["method"])(*args))}
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        kind = next((name for name, cls in ERRORS.items() if isinstance(exc, cls)), "error")
        result = {"error": {"kind": kind, "message": str(exc), "repr": repr(exc)}}
    result["finished"] = time.time()
    write(path / "result.json", result)
    ring(path.parent.parent)
