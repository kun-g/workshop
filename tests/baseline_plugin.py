"""录引擎行为：事实行、同一次提交、投影、心跳、重启标记。

只在 WORKSHOP_RECORD_BASELINE=1 时由 conftest 装上。包装对没注入
conftest.Clock 的引擎是直通。生产代码不改。
"""
from contextlib import contextmanager
import contextvars
from datetime import datetime, timedelta, timezone
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from workshop.core import engine as engine_module
from workshop.core import log as log_module
from workshop.core import models as models_module
from workshop.core.engine import Engine
from workshop.core.log import Log

_current = contextvars.ContextVar("engine_baseline_trace", default=None)
_installed = False
_orig_log_init = Log.__init__
_orig_append = Log.append
_orig_transaction = Log.transaction
_orig_close = Log.close
_orig_init = Engine.__init__
_orig_after = Engine.after
_orig_heartbeat = Engine.heartbeat

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_HEX64 = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_HEX40 = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
_HEX32_UUID = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}(?![0-9a-f])",
    re.IGNORECASE,
)
_HEX24 = re.compile(r"(?<![0-9a-f])[0-9a-f]{24}(?![0-9a-f])")
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2}:\d{2}")
# macOS 上 /tmp 是 /private/tmp。两条都要整段换成 $tmpdir，不能只换掉后半留下 /private。
_WAKE_TMP = re.compile(r"(?:/private)?/tmp/ws-wake-[A-Za-z0-9_]+")
_CLOCK_ORIGIN = datetime(2026, 11, 13, 8, tzinfo=timezone.utc)
# 这一次测试里，conftest.Clock 真正返回过的时刻，以及从这些时刻加减得到的时刻。
# 只把这些写成偏移。恰好落在整秒上的墙上时钟不在这里，仍编成 $time:N。
_clock_instants = set()
_orig_stamp = models_module.stamp
_orig_clock_call = None
_HEX_CHARS = set("0123456789abcdef")
_TAIL_CHARS = _HEX_CHARS | {"-"}
_TABLES = ("items", "cards", "rounds", "controls")


_child_trace = None


def current_trace():
    trace = _current.get()
    if trace is not None:
        return trace
    return _child_trace


def enable_child_trace():
    """命令行子进程不走 pytest。父进程把已绑定的库登记在磁盘上，子进程只记录这些库。"""
    global _child_trace
    if _child_trace is None:
        _child_trace = ChildTrace()


def is_conftest_clock(clock):
    kind = type(clock)
    return kind.__name__ == "Clock" and kind.__module__ in ("conftest", "tests.conftest")


class _ClockInstant(datetime):
    """从 conftest.Clock 来的时刻。加减 timedelta 的结果仍算这个时钟给出的。"""

    def __add__(self, other):
        return _keep_instant(datetime.__add__(self, other))

    def __sub__(self, other):
        return _keep_instant(datetime.__sub__(self, other))


def _as_instant(value):
    if isinstance(value, _ClockInstant):
        _clock_instants.add(_orig_stamp(value))
        return value
    if not isinstance(value, datetime):
        return value
    noted = _ClockInstant(
        value.year, value.month, value.day,
        value.hour, value.minute, value.second, value.microsecond,
        tzinfo=value.tzinfo, fold=value.fold,
    )
    _clock_instants.add(_orig_stamp(noted))
    return noted


def _keep_instant(result):
    if not isinstance(result, datetime):
        return result
    if isinstance(result, _ClockInstant):
        _clock_instants.add(_orig_stamp(result))
        return result
    return _as_instant(result)


def _note_instant(value):
    return _as_instant(value)


def _recording_stamp(value):
    result = _orig_stamp(value)
    if isinstance(value, _ClockInstant):
        _clock_instants.add(result)
    return result


def _clock_call(self):
    return _note_instant(_orig_clock_call(self))


def _patch_clock():
    global _orig_clock_call
    if _orig_clock_call is not None:
        return
    for module in list(sys.modules.values()):
        kind = getattr(module, "Clock", None)
        if not isinstance(kind, type) or kind.__name__ != "Clock":
            continue
        if kind.__module__ not in ("conftest", "tests.conftest"):
            continue
        _orig_clock_call = kind.__call__
        kind.__call__ = _clock_call
        return


