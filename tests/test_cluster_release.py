"""grant-console 批次的来源、状态恢复与真实发版脚本调用契约；无外部写操作。"""
import base64
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from workshop.core.ports import Retryable
from workshop.plugins.scheduled_release import ScheduledRelease
from workshop.plugins.scheduled_release.cluster import ClusterRelease
from workshop.plugins.scheduled_release.cluster_worker import Runner, Failure
from workshop.plugins.scheduled_release.io import IO, read, write
from workshop.plugins.sources.main_branch import MainBranchSource
import workshop.plugins.sources.main_branch as source_module

A, B, C = 'a' * 40, 'b' * 40, 'c' * 40
P = 'd' * 40
DIGEST = 'sha256:' + 'e' * 64


@pytest.fixture
def cluster(tmp_path):
    target = {'name': 'grant-console', 'repo': 'kun-g/grant-console', 'checkout': str(tmp_path / 'main'),
              'scheduled_release_mode': 'cluster', 'scheduled_release_state_dir': str(tmp_path / 'state'),
              'tag_command': ['scripts/tag-release.sh'], 'prepare': [],
              'sources': {'main_branch': {'every': '1h'}},
              'deployment': {'context': 'central-admin-agent', 'namespace': 'argocd',
                             'app': 'grant-console-centaladmin',
                             'rollout': {'namespace': 'agent-ops', 'name': 'grant-console'}}}
    data = {'id': 'grant-console:scheduled_release:' + B, 'object_key': B,
            'materials': [{'source': 'main_branch', 'body': {'kind': 'ahead', 'from': A, 'to': B}}]}
    return target, data, IO(tmp_path / 'state')


def test_source_production_baseline_hour_slot_and_failures(cluster, monkeypatch):
    target, _, io = cluster
    now = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)
    source = MainBranchSource(lambda: now)
    called = []
    production = A
    def command(argv, **kwargs):
        called.append(argv)
        output = B + '\trefs/heads/main' if argv[0] == 'git' else json.dumps({'status': {'sync': {'status': 'Synced', 'revision': P}}}) if argv[0] == 'kubectl' else json.dumps({
            'content': base64.b64encode(json.dumps({'source_commit': production}).encode()).decode()})
        return subprocess.CompletedProcess(argv, 0, output, '')
    monkeypatch.setattr(source_module, 'run', command)
    first = source.poll(target, None)
    assert first.events[0].body == {'kind': 'ahead', 'from': A, 'to': B}
    assert source.poll(target, first.cursor).events == [] and len(called) == 3
    production = B
    now += timedelta(hours=1)
    assert source.poll(target, first.cursor).events == []
    monkeypatch.setattr(source_module, 'run', lambda a, **k: subprocess.CompletedProcess(a, 1, '', 'offline'))
    with pytest.raises(Retryable, match='offline'):
        source.poll(target, first.cursor)
    monkeypatch.setattr(source_module, 'run', lambda a, **k: subprocess.CompletedProcess(a, 0,
        B + '\trefs/heads/main' if a[0] == 'git' else '{"content":"e30="}', ''))
    with pytest.raises(Retryable, match='production'):
        source.poll(target, first.cursor)


@pytest.mark.parametrize('status', ['reserved', 'built', 'blocked', 'unknown'])
def test_unfinished_batch_suppresses_new_hour_without_network(cluster, monkeypatch, status):
    target, data, io = cluster
    # 默认流程收到完整对象配置，也能找到定时流程的独立目录。
    target['flows'] = {'issue_delivery': {}, 'scheduled_release': {
        'scheduled_release_mode': target.pop('scheduled_release_mode'),
        'scheduled_release_state_dir': target.pop('scheduled_release_state_dir')}}
    write(io.root(target) / 'cluster-release.json', {'item': data['id'], 'to': B, 'status': status})
    monkeypatch.setattr(source_module, 'run', lambda *a, **k: pytest.fail('未决批次不应查远端'))
    assert MainBranchSource().poll(target, None).events == []


