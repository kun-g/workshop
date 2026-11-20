"""瞬时故障：标坏后按小时自动重试，成功自动解除并恢复被卡的活（核心 R14、R20）。"""
from datetime import datetime, timedelta

import pytest

from conftest import build, configuration, event, item
from workshop.core import faults
from workshop.core.managers import Store, observe_failures, observe_items
from workshop.core.ports import Retryable, WaitUnavailable
from workshop.github_identity import IdentityError
from workshop.reports.manager_status import sections
from workshop.reports.report import status


TRANSIENT = [Retryable('签发暂时不通'), WaitUnavailable('介入点服务一时连不上'),
             IdentityError('kun-g/workshop App 签发失败')]


def failing_flow(engine, error, failures):
    flow = engine.plugins.get('flow', 'flow')
    calls = {'left': failures}

    def brief(*args):
        if calls['left']:
            calls['left'] -= 1
            raise error
        return '完成这一阶段'

    flow.brief = brief
    return calls


def failed_three_times(engine, error):
    failing_flow(engine, error, failures=3)
    engine.ingest(event())
    engine.tick()                      # 第一次：退避 5 分钟
    engine.clock.now += timedelta(minutes=5)
    engine.tick()                      # 第二次：退避 10 分钟
    engine.clock.now += timedelta(minutes=10)
    engine.tick()                      # 第三次：升级隔离，下一次一小时后
    return engine.control('alpha')


@pytest.mark.parametrize('error', TRANSIENT)
def test_transient_failure_isolates_then_retries_hourly_and_recovers(engine, error):
    control = failed_three_times(engine, error)
    blocked = item(engine)
    assert control['broken'] and control['auto_retry']
    assert control['retry_at'] == engine.log.get('controls', 'retry:start:alpha:flow')['at']
    assert blocked['state'] == '卡住' and blocked['plugin_blocked']
    assert blocked['plugin_retry_at'] == control['retry_at']
    assert blocked['leave']['kind'] == '工序' and blocked['leave']['owner'] == '车间'
    assert '自动重试' in blocked['next']

    # 到点这一拍先解除隔离、恢复原状，再按原路径重试；这次签发/调用通了。
    engine.clock.now = datetime.fromisoformat(control['retry_at'])
    engine.tick()
    assert not engine.control('alpha').get('broken')
    recovered = item(engine)
    assert not recovered.get('plugin_blocked') and recovered['state'] == '跑着'


def test_code_error_still_isolates_without_auto_retry(engine):
    failing_flow(engine, TypeError('bad plugin code'), failures=3)
    engine.ingest(event())
    engine.tick()
    control = engine.control('alpha')
    assert control['broken'] and not control['auto_retry']
    assert item(engine)['state'] == '卡住' and item(engine)['plugin_blocked']
    engine.clock.now += timedelta(hours=3)
    engine.tick()
    assert engine.control('alpha')['broken'] and item(engine)['plugin_blocked']


def test_transient_failure_does_not_downgrade_permanent_isolation(engine):
    engine.ingest(event())
    engine.tick()
    engine.broken('alpha', 'flow', 'flow', TypeError('bad plugin code'))
    for _ in range(3):
        engine.retry_failure('alpha', 'flow', 'flow', 'start:alpha:flow', Retryable('暂时不通'))
    control = engine.control('alpha')
    assert control['broken'] and not control['auto_retry']
    assert item(engine)['state'] == '卡住' and item(engine)['plugin_blocked']
    assert not item(engine).get('plugin_retry_at')


def test_retry_success_clears_an_existing_isolation(engine):
    engine.ingest(event())
    engine.tick()
    at = (engine.clock() + timedelta(hours=1)).isoformat()
    engine.broken('alpha', 'flow', 'flow', Retryable('暂时不通'),
                  retry={'at': at, 'count': 3, 'key': 'source:alpha:source'})
    assert engine.control('alpha')['broken'] and item(engine)['plugin_blocked']
    engine.retry_clear('source:alpha:source')
    assert not engine.control('alpha')['broken']
    assert not item(engine).get('plugin_blocked')