class Trace:
    def __init__(self, nodeid):
        self.nodeid = nodeid
        self.roots = {}
        self.bound_paths = set()
        self.logs = []
        self.databases = {}
        self.engine_ids = {}
        self.uuids = {}
        self.shas = {}

    def add_root(self, path, label):
        for candidate in _path_aliases(str(path)):
            if candidate not in self.roots:
                self.roots[candidate] = label

    def record_for(self, log):
        key = str(Path(log.path).resolve())
        if key not in self.databases:
            self.databases[key] = {
                "path": key,
                "commits": [],
                "heartbeats": [],
                "events": [],
                "projection": None,
            }
        return self.databases[key]

    def bind(self, log):
        key = str(Path(log.path).resolve())
        self.bound_paths.add(key)
        log._baseline_bound = True
        if log not in self.logs:
            self.logs.append(log)
        self.record_for(log)
        publish_bound(key)

    def engine_no(self, engine):
        key = id(engine)
        if key not in self.engine_ids:
            self.engine_ids[key] = len(self.engine_ids) + 1
        return self.engine_ids[key]

    def add_commit(self, log, batch):
        if batch:
            anchor = log._baseline_anchor if log._baseline_anchor is not None else 0
            self.record_for(log)["commits"].append((anchor, list(batch)))

    def note_init(self, engine):
        record = self.record_for(engine.log)
        event = {"engine": self.engine_no(engine), "phase": "init", "memory": bool(engine.restart_requested)}
        if any(item.get("memory") for item in record["events"]):
            facts, heartbeat, projection = scan_restart_key(engine.log)
            event.update(in_facts=facts, in_heartbeat=heartbeat, in_projection=projection)
        record["events"].append(event)

    def note_restart(self, engine):
        facts, heartbeat, projection = scan_restart_key(engine.log)
        self.record_for(engine.log)["events"].append({
            "engine": self.engine_no(engine),
            "phase": "after",
            "memory": True,
            "in_facts": facts,
            "in_heartbeat": heartbeat,
            "in_projection": projection,
        })

    def add_heartbeat(self, engine, payload):
        anchor = engine.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0] or 0
        self.record_for(engine.log)["heartbeats"].append((anchor, payload))

    def snapshot(self, log):
        self.record_for(log)["projection"] = {
            "items": log.all("items"),
            "cards": log.all("cards"),
            "rounds": log.all("rounds"),
            "controls": log.all("controls"),
        }

    def finalize_open(self):
        for log in self.logs:
            try:
                log.db.execute("SELECT 1")
            except Exception:
                continue
            self.snapshot(log)

    def document(self):
        self.finalize_open()
        self.reread()
        incoming = child_records()
        databases = []
        for record in self.databases.values():
            extra = incoming.get(record["path"], [])
            commits = merge_commits(record["commits"], extra)
            heartbeats = merge_heartbeats(record["heartbeats"], extra)
            events = list(record["events"])
            events.extend(item["event"] for item in extra if item.get("kind") == "restart")
            seen = any(item.get("memory") for item in events)
            restart = {"seen": False}
            if seen:
                restart = {"seen": True, "trace": events}
            databases.append({
                "path": record["path"],
                "commits": commits,
                "projection": record["projection"],
                "heartbeats": heartbeats,
                "restart": restart,
            })
        return canonicalize(
            {"nodeid": self.nodeid, "databases": databases}, self.roots, set(_clock_instants))

    def reread(self):
        """子进程或线程可能在已关闭的连接之后又写了事实。结束时按文件重读投影。"""
        for record in list(self.databases.values()):
            path = Path(record["path"])
            if not path.exists():
                continue
            try:
                log = Log(path)
            except Exception:
                continue
            try:
                self.snapshot(log)
            finally:
                log.close()


