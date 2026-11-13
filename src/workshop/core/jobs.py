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
from dataclasses import fields, is_dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import traceback

from .models import After, Answer, Brief, Classification, Event, Evidence, FlowBrief, Poll, Receipt, RoundUsage, Selection, Started, Usage
from .ports import ChannelRejected, Retryable, Unavailable


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

ERRORS = {"Retryable": Retryable, "Unavailable": Unavailable, "ChannelRejected": ChannelRejected}
TYPES = {cls.__name__: cls for cls in (After, Answer, Brief, Classification, Event, Evidence, FlowBrief, Poll,
                                        Receipt, RoundUsage, Selection, Started, Usage)}
KEEP_SECONDS = 86400
SWEEP_SECONDS = 600


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
        spec = read(path / "spec.json")
        if spec and spec.get("item") and spec.get("label") and not (path / "result.json").exists() and held(path):
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
        self.swept = 0.0

    def path(self, port, name, method, identity):
        return self.directory / digest(port, name, method, identity)

    def exists(self, port, name, method, identity):
        return (self.path(port, name, method, identity) / "spec.json").exists()

    def call(self, port, name, method, args, *, identity=None, item=None, lookup=None):
        identity = list(args) if identity is None else identity
        path = self.path(port, name, method, identity)
        label = LABELS.get((port, method))
        result = read(path / "result.json")
        if result is not None:
            fresh = FRESH.get((port, method))
            if "error" in result:
                shutil.rmtree(path, ignore_errors=True)
                error = result["error"]
                kind = ERRORS.get(error.get("kind"))
                if kind:
                    raise kind(error.get("message", ""))
                raise JobFailed(error.get("repr", "作业失败"), error.get("message", ""))
            if fresh is not None and self.clock() - result.get("finished", 0) > fresh:
                shutil.rmtree(path, ignore_errors=True)
            else:
                if fresh == 0:
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.utime(path / "result.json")
                return decode(result["ok"])
        elif (path / "pid").exists():
            if held(path):
                spec = read(path / "spec.json") or {}
                if self.clock() > spec.get("deadline", float("inf")):
                    self.kill(path)
                    raise Retryable(f"作业超过 {self.deadline} 秒没出结果，已结束：{port}/{name}.{method}")
                raise Pending(path.name, label, item)
            if (path / "result.json").exists():
                # 读结果和查锁之间刚跑完。
                return self.call(port, name, method, args, identity=identity, item=item, lookup=lookup)
            shutil.rmtree(path, ignore_errors=True)
            raise Retryable(f"作业进程没写结果就退出了：{port}/{name}.{method}")
        self.start(path, {"port": port, "name": name, "method": method, "args": encode(list(args)),
                          "item": item, "label": label, "lookup": lookup,
                          "started": self.clock(), "deadline": self.clock() + self.deadline})
        raise Pending(path.name, label, item)

    def start(self, path, spec):
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True)
        write(path / "spec.json", spec)
        lock = os.open(path / "lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (path / "output.log").open("ab") as output:
                # 独立会话：守护进程退出或重启，作业照跑；锁随文件描述交给作业进程。
                child = subprocess.Popen([*self.command, str(path)], stdin=subprocess.DEVNULL, stdout=output,
                                         stderr=subprocess.STDOUT, pass_fds=(lock,), start_new_session=True)
        finally:
            os.close(lock)
        (path / "pid").write_text(str(child.pid))
        self.children[child.pid] = child

    def kill(self, path):
        try:
            os.killpg(int((path / "pid").read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
        shutil.rmtree(path, ignore_errors=True)

    def reap(self):
        """收掉已退出的作业进程（不等），定期清掉一天没人取的结果和没启动成的目录。"""
        for pid, child in list(self.children.items()):
            if child.poll() is not None:
                del self.children[pid]
        now = self.clock()
        if now - self.swept < SWEEP_SECONDS or not self.directory.is_dir():
            return
        self.swept = now
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


def execute(path, plugins, lookup=None):
    """作业进程里跑：按 spec 调插件，把结果或错误原子写进 result.json。"""
    path = Path(path)
    spec = json.loads((path / "spec.json").read_text())
    try:
        plugin = plugins.get(spec["port"], spec["name"])
        args = decode(spec["args"])
        if spec.get("lookup"):
            args.append(lookup(spec["lookup"]))
        result = {"ok": encode(getattr(plugin, spec["method"])(*args))}
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        kind = next((name for name, cls in ERRORS.items() if isinstance(exc, cls)), "error")
        result = {"error": {"kind": kind, "message": str(exc), "repr": repr(exc)}}
    result["finished"] = time.time()
    write(path / "result.json", result)
