"""合并冲突不隔离插件、已合并的活重走验收、车间合并自己后自更新、执行守边界，以及 11-13 卡住的两件怎么恢复。"""
import json
from pathlib import Path
import subprocess

import pytest

from workshop.core.cards import notice
from workshop.core.engine import Engine
from workshop.core.log import Log
from workshop.core.models import Event, Receipt
from workshop.core.ports import Retryable
from workshop.plugins.issue_delivery.io import run
from conftest import build, configuration, current, finish, item, reach
from test_issue_delivery import world, edit, calls, result, snapshot  # noqa: F401  world 是夹具


GIT = '/usr/bin/git'
CLOSING = {'leave': {'kind': '送回', 'at': '+7d', 'about': '复看'},
           'notice': [{'key': 'accepted', 'text': '验收通过；不对就在群里回一句「ws#7 不对」'}]}


def git(repo, *args):
    return subprocess.run([GIT, '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, name, text):
    Path(repo, name).write_text(text)
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost', 'commit', '-qm', name)
    return git(repo, 'rev-parse', 'HEAD')


def remote(world):
    return json.loads(world['store'].read_text())


def engine_for(world, name, **target):
    cfg = configuration()
    cfg.pop('taskhome')
    cfg['targets']['alpha'] = {**world['target'], 'plugin': 'issue_delivery', 'sources': [], **target}
    engine = build(world['root'] / name, cfg)
    engine.plugins.register('flow', 'issue_delivery', world['flow'])
    engine.ingest(Event('github', 'start', 'alpha', '7', 'test', {}, action='ready'))
    engine.tick()
    return engine


def walk(engine, world, stages, **payload):
    for stage in stages:
        finish(engine, stage, key='7', evidence=[result(world, checks=[['scripts/check.sh']], must_fix=[], suggestions=[], **payload)])


def fail_acceptance(engine, world, head):
    finish(engine, '收尾', key='7', evidence=[result(world, acceptance={'passed': False, 'output': '本地没有交付提交', 'head': head})])
    assert current(engine, '7')['stage'] == '执行'


def pass_acceptance_and_close(engine, world, head):
    finish(engine, '收尾', key='7', evidence=[result(world, acceptance={'passed': True, 'output': 'acceptance raw output', 'head': head})])
    assert current(engine, '7')['stage'] == '收尾'
    finish(engine, None, key='7', evidence=[result(world)], **CLOSING)
    assert item(engine, '7')['state'] == '完成'
    assert remote(world)['issue']['state'] == 'closed'


def brief(engine):
    return current(engine, '7')['brief']['instructions']


def rerun(engine, world, merge):
    finish(engine, '验收', key='7', evidence=[result(world, rerun_acceptance={'merge': merge, 'reason': '验收环境拿不到合并提交'})])


def merges(world):
    return len([c for c in calls(world) if c[1:3] == ['pr', 'merge']])


# 1. 合并冲突不是插件故障；io.run 的错误带 stderr 结尾

def test_run_failure_message_carries_stderr_tail():
    with pytest.raises(Retryable) as caught:
        run(['/bin/sh', '-c', 'echo 标准输出; echo "X Pull request is not mergeable: 冲突" >&2; exit 3'])
    assert '（exit 3）' in str(caught.value) and 'not mergeable: 冲突' in str(caught.value)
    # 长输出只留结尾；head -c 截断了多字节字符，坏字节也不能把暂时故障变成插件崩溃。
    with pytest.raises(Retryable) as caught:
        run(['/bin/sh', '-c', 'yes 噪声 | head -c 9000 >&2; echo 最后一行 >&2; exit 1'])
    message = str(caught.value)
    assert message.endswith('最后一行') and len(message) < 500


@pytest.mark.parametrize('merge_state,stderr,precheck', [
    ('dirty', '', True),
    ('blocked', 'X Pull request kun-g/workshop#11 is not mergeable: the base branch policy prohibits the merge.', False),
])
def test_merge_conflict_goes_back_to_execution_without_isolating_flow(world, merge_state, stderr, precheck):
    engine = engine_for(world, 'conflict-' + merge_state)
    walk(engine, world, ['说清楚', '执行', '证据', '审查', '交付'])
    pr = remote(world)['pr']
    pr.update(mergeable=merge_state != 'dirty', mergeable_state=merge_state)
    edit(world, pr=pr, merge_error=stderr)
    finish(engine, '验收', key='7', evidence=[result(world)])
    row = current(engine, '7')
    assert row['stage'] == '交付' and not engine.control('alpha').get('broken')
    assert '退回执行' in brief(engine) and 'advance="执行"' in brief(engine)
    if precheck:
        assert '合并冲突' in item(engine, '7')['gate_reason'] and merges(world) == 0
    else:
        assert 'base branch policy prohibits the merge' in item(engine, '7')['gate_reason'] and merges(world) == 1
    finish(engine, '执行', key='7', evidence=[result(world)])
    assert current(engine, '7')['stage'] == '执行'
    assert engine.log.all('cards') == [] and not engine.control('alpha').get('broken')
    engine.log.close()


def test_unexplained_merge_failure_stays_retryable_with_stderr(world):
    data = snapshot(world, '交付')
    world['io'].save(data, world['target'], {'effects': {}, 'issue': 7, 'reviewed_head': world['sha']})
    pr = remote(world)['pr']
    pr.update(mergeable=True, mergeable_state='clean')
    edit(world, pr=pr, merge_error='HTTP 502: Bad Gateway (https://api.github.com/graphql)')
    with pytest.raises(Retryable) as caught:
        world['flow'].after(data, '交付', 'infra', world['target'])
    assert 'Bad Gateway' in str(caught.value)
    # GitHub 还没算出能否合并：等一会再查，不起执行者，也不算故障。
    pr.update(mergeable=None, mergeable_state='unknown')
    edit(world, pr=pr)
    waiting = world['flow'].after(data, '交付', 'unknown', world['target'])
    assert waiting.pending and not waiting.rerun and 'Bad Gateway' in waiting.reason


# 2. 已合并的活重走验收：同一合并提交一次，再不过问 Kun

def test_merged_item_reruns_acceptance_once_then_blocks(world):
    engine = engine_for(world, 'rerun')
    walk(engine, world, ['说清楚', '执行', '证据', '审查', '交付', '验收'])
    sha = remote(world)['pr']['merge_commit_sha']
    fail_acceptance(engine, world, sha)
    assert f'合并提交 {sha}' in brief(engine) and 'rerun_acceptance' in brief(engine)
    rerun(engine, world, sha)
    assert current(engine, '7')['stage'] == '验收'
    assert merges(world) == 1 and world['io'].read(item(engine, '7'), world['target'])['acceptance_reruns'] == [sha]
    assert f'只验固定检查提交 {sha}' in brief(engine)
    fail_acceptance(engine, world, sha)
    assert '已重走过一次验收' in brief(engine) and 'outcome=nothing' in brief(engine)
    for attempt in range(3):
        rerun(engine, world, sha)
        if attempt < 2:
            assert current(engine, '7')['stage'] == '执行'
            assert '已重走过一次验收' in item(engine, '7')['gate_reason']
    assert item(engine, '7')['state'] == '卡住'
    asks = [c for c in engine.log.all('cards') if c['mode'] == 'ask']
    assert asks == [] and '已重走过一次验收' in item(engine, '7')['blocked_by']
    engine.log.close()


def test_core_returns_to_acceptance_only_for_items_that_reached_it(engine):
    """核心只认阶段：没到过验收不能从执行跳过去；到过的，流程证据过了才回验收。"""
    flow = engine.plugins.get('flow', 'flow')
    reach(engine, '执行')
    with pytest.raises(ValueError, match='advance 是「验收」；执行之后只能是「证据」'):
        finish(engine, '验收')
    assert current(engine)['stage'] == '执行' and current(engine)['receipt'] is None
    for stage in ('证据', '审查', '交付', '验收'):
        finish(engine, stage)
    flow.passed = False
    finish(engine, '收尾')
    assert current(engine)['stage'] == '执行'
    finish(engine, '验收')
    assert current(engine)['stage'] == '执行' and item(engine)['gate_reason'] == '本阶段证据未通过'
    flow.passed = True
    finish(engine, '验收')
    assert current(engine)['stage'] == '验收'


def test_rerun_refused_with_open_pr_wrong_commit_or_accepted_commit(world):
    data = snapshot(world, '执行')
    data['history'] = [{'stage': '验收', 'outcome': 'failed'}]
    receipt = lambda merge: Receipt('done', '', advance='验收', evidence=[result(world, rerun_acceptance={'merge': merge})])
    target = world['target']
    world['io'].save(data, target, {'effects': {}, 'issue': 7})
    assert '打开的 PR #11' in world['flow'].assess(data, receipt(world['sha']), target).reason
    pr = remote(world)['pr']
    pr.update(state='closed', merged=True, merge_commit_sha=world['sha'], merged_at='2026-11-13T08:00:00Z')
    edit(world, pr=pr)
    # 旧版本写的状态没有 delivered：release=none 合并即交付（ws#5、ws#18 就是这样）。
    assert world['flow'].assess(data, receipt(world['sha']), target).passed
    assert '不是本活最近的合并提交' in world['flow'].assess(data, receipt('f' * 40), target).reason
    assert 'rerun_acceptance.merge' in world['flow'].assess(
        data, Receipt('done', '', advance='验收', evidence=[result(world)]), target).reason
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'delivered': ['e' * 40]})
    assert '还没交付完成' in world['flow'].assess(data, receipt(world['sha']), target).reason
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'delivered': [world['sha']]})
    assert world['flow'].assess(data, receipt(world['sha']), target).passed
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'merge': world['sha'], 'accepted': True})
    assert '已验收通过' in world['flow'].assess(data, receipt(world['sha']), target).reason


