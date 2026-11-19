import base64
from datetime import datetime, timedelta
from dataclasses import asdict
import json
import os
import sys
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from workshop.core.models import After, Classification, Event, FlowBrief, Receipt
from workshop.plugins.issue_delivery import IssueDelivery, action_for
from workshop.plugins.issue_delivery.io import IO, clean_env
from workshop.core.ports import Retryable
from source_routing import RoutedGitHubSource as GitHubSource
from conftest import build, configuration, current, finish, in_process_commands, item


BODY = '''## Kun 的原话
检查失败时让错误可见。
## 依据
核心 R14
## 合并前 DoD
- 错误输出和非零退出码保留：`scripts/check.sh` 通过。
## 能跑的检查
```workshop-checks
[["scripts/check.sh"]]
```
## 不可逆或对外的动作
无
## 要不要亲眼看
否
'''

# 执行者交的 PR 正文：活标记，加逐项照抄合并前 DoD、附检查名的一节。
PR_BODY = '''<!-- workshop-item: alpha:7 -->
Related to #7
## 合并前 DoD
- 错误输出和非零退出码保留：`scripts/check.sh` 通过。
  证据：`scripts/check.sh`
'''


@pytest.fixture
def world(tmp_path, monkeypatch, git_template):
    commands = tmp_path / 'bin'
    commands.mkdir()
    script = Path(__file__).parent / 'fixtures/issue_delivery/fake_command.py'
    for name in ('gh', 'git', 'lark-cli', 'kubectl'):
        file = commands / name
        file.write_text(script.read_text())
        file.chmod(0o755)
    repo = tmp_path / 'checkout'
    template, sha = git_template({'scripts/check.sh': '#!/bin/sh\nprintf "raw check output\\n"\n',
                                  'scripts/tag-release.sh': script.read_text()})
    shutil.copytree(template, repo)
    store = tmp_path / 'remote.json'
    state = {'sha': sha, 'checkout': str(repo), 'requirement_contents': {'docs/requirements/核心.md': '**R14 失败不反复通知。**\n'},
             'issue': {'number': 7, 'body': BODY, 'state': 'open'},
             'pr': {'number': 11, 'body': PR_BODY, 'head': {'sha': sha}, 'state': 'open', 'merged': False}}
    store.write_text(json.dumps(state))
    monkeypatch.setenv('WORKSHOP_FAKE_STATE', str(store))
    monkeypatch.setenv('PATH', str(commands) + os.pathsep + os.environ['PATH'])
    in_process_commands(monkeypatch, commands)
    target = {'name': 'alpha', 'repo': 'kun-g/workshop', 'checkout': str(repo), 'release': 'none',
              'requirement_docs': ['docs/requirements/核心.md'],
              'ci': ['check'], 'short_name': 'ws', 'github_cache_dir': str(tmp_path / 'cache'),
              'tag_command': ['scripts/tag-release.sh'], 'delivery_action': '自己的交付',
              'deployment': {'app': 'test', 'context': 'readonly', 'namespace': 'argocd', 'revision_field': 'status.sync.revision', 'branch': 'production', 'release_file': 'release.json', 'source_field': 'source_commit',
              'rollout': {'namespace': 'apps', 'name': 'web'}}}
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
        base + '/issues?state=all&sort=updated&per_page=100': [{'number': 7, 'state': 'open', 'labels': [{'name': 'Ready for Agent'}], 'updated_at': '2026-11-13T00:01:00Z'}],
        base + '/pulls/comments?sort=updated&direction=asc&per_page=100': [],
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


def touch_source(world, responses):
    row = responses['repos/kun-g/workshop/issues?state=all&sort=updated&per_page=100'][0]
    row['updated_at'] = (datetime.fromisoformat(row['updated_at'].replace('Z', '+00:00'))
                         + timedelta(seconds=1)).isoformat().replace('+00:00', 'Z')
    edit(world, responses=responses)


