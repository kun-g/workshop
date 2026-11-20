"""干净副本里检查进程的 HOME/TMPDIR 不建在状态目录深处（ws#308）。

switchboard 的测试在 TMPDIR 下建 oma-pytest-xxxxxxxx/，再在里面放 unix socket；
macOS 的 AF_UNIX 路径上限 104 字节，TMPDIR 随状态目录变深就放不下。
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from workshop.plugins.issue_delivery.io import IO

from test_check_copies import GIT, commit, git, worktrees

# switchboard tests/switchboard/entry/test_local_console.py 拼的那条 socket 路径在 TMPDIR 之后的部分。
SOCKET_TAIL = '/oma-pytest-xxxxxxxx/local-console-absent.sock'
AF_UNIX_LIMIT = 104  # macOS sun_path 含结尾 NUL


def deep(base, length=300):
    """一个足够深的状态目录：比原先 ~/.local/state/workshop/issue_delivery 深得多。"""
    path = base
    while len(str(path)) < length:
        path = path / ('deep-state-' + 'd' * 20)
    return path


def wait_for(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
    raise AssertionError('timed out')


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / 'checkout'
    subprocess.run([GIT, 'init', '-q', '-b', 'main', str(repo)], check=True)
    commit(repo, {'README': 'v1\n'}, 'first')
    return repo


def target(repo, state_dir=None):
    return {'name': 'alpha', 'repo': 'kun-g/example', 'checkout': str(repo),
            **({'issue_delivery_state_dir': str(state_dir)} if state_dir else {})}


ECHO_ENV = '#!/bin/sh\necho "$HOME"; echo "$TMPDIR"\n'


def test_check_tmpdir_length_does_not_grow_with_state_dir_depth(tmp_path, checkout):
    sha = commit(checkout, {'scripts/check.sh': ECHO_ENV})
    seen = {}
    for name, state in {'shallow': tmp_path / 's', 'deep': deep(tmp_path)}.items():
        runs = IO(gh='/usr/bin/true', git=GIT).verify(target(checkout, state), sha, [['scripts/check.sh']])
        assert runs[-1]['exit_code'] == 0, runs
        _, tmpdir = runs[-1]['stdout'].split()
        assert not Path(tmpdir).is_relative_to(tmp_path), (name, tmpdir)
        seen[name] = tmpdir
    assert len(seen['deep']) == len(seen['shallow'])
    assert len(seen['deep'] + SOCKET_TAIL) < AF_UNIX_LIMIT, seen


def test_switchboard_socket_shape_binds_under_check_tmpdir(tmp_path, checkout):
    # 检查进程里照 switchboard 的形状建目录、真 bind 一个 AF_UNIX socket 再关掉。
    script = f'''#!{sys.executable}
import os, socket, tempfile
directory = tempfile.mkdtemp(prefix='oma-pytest-')
path = os.path.join(directory, 'local-console-absent.sock')
print(len(path), path)
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.close()
os.unlink(path)
print('bound')
'''
    sha = commit(checkout, {'scripts/check.sh': script})
    runs = IO(gh='/usr/bin/true', git=GIT).verify(target(checkout, deep(tmp_path)), sha, [['scripts/check.sh']])
    assert runs[-1]['exit_code'] == 0, runs[-1]['stdout'] + runs[-1]['stderr']
    length, path = runs[-1]['stdout'].splitlines()[0].split()
    assert int(length) == len(path) and path.endswith('/local-console-absent.sock') and '/oma-pytest-' in path
    assert runs[-1]['stdout'].splitlines()[-1] == 'bound'


def test_check_home_and_tmpdir_are_private_to_the_check_and_removed(tmp_path, checkout, monkeypatch):
    # 照生产配置：不配 issue_delivery_state_dir，状态目录落在运行车间的 HOME 下。
    real_home = tmp_path / 'real-home'
    real_home.mkdir()
    monkeypatch.setenv('HOME', str(real_home))
    sha = commit(checkout, {'scripts/check.sh': ECHO_ENV + 'touch "$TMPDIR/left-by-check"\nexit 5\n'})
    flow = IO(gh='/usr/bin/true', git=GIT)
    before = worktrees(checkout)
    seen = []
    for _ in range(2):
        runs = flow.verify(target(checkout), sha, [['scripts/check.sh']])
        assert runs[-1]['exit_code'] == 5
        home, tmpdir = (Path(p) for p in runs[-1]['stdout'].split())
        for path in (home, tmpdir):
            assert path != real_home and not path.is_relative_to(real_home) and not real_home.is_relative_to(path)
            assert not path.exists()  # 检查一结束就删掉，失败的检查也一样。
        seen.append((home, tmpdir))
    assert seen[0][0] != seen[1][0] and seen[0][1] != seen[1][1]  # 每轮新建，不共用。
    assert worktrees(checkout) == before
    assert list((real_home / '.local/state/workshop/issue_delivery/copies').glob('check-*')) == []


def test_check_home_left_by_killed_process_is_swept(tmp_path, checkout):
    # 跑检查的进程被 kill -9：finally 不执行，留下的 HOME/TMPDIR 由 cleanup_copies 过期后收走。
    state = deep(tmp_path)
    ready = tmp_path / 'ready'
    script = f'#!/bin/sh\necho "$HOME" > {ready}.part; echo "$TMPDIR" >> {ready}.part; mv {ready}.part {ready}\nmkfifo gate; read line < gate\n'
    sha = commit(checkout, {'scripts/check.sh': script})
    before = worktrees(checkout)
    runner = f'''
import sys
sys.path[:0] = {[str(Path(__file__).parent.parent / 'src')]!r}
from workshop.plugins.issue_delivery.io import IO
IO(gh='/usr/bin/true', git={GIT!r}).verify({target(checkout, state)!r}, {sha!r}, [['scripts/check.sh']])
'''
    process = subprocess.Popen([sys.executable, '-c', runner], start_new_session=True)
    try:
        wait_for(ready.exists)
    finally:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    home, tmpdir = (Path(p) for p in ready.read_text().split())
    assert home.is_dir() and tmpdir.is_dir()
    flow = IO(gh='/usr/bin/true', git=GIT)
    flow.cleanup_copies(target(checkout, state))
    assert home.is_dir()  # 还没过期限，可能是正在跑的检查。
    flow.cleanup_copies(target(checkout, state), older_than=-1)
    assert not home.exists() and not tmpdir.exists()
    assert list((state / 'copies').glob('check-*')) == []
    assert worktrees(checkout) == before