def test_transient_flow_failure_blocks_only_the_affected_flow(tmp_path):
    config = configuration()
    config['targets']['alpha'] = {'sources': [], 'plugin': 'flow', 'default_flow': 'flow',
        'flows': {'flow': {'triggers': []}, 'watch': {'plugin': 'other', 'triggers': []}}}
    engine = build(tmp_path / 'state', config)
    try:
        failing_flow(engine, Retryable('暂时不通'), failures=3)
        engine.ingest(event(), directed=True)
        for advance in (0, 5, 10):
            engine.clock.now += timedelta(minutes=advance)
            engine.tick()
        control = engine.control('@flow:alpha:flow')
        assert control['broken'] and control['auto_retry']
        assert not engine.control('alpha').get('broken')
        assert engine.flow_broken('alpha', 'flow') and not engine.flow_broken('alpha', 'watch')
    finally:
        engine.log.close()


def multi_flow_config():
    config = configuration()
    config['targets']['alpha'] = {'sources': [], 'plugin': 'flow', 'default_flow': 'flow',
        'flows': {'flow': {'triggers': []}, 'other': {'plugin': 'other', 'triggers': []}}}
    return config


def test_items_arriving_during_auto_retry_join_that_isolation(tmp_path):
    engine = build(tmp_path / 'state', multi_flow_config())
    try:
        # 一件已完成的活，隔离期间会收到 update。
        engine.ingest(event(key='done', ext_id='done'), directed=True)
        engine.change(item(engine, key='done'), state='完成', stage='收尾',
                      next='', blocked_by='', leave=None)
        # 另一流程永久隔离，和自动重试的流程隔离同时在。
        engine.broken('alpha', 'flow', 'other', RuntimeError('代码错误'))
        at = (engine.clock() + timedelta(hours=1)).isoformat()
        engine.broken('alpha', 'flow', 'flow', Retryable('暂时不通'),
                      retry={'at': at, 'count': 3, 'key': 'start:alpha:flow'})
        # 隔离期间新进来一件 ready，另一件已完成的活收到 update。
        engine.ingest(event(key='new', ext_id='new'), directed=True)
        engine.ingest(event(key='done', ext_id='done-update', action='update'), directed=True)
        for key in ('new', 'done'):
            row = item(engine, key=key)
            assert row['state'] == '卡住' and row['plugin_blocked']
            assert row['plugin_failure_key'] == '@flow:alpha:flow'
            assert row['plugin_retry_at'] == at
            assert row['leave']['owner'] == '车间'
        store = Store(tmp_path / 'managers')
        try:
            now = engine.clock().isoformat()
            observe_items(store, engine.log.open_items(), 'core', now)
            observe_failures(store, engine.log, 'core', now, engine.rules)
            concerns = {row['id'] for row in store.all()}
        finally:
            store.close()
        # 自动重试的隔离不登记事项，代码错误照旧登记。
        assert 'item:alpha:new' not in concerns and 'item:alpha:done' not in concerns
        assert 'fault:@flow:alpha:flow' not in concerns
        assert 'fault:@flow:alpha:other' in concerns
        # 到点只解除这一段；另一段还坏着，两件活也回到隔离前的状态。
        engine.clock.now = datetime.fromisoformat(at)
        assert faults.wake_broken(engine) is True
        assert engine.control('@flow:alpha:other')['broken']
        assert not engine.control('@flow:alpha:flow').get('broken')
        assert item(engine, key='new')['state'] == '空闲'
        assert not item(engine, key='new').get('plugin_blocked')
        assert item(engine, key='done')['state'] == '完成'
        assert not item(engine, key='done').get('plugin_blocked')
    finally:
        engine.log.close()


