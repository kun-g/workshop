"""历史轮次增长不增加一拍的存取；计数，不按机器速度判定。"""
import json
from contextlib import closing
from dataclasses import asdict, replace
import sqlite3

import pytest

from conftest import build, current, event, item
from workshop.core.jobs import Jobs, encode, decode, execute, read, write
from workshop.core.log import Log
from workshop.core.models import Brief, Poll
from workshop.core.records import StoredBrief, defer_items
from workshop.reports.report import why


class Reads:
    """在 SQLite 返回行的位置数实际载荷，不只数 JSON 解析。"""
    def __init__(self, db):
        self.db = db
        self.queries = 0
        self.bytes = 0

    def execute(self, sql, args=()):
        self.queries += 1
        cursor = self.db.execute(sql, args)
        owner = self

        class Cursor:
            def count(self, row):
                if row is not None:
                    owner.bytes += sum(len(v.encode()) if isinstance(v, str) else len(v) if isinstance(v, bytes)
                                       else 8 if v is not None else 0 for v in row)
                return row

            def fetchone(self):
                return self.count(cursor.fetchone())

            def fetchall(self):
                return [self.count(row) for row in cursor.fetchall()]

            def __iter__(self):
                for row in cursor:
                    yield self.count(row)

            def __getattr__(self, name):
                return getattr(cursor, name)
        return Cursor()

    def __getattr__(self, name):
        return getattr(self.db, name)


def test_recent_unavailable_does_not_read_finished_history(engine):
    engine.ingest(event())
    measurements = []
    for start, end in [(0, 100), (100, 2000)]:
        with engine.log.transaction():
            for i in range(start, end):
                row = dict(token=f'past-{i}', item='alpha:one', stage='接单', status='ended',
                           outcome='done', ended='2020-01-01T00:00:00Z', executor='writer',
                           brief={'instructions': '旧派活说明' * 100})
                engine.log.db.execute('INSERT INTO rounds(id,body) VALUES (?,?)',
                                      (row['token'], json.dumps(row, ensure_ascii=False)))
        original = engine.log.db
        reads = Reads(original)
        engine.log.db = reads
        try:
            assert engine.recently_unavailable(item(engine)) == []
        finally:
            engine.log.db = original
        measurements.append((reads.queries, reads.bytes))
    assert measurements[1] == measurements[0], measurements


class LocalJobs(Jobs):
    """真实作业登记、编码和结果消费，只把进程启动换成显式执行。"""
    def start(self, path, spec):
        path.mkdir(parents=True, exist_ok=True)
        write(path / 'spec.json', spec)
        self.spawned += 1

    def complete(self, plugins, skip=()):
        for specfile in self.directory.glob('*/spec.json'):
            spec = read(specfile)
            if spec['method'] not in skip and not (specfile.parent / 'result.json').exists():
                execute(specfile.parent, plugins)


def seed_history(engine, count):
    # 大说明先写进旧格式事实，走正式投影；同一活及别的活各一半。
    engine.ingest(event('other', ext_id='other'))
    brief = {'instructions': '中文说明' * 1000, 'materials': [{'body': '资料'}],
             'answers': [], 'workdir': None, 'readonly': False}
    with engine.log.transaction():
        for i in range(count):
            target = 'alpha:one' if i % 2 else 'alpha:other'
            row = dict(token=f'history-{i:06d}', item=target, stage='接单', status='ended',
                       outcome='done', ended='2020-01-01T00:00:00+00:00', executor='writer', brief=brief)
            engine.fact('round_reserved', row, target)
            engine.fact('round_result', dict(token=row['token'], stage='接单', outcome='done',
                                            summary='已完成', evidence=['证据' * 10]), target)
        for target in ['alpha:one', 'alpha:other']:
            engine.change(engine.log.get('items', target), state='空闲' if target == 'alpha:one' else '等你', round=None)


