"""#151：不登录机器的逐实例对账、来源与范围排除。"""
from copy import deepcopy
from pathlib import Path
import subprocess

import pytest

from test_watch_updates import software, world, item_for, advance  # noqa: F401
from workshop.plugins.watch_updates.inventory import reconcile, with_scopes, filter_scopes
from workshop.plugins.watch_updates.io import read, write
from workshop.plugins.watch_updates.operations import preview_inventory

NOW = '2026-11-16T01:00:00+00:00'


def observed(row=None, kind='deployment_repo'):
    row = deepcopy(row or software())
    for p in row['instances']:
        p['version_source'] = {'kind': kind, 'reference': 'repo@commit:values.yaml'}
    return row


def proposal(row=None, ok=True):
    return {'sources': [{'id': 'server', 'ok': ok, 'evidence': 'read' if ok else 'API unavailable'}],
            'software': [] if row is None else [row], 'removed': [], 'needs': []}


def test_unknown_and_missing_instances_preserve_version_date_evidence_and_input():
    before = {'software': [software()], 'excluded': {}}
    saved = deepcopy(before)
    row = observed()
    row['instances'][0].update(version='2.0.0', read_at=NOW)
    row['instances'][1].update(version='unknown', read_at=NOW, evidence='timeout', error='API timeout', role='agent')
    result, _ = reconcile(before, proposal(row), NOW, strict=True)
    one, two = result['software'][0]['instances']
    assert one['version'] == '2.0.0' and one['last_attempt']['ok']
    assert all(two[k] == saved['software'][0]['instances'][1][k] for k in ('version', 'read_at', 'evidence'))
    assert two['last_attempt'] == {'at': NOW, 'ok': False, 'error': 'API timeout'}
    assert two['role'] == 'agent'
    row['instances'].pop()
    result, _ = reconcile(before, proposal(row), NOW, strict=True)
    assert len(result['software'][0]['instances']) == 2
    assert result['software'][0]['instances'][1]['last_attempt']['ok'] is False
    assert before == saved


def test_source_failure_and_omitted_software_never_erase_known_reading():
    previous = {'software': [software()]}
    for candidate in (proposal(observed(), False), proposal()):
        after, _ = reconcile(previous, candidate, NOW, strict=True)
        for old, current in zip(previous['software'][0]['instances'], after['software'][0]['instances']):
            assert all(old[k] == current[k] for k in ('version', 'read_at', 'evidence'))
            assert current['last_attempt']['ok'] is False


def test_first_unknown_stays_unknown_and_history_cannot_be_redated():
    row = observed()
    row['instances'][0].update(version='unknown', error='no version endpoint')
    result, _ = reconcile({'software': []}, proposal(row), NOW, strict=True)
    assert result['software'][0]['instances'][0]['error'] == 'no version endpoint'
    row = observed(kind='history')
    row['instances'][0]['read_at'] = NOW
    with pytest.raises(ValueError, match='历史'):
        reconcile({'software': [software()]}, proposal(row), NOW, strict=True)
    with pytest.raises(ValueError, match='version_source'):
        reconcile({'software': [software()]}, proposal(software()), NOW, strict=True)


def test_scope_exclusion_survives_two_rediscoveries_and_does_not_remove_other_hosts():
    row = observed()
    row['instances'] += [dict(row['instances'][0], id='asia', host='k8s gmp-asia / monitoring'),
                         dict(row['instances'][0], id='central', host='k8s central-admin', cluster='central-admin')]
    previous = with_scopes({'software': [row], 'excluded': {'ignore': '不用盯'}}, [
        {'kind': 'cluster', 'value': 'gmp-asia', 'reason': '退役'},
        {'kind': 'host', 'value': 'dc1', 'reason': '不再盯'}])
    for _ in range(2):
        p = proposal(row)
        p['software'].append(observed(software('ignore')))
        previous, _ = reconcile(previous, p, NOW, strict=True)
        assert [s['id'] for s in previous['software']] == ['n8n']
        assert {i['id'] for i in previous['software'][0]['instances']} == {'two', 'central'}
        assert len(previous['excluded_scopes']) == 2
    # 只有范围内实例的软件整体移出；不从软件的 notes 推断其他范围。
    only = deepcopy(row)
    only['instances'] = [p for p in row['instances'] if p['id'] == 'asia']
    previous['software'] = [only]
    assert filter_scopes(previous)['software'] == []


