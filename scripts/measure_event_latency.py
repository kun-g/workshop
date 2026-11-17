"""在真实状态库上量 R27：诊断事件的记录时间到守护进程开始归件。

uv run python scripts/measure_event_latency.py --output /absolute/new.json --count 20 --gap 30

每个样本走真实入口 `workshop event --file`（默认用主检出的解释器，即生产代码），
事件是 cli:orca 对 workshop:75 的诊断更新：流程只把它归到原活当资料，不改阶段和状态。
口径（核心「按默认定」）：事件事实的 at 到守护进程登记 flow.classify 作业的 spec.started；
另记到 event_classified 事实的时间。观察期间另开线程读 heartbeat.json，记下每一拍的 tick_ms，
并原样带上观察窗口里 tick-slow.jsonl 新增的记录。只读状态库，不改守护进程。
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import statistics
import subprocess
import threading
import time
import uuid

from workshop.core.jobs import digest

STATE = Path("~/.local/state/workshop").expanduser()
REPO = Path("~/workspace/runtime/repos/workshop").expanduser()


def percentile(values, share):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(share * len(ordered) + 0.5) - 1))]


def summary(values):
    if not values:
        return None
    return {"n": len(values), "median": round(statistics.median(values), 3),
            "p95": round(percentile(values, 0.95), 3), "max": round(max(values), 3)}


class Beats(threading.Thread):
    """每 10 毫秒看一次心跳文件；每拍都换 at，只记新的。"""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.rows, self.stop = path, {}, threading.Event()

    def run(self):
        while not self.stop.wait(0.01):
            try:
                beat = json.loads(self.path.read_text())
            except (OSError, ValueError):
                continue
            self.rows.setdefault(beat.get("at"), {"at": beat.get("at"), "tick_ms": beat.get("tick_ms"),
                                                  "wake_ms": beat.get("wake_ms"),
                                                  "version": (beat.get("process") or {}).get("version")})


def probe(state, cli, conn, label, timeout):
    probe_id = f"r27-{label}-{uuid.uuid4()}"
    event = {"source": "orca", "ext_id": probe_id, "target": "workshop", "object_key": "75",
             "title": "R27 事件延迟取证（诊断事件）",
             "body": {"item_id": "workshop:75", "diagnostic": True,
                      "purpose": "Measure event-to-handling latency; no human instruction or approval."}}
    folder = state.parent / "workshop-r27-probes"
    folder.mkdir(exist_ok=True)
    path = folder / f"{probe_id}.json"
    path.write_text(json.dumps(event, ensure_ascii=False))
    baseline = conn.execute("SELECT max(seq) FROM facts").fetchone()[0]
    result = subprocess.run([*cli, "--state-dir", str(state), "event", "--file", str(path)],
                            capture_output=True, text=True, timeout=30)
    path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"event 退出 {result.returncode}: {result.stderr}")
    row = conn.execute("SELECT seq, at FROM facts WHERE seq>? AND kind='event' AND source='cli:orca' "
                       "AND json_extract(body, '$.ext_id')=?", (baseline, probe_id)).fetchone()
    entry = json.loads(conn.execute("SELECT body FROM effects WHERE id=?", (f"classify:{row[0]}",)).fetchone()[0])
    job = state / "jobs" / digest("flow", "issue_delivery", "classify", [entry["id"], entry["event"]])
    started = classified = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and (started is None or classified is None):
        if started is None:
            try:
                started = json.loads((job / "spec.json").read_text())["started"]
            except (OSError, ValueError, KeyError):
                pass
        if classified is None:
            found = conn.execute("SELECT seq, kind, at FROM facts WHERE seq>? AND kind IN ('event_classified','event_discarded') "
                                 "AND json_extract(body, '$.event_seq')=? LIMIT 1", (row[0], row[0])).fetchone()
            classified = dict(found) if found else None
        time.sleep(0.01)
    recorded = datetime.fromisoformat(row[1]).timestamp()
    sample = {"probe_id": probe_id, "stdout": result.stdout.strip(), "event_seq": row[0], "event_at": row[1],
              "classification_call_at": datetime.fromtimestamp(started, timezone.utc).isoformat() if started else None,
              "classification_fact": classified,
              "record_to_classification_call_ms": round((started - recorded) * 1000, 3) if started else None,
              "record_to_classification_fact_ms": round((datetime.fromisoformat(classified["at"]).timestamp() - recorded) * 1000, 3)
              if classified else None}
    return sample


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--gap", type=float, default=30, help="两个样本之间隔几秒")
    parser.add_argument("--state-dir", type=Path, default=STATE)
    parser.add_argument("--cli", nargs="+", default=[str(REPO / ".venv/bin/python"), "-m", "workshop.cli"])
    parser.add_argument("--label", default="probe")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"{args.output} 已存在")
    state = args.state_dir.resolve()
    conn = sqlite3.connect(f"file:{state / 'workshop.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    slow_path = state / "tick-slow.jsonl"
    slow_before = set(slow_path.read_text().splitlines()) if slow_path.exists() else set()
    beats = Beats(state / "heartbeat.json")
    beats.start()
    began = datetime.now(timezone.utc).isoformat()
    samples = []
    next_at = time.monotonic()
    for index in range(args.count):
        while time.monotonic() < next_at:
            time.sleep(min(0.05, next_at - time.monotonic()))
        next_at = time.monotonic() + args.gap
        sample = probe(state, args.cli, conn, args.label, args.timeout)
        samples.append(sample)
        print(json.dumps({k: sample[k] for k in ("event_seq", "event_at", "classification_call_at",
                                                 "record_to_classification_call_ms", "record_to_classification_fact_ms")},
                         ensure_ascii=False), flush=True)
    ended = datetime.now(timezone.utc).isoformat()
    beats.stop.set()
    beats.join()
    slow_after = slow_path.read_text().splitlines() if slow_path.exists() else []
    slow = [json.loads(line) for line in slow_after if line not in slow_before and line.strip()]
    ticks = [b for b in beats.rows.values() if b["at"] and began <= b["at"] <= ended]
    report = {"window": {"from": began, "to": ended}, "cli": args.cli, "state_dir": str(state),
              "versions": sorted({b["version"] for b in ticks if b["version"]}),
              "call_ms": summary([s["record_to_classification_call_ms"] for s in samples if s["record_to_classification_call_ms"] is not None]),
              "fact_ms": summary([s["record_to_classification_fact_ms"] for s in samples if s["record_to_classification_fact_ms"] is not None]),
              "missing": [s["probe_id"] for s in samples if s["record_to_classification_call_ms"] is None],
              "heartbeats": {"seen": len(ticks), "tick_ms": summary([b["tick_ms"] for b in ticks if b["tick_ms"] is not None]),
                             "over_100": [b for b in ticks if (b["tick_ms"] or 0) > 100]},
              "tick_slow_new": slow, "samples": samples, "heartbeat_rows": ticks}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("window", "versions", "call_ms", "fact_ms", "missing")}, ensure_ascii=False))
    print(json.dumps({"heartbeats": {k: v for k, v in report["heartbeats"].items()}, "tick_slow_new": len(slow)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
