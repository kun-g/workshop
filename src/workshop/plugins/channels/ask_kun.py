"""ask-kun CLI 通道；发送意图落盘后只能对账，不能猜测未发。"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from workshop.core.cards import validate_notice, validate_question
from workshop.core.models import Answer

DEFAULT_COMMAND = Path.home() / "workspace/runtime/worktrees/oh-my-assistant/workbench/workbench/agent-bin/ask-kun"


class AskKun:
    def __init__(self, command=None, state_dir=None, timeout=30):
        command = str(command or os.environ.get("WORKSHOP_ASK_KUN", DEFAULT_COMMAND))
        self.command = str(Path(shutil.which(command) or command).expanduser().absolute())
        self.directory = Path(state_dir or os.environ.get("WORKSHOP_STATE_DIR", "~/.local/state/workshop")).expanduser().resolve() / "ask-kun-channel"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.timeout = timeout

    def _path(self, key):
        return self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    def _read(self, key):
        path = self._path(key)
        return json.loads(path.read_text()) if path.exists() else None

    def _save(self, key, record):
        fd, name = tempfile.mkstemp(dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self._path(key))
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @contextmanager
    def _locked(self):
        with (self.directory / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _run(self, *args, input=None):
        return subprocess.run([self.command, *args], input=input, capture_output=True,
                              text=True, timeout=self.timeout)

    @staticmethod
    def _check(result):
        if result.returncode:
            raise RuntimeError(f"ask-kun exit {result.returncode}: {result.stderr.strip()[:500]}")

    def _questions(self):
        result = self._run("list", "--all")
        self._check(result)
        # 接线台模式返回 JSON；旧 CLI 的 TSV 不含完整 subject，不足以对账。
        try:
            data = json.loads(result.stdout)
        except ValueError:
            return None
        rows = data.get("questions") if isinstance(data, dict) else data
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return None
        return rows

    def _reconcile(self, key, record):
        if record.get("external"):
            return record["external"]
        if record["mode"] == "notice":
            # notify 自己按 request-id 幂等；对账重放同一正文，不另建消息。
            return self._notify(key, record)
        rows = self._questions()
        if rows is None:
            return None
        matches = [row for row in rows if row.get("from") == record["sender"]
                   and row.get("subject") == record["payload"]["subject"]]
        if len(matches) != 1 or not re.fullmatch(r"Q\d+", str(matches[0].get("id", ""))):
            return None
        row = matches[0]
        if row.get("answer_format") != "key":
            return None
        # 已登记但仍 sending/queued 也已有唯一编号，继续轮询原问题。
        record["external"] = row["id"]
        self._save(key, record)
        return row["id"]

    def reconcile(self, key, card):
        with self._locked():
            record = self._read(key)
            if record is None:
                # 本通道从未记意图，确定尚未调用外部命令。
                # notice 可安全重放；ask 缺账不能排除状态文件被删或迁移。
                return "absent" if card.get("mode") == "notice" else None
            return self._reconcile(key, record)

    def ask(self, key, card):
        validate_question(card)
        if any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,18}", c["key"]) for c in card["choices"]):
            raise ValueError("ask-kun 选项 key 只支持字母、数字、下划线、连字符")
        with self._locked():
            record = self._read(key)
            if record:
                if record["mode"] != "ask" or record["payload"] != card:
                    raise ValueError("同一通道 key 不能更换卡片")
                external = self._reconcile(key, record)
                if external:
                    return external
                raise RuntimeError("发问结果未确认，禁止重发，等待对账")
            digest = hashlib.sha256((str(self.directory) + key).encode()).hexdigest()[:24]
            record = {"mode": "ask", "sender": f"车间:{digest}", "payload": card}
            # 先准备本地文件，最后一刻才记发送意图。
            details = self.directory / (self._path(key).stem + ".txt")
            details.write_text(card["context"][:6000], encoding="utf-8")
            args = ["ask", "--no-wait", "--answer-format", "key", "--from", record["sender"],
                    "--subject", card["subject"], "--decision", card["decision"],
                    "--context", card["context"].replace("\n", " ")[:100], "--details-file", str(details),
                    "--recommend", card["recommend"], "--reason", card["reason"], "--kind", card["kind"]]
            for choice in card["choices"]:
                args.extend(["--choice", f"{choice['key']}={choice['label']}",
                             "--effect", f"{choice['key']}={choice['effect']}"])
            self._save(key, record)
            result = self._run(*args)
            self._check(result)
            external = result.stdout.strip()
            if not re.fullmatch(r"Q\d+", external):
                raise RuntimeError("ask-kun 没有返回唯一 Q 号，等待对账")
            record["external"] = external
            self._save(key, record)
            return external

    def _notify(self, key, record):
        card = record["payload"]
        # 旧正文从 stdin 输入，保留完整通知，避免 summary 的 160 字限制。
        result = self._run("notify", "--from", "车间", "--request-id", key, "-",
                           input=card["subject"] + "\n" + card["text"])
        self._check(result)
        # notify 成功无 stdout；稳定 request-id 是该通知的外部关联编号。
        record["external"] = key
        self._save(key, record)
        return key

    def notify(self, key, card):
        validate_notice(card)
        with self._locked():
            record = self._read(key)
            if record and (record["payload"] != card or record["mode"] != "notice"):
                raise ValueError("同一通道 key 不能更换通知")
            record = record or {"mode": "notice", "payload": card}
            if record.get("external"):
                return record["external"]
            self._save(key, record)
            return self._notify(key, record)

    def poll(self, cards):
        answers = []
        for card in cards:
            if card.get("answered") or card.get("cancelled"):
                continue
            result = self._run("wait", card["external"], "--timeout", "0")
            if result.returncode == 3:
                continue
            if result.returncode == 6:
                # Answer 无作废状态；报投递故障，保留原关口，不能伪造拒绝或批准。
                raise RuntimeError(f"{card['external']} 已在上游作废，需要处理原卡")
            self._check(result)
            value = result.stdout.strip()
            payload = card["payload"]
            approved = (payload["kind"] == "approval" and value == "yes"
                        and any(c["key"] == "yes" for c in payload["choices"]))
            answers.append(Answer(card["id"], value, approved))
        return answers

    def cancel(self, key, external):
        with self._locked():
            record = self._read(key)
            if record and record.get("cancelled"):
                return
            if not external:
                external = self._reconcile(key, record) if record else None
                if not external:
                    raise RuntimeError("取消前不能确定原卡是否已发")
            rows = self._questions()
            row = next((row for row in rows or [] if row.get("id") == external), None)
            if row is None:
                raise RuntimeError("取消前无法核对原卡")
            if row.get("status") not in ("answered", "defaulted", "cancelled"):
                result = self._run("cancel", external, "--reason", "车间原问题已失效")
                self._check(result)
            if record:
                record["cancelled"] = True
                self._save(key, record)


def make_channel():
    return AskKun()
