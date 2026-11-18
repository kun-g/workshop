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