class ChildTrace:
    """子进程里的记录器。提交立刻落盘，父进程结束测试时按事实序号并进去。"""

    def __init__(self):
        self.bound_paths = set()
        self._handle = None

    def refresh(self):
        directory = os.environ.get("WORKSHOP_BASELINE_BOUND")
        if not directory:
            return
        folder = Path(directory)
        if not folder.exists():
            return
        for marker in folder.glob("*.path"):
            self.bound_paths.add(marker.read_text().strip())

    def bind(self, log):
        log._baseline_bound = True

    def add_commit(self, log, batch):
        if not batch:
            return
        anchor = log._baseline_anchor if log._baseline_anchor is not None else 0
        self._write({"path": str(Path(log.path).resolve()), "kind": "commit", "anchor": anchor, "rows": batch})

    def note_init(self, engine):
        return None

    def snapshot(self, log):
        return None

    def note_restart(self, engine):
        facts, heartbeat, projection = scan_restart_key(engine.log)
        self._write({
            "path": str(Path(engine.log.path).resolve()),
            "kind": "restart",
            "event": {
                "engine": "cli",
                "phase": "after",
                "memory": True,
                "in_facts": facts,
                "in_heartbeat": heartbeat,
                "in_projection": projection,
            },
        })

    def add_heartbeat(self, engine, payload):
        anchor = engine.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0] or 0
        self._write({
            "path": str(Path(engine.log.path).resolve()),
            "kind": "heartbeat",
            "anchor": anchor,
            "payload": payload,
        })

    def _write(self, payload):
        import fcntl
        if self._handle is None:
            directory = Path(os.environ["WORKSHOP_BASELINE_PARTS"]) / "children"
            directory.mkdir(parents=True, exist_ok=True)
            name = f"{os.getpid()}-{hashlib.sha256(os.urandom(8)).hexdigest()}.jsonl"
            self._handle = (directory / name).open("a", encoding="utf-8")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        try:
            self._handle.write(line)
            self._handle.flush()
        finally:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)


def publish_bound(path):
    directory = os.environ.get("WORKSHOP_BASELINE_BOUND")
    if not directory:
        return
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (hashlib.sha256(path.encode()).hexdigest() + ".path")
    if target.exists():
        return
    temporary = target.with_suffix(".tmp")
    temporary.write_text(path)
    temporary.replace(target)


def child_records():
    raw = os.environ.get("WORKSHOP_BASELINE_PARTS")
    grouped = {}
    if not raw:
        return grouped
    directory = Path(raw) / "children"
    if not directory.exists():
        return grouped
    for file in sorted(directory.glob("*.jsonl")):
        for line in file.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            grouped.setdefault(item["path"], []).append(item)
    return grouped


def _batch_key(anchor, rows):
    stored = [row["seq"] for row in rows if row.get("stored") and "seq" in row]
    return min(stored) if stored else anchor + 0.5


def merge_commits(parent, extra):
    batches = list(parent)
    batches.extend((item["anchor"], item["rows"]) for item in extra if item.get("kind") == "commit")
    batches.sort(key=lambda item: _batch_key(item[0], item[1]))
    published = []
    for _, rows in batches:
        published.append([{key: value for key, value in row.items() if key != "seq"} for row in rows])
    return published


def _elapsed(payload):
    edited = dict(payload)
    for key in ("tick_ms", "wake_ms"):
        if isinstance(edited.get(key), (int, float)):
            edited[key] = "$elapsed"
    return edited


def merge_heartbeats(parent, extra):
    if not any(item.get("kind") == "heartbeat" for item in extra):
        return [_elapsed(payload) for _, payload in parent]
    combined = list(parent)
    combined.extend((item["anchor"], item["payload"]) for item in extra if item.get("kind") == "heartbeat")
    combined.sort(key=lambda item: item[0])
    return [_elapsed(payload) for _, payload in combined]


def scan_restart_key(log):
    def contains(query):
        return any("restart_requested" in body for (body,) in log.db.execute(query))

    facts = contains("SELECT body FROM facts")
    projection = any(contains(f"SELECT body FROM {table}") for table in _TABLES)
    heartbeat_path = log.path.parent / "heartbeat.json"
    try:
        heartbeat = "restart_requested" in heartbeat_path.read_text()
    except FileNotFoundError:
        heartbeat = False
    return facts, heartbeat, projection