def test_source_labels_comments_etag_and_replay(world):
    responses = source_responses(world)
    edit(world, responses=responses)
    source = GitHubSource(str(world['commands'] / 'gh'))
    batch = source.poll(world['target'], None)
    assert any(e.action == 'ready' for e in batch.events)
    assert {e.object_key for e in batch.events} == {'7'}
    quiet = source.poll(world['target'], batch.cursor)
    assert quiet.events == []
    assert source.poll(world['target'], quiet.cursor).events == []
    assert source.poll(world['target'], None).events == batch.events
    assert any('If-None-Match: "AbC' in a for call in calls(world) for a in call)
    comments = [{'id': n, 'body': '不对' if n < 3 else '普通意见', 'user': {'login': 'kun-g' if n == 1 else 'other'}} for n in (1, 2, 3)]
    comments += [{'id': 4, 'body': 'done', 'user': {'login': 'bot', 'type': 'Bot'}},
                 {'id': 5, 'body': '<!-- workshop-comment: alpha:7 --> done', 'user': {'login': 'kun-g'}}]
    responses['repos/kun-g/workshop/issues/7/comments?per_page=100'] = comments
    edit(world, responses=responses)
    touch_source(world, responses)
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
    responses['repos/kun-g/workshop/issues?state=all&sort=updated&per_page=100'].append({**pr, 'pull_request': {}, 'updated_at': '2026-11-13T00:01:00Z'})
    responses['repos/kun-g/workshop/pulls/11'] = pr
    endpoint = 'repos/kun-g/workshop/issues/11/comments?per_page=100'
    responses[endpoint] = {'_response': True, 'data': [], 'next': endpoint + '&page=2'}
    responses[endpoint + '&page=2'] = [{'id': 99, 'body': '不对，重做', 'user': {'login': 'kun-g'}}]
    edit(world, responses=responses)
    events = GitHubSource(str(world['commands'] / 'gh')).poll(world['target'], None).events
    assert all(e.object_key == '7' for e in events)
    assert any(e.body['kind'] == 'pr-comment' and e.action == 'rework' for e in events)
    assert any(e.body['kind'] == 'pr-ci' for e in events)


def test_after_merge_close_tag_are_idempotent_and_merge_keeps_issue(world):
    target = {**world['target'], 'release': 'tag'}
    data = snapshot(world, '交付')
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'dod_head': world['sha']})
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
    world['flow'].after(data, '收尾', 'close', target)
    world['flow'].after(data, '收尾', 'close', target)
    assert len([c for c in calls(world) if c[1:3] == ['issue', 'close']]) == 1
    merge = next(c for c in calls(world) if c[1:3] == ['pr', 'merge'])
    assert merge[merge.index('--body') + 1] == 'Related to #7'
    assert merge[merge.index('--match-head-commit') + 1] == world['sha']


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
    engine.ingest(Event('github', 'start', 'alpha', '7', 'test', {}, depth=2, action='ready'))
    engine.tick()
    assert item(engine, '7')['stage'] == '执行'
    assert not engine.log.all('rounds')
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
    assert recorded['stage'] == '执行' and current(engine, '7')['token'] == token
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
        touch_source(world, responses)
        batch = source.poll(world['target'], first.cursor)
        assert batch.events and all(e.action == 'update' for e in batch.events)
        # 丢了本地游标重放，控制事件仍与第一次相同，核心可以去重。
        replay = source.poll(world['target'], None)
        assert next(e.ext_id for e in replay.events if e.action == 'rework') == original.ext_id
    cursor = json.loads(batch.cursor)
    assert all(set(v) == {'etag', 'fingerprint', 'next'} for v in cursor['cache'].values())
    assert BODY not in batch.cursor
    assert list(source.cache_root.glob('*.json'))


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
    touch_source(world, responses)
    second = source.poll(world['target'], first.cursor)
    assert [e.ext_id for e in second.events if e.action == 'ready'] == ['kun-g/workshop:label:7:82']
    responses['repos/kun-g/workshop/issues?state=all&sort=updated&per_page=100'][0]['state'] = 'closed'
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
    world['io'].save(data, world['target'], {'effects': {}, 'issue': 7, 'dod_head': world['sha']})
    state = json.loads(world['store'].read_text())
    state['pr']['title'] = 'Closes: #7'
    edit(world, pr=state['pr'])
    assert world['flow'].after(data, '交付', 'title', world['target']).pending
    edit(world, pr=None)
    assert world['flow'].after(data, '交付', 'missing', world['target']).pending
    assert world['flow'].after(data, '收尾', 'unaccepted', world['target']).pending
    assert not any(c[1:3] in (['pr', 'merge'], ['issue', 'close']) for c in calls(world))


