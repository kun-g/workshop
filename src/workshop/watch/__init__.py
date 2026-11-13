"""独立看门：只读核心状态，通知失败时登记等你清单。"""
from contextlib import contextmanager
from datetime import datetime, timezone
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
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("时间缺时区")
    return parsed


class Watch:
    def __init__(self, state_dir, *, ask_kun=None, attention=None, python=None, timeout=30):
        self.directory = Path(state_dir).expanduser().resolve()
        command = str(ask_kun or os.environ.get("WORKSHOP_ASK_KUN", DEFAULT_ASK))
        self.ask_kun = str(Path(shutil.which(command) or command).expanduser().absolute())
        self.attention = str(Path(attention or DEFAULT_ATTENTION).expanduser().resolve())
        self.python = str(Path(python or sys.executable).resolve())
        self.timeout = timeout

    def faults(self, now):
        faults = {}
        try:
            heartbeat = json.loads((self.directory / "heartbeat.json").read_text())
            age = (now - timestamp(heartbeat["last_ok_at"])).total_seconds()
            if age > 180 or age < -60:
                faults["heartbeat"] = {"anchor": heartbeat["last_ok_at"],
                                       "text": f"车间最后成功走完一轮 tick 的时间为 {heartbeat['last_ok_at']}，请检查守护进程。"}
        except (OSError, ValueError, KeyError, TypeError):
            faults["heartbeat"] = {"anchor": "unreadable", "text": "车间成功心跳缺失或无法读取，请检查守护进程。"}
        try:
            # mode=ro 不创建数据库、不改任何核心视图；独立于核心和插件装配。
            with sqlite3.connect((self.directory / "workshop.db").as_uri() + "?mode=ro", uri=True, timeout=5) as db:
                for rid, body in db.execute("SELECT id, body FROM rounds"):
                    row = json.loads(body)
                    if row["status"] in ("starting", "running", "finishing") and (now - timestamp(row["at"])).total_seconds() > 7200:
                        faults[f"round:{rid}"] = {"anchor": row["at"], "text": f"车间一轮 {rid} 从 {row['at']} 起超过两小时未结算（{row['status']}），请检查执行者和结算日志。"}
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            faults["database"] = {"anchor": "unreadable", "text": "车间状态数据库缺失或无法读取，无法检查未结算轮，请检查状态目录。"}
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
        row = {"来源会话": incident["id"], "从几点起": incident["since"], "种类": "等你",
               "要什么": "请 Kun 检查车间：" + incident["text"], "推荐": "检查守护进程和通道日志，修复后由看门确认恢复",
               "去哪看": str(self.directory), "状态": "待处理", "推飞书": False, "持续告警": True}
        result = subprocess.run([self.python, self.attention, "--writer", "车间", "--row",
                                 json.dumps(row, ensure_ascii=False)], capture_output=True, text=True, timeout=self.timeout)
        if result.returncode:
            raise RuntimeError(f"看门通知和等你清单均失败：{result.stderr[:500]}")
        return "attention"

    def run(self, now=None):
        now = now or datetime.now(timezone.utc)
        with self._locked():
            path = self.directory / "watch.json"
            state = json.loads(path.read_text()) if path.exists() else {"sequence": 0, "active": {}}
            faults = self.faults(now)
            active = state["active"]
            for key in list(active):
                if key not in faults:
                    del active[key]
            for key, fault in faults.items():
                if key not in active or active[key]["anchor"] != fault["anchor"]:
                    state["sequence"] += 1
                    fingerprint = hashlib.sha256(f"{self.directory}:{key}:{state['sequence']}:{now.isoformat()}".encode()).hexdigest()[:24]
                    active[key] = {**fault, "id": f"workshop-watch:{now.date()}:{fingerprint}",
                                   "since": now.isoformat(), "reported": False}
            # 同一次故障跨日期不重复；确认恢复后再次故障会分配新 id。
            self._save(state)
            errors = []
            for incident in active.values():
                if not incident["reported"]:
                    try:
                        incident["via"] = self._send(incident)
                        incident["reported"] = True
                        self._save(state)
                    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        errors.append(str(exc))
            if errors:
                raise RuntimeError("; ".join(errors))
            return state
