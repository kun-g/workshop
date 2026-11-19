"""给状态里记了合并的 PR、但没记 PR 概况的活补上概况（issue #300）。

概要是「改动文件数、增加行数、删除行数、改了什么」，合并时由交付插件记进活状态；老活没有这一笔，
`workshop usage --by kind|size` 只能把它们归到「未知」。这个脚本用交付对象规则里的 App 身份只读
GitHub，按 PR 号补上。

默认只打印：每件一行（活号、PR 号、算出的概况），最后一行是合计。加 --apply 才写，这时必须同时
用参数给出状态库和插件状态目录，给的是生产目录就拒绝：生产状态由协调对话在合并后处理。
读写状态文件走 issue 交付插件现有的加锁和原子写；重复跑不重复记。
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from workshop import github_identity
from workshop.core.log import Log
from workshop.plugins.issue_delivery import stats as prstats
from workshop.plugins.issue_delivery.io import IO

PRODUCTION = Path("~/.local/state/workshop").expanduser().resolve()
DEFAULT_DB = PRODUCTION / "workshop.db"
DEFAULT_DIRECTORY = PRODUCTION / "issue_delivery"

ORDER = (prstats.DOC, prstats.TEST, prstats.PROGRAM)


def state_path(directory, item_id):
    return directory / (hashlib.sha256(item_id.encode()).hexdigest() + ".json")


def read_state(path):
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def pending(log, directory):
    """每件活还缺概况的已合并 PR：[(活号, 交付对象, PR 号)]。"""
    result = []
    for row in log.all("items", fields=("id", "target")):
        state = read_state(state_path(directory, row["id"]))
        number = (state or {}).get("pr")
        if not state or not state.get("merge") or type(number) is not int:
            continue
        recorded = state.get("pr_stats")
        if isinstance(recorded, dict) and str(number) in recorded:
            continue
        result.append((row["id"], row.get("target"), number))
    return result


def fetch(target, io, number):
    pr = io.api(f'repos/{target["repo"]}/pulls/{number}')
    files = io.pages(f'repos/{target["repo"]}/pulls/{number}/files?per_page=100')
    return prstats.classify(files, pr.get("changed_files"))


@github_identity.scoped
def fetch_all(target, io, numbers):
    return {number: fetch(target, io, number) for number in numbers}


def describe(summary):
    return f'文件 {summary["files"]}、+{summary["additions"]}/-{summary["deletions"]}、{summary["kind"]}'


def record(io, item_id, target, number, summary):
    """按 PR 号记进活状态；同一个 PR 已经在的不再写。"""
    item = {"id": item_id}
    with io.locked(item, target) as state:
        recorded = state.setdefault("pr_stats", {})
        if str(number) in recorded:
            return False
        recorded[str(number)] = summary
        io.save(item, target, state)
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-db", type=Path, help=f"状态库；默认 {DEFAULT_DB}")
    parser.add_argument("--issue-delivery-state-dir", type=Path,
                        help=f"issue 交付的活状态目录；默认 {DEFAULT_DIRECTORY}")
    parser.add_argument("--apply", action="store_true", help="真的写；必须同时给出状态库和插件状态目录")
    args = parser.parse_args(argv)
    if args.apply and (args.state_db is None or args.issue_delivery_state_dir is None):
        raise SystemExit("--apply 必须同时用 --state-db 和 --issue-delivery-state-dir 给出副本路径")
    state_db = (args.state_db or DEFAULT_DB).expanduser().resolve()
    directory = (args.issue_delivery_state_dir or DEFAULT_DIRECTORY).expanduser().resolve()
    if args.apply:
        for path in (state_db, directory):
            if path.is_relative_to(PRODUCTION):
                raise SystemExit(f"拒绝：{path} 在生产目录下；补历史数据只对副本写")
    log = Log.readonly(state_db)
    try:
        saved = log.get("controls", "@rules") or {}
        targets = (saved.get("data") or {}).get("targets") or {}
        todo = {}
        for item_id, name, number in pending(log, directory):
            target = targets.get(name)
            if not isinstance(target, dict) or not target.get("repo"):
                raise SystemExit(f"{item_id} 的交付对象 {name!r} 在规则里没有登记，不能查 GitHub")
            todo.setdefault((item_id, name), []).append(number)
        io = IO(state_dir=directory)
        done = {}
        for (item_id, name), numbers in todo.items():
            target = targets[name]
            summaries = fetch_all(target, io, numbers)
            for number in numbers:
                print(f'{item_id}\t#{number}\t{describe(summaries[number])}')
                if args.apply:
                    record(io, item_id, target, number, summaries[number])
            merged = dict((read_state(state_path(directory, item_id)) or {}).get("pr_stats") or {})
            merged.update({str(number): summary for number, summary in summaries.items()})
            done[item_id] = prstats.combine(merged)
    finally:
        log.close()
    counts = {kind: 0 for kind in (*ORDER, prstats.UNKNOWN)}
    for summary in done.values():
        kind = (summary or {}).get("kind")
        counts[kind if kind in counts else prstats.UNKNOWN] += 1
    print(f'合计：补 {len(done)} 件；只改文档 {counts[prstats.DOC]}；只改测试和文档 {counts[prstats.TEST]}；'
          f'改了程序 {counts[prstats.PROGRAM]}；未知 {counts[prstats.UNKNOWN]}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