def test_preparation_cwd_and_check_timeout_are_evidence(world):
    target = {**world['target'], 'prepare': [{'argv': ['/bin/sh', '-c', 'pwd; echo prepared > prepared'], 'cwd': 'scripts'}]}
    output = world['io'].verify(target, world['sha'], [['/bin/sh', '-c', 'cat scripts/prepared']])
    assert output[0]['kind'] == 'prepare' and output[0]['stdout'].strip().endswith('/repo/scripts')
    assert output[1]['stdout'] == 'prepared\n'
    target = {**world['target'], 'check_timeout': 0.02}
    output = world['io'].verify(target, world['sha'], [[sys.executable, '-c', 'import signal; signal.pause()']])
    assert output[0]['exit_code'] == 124 and output[0]['timed_out']


def test_missing_check_tool_is_a_failed_check_not_a_plugin_failure(world):
    output = world['io'].verify(world['target'], world['sha'],
                                [['/bin/sh', '-c', 'true'], ['no-such-tool-for-workshop-test', 'check']])
    assert output[0]['exit_code'] == 0
    assert output[1]['exit_code'] == 127 and output[1]['missing_tool'] == 'no-such-tool-for-workshop-test'
    assert '找不到 no-such-tool-for-workshop-test' in output[1]['stderr']


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
    rollout = next(c for c in calls(world) if c[0] == 'kubectl' and 'deployment' in c)
    assert rollout == ['kubectl', '--request-timeout=20s', '--context', 'readonly', '-n', 'apps', 'get', 'deployment', 'web', '-o', 'json']
    edit(world, production_sha='f' * 40)
    assert not world['flow']._deployed(world['target'], world['sha'])


ROLLED_OUT = {'metadata': {'generation': 5}, 'spec': {'replicas': 2},
              'status': {'observedGeneration': 5, 'replicas': 2, 'updatedReplicas': 2, 'availableReplicas': 2}}


@pytest.mark.parametrize('change', [
    {'metadata': {'generation': 6}},                                       # 控制器还没看到新一代
    {'status': {**ROLLED_OUT['status'], 'updatedReplicas': 1}},            # 新副本数不够
    {'status': {**ROLLED_OUT['status'], 'replicas': 3}},                   # 旧副本还没走完
    {'status': {**ROLLED_OUT['status'], 'availableReplicas': 1}},          # 新副本没全部可用
    {'status': {**ROLLED_OUT['status'], 'conditions': [{'type': 'Progressing', 'reason': 'ProgressDeadlineExceeded'}]}},
])
def test_deployed_requires_rollout_complete(world, change):
    app = {'status': {'sync': {'status': 'Synced', 'revision': 'e' * 40}, 'health': {'status': 'Healthy'}}}
    edit(world, app=app, deployed_sha=world['sha'], rollout=ROLLED_OUT)
    assert world['flow']._deployed(world['target'], world['sha'])
    edit(world, rollout={**ROLLED_OUT, **change})
    assert not world['flow']._deployed(world['target'], world['sha'])


def test_deployed_fails_without_rollout_rule(world):
    app = {'status': {'sync': {'status': 'Synced', 'revision': 'e' * 40}, 'health': {'status': 'Healthy'}}}
    edit(world, app=app, deployed_sha=world['sha'])
    deployment = {k: v for k, v in world['target']['deployment'].items() if k != 'rollout'}
    assert not world['flow']._deployed({**world['target'], 'deployment': deployment}, world['sha'])


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
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'dod_head': world['sha']})
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


def test_editing_ordinary_comment_into_rework_is_only_update(world):
    responses = source_responses(world)
    endpoint = 'repos/kun-g/workshop/issues/7/comments?per_page=100'
    responses[endpoint] = [{'id': 9, 'body': '先补一句资料', 'user': {'login': 'kun-g'}}]
    edit(world, responses=responses)
    source = GitHubSource(str(world['commands'] / 'gh'))
    first = source.poll(world['target'], None)
    responses[endpoint][0]['body'] = '不对'
    edit(world, responses=responses)
    touch_source(world, responses)
    changed = source.poll(world['target'], first.cursor)
    assert len(changed.events) == 1 and changed.events[0].action == 'update'
    responses[endpoint].append({'id': 10, 'body': '不对', 'user': {'login': 'kun-g'}})
    edit(world, responses=responses)
    touch_source(world, responses)
    fresh = source.poll(world['target'], changed.cursor)
    assert len(fresh.events) == 1 and fresh.events[0].action == 'rework'


