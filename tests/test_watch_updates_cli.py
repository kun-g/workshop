"""本地预览隔离、人工补发幂等与命令失败行为。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_watch_updates import (advance, judgment, judged, weekly, world)  # noqa: F401
from workshop.plugins.watch_updates.__main__ import main
from workshop.plugins.watch_updates.calendar import period
from workshop.plugins.watch_updates.feedback import feedback
from workshop.plugins.watch_updates.io import IO, read, write
from workshop.plugins.watch_updates.operations import prepare_preview, render_preview, resend_weekly


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_preview_uses_fresh_judgment_and_leaves_inputs_unchanged(world, tmp_path):
    io, target, flow, source = world
    item, poll = judged(world)
    weekly(world, poll)  # 旧报告不能阻止新预览，不能引用其生产文件路径。
    before = files(tmp_path)
    output = tmp_path / 'private-preview'
    prepare_preview(target, [item['object_key']], output, io)
    bundle = read(output/'preview.json')
    assert 'judgment' not in bundle['state']['versions'][item['object_key']]
    assert not bundle['state']['reports']
    assert '"judgment"' not in (output/(item['object_key']+'.brief.txt')).read_text()
    supplied = tmp_path/'new-judgments.json'
    write(supplied, {item['object_key']: judgment(True)})
    result = render_preview(output, supplied)
    html = Path(result['path']).read_text()
    assert result['sent'] is False and '影响公开服务' in html
    assert '没有相关修复' not in html
    assert read(output/'report.json')['snapshot'][item['object_key']]['judgment']['format'] == 'watch-judgment/v2'
    after = files(tmp_path)
    assert all(after.get(name) == value for name, value in before.items())
    assert set(after)-set(before) <= {p for p in after if p.startswith('private-preview/')} | {'new-judgments.json'}
    assert len(io.sent) == 1  # 只有准备测试时的正式周报。


@pytest.mark.parametrize('bad', ['missing', 'three', 'duplicate', 'notes', 'overlap', 'existing'])
def test_bad_preview_inputs_do_not_modify_production(world, tmp_path, bad):
    io, target, flow, source = world
    item, _ = judged(world)
    key = item['object_key']
    output = tmp_path/'preview'
    keys = [key]
    if bad == 'missing':
        keys = ['absent']
    elif bad == 'three':
        keys = [key, 'x', 'y']
    elif bad == 'duplicate':
        keys = [key, key]
    elif bad == 'notes':
        state = io.state(target)
        state['versions'][key]['notes'] = str(tmp_path/'no-notes')
        io.save(target, state)
    elif bad == 'overlap':
        output = Path(target['watch_updates']['state_dir'])/'preview'
    else:
        output.mkdir()
    before = files(tmp_path)
    with pytest.raises(ValueError):
        prepare_preview(target, keys, output, io)
    assert files(tmp_path) == before


def test_cli_preview_failure_and_valid_run(world, tmp_path, capsys):
    io, target, flow, source = world
    item, _ = judged(world)
    rules = tmp_path/'rules.json'
    write(rules, {'targets': {'alpha': target}})
    output = tmp_path/'preview'
    assert main(['prepare-preview', '--rules', str(rules), '--target', 'alpha', '--version', item['object_key'], '--output', str(output)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'prepared'
    supplied = tmp_path/'judgments.json'
    write(supplied, {item['object_key']: {'instances': []}})
    assert main(['render-preview', '--preview', str(output), '--judgments', str(supplied)]) == 1
    assert '判断必须' in capsys.readouterr().err
    assert not (output/'reports').exists()
    write(supplied, {item['object_key']: judgment()})
    result = subprocess.run([sys.executable, '-m', 'workshop.plugins.watch_updates', 'render-preview',
                             '--preview', str(output), '--judgments', str(supplied)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'previewed'


def test_resend_already_sent_creates_snapshot_and_same_request_dedupes(world):
    io, target, flow, source = world
    item, poll = judged(world)
    weekly(world, poll)
    original = deepcopy(io.state(target)['reports'][io.sent[0]['id']])
    old_html = Path(original['path']).read_bytes()
    state = io.state(target)
    state['versions'][item['object_key']]['judgment']['instances'][0]['headline'] = '等待维护窗口'
    io.save(target, state)
    first = resend_weekly(target, 'manual-1', io)
    second = resend_weekly(target, 'manual-1', io)
    assert first == second and first['status'] == 'sent'
    assert len(io.sent) == 2
    assert first['report'] != original['id']
    assert Path(original['path']).read_bytes() == old_html
    state = io.state(target)
    assert state['reports'][original['id']] == original
    assert item['object_key'] in state['reports'][first['report']]['versions']
    parsed = feedback(json.dumps({'report': original['id'], 'items': [{'id': 'one', 'choice': '没问题', 'note': ''}]}), state)
    assert parsed['items'][0]['key'] == item['object_key']
    # 跨周重试绑定原请求，不偷偷生成下一周报告。
    io.time += timedelta(days=7)
    assert resend_weekly(target, 'manual-1', io) == first
    assert len(io.sent) == 2
    assert not io.state(target)['outbox']


def test_resend_empty_week_and_requires_existing_anchor(world):
    io, target, flow, source = world
    with pytest.raises(ValueError, match='尚无已完成对账'):
        resend_weekly(target, 'empty', io)
    state = io.state(target)
    state['weeks']['reconcile-2026-11-16'] = {'reconciled': True}
    io.save(target, state)
    result = resend_weekly(target, 'empty', io)
    assert result['status'] == 'sent'
    assert '本周没有该升的项' in io.sent[0]['summary']
    assert io.sent[0]['versions'] == []
    assert resend_weekly(target, 'empty', io) == result
    assert len(io.sent) == 1


def test_resend_failed_report_reuses_frozen_payload(world):
    io, target, flow, source = world
    item, poll = judged(world)
    state = io.state(target)
    state['weeks']['reconcile-2026-11-16'] = {'reconciled': True}
    io.save(target, state)
    actual_send = io.send_report
    def fail(*args):
        raise ValueError('发送未确认')
    io.send_report = fail
    with pytest.raises(ValueError, match='发送未确认'):
        resend_weekly(target, 'failed', io)
    saved = io.state(target)
    report_id = saved['resends']['failed']['report']
    old_snapshot = deepcopy(saved['reports'][report_id]['snapshot'])
    saved['versions'][item['object_key']]['judgment']['instances'][0]['headline'] = '后来的判断'
    io.save(target, saved)
    io.send_report = actual_send
    result = resend_weekly(target, 'failed', io)
    assert result['report'] == report_id and len(io.sent) == 1
    assert io.sent[0]['snapshot'] == old_snapshot


def test_resend_uses_delivery_journal_and_does_not_bypass_unknown_result(world):
    io, target, flow, source = world
    item, poll = judged(world)
    weekly(world, poll)
    config = target['watch_updates']
    config.update(lark='fake-lark', profile='test', kun_open_id='test')
    actual = IO(io.clock)
    calls = []
    def failed(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, '', 'disconnected')
    actual.command = failed
    with pytest.raises(ValueError, match='发送未确认'):
        resend_weekly(target, 'supplement', actual)
    io.time += timedelta(hours=1)
    # 新 request 也要继续未确认的报告，不能绕过发送窗口另发一份。
    with pytest.raises(ValueError, match='结果不明'):
        resend_weekly(target, 'another-request', actual)
    assert len(calls) == 1
    state = io.state(target)
    assert state['resends']['supplement']['report'] == state['resends']['another-request']['report']


def test_resend_delivery_success_before_state_save_is_not_sent_twice(world, monkeypatch):
    io, target, flow, source = world
    item, poll = judged(world)
    weekly(world, poll)
    target['watch_updates'].update(lark='fake-lark', profile='test', kun_open_id='test')
    actual = IO(io.clock)
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"ok":true,"data":{"message_id":"om_sent"}}', '')
    actual.command = command
    save = actual.save
    def crash_after_sent(target, state):
        if any(r.get('sent') and '-resend-' in r['id'] for r in state['reports'].values()):
            raise OSError('crash after journal success')
        return save(target, state)
    monkeypatch.setattr(actual, 'save', crash_after_sent)
    with pytest.raises(OSError):
        resend_weekly(target, 'retry', actual)
    monkeypatch.setattr(actual, 'save', save)
    assert resend_weekly(target, 'retry', actual)['status'] == 'sent'
    assert len(calls) == 1


@pytest.mark.parametrize('now,expected', [
    ('2026-11-16T00:59:00+00:00', ('2026-11-15', '2026-11-09')),
    ('2026-11-16T01:00:00+00:00', ('2026-11-16', '2026-11-16')),
    ('2026-11-15T23:59:00-05:00', ('2026-11-16', '2026-11-16')),
])
def test_period_matches_source_clock_and_week_start(world, now, expected):
    io, target, flow, source = world
    io.time = datetime.fromisoformat(now)
    assert period(target['watch_updates'], io.clock()) == expected
    poll = source.poll(target, None)
    cursor = json.loads(poll.cursor)
    assert (cursor['day'], cursor['week']) == expected


def test_cli_resend_reports_failure_with_request(world, tmp_path, capsys):
    io, target, flow, source = world
    rules = tmp_path/'rules.json'
    write(rules, {'targets': {'alpha': target}})
    assert main(['resend-weekly', '--rules', str(rules), '--target', 'alpha', '--request', 'manual']) == 1
    assert 'request=manual' in capsys.readouterr().err
