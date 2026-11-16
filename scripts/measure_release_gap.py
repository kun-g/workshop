"""只读采样心跳；最多等待指定秒数，保存原样样本并核对新旧进程空窗。

uv run python scripts/measure_release_gap.py --output /absolute/new-file.jsonl
在预期重启前启动（默认 30 秒窗口），不会发版或重启，不调用 pgrep。
首次升级旧版没有 process 标记时仍报告时间差，但身份不全时不判通过。
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time


def gap(before, after):
    current = after.get('process', {})
    old = current.get('previous', {})
    if not current.get('first_at') or not old.get('at') or not before.get('at'):
        return None
    first, last, initial = (datetime.fromisoformat(v) for v in (current['first_at'], old['at'], before['at']))
    if first <= initial or last < initial or before.get('process', {}).get('first_at') == current['first_at']:
        return None
    seconds = (first - last).total_seconds()
    identified = bool(old.get('pid') and current.get('pid') and old.get('version') and current.get('version'))
    return {'old': old, 'new': {k: current.get(k) for k in ('pid', 'version', 'first_at')},
            'gap_seconds': seconds, 'identities_complete': identified,
            'passed': identified and 0 <= seconds <= 1}


def sample(directory, output, timeout=30):
    if timeout <= 0:
        raise ValueError('timeout 必须为正数')
    heartbeat = directory / 'heartbeat.json'
    before = json.loads(heartbeat.read_text())
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    with output.open('x') as out:
        out.write(json.dumps({'initial': before}, ensure_ascii=False) + '\n')
        while True:
            try:
                raw = heartbeat.read_text()
                row = json.loads(raw)
                result = gap(before, row)
                record = {'observed_at': datetime.now(timezone.utc).isoformat(), 'heartbeat_raw': raw}
            except (OSError, ValueError) as exc:
                result, record = None, {'error': str(exc)}
            out.write(json.dumps(record, ensure_ascii=False) + '\n')
            if result is not None:
                out.write(json.dumps({'result': result}, ensure_ascii=False) + '\n')
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {'passed': False, 'reason': '窗口内未取得完整重启边界；另择窗口重测'}
            pause.wait(min(0.05, remaining))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/workshop')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    result = sample(args.state_dir, args.output, args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
