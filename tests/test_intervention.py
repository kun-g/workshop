"""持久操作、许可和跨进程协议的回归，不用墙钟 sleep。"""
from datetime import timedelta
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from workshop.core import waits
from workshop.core.models import After, Receipt
from workshop.core.rules import Rules
from workshop.plugins.intervention.client import Client, request
from workshop.plugins.intervention.service import Service
from conftest import event

RULES = Path(__file__).resolve().parents[1] / 'rules/workshop.yaml'


@pytest.fixture
def service(tmp_path):
    service = Service(tmp_path, RULES)
    yield service
    service.store.close()


def create(service, operation='create:1'):
    return service.call('create', {'operation': operation, 'point': 'decision', 'owner': 'alpha:one',
                                  'content': {'key': 'choice', 'version': '1', 'stage': '执行', 'question': '继续还是停止', 'reason': '需取舍', 'recommend': '停止'}})['ref']


def answer(service, ref, value='no', operation='answer:1'):
    return service.call('answer', {'operation': operation, 'ref': ref, 'value': value,
        'source': {'channel': 'session', 'quote': '停止', 'session': 'recorded-session', 'version': '1'}})


def permit(service, operation='permit:1', scope='idle:one'):
    return service.call('permit', {'operation': operation, 'point': 'manager_help', 'scope': scope,
        'owner': 'alpha:one', 'basis': {'action': '处理', 'reason': '原因', 'evidence': '事实号', 'status_location': '卡住'}})['ref']


def delivery(service):
    return service.call('enqueue', {'operation': 'enqueue:1', 'permit': permit(service), 'payload': {'text': '处理'}})['ref']


def test_committed_requests_survive_reopen_and_conflicts(tmp_path):
    s = Service(tmp_path, RULES)
    ref = create(s)
    token = answer(s, ref)['token']
    s.store.close()
    s = Service(tmp_path, RULES)
    assert create(s) == ref
    assert answer(s, ref)['token'] == token
    assert answer(s, ref, operation='answer:again')['token'] == token
    with pytest.raises(ValueError, match='同一操作号'):
        answer(s, ref, 'yes')
    with pytest.raises(ValueError, match='已有回答'):
        answer(s, ref, 'yes', 'answer:changed')
    for _ in range(2):
        s.call('ack', {'operation': 'ack:1', 'ref': ref, 'token': token})
    assert s.store.get(ref)['acknowledged']
    assert [r[0] for r in s.store.db.execute('SELECT kind FROM journal')] == ['created', 'answered', 'acknowledged']
    s.store.close()


def test_empty_answers_and_unregistered_messages_never_authorize(service):
    ref = create(service)
    for value in (None, ''):
        with pytest.raises(ValueError):
            answer(service, ref, value)
    assert service.call('status', {'ref': ref}) == {'ref': ref, 'ready': False, 'token': None}
    for point in ('progress', 'completed', 'decision', 'authorization'):
        with pytest.raises(ValueError):
            service.call('permit', {'operation': 'p:' + point, 'point': point})
    with pytest.raises(ValueError):
        service.call('ack', {'operation': 'ack', 'ref': ref, 'token': 'invented'})


def test_shared_idle_allowance_and_cancel(service):
    p = permit(service)
    assert permit(service, 'permit:second') == p
    assert permit(service, 'permit:next', 'idle:two') != p
    d = delivery(service)
    service.call('cancel', {'operation': 'cancel:1', 'ref': p, 'reason': '已解决'})
    assert service.call('dispatch', {'ref': d})['state'] == 'cancelled'
    with pytest.raises(ValueError):
        service.call('enqueue', {'operation': 'enqueue:again', 'permit': p, 'payload': {'text': '处理'}})


def test_delivery_uncertainty_reconciles_before_retry(service, monkeypatch):
    ref = delivery(service)
    calls = []
    results = [OSError('lost reply'), {'state': 'unknown'}, {'state': 'sent', 'external': 'receipt'}]
    def transport(config, action, record):
        calls.append(action)
        value = results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(service, 'transport', transport)
    assert service.dispatch(ref)['state'] == 'uncertain'
    assert service.dispatch(ref)['state'] == 'uncertain'
    assert service.dispatch(ref)['external'] == 'receipt'
    assert service.dispatch(ref)['external'] == 'receipt'
    assert calls == ['send', 'reconcile', 'reconcile']
    assert service.store.get(ref)['payload'] == {'text': '处理'}


