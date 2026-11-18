from dataclasses import replace
import json
from pathlib import Path

from conftest import build, configuration
from test_issue_delivery import world, source_responses, edit, touch_source
from workshop.plugins.sources.github import GitHubSource
from workshop.plugins.issue_delivery import IssueDelivery
from workshop.core.rules import Rules
from workshop.core.engine import Engine


def configured(world, *, legacy=False):
    data = configuration()
    triggers = Rules.load(Path('rules/workshop.yaml')).target('workshop')['triggers']
    target = {**world['target'], 'sources': []}
    if legacy:
        target['plugin'] = 'issue_delivery'
        target.pop('flows', None)
    else:
        target.pop('plugin', None)
        target['flows'] = {'flow': {'triggers': triggers}}
    data['targets']['alpha'] = target
    engine = build(world['root'] / 'core', data)
    return engine


def test_raw_source_has_no_business_actions_and_rules_keep_rework_boundaries(world):
    responses = source_responses(world)
    endpoint = 'repos/kun-g/workshop/issues/7/comments?per_page=100'
    bodies = ['不对', '不对，原因', '不对劲', '引用「不对」', '> 不对', '不对\n原因']
    responses[endpoint] = [{'id': n + 1, 'body': body, 'user': {'login': 'kun-g'}} for n, body in enumerate(bodies)]
    responses[endpoint] += [{'id': 20, 'body': '不对', 'user': {'login': 'other'}},
                            {'id': 21, 'body': '不对', 'user': {'login': 'robot', 'type': 'Bot'}},
                            {'id': 22, 'body': '不对\n<!-- workshop-comment: alpha:7 -->', 'user': {'login': 'kun-g'}}]
    edit(world, responses=responses)
    source = GitHubSource(str(world['commands'] / 'gh'))
    batch = source.poll(world['target'], None)
    assert all(e.action == 'update' for e in batch.events)
    assert all({'kind', 'actor', 'author', 'label', 'text', 'by_workshop', 'edited'} <= e.body.keys() for e in batch.events)
    e = configured(world)
    for incoming in batch.events:
        e.ingest(incoming)
    actions = {f['body']['body']['data'].get('id'): f['body']['action'] for f in e.log.facts('alpha:7')
               if f['kind'] == 'event' and f['body']['body']['kind'] == 'issue-comment'}
    assert {key for key, value in actions.items() if value == 'rework'} == {1, 2, 6}
    assert len(e.log.all('items')) == 1
    e.change(e.log.get('items', 'alpha:7'), pending_rework=False)
    responses[endpoint][0]['body'] = '不对，改正文'
    edit(world, responses=responses)
    touch_source(world, responses)
    edited = source.poll(world['target'], batch.cursor)
    for incoming in edited.events:
        e.ingest(incoming)
    assert not e.log.get('items', 'alpha:7')['pending_rework']
    # Lost cursor rereads the edited body, but the action identity remains the same.
    for incoming in source.poll(world['target'], None).events:
        e.ingest(incoming)
    assert not e.log.get('items', 'alpha:7')['pending_rework']
    e.log.close()


def test_legacy_rules_use_adapter_and_custom_label(world):
    world['target']['label'] = 'Ship It'
    responses = source_responses(world)
    responses['repos/kun-g/workshop/issues/7/events?per_page=100'][0]['label']['name'] = 'Ship It'
    edit(world, responses=responses)
    source = GitHubSource(str(world['commands'] / 'gh'))
    batch = source.poll(world['target'], None)
    e = configured(world, legacy=True)
    for incoming in batch.events:
        e.ingest(incoming)
    assert e.log.get('items', 'alpha:7')
    assert e.rules.target('alpha')['legacy_triggers']['github'][0]['when']['label'] == 'Ship It'
    e.log.close()


def test_only_config_changes_actor_label_and_text(world):
    responses = source_responses(world)
    label = responses['repos/kun-g/workshop/issues/7/events?per_page=100'][0]
    label['actor'] = {'login': 'different-human'}
    label['label']['name'] = 'Queue'
    edit(world, responses=responses)
    source = GitHubSource(str(world['commands'] / 'gh'))
    batch = source.poll(world['target'], None)
    e = configured(world)
    triggers = e.rules.data['targets']['alpha']['flows']['flow']['triggers']
    triggers[0]['when'].update(actor='different-human', label='Queue')
    for incoming in batch.events:
        e.ingest(incoming)
    assert e.log.get('items', 'alpha:7')
    e.log.close()


def test_old_poll_result_is_normalized_before_cursor_commit(world):
    from workshop.core.models import Event, Poll
    from workshop.core.jobs import Pending, encode, decode
    e = configured(world)
    e.rules.data['targets']['alpha']['sources'] = ['github']
    value = {'id': 81, 'event': 'labeled', 'actor': {'login': 'kun-g'}, 'label': {'name': 'Ready for Agent'}}
    old = Event('github', 'kun-g/workshop:label:7:81', 'alpha', '7', 'old pending poll',
                {'kind': 'label', 'repo': 'kun-g/workshop', 'number': 7, 'data': value}, action='ready')
    payload = encode(Poll([old], 'old-cursor'))
    payload['fields'].pop('normalized')
    for field in ('flow', 'flow_default', 'action_id', 'previous_ids'):
        payload['fields']['events'][0]['fields'].pop(field)
    batch = decode(payload)  # same format as a result.json written before upgrade
    source = GitHubSource(str(world['commands'] / 'gh'))
    e.plugins.register('source', 'github', source)
    class CachedJobs:
        pending = True
        def call(self, port, name, method, args, **kwargs):
            if method == 'poll':
                return batch
            assert method == 'normalize'
            if self.pending:
                raise Pending('normalizing', 'normalizing', None)
            return source.normalize(*args, lambda **kw: None)
    e.jobs = CachedJobs()
    e.sources()
    assert not e.log.all('items') and not e.log.get('cursors', 'alpha:github')
    e.jobs.pending = False
    e.sources()
    assert e.log.get('items', 'alpha:7')
    assert e.log.get('cursors', 'alpha:github')['cursor'] == 'old-cursor'
    assert not e.log.get('items', 'alpha:7')['pending_rework']
    e.sources()
    assert len(e.log.get('items', 'alpha:7')['materials']) == 1
    e.log.close()


def test_old_ready_is_still_checked_against_new_rule(world):
    from workshop.core.models import Event, Poll
    e = configured(world)
    old = Event('github', 'label:7:81', 'alpha', '7', 'old',
                {'kind': 'label', 'data': {'actor': {'login': 'kun-g'}, 'label': {'name': 'Ready for Agent'}}}, action='ready')
    e.rules.data['targets']['alpha']['flows']['flow']['triggers'][0]['when']['actor'] = 'different-human'
    for incoming in GitHubSource.normalize(Poll([old], 'cursor'), world['target'], lambda **kw: None).events:
        e.ingest(incoming)
    assert e.log.get('items', 'alpha:7') is None
    e.log.close()
