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

# gh 替身：按 --head 过滤本地的 PR 清单；只读，不建 PR。
GH = r'''
import json, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(sys.argv[0]).parent
with (root / 'gh-calls').open('a') as f:
    f.write(json.dumps(args) + '\n')
if (root / 'gh-down').exists():
    print('gh 没开', file=sys.stderr); sys.exit(1)
records = json.loads((root / 'prs.json').read_text()) if (root / 'prs.json').exists() else []
head = args[args.index('--head') + 1] if '--head' in args else None
print(json.dumps([p for p in records if head is None or p.get('headRefName') == head]))
'''


def set_prs(local, *prs):
    records = [{'headRefName': local['branch'].removeprefix('refs/heads/'), 'url': 'https://example/pr/1',
                'mergedAt': '2026-11-18T00:00:00Z', **pr} for pr in prs]
    (local['root'] / 'prs.json').write_text(json.dumps(records))


def merged(local, number=1, **extra):
    # 默认 PR 的 head 就是工作区当前提交；要模拟本地又多了提交，显式传 headRefOid。
    extra.setdefault('headRefOid', git(local['workdir'], 'rev-parse', 'HEAD'))
    set_prs(local, {'number': number, 'state': 'MERGED', **extra})


def git(repo, *args):
    return subprocess.run([GIT, '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name='file.txt', text='x\n', message='change'):
    Path(repo, name).write_text(text)
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost', 'commit', '-qm', message)
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def local(tmp_path):
    """主检出 + 远端裸库 + 车间按活 id 开的 worktree + App/gh 替身。"""
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
    gh = tmp_path / 'gh'
    gh.write_text('#!' + sys.executable + '\n' + GH)
    gh.chmod(0o755)
    executor = Executor('orca-claude', state_dir=tmp_path / 'state',
                        commands={'orca': str(orca), 'git': GIT, 'gh': str(gh)}, home=tmp_path)
    data = {'id': 'alpha:7', 'target': 'alpha', 'object_key': '7'}
    slug = executor.slug(data)
    workdir = tmp_path / 'worktrees' / slug
    git(repo, 'worktree', 'add', '-q', '-b', 'ws/' + slug, str(workdir), 'HEAD')
    data['workdir'] = str(workdir)
    return dict(repo=repo, executor=executor, item=data, workdir=workdir, branch='ws/' + slug,
                target={'name': 'alpha', 'checkout': str(repo), 'repo': 'alpha/repo'}, root=tmp_path)


def branches(repo):
    return git(repo, 'branch', '--format=%(refname:short)').split()


def test_merged_pr_releases_worktree_and_branch_even_without_pushed_commits(local):
    # 仓库用压缩合并：分支提交不在 main、也不在远端，PR 已合并就能收回。
    commit(local['workdir'], message='work')
    merged(local, 12)
    assert local['executor'].release(local['item'], local['target'], dry_run=True) == Release(
        True, f"删 {local['workdir']}，删分支 {local['branch']}（PR #12 已合并）")
    assert local['workdir'].exists()
    assert local['executor'].release(local['item'], local['target']) == Release(True)
    assert not local['workdir'].exists()
    assert str(local['workdir']) not in git(local['repo'], 'worktree', 'list')
    assert branches(local['repo']) == ['main']


def test_merged_pr_keeps_worktree_when_tip_has_commit_outside_pr_head(local):
    # PR 合并的是 head；工作区后来又多了一次本地提交，远端也没有，删分支就丢了。
    head = commit(local['workdir'], 'pr.txt', 'pr\n', 'pr work')
    merged(local, 20, headRefOid=head)
    commit(local['workdir'], 'local.txt', 'local\n', 'local only')
    result = local['executor'].release(local['item'], local['target'], dry_run=True)
    assert not result.released and '不在 PR #20 的 head 里' in result.reason
    assert local['workdir'].exists() and local['branch'] in branches(local['repo'])
    assert not local['executor'].release(local['item'], local['target']).released
    assert local['workdir'].exists() and local['branch'] in branches(local['repo'])


def test_detached_head_commit_outside_pr_head_is_kept(local):
    # detached HEAD 上的新提交同样不在 PR head 里，不能只删工作区了事。
    head = commit(local['workdir'], 'pr.txt', 'pr\n', 'pr work')
    merged(local, 21, headRefOid=head)
    git(local['workdir'], 'checkout', '-q', '--detach')
    commit(local['workdir'], 'detached.txt', 'local\n', 'detached only')
    result = local['executor'].release(local['item'], local['target'])
    assert not result.released and '不在 PR #21 的 head 里' in result.reason
    assert local['workdir'].exists() and local['branch'] in branches(local['repo'])


def test_orca_codex_releases_the_same_way(local):
    # orca-codex 也按活 id 开同名 worktree，收回规则相同。
    codex = Executor('orca-codex', state_dir=local['root'] / 'state-codex',
                     commands={'orca': local['executor'].commands['orca'], 'git': GIT,
                               'gh': local['executor'].commands['gh']}, home=local['root'])
    merged(local)
    assert codex.release(local['item'], local['target']) == Release(True)
    assert not local['workdir'].exists() and branches(local['repo']) == ['main']


def test_orca_suffix_is_the_same_worktree(local):
    # Orca 重名时会给目录加 -2 后缀；分支也跟着变，仍按活 id 认回来。
    slug = local['executor'].slug(local['item'])
    suffixed = local['root'] / 'worktrees' / (slug + '-2')
    git(local['repo'], 'worktree', 'add', '-q', '-b', 'ws/' + slug + '-2', str(suffixed), 'HEAD')
    (local['root'] / 'prs.json').write_text(json.dumps([
        {'number': 13, 'state': 'MERGED', 'headRefName': 'ws/' + slug + '-2',
         'headRefOid': git(suffixed, 'rev-parse', 'HEAD'),
         'mergedAt': '2026-11-18T00:00:00Z', 'url': 'https://example/pr/13'}]))
    item = dict(local['item'], workdir=str(suffixed))
    assert local['executor'].release(item, local['target']) == Release(True)
    assert not suffixed.exists()
    assert 'ws/' + slug + '-2' not in branches(local['repo'])


def test_dirty_worktree_is_kept_with_reason(local):
    # E3：有未提交改动不删，哪怕 PR 已合并。
    merged(local)
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
    merged(local)
    (local['workdir'] / '.venv').mkdir()
    (local['workdir'] / '.venv/x').write_text('x')
    assert local['executor'].release(local['item'], local['target']) == Release(True)


def test_nested_probe_worktrees_are_removed_inside_out(local):
    merged(local)
    nested = local['workdir'] / '.local' / 'probe-core'
    git(local['repo'], 'worktree', 'add', '-q', '--detach', str(nested), 'HEAD')
    assert local['executor'].release(local['item'], local['target']) == Release(True)
    assert not nested.exists() and not local['workdir'].exists()
    calls = [json.loads(line) for line in (local['root'] / 'orca-calls').read_text().splitlines()]
    removals = [call[call.index('--worktree') + 1].removeprefix('path:')
                for call in calls if call[:2] == ['worktree', 'rm']]
    assert removals == [str(nested), str(local['workdir'])]
    assert branches(local['repo']) == ['main']


def test_dirty_nested_worktree_keeps_everything(local):
    merged(local)
    nested = local['workdir'] / '.local' / 'probe-core'
    git(local['repo'], 'worktree', 'add', '-q', '--detach', str(nested), 'HEAD')
    (nested / 'probe.txt').write_text('unsaved\n')
    result = local['executor'].release(local['item'], local['target'])
    assert not result.released and str(nested) in result.reason and '1 处未提交改动' in result.reason
    assert nested.exists() and local['workdir'].exists()


def test_dirty_nested_clone_keeps_everything(local):
    merged(local)
    clone = local['workdir'] / '.local' / 'clone'
    subprocess.run([GIT, 'clone', '-q', str(local['repo']), str(clone)], check=True)
    (clone / 'README').write_text('unsaved clone change\n')
    result = local['executor'].release(local['item'], local['target'])
    assert not result.released and str(clone) in result.reason and '1 处未提交改动' in result.reason
    assert clone.exists() and local['workdir'].exists()


def test_open_pr_is_kept(local):
    set_prs(local, {'number': 3, 'state': 'OPEN'})
    result = local['executor'].release(local['item'], local['target'])
    assert not result.released and 'PR #3 还开着' in result.reason
    assert local['workdir'].exists() and local['branch'] in branches(local['repo'])


def test_closed_unmerged_pr_is_kept(local):
    commit(local['workdir'], message='unmerged work')
    set_prs(local, {'number': 4, 'state': 'CLOSED'})
    result = local['executor'].release(local['item'], local['target'])
    assert not result.released and 'PR #4 已关闭但没合并' in result.reason
    assert local['workdir'].exists() and local['branch'] in branches(local['repo'])


def test_missing_pr_is_kept(local):
    commit(local['workdir'], message='unmerged work')
    result = local['executor'].release(local['item'], local['target'])
    assert not result.released and '找不到已合并的 PR' in result.reason
    assert local['workdir'].exists() and local['branch'] in branches(local['repo'])


def test_branch_already_in_main_is_released_without_pr(local):
    # 没有 PR，但分支头就是 origin/main 上的提交；删工作区和分支不会丢东西。
    assert '分支已并入 main' in local['executor'].release(
        local['item'], local['target'], dry_run=True).reason
    assert local['executor'].release(local['item'], local['target']) == Release(True)
    assert not local['workdir'].exists() and local['branch'] not in branches(local['repo'])


def test_directories_not_opened_for_this_item_are_left_alone(local):
    other = dict(local['item'], workdir=str(local['root'] / 'worktrees/someone-else'))
    assert local['executor'].release(other, local['target']) is None
    # 名字对得上但不是这个主检出上的 worktree，也不碰。
    plain = local['root'] / 'plain' / local['executor'].slug(local['item'])
    plain.mkdir(parents=True)
    assert local['executor'].release(dict(local['item'], workdir=str(plain)), local['target']) is None
    assert plain.exists()
    assert Executor('codex', state_dir=local['root'] / 's2', commands={'git': GIT}).release(local['item'], local['target']) is None


def test_already_removed_directory_still_drops_merged_branch(local):
    merged(local)
    git(local['repo'], 'worktree', 'remove', str(local['workdir']))
    assert local['executor'].release(local['item'], local['target']) == Release(True)
    assert branches(local['repo']) == ['main']


def test_orca_failure_is_retryable_and_nothing_is_lost(local):
    merged(local)
    (local['root'] / 'orca-down').write_text('')
    with pytest.raises(Retryable):
        local['executor'].release(local['item'], local['target'])
    assert local['workdir'].exists() and local['branch'] in branches(local['repo'])


def test_gh_failure_is_retryable_and_nothing_is_lost(local):
    merged(local)
    (local['root'] / 'gh-down').write_text('')
    with pytest.raises(Retryable):
        local['executor'].release(local['item'], local['target'])
    assert local['workdir'].exists() and local['branch'] in branches(local['repo'])


def test_missing_gh_keeps_worktree_with_reason(local):
    executor = Executor('orca-claude', state_dir=local['root'] / 'state-nogh',
                        commands={'orca': local['executor'].commands['orca'], 'git': GIT, 'gh': None},
                        home=local['root'])
    result = executor.release(local['item'], local['target'])
    assert not result.released and '找不到 gh' in result.reason
    assert local['workdir'].exists()


def test_rework_after_release_recreates_same_worktree(local, monkeypatch):
    # E4：收回后返工，launch 和 resume 都按同一个名字重新开 worktree。
    executor, data = local['executor'], local['item']
    merged(local)
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


def test_kept_workdir_query_only_returns_directories_still_there(engine, tmp_path):
    # 目录已不在（手工删了、存量命令收回了）就不再登记项目经理事项。
    wire(engine, Flow(), Release(False, '/w/ws-1 有 1 处未提交改动，没删'))
    complete(engine)
    engine.tick()
    assert engine.log.kept_workdirs() == []
    real = tmp_path / 'ws-real'
    real.mkdir()
    engine.change(item(engine), workdir=str(real))
    assert [row['id'] for row in engine.log.kept_workdirs()] == ['alpha:one']


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


def test_engine_releases_abandoned_item_once(engine):
    # 活结束包括完成和放弃；放弃后工作目录按同一规则收回。
    releaser = wire(engine, Flow(), Release(True))
    reach(engine, '验收')
    row = item(engine)
    abandoned = {**row, 'id': 'alpha:abandoned', 'state': '放弃', 'workdir': '/fake/workdir',
                 'workdir_released': None, 'release_at': None}
    engine.log.db.execute('INSERT INTO items VALUES (?,?)',
                          (abandoned['id'], json.dumps(abandoned, ensure_ascii=False, default=str)))
    engine.tick()
    assert releaser.calls == ['alpha:abandoned']
    assert releaser.calls == ['alpha:abandoned'] and item(engine)['state'] == '跑着'


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
