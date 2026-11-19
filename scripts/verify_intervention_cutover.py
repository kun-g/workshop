#!/usr/bin/env python3
"""#176 存量转换的隔离回放：现役状态库和介入点库的完整副本上转换、重跑、中断续跑、失败安全态、计时、旧版本跑一拍。

uv run python scripts/verify_intervention_cutover.py --output <目录> [--old-commit <上一版本提交>]

只读现役库（sqlite 在线备份成副本），不碰现役守护进程、现役介入点服务和现役 launchd；
介入点服务在副本目录里另起，送达命令写本地收件箱；外部命令（飞书、ask-kun、gh、orca、launchctl）换成只记录调用、退出 1 的替身。
「合成」副本在现役副本上另加每类一条旧记录，只用来证明每类去向；现役分布看「现役」副本。
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import pwd
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
LIVE = HOME / ".local/state/workshop"
LIVE_SERVICE = HOME / ".local/state/workshop-intervention"
RECORDER = '''#!/bin/sh
# 隔离回放的外部命令替身：只记录调用，不联网、不发消息。
printf '%s\\t%s\\n' "$(basename "$0")" "$*" >> "{log}"
exit 1
'''
INBOX = '''import json, sqlite3, sys
from pathlib import Path
root = Path(sys.argv[1]); m = json.load(sys.stdin)
with sqlite3.connect(root / 'inbox.db') as db:
    db.execute('CREATE TABLE IF NOT EXISTS calls (action TEXT, key TEXT, payload TEXT)')
    db.execute('INSERT INTO calls VALUES (?,?,?)', (m['action'], m.get('key'), json.dumps(m.get('payload'), ensure_ascii=False)))
print(json.dumps({'state': 'absent'} if m['action'] == 'reconcile' else {'state': 'sent', 'external': 'inbox:' + str(m.get('key'))}))
'''


def emit(step, text):
    print(f"\n===== {step} =====\n{text.rstrip()}", flush=True)


def copy_db(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


class Replay:
    def __init__(self, output, name):
        self.root = (output / name).resolve()
        self.state = self.root / "state"
        self.service = self.root / "intervention"
        self.bin = self.root / "bin"
        self.calls = self.root / "external-calls.log"
        self.rules = self.root / "rules.yaml"
        # Unix socket 路径有 104 字节上限；输出目录可能很深，socket 单放一个短的私有目录。
        self.endpoint = Path(tempfile.mkdtemp(prefix="w176-", dir="/tmp")) / "s.sock"

    def prepare(self):
        if self.root.exists():
            shutil.rmtree(self.root)
        for d in (self.state, self.service / "run", self.bin, self.root / "home"):
            d.mkdir(parents=True)
        self.service.chmod(0o700)
        (self.service / "run").chmod(0o700)
        started = time.monotonic()
        copy_db(LIVE / "workshop.db", self.state / "workshop.db")
        copy_db(LIVE_SERVICE / "intervention.db", self.service / "intervention.db")
        if (LIVE / "ask-kun-channel").exists():
            shutil.copytree(LIVE / "ask-kun-channel", self.state / "ask-kun-channel")
        for name in ("release.json", "watch.json"):
            if (LIVE / name).exists():
                shutil.copy2(LIVE / name, self.state / name)
        took = time.monotonic() - started
        self.calls.write_text("")
        for tool in ("lark-cli", "ask-kun", "gh", "orca", "launchctl", "codex", "claude", "cursor-agent", "kubectl"):
            path = self.bin / tool
            path.write_text(RECORDER.format(log=self.calls))
            path.chmod(0o755)
        inbox = self.root / "inbox.py"
        inbox.write_text(INBOX)
        data = yaml.safe_load((ROOT / "rules/workshop.yaml").read_text())
        data["wait_services"]["intervention"]["endpoint"] = str(self.endpoint)
        data["intervention"]["transport"] = [sys.executable, str(inbox), str(self.root)]
        data["status_page"] = "http://127.0.0.1:9/status.html"
        self.rules.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        # 现役库记着守护进程用的规则（status 读它找等待服务）；副本改记副本规则，免得连到现役 socket。
        with sqlite3.connect(self.state / "workshop.db") as db:
            row = db.execute("SELECT body FROM controls WHERE id='@rules'").fetchone()
        if row:
            sys.path.insert(0, str(ROOT / "src"))
            from workshop.core.log import Log
            log = Log(self.state / "workshop.db")
            try:
                log.append("control", {"id": "@rules", "data": data, "path": str(self.rules)})
            finally:
                log.close()
        sizes = {p.name: p.stat().st_size for p in (self.state / "workshop.db", self.service / "intervention.db")}
        return f"副本 {self.root}\n现役来源 {LIVE / 'workshop.db'}、{LIVE_SERVICE / 'intervention.db'}（sqlite 在线备份，只读）\n大小 {sizes}\n复制耗时 {took:.2f} 秒"

    def env(self):
        env = {k: os.environ[k] for k in ("LANG", "TMPDIR") if k in os.environ}
        env.update(HOME=str(self.root / "home"), PATH=f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                   WORKSHOP_ASK_KUN=str(self.bin / "ask-kun"), GIT_CONFIG_NOSYSTEM="1")
        return env

    @contextmanager
    def serve(self):
        reader, writer = os.pipe()
        log = (self.service / "process.log").open("w")
        process = subprocess.Popen([sys.executable, "-m", "workshop.plugins.intervention", "--state-dir", str(self.service),
                                    "--rules", str(self.rules), "--endpoint", str(self.endpoint), "serve", "--ready-fd", str(writer)],
                                   pass_fds=(writer,), stdout=log, stderr=log, env={**self.env(), "PYTHONPATH": str(ROOT / "src")})
        os.close(writer)
        try:
            ready, _, _ = select.select([reader], [], [], 15)
            if not ready or os.read(reader, 64) != b"ready\n":
                raise RuntimeError("隔离介入点服务没起来；看 " + str(self.service / "process.log"))
            yield
        finally:
            os.close(reader)
            process.terminate()
            process.wait(timeout=10)
            log.close()

    def run(self, *argv, python=None, cwd=ROOT, check=False, input=None):
        argv = [python or sys.executable, *argv]
        started = time.monotonic()
        result = subprocess.run(argv, cwd=cwd, env={**self.env(), "PYTHONPATH": str(Path(cwd) / "src")},
                                capture_output=True, text=True, timeout=600, input=input)
        took = time.monotonic() - started
        text = f"$ {' '.join(map(str, argv))}\nexit={result.returncode}（{took:.2f} 秒）\n{result.stdout}{result.stderr}"
        if check and result.returncode:
            raise RuntimeError(text)
        return result.returncode, text

    def convert(self, *argv):
        return self.run(str(ROOT / "scripts/convert_intervention.py"), "--state-dir", str(self.state),
                        "--intervention-db", str(self.service / "intervention.db"), "--rules", str(self.rules),
                        "--backup-dir", str(self.root / "backups"), *argv)

    def inbox(self):
        path = self.root / "inbox.db"
        if not path.exists():
            return []
        with sqlite3.connect(path) as db:
            return db.execute("SELECT action, key FROM calls").fetchall()

    def sends(self):
        calls = [line for line in self.calls.read_text().splitlines() if line.strip()]
        return f"送达命令收件箱：{self.inbox()}\n外部命令替身调用：{calls}"


def seed(replay):
    """合成副本：每类旧记录各加一条，挂在两件合成的活上（一件 workshop、一件 grant-console），都标「合成」。"""
    sys.path.insert(0, str(ROOT / "src"))
    from workshop.core.log import Log
    from workshop.core.models import stamp, utcnow
    log = Log(replay.state / "workshop.db")
    at = stamp(utcnow())
    made = []
    try:
        for target in ("workshop", "grant-console"):
            key = "cutover-seed"
            item = f"{target}:{key}"
            log.append("event", {"source": "replay", "ext_id": "seed-" + target, "target": target, "object_key": key,
                                 "title": "合成：存量转换回放", "body": {"kind": "合成"}, "depth": 0, "action": "ready",
                                 "flow": "", "initial_stage": "执行"}, item, source="replay", ext_id="seed-" + target)
            log.append("item_changed", {"stage": "执行", "state": "等你", "next": "等回答", "approved": ["合成核心批准"],
                                        "billing_approved": ["合成按量配置"], "frozen_approved": ["docs/合成.md"],
                                        "pending_advance": {"to": "交付", "card": item + ":approval"}}, item)
            question = {"subject": "合成：要不要继续", "decision": "合成：要不要继续", "context": "隔离回放合成的普通问卡",
                        "choices": [{"key": "yes", "label": "继续", "effect": "继续"}, {"key": "no", "label": "调整", "effect": "调整"}],
                        "recommend": "yes", "reason": "合成", "kind": "reversible"}
            for suffix, mode, payload, extra in (
                    ("ask", "ask", question, {"sent": True, "external": "Q-seed-" + target}),
                    ("approval", "ask", {**question, "subject": "合成：批准上线", "decision": "合成：批准上线", "kind": "approval"}, {"sent": True}),
                    ("notice", "notice", {"subject": "合成：进度", "text": "合成的待发通知", "fyi": True}, {}),
                    ("uncertain", "notice", {"subject": "合成：结果不明", "text": "合成的结果不明通知", "fyi": True}, {"sending": True})):
                card = {"id": f"{item}:{suffix}", "item": item, "mode": mode, "payload": payload, "sent": False, "answered": False,
                        "sending": False, "cancelled": False, "key": suffix, **extra}
                log.append("card_created", card, item)
                made.append(card["id"])
            for kind, owner in (("送回", "Kun"), ("工序", "agent")):
                timer = {"id": f"{item}:leave:{kind}", "item": item, "target": target, "at": "2099-01-01T00:00:00+00:00",
                         "about": f"合成：到点再看（{kind}）", "kind": kind, "owner": owner, "fired": False}
                log.append("timer_created", timer, item, source="timer-plan", ext_id=timer["id"])
                made.append(timer["id"])
    finally:
        log.close()
    return "合成旧记录：" + "、".join(made) + f"（{at}）"


def old_version(replay, commit):
    """上一版本在转换后的副本上跑一拍；交付对象先暂停，只看旧出口会不会重发。"""
    checkout = replay.root / "old-checkout"
    subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", str(checkout), commit], check=True,
                   capture_output=True, text=True)
    try:
        subprocess.run(["uv", "sync", "--locked", "--quiet"], cwd=checkout, check=True, capture_output=True, text=True)
        python = str(checkout / ".venv/bin/python")
        data = yaml.safe_load((checkout / "rules/workshop.yaml").read_text())
        data["wait_services"]["intervention"]["endpoint"] = str(replay.endpoint)
        data["intervention"]["transport"] = [sys.executable, str(replay.root / "inbox.py"), str(replay.root)]
        data.pop("managers", None)
        old_rules = replay.root / "old-rules.yaml"
        old_rules.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        outputs = []
        for target in data["targets"]:
            # 暂停只挡派新的轮（免得在副本上起真执行者）；卡片发送、定时、等待对账照常跑。
            code, text = replay.run("-m", "workshop.cli", "--state-dir", str(replay.state), "--rules", str(old_rules),
                                    "pause", target, python=python, cwd=checkout)
            outputs.append(text)
        code, text = replay.run("-m", "workshop.cli", "--state-dir", str(replay.state), "--rules", str(old_rules),
                                "run", "--once", python=python, cwd=checkout)
        outputs.append(text)
        return "\n".join(outputs)
    finally:
        subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(checkout)], capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--old-commit", default="origin/main", help="上一版本（回退目标）")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    # 一、现役副本：清单、预览、两次执行、对账、计时、上一版本跑一拍。
    live = Replay(args.output, "live")
    emit("现役副本：准备", live.prepare())
    with live.serve():
        for step, argv in (("转换前清单", ("inventory",)), ("预览", ("preview", "--rows")),
                           ("上线时切换（计时，先 workshop 后 grant-console）", ("cutover",)),
                           ("同一批再跑：run --target workshop", ("run", "--target", "workshop")),
                           ("同一批再跑：run --target grant-console", ("run", "--target", "grant-console")),
                           ("再跑一次 cutover：已转完不再备份", ("cutover",)),
                           ("对账表", ("reconcile",))):
            emit("现役副本：" + step, live.convert(*argv)[1])
        emit("现役副本：发送记录", live.sends())
    with live.serve():
        emit(f"现役副本：上一版本（{args.old_commit}）跑一拍", old_version(live, args.old_commit))
        emit("现役副本：上一版本跑一拍后的发送记录（应为空）", live.sends())

    # 二、合成副本：每类旧记录各一条；grant-console 那批中途失败一次（安全态），再中断一次（进程被杀），续跑。
    synth = Replay(args.output, "synthetic")
    emit("合成副本：准备", synth.prepare())
    emit("合成副本：加每类一条旧记录", seed(synth))
    with synth.serve():
        emit("合成副本：转换前清单", synth.convert("inventory", "--rows")[1])
        emit("合成副本：预览", synth.convert("preview", "--rows")[1])
        emit("合成副本：上线时切换，grant-console 写 3 条后失败", synth.convert("--inject-failure", "grant-console:3", "cutover")[1])
        emit("合成副本：失败后 workshop status", synth.run("-m", "workshop.cli", "--state-dir", str(synth.state), "--rules", str(synth.rules), "status")[1])
        emit("合成副本：失败后的发送记录（应为空）", synth.sends())
        emit("合成副本：修好后续跑，进程在写 2 条后被杀", synth.convert("--inject-failure", "grant-console:2:kill", "run", "--target", "grant-console")[1])
        emit("合成副本：被杀后续跑", synth.convert("run", "--target", "grant-console")[1])
        emit("合成副本：同一批再跑", synth.convert("run", "--target", "grant-console")[1])
        emit("合成副本：续跑后 workshop status", synth.run("-m", "workshop.cli", "--state-dir", str(synth.state), "--rules", str(synth.rules), "status")[1])
        emit("合成副本：对账表", synth.convert("reconcile")[1])
        with sqlite3.connect(synth.state / "workshop.db") as db:
            detail = json.loads(db.execute("SELECT body FROM facts WHERE source='cutover' AND ext_id=?",
                                           ("问卡:workshop:cutover-seed:ask",)).fetchone()[0])["detail"]
        emit("合成副本：转成的等待（服务里的记录）",
             synth.run("-m", "workshop.plugins.intervention", "--state-dir", str(synth.service), "--rules", str(synth.rules),
                       "--endpoint", str(synth.endpoint), "call", "get", input=json.dumps({"ref": detail["wait_ref"]}))[1])
        emit("合成副本：旧卡号登记会话回答（answer-wait 认旧飞书 Q 号）",
             synth.run("-m", "workshop.cli", "--state-dir", str(synth.state), "--rules", str(synth.rules), "answer-wait",
                       "Q-seed-workshop", "yes", "--version", detail["version"], "--quote", "合成回放：继续", "--session", "replay")[1])
        emit("合成副本：同一回答再登记一次（不重复记）",
             synth.run("-m", "workshop.cli", "--state-dir", str(synth.state), "--rules", str(synth.rules), "answer-wait",
                       "Q-seed-workshop", "yes", "--version", detail["version"], "--quote", "合成回放：继续", "--session", "replay")[1])
        emit("合成副本：发送记录（应为空）", synth.sends())
    return 0


if __name__ == "__main__":
    sys.exit(main())