def test_event_does_not_move_an_item_into_another_isolation(tmp_path):
    engine = build(tmp_path / 'state', multi_flow_config())
    try:
        engine.ingest(event(key='one', ext_id='1'), directed=True)
        engine.tick()
        engine.broken('alpha', 'executor', 'writer', RuntimeError('执行者代码错误'))
        before = item(engine, key='one')
        assert before['plugin_failure_key'] == 'alpha' and not before.get('plugin_retry_at')
        at = (engine.clock() + timedelta(hours=1)).isoformat()
        engine.broken('alpha', 'flow', 'flow', Retryable('暂时不通'),
                      retry={'at': at, 'count': 3, 'key': 'start:alpha:flow'})
        engine.ingest(event(key='one', ext_id='2', action='update'), directed=True)
        after = item(engine, key='one')
        assert after['plugin_failure_key'] == 'alpha' and after['plugin_retry_at'] is None
        assert after['blocked_by'] == before['blocked_by']
    finally:
        engine.log.close()


def test_woken_cleanup_is_retried_in_the_same_process(engine):
    engine.ingest(event())
    engine.tick()
    calls = []
    def cleanup(target, keep):
        calls.append(target['name'])
    engine.plugins.get('flow', 'flow').cleanup = cleanup
    for _ in range(3):
        engine.retry_failure('alpha', 'flow', 'flow', 'cleanup:alpha:flow', Retryable('暂时不通'))
    control = engine.control('alpha')
    assert control['broken'] and control['auto_retry']
    assert engine.cleaned_flows is True
    engine.clock.now = datetime.fromisoformat(control['retry_at'])
    engine.tick()
    assert not engine.control('alpha').get('broken')
    assert calls == ['alpha']
    assert not engine.control('retry:cleanup:alpha:flow').get('count')


def test_wake_is_all_or_nothing_when_restoring_an_item_fails(engine):
    engine.ingest(event())
    engine.ingest(event(key='two', ext_id='2'))
    engine.tick()
    at = (engine.clock() + timedelta(hours=1)).isoformat()
    engine.broken('alpha', 'flow', 'flow', Retryable('暂时不通'),
                  retry={'at': at, 'count': 3, 'key': 'start:alpha:flow'})
    assert item(engine, key='one')['plugin_blocked'] and item(engine, key='two')['plugin_blocked']
    engine.clock.now = datetime.fromisoformat(at)
    original = engine.change
    def interrupted(target, *args, **kwargs):
        if target['id'] == 'alpha:two':
            raise RuntimeError('写恢复事实时进程被打断')
        return original(target, *args, **kwargs)
    engine.change = interrupted
    with pytest.raises(RuntimeError):
        faults.wake_broken(engine)
    engine.change = original
    # 清控制和逐件恢复在同一事务里：失败整笔回滚，控制仍是自动重试，活仍卡着。
    control = engine.control('alpha')
    assert control['broken'] and control['auto_retry'] and control['retry_at'] == at
    assert item(engine, key='one')['plugin_blocked'] and item(engine, key='two')['plugin_blocked']
    assert not [fact for fact in engine.log.facts() if fact['kind'] == 'plugin_retry_wake']
    # 下一拍还看得到这条自动重试；这次恢复成功，两件活都回到原状。
    assert faults.wake_broken(engine) is True
    assert not engine.control('alpha').get('broken')
    assert not item(engine, key='one').get('plugin_blocked')
    assert not item(engine, key='two').get('plugin_blocked')


def test_status_and_manager_item_show_auto_retry_time(engine):
    engine.ingest(event())
    engine.tick()
    at = (engine.clock() + timedelta(hours=1)).isoformat()
    engine.broken('alpha', 'flow', 'flow', Retryable('暂时不通'),
                  retry={'at': at, 'count': 3, 'key': 'source:alpha:source'})
    text = status(engine.log, rules=engine.rules.data, plugins=engine.plugins)
    assert '车间自动重试，下次' in text
    view = {'rows': [], 'waits': [], 'managers': [{
        'id': 'fault:alpha', 'active': True, 'state': 'queued', 'attempts': [],
        'facts': [{'value': {'reason': '插件需要恢复',
                             'control': {'broken': True, 'auto_retry': True, 'retry_at': at}}}]}]}
    line = sections(view)['项目经理处理中']['lines'][0]
    assert '自动重试中，下次' in line and '插件需要恢复' not in line