def test_renamed_files_count_old_and_new_paths(world):
    edit(world, files=[{'filename': 'src/moved.yaml', 'previous_filename': 'deploy/a.yaml', 'status': 'renamed'},
                       {'filename': 'src/b.py'}])
    data = snapshot(world, '执行')
    receipt = Receipt('done', '', evidence=[result(world)])
    assert world['flow'].assess(data, receipt, world['target']).paths == ['deploy/a.yaml', 'src/b.py', 'src/moved.yaml']


# #14 的离线回放：关掉的对象仍可返工，但不常驻扫描。
SOURCE_BASE = 'repos/kun-g/workshop'
SOURCE_ISSUES = SOURCE_BASE + '/issues?state=all&sort=updated&per_page=100'
SOURCE_COMMENTS = SOURCE_BASE + '/pulls/comments?sort=updated&direction=asc&per_page=100'


def incremental_fixture(world):
    responses = source_responses(world)
    rows = responses[SOURCE_ISSUES]
    for n in (1, 2, 3):
        row = {'number': n, 'state': 'closed', 'body': f'完成 {n}', 'labels': [],
               'updated_at': '2026-11-13T00:00:00Z'}
        rows.append(row)
        responses[f'{SOURCE_BASE}/issues/{n}'] = row
        responses[f'{SOURCE_BASE}/issues/{n}/events?per_page=100'] = []
        responses[f'{SOURCE_BASE}/issues/{n}/comments?per_page=100'] = []
    pr = {**json.loads(world['store'].read_text())['pr'], 'updated_at': '2026-11-13T00:01:00Z'}
    rows.append({**pr, 'pull_request': {}})
    responses[f'{SOURCE_BASE}/pulls/11'] = pr
    edit(world, responses=responses)
    target = {**world['target'], 'github_requests_per_tick': 100}
    source = GitHubSource(str(world['commands'] / 'gh'))
    old = json.dumps({'known': {str(n): True for n in (1, 2, 3, 7)}, 'seen': {}, 'cache': {}})
    return responses, target, source, old


def source_urls(world, start=0):
    return [next(a for a in c if a.startswith('repos/')) for c in calls(world)[start:]]


def change_row(responses, number, **fields):
    next(r for r in responses[SOURCE_ISSUES] if r['number'] == number).update(fields)


def test_incremental_quiet_only_lists_and_open_head_ci(world):
    responses, target, source, old = incremental_fixture(world)
    first = source.poll(target, old)
    before = len(calls(world))
    quiet = source.poll(target, first.cursor)
    urls = source_urls(world, before)
    assert quiet.events == []
    assert len(urls) == 4
    assert urls[0].startswith(SOURCE_BASE + '/issues?state=all&sort=updated&since=')
    assert urls[1].startswith(SOURCE_BASE + '/pulls/comments?sort=updated&direction=asc&since=')
    assert urls[2:] == [f'{SOURCE_BASE}/commits/{world["sha"]}/check-runs?per_page=100',
                       f'{SOURCE_BASE}/commits/{world["sha"]}/status']
    assert json.loads(quiet.cursor)['watermark'] == '2026-11-13T00:01:00Z'
    assert '/pulls?state=all' not in '\n'.join(source_urls(world))


