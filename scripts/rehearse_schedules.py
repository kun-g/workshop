#!/usr/bin/env python3
"""核心 R34 周期任务的真实路径；只在临时目录里跑，不碰现役状态库、主检出和守护进程。

  uv run python scripts/rehearse_schedules.py [--keep]

在临时状态目录起当前检出的 `workshop.cli run`（真守护进程、真作业进程、真信箱），用真实入口
`workshop schedule add` 登记一条一分钟后到点的每天任务（Asia/Shanghai），等到点开出活，
原样打出 `workshop schedule list`、`workshop status` 和事实表里与这条任务有关的事实。

流程和执行者用演练替身（本文件里的类，经临时 dist-info 的入口点装进守护进程和作业进程）：
派活只到预订一轮为止，量的是核心自己的路径，不起模型、不发网络请求。

这是真实时钟的演练脚本，不是测试：等心跳、等到点用「查条件 + 总超时」的循环，循环里短暂 sleep 让出 CPU，
和 rehearse_batching.py 一样；E8 管的是测试，测试（tests/test_schedules.py）只用假时钟，不 sleep。
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

from workshop.core.models import STAGES, Evidence, Started

ROOT = Path(__file__).resolve().parents[1]
TARGET = "rehearsal"
SCHEDULE = "rehearsal-daily"


class Flow:
    def classify(self, event, item, target, find_external=None):
        return "update"

    def stages(self, target):
        return STAGES[:-1]

    def brief(self, item, stage, target):
        return "演练：周期任务开出来的活"

    def assess(self, item, receipt, target):
        return Evidence(awaits_update=True, reason="演练")

    def after(self, item, stage, key, target, services=None):
        return None

    def cleanup(self, target, keep):
        return None


class Executor:
    """只返回会话标识，不起任何进程；轮次留在跑着。"""

    def available(self, config):
        return True

    def launch(self, token, brief, item, config, target):
        return Started(f"rehearsal:{item['id']}", f"rehearsal:{token}", None)

    def resume(self, token, session, brief, item, config, target):
        return Started(session, f"rehearsal:{token}", None)

    def poll(self, token, started):
        return "running"

    def stop(self, token, started):
        return None

    def usage(self, token, started, since):
        return None


def make_flow():
    return Flow()


def make_executor():
    return Executor()


def rules():
    role = {"use": "rehearsal", "family": "rehearsal", "billing": "subscription"}
    return {"targets": {TARGET: {"plugin": "schedule_rehearsal", "sources": []}},
            "executors": {"writer": role, "reviewer": [role], "acceptor": [role]},
            "limits": {"concurrent": 5, "chain_depth": 8}, "selector": "ordered", "ask_first": [], "skip_ask": []}


def install_plugins(folder):
    folder.mkdir(parents=True)
    shutil.copy(Path(__file__).resolve(), folder / "schedule_rehearsal.py")
    info = folder / "schedule_rehearsal-0.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: schedule-rehearsal\nVersion: 0.0.0\n")
    (info / "entry_points.txt").write_text(
        "[workshop.flow]\nschedule_rehearsal = schedule_rehearsal:make_flow\n\n"
        "[workshop.executor]\nrehearsal = schedule_rehearsal:make_executor\n")


def show(cli, env, *argv):
    result = subprocess.run([*cli, *argv], env=env, capture_output=True, text=True, timeout=60)
    print(f"$ workshop {' '.join(argv)}  (exit {result.returncode})")
    sys.stdout.write(result.stdout)
    sys.stdout.write(result.stderr)
    return result


def main():
    import yaml
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="workshop-r34-daemon-")).resolve()
    state, home = root / "state", root / "home"
    home.mkdir()
    install_plugins(root / "plugins")
    rules_path = root / "rules.yaml"
    rules_path.write_text(yaml.safe_dump(rules(), allow_unicode=True))
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
    env.update(HOME=str(home), PYTHONPATH=f"{ROOT / 'src'}:{root / 'plugins'}", WORKSHOP_DEPLOYMENT="none")
    cli = [sys.executable, "-m", "workshop.cli", "--state-dir", str(state), "--rules", str(rules_path)]
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    print(f"commit {commit}")
    print(f"state {state}")
    log = (root / "daemon.log").open("w")
    process = subprocess.Popen([*cli, "run"], env=env, stdout=log, stderr=subprocess.STDOUT)
    code = 1
    try:
        deadline = time.monotonic() + 30
        while not (state / "heartbeat.json").exists():
            if time.monotonic() > deadline or process.poll() is not None:
                raise SystemExit(f"守护进程没起来：{(root / 'daemon.log').read_text()}")
            time.sleep(0.05)
        print(f"daemon pid {process.pid}")
        zone = ZoneInfo("Asia/Shanghai")
        now = datetime.now(timezone.utc)
        local = (now + timedelta(minutes=1)).astimezone(zone)
        due = (local.replace(second=0, microsecond=0) + timedelta(minutes=1)) if local.second or local.microsecond else local
        print(f"now {now.isoformat()}  due {due.isoformat()}")
        show(cli, env, "schedule", "add", SCHEDULE, "--target", TARGET, "--title", "演练日报",
             "--every", "daily", "--at", due.strftime("%H:%M"), "--tz", "Asia/Shanghai")
        show(cli, env, "schedule", "list")
        ext_id = f"{SCHEDULE}:{due.isoformat()}"
        found = None
        deadline = time.monotonic() + 200
        while found is None and time.monotonic() < deadline and process.poll() is None:
            conn = sqlite3.connect(f"file:{state / 'workshop.db'}?mode=ro", uri=True)
            try:
                found = conn.execute("SELECT seq, at FROM facts WHERE source='schedule' AND ext_id=?", (ext_id,)).fetchone()
                reserved = conn.execute("SELECT 1 FROM facts WHERE kind='round_reserved' AND item=?",
                                        (f"{TARGET}:{SCHEDULE}@{due.isoformat()}",)).fetchone() if found else None
                if found and not reserved:
                    found = None  # 等开出来的活也派上轮，状态里看到它在走
            finally:
                conn.close()
            if found is None:
                time.sleep(0.2)
        if found is None:
            print("到点 200 秒内没有开活")
            return 1
        late = (datetime.fromisoformat(found[1]) - due).total_seconds()
        print(f"开活事实 seq={found[0]} at={found[1]}，比到点晚 {late:.3f} 秒")
        show(cli, env, "status")
        show(cli, env, "schedule", "list")
        conn = sqlite3.connect(f"file:{state / 'workshop.db'}?mode=ro", uri=True)
        try:
            print("$ sqlite3 workshop.db \"SELECT seq,kind,source,ext_id,item,at,body FROM facts "
                  "WHERE source LIKE 'schedule%' OR json_extract(body,'$.id') LIKE '@schedule:%' "
                  "OR (kind='round_reserved' AND item LIKE 'rehearsal:rehearsal-daily@%') ORDER BY seq\"")
            for row in conn.execute("SELECT seq,kind,source,ext_id,item,at,body FROM facts "
                                    "WHERE source LIKE 'schedule%' OR json_extract(body,'$.id') LIKE '@schedule:%' "
                                    "OR (kind='round_reserved' AND item LIKE ?) ORDER BY seq", (f"{TARGET}:{SCHEDULE}@%",)):
                body = json.loads(row[6])
                if row[1] == "round_reserved":
                    body = {k: body[k] for k in ("token", "stage", "executor", "batch") if k in body}
                print("|".join(str(v) for v in row[:6]) + "|" + json.dumps(body, ensure_ascii=False))
        finally:
            conn.close()
        code = 0
    finally:
        process.terminate()
        process.wait(timeout=30)
        log.close()
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
