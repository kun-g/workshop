import base64
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess

import pytest

from workshop.core.models import After, Classification, Event, FlowBrief, Receipt
from workshop.plugins.issue_delivery import IssueDelivery, action_for
from workshop.plugins.issue_delivery.io import IO, clean_env
from workshop.plugins.sources.github import GitHubSource
from conftest import build, configuration, current, finish, item


BODY = '''## Kun 的原话
检查失败时让错误可见。
## 验收标准
错误输出和非零退出码保留。
## 验收步骤
运行检查，并观察 stdout。
## 能跑的检查
```workshop-checks
[["scripts/check.sh"]]
```
## 要不要亲眼看
否
'''


@pytest.fixture
def world(tmp_path, monkeypatch):
    commands = tmp_path / 'bin'
    commands.mkdir()
    script = Path(__file__).parent / 'fixtures/issue_delivery/fake_command.py'
    for name in ('gh', 'git', 'lark-cli', 'kubectl'):
        file = commands / name
        file.write_text(script.read_text())
        file.chmod(0o755)
    repo = tmp_path / 'checkout'
    repo.mkdir()
    (repo / 'scripts').mkdir()
    check = repo / 'scripts/check.sh'
    check.write_text('#!/bin/sh\nprintf "raw check output\\n"\n')
    check.chmod(0o755)
    tag = repo / 'scripts/tag-release.sh'
    tag.write_text(script.read_text())
    tag.chmod(0o755)
    subprocess.run(['/usr/bin/git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['/usr/bin/git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['/usr/bin/git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=t@localhost', 'commit', '-qm', 'fixture'], check=True)
    sha = subprocess.check_output(['/usr/bin/git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    store = tmp_path / 'remote.json'
    state = {'sha': sha, 'checkout': str(repo), 'issue': {'number': 7, 'body': BODY, 'state': 'open'},
             'pr': {'number': 11, 'body': '<!-- workshop-item: alpha:7 -->', 'head': {'sha': sha}, 'state': 'open', 'merged': False}}
    store.write_text(json.dumps(state))
    monkeypatch.setenv('WORKSHOP_FAKE_STATE', str(store))
    monkeypatch.setenv('PATH', str(commands) + os.pathsep + os.environ['PATH'])
    target = {'name': 'alpha', 'repo': 'kun-g/workshop', 'checkout': str(repo), 'release': 'none',
              'ci': ['check'], 'short_name': 'ws', 'github_cache_dir': str(tmp_path / 'cache'),
              'tag_command': ['scripts/tag-release.sh'], 'delivery_action': '自己的交付', 'feishu_chat_id': 'oc_group', 'kun_feishu_open_id': 'ou_kun',
              'ask_kun_state': str(tmp_path / 'asks.json'), 'deployment': {'app': 'test', 'context': 'readonly', 'namespace': 'argocd', 'revision_field': 'status.sync.revision', 'branch': 'production', 'release_file': 'release.json', 'source_field': 'source_commit'}}
    io = IO(str(commands / 'gh'), str(commands / 'git'), tmp_path / 'flow', str(commands / 'lark-cli'))
    return dict(store=store, commands=commands, target=target, io=io, flow=IssueDelivery(io), sha=sha, root=tmp_path)


def edit(world, **changes):
    state = json.loads(world['store'].read_text())
    state.update(changes)
    world['store'].write_text(json.dumps(state))
    return state


def calls(world):
    path = world['store'].with_suffix('.calls')
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def result(world, **extra):
    return json.dumps({'schema': 'issue-delivery/v1', 'head': world['sha'], **extra})


def snapshot(world, stage='接单'):
    return {'id': 'alpha:7', 'target': 'alpha', 'object_key': '7', 'stage': stage, 'materials': [],
            'checks': [['scripts/check.sh']], 'history': [],
            'result': {'outcome': 'done', 'summary': 'done', 'evidence': [result(world)]}}


def message(text='不对', sender='ou_kun', parent=None, chat='oc_group'):
    return {'ok': True, 'data': {'items': [{'message_id': 'om_real', 'msg_type': 'text', 'chat_id': chat,
            'sender': {'id': sender, 'id_type': 'open_id', 'sender_type': 'user'},
            'body': {'content': json.dumps({'text': text})}, 'parent_id': parent}]}}


def source_responses(world):
    base = 'repos/kun-g/workshop'
    sha = world['sha']
    return {
        base + '/issues?state=all&labels=Ready+for+Agent&per_page=100': [{'number': 7}],
        base + '/issues/7': {'number': 7, 'body': BODY},
        base + '/issues/7/events?per_page=100': [{'id': 80, 'event': 'labeled', 'label': {'name': 'Ready for Agent'}, 'actor': {'login': 'kun-g'}}],
        base + '/issues/7/comments?per_page=100': [],
        base + '/pulls?state=all&per_page=100': [],
        base + '/issues/11/comments?per_page=100': [],
        base + '/pulls/11/comments?per_page=100': [],
        base + '/pulls/11/reviews?per_page=100': [],
        base + '/commits/' + sha + '/check-runs?per_page=100': {'check_runs': []},
        base + '/commits/' + sha + '/status': {'statuses': []},
    }


def test_source_labels_comments_etag_and_replay(world):
    responses = source_responses(world)
    edit(world, responses=responses)
    source = GitHubSource(str(world['commands'] / 'gh'))
    batch = source.poll(world['target'], None)
    assert any(e.action == 'ready' for e in batch.events)
    assert {e.object_key for e in batch.events} == {'7'}
    assert source.poll(world['target'], batch.cursor).events == []
    assert source.poll(world['target'], None).events == batch.events
    assert any('If-None-Match: "AbC' in a for call in calls(world) for a in call)
    comments = [{'id': n, 'body': '不对' if n < 3 else '普通意见', 'user': {'login': 'kun-g' if n == 1 else 'other'}} for n in (1, 2, 3)]
    comments += [{'id': 4, 'body': 'done', 'user': {'login': 'bot', 'type': 'Bot'}},
                 {'id': 5, 'body': '<!-- workshop-comment: alpha:7 --> done', 'user': {'login': 'kun-g'}}]
    responses['repos/kun-g/workshop/issues/7/comments?per_page=100'] = comments
    edit(world, responses=responses)
    new = source.poll(world['target'], batch.cursor)
    assert len(new.events) == 3 and {e.object_key for e in new.events} == {'7'}
    assert [e.action for e in new.events] == ['rework', 'update', 'update']
    assert all('kun-g/workshop:issue-comment:7:' in e.ext_id for e in new.events)
    engine = build(world['root'] / 'core')
    for event in [*batch.events, *new.events, *new.events]:
        engine.ingest(event)
    assert len(engine.log.all('items')) == 1
    engine.log.close()


def test_source_pr_marker_and_pagination(world):
    responses = source_responses(world)
    pr = json.loads(world['store'].read_text())['pr']
    responses['repos/kun-g/workshop/pulls?state=all&per_page=100'] = [pr]
    endpoint = 'repos/kun-g/workshop/issues/11/comments?per_page=100'
    responses[endpoint] = {'_response': True, 'data': [], 'next': endpoint + '&page=2'}
    responses[endpoint + '&page=2'] = [{'id': 99, 'body': '不对，重做', 'user': {'login': 'kun-g'}}]
    edit(world, responses=responses)
    events = GitHubSource(str(world['commands'] / 'gh')).poll(world['target'], None).events
    assert all(e.object_key == '7' for e in events)
    assert any(e.body['kind'] == 'pr-comment' and e.action == 'rework' for e in events)
    assert any(e.body['kind'] == 'pr-ci' for e in events)


def test_classify_checks_upstream_sender_text_chat_and_reference(world):
    flow, target = world['flow'], world['target']
    event = Event('cli:feishu', 'om_real', 'alpha', 'om_real', 'untrusted', {'from_kun': True, 'text': '不对'})
    edit(world, message=message(sender='ou_attacker'))
    assert flow.classify(event, None, target, lambda _: None) == Classification(discard=True)
    edit(world, message=message(text='普通资料'))
    assert flow.classify(event, None, target, lambda _: None) == Classification()
    edit(world, message=message(chat='oc_other'))
    assert flow.classify(event, None, target, lambda _: None) == Classification(discard=True)
    edit(world, message=message(text='@Ash 不对', parent='om_card'))
    Path(target['ask_kun_state']).write_text(json.dumps({'asks': {'Q1': {'id': 'Q1', 'message_id': 'om_card', 'chat_id': 'oc_group'}}}))
    assert flow.classify(event, None, target, lambda q: {'item': {'object_key': '7'}} if q == 'Q1' else None) == Classification('rework', '7')
    assert flow.classify(Event('cli:github', 'om_real', 'alpha', '7', 'x', {'from_kun': True}), None, target, lambda _: None) == Classification()
    Path(target['ask_kun_state']).write_text('{broken')
    assert flow.classify(event, None, target, lambda _: None) == Classification()


def test_intake_creates_issue_once_and_binds_alias(world):
    edit(world, issue=None, message=message(text='改一下错误提示'))
    data = snapshot(world)
    data.update(id='alpha:om_real', object_key='om_real', materials=[{'source': 'cli:feishu', 'ext_id': 'om_real'}])
    assert world['flow'].after(data, '接单', 'key-intake', world['target']).aliases == ['7']
    assert world['flow'].after(data, '接单', 'key-intake', world['target']).aliases == ['7']
    assert len([c for c in calls(world) if '--input' in c]) == 1
    # 写入成功、本地结果丢失：以远端标记回查，不重新创建。
    state = world['io'].read(data, world['target'])
    state.pop('issue')
    state['effects'].clear()
    world['io'].save(data, world['target'], state)
    assert world['flow'].after(data, '接单', 'key-intake', world['target']).aliases == ['7']
    assert len([c for c in calls(world) if '--input' in c]) == 1


def test_evidence_reruns_checks_review_is_readonly(world):
    data = snapshot(world, '证据')
    receipt = Receipt('done', 'not trusted', evidence=[result(world)])
    evidence = world['flow'].assess(data, receipt, world['target'])
    assert evidence.passed
    assert json.loads(evidence.reason)['runs'][0]['stdout'] == 'raw check output\n'
    brief = world['flow'].brief(data, '审查', world['target'])
    assert isinstance(brief, FlowBrief) and brief.readonly
    assert Path(brief.workdir).stat().st_mode & 0o222 == 0
    assert subprocess.check_output(['/usr/bin/git', '-C', brief.workdir, 'remote', 'get-url', 'origin'], text=True).strip() == 'https://invalid.invalid/read-only'
    assert 'AWS_ACCESS_KEY_ID' not in clean_env(Path('/tmp/home'))
    assert not world['flow'].assess(data, Receipt('done', 'lying', evidence=[result(world, head='f' * 40)]), world['target']).passed


def test_after_merge_close_tag_are_idempotent_and_merge_keeps_issue(world):
    target = {**world['target'], 'release': 'tag'}
    data = snapshot(world, '交付')
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'reviewed_head': world['sha']})
    first = world['flow'].after(data, '交付', 'delivery-1', target)
    assert first.pending
    assert world['flow'].after(data, '交付', 'delivery-1', target) == first
    state = json.loads(world['store'].read_text())
    assert state['issue']['state'] == 'open' and state['tags']
    assert len([c for c in calls(world) if c[1:3] == ['pr', 'merge']]) == 1
    assert len([c for c in calls(world) if c[0] == 'tag-release.sh']) == 1
    app = {'spec': {'source': {'repoURL': 'git@github.com:kun-g/workshop.git', 'targetRevision': 'production', 'path': 'manifests'}},
           'status': {'sync': {'status': 'Synced', 'revision': 'e' * 40}, 'health': {'status': 'Healthy'}}}
    edit(world, app=app, deployed_sha=world['sha'])
    assert not world['flow'].after(data, '交付', 'delivery-2', target).pending
    assert len([c for c in calls(world) if c[0] == 'tag-release.sh']) == 1
    world['flow'].after(data, '验收', 'accept', target)
    world['flow'].after(data, '收尾', 'close', target)
    world['flow'].after(data, '收尾', 'close', target)
    assert len([c for c in calls(world) if c[1:3] == ['issue', 'close']]) == 1
    merge = next(c for c in calls(world) if c[1:3] == ['pr', 'merge'])
    assert merge[merge.index('--body') + 1] == 'Related to #7'
    assert merge[merge.index('--match-head-commit') + 1] == world['sha']


def test_engine_eight_stages_with_real_flow_and_fake_executor(world):
    cfg = configuration()
    cfg.pop('taskhome')
    cfg['targets']['alpha'] = {**world['target'], 'plugin': 'issue_delivery', 'sources': []}
    engine = build(world['root'] / 'engine', cfg)
    engine.plugins.register('flow', 'issue_delivery', world['flow'])
    engine.ingest(Event('github', 'labeled', 'alpha', '7', 'test', {}))
    engine.tick()
    stages = list(world['flow'].stages(world['target']))
    for stage in stages:
        assert current(engine, '7')['stage'] == stage
        payload = {'head': world['sha'], 'checks': [['scripts/check.sh']], 'must_fix': [], 'suggestions': [],
                   'acceptance': {'passed': True, 'output': 'acceptance raw output', 'head': world['sha']}}
        extra = {}
        if stage == '收尾':
            extra = {'leave': {'kind': '送回', 'at': '+7d', 'about': '复看'}, 'notice': [{'key': 'accepted', 'text': '验收通过；不对就在群里回一句「ws#7 不对」'}]}
        advance = stages[stages.index(stage) + 1] if stage != '收尾' else None
        finish(engine, advance, key='7', evidence=[result(world, **payload)], **extra)
        if stage == '交付':
            assert json.loads(world['store'].read_text())['issue']['state'] == 'open'
    assert item(engine, '7')['state'] == '完成'
    assert json.loads(world['store'].read_text())['issue']['state'] == 'closed'
    engine.log.close()


@pytest.mark.parametrize('body', ['Closes #7', 'Closes: #7', 'Closes:#7', 'Fixes: #7', 'Fixes kun-g/workshop#7', 'Resolves https://github.com/kun-g/workshop/issues/7'])
def test_auto_close_references_are_rejected(world, body):
    state = json.loads(world['store'].read_text())
    state['pr']['body'] += '\n' + body
    edit(world, pr=state['pr'])
    data = snapshot(world, '证据')
    assert not world['flow'].assess(data, Receipt('done', '', evidence=[result(world)]), world['target']).passed


def test_required_checks_and_unclear_issue_do_not_pass(world):
    data = snapshot(world, '说清楚')
    receipt = Receipt('done', '', evidence=[result(world, checks=[['echo', 'pass']])])
    remote = json.loads(world['store'].read_text())
    remote['issue']['body'] = BODY.replace('[["scripts/check.sh"]]', '[["echo", "pass"]]')
    edit(world, issue=remote['issue'])
    assert not world['flow'].assess(data, receipt, world['target']).passed
    remote['issue']['body'] = BODY.replace('错误输出和非零退出码保留。', '待定')
    edit(world, issue=remote['issue'])
    receipt = Receipt('done', '', evidence=[result(world, checks=[['scripts/check.sh']])])
    assert not world['flow'].assess(data, receipt, world['target']).passed


def test_changed_head_and_failed_ci_prevent_merge(world):
    data = snapshot(world, '交付')
    world['io'].save(data, world['target'], {'effects': {}, 'issue': 7, 'reviewed_head': 'f' * 40})
    pending = world['flow'].after(data, '交付', 'changed', world['target'])
    assert pending.pending and pending.rerun and '合并前头提交' in pending.reason
    assert not any(c[1:3] == ['pr', 'merge'] for c in calls(world))
    world['io'].save(data, world['target'], {'effects': {}, 'issue': 7, 'reviewed_head': world['sha']})
    edit(world, checks=[{'name': 'check', 'status': 'completed', 'conclusion': 'failure'}])
    assert world['flow'].after(data, '交付', 'ci', world['target']).pending
    assert not any(c[1:3] == ['pr', 'merge'] for c in calls(world))


def test_review_failure_does_not_record_approved_head(world):
    data = snapshot(world, '审查')
    world['io'].save(data, world['target'], {'effects': {}, 'issue': 7, 'evidence_head': world['sha']})
    receipt = Receipt('done', '', advance='执行', evidence=[result(world, must_fix=['bug'], suggestions=[])])
    assert not world['flow'].assess(data, receipt, world['target']).passed
    data['result'] = asdict(receipt)
    world['flow'].after(data, '审查', 'back', world['target'])
    assert not world['io'].read(data, world['target']).get('reviewed_head')


def test_visual_acceptance_requires_actual_answer_and_bound_commit(world):
    data = snapshot(world, '验收')
    state = json.loads(world['store'].read_text())
    state['issue']['body'] = BODY.replace('否\n', '是\n看一下界面\n')
    edit(world, issue=state['issue'])
    world['io'].save(data, world['target'], {'effects': {}, 'issue': 7, 'merge': world['sha']})
    receipt = Receipt('done', '', evidence=[result(world, acceptance={'passed': True, 'head': world['sha'], 'output': 'raw'})])
    assert not world['flow'].assess(data, receipt, world['target']).passed
    data['materials'] = [{'role': 'Kun 的回答', 'key': 'token:visual-acceptance', 'body': 'yes'}]
    assert world['flow'].assess(data, receipt, world['target']).passed
    data['materials'][0]['body'] = 'no'
    assert not world['flow'].assess(data, receipt, world['target']).passed


def test_relabel_after_unclear_resumes_same_item(world):
    cfg = configuration()
    cfg.pop('taskhome')
    cfg['targets']['alpha'] = {**world['target'], 'plugin': 'issue_delivery', 'sources': []}
    engine = build(world['root'] / 'unclear', cfg)
    engine.plugins.register('flow', 'issue_delivery', world['flow'])
    engine.ingest(Event('github', 'start', 'alpha', '7', 'test', {}))
    engine.tick()
    finish(engine, '说清楚', key='7')
    finish(engine, key='7', notice=[{'key': 'clarify', 'text': '先聊清楚标准'}], leave={'kind': '等你', 'about': '改 issue 后重打标签'})
    assert item(engine, '7')['state'] == '等你'
    engine.ingest(Event('github', 'comment', 'alpha', '7', 'test', {}))
    engine.tick()
    assert item(engine, '7')['state'] == '等你'
    engine.ingest(Event('github', 'relabel', 'alpha', '7', 'test', {}, action='ready'))
    engine.tick()
    assert current(engine, '7')['stage'] == '说清楚' and len(engine.log.all('items')) == 1
    engine.log.close()


def test_sensitive_paths_require_two_reviewers_before_delivery(world):
    cfg = configuration()
    cfg.pop('taskhome')
    cfg['targets']['alpha'] = {**world['target'], 'plugin': 'issue_delivery', 'sources': [], 'two_reviewers': ['deploy/']}
    cfg['executors']['reviewer'].append({'use': 'acceptor', 'family': 'c', 'billing': 'subscription'})
    edit(world, files=[{'filename': 'deploy/config.yaml'}])
    engine = build(world['root'] / 'two', cfg)
    engine.plugins.register('flow', 'issue_delivery', world['flow'])
    engine.ingest(Event('github', 'start', 'alpha', '7', 'test', {}))
    engine.tick()
    for stage in ['说清楚', '执行', '证据', '审查']:
        finish(engine, stage, key='7', evidence=[result(world, checks=[['scripts/check.sh']])])
    assert current(engine, '7')['config']['family'] == 'b'
    finish(engine, '交付', key='7', evidence=[result(world, must_fix=[], suggestions=['optional'])])
    assert current(engine, '7')['stage'] == '审查' and current(engine, '7')['config']['family'] == 'c'
    assert not world['io'].read(item(engine, '7'), world['target']).get('reviewed_head')
    finish(engine, '交付', key='7', evidence=[result(world, must_fix=[], suggestions=[])])
    assert current(engine, '7')['stage'] == '交付'
    assert world['io'].read(item(engine, '7'), world['target'])['reviewed_head'] == world['sha']
    engine.log.close()


def test_failed_real_check_overrides_receipt_claim(world):
    repo = world['target']['checkout']
    Path(repo, 'scripts/check.sh').write_text('#!/bin/sh\necho actual-failure >&2\nexit 2\n')
    subprocess.run(['/usr/bin/git', '-C', repo, 'add', '.'], check=True)
    subprocess.run(['/usr/bin/git', '-C', repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost', 'commit', '-qm', 'fail'], check=True)
    sha = subprocess.check_output(['/usr/bin/git', '-C', repo, 'rev-parse', 'HEAD'], text=True).strip()
    state = json.loads(world['store'].read_text())
    state['pr']['head']['sha'] = sha
    edit(world, pr=state['pr'])
    evidence = world['flow'].assess(snapshot(world, '证据'), Receipt('done', '全部通过', evidence=[result(world, head=sha)]), world['target'])
    assert not evidence.passed
    assert json.loads(evidence.reason)['runs'][0]['exit_code'] == 2
    assert json.loads(evidence.reason)['runs'][0]['stderr'] == 'actual-failure\n'


def test_old_visual_answer_does_not_approve_new_delivery(world):
    data = snapshot(world, '验收')
    state = json.loads(world['store'].read_text())
    state['issue']['body'] = BODY.replace('否\n', '是\n')
    edit(world, issue=state['issue'])
    world['io'].save(data, world['target'], {'effects': {}, 'issue': 7, 'merge': world['sha'], 'acceptance_after': 10})
    data['materials'] = [{'role': 'Kun 的回答', 'key': 'token:visual-acceptance', 'seq': 9, 'body': 'yes'}]
    receipt = Receipt('done', '', evidence=[result(world, acceptance={'passed': True, 'head': world['sha'], 'output': 'raw'})])
    assert not world['flow'].assess(data, receipt, world['target']).passed


def test_close_recovers_after_remote_success_without_repeating(world):
    data = snapshot(world, '收尾')
    world['io'].save(data, world['target'], {'effects': {}, 'issue': 7, 'accepted': True})
    world['flow'].after(data, '收尾', 'close', world['target'])
    state = world['io'].read(data, world['target'])
    state['effects'].clear()
    world['io'].save(data, world['target'], state)
    world['flow'].after(data, '收尾', 'close', world['target'])
    assert len([c for c in calls(world) if c[1:3] == ['issue', 'close']]) == 1


def test_source_bounded_ticks_finish_large_sweep_without_losing_events(world):
    edit(world, responses=source_responses(world))
    source = GitHubSource(str(world['commands'] / 'gh'))
    cursor, events = None, []
    target = {**world['target'], 'github_requests_per_tick': 1}
    for _ in range(10):
        before = len(calls(world))
        batch = source.poll(target, cursor)
        assert len(calls(world)) - before <= 1
        events.extend(batch.events)
        cursor = batch.cursor
        if not json.loads(cursor)['refreshed']:
            break
    else:
        pytest.fail('scan starved instead of continuing across ticks')
    assert {e.body['kind'] for e in events} == {'issue', 'label'}
    assert len({e.ext_id for e in events}) == len(events)


@pytest.mark.parametrize('text', ['不对，先换这个做法', '继续'])
def test_orca_steering_records_existing_item_without_action_depth_or_dispatch(world, text):
    cfg = configuration()
    cfg.pop('taskhome')
    cfg['targets']['alpha'] = {**world['target'], 'plugin': 'issue_delivery', 'sources': []}
    engine = build(world['root'] / 'orca-steering', cfg)
    engine.plugins.register('flow', 'issue_delivery', world['flow'])
    engine.ingest(Event('github', 'start', 'alpha', '7', 'test', {}, depth=2))
    engine.tick()
    token = current(engine, '7')['token']
    # 与 W2-B 钩子一致：事件携带对象键及活 id，不传 round_token。
    steering = Event('orca', 'prompt-1', 'alpha', '7', 'Orca 插话',
                     {'item_id': 'alpha:7', 'text': text}, depth=8, action='rework')
    assert engine.ingest(steering, ingress='external')
    engine.tick()
    recorded = item(engine, '7')
    assert len(engine.log.all('items')) == 1
    assert recorded['depth'] == 2 and not recorded['pending_rework']
    assert recorded['stage'] == '接单' and current(engine, '7')['token'] == token
    assert recorded['materials'][-1]['source'] == 'cli:orca'
    assert recorded['materials'][-1]['body'] == steering.body
    classification = [f for f in engine.log.facts() if f['kind'] == 'event_classified'][-1]
    assert classification['body']['action'] == 'update'
    assert classification['body']['event']['object_key'] == '7'
    assert classification['body']['event']['depth'] == 0
    # 正文自称别的活、未知活或跨目标都不能提供新的归件依据。
    lookup = lambda _: pytest.fail('Orca 插话不查卡片或授予动作')
    known = Event('cli:orca', 'known', 'alpha', '7', 'Orca 插话', steering.body)
    assert world['flow'].classify(known, recorded, world['target'], lookup) == Classification('update', '7')
    event = Event('cli:orca', 'other', 'alpha', '7', 'Orca 插话', {'item_id': 'alpha:other', 'text': '不对'})
    assert world['flow'].classify(event, recorded, world['target'], lookup) == Classification()
    assert world['flow'].classify(event, None, world['target'], lookup) == Classification()
    assert world['flow'].classify(event, {**recorded, 'target': 'beta'}, world['target'], lookup) == Classification()
    engine.log.close()


def test_source_control_ids_survive_edits_reactions_and_cursor_replay(world):
    responses = source_responses(world)
    endpoint = 'repos/kun-g/workshop/issues/7/comments?per_page=100'
    responses[endpoint] = [{'id': 1, 'body': '不对', 'user': {'login': 'kun-g'}}]
    edit(world, responses=responses)
    source = GitHubSource(str(world['commands'] / 'gh'))
    first = source.poll(world['target'], None)
    original = next(e for e in first.events if e.action == 'rework')
    assert original.ext_id == 'kun-g/workshop:issue-comment:7:1'
    for field, value in [('reactions', {'+1': 1}), ('body', '不对，换个做法')]:
        responses[endpoint][0][field] = value
        edit(world, responses=responses)
        batch = source.poll(world['target'], first.cursor)
        assert batch.events and all(e.action == 'update' for e in batch.events)
        # 丢了本地游标重放，控制事件仍与第一次相同，核心可以去重。
        replay = source.poll(world['target'], None)
        assert next(e.ext_id for e in replay.events if e.action == 'rework') == original.ext_id
    cursor = json.loads(batch.cursor)
    assert all(set(v) == {'etag', 'fingerprint', 'next'} for v in cursor['cache'].values())
    assert BODY not in batch.cursor
    assert list(Path(world['target']['github_cache_dir']).glob('*.json'))


def test_own_label_non_kun_label_and_closed_initial_issue(world):
    responses = source_responses(world)
    responses['repos/kun-g/workshop/issues/7']['body'] += '\n<!-- workshop-intake: alpha:om_real -->'
    labels = responses['repos/kun-g/workshop/issues/7/events?per_page=100']
    labels.append({**labels[0], 'id': 81, 'actor': {'login': 'other'}})
    edit(world, responses=responses)
    source = GitHubSource(str(world['commands'] / 'gh'))
    first = source.poll(world['target'], None)
    assert all(e.action == 'update' for e in first.events)
    labels.append({**labels[0], 'id': 82})
    edit(world, responses=responses)
    second = source.poll(world['target'], first.cursor)
    assert [e.ext_id for e in second.events if e.action == 'ready'] == ['kun-g/workshop:label:7:82']
    responses['repos/kun-g/workshop/issues?state=all&labels=Ready+for+Agent&per_page=100'][0]['state'] = 'closed'
    edit(world, responses=responses)
    assert source.poll(world['target'], None).events == []


@pytest.mark.parametrize('ci,checks,expected', [
    ('none', [], True), (['a'], [], False),
    (['a'], [{'name': 'a', 'status': 'completed', 'conclusion': 'skipped'}], False),
    (['a', 'b'], [{'name': 'a', 'status': 'completed', 'conclusion': 'success'}, {'name': 'b', 'status': 'completed', 'conclusion': 'skipped'}], True),
    (['a'], [{'name': 'a', 'status': 'in_progress', 'conclusion': None}], False),
])
def test_ci_policy(world, ci, checks, expected):
    edit(world, checks=checks, statuses=[])
    pr = json.loads(world['store'].read_text())['pr']
    assert world['flow']._ci(pr, {**world['target'], 'ci': ci}) is expected


def test_title_auto_close_and_missing_business_data_are_pending(world):
    data = snapshot(world, '交付')
    world['io'].save(data, world['target'], {'effects': {}, 'issue': 7, 'reviewed_head': world['sha']})
    state = json.loads(world['store'].read_text())
    state['pr']['title'] = 'Closes: #7'
    edit(world, pr=state['pr'])
    assert world['flow'].after(data, '交付', 'title', world['target']).pending
    edit(world, pr=None)
    assert world['flow'].after(data, '交付', 'missing', world['target']).pending
    assert world['flow'].after(data, '收尾', 'unaccepted', world['target']).pending
    assert not any(c[1:3] in (['pr', 'merge'], ['issue', 'close']) for c in calls(world))


@pytest.mark.parametrize("text", ["@Ash ws#7 不对，重做", "@Ash ws#7不对，重做"])
def test_feishu_group_name_and_dm_card_are_verified(world, text):
    flow, target = world['flow'], world['target']
    event = Event('cli:feishu', 'om_real', 'alpha', 'om_real', 'data', {'from_kun': True, 'text': 'ws#7 不对'})
    def lookup(external=None, *, object_key=None):
        return {'item': {'object_key': '7'}} if external == 'Q1' or object_key == '7' else None
    edit(world, message=message(text))
    assert flow.classify(event, None, target, lookup) == Classification('rework', '7')
    edit(world, message=message('ws#7 不对', sender='attacker'))
    assert flow.classify(event, None, target, lookup).discard
    edit(world, message=message('ws#999 不对'))
    assert flow.classify(event, None, target, lookup).discard
    Path(target['ask_kun_state']).write_text(json.dumps({'asks': {'Q1': {'id': 'Q1', 'message_id': 'om_card', 'chat_id': 'oc_dm'}}}))
    edit(world, message=message('不对', parent='om_card', chat='oc_dm'))
    assert flow.classify(event, None, target, lookup) == Classification('rework', '7')
    edit(world, message=message('不对', parent='om_card', chat='oc_wrong_dm'))
    assert flow.classify(event, None, target, lookup).discard
    Path(target['ask_kun_state']).write_text('{bad')
    edit(world, message=message('不对', parent='om_card', chat='oc_dm'))
    assert flow.classify(event, None, target, lookup).discard


def test_preparation_cwd_and_check_timeout_are_evidence(world):
    target = {**world['target'], 'prepare': [{'argv': ['/bin/sh', '-c', 'pwd; echo prepared > prepared'], 'cwd': 'scripts'}]}
    output = world['io'].verify(target, world['sha'], [['/bin/sh', '-c', 'cat scripts/prepared']])
    assert output[0]['kind'] == 'prepare' and output[0]['stdout'].strip().endswith('/repo/scripts')
    assert output[1]['stdout'] == 'prepared\n'
    target = {**world['target'], 'check_timeout': 0.02}
    output = world['io'].verify(target, world['sha'], [['/bin/sleep', '1']])
    assert output[0]['exit_code'] == 124 and output[0]['timed_out']


def test_review_cleanup_preserves_active_and_removes_finished(world):
    data = snapshot(world, '审查')
    path = Path(world['flow'].brief(data, '审查', world['target']).workdir)
    world['flow'].cleanup(world['target'], [str(path)])
    assert path.exists()
    world['flow'].cleanup(world['target'], [])
    assert not path.exists()


def test_bot_approve_uses_reviewed_sha_and_recovers_idempotently(world, monkeypatch):
    import io
    signer = world['commands'] / 'signer'
    signer.write_text('#!/bin/sh\nprintf "fixture-token\\n"\n')
    signer.chmod(0o755)
    target = {**world['target'], 'bot_review': {'login': 'lambdatheory-agent[bot]', 'signer': [str(signer)]}}
    requests = []
    def send(request, timeout):
        payload = json.loads(request.data)
        requests.append(payload)
        assert request.headers['Authorization'] == 'Bearer fixture-token'
        edit(world, reviews=[{'user': {'login': 'lambdatheory-agent[bot]'}, 'commit_id': payload['commit_id'], 'state': 'APPROVED'}])
        return io.BytesIO(b'{}')
    monkeypatch.setattr('urllib.request.urlopen', send)
    data = snapshot(world, '审查')
    world['flow'].after(data, '审查', 'review1', target)
    state = world['io'].read(data, target)
    state['effects'].clear()  # 模拟远端批准成功、本地落盘前崩溃。
    world['io'].save(data, target, state)
    world['flow'].after(data, '审查', 'review1', target)
    assert len(requests) == 1
    assert requests[0]['event'] == 'APPROVE' and requests[0]['commit_id'] == world['sha']


def test_delivery_ci_pending_does_not_isolate_flow_or_launch_executor(world):
    from datetime import datetime
    cfg = configuration()
    cfg.pop('taskhome')
    cfg['targets']['alpha'] = {**world['target'], 'plugin': 'issue_delivery', 'sources': []}
    engine = build(world['root'] / 'pending', cfg)
    engine.plugins.register('flow', 'issue_delivery', world['flow'])
    engine.ingest(Event('github', 'start', 'alpha', '7', 'test', {}))
    engine.tick()
    for stage in ['说清楚', '执行', '证据', '审查', '交付']:
        finish(engine, stage, key='7', evidence=[result(world, checks=[['scripts/check.sh']], must_fix=[], suggestions=[])])
    edit(world, checks=[])
    finish(engine, '验收', key='7', evidence=[result(world)])
    assert item(engine, '7')['state'] == '定时' and not engine.active()
    assert not engine.control('alpha').get('broken')
    edit(world, checks=[{'name': 'check', 'status': 'completed', 'conclusion': 'success'}])
    engine.clock.now = datetime.fromisoformat(item(engine, '7')['after_wait']['at'])
    engine.tick()
    assert current(engine, '7')['stage'] == '验收'
    engine.log.close()


def test_deployed_matches_production_and_contains_merged_commit(world):
    repo = world['target']['checkout']
    Path(repo, 'extra').write_text('another reviewed change')
    subprocess.run(['/usr/bin/git', '-C', repo, 'add', '.'], check=True)
    subprocess.run(['/usr/bin/git', '-C', repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost', 'commit', '-qm', 'next'], check=True)
    descendant = subprocess.check_output(['/usr/bin/git', '-C', repo, 'rev-parse', 'HEAD'], text=True).strip()
    app = {'status': {'sync': {'status': 'Synced', 'revision': 'e' * 40}, 'health': {'status': 'Healthy'}}}
    edit(world, app=app, deployed_sha=descendant)
    assert world['flow']._deployed(world['target'], world['sha'])
    call = next(c for c in calls(world) if c[0] == 'kubectl')
    assert call == ['kubectl', '--request-timeout=20s', '--context', 'readonly', '-n', 'argocd', 'get', 'application', 'test', '-o', 'json']
    edit(world, production_sha='f' * 40)
    assert not world['flow']._deployed(world['target'], world['sha'])


def test_release_submission_switches_to_no_executor_wait(world):
    target = {**world['target'], 'release': 'tag'}
    data = snapshot(world, '交付')
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'reviewed_head': world['sha']})
    assert world['flow'].after(data, '交付', 'first', target).rerun
    data['result']['evidence'] = [result(world, release_submitted=True)]
    pending = world['flow'].after(data, '交付', 'submitted', target)
    assert pending.pending and not pending.rerun
    assert world['flow'].after(data, '交付', 'submitted', target) == pending
    assert len([c for c in calls(world) if c[0] == 'tag-release.sh']) == 1


@pytest.mark.parametrize('failure', ['check', 'changed_head'])
def test_gate_feedback_sends_failed_work_back_to_execution_without_card(world, failure):
    cfg = configuration()
    cfg.pop('taskhome')
    cfg['targets']['alpha'] = {**world['target'], 'plugin': 'issue_delivery', 'sources': []}
    engine = build(world['root'] / ('gate-' + failure), cfg)
    engine.plugins.register('flow', 'issue_delivery', world['flow'])
    engine.ingest(Event('github', 'start', 'alpha', '7', 'test', {}))
    engine.tick()
    for stage in ['说清楚', '执行', '证据']:
        finish(engine, stage, key='7', evidence=[result(world, checks=[['scripts/check.sh']])])
    if failure == 'check':
        # 创建真实失败提交，流程必须实际运行到非零退出码。
        repo = world['target']['checkout']
        Path(repo, 'scripts/check.sh').write_text('#!/bin/sh\necho regression-failed >&2\nexit 2\n')
        subprocess.run(['/usr/bin/git', '-C', repo, 'add', '.'], check=True)
        subprocess.run(['/usr/bin/git', '-C', repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost', 'commit', '-qm', 'check fails'], check=True)
        sha = subprocess.check_output(['/usr/bin/git', '-C', repo, 'rev-parse', 'HEAD'], text=True).strip()
        pr = json.loads(world['store'].read_text())['pr']
        pr['head']['sha'] = sha
        edit(world, pr=pr)
        finish(engine, '审查', key='7', evidence=[result(world, head=sha)])
        assert current(engine, '7')['stage'] == '证据'
        feedback = current(engine, '7')['brief']['instructions']
        assert '检查失败' in feedback and 'regression-failed' in feedback
    else:
        finish(engine, '审查', key='7', evidence=[result(world)])
        finish(engine, '交付', key='7', evidence=[result(world, must_fix=[], suggestions=[])])
        pr = json.loads(world['store'].read_text())['pr']
        pr['head']['sha'] = 'f' * 40
        edit(world, pr=pr)
        # 回执给新 SHA，仍必须识别它不是审过的那一版。
        finish(engine, '验收', key='7', evidence=[result(world, head='f' * 40)])
        assert current(engine, '7')['stage'] == '交付'
        feedback = current(engine, '7')['brief']['instructions']
        assert '头提交变化' in feedback and 'f' * 40 in feedback
    assert 'advance="执行"' in feedback and '不问 Kun' in feedback
    # 假执行者按重派说明交退回回执，核心保留 A 的前进关口，不需新增业务逻辑。
    finish(engine, '执行', key='7', evidence=[result(world)])
    assert current(engine, '7')['stage'] == '执行'
    assert engine.log.all('cards') == []
    engine.log.close()


def test_release_uses_merged_script_in_owned_checkout_without_changing_main(world):
    repo = world['target']['checkout']
    subprocess.run(['/usr/bin/git', '-C', repo, 'checkout', '-qb', 'kuns-work'], check=True)
    Path(repo, 'scripts/tag-release.sh').write_text('#!/bin/sh\necho WRONG-SCRIPT >&2\nexit 99\n')
    subprocess.run(['/usr/bin/git', '-C', repo, 'add', '.'], check=True)
    subprocess.run(['/usr/bin/git', '-C', repo, '-c', 'user.name=Test', '-c', 'user.email=t@localhost', 'commit', '-qm', 'unrelated branch'], check=True)
    Path(repo, 'untracked').write_text('Kun is working')
    def main_state():
        return [subprocess.check_output(['/usr/bin/git', '-C', repo, *args], text=True) for args in
                [('status', '--porcelain'), ('symbolic-ref', 'HEAD'), ('show-ref',), ('reflog', '--all')]]
    before = main_state()
    target = {**world['target'], 'release': 'tag'}
    data = snapshot(world, '交付')
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'reviewed_head': world['sha']})
    pending = world['flow'].after(data, '交付', 'own-release', target)
    assert pending.pending
    checkout = next(p for p in (world['io'].root(target) / 'releases').iterdir() if p.is_dir())
    assert subprocess.check_output(['/usr/bin/git', '-C', str(checkout), 'rev-parse', 'HEAD'], text=True).strip() == world['sha']
    assert subprocess.run(['/usr/bin/git', '-C', str(checkout), 'symbolic-ref', '-q', 'HEAD']).returncode == 1
    # 真正运行的是合并提交里的 fake tag 脚本，其他分支那份会退出 99。
    assert len([c for c in calls(world) if c[0] == 'tag-release.sh']) == 1
    assert world['io'].read(data, target)['tag_output']['exit_code'] == 0
    app = {'status': {'sync': {'status': 'Synced', 'revision': 'e' * 40}, 'health': {'status': 'Healthy'}}}
    edit(world, app=app, deployed_sha=world['sha'])
    assert world['flow']._deployed(target, world['sha'])
    assert before == main_state()
    fetches = [c for c in calls(world) if c[0] == 'git' and 'fetch' in c]
    assert fetches and all(c[c.index('-C') + 1] == str(checkout) for c in fetches)
    assert len([c for c in calls(world) if c[0] == 'git' and 'clone' in c]) == 1


def test_delivery_instructions_wait_for_sync_and_do_not_advance_authorization(world):
    brief = world['flow'].brief(snapshot(world, '交付'), '交付', world['target'])
    assert '真正 sync 完并确认上线后' in brief
    assert '还在等授权卡时' in brief and '回执不带 advance' in brief
    assert '也不带 release_submitted' in brief


def test_editing_ordinary_comment_into_rework_is_only_update(world):
    responses = source_responses(world)
    endpoint = 'repos/kun-g/workshop/issues/7/comments?per_page=100'
    responses[endpoint] = [{'id': 9, 'body': '先补一句资料', 'user': {'login': 'kun-g'}}]
    edit(world, responses=responses)
    source = GitHubSource(str(world['commands'] / 'gh'))
    first = source.poll(world['target'], None)
    responses[endpoint][0]['body'] = '不对'
    edit(world, responses=responses)
    changed = source.poll(world['target'], first.cursor)
    assert len(changed.events) == 1 and changed.events[0].action == 'update'
    responses[endpoint].append({'id': 10, 'body': '不对', 'user': {'login': 'kun-g'}})
    edit(world, responses=responses)
    fresh = source.poll(world['target'], changed.cursor)
    assert len(fresh.events) == 1 and fresh.events[0].action == 'rework'


def test_waiting_for_release_authorization_does_not_advance_or_resubmit(world):
    cfg = configuration()
    cfg.pop('taskhome')
    cfg['targets']['alpha'] = {**world['target'], 'plugin': 'issue_delivery', 'sources': [], 'release': 'tag'}
    engine = build(world['root'] / 'release-auth', cfg)
    engine.plugins.register('flow', 'issue_delivery', world['flow'])
    engine.ingest(Event('github', 'start', 'alpha', '7', 'test', {}))
    engine.tick()
    for stage in ['说清楚', '执行', '证据', '审查', '交付']:
        finish(engine, stage, key='7', evidence=[result(world, checks=[['scripts/check.sh']], must_fix=[], suggestions=[])])
    finish(engine, '验收', key='7', evidence=[result(world)])
    assert current(engine, '7')['stage'] == '交付'
    prior_effects = dict(world['io'].read(item(engine, '7'), world['target'])['effects'])
    finish(engine, key='7', evidence=[result(world)], later=[{'key': 'authorization', 'at': '+5m', 'about': '等授权卡'}])
    assert item(engine, '7')['stage'] == '交付' and item(engine, '7')['state'] == '定时'
    state = world['io'].read(item(engine, '7'), world['target'])
    assert state['effects'] == prior_effects and not state.get('release_submitted')
    assert not engine.active()
    engine.log.close()


def test_acceptance_gets_readonly_merge_copy_and_flow_run_checks(world):
    # 只读验收者跑不了 uv/测试；流程在合并提交上跑一次登记检查交给它（ws#18 验收第一轮失败的原因）。
    data = snapshot(world, '验收')
    world['io'].save(data, world['target'], {'effects': {}, 'issue': 7, 'merge': world['sha']})
    brief = world['flow'].brief(data, '验收', world['target'])
    assert isinstance(brief, FlowBrief) and brief.readonly
    assert Path(brief.workdir).stat().st_mode & 0o222 == 0
    assert subprocess.check_output(['/usr/bin/git', '-C', brief.workdir, 'rev-parse', 'HEAD'], text=True).strip() == world['sha']
    assert f'验收提交 {world["sha"]}' in brief.instructions
    assert '$ scripts/check.sh（check）\nexit_code=0\n--- stdout ---\nraw check output\n' in brief.instructions
    clones = len([c for c in calls(world) if c[:1] == ['git'] and 'clone' in c])
    assert clones == 2  # 一次检查副本、一次只读副本
    again = world['flow'].brief(data, '验收', world['target'])
    assert again.instructions == brief.instructions and again.workdir == brief.workdir
    assert len([c for c in calls(world) if c[:1] == ['git'] and 'clone' in c]) == clones
    assert world['io'].read(data, world['target'])['acceptance_runs']['head'] == world['sha']
