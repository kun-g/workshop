#!/usr/bin/env python3
"""核心 R33 合批的两段原样证据；只在临时目录里跑，不碰现役状态库、主检出和守护进程。

先量后改（同一个脚本分别跑在 base 和分支提交的代码上）：
  uv run python scripts/rehearse_batching.py count --commit <base 或分支提交>
  用 git archive 取该提交的 src，在临时状态库里起核心，按真实时钟约 150ms 间隔写三条来源事件，
  一拍一拍推进（用该提交自己的 wait_timeout 决定睡多久），代执行者立刻交回执；数一共起了几轮。

真实节奏（真守护进程、默认 500ms 窗口）：
  uv run python scripts/rehearse_batching.py daemon --samples 5
  在临时状态目录起当前检出的 `workshop.cli run`（真实作业进程），每个样本用真实入口
  `workshop event --file` 给一件空闲的新活写一条事件，原样打出事件事实和 round_reserved 事实的时间。

执行者和流程用演练替身（本文件里的类，经临时 dist-info 的入口点装进守护进程和作业进程）：
派活只到预订一轮为止，量的是核心自己的路径，不起模型、不发网络请求。
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

from workshop.core.models import STAGES, Evidence, Started

ROOT = Path(__file__).resolve().parents[1]
TARGET = "rehearsal"


class Flow:
    """每条事件都只当资料；每轮结算后停在「等资料」，来源有新事件再起一轮（用户连发几条的路径）。"""

    def classify(self, event, item, target, find_external=None):
        return "update"

    def stages(self, target):
        return STAGES[:-1]

    def brief(self, item, stage, target):
        return "演练：看一下这批事件"

    def assess(self, item, receipt, target):
        return Evidence(awaits_update=True, reason="等来源补充")

    def after(self, item, stage, key, target, services=None):
        return None

    def cleanup(self, target, keep):
        return None


class Executor:
    """只返回会话标识，不起任何进程；轮次由演练代交回执或留在跑着。"""

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


def rules(window=None):
    limits = {"concurrent": 5, "chain_depth": 8}
    if window is not None:
        limits["batch_window_ms"] = window
    role = {"use": "rehearsal", "family": "rehearsal", "billing": "subscription"}
    return {"targets": {TARGET: {"plugin": "batching_rehearsal", "sources": []}},
            "executors": {"writer": role, "reviewer": [role], "acceptor": [role]},
            "limits": limits, "selector": "ordered", "ask_first": [], "skip_ask": []}


def commit_of(path):
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


# ---------- 先量后改 ----------

def count(args):
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", args.commit], check=True,
                            capture_output=True, text=True).stdout.strip()
    root = Path(tempfile.mkdtemp(prefix="workshop-r33-count-"))
    try:
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "src"], check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", str(root)], input=archive, check=True)
        env = {**os.environ, "PYTHONPATH": str(root / "src"), "WORKSHOP_DEPLOYMENT": "none"}
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "count-inner",
                                 "--state", str(root / "state"), "--commit", commit, "--gap", str(args.gap)],
                                env=env, capture_output=True, text=True, timeout=120)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    finally:
        shutil.rmtree(root, ignore_errors=True)


def count_inner(args):
    import workshop
    from workshop.core.engine import Engine
    from workshop.core.log import Log
    from workshop.core.models import Event
    from workshop.core.rules import Rules
    from workshop.registry import Registry
    from workshop.plugins.selectors.ordered import OrderedSelector

    code = Path(workshop.__file__).resolve().parents[1]
    print(f"commit {args.commit}")
    print(f"code {code}")
    registry = Registry()
    registry.register("flow", "batching_rehearsal", Flow())
    registry.register("executor", "rehearsal", Executor())
    registry.register("selector", "ordered", OrderedSelector())
    engine = Engine(Log(Path(args.state) / "workshop.db"), Rules.validate(rules()), registry)
    print(f"batch_window_ms {engine.rules.data['limits'].get('batch_window_ms', '（规则未写，用代码默认）')}")
    began = time.monotonic()
    plan = [(i * args.gap, f"e{i + 1}") for i in range(3)]
    end = plan[-1][0] + 3.0
    while True:
        now = time.monotonic() - began
        while plan and plan[0][0] <= now:
            _, name = plan.pop(0)
            event = Event("rehearsal", name, TARGET, "1", "演练：连发三条", {"text": name},
                          action="ready" if name == "e1" else "update")
            engine.ingest(event)
            print(f"write {name} at +{(time.monotonic() - began) * 1000:.0f}ms")
        engine.tick()
        submitted = False
        for row in engine.active():
            if row.get("receipt") is None and row["status"] in ("starting", "running"):
                # 代执行者交回执：一轮看完就交，越快越容易碎成多轮。
                engine.submit(row["token"], {"outcome": "done", "summary": "看过了"})
                submitted = True
        if submitted:
            engine.tick()
        now = time.monotonic() - began
        if now >= end:
            break
        wait = min(engine.wait_timeout(), end - now, *(at - now for at, _ in plan))
        time.sleep(max(0.0, wait))  # 演练脚本按主循环给的时长睡；核心本身不睡。
    rows = engine.log.db.execute("SELECT seq, at, json_extract(body, '$.batch') FROM facts "
                                 "WHERE kind='round_reserved' ORDER BY seq").fetchall()
    events = engine.log.db.execute("SELECT seq, at, json_extract(body, '$.ext_id') FROM facts "
                                   "WHERE kind='event' ORDER BY seq").fetchall()
    for seq, at, name in events:
        print(f"event seq={seq} at={at} ext_id={name}")
    for seq, at, batch in rows:
        print(f"round_reserved seq={seq} at={at} batch={batch}")
    print(f"rounds {len(rows)}")
    engine.log.close()
    return 0


# ---------- 真实节奏 ----------

def install_plugins(folder):
    """临时 dist-info 把演练替身登记成入口点，守护进程和作业进程照常按入口点装插件。"""
    folder.mkdir(parents=True)
    shutil.copy(Path(__file__).resolve(), folder / "batching_rehearsal.py")
    info = folder / "batching_rehearsal-0.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: batching-rehearsal\nVersion: 0.0.0\n")
    (info / "entry_points.txt").write_text(
        "[workshop.flow]\nbatching_rehearsal = batching_rehearsal:make_flow\n\n"
        "[workshop.executor]\nrehearsal = batching_rehearsal:make_executor\n")


def daemon(args):
    import yaml

    root = Path(tempfile.mkdtemp(prefix="workshop-r33-daemon-")).resolve()
    state, home = root / "state", root / "home"
    home.mkdir()
    install_plugins(root / "plugins")
    rules_path = root / "rules.yaml"
    rules_path.write_text(yaml.safe_dump(rules(), allow_unicode=True))
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR") if k in os.environ}
    env.update(HOME=str(home), PYTHONPATH=f"{ROOT / 'src'}:{root / 'plugins'}", WORKSHOP_DEPLOYMENT="none",
               WORKSHOP_ASK_KUN="/usr/bin/false")
    cli = [sys.executable, "-m", "workshop.cli", "--state-dir", str(state), "--rules", str(rules_path)]
    print(f"commit {commit_of(ROOT)}")
    print(f"state {state}")
    print(f"rules limits {json.dumps(rules()['limits'], ensure_ascii=False)}（未写 batch_window_ms，用默认）")
    log = (root / "daemon.log").open("w")
    process = subprocess.Popen([*cli, "run"], env=env, stdout=log, stderr=subprocess.STDOUT)
    failed = 0
    try:
        deadline = time.monotonic() + 30
        while not (state / "heartbeat.json").exists():
            if time.monotonic() > deadline or process.poll() is not None:
                raise SystemExit(f"守护进程没起来：{(root / 'daemon.log').read_text()}")
            time.sleep(0.05)
        print(f"daemon pid {process.pid}")
        for index in range(args.samples):
            time.sleep(args.gap)
            key = f"idle-{index + 1}"
            path = root / f"{key}.json"
            path.write_text(json.dumps({"source": "rehearsal", "ext_id": key, "target": TARGET, "object_key": key,
                                        "title": "演练：空闲单活写一条事件", "body": {"text": key}}, ensure_ascii=False))
            written = subprocess.run([*cli, "event", "--file", str(path)], env=env, capture_output=True, text=True, timeout=30)
            print(f"$ workshop event --file {path.name} -> exit {written.returncode} {written.stdout.strip()}")
            conn = sqlite3.connect(f"file:{state / 'workshop.db'}?mode=ro", uri=True)
            try:
                event = conn.execute("SELECT seq, kind, at FROM facts WHERE kind='event' AND json_extract(body, '$.ext_id')=?",
                                     (key,)).fetchone()
                reserved = None
                deadline = time.monotonic() + 10
                while reserved is None and time.monotonic() < deadline:
                    reserved = conn.execute("SELECT seq, kind, at, item, json_extract(body, '$.batch') FROM facts "
                                            "WHERE kind='round_reserved' AND item=?", (f"{TARGET}:{key}",)).fetchone()
                    time.sleep(0.01)
                classified = conn.execute("SELECT seq, kind, at FROM facts WHERE kind='event_classified' "
                                          "AND json_extract(body, '$.event_seq')=?", (event[0],)).fetchone()
            finally:
                conn.close()
            print(f"事件事实        seq={event[0]} kind={event[1]} at={event[2]}")
            print(f"归件事实        seq={classified[0]} kind={classified[1]} at={classified[2]}")
            if reserved is None:
                print("round_reserved  没有出现（10 秒内）")
                failed += 1
                continue
            print(f"round_reserved  seq={reserved[0]} kind={reserved[1]} at={reserved[2]} item={reserved[3]} batch={reserved[4]}")
            ms = (datetime.fromisoformat(reserved[2]) - datetime.fromisoformat(event[2])).total_seconds() * 1000
            inside = 500 <= ms <= 1000
            failed += not inside
            print(f"写入到起轮      {ms:.3f} ms  {'在 500–1000ms 内' if inside else '不在 500–1000ms 内'}")
    finally:
        process.terminate()
        process.wait(timeout=30)
        log.close()
    print(f"samples {args.samples} outside {failed}")
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("count")
    p.add_argument("--commit", required=True)
    p.add_argument("--gap", type=float, default=0.15)
    p = sub.add_parser("count-inner")
    p.add_argument("--state", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--gap", type=float, default=0.15)
    p = sub.add_parser("daemon")
    p.add_argument("--samples", type=int, default=5)
    p.add_argument("--gap", type=float, default=1.0, help="两个样本之间隔几秒")
    p.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args()
    return {"count": count, "count-inner": count_inner, "daemon": daemon}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
