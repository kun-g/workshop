"""重启契约：真事实库、假时钟，经完成钩子和每拍入口核验。"""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from conftest import event
from workshop.core.engine import Engine
from workshop.core.jobs import Pending
from workshop.core.models import After


def request(engine, key, object_key='one'):
    engine.ingest(event(object_key, ext_id=key))
    item = engine.log.get('items', 'alpha:' + object_key)
    item['result'] = {'outcome': 'done', 'summary': key}
    with pytest.raises(Pending):
        engine.after(item, '执行', key, engine.item_target(item))
    return item


def callback(engine, fail=None):
    calls = []
    def after(item, stage, key, target):
        calls.append((item['id'], stage, key, deepcopy(target.get('runtime', {})), item.get('result')))
        if 'first_ok_at' in target.get('runtime', {}):
            if fail and item['object_key'] == fail:
                raise ValueError('重调失败')
            return After()
        return After(restart=True)
    engine.plugins.get('flow', 'flow').after = after
    return calls


def revived(engine):
    engine.clock.now += timedelta(seconds=1)
    return Engine(engine.log, engine.rules, engine.plugins, engine.clock, daemon={"pid": 4242, "start": "new"})


def test_restart_replays_original_hook_only_after_successful_first_beat(engine):
    calls = callback(engine)
    original = request(engine, 'original')
    assert engine.restart_requested
    engine.log.rebuild()
    new = revived(engine)
    new.tick()
    assert len(calls) == 2
    who, stage, key, runtime, result = calls[-1]
    assert (who, stage, key, result) == ('alpha:one', '执行', 'original', original['result'])
    heartbeat = json.loads(new.log.path.with_name('heartbeat.json').read_text())
    assert runtime == {'daemon': heartbeat['process'], 'first_ok_at': heartbeat['last_ok_at']}
    assert runtime['daemon']['start']
    assert calls[0][3] == {}
    assert not new.restart_requested
    assert not new.log.all('timers')
    assert new.wait_timeout() == 0
    # 结算原钩子和另一个普通调用都不携带重启字段。
    assert not new.after(original, '执行', 'original', new.item_target(original)).restart
    request(new, 'ordinary', 'two')
    assert calls[-1][3] == {}


def test_failed_first_beat_does_not_confirm_until_first_success(engine, monkeypatch):
    calls = callback(engine)
    request(engine, 'original')
    new = revived(engine)
    tick = new._tick
    monkeypatch.setattr(new, '_tick', lambda *args: (_ for _ in ()).throw(OSError('首拍失败')))
    new.tick()
    assert len(calls) == 1 and new.first_ok_at is None
    new.clock.now += timedelta(seconds=3)
    monkeypatch.setattr(new, '_tick', tick)
    new.tick()
    assert len(calls) == 2
    assert calls[-1][3]['first_ok_at'] == new.clock().isoformat()


def test_two_requests_survive_rebuild_and_both_resume_in_first_beat(engine):
    calls = callback(engine)
    request(engine, 'first')
    request(engine, 'second', 'two')
    engine.log.rebuild()
    new = revived(engine)
    new.tick()
    assert [call[2] for call in calls] == ['first', 'second', 'first', 'second']
    assert calls[2][3] == calls[3][3]
    assert all(row['done'] for row in new.log.all('restarts'))


def test_callback_error_is_not_confirmed_and_does_not_crash_heartbeat(engine):
    calls = callback(engine, fail='one')
    request(engine, 'first')
    new = revived(engine)
    new.tick()
    assert len(calls) == 2
    assert not new.log.get('restarts', 'first')['done']
    assert new.flow_broken('alpha')
    assert new.first_ok_at
    assert not new.restart_requested
    assert new.log.get('items', 'alpha:one')['state'] == '卡住'
