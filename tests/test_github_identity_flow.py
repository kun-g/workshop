import json
import subprocess
from pathlib import Path

import pytest

from test_issue_delivery import world, snapshot, source_responses, edit, touch_source
from workshop.plugins.sources.github import GitHubSource
from workshop import github_identity as identity
from workshop.core.ports import TargetBlocked
from workshop.core.models import Receipt


def configure(world, monkeypatch, *, fail=False):
    target = world['target']
    target['github_identity'] = {'mode': 'app', 'gh': str(world['commands'] / 'gh'), 'signer': ['fake-signer']}
    original = subprocess.run
    observed = []
    def run(argv, **kwargs):
        if argv[0] == 'fake-signer':
            assert argv[1:] == ['--repo', target['repo']]
            observed.append(('sign', None))
            return subprocess.CompletedProcess(argv, 1 if fail else 0, 'app-token', 'secret-signing-error')
        if Path(argv[0]).name in ('gh', 'git'):
            observed.append((argv, kwargs.get('env')))
        return original(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    return observed


def test_flow_merge_and_issue_writes_use_app_environment(world, monkeypatch):
    observed = configure(world, monkeypatch)
    target, flow = world['target'], world['flow']
    data = snapshot(world, '交付')
    world['io'].save(data, target, {'effects': {}, 'issue': 7, 'reviewed_head': world['sha']})
    assert not flow.after(data, '交付', 'delivery', target).pending
    assert sum(argv == 'sign' for argv, _ in observed) == 1
    merged = [env for argv, env in observed if argv != 'sign' and argv[1:3] == ['pr', 'merge']]
    assert len(merged) == 1 and merged[0]['GH_TOKEN'] == 'app-token'
    flow.after(data, '验收', 'accept', target)
    flow.after(data, '收尾', 'close', target)
    closed = [env for argv, env in observed if argv != 'sign' and argv[1:3] == ['issue', 'close']]
    assert len(closed) == 1 and closed[0]['GH_TOKEN'] == 'app-token'
    # API writes used for registration/comments/labels use the same process boundary.
    @identity.scoped
    def writes(target):
        world['io'].api('repos/kun-g/workshop/issues/7/comments', data={'body': 'test'})
        world['io'].api('repos/kun-g/workshop/issues/7/labels', data={'labels': ['Ready for Agent']})
    writes(target)
    writes_seen = [env for argv, env in observed if argv != 'sign' and '--method' in argv]
    assert writes_seen and all(env['GH_TOKEN'] == 'app-token' for env in writes_seen)
    assert 'app-token' not in json.dumps(world['io'].read(data, target))


def test_flow_failed_signer_never_calls_github_and_does_not_swallow_failure(world, monkeypatch):
    observed = configure(world, monkeypatch, fail=True)
    data = snapshot(world, '交付')
    with pytest.raises(TargetBlocked) as failure:
        world['flow'].after(data, '交付', 'delivery', world['target'])
    assert observed == [('sign', None)]
    assert 'secret-signing-error' not in str(failure.value)
    # A failed runner can hand back a waiting receipt even while GitHub is unavailable.
    result = world['flow'].assess(data, Receipt('nothing', 'signing failed'), world['target'])
    assert not result.passed and observed == [('sign', None)]


def test_source_bot_cannot_release_or_rework_but_human_can(world, monkeypatch):
    observed = configure(world, monkeypatch)
    responses = source_responses(world)
    base = 'repos/kun-g/workshop/issues/7'
    responses[base + '/events?per_page=100'][0]['actor'] = {'login': 'lambdatheory-agent[bot]', 'type': 'Bot'}
    responses[base + '/comments?per_page=100'] = [{'id': 101, 'body': '不对', 'user': {'login': 'lambdatheory-agent[bot]', 'type': 'Bot'}}]
    edit(world, responses=responses)
    source = GitHubSource(str(world['commands'] / 'gh'))
    first = source.poll(world['target'], None)
    assert not any(e.action in ('ready', 'rework') for e in first.events)
    assert all(env['GH_TOKEN'] == 'app-token' for argv, env in observed if argv != 'sign')
    responses[base + '/events?per_page=100'].append({'id': 81, 'event': 'labeled', 'label': {'name': 'Ready for Agent'}, 'actor': {'login': 'kun-g', 'type': 'User'}})
    responses[base + '/comments?per_page=100'].append({'id': 102, 'body': '不对', 'user': {'login': 'kun-g', 'type': 'User'}})
    touch_source(world, responses)
    second = source.poll(world['target'], first.cursor)
    assert {'ready', 'rework'} <= {e.action for e in second.events}
    assert source.poll(world['target'], second.cursor).events == []
