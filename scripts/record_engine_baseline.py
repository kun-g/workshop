"""用现有测试把注入了 conftest.Clock 的引擎录成摘要。

用法：
  uv run python scripts/record_engine_baseline.py          # 重写 tests/baseline/schema.json
  uv run python scripts/record_engine_baseline.py --check  # 重录并核对已提交的摘要

完整 JSON 不入库。摘要是每个测试规范化结果的 sha256。
pytest 不过就失败，不写半截结果。

scripts/check.sh 不跑这个核对。拆 src/workshop/core/engine.py 的人在合并前自己跑
`--check`，把输出贴进 PR。摘要不一致就停，不要改 tests/baseline/schema.json 去迁就新代码。
改代码前先跑一次 `--check`：通过时全文会留在 `.local/baseline-json/`。改完再跑，
失败时会拿这份全文和这次的录制做 diff。没有这份全文时，只给出这次录到的 JSON 路径。

下面这几个测试的事实条数跟着真子进程耗时走，同一注入时钟下两次录制对不齐，不收进摘要。
"""
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tests" / "baseline"
LOCAL_JSON = ROOT / ".local" / "baseline-json"
MISMATCH_JSON = ROOT / ".local" / "baseline-mismatch"
# 这些测试用真进程和 time.monotonic 轮询。提交条数、心跳条数、活的状态会随墙上时间变。
OMITTED = {
    "tests/test_jobs.py::test_check_timeout_in_job_records_check_failed":
        "检查超时跟着真子进程耗时走",
    "tests/test_jobs.py::test_native_process_creation_does_not_hold_tick":
        "进程创建耗时不由注入时钟决定",
    "tests/test_jobs.py::test_restart_recovers_running_or_finished_check_without_rerun[False]":
        "重启时检查是否已跑完，取决于真进程进度",
    "tests/test_jobs.py::test_restart_recovers_running_or_finished_check_without_rerun[True]":
        "重启时检查是否已跑完，取决于真进程进度",
    "tests/test_jobs.py::test_slow_process_creation_does_not_hold_tick":
        "慢速创建进程的拍数随墙上时间变化",
}


def main():
    check = "--check" in sys.argv
    positionals = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    out = Path(positionals[0]).resolve() if positionals else DEFAULT_OUT
    parts = Path(tempfile.mkdtemp(prefix="workshop-baseline-"))
    try:
        run_pytest(parts)
        schema, documents = build_schema(parts)
        if check:
            expected_path = DEFAULT_OUT / "schema.json"
            expected = json.loads(expected_path.read_text())
            problems, mismatched = diff_schema(expected, schema)
            if problems:
                print("\n".join(problems))
                emit_mismatch(documents, mismatched)
                raise SystemExit(1)
            save_local_json(documents)
            print(f"基线摘要一致：{len(schema['digests'])} 个测试")
            return
        write_schema(schema, out)
        if out == DEFAULT_OUT:
            save_local_json(documents)
        print(f"录了 {len(schema['digests'])} 个测试，排除 {len(schema['omitted'])} 个，"
              f"没录 {len(schema['unrecorded'])} 个 → {out / 'schema.json'}")
    finally:
        shutil.rmtree(parts, ignore_errors=True)


