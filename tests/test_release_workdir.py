"""收尾后收回执行者的 worktree 和本地分支（ws#48 验收标准 E）。"""
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys

import pytest

from workshop.core.models import Release, Started
from workshop.core.ports import Retryable
from workshop.plugins.executors.local.adapters import Executor
from conftest import event, finish, item, reach
from test_issue_delivery import world  # noqa: F401  夹具

GIT = '/usr/bin/git'

# Orca 替身：create/rm 真的在主检出上开、删 worktree，别的命令照常回 ok。
ORCA = r'''
import json, pathlib, subprocess, sys
args = sys.argv[1:]
root = pathlib.Path(sys.argv[0]).parent
with (root / 'orca-calls').open('a') as f:
    f.write(json.dumps(args) + '\n')
def value(flag): return args[args.index(flag) + 1]
if (root / 'orca-down').exists():
    print('orca 没开', file=sys.stderr); sys.exit(1)
if args[:2] == ['worktree', 'create']:
    repo = value('--repo').removeprefix('path:')
    path = root / 'worktrees' / value('--name')
    subprocess.run(['/usr/bin/git', '-C', repo, 'worktree', 'add', '-q', '-b', value('--branch'), str(path), 'HEAD'], check=True)
    print(json.dumps({'ok': True, 'result': {'worktree': {'path': str(path)}}}))
elif args[:2] == ['worktree', 'rm']:
    path = value('--worktree').removeprefix('path:')
    common = subprocess.run(['/usr/bin/git', '-C', path, 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                            capture_output=True, text=True, check=True).stdout.strip()
    result = subprocess.run(['/usr/bin/git', '--git-dir', common, 'worktree', 'remove', path], capture_output=True, text=True)
    if result.returncode:
        print(result.stderr, file=sys.stderr); sys.exit(1)
    print(json.dumps({'ok': True, 'result': {}}))
else:
    print(json.dumps({'ok': True, 'result': {'handle': 'term-1'}}))
'''


