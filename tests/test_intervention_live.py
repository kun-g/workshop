"""上线后检查必须能指出真实故障，并且只读状态库。"""
import argparse
import json
from pathlib import Path
import runpy

import pytest

from workshop.core.log import Log

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def live(tmp_path):
    code = runpy.run_path(str(ROOT / 'scripts/verify_intervention_live.py'))
    (tmp_path / 'release.json').write_text(json.dumps({'status': 'ok', 'at': '2026-01-01T00:00:00+00:00',
        'confirmed_at': '2026-01-01T00:00:01+00:00', 'to': 'abc'}))
    log = Log(tmp_path / 'workshop.db')
    log.append('control', {'id': '@rules', 'path': str(ROOT / 'rules/workshop.yaml')})
    yield code, log, tmp_path
    log.close()


def test_no_cards_inspects_sent_facts_and_preserves_database(live, capsys):
    code, log, state = live
    args = argparse.Namespace(state_dir=state, check='no-cards')
    log.append('card_created', {'id': 'ordinary', 'mode': 'notice', 'payload': {'key': 'later'}})
    before = log.db.execute('SELECT count(*) FROM facts').fetchone()[0]
    code['verify'](args)
    assert log.db.execute('SELECT count(*) FROM facts').fetchone()[0] == before
    log.append('card_changed', {'id': 'ordinary', 'sent': True})
    with pytest.raises(ValueError, match='仍发送'):
        code['verify'](args)
    assert 'ordinary' in capsys.readouterr().out


def test_release_window_and_empty_status_are_checked(live, capsys):
    code, log, state = live
    code['verify'](argparse.Namespace(state_dir=state, check='status'))
    assert '等了多久' in capsys.readouterr().out
    (state / 'release.json').write_text(json.dumps({'status': 'pending'}))
    with pytest.raises(ValueError, match='空窗口'):
        code['release_start'](state)


def test_service_rejects_wrong_rule_digest_and_stale_heartbeat(live, monkeypatch):
    code, log, state = live
    import hashlib
    rule = ROOT / 'rules/workshop.yaml'
    result = {'rules': str(rule), 'rules_sha256': 'wrong'}
    class Port:
        def validate(self, config):
            pass
        def invoke(self, *args):
            return result
    class Plugins:
        def get(self, *args):
            return Port()
    monkeypatch.setitem(code['verify'].__globals__, 'Registry', Plugins)
    args = argparse.Namespace(state_dir=state, check='service')
    with pytest.raises(ValueError, match='现役规则'):
        code['verify'](args)
    result['rules_sha256'] = hashlib.sha256(rule.read_bytes()).hexdigest()
    (state / 'heartbeat.json').write_text(json.dumps({'at': '2026-01-01T00:00:00+00:00', 'process': {}}))
    with pytest.raises(ValueError, match='心跳'):
        code['verify'](args)


def test_intake_uses_recorded_label_events_only(live):
    code, log, state = live
    item = {'id': 'workshop:7', 'target': 'workshop', 'object_key': '7', 'created': '2026-11-18T00:00:00+00:00'}
    log.db.execute('INSERT INTO items VALUES (?,?)', (item['id'], json.dumps(item)))
    target = {'name': 'workshop', 'repo': 'kun-g/workshop', 'intake_app_logins': ['lambdatheory-agent[bot]']}
    assert code['inspect_intake'](log, target, '2026-01-01')['problems']
    for actor, expected in [('stranger', True), ('lambdatheory-agent[bot]', False)]:
        log.append('event', {'pending_classification': True, 'body': {'kind': 'label',
            'label': 'Ready for Agent', 'actor': actor}}, item['id'], source='github')
        assert bool(code['inspect_intake'](log, target, '2026-01-01')['problems']) == expected


def test_checks_run_with_temporary_home_without_github(live):
    import subprocess
    import sys
    import pwd
    from workshop.plugins.issue_delivery.io import clean_env
    code, log, state = live
    home = state / 'temporary-home'
    home.mkdir()
    for name in ('no-cards', 'status', 'intake'):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/verify_intervention_live.py'), name,
            '--state-dir', str(state)], env=clean_env(home), capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
    assert code['resident_path']('~/service.sock', state) == Path(pwd.getpwuid(state.stat().st_uid).pw_dir) / 'service.sock'


