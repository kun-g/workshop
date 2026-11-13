"""独立看门：只读核心状态，通知失败时登记等你清单。"""
from contextlib import contextmanager, closing
from datetime import datetime, timezone, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

DEFAULT_ASK = Path.home() / "workspace/runtime/worktrees/oh-my-assistant/workbench/workbench/agent-bin/ask-kun"
DEFAULT_ATTENTION = Path.home() / "workspace/runtime/tools/stuck_watch/attention.py"


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("时间必须是字符串")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间缺时区")
    return parsed


class Watch:
    def __init__(self, state_dir, *, ask_kun=None, attention=None, attention_directory=None, python=None, timeout=30):
        self.directory = Path(state_dir).expanduser().resolve()
        command = str(ask_kun or os.environ.get("WORKSHOP_ASK_KUN", DEFAULT_ASK))
        self.ask_kun = str(Path(shutil.which(command) or command).expanduser().absolute())
        self.attention = str(Path(attention or DEFAULT_ATTENTION).expanduser().resolve())
        self.python = str(Path(python or sys.executable).resolve())
        self.timeout = timeout
        self.attention_directory = attention_directory
        self.changed = False
        self.scope = hashlib.sha256(str(self.directory).encode()).hexdigest()[:16]

    def faults(self, now):
        faults = {}
        try:
            heartbeat = json.loads((self.directory / "heartbeat.json").read_text())
            age = (now - timestamp(heartbeat["last_ok_at"])).total_seconds()
            if age > 180 or age < -60:
                faults["heartbeat"] = {"anchor": heartbeat["last_ok_at"],
                                       "text": "车间 heartbeat.json 已超过 3 分钟没更新或时间异常，需要你检查守护进程。"}
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
                                "text": f"车间 workshop.db 中一轮 {rid} 超过期限和一分钟结算余量仍未结算，需要你检查执行者和结算日志。"}
                    except (ValueError, KeyError, TypeError):
                        print(f"车间看门：跳过损坏的轮次记录 {rid}", file=sys.stderr)
        except sqlite3.Error:
            faults["database"] = {"anchor": "unreadable", "text": "车间 workshop.db 无法读取，请看 heartbeat.json 和守护进程日志，需要你检查状态目录。"}
        return faults

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "watch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _save(self, state):
        fd, name = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.directory / "watch.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _send(self, incident):
        try:
            result = subprocess.run([self.ask_kun, "notify", "--request-id", incident["id"],
                                     "--from", "车间", "-"], input=incident["text"],
                                    capture_output=True, text=True, timeout=self.timeout)
            if result.returncode == 0:
                return "ask-kun"
        except (OSError, subprocess.TimeoutExpired):
            pass
        if incident.get("attention_written"):
            return "attention"
        row = {"来源会话": incident["id"], "从几点起": incident["since"], "种类": "等你",
               "要什么": incident["text"], "推荐": "检查守护进程和通道日志，修复后由看门确认恢复",
               "去哪看": str(self.directory), "状态": "待处理", "推飞书": True, "持续告警": True}
        args = [self.python, self.attention, "--writer", "车间", "--row", json.dumps(row, ensure_ascii=False)]
        if self.attention_directory:
            args.extend(["--directory", str(self.attention_directory)])
        result = subprocess.run(args, capture_output=True, text=True, timeout=self.timeout)
        if result.returncode:
            raise RuntimeError(f"看门通知和等你清单均失败：{result.stderr[:500]}")
        incident["attention_written"] = True
        return "attention"

    @staticmethod
    def summary(state):
        return {key: {name: incident.get(name) for name in ("id", "confirmed", "reported", "via")}
                for key, incident in state["active"].items()}

    def run(self, now=None):
        now = now or datetime.now(timezone.utc)
        with self._locked():
            path = self.directory / "watch.json"
            state = json.loads(path.read_text()) if path.exists() else {"sequence": 0, "active": {}}
            before = self.summary(state)
            faults = self.faults(now)
            active = state["active"]
            for key in list(active):
                if key not in faults:
                    del active[key]
            for key, fault in faults.items():
                if key not in active or active[key]["anchor"] != fault["anchor"]:
                    state["sequence"] += 1
                    fingerprint = hashlib.sha256(f"{self.directory}:{key}:{state['sequence']}:{now.isoformat()}".encode()).hexdigest()[:24]
                    active[key] = {**fault, "id": f"workshop-watch:{self.scope}:{now.date()}:{fingerprint}",
                                   "since": now.isoformat(), "last_seen": now.isoformat(),
                                   "confirmed": False, "reported": False}
                else:
                    incident = active[key]
                    elapsed = (now - timestamp(incident.get("last_seen", incident["since"]))).total_seconds()
                    if not incident.get("confirmed") and 60 <= elapsed <= 120:
                        incident["confirmed"] = True
                    if elapsed >= 60 or elapsed < 0:
                        incident["last_seen"] = now.isoformat()
            # 同一次故障跨日期不重复；确认恢复后再次故障会分配新 id。
            self._save(state)
            errors = []
            for incident in active.values():
                if incident.get("confirmed") and not incident["reported"]:
                    try:
                        incident["via"] = self._send(incident)
                        incident["reported"] = incident["via"] == "ask-kun"
                        self._save(state)
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        errors.append(str(exc))
            self.changed = before != self.summary(state)
            if errors:
                raise RuntimeError("; ".join(errors))
            return state


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="独立检查车间成功心跳和未结算轮")
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("WORKSHOP_STATE_DIR", "~/.local/state/workshop")))
    parser.add_argument("--ask-kun")
    parser.add_argument("--attention", type=Path)
    parser.add_argument("--attention-directory", type=Path)
    args = parser.parse_args(argv)
    watcher = Watch(args.state_dir, ask_kun=args.ask_kun, attention=args.attention,
                    attention_directory=args.attention_directory)
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
