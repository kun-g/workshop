"""执行者自己留的东西：轮次目录的保留和清理。

轮次目录只在两种时候还有用：这一轮还在跑，或某一轮会话要接回（会话的 info.json
指向它的最新一轮）。其余按最后一轮结束时留 ROUND_KEEP_SECONDS；核心的 why/usage
从 workshop.db 读，不依赖这些目录。轮次里如果有执行时开的探针 worktree，先
`orca worktree rm` 掉里面的，再删目录。
"""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

ROUND_KEEP_SECONDS = 3 * 86400
ROUND_SWEEP_SECONDS = 3600
ROUND_NAME = re.compile(r'[0-9a-f]{64}')
END_MARKERS = ('stopped.json', 'exited.json')
# 找探针 worktree 时不下钻这些目录：里面不会再有执行者开的工作区。
SKIP_DIRS = {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache',
             '.import_linter_cache', 'site-packages', 'uv-cache', '.cache', 'target',
             'build', 'dist', '.mypy_cache', '.ruff_cache', '.tox', '.nox'}


def size_of(path):
    total = 0
    for root, directories, files in os.walk(path):
        directories[:] = [name for name in directories if name != '.git']
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                pass
    return total


def held(path):
    try:
        handle = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(handle)


def referenced_rounds(kind_root):
    """会话下一轮要接回的那一轮：sessions/<会话>/info.json 记着轮次目录名。"""
    result = set()
    for path in (Path(kind_root) / 'sessions').glob('*/info.json'):
        try:
            result.add(json.loads(path.read_text())['round'])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def _git(*args):
    return subprocess.run(['/usr/bin/git', *args], capture_output=True, text=True, timeout=60)


def _git_roots(directory, max_depth=6):
    """轮次目录里可能的探针工作区；只找顶层浅处的 .git，不为它遍历整个 .venv。"""
    roots, base = [], len(Path(directory).parts)
    for current, directories, files in os.walk(directory):
        if len(Path(current).parts) - base >= max_depth:
            directories[:] = []
        if '.git' in files or '.git' in directories:
            roots.append(Path(current))
        directories[:] = [name for name in directories if name not in SKIP_DIRS and not name.startswith('__')]
    return roots


def git_roots(directory):
    """轮次/工作区里嵌着的 git 根（克隆或链接工作区），不含目录本身。"""
    directory = Path(directory).resolve()
    return [root for root in _git_roots(directory) if root.resolve() != directory]


def changes(directory):
    """未提交改动；查不了也按脏处理，宁可留着。"""
    result = _git('-C', str(directory), 'status', '--porcelain', '-z', '-uall', '--no-renames')
    if result.returncode:
        return ['?']
    return [record for record in result.stdout.split('\0') if record]


def nested_worktrees(directory):
    """轮次目录里登记在外的 worktree；先删里面的，外层才能删干净。"""
    directory = Path(directory).resolve()
    found = {}
    for root in _git_roots(directory):
        result = _git('-C', str(root), 'rev-parse', '--path-format=absolute', '--git-common-dir')
        if result.returncode:
            continue
        common = Path(result.stdout.strip()).resolve()
        # 普通克隆的主工作区就是它自己，不算嵌套；链接工作区本身就是探针，要算。
        own_repo = common == (root / '.git').resolve()
        listing = _git('--git-dir', str(common), 'worktree', 'list', '--porcelain')
        if listing.returncode:
            continue
        for line in listing.stdout.splitlines():
            if not line.startswith('worktree '):
                continue
            path = Path(line[len('worktree '):])
            try:
                target = path.resolve()
                inside = (target != directory and target.is_relative_to(directory)
                          and (target != root or not own_repo))
            except OSError:
                continue
            if inside:
                found[path] = None
    return sorted(found, key=lambda path: len(path.parts), reverse=True)


def _remove_worktree(path, orca):
    if not orca:
        raise RuntimeError('找不到 orca，不能删嵌套工作区')
    try:
        result = subprocess.run([orca, 'worktree', 'rm', '--worktree', 'path:' + str(path), '--json'],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('orca 删除工作区结果不确定：' + type(exc).__name__) from exc
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-300:] or 'orca 删除工作区失败')
    try:
        value = json.loads(result.stdout)
    except ValueError:
        value = {}
    if value.get('ok') is False:
        raise RuntimeError(str(value)[:300])
    if Path(path).exists():
        raise RuntimeError('orca 返回成功但工作区还在')


