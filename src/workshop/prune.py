"""按保留规则把车间状态目录里的存量清一次。

和守护进程里的清理用同一套规则：执行者轮次留 3 天、项目经理作业结果留 1 小时、
项目经理会话目录留 7 天；执行者的工作区只在活已结束、PR 已合并且没有未提交改动时收回。
默认先列再删；`--dry-run` 只列不删。规则细节见 docs/implementation/执行者.md。
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time

from .core.jobs import KEEP_SECONDS, sweep_directory
from .core.managers import MANAGER_JOBS_KEEP_SECONDS, MANAGER_SESSION_KEEP_SECONDS, sweep_sessions
from .core.ports import Retryable

WORKTREE = re.compile(r'ws-[0-9a-f]{16}(?:-\d+)?')


@contextmanager
def locked(directory):
    """和守护进程的一拍用同一把锁；持锁期间活不会变，删除不会撞上返工。"""
    with (Path(directory) / 'workshop.db.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def human(size):
    value = float(size)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} GB'


def _concerns(directory):
    """只读打开 managers.db；读不到就当没有当前事项。"""
    path = Path(directory) / 'managers.db'
    if not path.exists():
        return []
    for query in ('?mode=ro', '?immutable=1'):
        try:
            connection = sqlite3.connect(path.as_uri() + query, uri=True, timeout=0)
            try:
                return [json.loads(row[0]) for row in connection.execute('SELECT body FROM concerns')]
            finally:
                connection.close()
        except (sqlite3.Error, ValueError, TypeError):
            continue
    print(f'车间 prune：读不了 {path}，项目经理会话按没有当前事项处理', file=sys.stderr)
    return []


def protected_episodes(directory):
    return {row.get('episode') for row in _concerns(directory)
            if row.get('episode') and (row.get('active') or row.get('token'))}


def ended_workdirs(log):
    return [json.loads(row[0]) for row in log.db.execute(
        "SELECT body FROM items WHERE json_extract(body, '$.state') IN ('完成', '放弃') "
        "AND coalesce(json_extract(body, '$.workdir'), '') != ''")]


def known_workdirs(log):
    result = {}
    for row in log.db.execute("SELECT body FROM items WHERE coalesce(json_extract(body, '$.workdir'), '') != ''"):
        item = json.loads(row[0])
        result[str(Path(item['workdir']).resolve())] = item
    return result


def _git(checkout, *args):
    import subprocess
    return subprocess.run(['/usr/bin/git', '-C', str(checkout), *args],
                          capture_output=True, text=True, timeout=60)


def checkouts(rules):
    seen = set()
    for name in rules.data['targets']:
        path = rules.target(name).get('checkout')
        if isinstance(path, str) and path.strip():
            seen.add(str(Path(path).expanduser().resolve()))
    return sorted(seen)


def worktree_entries(checkout):
    result = _git(checkout, 'worktree', 'list', '--porcelain')
    if result.returncode:
        raise ValueError(f'读不了 {checkout} 的工作区清单：{result.stderr.strip()[-300:]}')
    entries = []
    for block in result.stdout.split('\n\n'):
        fields = dict(line.split(' ', 1) if ' ' in line else (line, '') for line in block.splitlines())
        if fields.get('worktree'):
            entries.append(fields)
    return entries


def plan_worktrees(log, rules, registry):
    """返回要删的、留着的、以及没有对应活的执行者工作区。"""
    ended = ended_workdirs(log)
    known = known_workdirs(log)
    candidates, kept = [], []
    seen = set()
    for item in ended:
        try:
            target = rules.target(item['target'], item.get('flow', ''))
        except (KeyError, ValueError):
            kept.append((Path(item['workdir']), '交付对象已不在规则里，没删'))
            continue
        # 和看门一样先问流程：流程说还要留（例如 PR 还开着）就不进入执行者的收回判断。
        try:
            flow = registry.get('flow', target['plugin'])
        except (LookupError, ValueError, KeyError):
            flow = None
        hold = getattr(flow, 'hold_workdir', None)
        if hold:
            try:
                reason = hold(item, target)
            except Retryable as exc:
                kept.append((Path(item['workdir']), f'流程查不了：{exc}'))
                continue
            except Exception as exc:
                kept.append((Path(item['workdir']), f'流程检查失败：{type(exc).__name__}: {exc}'))
                continue
            if reason and not isinstance(reason, str):
                kept.append((Path(item['workdir']), '流程留工作目录的原因不是字符串，没删'))
                continue
            if reason:
                kept.append((Path(item['workdir']), reason))
                continue
        for name in sorted(item.get('sessions') or {}):
            try:
                executor = registry.get('executor', name)
            except (LookupError, ValueError):
                continue
            release = getattr(executor, 'release', None)
            if not release:
                continue
            try:
                result = release(item, target, dry_run=True)
            except Retryable as exc:
                kept.append((Path(item['workdir']), f'{name} 查不了：{exc}'))
                continue
            except Exception as exc:
                kept.append((Path(item['workdir']), f'{name} 检查失败：{type(exc).__name__}: {exc}'))
                continue
            if result is None:
                continue
            if not result.released:
                kept.append((Path(item['workdir']), result.reason))
                continue
            if not result.reason:
                continue  # 工作区和分支都不在了，没有可删的。
            key = str(Path(item['workdir']).resolve())
            if key not in seen:
                seen.add(key)
                candidates.append({'item': item, 'target': target, 'executor': executor,
                                   'path': Path(item['workdir']),
                                   'action': result.reason or '删'})
    deduped, kept_paths = [], set()
    for path, reason in kept:
        key = str(path.resolve())
        if key in kept_paths:
            continue
        kept_paths.add(key)
        deduped.append((path, reason))
    kept = deduped
    orphans = []
    for checkout in checkouts(rules):
        try:
            entries = worktree_entries(checkout)
        except (ValueError, OSError) as exc:
            print(f'车间 prune：{exc}', file=sys.stderr)
            continue
        for entry in entries:
            path = Path(entry['worktree'])
            if not WORKTREE.fullmatch(path.name):
                continue
            key = str(path.resolve())
            if key in known or key in seen or key in kept_paths:
                continue
            reason = '目录已不在（git 里还登记着），没动' if not path.exists() else '没有对应的活，没动'
            orphans.append((path, reason))
    return candidates, kept, orphans


def unfinished_round_workdirs(log):
    """还没结束的轮正在用的工作目录；这些目录不能收回。"""
    result = set()
    for row in log.all('rounds', unless=('status', 'ended')):
        workdir = row.get('workdir')
        if workdir:
            result.add(str(Path(workdir).resolve()))
    return result


def skip_reason(log, item_id, path, active):
    """真删前按 id 重读：返回空串才删；返回原因就跳过。"""
    item = log.get('items', item_id)
    if not item:
        return '活已经不在了'
    if item.get('state') not in ('完成', '放弃'):
        return f"活现在是 {item.get('state')}，没删"
    if item.get('pending_rework'):
        return '活标了返工，没删'
    if not item.get('workdir') or str(Path(item['workdir']).resolve()) != str(Path(path).resolve()):
        return '活的工作目录变了，没删'
    if str(Path(path).resolve()) in active:
        return '这个工作目录上还有没停的轮，没删'
    return ''


def _sweep_rounds(executor_root, now, dry_run):
    from .plugins.executors.local.retention import sweep_rounds
    orca = shutil.which('orca')
    records, summaries, kept = [], [], []
    root = Path(executor_root)
    if not root.is_dir():
        return records, summaries, kept
    for kind in sorted(root.iterdir()):
        if not kind.is_dir() or not (kind / 'rounds').is_dir():
            continue
        removed = sweep_rounds(kind, now, dry_run=dry_run, orca=orca, kept=kept)
        if not removed:
            continue
        size = sum(record['bytes'] for record in removed)
        records.extend(removed)
        summaries.append((kind.name, len(removed), size))
    return records, summaries, kept


def _bulk(title, records, verbose, extra=()):
    if not records:
        return
    size = sum(record['bytes'] for record in records)
    print(f'{title}：{len(records)} 个，{human(size)}')
    for name, count, amount in extra:
        print(f'  {name}：{count} 个，{human(amount)}')
    if verbose:
        for record in records:
            print(f'  {record["path"]}：{human(record["bytes"])}')


def prune(args, log, rules, registry):
    dry_run = bool(args.dry_run)
    verbose = bool(args.verbose)
    now = time.time()
    executor_root = Path(os.environ.get('WORKSHOP_EXECUTOR_STATE_DIR',
                                        '~/.local/state/workshop-executors')).expanduser()
    # 先只读地算一遍，把要删的列出来；真的删的时候再按同一规则重算，认回这期间的变化。
    rounds, summaries, round_kept = _sweep_rounds(executor_root, now, True)
    jobs = sweep_directory(args.state_dir / 'manager-jobs', now,
                           keep_seconds=MANAGER_JOBS_KEEP_SECONDS, dry_run=True)
    core_jobs = sweep_directory(args.state_dir / 'jobs', now,
                                keep_seconds=KEEP_SECONDS, dry_run=True)
    sessions = sweep_sessions(args.state_dir, now, keep_seconds=MANAGER_SESSION_KEEP_SECONDS,
                              protected=protected_episodes(args.state_dir), dry_run=True)
    candidates, kept, orphans = plan_worktrees(log, rules, registry)

    total_count = len(rounds) + len(jobs) + len(core_jobs) + len(sessions) + len(candidates)
    total_size = (sum(r['bytes'] for r in rounds) + sum(r['bytes'] for r in jobs)
                  + sum(r['bytes'] for r in core_jobs) + sum(r['bytes'] for r in sessions))
    print('保留规则：执行者轮次结束 3 天、经理作业结果 1 小时、车间作业结果 1 天、'
          '经理会话 7 天；工作区在活结束、PR 已合并且没有未提交改动时收回。')
    print(f'将删除 {total_count} 个目录，共 {human(total_size)}'
          + ('（只列出，没动文件）' if dry_run else '') + '。')
    _bulk('执行者轮次', rounds, verbose, summaries)
    if round_kept:
        print(f'留着 {len(round_kept)} 个执行者轮次（有未提交改动）：')
        for record in round_kept:
            print(f'  {record["path"]}：{record["reason"]}')
    _bulk('经理作业', jobs, verbose)
    _bulk('车间作业', core_jobs, verbose)
    _bulk('经理会话', sessions, verbose)
    if candidates:
        print(f'工作区：{len(candidates)} 个')
        for candidate in candidates:
            print(f'  将删 {candidate["path"]}：{candidate["action"]}')
    if kept:
        print(f'留着 {len(kept)} 个工作区：')
        for path, reason in kept:
            print(f'  {path}：{reason}')
    if orphans:
        print(f'没有对应活的执行者工作区 {len(orphans)} 个：')
        for path, reason in orphans:
            print(f'  {path}：{reason}')

    if dry_run:
        return 0

    removed_rounds, _, _ = _sweep_rounds(executor_root, time.time(), False)
    removed_jobs = sweep_directory(args.state_dir / 'manager-jobs', time.time(),
                                   keep_seconds=MANAGER_JOBS_KEEP_SECONDS, measure=True)
    removed_core_jobs = sweep_directory(args.state_dir / 'jobs', time.time(),
                                        keep_seconds=KEEP_SECONDS, measure=True)
    removed_sessions = sweep_sessions(args.state_dir, time.time(),
                                      keep_seconds=MANAGER_SESSION_KEEP_SECONDS,
                                      protected=protected_episodes(args.state_dir))
    removed_size = (sum(r['bytes'] for r in removed_rounds) + sum(r['bytes'] for r in removed_jobs)
                    + sum(r['bytes'] for r in removed_core_jobs) + sum(r['bytes'] for r in removed_sessions))
    removed_count = (len(removed_rounds) + len(removed_jobs) + len(removed_core_jobs)
                     + len(removed_sessions))
    print(f'已删除 {removed_count} 个目录，共 {human(removed_size)}；开始删工作区。')
    if candidates:
        # 计划是只读快照；真删前和守护进程抢同一把锁，按 id 重读，避免把刚返工、
        # 正在原工作区开轮的那件活的工作区删掉。
        with locked(args.state_dir):
            active = unfinished_round_workdirs(log)
            for candidate in candidates:
                path = candidate['path']
                why = skip_reason(log, candidate['item']['id'], path, active)
                if why:
                    print(f'  {path}：跳过（{why}）')
                    continue
                item = log.get('items', candidate['item']['id'])
                try:
                    result = candidate['executor'].release(item, candidate['target'])
                except Exception as exc:
                    print(f'  {path}：没删成：{type(exc).__name__}: {exc}', file=sys.stderr)
                    continue
                if result is not None and not result.released:
                    print(f'  {path}：改主意没删：{result.reason}')
                else:
                    print(f'  {path}：删了')
    return 0