def test_legacy_tag_state_checks_production_before_rerun(world):
    """旧状态没有交付记录、release=tag：当场核对线上是否包含这个合并提交。"""
    target = {**world['target'], 'release': 'tag'}
    data = snapshot(world, '执行')
    data['history'] = [{'stage': '验收', 'outcome': 'failed'}]
    world['io'].save(data, target, {'effects': {}, 'issue': 7})
    pr = remote(world)['pr']
    pr.update(state='closed', merged=True, merge_commit_sha=world['sha'], merged_at='2026-11-13T08:00:00Z')
    app = {'status': {'sync': {'status': 'Synced', 'revision': 'e' * 40}, 'health': {'status': 'Healthy'}}}
    edit(world, pr=pr, app=app, deployed_sha='e' * 40)
    receipt = Receipt('done', '', advance='验收', evidence=[result(world, rerun_acceptance={'merge': world['sha']})])
    assert '还没交付完成' in world['flow'].assess(data, receipt, target).reason
    edit(world, deployed_sha=world['sha'])
    assert world['flow'].assess(data, receipt, target).passed


def test_rerun_refuses_merged_commit_whose_release_did_not_finish(world):
    """审查 PR #25 的反例：A 上线后验收没过；B 合并了还没上线，交付退回执行后请求重走 B，不能进验收、不能关 issue。"""
    repo, a = world['target']['checkout'], world['sha']
    git(repo, 'checkout', '-qB', 'main')
    git(repo, 'checkout', '-qb', 'new-fix')
    b = commit(repo, 'fix.txt', 'new fix\n')
    git(repo, 'checkout', '-q', 'main')
    edit(world, app={'status': {'sync': {'status': 'Synced', 'revision': 'e' * 40}, 'health': {'status': 'Healthy'}}}, deployed_sha=a)
    engine = engine_for(world, 'a-then-b', release='tag')
    walk(engine, world, ['说清楚', '执行', '证据', '审查', '交付', '验收'])
    assert world['io'].read(item(engine, '7'), world['target'])['delivered'] == [a]
    first = remote(world)['pr']
    fail_acceptance(engine, world, a)
    edit(world, prs=[first], sha=b, merged_at='2026-11-13T09:00:00Z',
         pr={'number': 12, 'body': '<!-- workshop-item: alpha:7 -->', 'head': {'sha': b}, 'state': 'open', 'merged': False})
    walk(engine, world, ['证据', '审查', '交付'], head=b)
    finish(engine, '验收', key='7', evidence=[result(world, head=b)])
    assert current(engine, '7')['stage'] == '交付' and item(engine, '7')['gate_reason'] == '线上版本尚未核对通过'
    finish(engine, '执行', key='7', evidence=[result(world, head=b)])
    assert current(engine, '7')['stage'] == '执行'
    assert f'最近的合并提交 {b} 还没交付完成' in brief(engine) and 'rerun_acceptance=' not in brief(engine)
    rerun(engine, world, b)
    assert current(engine, '7')['stage'] == '执行' and '还没交付完成' in item(engine, '7')['gate_reason']
    rerun(engine, world, a)
    assert current(engine, '7')['stage'] == '执行' and '不是本活最近的合并提交' in item(engine, '7')['gate_reason']
    state = world['io'].read(item(engine, '7'), world['target'])
    assert state['delivered'] == [a] and 'acceptance_reruns' not in state
    assert remote(world)['issue']['state'] == 'open' and remote(world)['deployed_sha'] == a
    engine.log.close()