@pytest.mark.parametrize('status', ['rejected', 'failed'])
def test_terminal_batch_skips_same_sha_but_new_merge_is_eligible(cluster, monkeypatch, status):
    target, data, io = cluster
    write(io.root(target) / 'cluster-release.json', {'item': data['id'], 'to': B, 'status': status})
    remote = B
    def command(argv, **kwargs):
        text = remote + '\trefs/heads/main' if argv[0] == 'git' else json.dumps({'status': {'sync': {'status': 'Synced', 'revision': P}}}) if argv[0] == 'kubectl' else json.dumps({
            'content': base64.b64encode(json.dumps({'source_commit': B}).encode()).decode()})
        return subprocess.CompletedProcess(argv, 0, text, '')
    monkeypatch.setattr(source_module, 'run', command)
    assert MainBranchSource().poll(target, None).events == []
    remote = C
    assert MainBranchSource().poll(target, None).events[0].body['to'] == C


def test_batch_claim_wait_restart_and_exactly_one_deploy(cluster, monkeypatch):
    target, data, io = cluster
    calls, finished = [], {}
    def worker(self, target, state, phase):
        calls.append((phase, state['to']))
        return finished.get(phase)
    monkeypatch.setattr(ClusterRelease, 'worker', worker)
    flow = ScheduledRelease(io)
    assert not flow.after(data, '接单', 'intake', target).pending
    assert read(io.root(target) / 'cluster-release.json')['status'] == 'reserved'
    assert flow.after(data, '证据', 'build', target).pending
    # 新的流程实例认回固定 B；不从 main 的新提交重新构造批次。
    finished['build'] = {'status': 'ok', 'tag': 'v-test', 'run_id': 1}
    flow = ScheduledRelease(io)
    assert not flow.after(data, '证据', 'build', target).pending
    assert flow.after(data, '交付', 'deploy', target).pending
    other = {**data, 'id': data['id'].replace(B, C), 'object_key': C,
             'materials': [{'source': 'main_branch', 'body': {'kind': 'ahead', 'from': B, 'to': C}}]}
    assert flow.after(other, '接单', 'intake-other', target).pending
    assert read(io.root(target) / 'cluster-release.json')['to'] == B
    finished['deploy'] = {'status': 'ok', 'production': P, 'grant': {'id': 'req-one'}}
    assert not flow.after(data, '交付', 'deploy', target).pending
    count = len(calls)
    assert not ScheduledRelease(io).after(data, '交付', 'deploy', target).pending
    assert len(calls) == count
    assert read(io.root(target) / 'cluster-release.json')['status'] == 'ok'
    assert not flow.after(other, '接单', 'intake-other', target).pending
    # 旧批次迟到的收尾不能覆盖新批次的占位。
    assert flow.after(data, '收尾', 'tail', target).pending
    assert read(io.root(target) / 'cluster-release.json')['to'] == C


@pytest.mark.parametrize('status', ['rejected', 'blocked', 'failed'])
def test_batch_failure_preserves_reason_and_does_not_repeat(cluster, monkeypatch, status):
    target, data, io = cluster
    def worker(self, target, state, phase):
        if phase == 'build':
            return {'status': 'ok'}
        return {'status': status, 'reason': 'request req-one; log /evidence/grant.log'}
    monkeypatch.setattr(ClusterRelease, 'worker', worker)
    flow = ScheduledRelease(io)
    flow.after(data, '证据', 'build', target)
    result = flow.after(data, '交付', 'deploy', target)
    assert bool(result.blocked) == (status == 'blocked')
    assert 'req-one' in (result.blocked or result.notice)
    assert read(io.root(target) / 'cluster-release.json')['status'] == status
    assert flow.after(data, '交付', 'deploy', target) == result
    assert not (io.root(target) / 'release.json').exists()