def test_confirmed_absence_allows_same_key_retry(service, monkeypatch):
    ref = delivery(service)
    actions = []
    results = iter([{'state': 'unknown'}, {'state': 'absent'}, {'state': 'sent', 'external': 'receipt'}])
    def transport(config, action, record):
        actions.append((action, record['id']))
        return next(results)
    monkeypatch.setattr(service, 'transport', transport)
    service.dispatch(ref)
    service.dispatch(ref)
    assert actions == [('send', ref), ('reconcile', ref), ('send', ref)]


class LocalPort(Client):
    def __init__(self, service):
        self.service = service
        self.down = False
    def invoke(self, action, data, config):
        if self.down:
            raise ConnectionError('stopped')
        return self.service.call(action, data)


def setup_wait(engine, service):
    engine.rules = Rules({**engine.rules.data, 'wait_services': {'intervention': {'endpoint': '/unused'}}})
    port = LocalPort(service)
    engine.plugins.register('wait', 'intervention', port)
    engine.ingest(event())
    item = engine.log.get('items', 'alpha:one')
    ref = create(service)
    binding = {'provider': 'intervention', 'ref': ref, 'operation': 'bind:1'}
    with engine.log.transaction():
        changes = engine.pending_changes(item, After(pending=True, reason='等待', next='重查', wait=binding), {},
                                         {'key': 'after:test', 'receipt': {}, 'to': '说清楚', 'complete': False})
        engine.change(item, **changes)
    return port, ref, binding


def test_core_preserves_pause_on_outage_and_resumes_once_without_approval(engine, service):
    port, ref, binding = setup_wait(engine, service)
    port.down = True
    waits.reconcile(engine)
    item = engine.log.get('items', 'alpha:one')
    assert item['state'] == '等你' and item['stage'] == '接单'
    assert engine.waiting(item)
    engine.advance(item)
    assert not engine.active()
    answer(service, ref)  # 否也只是唤醒，不能被核心解释为批准。
    port.down = False
    engine.clock.now += timedelta(seconds=5)
    waits.reconcile(engine)
    item = engine.log.get('items', 'alpha:one')
    assert item['state'] == '空闲' and item['stage'] == '接单' and item['approved'] == []
    # core 在恢复后、确认前重建：暂停/恢复状态来自事实，不重复消费结果。
    engine.log.rebuild()
    waits.reconcile(engine)
    waits.reconcile(engine)
    assert service.store.get(ref)['acknowledged']
    assert len([f for f in engine.log.facts() if f['kind'] == 'wait_resumed']) == 1
    assert engine.log.get('waits', binding['operation'])['state'] == 'acknowledged'
    assert '停止' not in json.dumps(engine.log.all('waits'), ensure_ascii=False)
    with pytest.raises(ValueError):
        waits.bind(engine, engine.log.get('items', 'alpha:one'), binding)


def test_stale_result_does_not_restore_changed_stage(engine, service):
    _, ref, _ = setup_wait(engine, service)
    item = engine.log.get('items', 'alpha:one')
    engine.change(item, stage='执行', state='卡住')
    answer(service, ref)
    waits.reconcile(engine)
    assert engine.log.get('items', 'alpha:one')['stage'] == '执行'
    assert engine.log.get('items', 'alpha:one')['state'] == '卡住'
    assert engine.log.get('waits', 'bind:1')['state'] == 'superseded'


def test_full_round_wait_finishes_executor_then_rechecks_original_stage(engine, service):
    engine.rules = Rules({**engine.rules.data, 'wait_services': {'intervention': {'endpoint': '/unused'}}})
    engine.plugins.register('wait', 'intervention', LocalPort(service))
    engine.ingest(event())
    flow = engine.plugins.get('flow', 'flow')
    ref = create(service)
    def after(item, stage, key, target):
        if service.store.get(ref)['answer']:
            return After(blocked='回答为否', next='按回答调整')
        return After(pending=True, reason='等待', next='重查', wait={'provider': 'intervention', 'ref': ref, 'operation': 'bind:round'})
    flow.after = after
    engine.tick()
    row = engine.active()[0]
    engine.submit(row['token'], Receipt('done', '接单完成', advance='说清楚').json())
    engine.tick()
    item = engine.log.get('items', 'alpha:one')
    assert item['state'] == '等你' and item['stage'] == '接单'
    assert not engine.active()
    answer(service, ref)
    engine.clock.now += timedelta(seconds=5)
    engine.tick()
    item = engine.log.get('items', 'alpha:one')
    assert item['state'] == '卡住' and item['stage'] == '接单'
    assert item['blocked_by'] == '回答为否'