def test_acceptance_needs_the_commit_to_have_finished_delivery(world):
    data = snapshot(world, '验收')
    target = {**world['target'], 'release': 'tag'}
    receipt = Receipt('done', '', evidence=[result(world, acceptance={'passed': True, 'head': world['sha'], 'output': 'raw'})])
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'merge': world['sha'], 'delivered': ['e' * 40]})
    assert not world['flow'].assess(data, receipt, target).passed
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'merge': world['sha'], 'delivered': [world['sha']]})
    assert world['flow'].assess(data, receipt, target).passed


# 3. 车间合并自己后自更新

@pytest.fixture
def main_checkout(world):
    repo = world['target']['checkout']
    git(repo, 'checkout', '-qB', 'main')
    git(repo, 'checkout', '-qb', 'side')
    merged = commit(repo, 'feature.txt', 'merged change\n')
    git(repo, 'checkout', '-q', 'main')
    edit(world, sha=merged)
    return repo, merged


def test_self_release_fast_forwards_clean_main_and_requests_restart(world, main_checkout):
    repo, merged = main_checkout
    marker = world['root'] / 'prepared'
    target = {**world['target'], 'release': 'self', 'prepare': [{'argv': ['/bin/sh', '-c', f'pwd > {marker}']}]}
    data = snapshot(world, '交付')
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'reviewed_head': world['sha']})
    done = world['flow'].after(data, '交付', 'deliver', target)
    assert done.restart and not done.pending and not done.notice
    assert git(repo, 'rev-parse', 'HEAD') == merged and git(repo, 'symbolic-ref', '--short', 'HEAD') == 'main'
    assert Path(marker.read_text().strip()).resolve() == Path(repo).resolve()
    assert world['flow'].after(data, '交付', 'deliver', target) == done