def _walk(value, visit):
    if isinstance(value, str):
        return visit(value)
    if isinstance(value, list):
        return [_walk(item, visit) for item in value]
    if isinstance(value, dict):
        return {_walk(key, visit) if isinstance(key, str) else key: _walk(value[key], visit)
                for key in sorted(value, key=lambda item: item if isinstance(item, str) else str(item))}
    return value


def _path_aliases(text):
    """同一目录在 Linux 写成 /tmp，在 macOS 上 resolve 之后是 /private/tmp。两种都登记。"""
    aliases = {text}
    try:
        aliases.add(str(Path(text).resolve()))
    except OSError:
        pass
    extra = set()
    for item in aliases:
        if item.startswith("/private/tmp") or item.startswith("/private/var"):
            extra.add(item.removeprefix("/private"))
        elif item.startswith("/tmp") or item.startswith("/var"):
            extra.add("/private" + item)
    aliases.update(extra)
    return {item for item in aliases if item}


def _format_offset(delta):
    sign = "-" if delta < timedelta(0) else "+"
    delta = abs(delta)
    micros = delta.microseconds
    total = int(delta.total_seconds())
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or micros or not parts:
        if micros:
            parts.append(f"{seconds}.{micros:06d}".rstrip("0") + "s")
        else:
            parts.append(f"{seconds}s")
    return sign + "".join(parts)


def _clock_offset(raw):
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    moment = moment.astimezone(timezone.utc)
    return _format_offset(moment - _CLOCK_ORIGIN)


def _rewrite_clock(text, clock):
    if not clock:
        return text

    def replace(match):
        raw = match.group(0)
        if raw not in clock:
            return raw
        return _clock_offset(raw) or raw

    return _ISO.sub(replace, text)


def _replace_roots(text, roots):
    for root, label in sorted(roots.items(), key=lambda item: len(item[0]), reverse=True):
        if root and root in text:
            text = text.replace(root, label)
    executable = sys.executable
    if executable and executable in text:
        text = text.replace(executable, "$python")
    for directory in _WAKE_TMP.findall(text):
        text = text.replace(directory, "$tmpdir")
    return text


def _collect(value, found):
    if isinstance(value, str):
        found["iso"] += _ISO.findall(value)
        found["uuid"] += [item.lower() for item in _UUID.findall(value)]
        found["uuid"] += [item.lower() for item in _HEX32_UUID.findall(value)]
        found["hex64"] += [item.lower() for item in _HEX64.findall(value)]
        found["hex40"] += [item.lower() for item in _HEX40.findall(value)]
        found["hex24"] += [item.lower() for item in _HEX24.findall(value)]
    elif isinstance(value, list):
        for item in value:
            _collect(item, found)
    elif isinstance(value, dict):
        for key in sorted(value, key=lambda item: item if isinstance(item, str) else str(item)):
            if isinstance(key, str):
                _collect(key, found)
            _collect(value[key], found)


def _assign(values):
    tokens = {}
    ordered = []
    for raw in values:
        if raw not in tokens:
            tokens[raw] = None
            ordered.append(raw)
    return ordered


class _TokenNode:
    __slots__ = ("kids", "token", "prefix")

    def __init__(self):
        self.kids = {}
        self.token = None
        self.prefix = None


def _insert_token(root, raw, token, allow_prefix):
    node = root
    for char in raw.lower():
        child = node.kids.get(char)
        if child is None:
            child = _TokenNode()
            node.kids[char] = child
        node = child
        if not allow_prefix:
            continue
        if node.prefix is None:
            node.prefix = token
        elif node.prefix != token:
            node.prefix = False
    node.token = token