def test_incremental_closed_issue_rework_new_ready_and_overlap(world):
    responses, target, source, old = incremental_fixture(world)
    first = source.poll(target, old)
    change_row(responses, 2, updated_at='2026-11-13T00:02:00Z')
    responses[f'{SOURCE_BASE}/issues/2/comments?per_page=100'] = [
        {'id': 100, 'body': '不对，补上关闭后返工', 'user': {'login': 'kun-g'}}]
    row = {'number': 8, 'state': 'open', 'labels': [{'name': 'Ready for Agent'}],
           'updated_at': '2026-11-13T00:02:00Z'}
    responses[SOURCE_ISSUES].append(row)
    responses[f'{SOURCE_BASE}/issues/8'] = row
    responses[f'{SOURCE_BASE}/issues/8/events?per_page=100'] = [
        {'id': 88, 'event': 'labeled', 'label': {'name': 'Ready for Agent'}, 'actor': {'login': 'kun-g'}}]
    responses[f'{SOURCE_BASE}/issues/8/comments?per_page=100'] = []
    edit(world, responses=responses)
    before = len(calls(world))
    batch = source.poll(target, first.cursor)
    assert [(e.object_key, e.action) for e in batch.events if e.action != 'update'] == [('2', 'rework'), ('8', 'ready')]
    urls = source_urls(world, before)
    assert not any(f'/issues/{n}/' in u or u.endswith(f'/issues/{n}') for n in (1, 3, 7) for u in urls)
    restarted = GitHubSource(str(world['commands'] / 'gh'))
    assert restarted.poll(target, batch.cursor).events == []
    engine = build(world['root'] / 'incremental-core')
    for e in [*batch.events, *batch.events]:
        engine.ingest(e)
    assert any(i['object_key'] == '8' for i in engine.log.all('items'))
    engine.log.close()


def test_incremental_inline_edit_without_parent_update_and_closed_pr(world):
    responses, target, source, old = incremental_fixture(world)
    change_row(responses, 11, state='closed')
    responses[f'{SOURCE_BASE}/pulls/11']['state'] = 'closed'
    comment = {'id': 200, 'body': '普通意见', 'user': {'login': 'kun-g'},
               'updated_at': '2026-11-13T00:01:00Z', 'pull_request_url': 'https://api.github.com/repos/kun-g/workshop/pulls/11'}
    responses[SOURCE_COMMENTS] = [comment]
    responses[f'{SOURCE_BASE}/pulls/11/comments?per_page=100'] = [comment]
    edit(world, responses=responses)
    first = source.poll(target, old)
    assert all(e.body['kind'] != 'pr-ci' for e in first.events)
    comment.update(body='不对', updated_at='2026-11-13T00:03:00Z')
    edit(world, responses=responses)
    before = len(calls(world))
    batch = source.poll(target, first.cursor)
    assert len(batch.events) == 1
    event = batch.events[0]
    assert event.action == 'update' and event.body['kind'] == 'pr-review-comment' and event.object_key == '7'
    assert len(source_urls(world, before)) == 2
    assert json.loads(batch.cursor)['watermark'] == '2026-11-13T00:01:00Z'
    assert json.loads(batch.cursor)['review_watermark'] == '2026-11-13T00:03:00Z'
    assert source.poll(target, batch.cursor).events == []


def test_incremental_ci_changes_push_review_and_close(world):
    responses, target, source, old = incremental_fixture(world)
    first = source.poll(target, old)
    responses[f'{SOURCE_BASE}/commits/{world["sha"]}/check-runs?per_page=100'] = {
        'check_runs': [{'id': 50, 'status': 'completed', 'conclusion': 'success'}]}
    edit(world, responses=responses)
    batch = source.poll(target, first.cursor)
    assert [e.body['kind'] for e in batch.events] == ['pr-ci']
    sha = 'b' * 40
    change_row(responses, 11, updated_at='2026-11-13T00:04:00Z')
    responses[f'{SOURCE_BASE}/pulls/11']['head'] = {'sha': sha}
    responses[f'{SOURCE_BASE}/pulls/11/reviews?per_page=100'] = [
        {'id': 201, 'body': '不对，补测试', 'user': {'login': 'kun-g'}}]
    responses[f'{SOURCE_BASE}/commits/{sha}/check-runs?per_page=100'] = {'check_runs': []}
    responses[f'{SOURCE_BASE}/commits/{sha}/status'] = {'statuses': []}
    edit(world, responses=responses)
    before = len(calls(world))
    pushed = source.poll(target, batch.cursor)
    assert {e.body['kind'] for e in pushed.events} == {'pr-head', 'pr-review', 'pr-ci'}
    assert any(e.action == 'rework' for e in pushed.events)
    assert all(world['sha'] not in u for u in source_urls(world, before))
    change_row(responses, 11, state='closed', updated_at='2026-11-13T00:05:00Z')
    responses[f'{SOURCE_BASE}/pulls/11']['state'] = 'closed'
    edit(world, responses=responses)
    closed = source.poll(target, pushed.cursor)
    assert json.loads(closed.cursor)['open_prs'] == {}
    before = len(calls(world))
    source.poll(target, closed.cursor)
    assert len(source_urls(world, before)) == 2