@pytest.mark.parametrize('mode', ['idle', 'selecting', 'running', 'starting', 'unavailable'])
def test_tick_reads_do_not_grow_with_ended_rounds(tmp_path, monkeypatch, mode):
    samples = []
    for count in (100, 2000):
        root = tmp_path / f'{count:04d}'
        engine = build(root)
        engine.ingest(event())
        seed_history(engine, count)
        if mode == 'idle':
            engine.change(item(engine), state='等你')
        if mode == 'running':
            engine.tick()
        if mode == 'unavailable':
            # 最近确有一次 unavailable；再多的旧轮不改变本次选择。
            engine.fact('round_reserved', dict(token='last-failed', item='alpha:one', stage='接单',
                        status='ended', outcome='unavailable', ended=engine.clock().isoformat(),
                        executor='removed-profile', brief=asdict(Brief('最近失败', []))), 'alpha:one')
            engine.change(item(engine), state='空闲', round=None)
        jobs = LocalJobs(root / 'jobs', ['never-started'])
        engine.jobs = jobs
        try:
            skip = ('launch', 'resume') if mode == 'starting' else ('select',)
            for _ in range(8):
                engine.tick()
                jobs.complete(engine.plugins, skip=skip)
            if mode in ('selecting', 'unavailable'):
                assert any(read(p)['method'] == 'select' for p in jobs.directory.glob('*/spec.json'))
            elif mode in ('running', 'starting'):
                assert engine.active()[0]['status'] == ('running' if mode == 'running' else 'starting')
            original = engine.log.db
            reads = Reads(original)
            steps = []
            original.set_progress_handler(lambda: steps.append(1) or 0, 100)
            engine.log.db = reads
            try:
                with monkeypatch.context() as patch:
                    # 不能另开连接把大读取藏到计数器之外；后台 complete 在两拍之间执行。
                    patch.setattr(sqlite3, 'connect', lambda *a, **k: pytest.fail('tick opened another database'))
                    engine.tick()
            finally:
                engine.log.db = original
                original.set_progress_handler(None, 0)
            assert engine.control('@tick').get('failures', 0) == 0
            samples.append((reads.queries, reads.bytes, len(steps)))
        finally:
            jobs.launcher.shutdown(wait=True)
            engine.log.close()
    print(f'{mode}: 100/2000 rounds (queries, bytes, VM/100) = {samples}')
    assert samples[0][0] == samples[1][0], samples
    # 轮数、修订号、事实编号的十进制位数会变；只允许这些标量的固定小量变化。
    assert abs(samples[1][1] - samples[0][1]) <= 128, samples
    # B-tree 深度可能随页数变动，容许固定小量寻址开销，扫描两千行会超过此界。
    assert samples[1][2] <= samples[0][2] + 20, samples


def test_round_filters_really_use_indexes(engine):
    engine.ingest(event())
    engine.tick()
    plans = []
    original = engine.log.db

    class Plans(Reads):
        def execute(self, sql, args=()):
            if sql.startswith('SELECT body FROM rounds'):
                plans.extend(row[3] for row in original.execute('EXPLAIN QUERY PLAN ' + sql, args))
            return super().execute(sql, args)

    engine.log.db = Plans(original)
    try:
        engine.log.all('rounds', where=('item', 'alpha:one'))
        engine.log.all('rounds', where=('status', 'running'))
        engine.log.all('rounds', where=('item', 'alpha:one'), unless=('status', 'ended'))
        engine.active()
    finally:
        engine.log.db = original
    assert any('rounds_item' in p for p in plans)
    assert any('rounds_status' in p for p in plans)
    assert any('rounds_active' in p for p in plans)
    assert any('rounds_active_item' in p for p in plans)
    assert not any('SCAN rounds' in p or 'TEMP B-TREE' in p for p in plans), plans


def test_brief_and_history_references_survive_job_encoding_and_rebuild(engine, tmp_path):
    engine.ingest(event())
    engine.tick()
    full = current(engine)
    row = engine.log.get('rounds', full['token'])
    conclusion = dict(token=row['token'], stage='接单', outcome='done', summary='完整结论', evidence=['原样输出'])
    engine.fact('round_result', conclusion, 'alpha:one')
    assert 'brief' not in row
    assert len(json.dumps(row)) < len(json.dumps(full))
    before = why(engine.log, 'alpha:one', raw=True)
    ref = StoredBrief(str(engine.log.path), row['brief_ref'])
    history = engine.log.history('alpha:one')
    assert history.count == 1
    assert decode(encode(ref)).load() == Brief(**full['brief'])
    assert decode(encode(history)).load()[0]['brief'] == full['brief']
    engine.log.rebuild()
    assert why(engine.log, 'alpha:one', raw=True) == before
    assert ref.load() == Brief(**full['brief'])
    assert engine.log.history('alpha:one').count == 1
    # 真正的后台入口收到普通 Brief / list，而非新的引用类型。
    from types import SimpleNamespace
    seen = []
    plugin = SimpleNamespace(select=lambda *args: seen.append(args) or None)
    registry = SimpleNamespace(get=lambda *args: plugin)
    directory = tmp_path / 'background'
    directory.mkdir()
    write(directory / 'spec.json', {'port': 'selector', 'name': 'test', 'method': 'select',
                                    'args': encode([ref, history, *defer_items(
                                        [engine.log.get('items', 'alpha:one')], engine.log.path)])})
    execute(directory, registry)
    assert read(directory / 'result.json')['ok'] is None
    assert isinstance(seen[0][0], Brief) and isinstance(seen[0][1], list)
    assert seen[0][1][0]['token'] == full['token']
    assert seen[0][2]['history'] == [conclusion]