def test_self_release_asks_core_to_restart_after_this_tick(world, main_checkout):
    repo, merged = main_checkout
    engine = engine_for(world, 'self-engine', release='self')
    walk(engine, world, ['说清楚', '执行', '证据', '审查', '交付'])
    assert not engine.restart_requested
    finish(engine, '验收', key='7', evidence=[result(world)])
    assert engine.restart_requested and current(engine, '7')['stage'] == '验收'
    assert git(repo, 'rev-parse', 'HEAD') == merged
    engine.log.close()


UNCLEAN = {'dirty': '未提交的改动或未跟踪的文件', 'untracked': '未提交的改动或未跟踪的文件',
           'branch': '不在 main', 'forked': '不能快进到合并提交'}


@pytest.mark.parametrize('problem', UNCLEAN)
def test_self_release_leaves_unclean_main_alone_and_notifies_once(world, main_checkout, problem):
    repo, merged = main_checkout
    if problem == 'dirty':
        Path(repo, 'scripts/check.sh').write_text('#!/bin/sh\necho Kun 正在改\n')
    elif problem == 'untracked':
        Path(repo, 'notes.txt').write_text('scratch\n')
    elif problem == 'branch':
        git(repo, 'checkout', '-qb', 'kuns-work')
    else:
        # 主检出 main 上有本地提交，和合并提交分叉：快进不了。
        commit(repo, 'local.txt', 'Kun 本地的提交\n')
    before = git(repo, 'rev-parse', 'HEAD')
    engine = engine_for(world, 'self-' + problem, release='self')
    walk(engine, world, ['说清楚', '执行', '证据', '审查', '交付'])
    finish(engine, '验收', key='7', evidence=[result(world)])
    assert current(engine, '7')['stage'] == '验收' and not engine.restart_requested
    assert git(repo, 'rev-parse', 'HEAD') == before
    if problem == 'dirty':
        assert 'Kun 正在改' in Path(repo, 'scripts/check.sh').read_text()
    notices = [c for c in engine.log.all('cards') if c['key'].startswith('after-notice:')]
    assert len(notices) == 1
    text = notices[0]['payload']['text']
    assert merged in text and 'git merge --ff-only' in text and 'kickstart' in text
    assert UNCLEAN[problem] in text
    engine.tick()
    assert len([c for c in engine.log.all('cards') if c['key'].startswith('after-notice:')]) == 1
    engine.log.close()