def test_explicit_removal_is_per_instance_and_requires_success():
    previous = {'software': [software()]}
    p = proposal(observed())
    p['removed'] = [{'id': 'n8n', 'instance': 'one', 'source': 'server', 'uninstalled': True, 'evidence': 'manifest deleted'}]
    after, _ = reconcile(previous, p, NOW, strict=True)
    assert [i['id'] for i in after['software'][0]['instances']] == ['two']
    p['sources'][0]['ok'] = False
    with pytest.raises(ValueError, match='移出'):
        reconcile(previous, p, NOW, strict=True)
    p['sources'][0]['ok'] = True
    del p['removed'][0]['instance']
    with pytest.raises(ValueError, match='instance'):
        reconcile(previous, p, NOW, strict=True)


def test_repository_declaration_conflict_keeps_both_provenances():
    previous = {'software': [observed(kind='dokploy')]}
    row = observed()
    row['instances'][0].update(version='2.0.0', read_at=NOW)
    result, _ = reconcile(previous, proposal(row), NOW, strict=True)
    place = result['software'][0]['instances'][0]
    assert place['version_source']['kind'] == 'deployment_repo'
    assert [(v['version'], v['version_source']['kind']) for v in place['conflicts']] == [
        ('1.0.0', 'dokploy'), ('2.0.0', 'deployment_repo')]


def test_upgrade_records_use_latest_completed_reading_without_discovering_or_deploying():
    old = {'software': [software()]}
    records = [{'software': 'n8n', 'instance': 'one', 'version': '1.2.0', 'completed_at': '2026-11-15T00:00:00+00:00', 'evidence': 'upgrade:1'},
               {'software': 'n8n', 'instance': 'one', 'version': '1.3.0', 'completed_at': NOW, 'evidence': 'upgrade:2'},
               {'software': 'unknown', 'instance': 'missing', 'version': '2.0.0', 'completed_at': NOW, 'evidence': 'upgrade:3'}]
    after, _ = reconcile(old, proposal(), NOW, strict=True, upgrades=reversed(records))
    first, second = after['software'][0]['instances']
    assert (first['version'], first['read_at'], first['evidence']) == ('1.3.0', NOW, 'upgrade:2')
    assert first['version_source']['kind'] == 'upgrade_record'
    assert second['version'] == '1.1.0'
    assert len(after['software']) == 1
    # 较早的升级记录不覆盖较新声明值，但留下冲突。
    row = observed()
    row['instances'][0].update(version='2.0.0', read_at=NOW)
    result, _ = reconcile(old, proposal(row), NOW, strict=True, upgrades=records[:1])
    assert result['software'][0]['instances'][0]['version'] == '2.0.0'
    assert len(result['software'][0]['instances'][0]['conflicts']) == 2


def test_complete_flow_uses_fixed_sources_and_never_executes_remote_commands(world, monkeypatch, tmp_path):
    io, target, flow, source = world
    target['watch_updates'].update(inventory_sources=[{'id': 'server', 'kind': 'deployment_repo'}],
        upgrade_records=str(tmp_path/'upgrades.json'),
        excluded_scopes=[{'kind': 'host', 'value': 'dc2', 'reason': '不用盯'}])
    write(tmp_path/'upgrades.json', {'records': [{'software': 'n8n', 'instance': 'one', 'version': '2.0.0',
        'completed_at': NOW, 'evidence': 'upgrade:test'}]})
    calls = []
    real = subprocess.run
    def run(argv, *args, **kw):
        calls.append(argv)
        assert isinstance(argv, list) and Path(argv[0]).name == 'git', argv
        assert argv[1] in ('init', '-C'), argv
        assert not any(x in argv for x in ('ssh', 'kubectl', 'ssm', 'exec'))
        return real(argv, *args, **kw)
    monkeypatch.setattr(subprocess, 'run', run)
    poll = source.poll(target, None)
    item = item_for(next(e for e in poll.events if e.body['kind'] == 'reconcile'))
    brief = flow.brief(item, '说清楚', target)
    assert '禁止调用 ssh、kubectl' in brief.instructions
    assert brief.materials[0]['body']['upgrades']['records'][0]['version'] == '2.0.0'
    p = proposal(observed())
    advance(flow, item, target, '说清楚', {'inventory': p})
    current = read(io.file(target, 'watchlist'))
    assert [i['id'] for i in current['software'][0]['instances']] == ['one']
    assert current['software'][0]['instances'][0]['version'] == '2.0.0'
    assert read(io.file(target, 'sources'))['sources'] == target['watch_updates']['inventory_sources']
    assert not io.sent and calls
    # 重放也不能把 dc2 加回来。
    advance(flow, item, target, '说清楚', {'inventory': p})
    assert len(io.watchlist(target)['software'][0]['instances']) == 1