def git(repo, *args):
    return subprocess.run([GIT, '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name='file.txt', text='x\n', message='change'):
    Path(repo, name).write_text(text)
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost', 'commit', '-qm', message)
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def local(tmp_path):
    """主检出 + 远端裸库 + 车间按活 id 开的 worktree。"""
    remote = tmp_path / 'remote.git'
    subprocess.run([GIT, 'init', '-q', '--bare', '-b', 'main', str(remote)], check=True)
    repo = tmp_path / 'checkout'
    subprocess.run([GIT, 'init', '-q', '-b', 'main', str(repo)], check=True)
    commit(repo, 'README', 'hello\n', 'first')
    git(repo, 'remote', 'add', 'origin', str(remote))
    git(repo, 'push', '-q', '-u', 'origin', 'main')
    orca = tmp_path / 'orca'
    orca.write_text('#!' + sys.executable + '\n' + ORCA)
    orca.chmod(0o755)
    executor = Executor('orca-claude', state_dir=tmp_path / 'state', commands={'orca': str(orca), 'git': GIT}, home=tmp_path)
    data = {'id': 'alpha:7', 'target': 'alpha', 'object_key': '7'}
    slug = executor.slug(data)
    workdir = tmp_path / 'worktrees' / slug
    git(repo, 'worktree', 'add', '-q', '-b', 'ws/' + slug, str(workdir), 'HEAD')
    data['workdir'] = str(workdir)
    return dict(repo=repo, executor=executor, item=data, workdir=workdir, branch='ws/' + slug,
                target={'name': 'alpha', 'checkout': str(repo)}, root=tmp_path)


def branches(repo):
    return git(repo, 'branch', '--format=%(refname:short)').split()


def test_pushed_work_is_released_worktree_and_branch_deleted(local):
    # E1：分支上的提交都在远端，worktree 和本地分支都删；主检出自己的分支不动。
    commit(local['workdir'], message='work')
    git(local['workdir'], 'push', '-q', 'origin', local['branch'])
    assert local['executor'].release(local['item'], local['target'], dry_run=True) == Release(
        True, f"删 {local['workdir']}，删分支 {local['branch']}")
    assert local['workdir'].exists()
    assert local['executor'].release(local['item'], local['target']) == Release(True)
    assert not local['workdir'].exists()
    assert str(local['workdir']) not in git(local['repo'], 'worktree', 'list')
    assert branches(local['repo']) == ['main']


def test_orca_codex_releases_the_same_way(local):
    # orca-codex 也按活 id 开同名 worktree，收回规则相同。
    codex = Executor('orca-codex', state_dir=local['root'] / 'state-codex',
                     commands={'orca': local['executor'].commands['orca'], 'git': GIT}, home=local['root'])
    git(local['workdir'], 'push', '-q', 'origin', local['branch'])
    assert codex.release(local['item'], local['target']) == Release(True)
    assert not local['workdir'].exists() and branches(local['repo']) == ['main']


def test_dirty_worktree_is_kept_with_reason(local):
    # E3：有未提交改动不删。
    (local['workdir'] / 'README').write_text('edited\n')
    (local['workdir'] / 'new.txt').write_text('new\n')
    result = local['executor'].release(local['item'], local['target'])
    assert not result.released and '2 处未提交改动' in result.reason and str(local['workdir']) in result.reason
    assert local['workdir'].exists() and local['branch'] in branches(local['repo'])


def test_ignored_files_do_not_count_as_changes(local):
    git(local['repo'], 'config', 'core.excludesFile', '/dev/null')
    (local['workdir'] / '.gitignore').write_text('.venv/\n')
    git(local['workdir'], 'add', '.gitignore')
    git(local['workdir'], '-c', 'user.name=T', '-c', 'user.email=t@l', 'commit', '-qm', 'ignore')
    git(local['workdir'], 'push', '-q', 'origin', local['branch'])
    (local['workdir'] / '.venv').mkdir()
    (local['workdir'] / '.venv/x').write_text('x')
    assert local['executor'].release(local['item'], local['target']) == Release(True)


def test_unpushed_commits_are_kept_with_reason(local):
    # E3：本地分支有远端没有的提交，不删。
    commit(local['workdir'], message='local only')
    result = local['executor'].release(local['item'], local['target'])
    assert not result.released and '1 个提交不在远端' in result.reason
    assert local['workdir'].exists() and local['branch'] in branches(local['repo'])


def test_directories_not_opened_for_this_item_are_left_alone(local):
    other = dict(local['item'], workdir=str(local['root'] / 'worktrees/someone-else'))
    assert local['executor'].release(other, local['target']) is None
    # 名字对得上但不是这个主检出上的 worktree，也不碰。
    plain = local['root'] / 'plain' / local['executor'].slug(local['item'])
    plain.mkdir(parents=True)
    assert local['executor'].release(dict(local['item'], workdir=str(plain)), local['target']) is None
    assert plain.exists()
    assert Executor('codex', state_dir=local['root'] / 's2', commands={'git': GIT}).release(local['item'], local['target']) is None


def test_already_removed_directory_still_drops_pushed_branch(local):
    git(local['repo'], 'worktree', 'remove', str(local['workdir']))
    assert local['executor'].release(local['item'], local['target']) == Release(True)
    assert branches(local['repo']) == ['main']


def test_orca_failure_is_retryable_and_nothing_is_lost(local):
    (local['root'] / 'orca-down').write_text('')
    with pytest.raises(Retryable):
        local['executor'].release(local['item'], local['target'])
    assert local['workdir'].exists() and local['branch'] in branches(local['repo'])


def test_rework_after_release_recreates_same_worktree(local, monkeypatch):
    # E4：收回后返工，launch 和 resume 都按同一个名字重新开 worktree。
    executor, data = local['executor'], local['item']
    git(local['workdir'], 'push', '-q', 'origin', local['branch'])
    assert executor.release(data, local['target']) == Release(True)
    monkeypatch.setattr(Executor, '_started', lambda self, directory, spec: Started(spec['session'], 'term-1', spec['public_workdir']))
    target = {**local['target'], 'runtime': {'command': ['/usr/bin/true'], 'env': {}}}
    from workshop.core.models import Brief
    brief = Brief('说明', [])
    session = 'b7b5e5d4-3f1a-4c55-9a43-7d1d37c0f001'
    session_dir = executor.root / 'sessions' / session
    session_dir.mkdir(parents=True)
    old_round = executor.directory('old')
    old_round.mkdir(parents=True)
    (old_round / 'stopped.json').write_text('{}')
    (session_dir / 'info.json').write_text(json.dumps({'workdir': str(local['workdir']), 'round': old_round.name}))
    started = executor.resume('rework', session, brief, {**data, 'workdir': None}, {'billing': 'subscription'}, target)
    assert started.workdir == str(local['workdir'].resolve()) and local['workdir'].exists()
    assert git(local['workdir'], 'rev-parse', '--abbrev-ref', 'HEAD') == local['branch']
    creates = [json.loads(line) for line in (local['root'] / 'orca-calls').read_text().splitlines()]
    assert [c[:2] for c in creates].count(['worktree', 'create']) == 1
    assert creates[-1][:2] == ['terminal', 'create']


# 核心：什么时候请执行者收回、收回后怎么记


class Flow:
    def __init__(self):
        self.hold = ''

    def hold_workdir(self, item, target):
        if self.hold == 'retry':
            raise Retryable('GitHub 暂时不通')
        return self.hold


class Releaser:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def release(self, item, target):
        self.calls.append(item['id'])
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def complete(engine):
    reach(engine, '收尾')
    finish(engine, None, leave={'kind': '送回', 'at': '+7d', 'about': '复看'})
    assert item(engine)['state'] == '完成'


def wire(engine, flow, result):
    executor = engine.plugins.get('executor', 'writer')
    releaser = Releaser(result)
    executor.release = releaser.release
    fake_flow = engine.plugins.get('flow', 'flow')
    fake_flow.hold_workdir = flow.hold_workdir
    return releaser


def test_engine_releases_completed_item_once_and_clears_workdir(engine):
    flow = Flow()
    releaser = wire(engine, flow, Release(True))
    complete(engine)  # 收尾那一拍里就收回。
    assert releaser.calls == ['alpha:one']
    row = item(engine)
    assert row['workdir'] is None and row['workdir_released'] == '已收回'
    engine.tick()
    assert releaser.calls == ['alpha:one']


def test_engine_waits_while_flow_holds_and_rechecks_an_hour_later(engine):
    # E2：流程说还要留（PR 还开着），不调执行者；一小时后再问。
    flow = Flow()
    flow.hold = 'PR #11 还开着'
    releaser = wire(engine, flow, Release(True))
    complete(engine)
    engine.tick()
    assert releaser.calls == [] and item(engine)['release_hold'] == 'PR #11 还开着'
    flow.hold = ''
    engine.tick()
    assert releaser.calls == []
    engine.clock.now += timedelta(hours=1, seconds=1)
    engine.tick()
    assert releaser.calls == ['alpha:one'] and item(engine)['workdir'] is None


def test_engine_flow_retry_does_not_isolate_target(engine):
    flow = Flow()
    flow.hold = 'retry'
    releaser = wire(engine, flow, Release(True))
    complete(engine)
    engine.tick()
    assert releaser.calls == [] and not engine.control('alpha').get('broken')


def test_engine_kept_workdir_records_reason_once(engine):
    # E3：执行者说不能收，原因记在活上（status、why 里看），不发通知（介入点 R13），不再重试，工作目录留着。
    releaser = wire(engine, Flow(), Release(False, '/w/ws-1 有 2 处未提交改动，没删'))
    complete(engine)
    engine.tick()
    engine.tick()
    assert releaser.calls == ['alpha:one']
    assert not engine.log.all('cards')
    assert '2 处未提交改动' in item(engine)['workdir_released']
    assert item(engine)['workdir'] == '/fake/workdir'


def test_engine_executor_retry_keeps_workdir_and_target_healthy(engine):
    releaser = wire(engine, Flow(), Retryable('Orca 没开'))
    complete(engine)
    engine.tick()
    assert releaser.calls == ['alpha:one'] and item(engine)['workdir'] == '/fake/workdir'
    assert not engine.control('alpha').get('broken')
    engine.clock.now += timedelta(hours=1, seconds=1)
    releaser.result = Release(True)
    engine.tick()
    assert item(engine)['workdir'] is None


def test_engine_does_not_release_unfinished_or_running_items(engine):
    releaser = wire(engine, Flow(), Release(True))
    reach(engine, '验收')
    engine.tick()
    assert releaser.calls == []


def test_engine_rework_after_release_resets_marker(engine):
    # E4（核心侧）：返工开新一轮后，收尾时重新判断收不收。
    releaser = wire(engine, Flow(), Release(True))
    complete(engine)
    engine.tick()
    assert item(engine)['workdir_released'] == '已收回'
    engine.ingest(event(action='rework', ext_id='rework-1'))
    engine.tick()
    row = item(engine)
    assert row['state'] == '跑着' and row['workdir'] == '/fake/workdir' and row['workdir_released'] is None
    for stage in ('证据', '审查', '交付', '验收', '收尾'):
        finish(engine, stage)
    finish(engine, None, leave={'kind': '送回', 'at': '+7d', 'about': '复看'})
    assert item(engine)['state'] == '完成' and releaser.calls == ['alpha:one', 'alpha:one']
    assert item(engine)['workdir'] is None


def test_issue_flow_holds_workdir_while_item_pr_is_open(world):
    # E2：这件活还有打开的 PR 时留着；PR 合并或关掉后可以收回。
    data = {'id': 'alpha:7', 'target': 'alpha', 'object_key': '7'}
    assert world['flow'].hold_workdir(data, world['target']) == 'PR #11 还开着'
    state = json.loads(world['store'].read_text())
    state['pr'].update(state='closed', merged=True)
    world['store'].write_text(json.dumps(state))
    assert world['flow'].hold_workdir(data, world['target']) == ''