def test_incremental_retryable_keeps_watermarks_and_replays(world, monkeypatch):
    responses, target, source, old = incremental_fixture(world)
    first = source.poll(target, old)
    original = json.loads(first.cursor)
    change_row(responses, 2, updated_at='2026-11-13T00:03:00Z')
    responses[f'{SOURCE_BASE}/issues/2/comments?per_page=100'] = [
        {'id': 101, 'body': '不对', 'user': {'login': 'kun-g'}}]
    edit(world, responses=responses)
    real = source._get
    def fail(endpoint, cache):
        if '/pulls/comments?' in endpoint:
            raise Retryable('模拟中途网络故障')
        return real(endpoint, cache)
    monkeypatch.setattr(source, '_get', fail)
    with pytest.raises(Retryable):
        source.poll(target, first.cursor)
    assert json.loads(first.cursor) == original
    monkeypatch.setattr(source, '_get', real)
    resumed = source.poll(target, first.cursor)
    assert any(e.action == 'rework' for e in resumed.events)
    assert json.loads(resumed.cursor)['watermark'] == '2026-11-13T00:03:00Z'


def test_incremental_budget_restart_pagination_and_atomic_watermarks(world):
    responses, target, source, old = incremental_fixture(world)
    rows = responses[SOURCE_ISSUES]
    page2 = SOURCE_ISSUES + '&page=2'
    responses[SOURCE_ISSUES] = {'_response': True, 'data': rows[:2], 'next': page2}
    responses[page2] = rows[2:]
    edit(world, responses=responses)
    cursor, events = old, []
    target['github_requests_per_tick'] = 1
    for _ in range(40):
        before = len(calls(world))
        # 每一拍新进程、新实例，只允许游标携带进度。
        source = GitHubSource(str(world['commands'] / 'gh'))
        batch = source.poll(target, cursor)
        assert len(calls(world)) - before <= 1
        cursor = batch.cursor
        events.extend(batch.events)
        state = json.loads(cursor)
        if not state['refreshed']:
            break
        assert 'watermark' not in state
    else:
        pytest.fail('small budget starves the scan')
    assert state['watermark'] == '2026-11-13T00:01:00Z'
    assert any(e.action == 'ready' for e in events)
    assert len(events) == len({e.ext_id for e in events})
    assert page2 in source_urls(world)


def test_incremental_upgrade_legacy_seen_and_shared_cache(world):
    import hashlib
    responses = source_responses(world)
    detail = responses[f'{SOURCE_BASE}/issues/7']
    label = responses[f'{SOURCE_BASE}/issues/7/events?per_page=100'][0]
    signature = lambda v: hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()
    root = Path(world['target']['github_cache_dir'])
    root.mkdir()
    payload = json.dumps(detail, sort_keys=True, ensure_ascii=False)
    fingerprint = hashlib.sha256(payload.encode()).hexdigest()
    legacy = root / (fingerprint + '.json')
    legacy.write_text(payload)
    old = json.dumps({'known': {'7': True}, 'seen': {'issue:7:body': signature(detail), 'label:7:80': signature(label)},
                      'cache': {f'{SOURCE_BASE}/issues/7': {'fingerprint': fingerprint, 'etag': None, 'next': None}},
                      'refreshed': [f'{SOURCE_BASE}/issues/7']})
    edit(world, responses=responses)
    source = GitHubSource(str(world['commands'] / 'gh'))
    first = source.poll(world['target'], old)
    assert first.events == []
    assert json.loads(first.cursor)['watermark'] == '2026-11-13T00:01:00Z'
    assert source.cache_root != root and legacy.exists()
    assert source.poll(world['target'], first.cursor).events == []
    assert not legacy.exists()
    # 另一条尚未升级的旧游标失去共享缓存，仍可通过 GET 完成迁移且不重复控制事件。
    assert GitHubSource(str(world['commands'] / 'gh')).poll({**world['target'], 'name': 'beta'}, old).events == []


