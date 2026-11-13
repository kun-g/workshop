"""检查副本改用主检出上的 worktree、按锁文件复用依赖缓存（ws#48 验收标准 B、D）。"""
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import zipfile

import pytest

from workshop.plugins.issue_delivery.io import IO

GIT = '/usr/bin/git'
SRC = Path(__file__).parent.parent / 'src'


def git(repo, *args, **kwargs):
    return subprocess.run([GIT, '-C', str(repo), *args], check=True, capture_output=True, text=True, **kwargs).stdout.strip()


def commit(repo, files, message='change'):
    for name, text in files.items():
        path = Path(repo, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if name.endswith('.sh'):
            path.chmod(0o755)
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost', 'commit', '-q', '--allow-empty', '-m', message)
    return git(repo, 'rev-parse', 'HEAD')


def history(repo, count):
    """用 fast-import 一次造 count 个提交，每个提交改同一个文件。"""
    subprocess.run([GIT, 'init', '-q', '-b', 'main', str(repo)], check=True)
    lines = []
    for n in range(1, count + 1):
        data = f'line {n}\n'.encode()
        lines.append(b'commit refs/heads/main\nmark :%d\ncommitter T <t@localhost> %d +0000\ndata 1\nc\n' % (n, 1700000000 + n))
        if n > 1:
            lines.append(b'from :%d\n' % (n - 1))
        lines.append(b'M 100644 inline file.txt\ndata %d\n%s\n' % (len(data), data))
    subprocess.run([GIT, '-C', str(repo), 'fast-import', '--quiet'], input=b''.join(lines), check=True)
    git(repo, 'checkout', '-q', 'main')
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / 'checkout'
    subprocess.run([GIT, 'init', '-q', '-b', 'main', str(repo)], check=True)
    commit(repo, {'scripts/check.sh': '#!/bin/sh\necho ok\n', 'README': 'v1\n'}, 'first')
    return repo


def io(tmp_path):
    return IO(gh='/usr/bin/true', git=GIT, state_dir=tmp_path / 'flow')


def target(repo, **extra):
    return {'name': 'alpha', 'repo': 'kun-g/example', 'checkout': str(repo), **extra}


def worktrees(repo):
    return git(repo, 'worktree', 'list', '--porcelain')


def state(repo):
    return [git(repo, 'rev-parse', 'HEAD'), git(repo, 'symbolic-ref', 'HEAD'), git(repo, 'status', '--porcelain', '--ignored'),
            git(repo, 'config', '--list', '--local'), sorted(p.name for p in (repo / '.git/hooks').iterdir())]


def test_no_separate_clone_in_source():
    # B1：不再 clone --no-local，也不另建车间自有仓库。
    assert not [p for p in SRC.rglob('*.py') if '--no-local' in p.read_text()]


@pytest.mark.parametrize('case', ['pass', 'fail', 'timeout', 'prepare-raises'])
def test_copy_is_removed_after_every_outcome(tmp_path, checkout, case):
    # B2：通过、失败、超时、准备命令抛异常，结束后主检出的 worktree 登记与准备前相同。
    script = {'pass': 'echo ok', 'fail': 'echo bad >&2; exit 3', 'timeout': 'sleep 5', 'prepare-raises': 'echo ok'}[case]
    sha = commit(checkout, {'scripts/check.sh': f'#!/bin/sh\n{script}\n'})
    before = worktrees(checkout)
    extra = {'check_timeout': 0.5} if case == 'timeout' else {}
    if case == 'prepare-raises':
        extra['prepare'] = [{'argv': ['true'], 'cwd': '../..'}]
    flow = io(tmp_path)
    if case == 'prepare-raises':
        with pytest.raises(ValueError, match='干净副本内'):
            flow.verify(target(checkout, **extra), sha, [['scripts/check.sh']])
    else:
        runs = flow.verify(target(checkout, **extra), sha, [['scripts/check.sh']])
        assert runs[-1]['exit_code'] == {'pass': 0, 'fail': 3, 'timeout': 124}[case]
    assert worktrees(checkout) == before
    assert list((tmp_path / 'flow/copies').glob('check-*')) == []


def test_review_copy_is_readonly_worktree_and_cleanup_unregisters_it(tmp_path, checkout):
    # B2、B7：只读副本仍去写权限；清理后主检出不留登记。
    sha = git(checkout, 'rev-parse', 'HEAD')
    before = worktrees(checkout)
    flow = io(tmp_path)
    path = Path(flow.review_copy({'id': 'alpha:7'}, target(checkout), sha))
    assert path.stat().st_mode & 0o222 == 0 and (path / 'README').stat().st_mode & 0o222 == 0
    assert str(path) in worktrees(checkout)
    assert git(path, 'rev-parse', 'HEAD') == sha
    # 再要同一提交的只读副本，复用而不重开。
    assert flow.review_copy({'id': 'alpha:7'}, target(checkout), sha) == str(path)
    flow.cleanup_reviews(target(checkout), keep=[])
    assert path.exists()  # 刚建的在宽限期内（REVIEW_GRACE_SECONDS），不清。
    old = time.time() - 2 * 3600
    os.utime(path, (old, old))
    flow.cleanup_reviews(target(checkout), keep=[])
    assert not path.exists() and worktrees(checkout) == before


def test_stale_check_copy_left_by_killed_process_is_swept(tmp_path, checkout):
    sha = git(checkout, 'rev-parse', 'HEAD')
    flow = io(tmp_path)
    before = worktrees(checkout)
    leftover = tmp_path / 'flow/copies/check-killed'
    leftover.mkdir(parents=True)
    flow.copy(target(checkout), sha, leftover / 'repo')
    flow.cleanup_copies(target(checkout))
    assert leftover.exists()  # 还没过期限，可能是正在跑的检查。
    flow.cleanup_copies(target(checkout), older_than=-1)
    assert not leftover.exists() and worktrees(checkout) == before


def test_copy_time_does_not_grow_with_history(tmp_path):
    # B3：已有该提交时准备副本 ≤ 1 秒；5000 个提交的仓库不超过 10 个提交的 3 倍。
    small, large = tmp_path / 'small', tmp_path / 'large'
    shas = {small: history(small, 10), large: history(large, 5000)}
    flow = io(tmp_path)
    counter = iter(range(100))

    def prepare(repo):
        destination = tmp_path / f'copy-{next(counter)}'
        flow.copy(target(repo), shas[repo], destination)
        flow.drop(target(repo), destination)

    def timed(repo):
        start = time.perf_counter()
        destination = tmp_path / f'copy-{next(counter)}'
        flow.copy(target(repo), shas[repo], destination)
        elapsed = time.perf_counter() - start
        flow.drop(target(repo), destination)
        return elapsed

    prepare(small), prepare(large)
    small_time = min(timed(small) for _ in range(3))
    large_time = min(timed(large) for _ in range(3))
    assert int(git(large, 'rev-list', '--count', 'HEAD')) == 5000
    assert small_time <= 1 and large_time <= 1
    assert large_time <= 3 * small_time, (small_time, large_time)


def test_untracked_file_in_copy_does_not_reach_next_copy(tmp_path, checkout):
    # B4：副本里写未跟踪文件，下一次副本里没有它；主检出也没有。
    sha = commit(checkout, {'scripts/check.sh': '#!/bin/sh\necho leftover > untracked.txt\nmkdir -p build && echo x > build/out\n'})
    flow = io(tmp_path)
    seen = []
    flow.verify(target(checkout), sha, [['scripts/check.sh']])
    original = flow.copy

    def spy(target, sha, destination):
        result = original(target, sha, destination)
        seen.append(sorted(p.name for p in Path(destination).iterdir()))
        return result

    flow.copy = spy
    flow.verify(target(checkout), sha, [['scripts/check.sh']])
    assert seen == [['.git', 'README', 'scripts']]
    assert not (checkout / 'untracked.txt').exists() and not (checkout / 'build').exists()


def test_main_checkout_hooks_do_not_run_and_main_is_untouched(tmp_path, checkout):
    # B5：主检出的钩子在准备副本和检查里的 git 命令中都不运行；主检出的 HEAD、分支、工作区、配置、钩子不变。
    marker = tmp_path / 'hook-ran'
    hook = checkout / '.git/hooks/post-checkout'
    hook.write_text(f'#!/bin/sh\necho "$0" >> {marker}\n')
    hook.chmod(0o755)
    sha = commit(checkout, {'scripts/check.sh': '#!/bin/sh\ngit checkout -q --detach HEAD && git status --short\n'})
    git(checkout, 'checkout', '-q', '-b', 'other')
    commit(checkout, {'README': 'main moved on\n'})
    marker.unlink(missing_ok=True)
    before = state(checkout)
    runs = io(tmp_path).verify(target(checkout), sha, [['scripts/check.sh']])
    assert runs[-1]['exit_code'] == 0, runs
    assert not marker.exists()
    assert state(checkout) == before


def test_parallel_checks_on_one_checkout_each_see_their_own_commit(tmp_path, checkout):
    # B6：同一交付对象两件活并发准备副本、跑检查，各自拿到自己提交的内容。
    first = commit(checkout, {'scripts/check.sh': '#!/bin/sh\nsleep 0.3; cat VERSION\n', 'VERSION': 'one\n'})
    second = commit(checkout, {'VERSION': 'two\n'})
    before = worktrees(checkout)
    flow = io(tmp_path)
    results = {}

    def check(sha):
        results[sha] = flow.verify(target(checkout), sha, [['scripts/check.sh']])

    threads = [threading.Thread(target=check, args=(sha,)) for sha in (first, second)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results[first][-1]['stdout'] == 'one\n' and results[second][-1]['stdout'] == 'two\n'
    assert worktrees(checkout) == before


def test_check_env_has_fresh_home_and_cache_keyed_by_lockfile(tmp_path, checkout):
    # D1、D2：HOME 每轮新建且为空；UV_CACHE_DIR 在 HOME 之外，按 uv.lock 内容分目录。
    script = '#!/bin/sh\necho "$HOME"; echo "$UV_CACHE_DIR"; ls -A "$HOME" | wc -l | tr -d " "\n'
    one = commit(checkout, {'scripts/check.sh': script, 'uv.lock': 'version = 1\n'})
    two = commit(checkout, {'uv.lock': 'version = 2\n'})
    flow = io(tmp_path)

    def env(sha):
        home, cache, entries = flow.verify(target(checkout), sha, [['scripts/check.sh']])[-1]['stdout'].split()
        return Path(home), Path(cache), entries

    home_a, cache_a, entries = env(one)
    home_b, cache_b, _ = env(one)
    _, cache_c, _ = env(two)
    assert entries == '0' and home_a != home_b
    assert cache_a == cache_b != cache_c
    assert cache_a.is_dir() and not cache_a.is_relative_to(home_a)
    assert cache_a.parent == tmp_path / 'flow/uv-cache'


def test_without_lockfile_no_shared_cache(tmp_path, checkout):
    sha = commit(checkout, {'scripts/check.sh': '#!/bin/sh\necho "cache=${UV_CACHE_DIR:-none}"\n'})
    assert io(tmp_path).verify(target(checkout), sha, [['scripts/check.sh']])[-1]['stdout'] == 'cache=none\n'


def test_old_caches_are_pruned_but_recent_ones_kept(tmp_path, checkout):
    flow = io(tmp_path)
    root = tmp_path / 'flow/uv-cache'
    old = time.time() - 3 * 86400
    for n in range(8):
        directory = root / f'old{n}'
        (directory / 'pkg').mkdir(parents=True)
        (directory / 'pkg/file').write_text('x')
        (directory / 'pkg/file').chmod(0o444)
        os.utime(directory, (old + n, old + n))
    (root / 'fresh').mkdir()
    sha = commit(checkout, {'uv.lock': 'version = 3\n'})
    copy = tmp_path / 'one'
    flow.copy(target(checkout), sha, copy)
    used = flow.dependency_cache(target(checkout), copy)
    flow.drop(target(checkout), copy)
    names = sorted(p.name for p in root.iterdir())
    # 留最近用过的 5 份（本次、fresh、最新的 3 份旧的），一天内用过的不删。
    assert used.name in names and 'fresh' in names
    assert names == sorted([used.name, 'fresh', 'old7', 'old6', 'old5'])


def wheel(directory):
    """手搓一个最小 wheel，作为本地包源；第二次准备时删掉它，只能靠缓存。"""
    directory.mkdir()
    path = directory / 'tinypkg-1.0-py3-none-any.whl'
    info = 'tinypkg-1.0.dist-info'
    files = {'tinypkg/__init__.py': 'VALUE = 1\n',
             f'{info}/METADATA': 'Metadata-Version: 2.1\nName: tinypkg\nVersion: 1.0\n',
             f'{info}/WHEEL': 'Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
    files[f'{info}/RECORD'] = ''.join(f'{name},,\n' for name in files) + f'{info}/RECORD,,\n'
    with zipfile.ZipFile(path, 'w') as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return directory


@pytest.mark.skipif(not shutil.which('uv'), reason='需要 uv')
def test_second_prepare_with_same_lock_hits_cache_offline(tmp_path, checkout):
    # D3：同一 uv.lock 第二次准备时以 UV_OFFLINE=1 跑 uv sync --locked 仍成功（包源已删，只能来自缓存）。
    links = wheel(tmp_path / 'links')
    project = {'pyproject.toml': ('[project]\nname = "demo"\nversion = "0"\nrequires-python = ">=3.9"\n'
                                  'dependencies = ["tinypkg"]\n\n[tool.uv]\npackage = false\n'
                                  f'find-links = ["{links}"]\nno-index = true\n'),
               'scripts/check.sh': '#!/bin/sh\n.venv/bin/python -c "import tinypkg; print(tinypkg.VALUE)"\n'}
    for name, text in project.items():
        path = checkout / name
        path.write_text(text)
    lock_env = {**os.environ, 'UV_CACHE_DIR': str(tmp_path / 'lock-cache'), 'HOME': str(tmp_path)}
    subprocess.run(['uv', 'lock', '--offline'], cwd=checkout, env=lock_env, check=True, capture_output=True)
    sha = commit(checkout, {})
    flow = io(tmp_path)
    online = target(checkout, prepare=[{'argv': ['uv', 'sync', '--locked']}], check_timeout=60)
    first = flow.verify(online, sha, [['scripts/check.sh']])
    assert [r['exit_code'] for r in first] == [0, 0], first
    # 包源目录留着、包删掉：uv 只能用缓存里的那份。
    for path in links.iterdir():
        path.unlink()
    offline = target(checkout, prepare=[{'argv': ['env', 'UV_OFFLINE=1', 'uv', 'sync', '--locked']}], check_timeout=60)
    second = flow.verify(offline, sha, [['scripts/check.sh']])
    assert [r['exit_code'] for r in second] == [0, 0], second
    assert second[-1]['stdout'] == '1\n'
