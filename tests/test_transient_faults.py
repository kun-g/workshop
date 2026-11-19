"""瞬时故障：标坏后按小时自动重试，成功自动解除并恢复被卡的活（核心 R14、R20）。"""
from datetime import datetime, timedelta

import pytest

from conftest import build, configuration, event, item
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
