"""事项所有权、失效恢复、订阅耗尽、额度归属及只读点击。"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import sys
import threading

import pytest
import yaml

from workshop.core.managers import Store, Coordinator, observe_items, instructions
from workshop.core.models import Started
from workshop.core.ports import InputBlocked
from workshop.core.activity import snapshot
from workshop.plugins.intervention.service import Service
from workshop.plugins.intervention.manager import help_card

NOW = '2026-11-18T00:00:00+00:00'
ROOT = Path(__file__).resolve().parents[1]


class Executor:
    def __init__(self):
        self.calls, self.live, self.accept, self.fail = [], True, True, False

    def available(self, config):
        return True

    def launch(self, token, brief, item, config, target):
        self.calls.append(('launch', token, brief))
        if self.fail:
            raise InputBlocked('订阅认证失败')
        return Started(token, {'kind': 'test'})

    def resume(self, token, session, brief, item, config, target):
        self.calls.append(('resume', token, session, brief))
        return Started(session, {'kind': 'test'})

    def stop(self, token, started):
        self.calls.append(('stop', token))

    def poll(self, token, started):
        return 'running' if self.live else 'exited'

    def inform(self, token, started, operation, text):
        self.calls.append(('inform', token, operation, text))
        return self.accept


@pytest.fixture
def coordinator(tmp_path):
    store = Store(tmp_path)
    executors = {'one': Executor(), 'two': Executor()}
    config = {'managers': {'candidates': ['one', 'two'], 'target': 'test', 'service': 'intervention',
                           'manual': str(ROOT / 'docs/项目经理.md')},
              'executor_profiles': {n: {'billing': 'subscription'} for n in executors},
              'wait_services': {'intervention': {}}}
    registry = SimpleNamespace(get=lambda port, name: executors[name])
    jobs = SimpleNamespace(call=lambda port, name, method, args, **kw: getattr(executors[name], method)(*args))
    rules = SimpleNamespace(data=config, target=lambda name: {}, path=str(ROOT / 'rules/workshop.yaml'))
    c = Coordinator(store, rules, registry, jobs, ['manager'], NOW)
    yield c, executors
    store.close()


def observe(c, key='item:a', number=1):
    return c.store.observe(key, {'title': '交付检查', 'reason': str(number)}, 'core', NOW)


def drive(c):
    for row in c.store.all():
        c.drive(row)


def conclusion(c, row, result='resolved'):
    value = {'schema': 'manager/v1', 'result': result}
    if result == 'blocked':
        value.update(action='恢复订阅登录', reason='登录已经失效', evidence='认证命令返回未登录', status_location='处理不了')
    c.store.receipt(row['token'], {'outcome': 'done', 'summary': '诊断完成', 'evidence': [json.dumps(value)]})


def test_one_session_per_item_new_facts_inform_and_parallel_capacity(coordinator):
    c, executors = coordinator
    observe(c)
    observe(c, 'item:b')
    drive(c)
    first = c.store.all()
    assert len({r['token'] for r in first}) == 2
    observe(c, number=2)
    drive(c)
    assert [r['token'] for r in c.store.all()] == [r['token'] for r in first]
    assert len([x for x in executors['one'].calls if x[0] == 'inform']) == 1
    drive(c)
    assert len([x for x in executors['one'].calls if x[0] == 'inform']) == 1
    assert not (c.store.directory / 'workshop.db').exists()


def test_cannot_inform_resume_after_exit_with_all_facts(coordinator):
    c, ex = coordinator
    observe(c)
    drive(c)
    original = c.store.all()[0]
    ex['one'].accept = False
    observe(c, number=2)
    drive(c)
    assert c.store.all()[0]['delivered'] == 1
    ex['one'].live = False
    drive(c)
    drive(c)
    drive(c)
    assert c.store.all()[0]['started']['session'] == original['started']['session']
    assert [x[0] for x in ex['one'].calls][-2:] == ['stop', 'resume']
    assert len(ex['one'].calls[-1][-1].materials[-1]['record']['facts']) == 2


def test_restart_takeover_healthy_long_running_no_second_launch(coordinator):
    c, ex = coordinator
    observe(c)
    drive(c)
    token = c.store.all()[0]['token']
    c.store.close()
    c.store = Store(c.store.directory)
    c.now = '2027-11-18T00:00:00+00:00'
    c.store.observe('item:a', {'title': '交付检查', 'reason': '1'}, 'watch', c.now)
    drive(c)
    assert c.store.all()[0]['token'] == token and c.store.all()[0]['owner'] == 'watch'
    assert len([x for x in ex['one'].calls if x[0] == 'launch']) == 1
    c.store.close()
    # fixture owns the original closed connection; sqlite close is idempotent.


def test_exhaustion_records_every_definite_failure_no_metered_candidate(coordinator):
    c, ex = coordinator
    for e in ex.values():
        e.fail = True
    observe(c)
    for _ in range(3):
        drive(c)
    row = c.store.all()[0]
    assert row['state'] == 'unavailable' and len(row['attempts']) == 2 and not row.get('token')
    assert row['conclusion']['unavailable']


def test_resolved_no_help_and_new_fact_reopens(coordinator):
    c, ex = coordinator
    observe(c)
    drive(c)
    conclusion(c, c.store.all()[0])
    drive(c)
    assert c.store.all()[0]['state'] == 'resolved'
    observe(c, number=2)
    drive(c)
    assert c.store.all()[0]['state'] == 'running'
    assert '不改代码、规则、需求，不开单' in instructions(c.store.directory)


def test_business_count_excludes_starting_receipt_manager_and_poll(engine, tmp_path):
    engine.active = lambda: [dict(token='running', status='running'), dict(token='starting', status='starting'),
                             dict(token='done', status='running', receipt={})]
    first = snapshot(engine, {}, NOW)
    assert first['agents'] == first['total'] == 1 and first['idle_since'] is None
    engine.active = lambda: []
    stop = (datetime.fromisoformat(NOW) + timedelta(seconds=10)).isoformat()
    idle = snapshot(engine, first, stop)
    assert idle['idle_since'] == stop
    assert snapshot(engine, idle, (datetime.fromisoformat(stop) + timedelta(minutes=6)).isoformat())['idle_since'] == stop


@pytest.fixture
def service(tmp_path, monkeypatch):
    config = yaml.safe_load((ROOT / 'rules/workshop.yaml').read_text())
    config['intervention']['status'] = {'recipient': 'Kun', 'chat': 'private',
        'command': [sys.executable, '-c', 'import json,sys; q=json.load(sys.stdin); print(json.dumps(dict(section=q["section"],lines=["检查：等你登录"],details=[])))']}
    path = tmp_path / 'rules.yaml'
    path.write_text(yaml.safe_dump(config, allow_unicode=True))
    s = Service(tmp_path / 'service', path)
    sent = []
    def transport(config, action, record):
        sent.append((action, record['id'], record['payload']))
        return {'state': 'sent', 'external': 'message:' + record['id']}
    monkeypatch.setattr(s, 'transport', transport)
    yield s, sent
    s.store.close()


def record(n='a', revision=1, total=0, idle=NOW, active=True, state='blocked'):
    return dict(id=n, episode=n, revision=revision, active=active, state=state,
        facts=[], attempts=[], business={'total': total, 'idle_since': idle},
        conclusion={'schema': 'manager/v1', 'result': 'blocked', 'action': '恢复订阅登录', 'reason': '登录已经失效',
                    'evidence': '认证命令返回未登录', 'status_location': '处理不了'})


def update(s, row):
    return s.call('manager_update', {'operation': f"update:{row['id']}:{row['revision']}", 'record': row})


def test_idle_shared_quota_restart_later_conclusions_status_only(service):
    s, sent = service
    update(s, record('a'))
    update(s, record('b'))
    s.call('manager_flush', {})
    assert len(sent) == 1 and len(sent[0][2]['members']) == 2
    update(s, record('c'))
    s.call('manager_flush', {})
    assert len(sent) == 1 and len(s.store.all('manager')) == 3
    s.store.close()
    s.store = __import__('workshop.plugins.intervention.store', fromlist=['Store']).Store(s.store_path if hasattr(s,'store_path') else s.rules.parent / 'service/intervention.db')
    s.call('manager_flush', {})
    assert len(sent) == 1
    update(s, record('d', total=1))
    update(s, record('e', total=1))
    s.call('manager_flush', {})
    assert len(sent) == 3
    update(s, record('f', idle='2026-11-19T00:00:00+00:00'))
    s.call('manager_flush', {})
    assert len(sent) == 4


def test_pushed_concerns_are_not_pushed_again_across_idle_segments(service):
    """11-19 回放：同样成员连续三段空闲只发第一张；第三段多一件新事，只发这一件。"""
    s, sent = service
    segments = ['2026-11-19T03:37:00+00:00', '2026-11-19T05:03:00+00:00', '2026-11-19T06:00:00+00:00']
    for n in ('a', 'b'):
        update(s, record(n, idle=segments[0]))
    s.call('manager_flush', {})
    assert [p['members'] for _, _, p in sent] == [['a', 'b']]
    for n in ('a', 'b'):
        update(s, record(n, revision=2, idle=segments[1]))
    s.call('manager_flush', {})
    assert len(sent) == 1
    for n in ('a', 'b'):
        update(s, record(n, revision=3, idle=segments[2]))
    fresh = record('c', idle=segments[2])
    fresh['conclusion'].update(action='清理残留问卡', evidence='问卡还挂着')
    update(s, fresh)
    s.call('manager_flush', {})
    assert len(sent) == 2
    card = sent[1][2]
    assert card['members'] == ['c'] and card['primary'] == 'c'
    assert card['card']['header']['title']['content'] == '清理残留问卡'
    panel = card['card']['body']['elements'][1]['elements']
    assert [b['content'] for b in panel] == ['**清理残留问卡**\n问卡还挂着']
    # 别的活跑起来后，推过的事不按「一件事一次」再推；没推过的照旧推一次。
    for n in ('a', 'b', 'c'):
        update(s, record(n, revision=4, total=1))
    update(s, record('d', total=1))
    s.call('manager_flush', {})
    s.call('manager_flush', {})
    assert [p['members'] for _, _, p in sent[2:]] == [['d']]


def test_global_business_one_record_drives_all_idle_and_incident_cards(service):
    """车间只发一份全局业务状态：同一空闲段的事项并一张卡，忙时按事项各一次。"""
    s, sent = service
    for name in ('a', 'b'):
        update(s, record(name, total=None))
    s.call('manager_flush', {'business': {'total': None, 'idle_since': None}})
    assert sent == []  # 没有可信业务快照，不选额度归属。
    s.call('manager_flush', {'business': {'total': 0, 'idle_since': NOW}})
    assert [p['members'] for _, _, p in sent] == [['a', 'b']]
    update(s, record('c', total=None))
    s.call('manager_flush', {'business': {'total': 0, 'idle_since': NOW}})
    assert len(sent) == 1  # 同一空闲段不再推已推过的，也不为新结论另开一张。
    s.call('manager_flush', {'business': {'total': 0, 'idle_since': '2026-11-19T00:00:00+00:00'}})
    assert [p['members'] for _, _, p in sent[1:]] == [['c']]  # 新空闲段只有没推过的事。
    update(s, record('d', total=None))
    s.call('manager_flush', {'business': {'total': 1, 'idle_since': None}})
    assert [p['members'] for _, _, p in sent[2:]] == [['d']]
    # 业务状态只存一条，状态不变不增 journal。
    before = s.store.db.execute('SELECT count(*) FROM journal').fetchone()[0]
    s.call('manager_flush', {'business': {'total': 1, 'idle_since': None}})
    assert s.store.db.execute('SELECT count(*) FROM journal').fetchone()[0] == before


def test_unchanged_record_is_not_appended_to_journal(service):
    s, _ = service
    s.store.put('delivery', 'delivery:same', {'id': 'delivery:same', 'state': 'uncertain'}, 'uncertain')
    before = s.store.db.execute('SELECT count(*) FROM journal').fetchone()[0]
    s.store.put('delivery', 'delivery:same', {'id': 'delivery:same', 'state': 'uncertain'}, 'uncertain')
    assert s.store.db.execute('SELECT count(*) FROM journal').fetchone()[0] == before


def test_resolve_cancels_queued_and_stale_update_cannot_reopen(service):
    s, sent = service
    update(s, record())
    # queue durable but hold dispatch to exercise the crash window
    from workshop.plugins.intervention.manager import queue
    with s.store.transaction():
        queue(s, record())
    update(s, record(revision=2, active=False, state='resolved'))
    s.call('manager_flush', {})
    assert sent == [] and s.store.all('delivery')[0]['state'] == 'cancelled'
    s.call('manager_update', {'operation': 'old', 'record': record()})
    assert not s.store.all('manager')[0]['active']


def event(delivery, who='Kun'):
    return {'event': {'operator': {'open_id': who},
        'context': {'open_chat_id': 'private', 'open_message_id': delivery['external']},
        'action': {'tag': 'button', 'value': {'ns': 'workshop-status/v1', 'delivery': delivery['id'], 'section': '处理不了'}}}}


def test_clicks_are_current_read_only_and_not_push_budget(service):
    s, sent = service
    update(s, record())
    s.call('manager_flush', {})
    delivery = s.store.all('delivery')[0]
    before = s.store.all('permit'), s.store.all('wait'), s.store.all('manager')
    assert not s.call('status_click', {'operation': 'foreign', 'event': event(delivery, 'other')})['accepted']
    assert len(sent) == 1
    for key in ('click1','click1','click2'):
        result = s.call('status_click', {'operation': key, 'event': event(delivery)})
        assert result['snapshot']['lines'] == ['检查：等你登录']
    assert len(sent) == 3
    assert before == (s.store.all('permit'), s.store.all('wait'), s.store.all('manager'))


@pytest.mark.parametrize('field,limit', [('action',20),('reason',50),('evidence',300)])
def test_card_limits_and_no_internal_identifiers(field, limit):
    c = record()['conclusion']
    assert help_card(c, 'delivery:test')['header']['subtitle']['content'] == '项目经理处理不了'
    assert help_card({**c, 'unavailable': True}, 'delivery:test')['header']['subtitle']['content'] == '车间程序：项目经理起不来'
    c[field] = '字' * (limit + 1)
    with pytest.raises(ValueError):
        help_card(c, 'delivery:test')


def test_resolved_primary_cancels_merged_card_even_with_other_blocked(service):
    s, sent = service
    from workshop.plugins.intervention.manager import queue
    update(s, record('a'))
    update(s, record('b'))
    with s.store.transaction():
        queue(s, record('a'))
    update(s, record('a', revision=2, active=False, state='resolved'))
    s.call('manager_flush', {})
    assert sent == [] and s.store.all('delivery')[0]['state'] == 'cancelled'
    assert any(r['id'] == 'b' and r['state'] == 'blocked' for r in s.store.all('manager'))


def test_business_transition_publishes_a_new_version(coordinator):
    c, ex = coordinator
    row = observe(c)
    row['publication'] = 'old-format'
    c.store.save(row, 'published')
    revision = row['revision']
    calls = []
    c.jobs = SimpleNamespace(call=lambda *a, **kw: calls.append(a) or {'ref': 'saved'})
    c.publish(row)
    assert len(calls) == 1
    record = calls[0][3][1]['record']
    # 发布格式变了而记录没动过：抬一版，介入点才会收下；之后再发同一内容不再重复。
    assert record['revision'] == revision + 1 and 'business' not in record
    assert row['published_revision'] == row['revision']
    c.publish(row)
    assert len(calls) == 1
    assert [json.loads(r[0])['event'] for r in c.store.db.execute('SELECT body FROM history ORDER BY seq')].count('renewed') == 1


def test_idle_changes_do_not_republish_or_snapshot_each_concern(coordinator):
    c, ex = coordinator
    for name in ('a', 'b', 'c'):
        observe(c, 'item:' + name)
    calls = []
    def call(port, name, method, args, **kw):
        calls.append((port, name, method, args))
        return {'ref': 'saved'} if port == 'wait' else getattr(ex[name], method)(*args)
    c.jobs = SimpleNamespace(call=call)
    c.run({'total': 0, 'idle_since': NOW})
    updates = lambda: [a for a in calls if a[2] == 'invoke' and a[3][0] == 'manager_update']
    assert len(updates()) == 3
    history = c.store.db.execute('SELECT count(*) FROM history').fetchone()[0]
    for minute in range(1, 6):
        c.run({'total': 0, 'idle_since': f'2026-11-18T00:0{minute}:00+00:00'})
    # 空闲段号变了 5 次，3 条事项不再各存快照、各重发一次。
    assert len(updates()) == 3
    assert c.store.db.execute('SELECT count(*) FROM history').fetchone()[0] == history
    flushes = [a for a in calls if a[2] == 'invoke' and a[3][0] == 'manager_flush']
    assert len(flushes) == 6
    assert flushes[-1][3][1] == {'business': {'total': 0, 'idle_since': '2026-11-18T00:05:00+00:00'}}
    # 结论有实质变化时照旧存快照（变化部分）并发布。
    observe(c, number=2)
    c.run({'total': 0, 'idle_since': '2026-11-18T00:06:00+00:00'})
    assert len(updates()) == 4
    assert c.store.db.execute('SELECT count(*) FROM history').fetchone()[0] > history
    observed = json.loads(c.store.db.execute(
        "SELECT body FROM history WHERE json_extract(body,'$.event')='observed' ORDER BY seq DESC LIMIT 1").fetchone()[0])
    assert observed['format'] == 2 and 'record' not in observed
    assert [fact['value']['reason'] for fact in observed['append']['facts']] == ['2']


def test_inform_acceptance_and_uncertainty_never_types_twice(tmp_path, monkeypatch):
    from workshop.plugins.executors.local.adapters import Executor
    from workshop.plugins.executors.local.storage import write
    e = Executor('orca-codex', state_dir=tmp_path)
    directory = e.directory('token')
    write(directory/'spec.json', {'session':'session'})
    write(directory/'terminal.json', {'handle':'terminal:1'})
    monkeypatch.setattr(e, 'poll', lambda *args:'running')
    started = Started('session','terminal:1')
    calls=[]
    def send(*argv):
        calls.append(argv)
        return {'accepted': True}
    monkeypatch.setattr(e, '_orca', send)
    assert e.inform('token',started,'op','新事实')
    assert e.inform('token',started,'op','新事实')
    assert len(calls)==1
    with pytest.raises(ValueError):
        e.inform('token',started,'op','换内容')
    def lost(*argv):
        calls.append(argv)
        raise RuntimeError('回包丢失')
    monkeypatch.setattr(e, '_orca', lost)
    assert not e.inform('token',started,'uncertain','另一事实')
    assert not e.inform('token',started,'uncertain','另一事实')
    assert len(calls)==2


def test_program_jobs_break_idle_even_when_finished_between_ticks(engine):
    import fcntl
    from workshop.core.jobs import write
    root=engine.log.path.parent/'jobs'
    program=root/'program'; program.mkdir(parents=True)
    poll=root/'poll'; poll.mkdir()
    write(program/'spec.json', {'port':'flow','method':'assess','item':'alpha:one'})
    write(poll/'spec.json', {'port':'source','method':'poll','item':'alpha:one'})
    with (program/'lock').open('a') as lock, (poll/'lock').open('a') as other:
        fcntl.flock(lock,fcntl.LOCK_EX)
        fcntl.flock(other,fcntl.LOCK_EX)
        live=snapshot(engine,{},NOW)
        assert live['jobs']==1 and live['job_ids']==['program']
    at=datetime.fromisoformat(NOW)+timedelta(seconds=20)
    write(program/'result.json', {'ok':{},'finished':at.timestamp()})
    before={'total':0,'idle_since':NOW,'last_activity_at':None}
    later=snapshot(engine,before,(at+timedelta(seconds=20)).isoformat())
    assert later['idle_since']==at.isoformat()


def test_first_idle_sample_starts_now_even_with_old_completed_round(engine):
    engine.log.db.execute('INSERT INTO rounds VALUES (?,?)',('old',json.dumps({'token':'old','status':'ended','ended':'2020-01-01T00:00:00+00:00'})))
    assert snapshot(engine,{},NOW)['idle_since']==NOW


def test_idle_heartbeat_reads_no_files_of_completed_jobs(engine, monkeypatch):
    """几千个已完成作业：确认过扫描起点的下一拍只 stat 目录，不读任何文件。"""
    from workshop.core import activity as activity_module
    from workshop.core.jobs import write
    root=engine.log.path.parent/'jobs'
    root.mkdir(parents=True)
    finished=(datetime.fromisoformat(NOW)-timedelta(hours=2)).timestamp()
    old=(datetime.fromisoformat(NOW)-timedelta(hours=1)).timestamp()
    for index in range(3000):
        path=root/f'job-{index}'
        path.mkdir()
        write(path/'spec.json', {'port':'flow','method':'assess','item':f'alpha:{index}','materials':['x'*2000]})
        write(path/'result.json', {'ok':None,'finished':finished,'port':'flow','method':'assess','item':f'alpha:{index}'})
        os.utime(path, (old, old))
    first=snapshot(engine,{},NOW)
    seen=[]
    real=activity_module.read
    monkeypatch.setattr(activity_module,'read',lambda path: seen.append(path) or real(path))
    second=snapshot(engine,first,(datetime.fromisoformat(NOW)+timedelta(seconds=1)).isoformat())
    assert second['total']==0 and second['job_ids']==[]
    assert seen==[]


def test_running_job_is_carried_between_ticks_without_reading_its_spec(engine, monkeypatch):
    """上一拍在跑的作业目录这一拍不再按 mtime 扫，照 job_ids 核对锁即可。"""
    import fcntl
    from workshop.core import activity as activity_module
    from workshop.core.jobs import write
    root=engine.log.path.parent/'jobs'
    live=root/'busy'; live.mkdir(parents=True)
    write(live/'spec.json', {'port':'flow','method':'brief','item':'alpha:one','materials':['x'*200000]})
    old=(datetime.fromisoformat(NOW)-timedelta(hours=1)).timestamp()
    os.utime(live, (old, old))
    with (live/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        first=snapshot(engine,{},NOW)
        assert first['jobs']==1 and first['job_ids']==['busy']
        seen=[]
        real=activity_module.read
        monkeypatch.setattr(activity_module,'read',lambda path: seen.append(path) or real(path))
        second=snapshot(engine,first,(datetime.fromisoformat(NOW)+timedelta(seconds=10)).isoformat())
        assert second['jobs']==1 and second['job_ids']==['busy']
    assert not [path for path in seen if path.name=='spec.json']


def test_job_completed_between_ticks_breaks_idle_using_result_identity(engine, monkeypatch):
    """两拍之间开工又完成的作业：按目录 mtime 找到，结果自带调用身份，不读大 spec。"""
    from workshop.core import activity as activity_module
    from workshop.core.jobs import write
    root=engine.log.path.parent/'jobs'
    root.mkdir(parents=True)
    first=snapshot(engine,{'total':0,'idle_since':NOW,'last_activity_at':None},NOW)
    assert first['scanned_at']==NOW
    at=datetime.fromisoformat(NOW)+timedelta(seconds=20)
    path=root/'brief'; path.mkdir()
    write(path/'spec.json', {'port':'flow','method':'brief','item':'alpha:one','materials':['x'*200000]})
    write(path/'result.json', {'ok':{},'finished':at.timestamp(),'port':'flow','method':'brief','item':'alpha:one'})
    os.utime(path, (at.timestamp(), at.timestamp()))
    seen=[]
    real=activity_module.read
    monkeypatch.setattr(activity_module,'read',lambda p: seen.append(p) or real(p))
    later=snapshot(engine,first,(at+timedelta(seconds=20)).isoformat())
    assert later['idle_since']==at.isoformat()
    assert not [p for p in seen if p.name=='spec.json']


def test_job_completed_between_ticks_with_old_result_format_reads_spec(engine):
    """旧作业进程写的结果没有调用身份，回落到读 spec 认出业务作业。"""
    from workshop.core.jobs import write
    root=engine.log.path.parent/'jobs'
    root.mkdir(parents=True)
    first=snapshot(engine,{'total':0,'idle_since':NOW,'last_activity_at':None},NOW)
    at=datetime.fromisoformat(NOW)+timedelta(seconds=20)
    path=root/'legacy'; path.mkdir()
    write(path/'spec.json', {'port':'flow','method':'assess','item':'alpha:one'})
    write(path/'result.json', {'ok':{},'finished':at.timestamp()})
    os.utime(path, (at.timestamp(), at.timestamp()))
    later=snapshot(engine,first,(at+timedelta(seconds=20)).isoformat())
    assert later['idle_since']==at.isoformat()


def test_old_heartbeat_without_scan_start_scans_from_its_own_at(engine, monkeypatch):
    """升级后第一拍：旧心跳没有 scanned_at，用它自己的 at 当扫描起点，不回头读历史作业。"""
    from workshop.core import activity as activity_module
    from workshop.core.jobs import write
    at=engine.clock().isoformat()
    root=engine.log.path.parent/'jobs'
    root.mkdir(parents=True)
    old=(datetime.fromisoformat(at)-timedelta(hours=1)).timestamp()
    for index in range(100):
        path=root/f'job-{index}'
        path.mkdir()
        write(path/'spec.json', {'port':'flow','method':'assess','item':f'alpha:{index}'})
        write(path/'result.json', {'ok':None,'finished':old-60})
        os.utime(path, (old, old))
    (engine.log.path.parent/'heartbeat.json').write_text(json.dumps(
        {'at':at,'business':{'total':0,'idle_since':at,'last_activity_at':None,'job_ids':[]}}))
    seen=[]
    real=activity_module.read
    monkeypatch.setattr(activity_module,'read',lambda path: seen.append(path) or real(path))
    engine.tick()
    assert seen==[]
    beat=json.loads((engine.log.path.parent/'heartbeat.json').read_text())
    assert beat['business']['scanned_at']==at


def test_shared_ledger_lock_does_not_allow_two_owners(tmp_path):
    a,b=Store(tmp_path),Store(tmp_path)
    try:
        with a.lock() as first, b.lock() as second:
            assert first and not second
    finally:
        a.close();b.close()


def test_early_receipt_recognizes_pending_start_before_finishing(coordinator):
    from workshop.core.jobs import Pending
    c, ex=coordinator
    original=c.jobs.call
    launches=[]
    def call(port,name,method,args,**kw):
        if method=='launch':
            launches.append(args[0])
            if len(launches)==1:raise Pending('launch')
            return Started('already-finished','handle')
        return original(port,name,method,args,**kw)
    c.jobs.call=call
    observe(c)
    with pytest.raises(Pending):drive(c)
    reserved=c.store.all()[0]
    conclusion(c,reserved)
    drive(c)
    assert c.store.all()[0]['delivered']==1 and c.store.all()[0]['state']=='running'
    drive(c)
    final=c.store.all()[0]
    assert final['state']=='resolved' and final['last_session']=='already-finished'
    assert len(set(launches))==1 and final['generation']==1


def test_bad_front_screen_rejected_while_agent_can_correct_receipt(coordinator):
    c, ex=coordinator
    observe(c);drive(c)
    row=c.store.all()[0]
    body=record()['conclusion'];body['reason']='请核对 #175'
    with pytest.raises(ValueError,match='首屏'):
        c.store.receipt(row['token'],{'outcome':'done','summary':'检查','evidence':[json.dumps(body)]})
    assert not c.store.db.execute('SELECT token FROM receipts').fetchall()
    conclusion(c,row,'blocked')
    drive(c)
    assert c.store.all()[0]['state']=='blocked'


def test_bad_card_does_not_block_another_incident_or_retry(service):
    s,sent=service
    bad=record('bad',total=1);bad['conclusion']['reason']='请核对 #175'
    update(s,bad);update(s,record('good',total=1))
    result=s.call('manager_flush',{})
    assert len(result['errors'])==1 and len(sent)==1
    assert sent[0][2]['primary']=='good'
    s.call('manager_flush',{})
    assert len(sent)==1


@pytest.mark.parametrize('family,status,allowed', [
    ('codex','Logged in using ChatGPT',True),('codex','Logged in using API key',False),
    ('claude',json.dumps({'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty','subscriptionType':'max'}),True),
    ('claude',json.dumps({'loggedIn':True,'authMethod':'api_key'}),False),
])
def test_manager_profiles_require_real_subscription_login(monkeypatch,family,status,allowed):
    import subprocess
    from workshop.plugins.executors.local.drivers import load
    from workshop.core.ports import Unavailable
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a[0],0,status,''))
    driver=load(family)
    executor=SimpleNamespace(interactive=False,commands={family:family})
    config={'billing':'subscription','require_subscription_login':True}
    if allowed:driver.preflight(executor,config)
    else:
        with pytest.raises(Unavailable):driver.preflight(executor,config)


def test_new_click_reads_new_content_and_read_failure_folds_path(service):
    s,sent=service
    update(s,record());s.call('manager_flush',{})
    delivery=s.store.all('delivery')[0]
    first=s.call('status_click',{'operation':'first','event':event(delivery)})
    config=yaml.safe_load(s.rules.read_text())
    config['intervention']['status']['command']=[sys.executable,'-c','import json,sys;q=json.load(sys.stdin);print(json.dumps(dict(section=q["section"],lines=["等待已解除"],details=[])))']
    s.rules.write_text(yaml.safe_dump(config,allow_unicode=True))
    again=s.call('status_click',{'operation':'first','event':event(delivery)})
    changed=s.call('status_click',{'operation':'second','event':event(delivery)})
    assert first['snapshot']==again['snapshot']
    assert changed['snapshot']['lines']==['等待已解除']
    config['intervention']['status']['command']=['/missing/private/read-command']
    s.rules.write_text(yaml.safe_dump(config,allow_unicode=True))
    failed=s.call('status_click',{'operation':'failed-read','event':event(delivery)})['snapshot']
    assert '/' not in failed['lines'][0] and '/missing/private' in failed['details'][0]


def test_help_card_button_opens_status_page_on_this_computer():
    c = {'action': '回答 2 件等你的活', 'reason': '没有能接着做的活', 'evidence': '等你 2 件', 'status_location': '等你'}
    button = help_card(c, 'delivery:test', 'http://127.0.0.1:8765/status.html')['body']['elements'][-1]
    assert button['text']['content'] == '看等你栏'
    assert button['behaviors'] == [{'type': 'open_url', 'default_url': 'http://127.0.0.1:8765/status.html#%E7%AD%89%E4%BD%A0'}]
    assert help_card(c, 'delivery:test')['body']['elements'][-1]['behaviors'][0]['type'] == 'callback'  # 没配地址退回回调


def build_history(tmp_path):
    """按旧版格式造 history：每行都是整条记录的快照。"""
    from workshop.core.managers import applied
    store = Store(tmp_path)
    for number in range(1, 6):
        row = store.observe('item:a', {'title': '交付检查', 'reason': str(number)}, 'core', NOW)
    row.update(state='blocked')
    store.save(row, 'observed')
    store.close()
    with sqlite3.connect(tmp_path / 'managers.db') as db:
        states, rows = {}, []
        for seq, ident, body in db.execute('SELECT seq,id,body FROM history ORDER BY seq').fetchall():
            value = json.loads(body)
            states[ident] = applied(states.get(ident), value)
            rows.append((seq, ident, json.dumps({'event': value['event'], 'record': states[ident]},
                                               ensure_ascii=False, sort_keys=True)))
        db.execute('DELETE FROM history')
        db.executemany('INSERT INTO history VALUES (?,?,?)', rows)
    return row


def test_compact_managers_rewrites_history_to_changes_and_keeps_records(tmp_path):
    from workshop.core.managers import compact, applied, HISTORY
    build_history(tmp_path)
    with sqlite3.connect(tmp_path / 'managers.db') as db:
        old = db.execute('SELECT count(*) FROM history').fetchone()[0]
        before_bytes = db.execute('SELECT sum(length(body)) FROM history').fetchone()[0]
    result = compact(tmp_path, now=datetime.fromisoformat(NOW))
    assert Path(result['backup']).exists() and result['rewritten'] == old
    assert result['after']['history_bytes'] < before_bytes
    with sqlite3.connect(tmp_path / 'managers.db') as db:
        bodies = [json.loads(r[0]) for r in db.execute('SELECT body FROM history ORDER BY seq')]
        assert all(b['format'] == HISTORY and 'record' not in b for b in bodies)
        # 变化记录按顺序接回去，和 concerns 里的当前记录完全一致。
        state = None
        for body in bodies:
            state = applied(state, body)
        concern = json.loads(db.execute("SELECT body FROM concerns WHERE id='item:a'").fetchone()[0])
        assert state == concern
    # 再跑一次是空的转换，事项照常能读能写。
    again = compact(tmp_path, now=datetime.fromisoformat(NOW))
    assert again['rewritten'] == 0 and again['after']['file_bytes'] <= result['after']['file_bytes']
    store = Store(tmp_path)
    try:
        assert [r['id'] for r in store.all()] == ['item:a']
        store.save(store.all()[0], 'observed')
    finally:
        store.close()


def test_compact_managers_waits_for_store_lock(tmp_path, monkeypatch):
    from workshop.core import managers as managers_module
    build_history(tmp_path)
    held = Store(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def hold():
        with held.lock() as acquired:
            assert acquired
            entered.set()
            release.wait(10)
    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(5)
    real = managers_module.fcntl.flock
    reached = threading.Event()
    def traced(descriptor, operation):
        reached.set()
        return real(descriptor, operation)
    monkeypatch.setattr(managers_module.fcntl, 'flock', traced)
    result = {}
    worker = threading.Thread(target=lambda: result.update(managers_module.compact(tmp_path, now=datetime.fromisoformat(NOW))))
    worker.start()
    assert reached.wait(5)
    assert worker.is_alive() and result == {}  # 守护进程持锁时不并发写
    release.set()
    holder.join(5)
    worker.join(10)
    assert Path(result['backup']).exists()
    held.close()


def test_compact_managers_cli_reports_sizes_and_missing(tmp_path, capsys):
    from workshop.cli import main
    build_history(tmp_path)
    assert main(['--state-dir', str(tmp_path), 'compact-managers']) == 0
    output = capsys.readouterr().out
    assert '已备份' in output and 'managers.db：' in output and 'history：' in output
    empty = tmp_path / 'empty'
    empty.mkdir()
    assert main(['--state-dir', str(empty), 'compact-managers']) == 0
    assert '没有 managers.db' in capsys.readouterr().out