def _apply_tokens(text, root):
    """从左到右走一遍。完整记号优先；截断前缀只在后面不再是十六进制或横线时换成同一个记号。"""
    if root is None or not text:
        return text
    lowered = text.lower()
    pieces = []
    index = 0
    length = len(text)
    while index < length:
        before = lowered[index - 1] if index else ""
        if before in _HEX_CHARS:
            pieces.append(text[index])
            index += 1
            continue
        node = root
        cursor = index
        best = None
        while cursor < length:
            child = node.kids.get(lowered[cursor])
            if child is None:
                break
            node = child
            cursor += 1
            nxt = lowered[cursor] if cursor < length else ""
            if nxt in _TAIL_CHARS:
                continue
            if node.token is not None:
                best = (cursor, node.token)
            elif node.prefix and cursor - index >= 12:
                best = (cursor, node.prefix)
        if best is None:
            pieces.append(text[index])
            index += 1
            continue
        pieces.append(best[1])
        index = best[0]
    return "".join(pieces)


def canonicalize(value, roots, clock=()):
    clock = set(clock)
    rooted = _walk(value, lambda text: _rewrite_clock(_replace_roots(text, roots), clock))
    found = {"iso": [], "uuid": [], "hex64": [], "hex40": [], "hex24": []}
    _collect(rooted, found)
    numbered = {}

    def take(raw, prefix):
        if raw not in numbered:
            numbered[raw] = f"{prefix}:{sum(token.startswith(prefix + ':') for token in numbered.values()) + 1}"
        return numbered[raw]

    root = _TokenNode()
    inserted = False
    for raw in _assign(found["uuid"]):
        _insert_token(root, raw, take(raw, "$uuid"), True)
        inserted = True
    for raw in _assign(found["hex64"]):
        _insert_token(root, raw, take(raw, "$hash"), True)
        inserted = True
    for raw in _assign(found["hex40"]):
        _insert_token(root, raw, take(raw, "$sha"), True)
        inserted = True
    for raw in _assign(found["hex24"]):
        _insert_token(root, raw, take(raw, "$id"), True)
        inserted = True
    for raw in _assign(found["iso"]):
        _insert_token(root, raw, take(raw, "$time"), False)
        inserted = True
    if not inserted:
        return rooted
    return _walk(rooted, lambda text: _apply_tokens(text, root))


def log_init(self, path):
    _orig_log_init(self, path)
    trace = current_trace()
    if trace is None:
        return
    refresh = getattr(trace, "refresh", None)
    if refresh is not None:
        refresh()
    if str(Path(path).resolve()) in trace.bound_paths:
        trace.bind(self)


def append(self, kind, body, item=None, *, source="core", ext_id=None, at=None):
    trace = current_trace()
    if trace is None or not getattr(self, "_baseline_bound", False):
        return _orig_append(self, kind, body, item, source=source, ext_id=ext_id, at=at)
    generated = ext_id is None
    snapshot = json.loads(json.dumps(body, ensure_ascii=False, allow_nan=False))
    with self.transaction():
        before = self.db.execute("SELECT max(seq) FROM facts").fetchone()[0] or 0
        stored = _orig_append(self, kind, body, item, source=source, ext_id=ext_id, at=at)
        entry = {
            "kind": kind,
            "source": source,
            "ext_id": ext_id,
            "ext_id_origin": "uuid" if generated else "literal",
            "item": item,
            "body": snapshot,
            "at": at,
            "stored": bool(stored),
        }
        if self._baseline_anchor is None:
            self._baseline_anchor = before
        if stored:
            entry["seq"] = before + 1
            row = self.db.execute(
                "SELECT source, ext_id, item, at FROM facts WHERE seq=?", (before + 1,)
            ).fetchone()
            if row is None:
                raise RuntimeError("基线记录找不到刚写入的事实")
            entry["source"] = row["source"]
            entry["ext_id"] = row["ext_id"]
            entry["item"] = row["item"]
            entry["at"] = row["at"]
        self._baseline_batch.append(entry)
        return stored


@contextmanager
def _recording_transaction(self):
    outer = self.depth == 0
    batch = None
    if outer:
        batch = []
        self._baseline_batch = batch
        self._baseline_anchor = None
    try:
        with _orig_transaction(self):
            yield
        if outer and batch:
            current_trace().add_commit(self, batch)
    finally:
        if outer:
            self._baseline_batch = None
            self._baseline_anchor = None


def transaction(self):
    trace = current_trace()
    if trace is None or not getattr(self, "_baseline_bound", False):
        return _orig_transaction(self)
    return _recording_transaction(self)


