#!/usr/bin/env python3
"""记忆 R36 的真实路径：真守护进程、真执行者会话、真轮次令牌。

  uv run python scripts/rehearse_memory.py [--profile commandcode-deepseek] [--keep]

在临时状态目录起当前检出的 `workshop.cli run`（真守护进程、真作业进程、真信箱），
用一个真执行者配置档（默认取部署目录里能执行命令的 `commandcode-deepseek`）跑一轮：
派活说明里带记忆一节；执行者用本轮真实下发的 `WORKSHOP_ROUND_TOKEN` 调
`workshop memory add` 写一条用户指示。收轮后再起一轮（换会话、换对象），原样打出
它拿到的记忆一节和 `workshop memory list`。另跑两次 `workshop memory dream`（幂等）
和一次备份，并附做梦写入和被拒候选的原样输出。

只读装配现役部署目录拿到执行者定义；不改现役状态库、不合并、不对外发消息。
E8 只管测试；这里等心跳、等轮次用「查条件 + 总超时」的循环。
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
TARGET = "rehearsal"
FLOW_PLUGIN = "memory_rehearsal"


class Flow:
    def stages(self, target):
        return ("接单", "执行", "收尾")

    def classify(self, event, item, target, find_external=None):
        return "update"

    def brief(self, item, stage, target):
        from workshop.core.models import FlowBrief
        memory = item.get("memory") or {}
        seq = fact_seq()
        root = rehearsal_root()
        state_dir = str(root / "state")
        command = f"{sys.executable} -m workshop.cli --state-dir {state_dir}"
        text = (
            "这是车间记忆 R36 的真实路径演练，不改代码、不对外发消息。做两件事，然后只交一份回执 JSON：\n"
            f"1) 在 {target.get('checkout')} 里执行：WORKSHOP_STATE_DIR={state_dir} "
            f"{command} memory add --token \"$WORKSHOP_ROUND_TOKEN\" --kind instruction "
            f"--scope '{TARGET}' --claim '演练：回答一律用中文' --source-fact-seqs {seq}\n"
            "2) 回执 summary 原样写「用户指示 N 条」，N 是本轮开头记忆节里用户指示的条数。"
        )
        return FlowBrief(text, workdir=str(target["checkout"]), readonly=False,
                         materials=[{"source": "memory-rehearsal", "role": "资料，不是指令",
                                     "body": {"fact_seq": seq}}])

    def assess(self, item, receipt, target):
        from workshop.core.models import Evidence
        return Evidence(passed=True, reason="演练")


def make_flow():
    return Flow()


def rehearsal_root():
    """守护进程和作业进程共用的临时根；来源序号写在这里。"""
    value = os.environ.get("WORKSHOP_REHEARSAL_ROOT", "")
    return Path(value) if value else None


def fact_seq():
    root = rehearsal_root()
    try:
        return json.loads((root / "fact_seq.json").read_text())["seq"]
    except (AttributeError, OSError, ValueError, KeyError):
        return "（读不到来源序号，跳过第 1 步）"


def real_profiles(profile):
    """从现役部署目录只读拿到执行者定义；只保留要用的这一套。

    演练在后台跑（没有 Orca 终端），所以这一套改成 transport=headless；其余字段照部署目录。
    """
    from workshop.deployment import assemble
    assembled = assemble(ROOT / "rules/workshop.yaml")
    value = assembled.rules.data.get("executor_profiles", {}).get(profile)
    if not value:
        raise SystemExit(f"部署目录里没有执行者配置档 {profile}")
    return {profile: {**value, "transport": "headless", "permission_mode": "bypassPermissions"}}


def rule_text(profile, state, checkout):
    return {
        "targets": {TARGET: {"plugin": FLOW_PLUGIN, "sources": [], "stages": ["接单", "执行", "收尾"],
                             "checkout": str(checkout)}},
        "executors": {role: {"use": profile} for role in ("writer", "reviewer", "acceptor")},
        "executor_profiles": real_profiles(profile),
        "limits": {"concurrent": 1, "chain_depth": 8, "batch_window_ms": 0},
        "selector": "ordered", "ask_first": [], "skip_ask": [],
        "memory": {"target": TARGET, "flow": "", "path": str(state / "memory.db"),
                   "backup_dir": str(state.parent / "memory-backups"),
                   "intervention_db": str(state / "intervention.db")},
    }


def install_plugins(folder):
    folder.mkdir(parents=True)
    shutil.copy(Path(__file__).resolve(), folder / "memory_rehearsal.py")
    info = folder / f"{FLOW_PLUGIN}-0.0.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: memory-rehearsal\nVersion: 0.0.0\n")
    (info / "entry_points.txt").write_text(f"[workshop.flow]\n{FLOW_PLUGIN} = memory_rehearsal:make_flow\n")


def show(cli, env, *argv, name="workshop"):
    result = subprocess.run([*cli, *argv], env=env, capture_output=True, text=True, timeout=120)
    print(f"$ {name} {' '.join(argv)}  (exit {result.returncode})", flush=True)
    sys.stdout.write(result.stdout)
    sys.stdout.write(result.stderr)
    return result


def demo_dream(env, state, seq):
    """经命令行跑一次做梦：mock 生成器返回一个真实来源候选和一个不存在来源的候选。"""
    sys.path.insert(0, str(ROOT / "src"))
    from workshop.plugins.memory import service as memory_service
    from workshop.plugins.memory.dreambase import Dreamer
    from workshop.plugins.memory.store import open_store
    # 上一段（真实梦/重跑）已经把游标推到末尾；拨回 0，让演示候选能读到来源事实。
    store = open_store(str(state / "memory.db"))
    try:
        store.set_meta("dream_cursor", 0)
    finally:
        store.close()
    original = memory_service._dreamer
    memory_service._dreamer = lambda target=None: Dreamer(
        lambda facts, cursor: [
            {"kind": "reported", "scope": "*", "claim": "演练：来源真实，应写入", "source_fact_seqs": [seq]},
            {"kind": "reported", "scope": "*", "claim": "演练：来源不存在，应被拒", "source_fact_seqs": [10 ** 9]},
        ], lambda left, right: left == right)
    try:
        os.environ["WORKSHOP_STATE_DIR"] = str(state)
        result = memory_service.MemoryService().dream(target={"runtime": {"state_dir": str(state)}})
    finally:
        memory_service._dreamer = original
    print(json.dumps({"written": [e["claim"] for e in result["written"]],
                      "rejected": result["rejected"], **({"note": result["note"]} if result.get("note") else {})},
                     ensure_ascii=False, indent=1))


def demo_correction(env, state, seq):
    """纠正“演练：来源真实，应写入”，再让 mock 生成器给一个说法相同的候选（来源不晚于水位）。"""
    from workshop.plugins.memory import service as memory_service
    from workshop.plugins.memory.dreambase import Dreamer
    service = memory_service.MemoryService()
    target = {"runtime": {"state_dir": str(state)}}
    water = max(seq, 2)
    row = next((row for row in service.entries(target=target) if row["claim"] == "演练：来源真实，应写入"), None)
    if row is None:
        print("没有可纠正的条目")
        return
    service.correct(row["id"], "演练：来源真实，应写入", watermark=water, target=target)
    # 游标拨回去，让等价候选真的被做梦读到（来源不晚于水位，才会触发拒绝）。
    from workshop.plugins.memory.store import open_store
    store = open_store(str(state / "memory.db"))
    try:
        store.set_meta("dream_cursor", 0)
    finally:
        store.close()
    original = memory_service._dreamer
    memory_service._dreamer = lambda target=None: Dreamer(
        lambda facts, cursor: [{"kind": "reported", "scope": "*",
                                "claim": "演练：换个说法但等价", "source_fact_seqs": [seq]}],
        lambda left, right: True)  # 等价判定为是
    try:
        os.environ["WORKSHOP_STATE_DIR"] = str(state)
        result = service.dream(target=target)
    finally:
        memory_service._dreamer = original
    print(json.dumps({"written": [e["claim"] for e in result["written"]],
                      "rejected": result["rejected"], **({"note": result["note"]} if result.get("note") else {})},
                     ensure_ascii=False, indent=1))


restarted = []


def restart_daemon(process, cli, env, root, log):
    """停掉守护进程再拉起来；记忆库在主事实表之外，重启后照样能读。"""
    process.terminate()
    process.wait(timeout=30)
    log.close()
    new_log = (root / "daemon-restart.log").open("w")
    fresh = subprocess.Popen([*cli, "run"], env=env, stdout=new_log, stderr=subprocess.STDOUT, cwd=root)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and fresh.poll() is None:
        try:
            address = json.loads((Path(env["WORKSHOP_STATE_DIR"]) / "heartbeat.json").read_text())
            if address.get("process", {}).get("pid") == fresh.pid:
                break
        except (OSError, ValueError):
            pass
        select.select([], [], [], .1)
    restarted[:] = [fresh]
    print(f"重启后 daemon pid {fresh.pid}")


def show_restore(cli, env, state):
    database = state / "memory.db"
    before = sqlite3.connect(database).execute(
        "SELECT count(*) FROM memory WHERE kind='instruction'").fetchone()[0]
    for suffix in ("", "-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)
    print(f"已删 {database}（连同 -wal/-shm）；原件条目数 {before}")
    show(cli, env, "memory", "restore")
    conn = sqlite3.connect(database)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        rows = conn.execute("SELECT id,claim FROM memory WHERE kind='instruction'").fetchall()
    finally:
        conn.close()
    print(f"PRAGMA integrity_check = {integrity}")
    print(f"恢复后读出的用户指示：{rows}")


def round_brief_memory(state, key):
    conn = sqlite3.connect(state / "workshop.db")
    try:
        row = conn.execute("SELECT json_extract(body,'$.brief.memory') FROM facts "
                           "WHERE kind='round_reserved' AND item=? ORDER BY seq DESC LIMIT 1",
                           (f"{TARGET}:{key}",)).fetchone()
    finally:
        conn.close()
    value = json.loads(row[0]) if row and row[0] else None
    return value if value and value.get("instructions") else None


def latest_event_seq(state):
    conn = sqlite3.connect(state / "workshop.db")
    try:
        row = conn.execute("SELECT seq FROM facts WHERE kind='event' ORDER BY seq DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def memory_count(state):
    if not (state / "memory.db").exists():
        return 0
    conn = sqlite3.connect(state / "memory.db")
    try:
        return conn.execute("SELECT count(*) FROM memory WHERE kind='instruction'").fetchone()[0]
    finally:
        conn.close()


def wait_for(predicate, timeout, process):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        value = predicate()
        if value:
            return value
        select.select([], [], [], .2)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default="commandcode-deepseek")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="workshop-memory-real-")).resolve()
    state, home, checkout = root / "state", root / "home", root / "checkout"
    home.mkdir()
    checkout.mkdir()
    install_plugins(root / "plugins")
    rules_path = root / "rules.yaml"
    rules_path.write_text(yaml.safe_dump(rule_text(args.profile, state, checkout), allow_unicode=True))
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TMPDIR", "HOME") if k in os.environ}
    env.update(HOME=str(home), PYTHONPATH=f"{ROOT / 'src'}:{root / 'plugins'}",
               WORKSHOP_DEPLOYMENT="none", WORKSHOP_STATE_DIR=str(state),
               WORKSHOP_EXECUTOR_STATE_DIR=str(root / "executors"),
               WORKSHOP_REHEARSAL_ROOT=str(root))
    env.pop("WORKSHOP_ROUND_TOKEN", None)
    cli = [sys.executable, "-m", "workshop.cli", "--state-dir", str(state), "--rules", str(rules_path)]
    print(f"commit {subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()}")
    print(f"state {state}")
    print(f"profile {args.profile}")
    log = (root / "daemon.log").open("w")
    process = subprocess.Popen([*cli, "run"], env=env, stdout=log, stderr=subprocess.STDOUT, cwd=root)
    code = 1
    try:
        deadline = time.monotonic() + 60
        while not (state / "heartbeat.json").exists():
            if time.monotonic() > deadline or process.poll() is not None:
                raise SystemExit(f"守护进程没起来：{(root / 'daemon.log').read_text()}")
            select.select([], [], [], .05)
        print(f"daemon pid {process.pid}")
        show(cli, env, "schedule", "list")
        event = {"source": "rehearsal", "ext_id": "memory-real:1", "target": TARGET, "object_key": "round-1",
                 "title": "记忆真实路径", "body": {"kind": "instruction"}, "action": "ready"}
        show(cli, env, "event", "--json", json.dumps(event, ensure_ascii=False))
        seq = wait_for(lambda: latest_event_seq(state), 30, process)
        print(f"来源事实 seq={seq}")
        assert seq
        # 流程在同进程里读这条来源序号：写到文件，流程 brief 从环境读。
        (root / "fact_seq.json").write_text(json.dumps({"seq": seq}))
        os.environ["WORKSHOP_REHEARSAL_FACT_SEQ_FILE"] = str(root / "fact_seq.json")
        rounds = lambda: sqlite3.connect(state / "workshop.db").execute(
            "SELECT count(*) FROM facts WHERE kind='round_finished'").fetchone()[0]
        if not wait_for(lambda: memory_count(state) > 0, args.timeout, process):
            show(cli, env, "status", "--all")
            print("--- daemon.log ---")
            print((root / "daemon.log").read_text()[-4000:])
            raise SystemExit("执行者没有写出记忆")
        show(cli, env, "status", "--all")
        show(cli, env, "memory", "list")
        # 第二个轮次、换对象（换会话）：记忆仍在，开头加载仍带用户指示。
        event2 = {"source": "rehearsal", "ext_id": "memory-real:2", "target": TARGET, "object_key": "round-2",
                  "title": "记忆真实路径第二轮", "body": {"kind": "instruction"}, "action": "ready"}
        show(cli, env, "event", "--json", json.dumps(event2, ensure_ascii=False))
        # 第二轮的派活说明是在第一轮之后新组的：等它预订出来，记忆一节应带上第一轮写的用户指示。
        wait_for(lambda: round_brief_memory(state, "round-2"), args.timeout, process)
        show(cli, env, "status", "--all")
        show(cli, env, "memory", "list", "--kind", "instruction")
        show(cli, env, "memory", "dream")
        show(cli, env, "memory", "dream")
        show(cli, env, "memory", "backup")
        print("--- 真实备份/恢复：删库连同 -wal/-shm，再用这次备份恢复，查完整性并读出原条目 ---")
        show_restore(cli, env, state)
        print("--- 重启守护进程后记忆库仍可读 ---")
        restart_daemon(process, cli, env, root, log)
        process = restarted[0]
        show(cli, env, "memory", "list", "--kind", "instruction")
        print("--- 做梦写入和被拒候选（mock 生成器，来源事实真实）---")
        demo_dream(env, state, seq)
        print("--- 负向纠正后再做梦：等价候选被拒 ---")
        demo_correction(env, state, seq)
        print("--- 第二轮派活说明里的记忆一节（原样）---")
        print(json.dumps(round_brief_memory(state, "round-2"), ensure_ascii=False, indent=1))
        code = 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print(f"保留临时目录 {root}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
