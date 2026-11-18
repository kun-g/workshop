from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import json

from conftest import build, configuration
from test_watch_updates import world, software, judgment, item_for, advance
from workshop.core.models import Event
from workshop.core.holding import resolve
from workshop.core.report import status, why
from workshop.plugins.watch_updates.flow import Flow
from workshop.plugins.watch_updates.io import read, write
from workshop.plugins.watch_updates.migration import migrate
from workshop.plugins.watch_updates.model import SCHEMA


def engine_for(world, tmp_path):
    io, target, _, source = world
    flow = Flow(io)
    config = configuration()
    triggers = read('rules/workshop.yaml')['targets']['workshop']['flows']['watch_updates']['triggers']
    config['targets']['alpha'] = {**target, 'sources': [], 'plugin': 'flow', 'pending_limit': 10000,
        'default_flow': 'flow', 'flows': {'flow': {'triggers': []},
                                        'watch_updates': {'plugin': 'flow', 'triggers': triggers}}}
    engine = build(tmp_path / 'engine', config)
    engine.plugins.register('flow', 'flow', flow)
    engine.clock.now = io.time
    return engine, flow


def ingest(engine, world, cursor=None, kinds=None):
    io, target, _, source = world
    poll = source.poll(target, cursor)
    batch = source.normalize(poll, target)
    for event in batch.events:
        if kinds is None or event.body['kind'] in kinds:
            engine.ingest(replace(event, action='rework' if event.body.get('resume') else 'ready'))
    return poll.cursor, batch


def until(engine, predicate):
    # 同步测试引擎每拍都有确定进展，无 wall-clock sleep。
    from time import monotonic
    deadline = monotonic() + 10
    for _ in range(150):
        engine.tick()
        if predicate():
            return
        if monotonic() >= deadline:
            break
    raise AssertionError(engine.log.all('items'))


def respond(engine, world, *, blocked=False):
    io, target, _, _ = world
    until(engine, lambda: bool(engine.active()))
    row = engine.active()[0]
    item = engine.log.get('items', row['item'])
    state, check = Flow(io).snapshot(item, target)
    from workshop.plugins.watch_updates.checks import candidate
    key, data = candidate(state, check)
    if blocked:
        value = {'blocked': {'reason': '真实来源不可达', 'next': '下次对账核对来源'}}
    elif data['kind'] == 'reconcile':
        value = {'inventory': {'sources': [{'id': 'server', 'ok': True, 'evidence': 'read'}],
                              'software': io.watchlist(target)['software'], 'needs': []}}
    else:
        value = {'judgment': judgment(True)}
    engine.submit(row['token'], {'outcome': 'done', 'summary': '处理本项', 'advance': '证据',
                                'evidence': [json.dumps({'schema': SCHEMA, 'task_key': key, **value})]})
    engine.tick()


def test_normalization_keeps_entry_identity_but_has_one_check(world):
    io, target, _, source = world
    value = io.watchlist(target)
    value['software'].append(software('second'))
    write(io.file(target, 'watchlist'), value)
    raw = source.poll(target, None)
    batch = source.normalize(raw, target)
    assert len(batch.events) == 3
    assert {e.object_key for e in batch.events} == {'check'}
    assert len({e.body['entry_key'] for e in batch.events}) == 3
    assert batch.cursor == raw.cursor and batch.normalized


