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
import sys
from copy import deepcopy

from workshop.core.cards import validate_notice, validate_question
from workshop.core.models import Answer
from workshop.core.ports import ChannelRejected

DEFAULT_COMMAND = Path.home() / "workspace/runtime/worktrees/oh-my-assistant/workbench/workbench/agent-bin/ask-kun"


class AskKun:
    def __init__(self, command=None, state_dir=None, timeout=30):
        command = str(command or os.environ.get("WORKSHOP_ASK_KUN", DEFAULT_COMMAND))
        self.command = str(Path(shutil.which(command) or command).expanduser().absolute())
        self.directory = Path(state_dir or os.environ.get("WORKSHOP_STATE_DIR", "~/.local/state/workshop")).expanduser().resolve() / "ask-kun-channel"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.timeout = timeout
        root = self.directory.parent
        database = root / "workshop.db"
        identity = "no-database"
        if database.exists():
            stat = database.stat()
            identity = f"{stat.st_dev}:{stat.st_ino}:{getattr(stat, 'st_birthtime', '')}"
        # 路径隔离试跑，数据库文件身份隔离同目录重建；普通内容更新不改变身份。
        self.scope = hashlib.sha256(f"{root}:{identity}".encode()).hexdigest()[:24]

    def _path(self, key):
        return self.directory / (hashlib.sha256((self.scope + key).encode()).hexdigest() + ".json")

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
        try:
            return subprocess.run([self.command, *args], input=input, capture_output=True,
                                  text=True, timeout=self.timeout)
        except (FileNotFoundError, PermissionError) as exc:
            raise ChannelRejected(str(exc)) from exc

    @staticmethod
    def _check(result):
        if result.returncode == 2 or result.returncode and "尚未发送" in result.stderr:
            raise ChannelRejected(result.stderr.strip() or "通道明确拒收，尚未发送")
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
        if record.get("rejected"):
            raise ChannelRejected(record["rejected"])
        if record.get("external"):
            return record["external"]
        if record["mode"] == "notice":
            # notify 自己按 request-id 幂等；对账重放同一正文，不另建消息。
            return self._notify(key, record)
        rows = self._questions()
        if rows is None:
            return None
        matches = [row for row in rows if row.get("from") == record["sender"]
                   and row.get("subject") == record.get("wire", record["payload"])["subject"]]
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

    @staticmethod
    def _prepare(card):
        wire = deepcopy(card)
        for name in ("subject", "decision", "reason"):
            if isinstance(wire.get(name), str):
                wire[name] = re.sub(r"[\r\n\t]+", " ", wire[name]).strip()
        for choice in wire.get("choices", []):
            for name in ("key", "label", "effect"):
                if isinstance(choice.get(name), str):
                    choice[name] = re.sub(r"[\r\n\t]+", " ", choice[name]).strip()
        try:
            validate_question(wire)
        except (ValueError, TypeError, KeyError) as exc:
            raise ChannelRejected(str(exc)) from exc
        if any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,18}", c["key"]) for c in wire["choices"]):
            raise ChannelRejected("选项 key 只支持字母、数字、下划线、连字符")
        # 工程标识的完整含义保留在详情；无法用短字段表达时显式指向详情。
        technical = re.compile(r"(?:[a-z]+://|www\.|(?:MR|PR|CI|Q)\s*[#!！＃]?[0-9０-９]+|"
                               r"(?<![a-z0-9])[a-f0-9]{7,40}(?![a-z0-9])|[/\\]|"
                               r"\.(?:py|md|json|ya?ml|toml|sh|js|ts|tsx|html|txt|csv|log)(?![a-z0-9_])|"
                               r"(?<![a-z0-9_])(?:worktree|commit|merge|rebase|cherry-pick|diff|pipeline|deploy|"
                               r"rollback|repo|pull request|merge request|CI|CD|SHA|CLI|SDK|stdout|stderr)(?![a-z0-9_]))", re.I)
        defaults = {"subject": "车间方案待确认", "decision": "是否按详情中的方案继续", "reason": "完整依据与影响已列在详情中"}
        for name, fallback in defaults.items():
            if technical.search(wire[name]):
                wire[name] = fallback
        labels = set()
        for index, choice in enumerate(wire["choices"], 1):
            if (choice["label"] in {"确定", "同意", "确认", "是", "否", "处理"}
                    or choice["label"] in labels or technical.search(choice["label"])):
                choice["label"] = f"采用选项{index}"
            # 后面的原标签也可能恰好等于自动标签；保持全局唯一。
            while choice["label"] in labels:
                choice["label"] = f"选择第{index}项" + "。" * len(labels)
            labels.add(choice["label"])
            if technical.search(choice["effect"]) or choice["effect"].rstrip("。.!！") == choice["label"].rstrip("。.!！"):
                choice["effect"] = f"按详情中选项{index}注明的结果执行"
        wire["context"] = "完整依据见下方详情"
        return wire

    def ask(self, key, card):
        wire = self._prepare(card)
        with self._locked():
            record = self._read(key)
            if record:
                if record["mode"] != "ask" or record["payload"] != card:
                    raise ChannelRejected("同一通道 key 不能更换卡片")
                external = self._reconcile(key, record)
                if external:
                    return external
                raise RuntimeError("发问结果未确认，禁止重发，等待对账")
            digest = hashlib.sha256((self.scope + key).encode()).hexdigest()[:24]
            record = {"mode": "ask", "sender": f"车间:{digest}", "payload": card, "wire": wire}
            details = self.directory / (self._path(key).stem + ".txt")
            original = {k: v for k, v in card.items() if k != "context"}
            full = json.dumps(original, ensure_ascii=False, indent=2) + "\n\n完整背景：\n" + card["context"]
            try:
                details.write_text(full[:6000], encoding="utf-8")
            except OSError as exc:
                raise ChannelRejected(str(exc)) from exc
            args = ["ask", "--no-wait", "--answer-format", "key", "--from", record["sender"],
                    "--subject", wire["subject"], "--decision", wire["decision"],
                    "--context", wire["context"], "--details-file", str(details),
                    "--recommend", wire["recommend"], "--reason", wire["reason"], "--kind", wire["kind"]]
            for choice in wire["choices"]:
                args.extend(["--choice", f"{choice['key']}={choice['label']}",
                             "--effect", f"{choice['key']}={choice['effect']}"])
            try:
                self._save(key, record)
            except OSError as exc:
                raise ChannelRejected(str(exc)) from exc
            try:
                result = self._run(*args)
                self._check(result)
            except ChannelRejected as exc:
                record["rejected"] = str(exc)
                self._save(key, record)
                raise
            external = result.stdout.strip()
            if not re.fullmatch(r"Q\d+", external):
                raise RuntimeError("ask-kun 没有返回唯一 Q 号，等待对账")
            record["external"] = external
            self._save(key, record)
            return external

    def _request_id(self, key):
        prefixed = f"workshop:{self.scope}:{key}"
        return prefixed if len(prefixed) <= 200 else f"workshop:{self.scope}:{hashlib.sha256(key.encode()).hexdigest()}"

    def _notify(self, key, record):
        card = record["payload"]
        # 旧正文从 stdin 输入，保留完整通知，避免 summary 的 160 字限制。
        result = self._run("notify", "--from", "车间", "--request-id", self._request_id(key), "-",
                           input=card["subject"] + "\n" + card["text"])
        self._check(result)
        # notify 成功无 stdout；稳定 request-id 是该通知的外部关联编号。
        record["external"] = key
        self._save(key, record)
        return key

    def notify(self, key, card):
        try:
            validate_notice(card)
        except ValueError as exc:
            raise ChannelRejected(str(exc)) from exc
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
        answers, failures = [], []
        checked = 0
        for card in cards:
            if card.get("answered") or card.get("cancelled"):
                continue
            checked += 1
            try:
                result = self._run("wait", card["external"], "--timeout", "0")
                if result.returncode == 3:
                    continue
                missing = result.returncode in (1, 2, 4) and any(
                    text in result.stderr for text in ("找不到该问题", "找不到问题", "unknown_question", "question_not_found"))
                if result.returncode and ("状态文件里没有" in result.stderr or "forbidden_node" in result.stderr):
                    rows = self._questions()
                    missing = rows is not None and not any(row.get("id") == card["external"] for row in rows)
                if result.returncode == 6 or missing:
                    answers.append(Answer(card["id"], "", cancelled=True))
                    continue
                self._check(result)
                value = result.stdout.strip()
                payload = card["payload"]
                approved = (payload["kind"] == "approval" and value == "yes"
                            and any(c["key"] == "yes" for c in payload["choices"]))
                answers.append(Answer(card["id"], value, approved))
            except (RuntimeError, ChannelRejected, OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"{card['external']}: {exc}")
                print(f"车间卡片轮询失败 {failures[-1]}", file=sys.stderr)
        if checked and len(failures) == checked:
            raise RuntimeError("; ".join(failures))
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