def test_incremental_cache_gc_after_success_preserves_committed_and_other_target(world):
    responses, target, source, old = incremental_fixture(world)
    first = source.poll(target, old)
    directory = source.cache_root
    pinned = {r['fingerprint'] for r in json.loads(first.cursor)['cache'].values()}
    other = GitHubSource(str(world['commands'] / 'gh'))
    other.poll({**target, 'name': 'beta'}, None)
    other_files = {f: f.read_bytes() for f in other.cache_root.glob('*.json')}
    orphan = directory / ('f' * 64 + '.json')
    orphan.write_text('{}')
    second = source.poll(target, first.cursor)
    assert not orphan.exists()
    assert all((directory / (name + '.json')).exists() for name in pinned)
    third = source.poll(target, second.cursor)
    fourth = source.poll(target, third.cursor)
    current = {r['fingerprint'] for r in json.loads(fourth.cursor)['cache'].values()}
    assert {f.stem for f in directory.glob('*.json')} == current
    assert all(f.read_bytes() == body for f, body in other_files.items())


@pytest.mark.parametrize('overlap', [-1, True, '60'])
def test_incremental_overlap_validation(world, overlap):
    source = GitHubSource(str(world['commands'] / 'gh'))
    with pytest.raises(ValueError, match='github_overlap_seconds'):
        source.poll({**world['target'], 'github_overlap_seconds': overlap}, None)


def test_incremental_removed_label_keeps_intake_and_removed_marker_stops_ci(world):
    responses, target, source, old = incremental_fixture(world)
    first = source.poll(target, old)
    change_row(responses, 7, labels=[], updated_at='2026-11-13T00:02:00Z')
    responses[f'{SOURCE_BASE}/issues/7/comments?per_page=100'] = [
        {'id': 303, 'body': '不对', 'user': {'login': 'kun-g'}}]
    change_row(responses, 11, body='撤掉关联', updated_at='2026-11-13T00:02:00Z')
    # 同仓库其他交付对象的 PR 不应读取详情或 CI。
    responses[SOURCE_ISSUES].append({'number': 12, 'pull_request': {}, 'body': '<!-- workshop-item: beta:7 -->',
                                     'updated_at': '2026-11-13T00:02:00Z'})
    edit(world, responses=responses)
    before = len(calls(world))
    batch = source.poll(target, first.cursor)
    assert any(e.action == 'rework' and e.object_key == '7' for e in batch.events)
    assert json.loads(batch.cursor)['open_prs'] == {}
    assert not any('/pulls/12' in u or '/commits/' in u for u in source_urls(world, before))


def test_incremental_feed_pagination_and_custom_overlap(world):
    responses, target, source, old = incremental_fixture(world)
    first = source.poll(target, old)
    # 只有补查的第二页含变化；issue 列表完全未变。
    comment = {'id': 404, 'body': '行内意见', 'user': {'login': 'other'},
               'updated_at': '2026-11-13T00:03:00Z', 'pull_request_url': 'https://api.github.com/repos/kun-g/workshop/pulls/11'}
    feed = SOURCE_BASE + '/pulls/comments?sort=updated&direction=asc&since=1969-12-31T23%3A58%3A00Z&per_page=100'
    responses[feed] = {'_response': True, 'data': [], 'next': feed + '&page=2'}
    responses[feed + '&page=2'] = [comment]
    edit(world, responses=responses)
    before = len(calls(world))
    batch = source.poll({**target, 'github_overlap_seconds': 120}, first.cursor)
    assert [e.body['kind'] for e in batch.events] == ['pr-review-comment']
    assert json.loads(batch.cursor)['review_watermark'] == '2026-11-13T00:03:00Z'
    urls = source_urls(world, before)
    assert 'since=2026-11-12T23%3A59%3A00Z' in urls[0]
    assert feed + '&page=2' in urls