def test_real_engine_multiple_entries_feedback_and_parallel_upgrade(world, tmp_path):
    io, target, _, source = world
    write(io.file(target, 'needs'), {'needs': []})
    value = io.watchlist(target)
    value['software'].append(software('second'))
    write(io.file(target, 'watchlist'), value)
    engine, flow = engine_for(world, tmp_path)
    try:
        cursor, batch = ingest(engine, world)
        for _ in range(3):
            respond(engine, world)
        until(engine, lambda: engine.log.all('items')[0]['state'] == '定时')
        assert len(engine.log.all('items')) == 1
        assert len(engine.log.all('rounds')) == 3  # 对账一次、两软件各判一次。
        report = next(r for r in io.sent if r['kind'] == 'weekly')
        assert report['anchor'] == 'check' and len(report['versions']) == 2
        ids = [k for k, r in report['feedback_targets'].items() if r['kind'] == 'instances'
               and all(m['key'] == report['versions'][0] for m in r['members'])]
        assert ids
        io.messages['upgrade'] = json.dumps({'report': report['id'], 'items': [{'id': x, 'choice': '升'} for x in ids]})
        engine.ingest(Event('feishu', 'upgrade', 'alpha', 'feedback', '反馈', {}), ingress='external')
        until(engine, lambda: len(io.state(target)['consumed']) == 1)
        cursor, _ = ingest(engine, world, cursor)
        until(engine, lambda: len(engine.log.all('items')) == 2 and
              all(i['state'] == '定时' for i in engine.log.all('items')))
        check = next(i for i in engine.log.all('items') if i['object_key'] == 'check')
        upgrade = next(i for i in engine.log.all('items') if i['object_key'].startswith('upgrade:'))
        assert check['next'].startswith('等反馈') and upgrade['next'] == '待升级'
        assert len([m for m in upgrade['materials'] if m['body'].get('kind') == 'upgrade']) == 2
        assert len(engine.log.all('rounds')) == 3  # 反馈、升级占位不再叫模型。
        # 等反馈时新版本进入同一件检查活并开始处理。
        io.time += timedelta(days=1)
        value['software'].append(software('third'))
        write(io.file(target, 'watchlist'), value)
        cursor, batch = ingest(engine, world, cursor)
        until(engine, lambda: bool(engine.active()))
        assert engine.active()[0]['item'] == check['id']
        assert len(engine.log.all('items')) == 2
    finally:
        engine.log.close()


def test_blocked_entry_does_not_prevent_other_judgments(world, tmp_path):
    io, target, _, _ = world
    value = io.watchlist(target)
    value['software'].append(software('second'))
    write(io.file(target, 'watchlist'), value)
    engine, _ = engine_for(world, tmp_path)
    try:
        ingest(engine, world, kinds={'version'})
        respond(engine, world, blocked=True)
        respond(engine, world)
        state = io.state(target)
        assert sum('judgment' in v for v in state['versions'].values()) == 1
        assert any(v.get('blocked') for v in state['checks']['check']['entries'].values())
        assert len(engine.log.all('items')) == 1
    finally:
        engine.log.close()


def test_migration_backups_aliases_history_and_replay(world, tmp_path):
    io, target, legacy, source = world
    engine, _ = engine_for(world, tmp_path)
    try:
        raw = source.poll(target, None)
        events = [e for e in raw.events if e.body['kind'] == 'version']
        old = replace(events[0], flow='watch_updates', action='ready')
        # 直接追加和真实来源一致的事实；旧 key 留历史，迁移只追加不改事实。
        engine.log.append('event', asdict(old), old.item_id, source=old.source, ext_id='old')
        engine.log.append('item_changed', {'state': '定时', 'stage': '验收', 'blocked_by': 'old reason'}, old.item_id)
        item = item_for(events[0])
        advance(legacy, item, target, '接单')
        advance(legacy, item, target, '执行', {'judgment': judgment()})
        state = io.state(target)
        state['checks'] = {'check': {'entries': {}, 'failures': {'old': {'error': '旧来源不可达'}}}}
        io.save(target, state)
        # 真实历史中的重判事件只有键，完整软件/发布资料已在版本状态里。
        minimal = {**asdict(old), 'body': {'kind': 'version', 'day': '2026-11-16', 'batch': []}}
        engine.log.append('item_changed', {'materials': [{'source': 'watch_updates', 'ext_id': 'minimal',
                                                         'body': minimal['body']}]}, old.item_id)
        ended = replace(old, object_key='finished', ext_id='finished')
        engine.log.append('event', asdict(ended), ended.item_id, source=ended.source, ext_id='finished')
        engine.log.append('item_changed', {'state': '完成'}, ended.item_id)
        first = migrate(engine.log.path, target, io=io)
        assert first['migrated'] == 1
        from pathlib import Path
        assert (Path(first['backup']) / 'workshop.db').is_file()
        assert migrate(engine.log.path, target, io=io) == {'status': 'unchanged', 'migrated': 0}
        assert resolve(engine.log, {}, old.item_id) == 'alpha:watch_updates:check'
        assert engine.log.get('items', ended.item_id)['state'] == '完成'
        assert 'old reason' in why(engine.log, 'alpha:watch_updates:check', raw=True)
        before = engine.log.all('items')
        engine.log.rebuild()
        assert engine.log.all('items') == before
    finally:
        engine.log.close()