def test_legacy_database_migrates_without_rewriting_facts_and_replays(engine, tmp_path):
    engine.ingest(event())
    engine.tick()
    row = current(engine)
    conclusion = dict(token=row['token'], stage='接单', outcome='done', summary='旧库完整结论', evidence=['旧输出'])
    engine.fact('round_result', conclusion, 'alpha:one')
    # 按旧格式建立独立库，旧轮次和旧事实都内嵌完整说明。
    path = tmp_path / 'legacy.db'
    with closing(sqlite3.connect(path)) as db:
        from workshop.core.view import TABLES
        for table in TABLES:
            db.execute(f'CREATE TABLE {table}(id TEXT PRIMARY KEY, body TEXT NOT NULL)')
            for key, raw in engine.log.db.execute(f'SELECT id,body FROM {table}'):
                value = json.loads(raw)
                if table == 'rounds':
                    value.pop('brief_ref', None)
                    value.pop('workdir', None)
                    value['brief'] = row['brief']
                if table == 'items':
                    value = engine.log.full_item(value)
                    value.pop('history_ref', None)
                db.execute(f'INSERT INTO {table} VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))
        db.execute('CREATE TABLE facts(seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, '
                   'ext_id TEXT NOT NULL, item TEXT, body TEXT NOT NULL, at TEXT NOT NULL, UNIQUE(source,ext_id))')
        for fact in engine.log.facts():
            if fact['kind'] == 'round_reserved':
                fact['body'].pop('brief_ref', None)
                fact['body'].pop('workdir', None)
                fact['body']['brief'] = row['brief']
            db.execute('INSERT INTO facts VALUES (?,?,?,?,?,?,?)',
                       tuple(fact[k] if k != 'body' else json.dumps(fact[k], ensure_ascii=False)
                             for k in ['seq', 'kind', 'source', 'ext_id', 'item', 'body', 'at']))
        db.commit()
        original = db.execute('SELECT * FROM facts').fetchall()
    with closing(Log(path)) as log:
        assert 'brief' not in log.get('rounds', row['token'])
        assert log.brief(log.get('rounds', row['token'])) == row['brief']
        assert log.full_item(log.get('items', 'alpha:one'))['history'] == [conclusion]
        assert [tuple(r) for r in log.db.execute('SELECT * FROM facts')] == original
        log.rebuild()
        assert log.brief(log.get('rounds', row['token'])) == row['brief']
        assert log.full_item(log.get('items', 'alpha:one'))['history'] == [conclusion]
        assert [tuple(r) for r in log.db.execute('SELECT * FROM facts')] == original
    with closing(Log(path)) as log:
        assert log.history('alpha:one').load()[0]['brief'] == row['brief']
        assert log.history('alpha:one').count == 1


def test_equal_cursor_does_not_append_a_snapshot_and_changes_replay(engine):
    source = engine.plugins.get('source', 'source')
    engine.sources()
    before = [f for f in engine.log.facts() if f['kind'] == 'cursor']
    engine.sources()
    assert [f for f in engine.log.facts() if f['kind'] == 'cursor'] == before
    source.poll = lambda target, cursor: Poll([event()], 'changed')
    engine.sources()
    assert engine.log.get('cursors', 'alpha:source')['cursor'] == 'changed'
    assert item(engine) is not None
    engine.log.rebuild()
    assert engine.log.get('cursors', 'alpha:source')['cursor'] == 'changed'
    assert len([f for f in engine.log.facts() if f['kind'] == 'cursor']) == len(before) + 1


def test_copy_measurement_leaves_source_unchanged_and_starts_no_process(engine, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    import subprocess
    engine.ingest(event())
    engine.tick()
    before = engine.log.facts()
    spec = importlib.util.spec_from_file_location('measure_tick_copy',
                                                Path(__file__).parents[1] / 'scripts/measure_tick_copy.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(subprocess, 'check_output', lambda *args, **kwargs: 'a' * 40 + '\n')
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('must not start a process'))
    output = tmp_path / 'measurement'
    result = module.measure(engine.log.path, output)
    assert isinstance(result['tick_ms'], int) and result['pending_jobs'] > 0
    assert result['inline_briefs_after'] == 0
    assert engine.log.facts() == before
    assert (output / 'heartbeat.json').is_file()
    with pytest.raises(FileExistsError):
        module.measure(engine.log.path, output)


@pytest.mark.parametrize('home', [False, True])
def test_tick_reads_do_not_grow_with_abandoned_material_payload(tmp_path, home):
    measurements = []
    for size in (100, 100_000):
        engine = build(tmp_path / str(size))
        for n in range(20):
            engine.ingest(replace(event(ext_id=str(n)), body={'text': 'x' * size}))
        # 真实故障的最大资料来自已放弃的活；没有工单插件时不应再读它们。
        if not home:
            engine.rules.data.pop('taskhome', None)
        engine.change(item(engine), state='放弃', blocked_by='fixture', ticket='ticket:fixture')
        if home:
            engine.taskhome(item(engine))
        jobs = LocalJobs(engine.log.path.parent / 'jobs', ['never-started'])
        engine.jobs = jobs
        original = engine.log.db
        reads = Reads(original)
        engine.log.db = reads
        try:
            engine.tick()
            assert engine.control('@tick').get('failures', 0) == 0
            engine.wait_timeout()
            measurements.append(reads.bytes)
        finally:
            engine.log.db = original
            jobs.launcher.shutdown(wait=True)
            engine.log.close()
    print(f'material bytes 100/100000: tick read bytes = {measurements}')
    assert abs(measurements[1] - measurements[0]) <= 128, measurements


def test_tick_and_wake_skip_completed_effects_and_timers(tmp_path):
    samples = []
    for count in (100, 2000):
        engine = build(tmp_path / str(count))
        engine.ingest(event())
        engine.change(item(engine), state='卡住', blocked_by='fixture')
        with engine.log.transaction():
            for n in range(count):
                engine.fact('effect', {'id': f'past-{n}', 'done': True})
                engine.fact('timer_created', {'id': f'past-{n}', 'fired': True})
        jobs = LocalJobs(engine.log.path.parent / 'jobs', ['never-started'])
        engine.jobs = jobs
        original = engine.log.db
        reads, steps, plans = Reads(original), [], []
        engine.log.db = reads
        original.set_progress_handler(lambda: steps.append(1) and 0, 100)
        try:
            engine.tick()
            engine.wait_timeout()
            assert engine.control('@tick').get('failures', 0) == 0
            samples.append((reads.bytes, len(steps)))
            for table, field in [('effects', 'done'), ('timers', 'fired')]:
                plans.extend(r[3] for r in original.execute(
                    f"EXPLAIN QUERY PLAN SELECT body FROM {table} "
                    f"WHERE (json_extract(body, '$.{field}') IS NOT 1) = 1 ORDER BY rowid"))
            assert all('_pending' in plan for plan in plans), plans
        finally:
            original.set_progress_handler(None, 0)
            engine.log.db = original
            jobs.launcher.shutdown(wait=True)
            engine.log.close()
    assert abs(samples[1][0] - samples[0][0]) <= 128, samples
    assert samples[1][1] <= samples[0][1] + 20, samples


def test_heartbeat_retains_restart_endpoints(engine, monkeypatch):
    from datetime import timedelta
    from workshop.core.engine import Engine
    monkeypatch.setenv('WORKSHOP_RUNNING_COMMIT', 'a' * 40)
    engine.heartbeat()
    path = engine.log.path.parent / 'heartbeat.json'
    engine.clock.now += timedelta(milliseconds=100)
    engine.heartbeat()
    old = json.loads(path.read_text())
    monkeypatch.setenv('WORKSHOP_RUNNING_COMMIT', 'b' * 40)
    restarted = Engine(engine.log, engine.rules, engine.plugins, engine.clock)
    engine.clock.now += timedelta(milliseconds=300)
    restarted.heartbeat()
    first = json.loads(path.read_text())
    engine.clock.now += timedelta(seconds=1)
    restarted.heartbeat()
    later = json.loads(path.read_text())
    assert first['process'] == later['process']
    assert first['process']['first_at'] == first['at']
    assert first['process']['previous']['at'] == old['at']
    assert first['process']['previous']['version'] == 'a' * 40
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('measure_release_gap',
        Path(__file__).parents[1] / 'scripts/measure_release_gap.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.gap(old, later)
    assert result['passed'] and result['gap_seconds'] == 0.3
    assert module.gap(first, later) is None  # 采样开始时已经换过代，不能冒充观察到重启。
    later['process']['previous']['version'] = None
    assert module.gap(old, later)['passed'] is False


def test_ended_items_sync_home_and_rework_remains_runnable(engine):
    engine.ingest(event())
    engine.change(item(engine), state='完成', ticket='ticket:fixture')
    assert engine.log.open_items() == []
    home = engine.plugins.get('taskhome', 'home')
    engine.tick()
    assert home.updates[-1][2]['state'] == '完成'
    count = len(home.updates)
    engine.tick()
    assert len(home.updates) == count
    engine.ingest(event(ext_id='rework', action='rework'))
    assert [i['id'] for i in engine.log.open_items()] == ['alpha:one']
    engine.tick()
    assert item(engine)['state'] != '完成'


def test_pending_item_queries_use_partial_indexes(engine):
    from workshop.core.log import OPEN_ITEMS, RELEASE_ITEMS
    for name, expression in [('open', OPEN_ITEMS), ('release', RELEASE_ITEMS),
                             ('program', "(json_extract(body, '$.program') IS NOT NULL)")]:
        plans = [r[3] for r in engine.log.db.execute(
            f'EXPLAIN QUERY PLAN SELECT body FROM items WHERE {expression} = 1 ORDER BY rowid')]
        assert all(f'items_{name}' in p for p in plans), plans


def test_failed_heartbeat_write_does_not_record_a_first_heartbeat(engine, monkeypatch):
    from pathlib import Path
    from datetime import timedelta
    original = Path.replace
    def fail(path, target):
        raise OSError('fixture write failed')
    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError, match='fixture write failed'):
        engine.heartbeat()
    assert not hasattr(engine, 'heartbeat_process')
    engine.clock.now += timedelta(seconds=2)
    monkeypatch.setattr(Path, 'replace', original)
    engine.heartbeat()
    heartbeat = json.loads((engine.log.path.parent / 'heartbeat.json').read_text())
    assert heartbeat['process']['first_at'] == heartbeat['at']


def test_idle_blocked_item_is_not_reloaded_without_pending_work(engine, monkeypatch):
    engine.ingest(event())
    engine.change(item(engine), state='卡住', blocked_by='fixture')
    engine.rules.data.pop('taskhome', None)
    current = item(engine)
    original = engine.log.get
    reads = []
    def get(table, key):
        if table == 'items':
            reads.append(key)
        return original(table, key)
    monkeypatch.setattr(engine.log, 'get', get)
    engine.advance(current)
    assert reads == []


def test_fast_tick_does_not_write_a_profile(engine):
    engine.tick()
    assert not (engine.log.path.parent / 'tick-slow.jsonl').exists()


def test_slow_tick_records_phase_times(engine):
    import time
    from workshop.core import beat
    beat.one(engine, time.monotonic() - 0.2)
    path = engine.log.path.parent / 'tick-slow.jsonl'
    row = json.loads(path.read_text().splitlines()[-1])
    assert row['tick_ms'] >= 100
    assert not any(name.startswith('channels') for name in row['spans'])  # 通道已退役（#176）。
    for name in ('lock', 'reap', 'sources', 'classify', 'timers', 'waits', 'rounds', 'advance', 'release', 'heartbeat'):
        assert name in row['spans']
        assert isinstance(row['spans'][name], (int, float))
    assert row['slow_items'] == []
    assert isinstance(row['open'], int) and isinstance(row['active'], int)


def test_slow_tick_names_the_slow_item(engine, monkeypatch):
    import time
    from workshop.core import beat
    engine.ingest(event())
    offset = {'v': 0.0}
    real = time.monotonic
    monkeypatch.setattr(time, 'monotonic', lambda: real() + offset['v'])
    original = beat.advance

    def slow(item):
        offset['v'] += 0.02
        return original(engine, item)

    monkeypatch.setattr(engine, 'advance', slow)
    beat.one(engine, time.monotonic() - 0.15)
    row = json.loads((engine.log.path.parent / 'tick-slow.jsonl').read_text().splitlines()[-1])
    assert row['slow_items'][0]['id'] == 'alpha:one'
    assert row['slow_items'][0]['ms'] >= 15


def test_slow_tick_log_keeps_the_tail(tmp_path):
    from workshop.core.beat import append_slow_tick
    path = tmp_path / 'tick-slow.jsonl'
    for n in range(10):
        append_slow_tick(path, {'n': n}, keep=3, limit=1)
    assert [json.loads(line)['n'] for line in path.read_text().splitlines()] == [7, 8, 9]