def sweep_rounds(kind_root, now=None, *, keep_seconds=ROUND_KEEP_SECONDS, protected=(),
                 dry_run=False, measure=True, orca=None, kept=None):
    """清一个执行者配置档下过期的轮次目录。

    protected 是要保留的轮次目录名（调用方正在停的那一轮）；会话 info.json 指向的
    最新一轮和还持有 run.lock 的轮次也一律保留。只有留下结束标记的轮次才可能删。
    """
    kind_root = Path(kind_root)
    rounds = kind_root / 'rounds'
    if not rounds.is_dir():
        return []
    now = time.time() if now is None else now
    protected_names = set(protected) | referenced_rounds(kind_root)
    removed, errors = [], []
    for path in rounds.iterdir():
        try:
            if path.is_symlink() or not path.is_dir() or not ROUND_NAME.fullmatch(path.name):
                continue
            if path.name in protected_names or held(path / 'run.lock'):
                continue
            markers = [path / name for name in END_MARKERS if (path / name).exists()]
            if not markers:
                continue
            # 结束标记是这一轮最后落盘的东西；目录里后来补的文件不推迟保留期。
            ended = max(marker.stat().st_mtime for marker in markers)
            if now - ended <= keep_seconds:
                continue
            dirty = [(root, changes(root)) for root in git_roots(path)]
            dirty = [(root, records) for root, records in dirty if records]
            if dirty:
                root, records = dirty[0]
                reason = f'{root} 有 {len(records)} 处未提交改动'
                if kept is not None:
                    kept.append({'path': path, 'reason': reason})
                raise RuntimeError(reason + '，没删')
            nested = nested_worktrees(path)
            record = {'path': path, 'nested': nested, 'bytes': size_of(path) if measure else 0}
            if dry_run:
                removed.append(record)
                continue
            for worktree in nested:
                _remove_worktree(worktree, orca)
            shutil.rmtree(path)
            removed.append(record)
        except (OSError, RuntimeError) as exc:
            errors.append({'path': path, 'error': str(exc)})
    if errors:
        # 调用方（停轮次）不该因为清理失败而失败；把没删掉的留到下一次或存量命令。
        for error in errors:
            print(f"清理轮次目录失败：{error['path']}：{error['error']}", file=os.sys.stderr)
    return removed


def sweep_state(state_dir, now=None, *, keep_seconds=ROUND_KEEP_SECONDS, dry_run=False,
                measure=True, orca=None):
    """清执行者状态目录下所有配置档的轮次；存量命令用。"""
    root = Path(state_dir)
    if not root.is_dir():
        return []
    removed = []
    for kind in sorted(root.iterdir()):
        if kind.is_dir() and (kind / 'rounds').is_dir():
            removed.extend(sweep_rounds(kind, now, keep_seconds=keep_seconds, dry_run=dry_run,
                                        measure=measure, orca=orca))
    return removed


def maybe_sweep(kind_root, *, protect=(), now=None, keep_seconds=ROUND_KEEP_SECONDS):
    """轮次结算后顺手清一次；每个配置档每小时最多一次。失败不影响停轮次。"""
    kind_root = Path(kind_root)
    if not kind_root.is_dir():
        return []
    marker = kind_root / '.round-sweep'
    now = time.time() if now is None else now
    try:
        if now - marker.stat().st_mtime < ROUND_SWEEP_SECONDS:
            return []
    except OSError:
        pass
    try:
        marker.touch()
    except OSError:
        pass
    try:
        return sweep_rounds(kind_root, now, keep_seconds=keep_seconds, protected=protect,
                            measure=False, orca=shutil.which('orca'))
    except Exception as exc:  # 清理是后台维护，不能把一轮的停止结果搞成失败。
        print(f'清理执行者轮次失败：{type(exc).__name__}: {exc}', file=os.sys.stderr)
        return []