def test_weekly_wait_keeps_same_check_and_completion_opens_next(world, tmp_path):
    io, target, _, source = world
    write(io.file(target, 'needs'), {'needs': []})
    engine, flow = engine_for(world, tmp_path)
    try:
        cursor, _ = ingest(engine, world)
        respond(engine, world)
        respond(engine, world)
        until(engine, lambda: engine.log.all('items')[0]['state'] == '定时')
        first_reported = next(iter(io.state(target)['versions'].values()))['first_reported']
        io.time += timedelta(days=7)
        engine.clock.now = io.time
        cursor, batch = ingest(engine, world, cursor)
        assert {e.object_key for e in batch.events} == {'check'}
        respond(engine, world)
        until(engine, lambda: engine.log.all('items')[0]['state'] == '定时')
        assert next(iter(io.state(target)['versions'].values()))['first_reported'] == first_reported
        assert len(engine.log.all('items')) == 1
        report = [r for r in io.sent if r['kind'] == 'weekly'][-1]
        from pathlib import Path
        assert '7 天' in Path(report['path']).read_text()
        choices = [{'id': k, 'choice': '没问题' if t['kind'] == 'instances' else '用不上'}
                   for k, t in report['feedback_targets'].items()]
        io.messages['complete'] = json.dumps({'report': report['id'], 'items': choices})
        engine.ingest(Event('feishu', 'complete', 'alpha', 'feedback', '完成反馈', {}), ingress='external')
        until(engine, lambda: engine.log.all('items')[0]['state'] == '完成')
        io.time += timedelta(days=1)
        watchlist = io.watchlist(target)
        watchlist['software'].append(software('new'))
        write(io.file(target, 'watchlist'), watchlist)
        cursor, batch = ingest(engine, world, cursor)
        assert {e.object_key for e in batch.events} == {'check:2'}
        until(engine, lambda: bool(engine.active()))
        assert engine.active()[0]['item'].endswith(':check:2')
        assert engine.log.all('items')[0]['state'] == '完成'
        io.messages['late'] = json.dumps({'report': report['id'], 'items': choices})
        engine.ingest(Event('feishu', 'late', 'alpha', 'feedback', '旧报告新反馈', {}), ingress='external')
        until(engine, lambda: any(m.get('ext_id') == 'late' for m in engine.log.all('items')[1]['materials']))
        assert len(engine.log.all('items')) == 2 and engine.log.all('items')[0]['state'] == '完成'
    finally:
        engine.log.close()


def test_version_failure_is_recorded_once_and_does_not_block_check(world, tmp_path):
    io, target, _, source = world
    io.upstream = ValueError('source unavailable')
    engine, flow = engine_for(world, tmp_path)
    try:
        cursor, batch = ingest(engine, world)
        assert {e.object_key for e in batch.events} == {'check'}
        respond(engine, world)
        until(engine, lambda: bool(io.sent))
        assert 'source unavailable' in json.dumps(io.state(target)['checks']['check']['failures'])
        assert len(engine.log.all('items')) == 1
        assert engine.log.all('items')[0]['state'] != '卡住'
        io.time += timedelta(days=1)
        cursor, batch = ingest(engine, world, cursor)
        assert batch.events == []
    finally:
        engine.log.close()