def test_scheduled_delivery_merges_once_and_waits_for_deployment_without_tag(world):
    target = {**world['target'], 'release': 'scheduled'}
    data = snapshot(world, '交付')
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'dod_head': world['sha']})
    first = world['flow'].after(data, '交付', 'scheduled', target)
    assert first.pending and not first.rerun
    assert not any(c[0] == 'tag-release.sh' for c in calls(world))
    assert not world['flow']._delivery_done(target, world['io'].read(data, target), world['sha'])
    app = {'status': {'sync': {'status': 'OutOfSync', 'revision': 'e' * 40}, 'health': {'status': 'Healthy'}}}
    edit(world, app=app, deployed_sha=world['sha'])
    assert world['flow'].after(data, '交付', 'scheduled', target).pending
    app['status']['sync']['status'] = 'Synced'
    edit(world, app=app)
    assert not world['flow'].after(data, '交付', 'scheduled', target).pending
    assert len([c for c in calls(world) if c[1:3] == ['pr', 'merge']]) == 1
    assert not any(c[0] == 'tag-release.sh' for c in calls(world))


@pytest.mark.parametrize('files,changed,wait', [
    ([{'filename': 'docs/guide.txt'}, {'filename': 'README.md'}, {'filename': 'services/api/tests/test_api.py'}], 3, False),
    ([{'filename': 'src/api.py'}, {'filename': 'tests/test_api.py'}], 2, True),
    ([{'filename': 'docs/guide.txt'}], 2, True),
    ([{'filename': 'docs/guide.txt'}], None, True),
    ([{'filename': 'docs/old.py', 'status': 'renamed', 'previous_filename': 'src/old.py'}], 1, True),
])
def test_non_runtime_merge_delivers_without_waiting_for_release(world, monkeypatch, files, changed, wait):
    target = {**world['target'], 'release': 'scheduled', 'release_ignored_paths': ['docs/', '**/tests/', '*.md']}
    data = snapshot(world, '交付')
    remote = edit(world)
    remote['pr']['changed_files'] = changed
    edit(world, pr=remote['pr'])
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'dod_head': world['sha']})
    pages = world['io'].pages
    monkeypatch.setattr(world['io'], 'pages', lambda endpoint: files if '/files?' in endpoint else pages(endpoint))
    result = world['flow'].after(data, '交付', 'docs-only', target)
    assert result.pending is wait
    assert not any(c[0] == 'tag-release.sh' for c in calls(world))
    state = world['io'].read(data, target)
    assert (world['sha'] in state.get('delivered', [])) is (not wait)
    if not wait:
        assert not any(c[0] == 'kubectl' for c in calls(world))


@pytest.mark.parametrize('release', ['scheduled', 'tag'])
def test_unreadable_pr_files_count_as_release_then_recheck_decides(world, monkeypatch, release):
    """取不到 PR 文件列表按要上线（issue交付 R4）；下一次回查取到且全在忽略路径时改判免上线并收尾。"""
    target = {**world['target'], 'release': release, 'release_ignored_paths': ['docs/', '**/tests/', '*.md']}
    data = snapshot(world, '交付')
    remote = edit(world)
    remote['pr']['changed_files'] = 1
    edit(world, pr=remote['pr'])
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'dod_head': world['sha']})
    pages = world['io'].pages
    # 合并确认时记需求文档那次读取成功；之后免上线判断那次失败，再下一次回查又取到。
    readable = iter([True, False, True])

    def files(endpoint):
        if '/files?' not in endpoint:
            return pages(endpoint)
        if not next(readable):
            raise Retryable('gh api 失败：HTTP 502')
        return [{'filename': 'docs/guide.txt'}]
    monkeypatch.setattr(world['io'], 'pages', files)
    first = world['flow'].after(data, '交付', 'unreadable', target)
    assert first.pending
    state = world['io'].read(data, target)
    assert 'non_release_merge' not in state and world['sha'] not in state.get('delivered', [])
    assert len([c for c in calls(world) if c[0] == 'tag-release.sh']) == (release == 'tag')
    second = world['flow'].after(data, '交付', 'readable', target)
    assert not second.pending
    state = world['io'].read(data, target)
    assert state['non_release_merge'] == world['sha'] and world['sha'] in state['delivered']
    assert world['flow']._delivery_done(target, state, world['sha'])
    assert len([c for c in calls(world) if c[1:3] == ['pr', 'merge']]) == 1
