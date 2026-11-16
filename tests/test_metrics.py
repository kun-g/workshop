from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from workshop.cli import main
from workshop.core.metrics import calculate, read_snapshot, window


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
RULES = {'limits': {'concurrent': 2}, 'targets': {}}


def fact(second, kind, body, item='a:1'):
    return (0, kind, item, BASE + timedelta(seconds=second), body)


def opened(second=0, item='a:1', **extra):
    return fact(second, 'event', dict(target='a', flow='flow-a', title='一件活', action='ready', **extra), item)


def run(first=0, last=60, facts=(), rules=RULES):
    return calculate(facts, {}, rules, BASE+timedelta(seconds=first), BASE+timedelta(seconds=last))


def test_short_round_and_checks_after_position_released():
    facts = [opened(),
             fact(10, 'round_reserved', dict(token='one', executor='worker')),
             fact(20, 'round_changed', dict(token='one', stopped=True)),
             fact(30, 'round_changed', dict(token='one', status='ended', outcome='failed')),
             fact(30, 'round_finished', dict(state='定时'))]
    result = run(facts=facts)['summary']
    assert result['seconds'] == {'在跑': 20, '定时': 30, '卡住': 0, '排队': 10, '等你': 0, '其他': 0}
    assert result['occupied_seconds'] == 10
    assert result['capacity_seconds'] == 120
    assert result['failed'] == result['rounds'] == 1


def test_clipped_rounds_and_all_state_facts_and_wait_age():
    facts = [opened(-50), fact(-20, 'round_reserved', dict(token='one', executor='worker')),
             fact(5, 'round_changed', dict(token='one', stopped=True)),
             fact(10, 'round_changed', dict(token='one', status='ended', outcome='done')),
             fact(10, 'item_changed', dict(state='定时')),
             fact(20, 'work_blocked', dict(state='卡住')),
             fact(30, 'gate_passed', dict(state='等你')),
             fact(40, 'round_finished', dict(state='排队'))]
    result = run(facts=facts)['summary']
    assert result['seconds'] == {'在跑': 10, '定时': 10, '卡住': 10, '排队': 20, '等你': 10, '其他': 0}
    assert result['rounds'] == 0
    assert result['occupied_seconds'] == 5
    result = run(50, 60, facts)['summary']
    assert result['longest']['排队']['seconds'] == 20  # 年龄不按窗口起点重置。


def test_program_round_is_flow_but_never_position_or_failure():
    facts = [opened(), fact(5, 'item_changed', dict(state='跑着')),
             fact(5, 'round_reserved', dict(token='program', executor=None)),
             fact(15, 'round_changed', dict(token='program', status='ended', outcome='failed')),
             fact(15, 'gate_passed', dict(state='完成'))]
    result = run(facts=facts)['summary']
    assert result['seconds']['在跑'] == 10
    assert result['occupied_seconds'] == result['rounds'] == result['failed'] == 0
    assert result['in_flight'] == 0


def test_pending_release_rework_and_abandonment():
    facts = [fact(-100, 'event', dict(target='a', flow='f', action='update', release_gate=True)),
             fact(10, 'event', dict(action='ready')),
             fact(20, 'item_changed', dict(state='等你', wait_for_ready=True)),
             fact(30, 'event', dict(action='ready')),
             fact(40, 'item_changed', dict(state='放弃')),
             fact(50, 'item_changed', dict(state='空闲', wait_for_ready=False))]
    result = run(0, 10, facts)['summary']
    assert result['pending'] == 1 and result['in_flight'] == 0
    assert result['oldest_pending']['seconds'] == 110
    result = run(facts=facts)['summary']
    assert result['in_flight'] == 1 and result['pending'] == 0
    assert result['seconds']['排队'] == 30


def test_hour_boundaries_groups_and_failures_start_cohort():
    facts = [opened(3590), opened(3590, 'b:2'),
             fact(3595, 'round_reserved', dict(token='one', executor='worker')),
             fact(3610, 'round_changed', dict(token='one', status='ended', outcome='failed'))]
    facts[1][4].update(target='b', flow='flow-b')
    result = run(3590, 3605, facts)
    assert len(result['groups']) == 2
    assert [h['occupied_seconds'] for h in result['hours']] == [5, 5]
    assert [h['failed'] for h in result['hours']] == [1, 0]
    assert result['summary']['occupied_seconds'] == 10
    assert result['summary']['seconds']['在跑'] == 10
    assert sum(g['occupied_seconds'] for g in result['groups']) == 10
    for state, seconds in result['summary']['seconds'].items():
        assert sum(h['seconds'][state] for h in result['hours']) == seconds