def test_worker_lock_survives_new_adapter_and_lost_process_is_not_restarted(cluster, monkeypatch):
    target, data, io = cluster
    directory = io.root(target) / 'batches' / hashlib.sha256(data['id'].encode()).hexdigest() / 'deploy'
    directory.mkdir(parents=True)
    state = {'item': data['id'], 'to': B, 'from': A, 'built': {'status': 'ok'}}
    write(directory / 'spec.json', {'already': 'submitted'})
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('禁止重复启动'))
    with (directory / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert ClusterRelease(io).worker(target, state, 'deploy') is None
        write(directory / 'result.json', {'status': 'ok', 'production': P})
        assert ClusterRelease(io).worker(target, state, 'deploy') is None
    assert ClusterRelease(io).worker(target, state, 'deploy')['status'] == 'ok'
    (directory / 'result.json').unlink()
    assert ClusterRelease(io).worker(target, state, 'deploy')['status'] == 'blocked'


class ScriptRunner(Runner):
    """模拟脚本契约，真实运行生产编排与发布记录校验。"""
    def __init__(self, spec, directory):
        super().__init__(spec, directory)
        self.commands = []
        self.sync_exit = 0
        self.server_status = 'pending'
        self.exec_request = False
        self.ci = 'success'
        self.wrong_artifact = False
        self.build_attempt = 2
        self.argo_sync = 'Synced'

    def command(self, argv, **kwargs):
        self.commands.append(argv)
        output = ''
        exit_code = 0
        if argv[0] == 'git':
            if 'ls-remote' in argv:
                output = f'{B}\trefs/tags/v-test^{{}}\n' if '--tags' in argv else P + '\trefs/heads/production\n'
            elif 'rev-parse' in argv:
                output = B
        elif argv[:2] == ['gh', 'api']:
            output = json.dumps({'workflow_runs': [{'id': 42, 'run_attempt': 2, 'head_sha': B,
                'head_branch': 'v-test', 'event': 'push', 'status': 'completed', 'conclusion': self.ci}]})
        elif argv[:3] == ['gh', 'run', 'download']:
            dest = Path(argv[argv.index('-D') + 1])
            url = 'https://github.com/kun-g/grant-console/actions/runs/42/attempts/2'
            write(dest / 'publish.json', {'release': {'source_commit': C if self.wrong_artifact else B,
                'source_repository': 'kun-g/grant-console', 'build_run_url': url.rsplit('/', 1)[0] + '/' + str(self.build_attempt), 'image_digest': DIGEST},
                'publish_run_url': url, 'destination_digest': DIGEST, 'destination': 'ecr-test'})
        elif argv[0] == 'scripts/push-production.sh':
            output = 'production -> ' + P
        elif argv[0] == 'scripts/sync-production.sh':
            exit_code = self.sync_exit
            grant_dir = self.directory / 'sync-production.test'
            grant_dir.mkdir()
            (grant_dir / 'grant.log').write_text('exec exec-one status=pending\n' if self.exec_request else 'request req-one status=pending\n')
        elif 'scripts/request_grant.py' in argv:
            assert argv[2:] == (['status', 'exec-one', '--kind', 'exec'] if self.exec_request else ['status', 'req-one'])
            output = json.dumps({'status': self.server_status})
        elif argv[0] == 'kubectl' and 'application' in argv:
            output = json.dumps({'status': {'sync': {'status': self.argo_sync, 'revision': P},
                                           'health': {'status': 'Healthy'}}})
        return {'argv': argv, 'exit_code': exit_code, 'stdout': output, 'stderr': ''}


@pytest.fixture
def runner(cluster, tmp_path):
    target, data, _ = cluster
    directory = tmp_path / 'batch' / 'deploy'
    directory.mkdir(parents=True)
    spec = {'item': data['id'], 'to': B, 'from': A, 'target': target, 'phase': 'deploy',
            'built': {'tag': 'v-test', 'run_id': 42, 'attempt': 2}}
    return ScriptRunner(spec, directory)


def test_worker_runbook_sequence_and_success_evidence(runner):
    result = runner.deploy()
    assert result['status'] == 'ok' and result['production'] == P and result['grant']['id'] == 'req-one'
    commands = runner.commands
    download = next(i for i, a in enumerate(commands) if a[:3] == ['gh', 'run', 'download'])
    render = next(i for i, a in enumerate(commands) if 'scripts/release.py' in a)
    push = next(i for i, a in enumerate(commands) if a[0] == 'scripts/push-production.sh')
    sync = next(i for i, a in enumerate(commands) if a[0] == 'scripts/sync-production.sh')
    assert download < render < push < sync
    assert commands[sync][1:4] == [P, '--require-exec', '--session-id']
    assert commands[sync][4] == runner.spec['item']
    approval = read(runner.directory / 'approval.json')
    assert approval['approval_reference'] == 'policy:cluster-card:2026-11-13'
    assert approval['source_commit'] == B
    assert not any('reset' in a or 'delete' in a or 'apply' in a for a in commands)


@pytest.mark.parametrize('status,expected', [('rejected', 'rejected'), ('denied', 'rejected'),
    ('expired', 'rejected'), ('pending', 'awaiting_grant'), ('decision_in_progress', 'awaiting_grant'), ('granted', 'blocked'), ('failed', 'blocked')])
def test_exit_three_is_not_assumed_to_mean_expired(runner, status, expected):
    runner.sync_exit, runner.server_status = 3, status
    result = runner.deploy()
    assert result['status'] == expected and result['grant']['id'] == 'req-one'
    assert sum(a[0] == 'scripts/sync-production.sh' for a in runner.commands) == 1


@pytest.mark.parametrize('exit_code', [4, 5, 6, 7, 8, 9, 124])
def test_sync_and_rollout_failures_block_without_rollback(runner, exit_code):
    runner.sync_exit = exit_code
    result = runner.deploy()
    assert result['status'] == 'blocked'
    assert result['grant']['log'].endswith('grant.log')
    assert not any('scripts/request_grant.py' in a for a in runner.commands)
    assert not any('reset' in a or 'rollback' in a for a in runner.commands)


def test_wrong_artifact_never_pushes_or_requests_a_grant(runner):
    runner.wrong_artifact = True
    with pytest.raises(Failure, match='发布记录'):
        runner.deploy()
    assert not any(a[0].startswith('scripts/') for a in runner.commands)


def test_ci_failure_never_reaches_production(runner):
    runner.ci = 'failure'
    with pytest.raises(Failure, match='tag CI'):
        runner.build()
    assert not any(a[0] in ('scripts/push-production.sh', 'scripts/sync-production.sh') for a in runner.commands)


def test_ci_success_binds_run_attempt_and_existing_tag(runner):
    built = runner.build()
    assert built['tag'] == 'v-test' and built['run_id'] == 42 and built['attempt'] == 2
    assert not any(a[0] == 'scripts/tag-release.sh' for a in runner.commands)


def test_command_preserves_raw_streams_and_nonzero_exit(cluster, tmp_path):
    target, data, _ = cluster
    runner = Runner({'target': target, 'to': B}, tmp_path)
    result = runner.command([sys.executable, '-c', 'import sys; print("out"); print("err", file=sys.stderr); sys.exit(7)'], check=False)
    assert (result['exit_code'], result['stdout'], result['stderr']) == (7, 'out\n', 'err\n')
    assert read(tmp_path / 'runs.json') == [result]


def test_rerun_failed_publish_accepts_successful_earlier_build(runner):
    runner.build_attempt = 1
    assert runner.deploy()['status'] == 'ok'


def test_artifact_from_future_build_attempt_is_rejected(runner):
    runner.build_attempt = 3
    with pytest.raises(Failure, match='发布记录'):
        runner.deploy()
    assert not any(a[0] == 'scripts/push-production.sh' for a in runner.commands)


def test_timed_out_pending_grant_only_polls_existing_request(cluster, monkeypatch):
    target, data, io = cluster
    def worker(self, target, state, phase):
        if phase == 'build':
            return {'status': 'ok', 'checkout': '/isolated/checkout'}
        return {'status': 'awaiting_grant', 'reason': 'local timeout; req-one',
                'grant': {'id': 'req-one', 'log': '/evidence/grant.log'}}
    monkeypatch.setattr(ClusterRelease, 'worker', worker)
    flow = ScheduledRelease(io)
    flow.after(data, '证据', 'build', target)
    assert flow.after(data, '交付', 'deploy', target).pending
    monkeypatch.setattr(ClusterRelease, 'worker', lambda *a: pytest.fail('不能重启同步程序'))
    status, calls = 'decision_in_progress', []
    def command(argv, **kwargs):
        calls.append(argv)
        return {'argv': argv, 'exit_code': 0, 'stdout': json.dumps({'status': status}), 'stderr': ''}
    monkeypatch.setattr(io, 'command', command)
    assert ScheduledRelease(io).after(data, '交付', 'deploy', target).pending
    status = 'expired'
    result = ScheduledRelease(io).after(data, '交付', 'deploy', target)
    assert not result.pending and not result.blocked
    assert read(io.root(target) / 'cluster-release.json')['status'] == 'rejected'
    assert all(argv[2:] == ['status', 'req-one'] for argv in calls)


def test_real_worker_survives_adapter_recreation_and_holds_inherited_lock(cluster, tmp_path, monkeypatch):
    import socket
    target, data, io = cluster
    commands = tmp_path / 'bin'
    commands.mkdir()
    script = '''#!PYTHON
import json, os, pathlib, socket, sys
args = sys.argv[1:]
if pathlib.Path(sys.argv[0]).name == 'git':
    if 'clone' in args:
        pathlib.Path(args[-1]).mkdir()
    if 'ls-remote' in args:
        print('SHA\\trefs/tags/v-test^{}')
else:
    with socket.create_connection(('127.0.0.1', int(os.environ['BATCH_GATE_PORT'])), timeout=10) as client:
        client.sendall(b'ready')
        assert client.recv(2) == b'go'
    print(json.dumps({'workflow_runs': [{'id': 42, 'run_attempt': 1, 'head_sha': 'SHA',
      'head_branch': 'v-test', 'event': 'push', 'status': 'completed', 'conclusion': 'success'}]}))
'''.replace('PYTHON', sys.executable).replace('SHA', B)
    for name in ('git', 'gh'):
        (commands / name).write_text(script)
        (commands / name).chmod(0o755)
    import os
    monkeypatch.setenv('PATH', str(commands) + os.pathsep + os.environ['PATH'])
    launched = []
    original = subprocess.Popen
    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        launched.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        server.settimeout(10)
        monkeypatch.setenv('BATCH_GATE_PORT', str(server.getsockname()[1]))
        state = {'item': data['id'], 'from': A, 'to': B}
        try:
            assert ClusterRelease(io).worker(target, state, 'build') is None
            client, _ = server.accept()
            with client:
                assert client.recv(5) == b'ready'
                assert ClusterRelease(io).worker(target, state, 'build') is None
                assert len(launched) == 1
                client.sendall(b'go')
            assert launched[0].wait(timeout=10) == 0
            built = ClusterRelease(io).worker(target, state, 'build')
            assert built['status'] == 'ok' and built['tag'] == 'v-test'
            assert len(launched) == 1
        finally:
            for child in launched:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)