def test_migration_retries_after_file_write_before_database_commit(world, tmp_path, monkeypatch):
    io, target, _, source = world
    engine, _ = engine_for(world, tmp_path)
    try:
        event = replace(next(e for e in source.poll(target, None).events if e.body['kind'] == 'version'),
                        flow='watch_updates', action='ready')
        engine.log.append('event', asdict(event), event.item_id)
        from workshop.core.log import Log
        original = Log.append
        def crash(log, kind, *args, **kwargs):
            if kind == 'alias_bound':
                raise OSError('interrupted')
            return original(log, kind, *args, **kwargs)
        monkeypatch.setattr(Log, 'append', crash)
        import pytest
        with pytest.raises(OSError, match='interrupted'):
            migrate(engine.log.path, target, io=io)
        assert not io.state(target).get('checks_migrated')
        assert engine.log.get('items', 'alpha:watch_updates:check') is None
        monkeypatch.setattr(Log, 'append', original)
        assert migrate(engine.log.path, target, io=io)['migrated'] == 1
        assert len(io.state(target)['checks']) == 1
        assert migrate(engine.log.path, target, io=io)['migrated'] == 0
    finally:
        engine.log.close()


def test_source_does_not_open_second_check_before_core_finishes(world, tmp_path):
    io, target, _, source = world
    engine, _ = engine_for(world, tmp_path)
    try:
        cursor, _ = ingest(engine, world, kinds={'version'})
        until(engine, lambda: bool(engine.active()))
        state = io.state(target)
        state['checks']['check']['closed'] = True  # 收尾钩子已落盘，核心尚未结算。
        io.save(target, state)
        target['watch_updates']['core_database'] = str(engine.log.path)
        io.time += timedelta(days=1)
        watchlist = io.watchlist(target)
        watchlist['software'].append(software('new'))
        write(io.file(target, 'watchlist'), watchlist)
        raw = source.poll(target, cursor)
        e = Event('watch_updates', 'completion-probe', 'alpha', 'check', '检查更新', {}, flow='watch_updates')
        engine.log.append('event', asdict(e), e.item_id)
        assert {e.object_key for e in source.normalize(raw, target).events} == {'check'}
        engine.log.append('item_changed', {'state': '完成'}, e.item_id)
        assert {e.object_key for e in source.normalize(raw, target).events} == {'check:2'}
    finally:
        engine.log.close()


def test_aggregate_deadline_reports_ready_entries_while_next_is_pending(world, tmp_path):
    io, target, _, _ = world
    value = io.watchlist(target)
    value['software'].append(software('slow'))
    write(io.file(target, 'watchlist'), value)
    engine, _ = engine_for(world, tmp_path)
    try:
        ingest(engine, world, kinds={'version'})
        io.time += timedelta(minutes=61)
        respond(engine, world)
        assert len(io.sent) == 1 and len(io.sent[0]['versions']) == 1
        assert io.sent[0]['blind']
        until(engine, lambda: bool(engine.active()))
        assert engine.active()[0]['item'] == 'alpha:watch_updates:check'
    finally:
        engine.log.close()


def test_startup_migration_failure_does_not_stop_other_work(tmp_path, monkeypatch, capsys):
    import workshop.cli as cli
    import workshop.running as running
    import workshop.plugins.watch_updates.migration as migration
    from workshop.core.log import Log
    config = configuration()
    config['targets']['alpha'].update(sources=[], watch_updates={'consolidate_on_start': True})
    engine = build(tmp_path, config)
    engine.ingest(Event('input', 'other-work', 'beta', 'other', '其他活', {}, action='ready'))
    registry = engine.plugins
    engine.log.close()
    monkeypatch.setattr(cli, 'Registry', lambda *args, **kwargs: registry)
    monkeypatch.setattr(running, 'record', lambda: None)
    def busy(*args, **kwargs):
        raise ValueError('旧活仍有未结算轮次')
    monkeypatch.setattr(migration, 'migrate', busy)
    assert cli.main(['--state-dir', str(tmp_path), 'run', '--once']) == 0
    assert '旧活仍有未结算轮次' in capsys.readouterr().out
    log = Log(tmp_path / 'workshop.db')
    try:
        assert log.get('items', 'beta:other')['state'] == '跑着'
    finally:
        log.close()