def test_preview_is_isolated_and_missing_upgrade_records_are_a_blind_spot(world, tmp_path, monkeypatch):
    io, target, flow, source = world
    target['watch_updates']['upgrade_records'] = str(tmp_path/'absent.json')
    before = {k: Path(target['watch_updates'][k]).read_bytes() for k in ('watchlist', 'needs', 'sources')}
    p = tmp_path/'proposal.json'
    write(p, proposal(observed()))
    monkeypatch.setattr(io, 'command', lambda *a, **kw: pytest.fail('预览不可执行任何命令'))
    result = preview_inventory(target, p, tmp_path/'preview', io)
    assert result['sent'] is False
    assert read(tmp_path/'preview/upgrades.json')['error'].startswith('尚无升级完成记录')
    assert all(Path(target['watch_updates'][k]).read_bytes() == v for k, v in before.items())
    with pytest.raises(ValueError, match='新目录'):
        preview_inventory(target, p, Path(target['watch_updates']['state_dir'])/'preview', io)


def test_wrong_remote_provenance_and_unregistered_sources_rejected(world):
    io, target, flow, _ = world
    target['watch_updates']['inventory_sources'] = [{'id': 'server', 'kind': 'dokploy', 'host': 'dc1'}]
    with pytest.raises(ValueError, match='范围'):
        flow.inventory(proposal(observed(kind='dokploy')), target)
    p = proposal(observed())
    p['sources'].append({'id': 'ssh', 'ok': True, 'evidence': 'not allowed'})
    with pytest.raises(ValueError, match='未登记'):
        flow.inventory(p, target)
    row = observed(kind='ssh')
    with pytest.raises(ValueError, match='来源'):
        reconcile({'software': []}, proposal(row), NOW, strict=True)


def test_upgrade_reader_failure_does_not_block_other_sources_or_trust_proposal(world, tmp_path):
    io, target, flow, _ = world
    location = tmp_path/'upgrades.json'
    target['watch_updates']['upgrade_records'] = str(location)
    write(location, {'records': [{'software': 'n8n', 'instance': 'one', 'version': 'unknown',
                                 'completed_at': NOW, 'evidence': 'not completed'}]})
    assert io.upgrades(target)['error'].startswith('升级完成记录不可用')
    row = observed()
    row['instances'][0]['version'] = '2.0.0'
    result, _ = flow.inventory(proposal(row), target)
    assert result['software'][0]['instances'][0]['version'] == '2.0.0'
    row['instances'][0]['version_source']['kind'] = 'upgrade_record'
    with pytest.raises(ValueError, match='完成记录'):
        flow.inventory(proposal(row), target)


def test_preview_inventory_cli_and_registered_policy(world, tmp_path, capsys):
    import json
    from workshop.plugins.watch_updates.__main__ import main
    from workshop.registry import load_rules
    io, target, _, _ = world
    rules, payload = tmp_path/'rules.json', tmp_path/'proposal.json'
    write(rules, {'targets': {'alpha': target}})
    write(payload, proposal(observed()))
    assert main(['preview-inventory', '--rules', str(rules), '--target', 'alpha',
                 '--proposal', str(payload), '--output', str(tmp_path/'out')]) == 0
    assert json.loads(capsys.readouterr().out)['sent'] is False
    real = load_rules(Path(__file__).parents[1]/'rules/workshop.yaml').data
    config = real['targets']['workshop']['watch_updates']
    assert {r['kind'] for r in config['inventory_sources']} == {'local','deployment_repo','dokploy','upgrade_record','needs'}
    assert any(r['kind'] == 'cluster' and r['value'] == 'gmp-asia' for r in config['excluded_scopes'])
    assert '禁止调用 ssh、kubectl' in real['executor_profiles']['watch-inventory']['prompt']