def test_self_release_prepare_failure_leaves_new_code_without_restart(world, main_checkout):
    """准备命令失败：主检出已经快进了，不重启，通知说清要重跑准备命令；活照常进验收。"""
    repo, merged = main_checkout
    # 只在主检出里失败；证据阶段在干净副本里跑同一组准备命令要成功。
    failing = f'[ "$(pwd -P)" != "{Path(repo).resolve()}" ] || {{ echo 依赖装不上 >&2; exit 1; }}'
    engine = engine_for(world, 'self-prepare', release='self', prepare=[{'argv': ['/bin/sh', '-c', failing]}])
    walk(engine, world, ['说清楚', '执行', '证据', '审查', '交付'])
    finish(engine, '验收', key='7', evidence=[result(world)])
    assert current(engine, '7')['stage'] == '验收' and not engine.restart_requested
    assert git(repo, 'rev-parse', 'HEAD') == merged
    state = world['io'].read(item(engine, '7'), world['target'])
    assert state['self_update']['updated'] and not state['self_update']['restart'] and state['delivered'] == [merged]
    [card] = [c for c in engine.log.all('cards') if c['key'].startswith('after-notice:')]
    text = card['payload']['text']
    assert '已快进主检出' in text and '依赖装不上' in text and '重跑' in text and '会用新代码起来' in text
    assert 'git merge --ff-only' not in text
    engine.log.close()


def test_round_started_in_the_restart_tick_survives_the_new_daemon(world, main_checkout):
    """请求重启的那一拍派出的验收轮，守护进程换了新进程后接着跑，不重派、只读副本不被清掉。"""
    repo, merged = main_checkout
    engine = engine_for(world, 'self-restart', release='self')
    walk(engine, world, ['说清楚', '执行', '证据', '审查', '交付'])
    finish(engine, '验收', key='7', evidence=[result(world)])
    row = current(engine, '7')
    assert engine.restart_requested and row['stage'] == '验收' and Path(row['brief']['workdir']).is_dir()
    # 守护进程退出；launchd 拉起的新进程用同一份事实库建引擎。执行者会话是独立进程组，还活着（同一批执行者替身）。
    path, rules, plugins, clock = engine.log.path, engine.rules, engine.plugins, engine.clock
    engine.log.close()
    revived = Engine(Log(path), rules, plugins, clock)
    assert not revived.restart_requested
    revived.tick()
    assert current(revived, '7')['token'] == row['token'] and Path(row['brief']['workdir']).is_dir()
    acceptor = plugins.get('executor', row['executor'])
    assert [c[1] for c in acceptor.calls if c[0] == 'launch'].count(row['token']) == 1
    pass_acceptance_and_close(revived, world, merged)
    assert not revived.restart_requested
    revived.log.close()


def test_run_loop_exits_after_the_tick_that_requested_restart(tmp_path, monkeypatch):
    import workshop.cli as cli
    from contextlib import nullcontext
    from types import SimpleNamespace
    ticks = []

    class Restarting:
        def __init__(self, *args, **kwargs):
            self.restart_requested = False

        def wake_paths(self):
            return {}

        def tick(self, received_at=None):
            ticks.append(True)
            self.restart_requested = True

    mailbox = SimpleNamespace(wait=lambda seconds: pytest.fail('重启请求后不再等下一拍'))
    monkeypatch.setattr(cli, 'Engine', Restarting)
    monkeypatch.setattr(cli, 'Mailbox', lambda *args: nullcontext(mailbox))
    assert cli.main(['--state-dir', str(tmp_path), 'run', '--interval', '60']) == 0
    assert ticks == [True]


