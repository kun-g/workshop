"""历史轮次增长不增加一拍的存取；计数，不按机器速度判定。"""
import json
from contextlib import closing
from dataclasses import asdict
import sqlite3

import pytest

from conftest import build, current, event, item
from workshop.core.jobs import Jobs, encode, decode, execute, read, write
from workshop.core.log import Log
from workshop.core.models import Brief, Poll
from workshop.core.records import StoredBrief, defer_items
from workshop.core.report import why


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