def test_real_socket_process_restarts_without_losing_wait():
    with tempfile.TemporaryDirectory(prefix='ws-wait-', dir='/tmp') as directory:
        root = Path(directory)
        endpoint = root / 'service.sock'
        def launch():
            reader, writer = os.pipe()
            process = subprocess.Popen([sys.executable, '-m', 'workshop.plugins.intervention',
                '--state-dir', str(root), '--rules', str(RULES), '--endpoint', str(endpoint),
                'serve', '--ready-fd', str(writer)], pass_fds=(writer,), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            os.close(writer)
            try:
                ready, _, _ = select.select([reader], [], [], 10)
                if not ready or os.read(reader, 64) != b'ready\n':
                    process.kill()
                    raise AssertionError(process.communicate(timeout=10))
            finally:
                os.close(reader)
            return process
        process = launch()
        data = {'operation': 'create', 'point': 'decision', 'owner': 'alpha:one', 'content': {'key': 'choice', 'version': '1', 'stage': '执行', 'question': '是否继续', 'reason': '需取舍', 'recommend': '停止'}}
        try:
            ref = request(endpoint, 'create', data)['ref']
            process.kill(); process.wait(timeout=10)
            process = launch()
            assert request(endpoint, 'create', data)['ref'] == ref
            assert request(endpoint, 'status', {'ref': ref})['ready'] is False
        finally:
            process.kill(); process.communicate(timeout=10)


@pytest.mark.parametrize('result', [None, {}, {'ref': 'wrong', 'ready': True, 'token': 'token'},
                                   {'ref': 'same', 'ready': True, 'token': ''}])
def test_invalid_or_empty_status_keeps_pause(engine, service, monkeypatch, result):
    port, ref, _ = setup_wait(engine, service)
    if result and result.get('ref') == 'same':
        result['ref'] = ref
    monkeypatch.setattr(port, 'poll', lambda *args: result)
    waits.reconcile(engine)
    assert engine.log.get('items', 'alpha:one')['state'] == '等你'
    assert not any(f['kind'] == 'wait_resumed' for f in engine.log.facts())


def test_ready_event_does_not_bypass_bound_wait(engine, service):
    setup_wait(engine, service)
    engine.ingest(event(ext_id='another-ready'))
    item = engine.log.get('items', 'alpha:one')
    assert item['state'] == '等你'
    engine.advance(item)
    assert not engine.active()


def test_background_pending_does_not_affect_other_work(engine, service, monkeypatch):
    from workshop.core.jobs import Pending
    setup_wait(engine, service)
    original = engine.call
    def call(port, *args, **kwargs):
        if port == 'wait':
            raise Pending('query')
        return original(port, *args, **kwargs)
    monkeypatch.setattr(engine, 'call', call)
    waits.reconcile(engine)
    engine.ingest(event(key='two', ext_id='two'))
    engine.advance(engine.log.get('items', 'alpha:two'))
    assert engine.active()[0]['item'] == 'alpha:two'
    assert engine.log.get('items', 'alpha:one')['state'] == '等你'


def test_file_change_wakes_wait_before_fallback_time(engine, service):
    from workshop.core.wake import Wake
    _, ref, _ = setup_wait(engine, service)
    waits.reconcile(engine)
    assert engine.log.get('waits', 'bind:1')['next_at'] > engine.clock.now.isoformat()
    answer(service, ref)
    changed = Path('/unused').with_suffix('.changed')
    engine.awaken(Wake(paths={changed}), engine.wake_paths())
    waits.reconcile(engine)
    assert engine.log.get('items', 'alpha:one')['state'] == '空闲'


def test_target_pause_holds_completed_signal_until_resume(engine, service):
    _, ref, _ = setup_wait(engine, service)
    answer(service, ref)
    engine.fact('control', {'id': 'alpha', 'paused': True})
    waits.reconcile(engine)
    assert engine.log.get('items', 'alpha:one')['state'] == '等你'
    engine.fact('control', {'id': 'alpha', 'paused': False})
    waits.reconcile(engine)
    assert engine.log.get('items', 'alpha:one')['state'] == '空闲'


def test_request_fields_cannot_override_stored_identity(service):
    with pytest.raises(ValueError, match='未知字段'):
        service.call('create', {'operation': 'inject', 'point': 'decision', 'owner': 'alpha:one',
                                'content': {'question': '继续'}, 'id': 'other'})
    assert service.store.all('wait') == []


def test_cancel_after_lost_send_still_records_confirmed_delivery(service, monkeypatch):
    ref = delivery(service)
    service.delivery_state(service.store.get(ref), 'sending')
    service.call('cancel', {'operation': 'cancel', 'ref': permit(service), 'reason': '已解决'})
    calls = []
    def transport(config, action, record):
        calls.append(action)
        return {'state': 'sent', 'external': 'already-sent'}
    monkeypatch.setattr(service, 'transport', transport)
    assert service.dispatch(ref)['external'] == 'already-sent'
    assert calls == ['reconcile']


def test_slow_dispatch_does_not_block_ping(service, tmp_path, monkeypatch):
    """一个连接卡在送达里，另一个连接的 ping 仍按自己的线程立即返回。"""
    from workshop.plugins.intervention.server import Handler, Server
    ref = delivery(service)
    entered, release = threading.Event(), threading.Event()
    def transport(config, action, record):
        entered.set()
        release.wait(10)
        return {'state': 'absent'}
    monkeypatch.setattr(service, 'transport', transport)
    # macOS 的 AF_UNIX 路径上限很短；socket 放短临时目录，状态库留在 tmp_path。
    with tempfile.TemporaryDirectory(prefix='ws-', dir='/tmp') as directory:
        endpoint = Path(directory) / 'threaded.sock'
        with Server(str(endpoint), Handler) as server:
            server.service, server.changed = service, endpoint.with_suffix('.changed')
            serving = threading.Thread(target=server.serve_forever, daemon=True)
            serving.start()
            dispatches = []
            dispatch = threading.Thread(target=lambda: dispatches.append(
                request(endpoint, 'dispatch', {'ref': ref}, timeout=10)), daemon=True)
            try:
                dispatch.start()
                assert entered.wait(5)
                began = time.monotonic()
                assert request(endpoint, 'ping', {}, timeout=2)['protocol'] == 1
                assert time.monotonic() - began < 1
                assert dispatch.is_alive()
                release.set()
                dispatch.join(10)
                assert not dispatch.is_alive()
                assert dispatches[0]['state'] == 'unsent'
            finally:
                release.set()
                server.shutdown()
                server.server_close()


def test_flush_stops_at_limit_and_time_budget(service, monkeypatch):
    from workshop.plugins.intervention import manager
    for n in range(12):
        allowed = service.call('permit', {'operation': f'permit:{n}', 'point': 'manager_help',
            'scope': f'idle:{n}', 'owner': 'alpha:one',
            'basis': {'action': '处理', 'reason': '原因', 'evidence': '事实号', 'status_location': '处理不了'}})['ref']
        service.call('enqueue', {'operation': f'enqueue:{n}', 'permit': allowed,
                                 'payload': {'kind': 'manager_help', 'members': [str(n)]}})
    sent = []
    def transport(config, action, record):
        sent.append(record['id'])
        return {'state': 'sent', 'external': 'm:' + record['id']}
    monkeypatch.setattr(service, 'transport', transport)
    first = manager.flush(service, limit=5)
    assert len(sent) == 5 and first['remaining'] == 7
    second = manager.flush(service, limit=5)
    assert len(sent) == 10 and second['remaining'] == 2
    third = manager.flush(service, limit=5)
    assert len(sent) == 12 and third['remaining'] == 0
    # 次数没到上限时，时间预算同样会让这一拍停下，剩下的下一拍再发。
    for n in range(12, 17):
        allowed = service.call('permit', {'operation': f'permit:{n}', 'point': 'manager_help',
            'scope': f'idle:{n}', 'owner': 'alpha:one',
            'basis': {'action': '处理', 'reason': '原因', 'evidence': '事实号', 'status_location': '处理不了'}})['ref']
        service.call('enqueue', {'operation': f'enqueue:{n}', 'permit': allowed,
                                 'payload': {'kind': 'manager_help', 'members': [str(n)]}})
    now = [0.0]
    def clock():
        return now[0]
    def dispatch(ref):
        now[0] += 8
        return {'id': ref, 'state': 'sent'}
    monkeypatch.setattr(service, 'dispatch', dispatch)
    budget = manager.flush(service, limit=10, seconds=20, clock=clock)
    assert len(budget['deliveries']) == 3 and budget['remaining'] == 2