def test_initial_enable_refuses_old_unconfirmed_production(runner):
    runner.spec['initial'] = True
    runner.argo_sync = 'OutOfSync'
    with pytest.raises(Failure, match='首次切换前旧版本'):
        runner.deploy()
    assert not any(a[0] in ('scripts/push-production.sh', 'scripts/sync-production.sh') for a in runner.commands)


def test_lost_worker_reports_existing_grant_id(cluster, monkeypatch):
    target, data, io = cluster
    directory = io.root(target) / 'batches' / hashlib.sha256(data['id'].encode()).hexdigest() / 'deploy'
    write(directory / 'spec.json', {'phase': 'deploy'})
    evidence = directory / 'sync-production.one'
    evidence.mkdir()
    (evidence / 'grant.log').write_text('request req-preserved status=pending\n')
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('不得重复发卡'))
    result = ClusterRelease(io).worker(target, {'item': data['id']}, 'deploy')
    assert result['status'] == 'blocked' and result['grant']['id'] == 'req-preserved'
    assert result['grant']['log'] == str(evidence / 'grant.log')


@pytest.mark.parametrize('files,release', [
    ([{'filename': 'docs/release.txt'}, {'filename': 'services/console/tests/test_app.py'}, {'filename': 'README.md'}], False),
    ([{'filename': 'src/service.py'}, {'filename': 'tests/test_app.py'}], True),
    ([{'filename': 'docs/old.py', 'status': 'renamed', 'previous_filename': 'src/old.py'}], True),
    ([{'filename': 'tests/test_app.py'}] * 300, True),
])
def test_cumulative_changes_use_actual_deployment_and_rule_paths(cluster, monkeypatch, files, release):
    target, _, _ = cluster
    target['release_ignored_paths'] = ['docs/', '**/tests/', '*.md']
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        if argv[0] == 'git':
            output = B + '\trefs/heads/main'
        elif argv[0] == 'kubectl':
            # Desired production has already advanced, but was never deployed.
            output = json.dumps({'status': {'sync': {'status': 'OutOfSync', 'revision': C},
                                           'history': [{'id': 3, 'revision': P}]}})
        elif '/contents/' in argv[-1]:
            assert argv[-1].endswith('?ref=' + P)
            output = json.dumps({'content': base64.b64encode(json.dumps({'source_commit': A}).encode()).decode()})
        else:
            assert argv[-1].endswith('/compare/' + A + '...' + B)
            output = json.dumps({'status': 'ahead', 'files': files})
        return subprocess.CompletedProcess(argv, 0, output, '')
    monkeypatch.setattr(source_module, 'run', command)
    poll = MainBranchSource().poll(target, None)
    assert bool(poll.events) is release
    assert len(calls) == 4


