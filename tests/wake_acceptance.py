"""手动运行的真实进程验收；60 秒空闲观察不加入日常 pytest。

uv run python tests/wake_acceptance.py
全部状态放临时目录，插件是假实现，不访问外部服务。
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from workshop import cli
from workshop.core.jobs import FRESH, Jobs
from workshop.core.log import Log
from workshop.core.models import Classification, Poll
from workshop.registry import Registry


class Source:
    def poll(self, target, cursor):
        time.sleep(0.2)  # 验收规定的假作业延迟，仅用于本手动脚本。
        return Poll([], "completed")


class Channel:
    def wake_paths(self, target):
        return [str(Path(os.environ["WAKE_ACCEPTANCE_STATE"]) / "answer")]

    def poll(self, cards):
        root = Path(os.environ["WAKE_ACCEPTANCE_STATE"])
        source = root / "answer"
        (root / "polled").write_text(json.dumps({"value": source.read_text() if source.exists() else "",
                                                "at": time.monotonic()}))
        return []


class Flow:
    def classify(self, event, item, target, find_external=None):
        root = Path(os.environ["WAKE_ACCEPTANCE_STATE"])
        (root / "processed").write_text(str(time.monotonic()))
        return Classification("update", discard=True)


def registry(*args):
    result = Registry()
    result.register("source", "fake", Source())
    result.register("channel", "fake", Channel())
    result.register("flow", "fake", Flow())
    return result


def service():
    # 仅替换插件装配；循环、信箱、CLI、作业启动和结果消费均用生产实现。
    cli.Registry = registry
    class TracedEngine(cli.Engine):
        def wait_timeout(self):
            value = super().wait_timeout()
            with (Path(os.environ["WAKE_ACCEPTANCE_STATE"]) / "waits.jsonl").open("a") as out:
                out.write(json.dumps({"at": time.time(), "wait": value, "due": self.jobs.due}) + "\n")
            return value
    cli.Engine = TracedEngine
    FRESH["source", "poll"] = FRESH["channel", "poll"] = 120
    class ServiceJobs(Jobs):
        def __init__(self, directory, command):
            super().__init__(directory, [sys.executable, __file__, "--service", *command[3:]])
    cli.Jobs = ServiceJobs
    return cli.main(sys.argv[2:])


def wait_for(test, timeout=10):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = test()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("验收超时")


def read(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def main():
    with tempfile.TemporaryDirectory(prefix="ws-accept-", dir="/tmp") as directory:
        root = Path(directory)
        config = {"targets": {"alpha": {"plugin": "fake", "sources": ["fake"]}},
                  "channel": "fake", "executors": {r: {"use": "fake", "billing": "subscription"} for r in ("writer", "reviewer", "acceptor")},
                  "limits": {"concurrent": 1, "chain_depth": 8}, "ask_first": [], "skip_ask": []}
        rules = root / "rules.json"
        rules.write_text(json.dumps(config))
        env = {**os.environ, "WAKE_ACCEPTANCE_STATE": str(root)}
        env.pop("WORKSHOP_ROUND_TOKEN", None)
        prefix = [sys.executable, "-m", "workshop.cli", "--state-dir", str(root), "--rules", str(rules)]
        output = (root / "daemon.log").open("w")
        daemon = subprocess.Popen([sys.executable, __file__, "--service", *prefix[3:], "run"], env=env, stdout=output, stderr=output)
        try:
            wait_for(lambda: (root / "wake.sock").exists())
            log = Log(root / "workshop.db")
            try:
                wait_for(lambda: log.get("cursors", "alpha:fake"))
                source_path = next(p for p in (root / "jobs").iterdir() if read(p / "spec.json").get("port") == "source")
                finished = read(source_path / "result.json")["finished"]
                consumed = time.time()
                assert consumed - finished <= 1
                print(f'作业结果写入后消费耗时：{(consumed-finished)*1000:.3f} ms', flush=True)
            finally:
                log.close()
            payload = {"source": "acceptance", "ext_id": "one", "target": "alpha", "object_key": "one", "title": "test", "body": "data"}
            command = subprocess.run([*prefix, "event", "--json", json.dumps(payload)], env=env, capture_output=True, text=True)
            returned = time.monotonic()
            assert command.returncode == 0, command.stderr
            processed = wait_for(lambda: read(root / "processed"))
            event_ms = max(0, processed - returned) * 1000
            assert event_ms <= 1000
            print(f'事件命令返回后开始处理：{event_ms:.3f} ms', flush=True)
            wait_for(lambda: read(root / "polled"))
            changed = time.monotonic()
            replacement = root / "answer.tmp"
            replacement.write_text("clicked")
            replacement.replace(root / "answer")
            value = wait_for(lambda: (v if (v := read(root / "polled")).get("value") == "clicked" else None))
            file_ms = (value["at"] - changed) * 1000
            assert file_ms <= 1000
            print(f'通道文件原子替换后重查：{file_ms:.3f} ms（未等 20 秒新鲜期）', flush=True)
            # 已结束的启动/结果信都消费后，观察无新信时的下一次心跳。
            time.sleep(0.3)
            baseline = read(root / "heartbeat.json")
            start = time.monotonic()
            time.sleep(30)
            assert read(root / "heartbeat.json")["at"] == baseline["at"], "空闲期间不应固定轮询"
            heartbeat = wait_for(lambda: (b if (b := read(root / "heartbeat.json")).get("at") != baseline["at"] else None), 31)
            elapsed = time.monotonic() - start
            assert elapsed <= 60
            print(f'无新信 30 秒没有空转，{elapsed:.3f} s 内更新心跳；wake_ms={baseline["wake_ms"]}', flush=True)
            assert daemon.poll() is None
            daemon.terminate()
            daemon.wait(timeout=5)
            payload["ext_id"] = "offline"
            result = subprocess.run([*prefix, "event", "--json", json.dumps(payload)], env=env, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            print('守护进程不在：workshop event 正常退出（exit 0）', flush=True)
        finally:
            if daemon.poll() is None:
                daemon.terminate()
                daemon.wait(timeout=5)
            output.close()
            text = (root / "daemon.log").read_text()
            if text:
                print('daemon stdout/stderr:\n' + text)
            if sys.exc_info()[0] is not None:
                print((root / 'waits.jsonl').read_text())


if __name__ == "__main__":
    raise SystemExit(service() if sys.argv[1:2] == ["--service"] else main())