def run_pytest(parts):
    # 本机没有 claude 时，收回工作目录的执行者测试会在启动前就判不可用。
    # 只在找不到真命令时放替身，并且放在这次录制自己的临时目录里。
    env = os.environ.copy()
    claude = shutil.which("claude", path=env.get("PATH"))
    if claude is None:
        stub = parts / "stub-bin"
        stub.mkdir()
        binary = stub / "claude"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        env["PATH"] = str(stub) + os.pathsep + env.get("PATH", "")
    env["WORKSHOP_RECORD_BASELINE"] = "1"
    env["WORKSHOP_BASELINE_PARTS"] = str(parts)
    env["WORKSHOP_BASELINE_BOUND"] = str(parts / "bound")
    hook = str(ROOT / "tests" / "baseline_hook")
    env["PYTHONPATH"] = hook + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONHASHSEED"] = "0"
    completed = subprocess.run(
        # 并行太满时，执行者替身还在写日志，测试就会读到半行。收成两路，录制仍按测试隔离。
        ["uv", "run", "pytest", "-q", "-n", "2", "--dist", "worksteal"],
        cwd=ROOT,
        env=env,
    )
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def build_schema(parts):
    documents = [json.loads(path.read_text()) for path in sorted(parts.glob("*.json"))]
    documents.sort(key=lambda document: document["nodeid"])
    seen = {path.read_text().strip() for path in (parts / "nodeids").glob("*.txt")} if (parts / "nodeids").exists() else set()
    recorded = {document["nodeid"] for document in documents}
    missing = sorted(set(OMITTED) - recorded)
    if missing:
        raise SystemExit("基线排除名单里的测试这次没录到：" + "，".join(missing))
    kept = [document for document in documents if document["nodeid"] not in OMITTED]
    digests = {}
    rendered = {}
    for document in kept:
        body = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        digests[document["nodeid"]] = hashlib.sha256(body.encode()).hexdigest()
        rendered[document["nodeid"]] = body
    schema = {
        "schema": 2,
        "clock": "tests/conftest.py Clock，起点 2026-11-13T08:00:00+00:00",
        "stored": "入库的是每个测试规范化结果的 sha256。完整 JSON 不入库。拆 engine.py 的人在合并前跑 scripts/record_engine_baseline.py --check，不进 scripts/check.sh。",
        "what": [
            "commits：同一次 transaction 提交里的事实行。stored 为 false 是 ON CONFLICT(source, ext_id) DO NOTHING 丢掉的尝试。命令行子进程和没带上 pytest 上下文的线程写进同一条库的提交也算，按事实序号插入。",
            "ext_id_origin：uuid 是 log.append 自己生成的 ext_id；literal 是调用方传入的，含代码写死的。",
            "projection：这些事实投影出的活、卡、轮、控制。测试结束时按库文件重读。",
            "heartbeats：同一时钟下写出的 heartbeat.json，按写出顺序。",
            "restart：after 见到重启时 restart_requested 的进程内轨迹；in_facts / in_heartbeat / in_projection 为假表示这个键没有写进库。",
        ],
        "normalization": {
            "uuid": "带横线的 uuid，以及 uuid4().hex 这种 32 位无横线写法，按本测试首次出现顺序换成 $uuid:N；通知标题里被截断的前缀换成同一个记号。不是 uuid4 版式的 32 位十六进制保持原样。ext_id_origin 仍标明它是生成的还是调用方传入的",
            "sha": "40 位十六进制换成 $sha:N，64 位换成 $hash:N，通道范围那 24 位换成 $id:N；截断前缀沿用同一记号",
            "time": "conftest.Clock 这次真正返回的时刻，以及从这些时刻加上或减去 timedelta 得到的时刻，写成相对起点的偏移，如 +0s、+30m、+2h、-10m。其余时刻，包括恰好落在整秒上的墙上时钟，按本测试首次出现顺序编成 $time:N",
            "paths": "pytest 临时目录换成 $pytest，单个测试的 tmp_path 换成 $testtmp，仓库根换成 $repo，解释器路径换成 $python。/tmp 和 /private/tmp 下的 ws-wake-* 都换成 $tmpdir",
            "elapsed": "heartbeat 的 tick_ms、wake_ms 若是数字则换成 $elapsed，null 保持 null",
        },
        "omitted": [{"nodeid": nodeid, "reason": OMITTED[nodeid]} for nodeid in sorted(OMITTED)],
        "digests": digests,
        "unrecorded": sorted(seen - recorded),
    }
    return schema, rendered


def diff_schema(expected, actual):
    problems = []
    mismatched = []
    if expected.get("digests") != actual["digests"]:
        old = expected.get("digests") or {}
        new = actual["digests"]
        for nodeid in sorted(set(old) | set(new)):
            if old.get(nodeid) != new.get(nodeid):
                mismatched.append(nodeid)
                problems.append(f"摘要不同：{nodeid}")
                if len(problems) >= 30:
                    problems.append("还有更多测试不一致")
                    break
    if expected.get("unrecorded") != actual["unrecorded"]:
        problems.append("没录的测试名单和已提交的摘要不一致")
        old = set(expected.get("unrecorded") or [])
        new = set(actual["unrecorded"])
        for nodeid in sorted(new - old)[:30]:
            problems.append(f"多了没录的测试：{nodeid}")
        for nodeid in sorted(old - new)[:30]:
            problems.append(f"少了没录的测试：{nodeid}")
    if expected.get("omitted") != actual["omitted"]:
        problems.append("排除名单和已提交的摘要不一致")
    return problems, mismatched


def _json_name(nodeid):
    return hashlib.sha256(nodeid.encode()).hexdigest() + ".json"


def save_local_json(rendered):
    """通过核对或重写仓库摘要时，把全文留在 .local，供下一次失败时 diff。不入库。"""
    if LOCAL_JSON.exists():
        shutil.rmtree(LOCAL_JSON)
    LOCAL_JSON.mkdir(parents=True)
    for nodeid, body in rendered.items():
        (LOCAL_JSON / _json_name(nodeid)).write_text(body)


def emit_mismatch(rendered, nodeids):
    if MISMATCH_JSON.exists():
        shutil.rmtree(MISMATCH_JSON)
    MISMATCH_JSON.mkdir(parents=True)
    shown = nodeids[:30]
    if not LOCAL_JSON.exists():
        print("工作区没有上一份完整 JSON。改代码前先跑一次不带改动的 --check，通过后全文会留在 "
              f"{LOCAL_JSON}。这次只给出新录制的路径。")
    for nodeid in shown:
        body = rendered.get(nodeid)
        if body is None:
            print(f"这次没录到：{nodeid}")
            continue
        path = MISMATCH_JSON / _json_name(nodeid)
        path.write_text(body)
        print(f"摘要不同：{nodeid}")
        print(f"新录制：{path}")
        previous = LOCAL_JSON / _json_name(nodeid)
        if not previous.exists():
            continue
        diff = list(difflib.unified_diff(
            previous.read_text().splitlines(), body.splitlines(),
            fromfile=str(previous), tofile=str(path), lineterm="",
        ))
        if len(diff) > 200:
            diff = diff[:200] + ["... 其余省略"]
        print("\n".join(diff))


def write_schema(schema, out):
    out.mkdir(parents=True, exist_ok=True)
    cases = out / "cases"
    if cases.exists():
        shutil.rmtree(cases)
    target = out / "schema.json"
    temporary = out / "schema.json.tmp"
    temporary.write_text(json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    temporary.replace(target)


if __name__ == "__main__":
    main()
