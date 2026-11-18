"""只读打开、读事务和事实查询的行为边界。"""
import sqlite3

import pytest

from workshop.core.log import Log
from conftest import event


def test_readonly_existing_database_during_write(engine):
    engine.ingest(event())
    before = engine.log.path.read_bytes()
    with engine.log.transaction():
        engine.log.append('control', {'id': 'uncommitted', 'value': 1})
        reader = Log.readonly(engine.log.path)
        try:
            with reader.read_transaction():
                assert reader.item_count() == 1
                assert reader.get('controls', 'uncommitted') is None
                with pytest.raises(sqlite3.OperationalError, match='readonly'):
                    reader.append('control', {'id': 'bad'})
        finally:
            reader.close()
        assert engine.log.path.read_bytes() == before


def test_readonly_missing_and_old_database_are_not_created_or_upgraded(tmp_path):
    missing = tmp_path / 'absent' / 'workshop.db'
    with pytest.raises(sqlite3.OperationalError):
        Log.readonly(missing)
    assert not missing.parent.exists()
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE original(value)')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='先启动守护进程'):
        Log.readonly(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [('original',)]


def test_read_transaction_keeps_snapshot_until_query_finishes(engine):
    engine.ingest(event())
    reader = Log.readonly(engine.log.path)
    try:
        with reader.read_transaction():
            assert reader.get('controls', 'new') is None
            engine.log.append('control', {'id': 'new', 'value': 7})
            with reader.read_transaction():
                assert reader.get('controls', 'new') is None
            assert reader.get('controls', 'new') is None
        with reader.read_transaction():
            assert reader.get('controls', 'new')['value'] == 7
        with pytest.raises(RuntimeError):
            with reader.read_transaction():
                raise RuntimeError('query failed')
        assert not reader.db.in_transaction
    finally:
        reader.close()


def test_fact_queries_preserve_nested_values_and_select_only_requested_fields(engine):
    engine.ingest(event())
    item = engine.log.all('items')[0]
    engine.change(item, state='卡住')
    reader = Log.readonly(engine.log.path)
    try:
        with reader.read_transaction():
            assert reader.item_count() == 1
            assert reader.state_since(item['id']) == engine.clock().isoformat()
            assert reader.state_since('missing') is None
            facts = reader.selected_facts(('event',), ('target', 'body', 'missing'))
            assert len(facts) == 1
            found = facts[0]
            assert reader.event_at(found['seq'])['body'] == found['body']['body']
            assert set(found['body']) == {'target', 'body'}
            assert reader.event_at(0) is None
            assert reader.selected_facts(('event',), ('target',), after=found['seq']) == []
            assert reader.all('items', fields=('id', 'title')) == [{'id': item['id'], 'title': item['title']}]
    finally:
        reader.close()


@pytest.mark.parametrize('command', [('status',), ('status', '--all'), ('status', '--html'),
                                     ('why', 'alpha:one'), ('usage',)])
def test_read_commands_succeed_while_writer_holds_transaction(engine, command, capsys):
    from workshop.cli import main
    engine.ingest(event())
    before = engine.log.path.read_bytes()
    with engine.log.transaction():
        engine.log.append('control', {'id': 'uncommitted', 'value': 1})
        assert main(['--state-dir', str(engine.log.path.parent), *command]) == 0
        assert not capsys.readouterr().err
    assert engine.log.path.read_bytes() == before


@pytest.mark.parametrize('command', [('status',), ('why', 'a:1'), ('usage',)])
def test_read_commands_do_not_create_missing_database(tmp_path, command, capsys):
    from workshop.cli import main
    path = tmp_path / 'absent'
    assert main(['--state-dir', str(path), *command]) == 2
    assert not path.exists()
    assert '车间：' in capsys.readouterr().err
