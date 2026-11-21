"""后台作业加载守护进程在跑的那份代码，不被主检出后来的快进带偏（定时发版 R3）。"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from workshop import running

GIT = '/usr/bin/git'


def git(repo, *args):
    return subprocess.run([GIT, '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text, message=None):
    path = Path(repo) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=T', '-c', 'user.email=t@localhost', 'commit', '-qm', message or name)
    return git(repo, 'rev-parse', 'HEAD')


PROBE = """
import json, sys
from workshop.core.models import Event
import workshop.core.models as models
try:
    # 修复提交 Event 不需要参数；子提交给它加了必填字段，按旧方式构造就报类型错误。
    Event()
except TypeError as exc:
    print(json.dumps({'error': str(exc), 'module': models.__file__}))
else:
    print(json.dumps({'ok': True, 'module': models.__file__}))
"""


@pytest.fixture
def repo(tmp_path):
    """一个装着 workshop 包的最小 git 仓库；有一份「修复提交」和一份给它加了必填字段的「子提交」。"""
    root = tmp_path / 'clone'
    source = root / 'src' / 'workshop'
    subprocess.run([GIT, 'init', '-q', '-b', 'main', str(root)], check=True)
    (source / 'core').mkdir(parents=True)
    (source / '__init__.py').write_text('')
    (source / 'core' / '__init__.py').write_text('')
    (source / 'core' / 'models.py').write_text('import dataclasses\n'
                                               '@dataclasses.dataclass\n'
                                               'class Event:\n'
                                               '    pass\n')
    fixed = commit(root, 'src/workshop/core/models.py', (source / 'core' / 'models.py').read_text(),
                   'fixed')
    # 子提交给 Event 加一个必填字段：旧代码不认，新代码要它。
    (source / 'core' / 'models.py').write_text('import dataclasses\n'
                                               '@dataclasses.dataclass\n'
                                               'class Event:\n'
                                               '    new_required_field: str\n')
    newer = commit(root, 'src/workshop/core/models.py', (source / 'core' / 'models.py').read_text(),
                   'new field')
    return {'root': root, 'source': source, 'fixed': fixed, 'newer': newer}


def probe(pythonpath):
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT')}
    env['PYTHONPATH'] = pythonpath
    result = subprocess.run([sys.executable, '-c', PROBE], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_background_jobs_load_running_commit_not_fast_forwarded_checkout(repo, tmp_path):
    # 守护进程以修复提交为「在跑」的提交：导出那份代码，主检出的工作区就是修复提交。
    exported = running.export(tmp_path / 'state', source=repo['source'], commit=repo['fixed'])
    assert (exported / 'workshop' / 'core' / 'models.py').is_file()
    assert (exported / 'workshop' / 'core' / 'models.py').read_text() == 'import dataclasses\n' \
        '@dataclasses.dataclass\nclass Event:\n    pass\n'
    # 主检出被快进到给 Event 加了必填字段的子提交。
    git(repo['root'], 'merge', '--ff-only', repo['newer'])
    assert 'new_required_field' in (repo['source'] / 'core' / 'models.py').read_text()
    # 作业进程经 PYTHONPATH 加载导出的那份，仍然是修复提交的代码，不报类型错误。
    result = probe(str(exported))
    assert result['ok'] is True and result['module'] == str(exported / 'workshop' / 'core' / 'models.py')
    # 对照：直接按主检出加载，就是老毛病——新旧代码混用的类型错误。
    bad = probe(str(repo['root'] / 'src'))
    assert 'new_required_field' in bad['error']


def test_prepare_exports_and_points_pythonpath_at_running_commit(repo, tmp_path, monkeypatch):
    state = tmp_path / 'state'
    # 守护进程在修复提交上启动：主检出就是修复提交。
    git(repo['root'], 'checkout', '-q', repo['fixed'])
    monkeypatch.setenv('PYTHONPATH', '/existing')
    # prepare 记下在跑的版本、导出那份代码，并把导出目录放到 PYTHONPATH 最前。
    running.prepare(state, source=repo['source'])
    first = os.environ['PYTHONPATH'].split(os.pathsep)
    assert first[0] == str(state / running.EXPORTS / repo['fixed'])
    assert '/existing' in first and running.current() == repo['fixed']
    # 同步的代码导出只做一次：同一提交不重复导出。
    marker = state / running.EXPORTS / repo['fixed'] / 'ready.json'
    before = marker.stat().st_mtime_ns
    running.export(state, source=repo['source'], commit=repo['fixed'])
    assert marker.stat().st_mtime_ns == before
    # 主检出快进后重新起一个守护进程：重新导出子提交，PYTHONPATH 指过去，不混用。
    git(repo['root'], 'merge', '--ff-only', repo['newer'])
    monkeypatch.setenv('PYTHONPATH', '')
    running.prepare(state, source=repo['source'])
    assert running.current() == repo['newer']
    assert os.environ['PYTHONPATH'].split(os.pathsep) == [str(state / running.EXPORTS / repo['newer'])]
    assert (state / running.EXPORTS / repo['newer'] / 'workshop' / 'core' / 'models.py').read_text() \
        == (repo['source'] / 'core' / 'models.py').read_text()


def test_export_ignores_unfinished_and_prunes_old_exports(repo, tmp_path):
    state = tmp_path / 'state'
    root = state / running.EXPORTS
    # 一次中断留下的半份导出没有 ready 标记：不认它，重导。
    half = root / repo['fixed']
    (half / 'workshop').mkdir(parents=True)
    (half / 'workshop' / 'partial.py').write_text('x')
    exported = running.export(state, source=repo['source'], commit=repo['fixed'])
    assert exported == half and (half / 'ready.json').is_file()
    assert not (half / 'workshop' / 'partial.py').exists()
    # 导出过的旧提交只保留最近几份。
    for index in range(running.KEEP_EXPORTS + 2):
        older = root / ('%040x' % index)
        (older / 'workshop').mkdir(parents=True)
        (older / 'ready.json').write_text('{}')
        os.utime(older, (index, index))
    running.export(state, source=repo['source'], commit=repo['fixed'])
    kept = sorted(path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith('.'))
    assert repo['fixed'] in kept and len(kept) == running.KEEP_EXPORTS