def test_no_approval_cards_checks_creation_even_without_send(live):
    code, log, state = live
    args = argparse.Namespace(state_dir=state, check='no-approval-cards')
    code['verify'](args)
    log.append('card_created', {'id':'legacy','core_approval':True,'mode':'ask','payload':{}})
    with pytest.raises(ValueError, match='仍建过'):
        code['verify'](args)


def test_authorization_audit_rejects_missing_or_mismatched_evidence(live):
    from workshop.core.rules import Rules
    code, log, state = live
    rules = Rules.load(ROOT/'rules/workshop.yaml')
    body = {'gate_stage':'交付','actions':['对外发消息'], 'action_targets':{'对外发消息':'target'},
            'authorized_actions':[{'action':'对外发消息','target':'target','request':'original','result':'approved','applied':True}]}
    log.db.execute('INSERT INTO items VALUES (?,?)', ('workshop:one',json.dumps({'cause':0,'stage':'审查','revision':0})))
    log.append('gate_passed', body, 'workshop:one')
    assert len(code['inspect_authorizations'](log,rules,'2026')) == 1
    body['authorized_actions'][0]['target'] = 'another'
    log.db.execute('INSERT INTO items VALUES (?,?)', ('workshop:two',json.dumps({'cause':0,'stage':'审查','revision':0})))
    log.append('gate_passed',body,'workshop:two')
    with pytest.raises(ValueError, match='缺原申请'):
        code['inspect_authorizations'](log,rules,'2026')


def test_deliveries_audit_rejects_non_html_and_duplicate_send(live):
    from workshop.core.rules import Rules
    from workshop.plugins.intervention.service import Service
    code, log, state = live
    service = Service(state,ROOT/'rules/workshop.yaml')
    permit = service.call('permit', {'operation':'p','point':'update_report','owner':'watch', 'scope':'report:one',
        'basis':{'contract':'盯更新','artifact':'one.html'}})['ref']
    delivery = service.call('enqueue', {'operation':'e','permit':permit,
        'payload':{'kind':'update_report','artifact':'one.html','sha256':'abc'}})['ref']
    service.transport = lambda *a: {'state':'sent','external':'om_one'}
    service.call('dispatch',{'ref':delivery})
    rules = Rules.load(ROOT/'rules/workshop.yaml')
    rules.data['targets']['workshop']['watch_updates']['state_dir'] = str(state/'watch')
    endpoint = state/'run/service.sock'
    assert len(code['inspect_deliveries'](rules,state,endpoint,'2026')['deliveries']) == 1
    entry = service.store.get(delivery)
    service.delivery_state(entry,'sending')
    with pytest.raises(ValueError, match='超过一次'):
        code['inspect_deliveries'](rules,state,endpoint,'2026')


def test_manager_count_sections_and_duplicate_session_checks_are_readonly(live):
    from workshop.core.managers import Store
    from workshop.core.rules import Rules
    from workshop.registry import Registry
    code,log,state=live
    rules=Rules.load(ROOT/'rules/workshop.yaml')
    before=log.db.total_changes
    value=code['inspect_managers'](log,state,rules,Registry())
    assert set(value['sections'])=={'等你','项目经理处理中','处理不了'}
    store=Store(state)
    try:
        for key in ('a','b'):
            row=store.observe(key,{'reason':'真实记录'},'core','2026-11-18T00:00:00+00:00')
            row.update(token=key,executor='codex',started={'session':'same'})
            store.save(row,'started')
        with pytest.raises(ValueError,match='重复'):
            code['inspect_managers'](log,state,rules,Registry())
    finally:store.close()
    assert log.db.total_changes==before


def test_business_count_mismatch_is_not_silently_accepted(live):
    code,log,state=live
    (state/'heartbeat.json').write_text(json.dumps({'business':{'agents':1,'jobs':0,'total':1,'round_tokens':['missing'],'job_ids':[]}}))
    with pytest.raises(ValueError,match='业务计数不符'):
        code['inspect_busy'](log,state)
    (state/'heartbeat.json').write_text(json.dumps({'business':{'agents':0,'jobs':0,'total':0,'round_tokens':[],'job_ids':[]}}))
    assert code['inspect_busy'](log,state)['observed']['total']==0