def test_overlapping_rounds_count_item_time_once_and_first_stopped_wins():
    facts = [opened(), fact(10, 'round_reserved', dict(token='one', executor='worker')),
             fact(15, 'round_reserved', dict(token='two', executor='worker')),
             fact(20, 'round_changed', dict(token='one', stopped=True)),
             fact(25, 'round_changed', dict(token='one', stopped=True)),
             fact(30, 'round_changed', dict(token='one', status='ended')),
             fact(35, 'round_changed', dict(token='two', status='ended'))]
    result = run(facts=facts)['summary']
    assert result['occupied_seconds'] == 30
    assert result['seconds']['在跑'] == 25


def test_empty_window_and_invalid_arguments():
    result = run(facts=[])['summary']
    assert result['in_flight'] == result['pending'] == result['load'] == result['efficiency'] == 0
    assert not any(result['seconds'].values())
    for kwargs in [dict(since='0h'), dict(since=''), dict(since='junk'), dict(start=BASE.isoformat()),
                   dict(start=BASE.isoformat(), end=BASE.isoformat()),
                   dict(start='2026-01-01', end='2026-01-02'),
                   dict(since='1h', start=BASE.isoformat(), end=(BASE+timedelta(hours=1)).isoformat())]:
        with pytest.raises(ValueError):
            window(**kwargs)
    first, last = window(now=BASE)
    assert last-first == timedelta(hours=24)


def database(path):
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE controls(id TEXT,body TEXT); CREATE TABLE items(id TEXT,body TEXT);'
                     'CREATE TABLE facts(seq INTEGER PRIMARY KEY,kind TEXT,item TEXT,at TEXT,body TEXT);')
    db.execute('INSERT INTO controls VALUES (?,?)', ('@rules', json.dumps({'data': RULES})))
    for i, (_, kind, item, at, body) in enumerate([opened(), fact(10, 'item_changed', dict(state='卡住'))]):
        db.execute('INSERT INTO facts VALUES (?,?,?,?,?)', (i, kind, item, at.isoformat(), json.dumps(body)))
    db.commit()
    return db


def test_cli_readonly_bypasses_log_and_allows_writer(tmp_path, capsys, monkeypatch):
    path = tmp_path / 'snapshot.db'
    writer = database(path)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('BEGIN IMMEDIATE')
    writer.execute('INSERT INTO items VALUES (?,?)', ('uncommitted', '{}'))
    monkeypatch.setattr('workshop.cli.Log', lambda *args: pytest.fail('must not initialize Log'))
    before = path.read_bytes()
    args = ['metrics', '--database', str(path), '--from', BASE.isoformat(),
            '--to', (BASE+timedelta(seconds=60)).isoformat()]
    assert main(args) == 0
    output = capsys.readouterr().out
    assert '汇总' in output and '卡住=50.000s' in output and '按小时' in output
    assert before == path.read_bytes()
    writer.rollback()
    assert writer.execute('SELECT count(*) FROM items').fetchone()[0] == 0
    writer.close()
    snapshot = read_snapshot(path)
    assert len(snapshot[0]) == 2


def test_cli_bad_database_and_window_no_side_effects(tmp_path, capsys):
    missing = tmp_path / 'missing' / 'workshop.db'
    assert main(['metrics', '--database', str(missing)]) == 2
    assert '车间：' in capsys.readouterr().err
    assert not missing.parent.exists()
    corrupt = tmp_path / 'bad.db'
    corrupt.write_text('not sqlite')
    assert main(['metrics', '--database', str(corrupt)]) == 2
    assert main(['metrics', '--since=-1h']) == 2


def test_classified_release_and_drop_do_not_leave_phantom_supply():
    facts = [fact(0, 'event', dict(target='a', action='update', pending_classification=True)),
             fact(10, 'event_classified', dict(event=dict(target='a', action='update', release_gate=True))),
             fact(20, 'event_classified', dict(action='ready')),
             fact(30, 'item_dropped', {})]
    result = run(facts=facts)['summary']
    assert result['pending'] == result['in_flight'] == 0
    assert result['seconds']['排队'] == 10


def test_hour_at_boundary_excludes_next_state():
    facts = [opened(), fact(3600, 'work_blocked', dict(state='卡住'))]
    result = run(0, 7200, facts)
    assert result['hours'][0]['longest']['排队']['seconds'] == 3600
    assert result['hours'][1]['longest']['卡住']['seconds'] == 3600
