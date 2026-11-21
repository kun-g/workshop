"""独立看门：只读核心状态，故障和空闲交给独立项目经理入口。"""
from contextlib import contextmanager, closing
from datetime import datetime, timezone, timedelta
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile

# 等待端口连续几次探活失败才自动重启；重启后仍不通才登记给项目经理。
WAIT_SERVICE_FAILURES = 3
WAIT_SERVICE_TIMEOUT = 2



def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("时间必须是字符串")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间缺时区")
    return parsed


def wait_service_probe(endpoint, timeout=WAIT_SERVICE_TIMEOUT):
    """用等待端口自己的 ping 探活；连不上、超时、拒绝或响应不合协议都算不通。"""
    payload = (json.dumps({"action": "ping", "data": {}}) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(Path(endpoint).expanduser()))
        connection.sendall(payload)
        with connection.makefile("rb") as stream:
            raw = stream.readline(65536)
    if not raw.endswith(b"\n"):
        raise ValueError("等待端口探活没有完整响应")
    value = json.loads(raw)
    result = value.get("result") if isinstance(value, dict) and value.get("ok") is True else None
    if not isinstance(result, dict) or result.get("protocol") != 1:
        raise ValueError("等待端口探活响应无效")
    return result


def launch_definition(endpoint, agents=None):
    """按 endpoint 找安装器写下的 launchd 定义；不猜 label，也不重写 plist（重写会丢 PATH）。"""
    target = str(Path(endpoint).expanduser())
    agents = Path(agents) if agents else Path(os.environ.get("HOME", "~")).expanduser() / "Library/LaunchAgents"
    try:
        paths = sorted(agents.glob("*.plist"))
    except OSError:
        return None
    for path in paths:
        try:
            definition = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        arguments = definition.get("ProgramArguments") if isinstance(definition, dict) else None
        if not isinstance(arguments, list):
            continue
        for index, argument in enumerate(arguments[:-1]):
            if argument == "--endpoint" and str(Path(arguments[index + 1]).expanduser()) == target:
                return definition.get("Label"), path
    return None


def launch_label(endpoint, agents=None):
    definition = launch_definition(endpoint, agents)
    return definition[0] if definition else None


def wait_service_restart(endpoint):
    """重启已加载的任务；没加载就从原 plist 装载。都不重写 plist，PATH 原样保留，送达命令仍找得到 lark-cli。"""
    definition = launch_definition(endpoint)
    if not definition:
        raise RuntimeError("找不到指向等待端口的 launchd 定义")
    label, path = definition
    domain = f"gui/{os.getuid()}"
    result = subprocess.run(["/bin/launchctl", "kickstart", "-k", f"{domain}/{label}"],
                            capture_output=True, text=True, timeout=15)
    if not result.returncode:
        return label
    # kickstart 找不到服务，说明任务没加载；用安装器留下的 plist 装载，不生成第二份定义。
    boot = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(path)],
                          capture_output=True, text=True, timeout=15)
    if boot.returncode:
        error = (result.stderr or result.stdout).strip() + "；" + (boot.stderr or boot.stdout).strip()
        raise RuntimeError(error.strip("；")[:300] or "launchctl 重启失败")
    return label