# 4. 执行者守边界

def test_execution_brief_states_boundaries(world):
    text = world['flow'].brief(snapshot(world, '执行'), '执行', world['target'])
    for phrase in ('只改本 issue 要的东西', '车间流程或环境的问题', 'outcome=nothing', 'blocked={reason:原因,next:建议动作}',
                   '不改车间自己的流程代码', f"不动主检出 {world['target']['checkout']}", '不重启车间'):
        assert phrase in text
    # 没到过验收的活不提重走验收，也不为此查 GitHub。
    assert 'rerun_acceptance' not in text
    assert not any(c[0] == 'gh' and any('/pulls?' in a for a in c) for c in calls(world))
    assert '不要动主检出、不要重启' in world['flow'].brief(snapshot(world, '交付'), '交付', world['target'])


# 5. 恢复 11-13 卡住的两件

def quarantine(engine):
    engine.broken('alpha', 'flow', 'issue_delivery', Retryable('命令失败：gh（exit 1）'))
    assert item(engine, '7')['state'] == '卡住'


@pytest.mark.parametrize('release', ['ready', 'rework'])
def test_recover_ws5_waiting_after_nothing(world, release):
    """ws#5：已合并、验收两次没过，执行交 nothing 等你，又被插件隔离。重打标签或在 issue 下评论「不对」都能重走验收收尾。"""
    engine = engine_for(world, 'ws5-' + release)
    walk(engine, world, ['说清楚', '执行', '证据', '审查', '交付', '验收'])
    sha = remote(world)['pr']['merge_commit_sha']
    fail_acceptance(engine, world, sha)
    card = {**notice('ws#7 验收要重走，需你定路径'), 'key': 'rerun-acceptance'}
    engine.submit(current(engine, '7')['token'], {'outcome': 'nothing', 'summary': '没有要改的代码', 'notice': [card],
                                                  'leave': {'kind': '等你', 'about': '等 Kun 定怎么重走验收'}})
    engine.tick()
    assert item(engine, '7')['state'] == '卡住' and item(engine, '7')['wait_for_ready']
    quarantine(engine)
    engine.resume('alpha', [])
    assert item(engine, '7')['state'] == '卡住' and not engine.active()
    engine.ingest(Event('github', 'kun-again', 'alpha', '7', 'label', {}, action=release))
    engine.tick()
    assert current(engine, '7')['stage'] == '执行' and 'rerun_acceptance' in brief(engine)
    rerun(engine, world, sha)
    assert current(engine, '7')['stage'] == '验收'
    pass_acceptance_and_close(engine, world, sha)
    assert merges(world) == 1
    engine.log.close()


@pytest.mark.parametrize('release', ['resume', 'rework'])
def test_recover_ws18_delivery_stuck_on_closed_duplicate_pr(world, release):
    """ws#18：#11 已合并，验收没过后执行者又开了重复的 #23；合并 #23 失败隔离了插件，Kun 已关 #23。"""
    engine = engine_for(world, 'ws18-' + release)
    walk(engine, world, ['说清楚', '执行', '证据', '审查', '交付', '验收'])
    first = remote(world)['pr']
    sha = first['merge_commit_sha']
    fail_acceptance(engine, world, sha)
    repo = world['target']['checkout']
    git(repo, 'checkout', '-qb', 'duplicate')
    duplicate = commit(repo, 'duplicate.txt', '重复的修正\n')
    edit(world, prs=[first], pr={'number': 23, 'body': '<!-- workshop-item: alpha:7 -->', 'head': {'sha': duplicate},
                                 'state': 'open', 'merged': False, 'mergeable': False, 'mergeable_state': 'dirty'})
    walk(engine, world, ['证据', '审查', '交付'], head=duplicate)
    token = current(engine, '7')['token']
    engine.submit(token, {'outcome': 'done', 'summary': '交付', 'advance': '验收', 'evidence': [result(world, head=duplicate)]})
    engine.fact('effect', {'id': f'after:{token}', 'done': False})
    quarantine(engine)
    engine.tick()
    assert item(engine, '7')['state'] == '卡住'
    edit(world, pr={**remote(world)['pr'], 'state': 'closed'})
    engine.resume('alpha', [])
    if release == 'rework':
        # Kun 在 issue 下评论「不对」：GitHub 来源核对是 kun-g 后给 rework。workshop 没绑飞书群，群里说认不出来。
        engine.ingest(Event('github', 'kun-says-wrong', 'alpha', '7', '不对', {}, action='rework'))
    engine.tick()
    assert not engine.control('alpha').get('broken')
    if release == 'resume':
        assert current(engine, '7')['stage'] == '交付' and '已关闭且没合并' in item(engine, '7')['gate_reason']
        finish(engine, '执行', key='7', evidence=[result(world, head=duplicate)])
    assert current(engine, '7')['stage'] == '执行'
    assert f'PR #11，合并提交 {sha}' in brief(engine)
    rerun(engine, world, sha)
    assert current(engine, '7')['stage'] == '验收' and f'只验固定检查提交 {sha}' in brief(engine)
    pass_acceptance_and_close(engine, world, sha)
    assert merges(world) == 1
    engine.log.close()