def close(self):
    trace = current_trace()
    if trace is not None and getattr(self, "_baseline_bound", False):
        try:
            self.db.execute("SELECT 1")
        except Exception:
            pass
        else:
            trace.snapshot(self)
    return _orig_close(self)


@functools.wraps(_orig_init)
def engine_init(self, *args, **kwargs):
    if "clock" in kwargs:
        clock = kwargs["clock"]
    elif len(args) >= 4:
        clock = args[3]
    else:
        clock = None
    log = kwargs["log"] if "log" in kwargs else args[0]
    trace = current_trace()
    record = trace is not None and is_conftest_clock(clock)
    if record:
        trace.bind(log)
    _orig_init(self, *args, **kwargs)
    if record:
        trace.note_init(self)


def after(self, *args, **kwargs):
    before = bool(getattr(self, "restart_requested", False))
    try:
        return _orig_after(self, *args, **kwargs)
    finally:
        trace = current_trace()
        if (trace is not None and getattr(getattr(self, "log", None), "_baseline_bound", False)
                and self.restart_requested and not before):
            trace.note_restart(self)


def heartbeat(self, began=None):
    _orig_heartbeat(self, began)
    trace = current_trace()
    if trace is None or not getattr(self.log, "_baseline_bound", False):
        return
    path = self.log.path.parent / "heartbeat.json"
    if path.exists():
        trace.add_heartbeat(self, json.loads(path.read_text()))


def _mark_child_process(args):
    """xdist 工人是 python -c，不能标成要录的子进程。命令行和其它解释器要标。"""
    if isinstance(args, (list, tuple)):
        texts = [str(item) for item in args]
        if "-c" in texts and "-m" not in texts:
            return False
    return True


def _popen_init(self, args, *rest, **kwargs):
    if _mark_child_process(args):
        env = kwargs.get("env")
        env = os.environ.copy() if env is None else dict(env)
        env["WORKSHOP_BASELINE_CHILD"] = "1"
        kwargs["env"] = env
    return _orig_popen_init(self, args, *rest, **kwargs)


_orig_popen_init = subprocess.Popen.__init__


def install():
    global _installed
    if _installed:
        return
    Log.__init__ = log_init
    Log.append = append
    Log.transaction = transaction
    Log.close = close
    Engine.__init__ = engine_init
    Engine.after = after
    Engine.heartbeat = heartbeat
    models_module.stamp = _recording_stamp
    engine_module.stamp = _recording_stamp
    log_module.stamp = _recording_stamp
    _patch_clock()
    subprocess.Popen.__init__ = _popen_init
    # 线程里不一定带得走 pytest 的上下文。同一进程里打开已绑定的库，仍把提交记下。
    enable_child_trace()
    _installed = True


def pytest_configure(config):
    install()


@pytest.fixture(autouse=True)
def _engine_baseline_roots(request):
    trace = current_trace()
    if trace is not None:
        trace.add_root(request.config._tmp_path_factory.getbasetemp(), "$pytest")
        trace.add_root(Path(__file__).resolve().parents[1], "$repo")
        # pytest 给同名临时目录加序号。序号取决于这个工人前面跑过几个同名测试，两次录制会对不齐。
        if "tmp_path" in request.fixturenames:
            trace.add_root(request.getfixturevalue("tmp_path"), "$testtmp")
    yield


def note_nodeid(nodeid):
    raw = os.environ.get("WORKSHOP_BASELINE_PARTS")
    if not raw:
        return
    folder = Path(raw) / "nodeids"
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(nodeid.encode()).hexdigest() + ".txt"
    (folder / name).write_text(nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    _clock_instants.clear()
    token = _current.set(Trace(item.nodeid))
    try:
        note_nodeid(item.nodeid)
        yield
    finally:
        try:
            trace = current_trace()
            if trace is not None and trace.databases:
                write_part(trace.document())
        finally:
            _current.reset(token)


def write_part(document):
    directory = Path(os.environ.get("WORKSHOP_BASELINE_PARTS", "/tmp/workshop-baseline-parts"))
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(document["nodeid"].encode()).hexdigest() + ".json"
    target = directory / name
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    temporary.replace(target)