def save_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class ReleaseRollback:
    """系统 Python 也能运行；只读规则快照，与发版作业共用文件契约。"""
    def __init__(self, directory):
        self.directory = directory

    def configuration(self):
        with closing(sqlite3.connect((self.directory / "workshop.db").as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            row = db.execute("SELECT body FROM controls WHERE id='@rules'").fetchone()
        data = json.loads(row[0])["data"] if row else {}
        if not isinstance(data, dict) or not isinstance(data.get("targets"), dict):
            raise ValueError("规则快照缺少交付对象")
        candidates = []
        for name, target in data.get("targets", {}).items():
            if not isinstance(target, dict) or not isinstance(target.get("flows", {}), dict):
                raise ValueError("规则快照的交付对象或流程无效")
            for flow, local in target.get("flows", {}).items():
                if not isinstance(local, dict):
                    raise ValueError("规则快照的流程配置无效")
                if local.get("plugin", flow) != "scheduled_release":
                    continue
                config = {**target, **local}
                if config.get('scheduled_release_mode', 'self') != 'self':
                    continue
                root = Path(config.get("scheduled_release_state_dir", "~/.local/state/workshop")).expanduser().resolve()
                if root == self.directory:
                    candidates.append((name, config))
        if len(candidates) != 1:
            raise ValueError("规则快照未唯一指定本状态目录的定时发版对象")
        return candidates[0]

    def command(self, argv, *, cwd=None, timeout=30, isolated=False):
        # prepare 不继承部署凭据，也不依赖损坏的项目虚拟环境启动看门。
        with tempfile.TemporaryDirectory(prefix="workshop-rollback-") as home:
            env = None
            if isolated:
                env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
                env.update(HOME=home, GH_CONFIG_DIR=home, GIT_CONFIG_GLOBAL="/dev/null",
                           GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                           AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
                           KUBECONFIG="/dev/null", GIT_CONFIG_COUNT="1",
                           GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0="/dev/null")
            try:
                result = subprocess.run(argv, cwd=cwd, env=env, timeout=timeout,
                                        capture_output=True, text=True, errors="replace")
                return dict(argv=list(argv), exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
            except subprocess.TimeoutExpired as exc:
                def text(value):
                    return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
                return dict(argv=list(argv), exit_code=124, stdout=text(exc.stdout), stderr=text(exc.stderr) + "\n命令超时")
            except OSError as exc:
                return dict(argv=list(argv), exit_code=127, stdout="", stderr=str(exc))

    def attempt(self, record, target, now):
        for field in ("from", "to"):
            if not isinstance(record.get(field), str) or not re.fullmatch("[0-9a-f]{40}", record[field]):
                raise ValueError("发版记录缺少完整 from/to 提交号")
        checkout = Path(target["checkout"]).expanduser().resolve()
        prepare = record.get("prepare")
        if not isinstance(prepare, list) or any(not isinstance(a, list) or not a or
                not all(isinstance(s, str) and s.strip() for s in a) for a in prepare):
            raise ValueError("发版记录 prepare 须为 argv 列表")
        cwds = record.get("prepare_cwds", ["."] * len(prepare))
        if not isinstance(cwds, list) or len(cwds) != len(prepare) or not all(isinstance(s, str) for s in cwds):
            raise ValueError("发版记录 prepare_cwds 与命令不对应")
        def directory(relative):
            cwd = (checkout / relative).resolve()
            if not cwd.is_relative_to(checkout):
                raise ValueError("准备命令目录超出主检出")
            return cwd
        for cwd in cwds:
            directory(cwd)
        timeout = target.get("release_timeout", 300)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("发版超时配置无效")
        git = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(checkout)]
        def read(*args):
            result = self.command(git + list(args))
            if result["exit_code"]:
                raise ValueError("回退预检失败：" + json.dumps(result, ensure_ascii=False))
            return result["stdout"].strip()
        if read("symbolic-ref", "--short", "HEAD") != "main":
            raise ValueError("主检出不在 main")
        if read("status", "--porcelain", "--untracked-files=all"):
            raise ValueError("主检出有未提交改动或未跟踪文件")
        if read("rev-parse", "HEAD") != record["to"]:
            raise ValueError("主检出 HEAD 不等于本次发版 to")
        if read("rev-parse", "--verify", record["from"] + "^{commit}") != record["from"]:
            raise ValueError("发版前的提交不存在")
        read("merge-base", "--is-ancestor", record["from"], record["to"])

        path = self.directory / "release.json"
        record["status"] = "rolling_back"
        record["rollback"] = {"at": now.isoformat(), "runs": []}
        save_json(path, record)  # 外部动作前登记；中断后不猜测动作有没有完成。
        def perform(argv, **kwargs):
            record["rollback"]["pending"] = list(argv)
            save_json(path, record)
            result = self.command(argv, **kwargs)
            record["rollback"]["runs"].append({**result, "cwd": str(kwargs.get("cwd") or checkout)})
            record["rollback"].pop("pending")
            save_json(path, record)
            if result["exit_code"]:
                raise ValueError("回退命令失败，退出 " + str(result["exit_code"]) + "；原样输出见 release.json")
        perform(git + ["reset", "--keep", record["from"]])
        for argv, cwd in zip(prepare, cwds):
            # reset 可能改变目录/符号链接，使用旧版内容再次核对。
            perform(argv, cwd=directory(cwd), timeout=timeout, isolated=True)
        perform(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.kun.workshop"])
        record.update(status="rolled_back", rolled_back_at=now.isoformat())
        save_json(path, record)

    def run(self, now):
        path = self.directory / "release.json"
        if not path.exists():
            return {}
        # 和 scheduled_release 的 IO.locked 使用同一把锁；取锁后才读记录。
        with (self.directory / "scheduled-release.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None  # 此次没有观测，不能把上次的通知去重记录当作已恢复。
            record, anchor = None, "unreadable"
            try:
                name, target = self.configuration()
                raw = path.read_text()
                anchor = hashlib.sha256(raw.encode()).hexdigest()
                record = json.loads(raw)
                if not isinstance(record, dict):
                    raise ValueError("发版记录须为对象")
                anchor = json.dumps([record.get(k) for k in ("item", "from", "to", "at")], ensure_ascii=False)
                if not isinstance(record.get("item"), str) or record["item"].split(":")[0] != name:
                    raise ValueError("发版记录与规则中的交付对象不匹配")
                status = record.get("status")
                if status == "ok":
                    return {}
                if status == "failed":
                    # 交付阶段失败由发版活和项目经理处理；发版记录本身合法，不能报成状态无效。
                    return {}
                if status == "restarting":
                    if (now - timestamp(record["at"])).total_seconds() <= 180:
                        return {}
                    self.attempt(record, target, now)
                elif status == "rolling_back":
                    raise ValueError("上次回退中断，命令结果未确认；不自动重做，请核对 release.json")
                elif status not in ("rolled_back", "rollback_failed"):
                    raise ValueError("发版记录状态无效")
                if record["status"] == "rolled_back":
                    text = "车间发版已退回上一版并请求重启；同一提交不重发，有新合并下个整点再试。详情见 release.json。"
                else:
                    text = "车间发版回退未完成，需要你处理：" + record["rollback"]["reason"]
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                if isinstance(record, dict) and record.get("status") == "rolling_back":
                    record["status"] = "rollback_failed"
                    if not isinstance(record.get("rollback"), dict):
                        record["rollback"] = {"unreadable": record.get("rollback")}
                    record.setdefault("rollback", {})["reason"] = str(exc)
                    save_json(path, record)
                text = "车间发版无法安全退回，需要你处理：" + str(exc)
            key = "release:" + hashlib.sha256(anchor.encode()).hexdigest()[:24]
            return {key: {"anchor": anchor, "text": text, "immediate": True}}


class Watch:
    def __init__(self, state_dir, *, manager_command=None, timeout=30, probe=None, restart=None):
        self.directory = Path(state_dir).expanduser().resolve()
        repository = Path(__file__).resolve().parents[3]
        self.manager_command = manager_command or [str(repository / '.venv/bin/python'), '-m',
            'workshop.manager_cli', '--state-dir', str(self.directory), '--rules', str(repository / 'rules/workshop.yaml')]
        if not isinstance(self.manager_command, list) or not all(isinstance(x, str) and x for x in self.manager_command):
            raise ValueError('manager-command 必须为非空 argv')
        self.timeout, self.changed = timeout, False
        self.scope = hashlib.sha256(str(self.directory).encode()).hexdigest()[:16]
        self.probe, self.restart = probe or wait_service_probe, restart or wait_service_restart

    def faults(self, now):
        faults = {}
        try:
            heartbeat = json.loads((self.directory / "heartbeat.json").read_text())
            age = (now - timestamp(heartbeat["last_ok_at"])).total_seconds()
            process_dead = False
            pid = heartbeat.get('process', {}).get('pid')
            if type(pid) is int and pid > 0:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    process_dead = True
            if process_dead or age > 180 or age < -60:
                faults["heartbeat"] = {"anchor": heartbeat["last_ok_at"],
                                       "text": "车间 heartbeat.json 已超过 3 分钟没更新或时间异常，需要你检查守护进程。"}
            if (type(heartbeat.get("delivery_failed_count")) is int and heartbeat["delivery_failed_count"] > 0
                    and heartbeat.get("delivery_failed_since")
                    and (now - timestamp(heartbeat["delivery_failed_since"])).total_seconds() > 180):
                faults["delivery"] = {"anchor": heartbeat["delivery_failed_since"],
                    "text": "车间 heartbeat.json 记录通道持续超过三分钟发不出去，需要你检查通道，详见 watch.json。"}
        except (OSError, ValueError, KeyError, TypeError):
            faults["heartbeat"] = {"anchor": "unreadable", "text": "车间 heartbeat.json 缺失或无法读取，需要你检查守护进程。"}
        try:
            with closing(sqlite3.connect((self.directory / "workshop.db").as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                duration = 7200  # 核心缺省规则；有快照时取快照，优先使用轮自身期限。
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='controls'").fetchone():
                    saved = db.execute("SELECT body FROM controls WHERE id='@rules'").fetchone()
                    if saved:
                        try:
                            duration = json.loads(saved[0])["data"]["limits"].get("round_timeout_seconds", 7200)
                            if type(duration) is not int or duration <= 0:
                                raise ValueError("轮次期限无效")
                        except (ValueError, KeyError, TypeError):
                            duration = 7200
                            print("车间看门：规则快照损坏，旧轮使用核心缺省期限", file=sys.stderr)
                for rid, body in db.execute("SELECT id, body FROM rounds"):
                    try:
                        row = json.loads(body)
                        if row["status"] not in ("starting", "running", "finishing"):
                            continue
                        deadline = timestamp(row["deadline"]) if row.get("deadline") else timestamp(row["at"]) + timedelta(seconds=duration)
                        if now > deadline + timedelta(seconds=60):
                            faults[f"round:{rid}"] = {"anchor": deadline.isoformat(),
                                "text": f"车间 workshop.db 中一轮 {rid} 超过期限和一分钟结算余量仍未结算，需要你检查执行者和结算日志，详见 watch.json。"}
                    except (ValueError, KeyError, TypeError):
                        print(f"车间看门：跳过损坏的轮次记录 {rid}", file=sys.stderr)
        except sqlite3.Error:
            faults["database"] = {"anchor": "unreadable", "text": "车间 workshop.db 无法读取，请看 heartbeat.json 和守护进程日志，需要你检查状态目录。"}
        return faults

    def services(self):
        """规则快照里的等待端口；读不到快照就不探活，不从别处补第二份端点。"""
        try:
            with closing(sqlite3.connect((self.directory / "workshop.db").as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='controls'").fetchone():
                    return {}
                saved = db.execute("SELECT body FROM controls WHERE id='@rules'").fetchone()
                data = json.loads(saved[0]).get("data") if saved else {}
                services = data.get("wait_services", {}) if isinstance(data, dict) else {}
                return {name: config["endpoint"] for name, config in services.items()
                        if isinstance(config, dict) and isinstance(config.get("endpoint"), str)
                        and config["endpoint"].strip()}
        except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError):
            return {}

    def service_faults(self, now, state):
        """连续探活失败到阈值就重启一次；重启后仍不通才交项目经理。

        失败次数、错误和重启结果留在 watch.json；交给项目经理的事项只有一条、
        anchor 取这段失败开始的时间，反复观测不会变成一串新事项。
        """
        records = state.setdefault("wait_services", {})
        faults = {}
        for name, endpoint in self.services().items():
            record = records.setdefault(name, {})
            record["endpoint"] = endpoint
            try:
                self.probe(endpoint)
            except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
                record["failures"] = record.get("failures", 0) + 1
                record.setdefault("since", now.isoformat())
                record["last_failure_at"] = now.isoformat()
                record["last_error"] = str(exc)[:300]
                if record["failures"] < WAIT_SERVICE_FAILURES:
                    continue
                if "restarted_at" not in record:
                    record["restarted_at"] = now.isoformat()
                    try:
                        record["restart_label"] = self.restart(endpoint)
                        record.pop("restart_error", None)
                    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as restart_error:
                        record["restart_error"] = str(restart_error)[:300]
                    continue  # 刚发出重启：下一拍再探，给进程重新监听的时间。
                faults["service:" + name] = {
                    "anchor": record["since"],
                    "text": f"等待端口 {name} 连续探活失败，自动重启没有恢复，需要你检查服务。"}
            else:
                if record.get("failures"):
                    record["failures"] = 0
                    record["recovered_at"] = now.isoformat()
                    record.pop("since", None)
                record.pop("restarted_at", None)
                record.pop("restart_error", None)
        return faults

    def tick_fault(self, state, now, awake):
        """连续三次观测到不同的慢心跳才告警；同一心跳重读不累计。"""
        sample = state.get("tick_observation", {}) if awake else {}
        try:
            heartbeat = json.loads((self.directory / "heartbeat.json").read_text())
            at, elapsed = heartbeat["at"], heartbeat["tick_ms"]
            age = (now - timestamp(at)).total_seconds()
            # 本次开始后才写下的心跳（扫故障期间守护进程又写了几拍）也是新鲜的；只拒绝明显来自未来的时间。
            if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not -5 <= age <= 180:
                raise ValueError("心跳耗时或时间无效")
            if sample.get("at") != at:
                # 时间倒退也不能接着上一次累计。
                count = sample.get("count", 0) if not sample.get("at") or timestamp(at) > timestamp(sample["at"]) else 0
                sample = {"at": at, "count": count + 1 if elapsed > 100 else 0,
                          "since": sample.get("since", at) if count and elapsed > 100 else at}
            state["tick_observation"] = sample
            if sample.get("count", 0) >= 3:
                return {"anchor": sample["since"],
                        "text": "车间 heartbeat.json 连续三次观测到不同心跳的 tick_ms 超过 100 毫秒，需要你检查主循环，详见 watch.json。"}
        except (OSError, ValueError, KeyError, TypeError):
            state.pop("tick_observation", None)
        return None

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "watch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _save(self, state):
        save_json(self.directory / "watch.json", state)

    def delegate(self, state, now):
        try:
            heartbeat = json.loads((self.directory / 'heartbeat.json').read_text())
            business = heartbeat.get('business', {})
        except (OSError, ValueError):
            business = {}
        # 核心失效时仍保留最后可信快照；不把未知当空闲重新发放额度。
        payload = {'at': now.isoformat(), 'faults': state['active'], 'business': business,
                   'takeover': 'heartbeat' in state['active'] or 'database' in state['active']}
        path = self.directory / 'manager-input.json'
        save_json(path, payload)
        result = subprocess.run([*self.manager_command, 'tick', '--file', str(path)],
                                capture_output=True, text=True, timeout=self.timeout)
        if result.returncode:
            raise RuntimeError('项目经理入口失败：' + result.stderr[:500])
        state['manager_result'] = result.stdout[-2000:]
        state.pop('manager_error', None)

    @staticmethod
    def summary(state):
        return {key: {name: incident.get(name) for name in ("id", "confirmed")}
                for key, incident in state["active"].items()}

    def run(self, now=None):
        now = now or datetime.now(timezone.utc)
        with self._locked():
            path = self.directory / "watch.json"
            state = json.loads(path.read_text()) if path.exists() else {"sequence": 0, "active": {}}
            before = self.summary(state)
            previous = state.get("last_run")
            awake = previous is not None and 0 <= (now - timestamp(previous)).total_seconds() <= 90
            state["last_run"] = now.isoformat()
            faults = self.faults(now)
            faults.update(self.service_faults(now, state))
            release_faults = ReleaseRollback(self.directory).run(now)
            faults.update(release_faults if release_faults is not None else
                          {k: v for k, v in state["active"].items() if k.startswith("release:")})
            tick_fault = self.tick_fault(state, now, awake)
            if tick_fault:
                faults["tick"] = tick_fault
            active = state["active"]
            for key in list(active):
                if key not in faults:
                    del active[key]
            for key, fault in faults.items():
                if key not in active or active[key]["anchor"] != fault["anchor"]:
                    state["sequence"] += 1
                    fingerprint = hashlib.sha256(f"{self.directory}:{key}:{state['sequence']}:{now.isoformat()}".encode()).hexdigest()[:24]
                    active[key] = {**fault, "id": f"workshop-watch:{self.scope}:{now.date()}:{fingerprint}",
                                   "since": now.isoformat(),
                                   "confirmed": False, "reported": False}
                active[key]["confirmed"] = awake or fault.get("immediate", False)
            # 同一次故障跨日期不重复；确认恢复后再次故障会分配新 id。
            self._save(state)
            try:
                self.delegate(state, now)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                state['manager_error'] = str(exc)
                self._save(state)
                raise
            self._save(state)
            self.changed = before != self.summary(state)
            return state


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="独立检查车间成功心跳和未结算轮")
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("WORKSHOP_STATE_DIR", "~/.local/state/workshop")))
    parser.add_argument("--manager-command", type=json.loads)
    args = parser.parse_args(argv)
    watcher = Watch(args.state_dir, manager_command=args.manager_command)
    try:
        state = watcher.run()
    except Exception as exc:
        print(f"车间看门失败：{exc}", file=sys.stderr)
        return 1
    if watcher.changed:
        print(json.dumps(watcher.summary(state), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