def test_completion_notice_goes_out_once_per_merge_commit(world):
    """ws#46：验收、重走验收、收尾里执行者各自发的完成通知，同一合并提交只出一张卡；进度通知不出卡。"""
    engine = engine_for(world, 'quiet')
    progress = {'notice': [{'key': 'opened', 'text': 'ws#7 PR #11 已开'}]}
    finish(engine, '说清楚', key='7', evidence=[result(world)], notice=[{'key': 'intake', 'text': 'ws#7 已接单'}])
    finish(engine, '执行', key='7', evidence=[result(world, checks=[['scripts/check.sh']])])
    finish(engine, '证据', key='7', evidence=[result(world)], **progress)
    walk(engine, world, ['审查', '交付', '验收'])
    sha = remote(world)['pr']['merge_commit_sha']
    fail_acceptance(engine, world, sha)
    rerun(engine, world, sha)
    done = {'acceptance': {'passed': True, 'output': 'acceptance raw output', 'head': sha}}
    finish(engine, '收尾', key='7', evidence=[result(world, **done)],
           notice=[{'key': 'acceptance-ws7', 'text': 'ws#7 已验收'}])
    cards = [c for c in engine.log.all('cards') if c['mode'] == 'notice']
    assert [(c['key'], c['payload']['text']) for c in cards] == [(f'notice:完成:{sha}', 'ws#7 已验收')]
    finish(engine, None, key='7', evidence=[result(world)], leave=CLOSING['leave'],
           notice=[*CLOSING['notice'], {'key': 'accepted-2', 'text': 'ws#7 已验收（又一条）；不对就在群里回一句「ws#7 不对」'}])
    assert item(engine, '7')['state'] == '完成'
    cards = [c for c in engine.log.all('cards') if c['mode'] == 'notice']
    assert [(c['key'], c['payload']['text']) for c in cards] == [(f'notice:完成:{sha}', 'ws#7 已验收')]
    held = [f['body'] for f in engine.log.facts('alpha:7') if f['kind'] == 'notice_held']
    assert [h['key'] for h in held] == ['intake', 'opened', 'accepted', 'accepted-2']
    engine.log.close()


def test_closing_evidence_carries_the_merge_commit(world):
    data = snapshot(world, '收尾')
    world['io'].save(data, world['target'], {'effects': {}, 'issue': 7, 'accepted': True, 'merge': world['sha']})
    receipt = Receipt('done', '', leave={'kind': '送回', 'at': '+7d', 'about': '复看'},
                      notice=[{**notice('已验收；不对就在群里回一句「ws#7 不对」'), 'key': 'accepted'}])
    evidence = world['flow'].assess(data, receipt, world['target'])
    assert evidence.passed and evidence.done_key == world['sha']


@pytest.mark.parametrize('stage', ['接单', '说清楚', '执行', '证据', '审查', '交付', '验收', '收尾'])
def test_briefs_say_no_progress_notices_except_closing(world, stage):
    data = snapshot(world, stage)
    world['io'].save(data, world['target'], {'effects': {}, 'issue': 7})
    text = world['flow'].brief(data, stage, world['target'])
    text = text if isinstance(text, str) else text.instructions
    assert ('不发进度通知' in text) is (stage != '收尾')
    if stage == '收尾':
        assert 'key=accepted' in text and 'ws#7 不对' in text