def test_exec_log_recognizes_request_kind_and_uncertain_does_not_restart(cluster, monkeypatch):
    from workshop.plugins.scheduled_release.io import grant_record
    target, data, io = cluster
    directory = io.root(target) / 'sync-production.test'
    directory.mkdir(parents=True)
    (directory / 'grant.log').write_text('exec exec-one status=pending\nexec exec-one status=uncertain\n')
    grant = grant_record(io.root(target))
    assert grant['id'] == 'exec-one' and grant['kind'] == 'exec'
    state = {'built': {'checkout': '/checkout'}, 'result': {'grant': grant}}
    calls = []
    status = 'approved-executing'
    def command(argv, **kwargs):
        calls.append(argv)
        return dict(exit_code=0, stdout=json.dumps({'status': status}), stderr='')
    monkeypatch.setattr(io, 'command', command)
    controller = ClusterRelease(io)
    assert controller.grant_outcome(state).pending
    status = 'uncertain'
    assert controller.grant_outcome(state).blocked
    assert state['status'] == 'blocked'
    assert all(argv[2:] == ['status', 'exec-one', '--kind', 'exec'] for argv in calls)


@pytest.mark.parametrize('status,expected', [('approved-executing', 'awaiting_grant'), ('completed', 'blocked'), ('uncertain', 'blocked'), ('failed', 'blocked')])
def test_exec_wait_after_script_exit_never_repeats_sync(runner, status, expected):
    runner.exec_request, runner.sync_exit, runner.server_status = True, 3, status
    result = runner.deploy()
    assert result['grant']['kind'] == 'exec' and result['grant']['id'] == 'exec-one'
    assert result['status'] == expected
    assert sum(a[0] == 'scripts/sync-production.sh' for a in runner.commands) == 1
