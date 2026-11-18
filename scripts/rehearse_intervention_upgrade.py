#!/usr/bin/env python3
"""隔离演练：从不认识 release_prepare 的旧版升级并首次安装介入点服务。

uv run python scripts/rehearse_intervention_upgrade.py --from-commit <旧版提交> --output /tmp/upgrade.json
真实守护进程、作业、裸仓库、检查和 launchd；仅操作临时目录及唯一后缀服务名，结束时卸载。
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--from-commit', required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix='w173-up-', dir='/tmp')).resolve()
    home, checkout, state = (root / name for name in ('home', 'checkout', 'state'))
    for directory in (home, state, root / 'bin'):
        directory.mkdir()
    # 看门的错误路径也不能触及现役 launchd。Git 和 Python 仍用真实程序。
    guard = root / 'bin' / 'launchctl'
    guard.write_text('#!/bin/sh\necho "isolated rehearsal: launchctl forbidden" >&2\nexit 1\n')
    guard.chmod(0o755)
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TMPDIR') if k in os.environ}
    env.update(HOME=str(home), PATH=str(root / 'bin') + ':' + env['PATH'],
               PYTHONPATH=str(checkout / 'src'), WORKSHOP_STATE_DIR=str(state),
               WORKSHOP_ASK_KUN='/usr/bin/false', GIT_CONFIG_NOSYSTEM='1',
               UV_PYTHON=sys.executable, UV_PYTHON_DOWNLOADS='never')
    def run(argv, cwd=None, check=True):
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
        if check and result.returncode:
            raise RuntimeError(f'{argv}: {result.stdout}\n{result.stderr}')
        return result
    def git(*argv):
        return run(['git', '-C', str(checkout), *argv]).stdout.strip()
    label = 'com.kun.workshop-intervention.replay-' + str(os.getpid())
    records = []
    processes, streams = [], []
    observed, watch_runs = [], []
    pause = threading.Event()
    try:
        head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        run(['git', 'clone', '--no-hardlinks', '--quiet', str(source), str(checkout)])
        git('checkout', '-B', 'main', head)
        git('config', 'user.name', 'Isolated rehearsal')
        git('config', 'user.email', 'rehearsal@localhost')
        remote = root / 'remote.git'
        run(['git', 'clone', '--bare', '--quiet', str(checkout), str(remote)])
        run(['git', 'config', '--global', 'url.' + remote.as_uri() + '.insteadOf',
             'https://github.com/rehearsal/workshop.git'])
        config = {
            'targets': {'rehearsal': {'repo': 'rehearsal/workshop', 'checkout': str(checkout),
                'default_flow': 'scheduled_release',
                'flows': {'scheduled_release': {'stages': ['证据', '交付', '验收', '收尾'],
                    'scheduled_release_state_dir': str(state),
                    'triggers': [{'source': 'main_branch', 'when': {'kind': 'ahead'}, 'action': 'ready'}]}},
                'sources': {'main_branch': {'every': '1h'}}, 'prepare': [],
                'github_identity': {'mode': 'inherit'}, 'check_timeout': 300}},
            'executors': {k: {'use': 'codex', 'family': 'codex', 'billing': 'subscription'}
                          for k in ('writer', 'reviewer', 'acceptor')},
            'limits': {'concurrent': 2, 'chain_depth': 8}, 'selector': 'ordered',
            'channel': 'ask_kun', 'notices': 'hold', 'ask_first': [], 'skip_ask': []}
        rules = root / 'rules.yaml'
        rules.write_text(yaml.safe_dump(config, allow_unicode=True))
        command = [sys.executable, '-m', 'workshop.cli', '--state-dir', str(state), '--rules', str(rules)]
        def start():
            stream = (root / f'daemon-{len(processes)}.log').open('w')
            streams.append(stream)
            process = subprocess.Popen([*command, 'run'], cwd=checkout, env=env, stdout=stream, stderr=subprocess.STDOUT)
            processes.append(process)
            return process
        def read(name):
            path = state / name
            return json.loads(path.read_text()) if path.exists() else {}
        def until(predicate, timeout):
            deadline = time.monotonic() + timeout
            while not predicate():
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'演练超时，日志目录 {root}')
                pause.wait(.05)
        baseline = args.from_commit
        current_rules = yaml.safe_load((source / 'rules/workshop.yaml').read_text())
        config['intervention'] = current_rules['intervention']
        config['wait_services'] = current_rules['wait_services']
        config['wait_services']['intervention']['endpoint'] = str(root / 'run/service.sock')
        rules.write_text(yaml.safe_dump(config, allow_unicode=True))
        git('reset', '--hard', baseline)
        old = start()
        until(lambda: bool(read('heartbeat.json')), 20)
        target = head
        run(['git', '--git-dir', str(remote), 'update-ref', 'refs/heads/main', target])
        run([*command, 'release-now', 'rehearsal'])
        deadline, next_watch = time.monotonic() + 300, 0
        new = None
        while True:
            record = read('release.json')
            if record and (not observed or observed[-1]['status'] != record['status']):
                observed.append({'observed_at': datetime.now(timezone.utc).isoformat(), **record})
            if record.get('status') in ('rolling_back', 'rolled_back', 'rollback_failed'):
                raise AssertionError(record)
            if time.monotonic() >= next_watch:
                watcher = run([sys.executable, '-m', 'workshop.watch', '--state-dir', str(state),
                               '--ask-kun', '/usr/bin/false', '--attention', '/usr/bin/false'], check=False)
                watch_runs.append(dict(exit_code=watcher.returncode, stdout=watcher.stdout, stderr=watcher.stderr))
                assert watcher.returncode == 0, watch_runs[-1]
                next_watch = time.monotonic() + .5
            if old.poll() is not None and new is None:
                assert old.returncode == 0, old.returncode
                assert record.get('status') == 'restarting', record
                config['targets']['rehearsal']['release_prepare'] = [{'argv': ['uv', 'run', 'python', 'scripts/install_intervention.py', '--rules', str(rules), '--state-dir', str(root / 'service'), '--launch-agents-dir', str(root / 'agents'), '--label', label]}]
                rules.write_text(yaml.safe_dump(config, allow_unicode=True))
                new = start()
            if record.get('status') == 'ok':
                break
            assert time.monotonic() < deadline, f'演练超时，日志目录 {root}'
            if record.get('status') == 'restarting':
                assert (datetime.now(timezone.utc) - datetime.fromisoformat(record['at'])).total_seconds() < 150
            pause.wait(.05)
        process = read('heartbeat.json')['process']
        start_time = run(['/bin/ps', '-p', str(new.pid), '-o', 'lstart=']).stdout.strip()
        seconds = (datetime.fromisoformat(record['confirmed_at']) - datetime.fromisoformat(record['at'])).total_seconds()
        assert seconds < 180
        assert record['confirmed_daemon'] == process
        assert process['pid'] == new.pid and process['start'] == start_time
        assert record['to'] == target and process['version'] == target
        from workshop.plugins.intervention.client import request
        ping = request(root / 'run/service.sock', 'ping', {})
        assert ping['rules'] == str(rules)
        preparation = next(json.loads(p.read_text()) for p in (state / 'scheduled_release').glob('*.json') if json.loads(p.read_text()).get('to') == target)
        records = list(preparation['release_prepare_runs'].values())
        assert len(records) == 1 and records[0]['exit_code'] == 0
        assert json.loads(records[0]['stdout'])['core_available'] is False
        assert not read('watch.json').get('active')
        result = dict(pr_head=head, baseline=baseline, trigger=target, service=ping, runs=records, old_code_did_not_install=True, directory=str(root),
                      restarting_to_ok_seconds=seconds, old_pid=old.pid, new_pid=new.pid,
                      process_start=start_time, confirmed_daemon=record['confirmed_daemon'],
                      first_ok_at=record['first_ok_at'], rollback=False, watch_runs=watch_runs,
                      transitions=observed)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        subprocess.run(['/bin/launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], capture_output=True)
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
        for stream in streams:
            stream.close()
        print(f'演练目录：{root}', file=sys.stderr)


if __name__ == '__main__':
    main()
