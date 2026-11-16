"""只读备份状态库后，在隔离目录跑一拍。所有外部作业只登记，绝不起进程。

uv run python scripts/measure_tick_copy.py --output /absolute/new-directory
输出为参考数据；冷作业缓存，不代替真实线上负载，不设耗时通过阈值。
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import time

from workshop.core.engine import Engine
from workshop.core.jobs import Jobs, write
from workshop.core.log import Log
from workshop.core.models import STAGES
from workshop.core.rules import Rules


class NoExternalJobs(Jobs):
    def start(self, path, spec):
        path.mkdir(parents=True, exist_ok=True)
        write(path / "spec.json", spec)
        self.spawned += 1


class NoExternalPlugins:
    def get(self, port, name):
        return self

    def stages(self, target):
        return tuple(target.get("stages", STAGES[:-1]))

    def cleanup(self, *args):
        raise AssertionError("副本测量不得同步执行插件")

    hold_workdir = release = cleanup


def measure(source, output, ticks=1):
    if ticks < 1:
        raise ValueError("ticks 必须为正数")
    source, output = source.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    destination = output / "workshop.db"
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination)) as copy:
            original.backup(copy)
    with closing(sqlite3.connect(destination)) as db:
        before = {table: dict(zip(("rows", "body_bytes"), db.execute(
            f"SELECT count(*), coalesce(sum(length(CAST(body AS BLOB))),0) FROM {table}").fetchone()))
                  for table in ("items", "rounds", "facts")}
        inline = db.execute("SELECT count(*) FROM rounds WHERE json_type(body, '$.brief') IS NOT NULL").fetchone()[0]
    opened = time.monotonic()
    with closing(Log(destination)) as log:
        storage_ms = round((time.monotonic() - opened) * 1000, 3)
        initialized = time.monotonic()
        saved = log.get("controls", "@rules")
        if not saved:
            raise ValueError("副本没有规则快照")
        rules = Rules(Rules.validate(saved["data"]).data)
        jobs = NoExternalJobs(output / "jobs", ["never-started"])
        try:
            engine = Engine(log, rules, NoExternalPlugins(), jobs=jobs)
            engine_init_ms = round((time.monotonic() - initialized) * 1000, 3)
            samples = []
            for _ in range(ticks):
                engine.tick()
                if engine.control("@tick").get("failures"):
                    raise RuntimeError("副本 tick 失败，详见副本事实")
                heartbeat = json.loads((output / "heartbeat.json").read_text())
                samples.append(heartbeat["tick_ms"])
            after = log.db.execute("SELECT coalesce(sum(length(CAST(body AS BLOB))),0) FROM rounds").fetchone()[0]
            items_after = log.db.execute("SELECT coalesce(sum(length(CAST(body AS BLOB))),0) FROM items").fetchone()[0]
            remaining = log.db.execute("SELECT count(*) FROM rounds WHERE json_type(body, '$.brief') IS NOT NULL").fetchone()[0]
            result = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                      "conditions": "只读 SQLite backup；冷作业缓存；外部调用只登记为等待，不起进程；迁移不计入 tick_ms",
                      "before": before, "rounds_body_bytes_after": after, "items_body_bytes_after": items_after,
                      "inline_briefs_before": inline, "inline_briefs_after": remaining,
                      "storage_open_ms": storage_ms, "engine_init_ms": engine_init_ms,
                      "tick_samples_ms": samples, "tick_ms": samples[0], "active": heartbeat["active"],
                      "pending_jobs": len(list((output / "jobs").glob("*/spec.json")))}
            (output / "measurement.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            return result
        finally:
            jobs.launcher.shutdown(wait=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.home() / ".local/state/workshop/workshop.db")
    parser.add_argument("--output", type=Path, required=True, help="尚不存在的独立目录")
    parser.add_argument("--ticks", type=int, default=1, help="副本连续拍数；不执行外部作业")
    args = parser.parse_args()
    print(json.dumps(measure(args.source, args.output, args.ticks), ensure_ascii=False, indent=2))
