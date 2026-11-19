#!/usr/bin/env python3
"""真实 launchd 隔离安装验收；仅操作随机名称和临时目录，退出时卸载该实例。"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid

import yaml

from workshop.core.log import Log
from workshop.plugins.intervention.client import request
from workshop.plugins.issue_delivery.io import clean_env

ROOT = Path(__file__).resolve().parents[1]


def verify(output):
    root = Path(tempfile.mkdtemp(prefix='w173-', dir='/tmp')).resolve()
    home = root / 'home'
    home.mkdir()
    label = 'com.kun.workshop-intervention.replay-' + uuid.uuid4().hex[:8]
    endpoint, state, agents, rules = root / 'run/service.sock', root / 'service', root / 'agents', root / 'rules.yaml'
    config = yaml.safe_load((ROOT / 'rules/workshop.yaml').read_text())
    config['wait_services']['intervention']['endpoint'] = str(endpoint)
    config['targets']['workshop']['checkout'] = str(ROOT)
    rules.write_text(yaml.safe_dump(config, allow_unicode=True))
    env = clean_env(home, ROOT / '.local/intervention-install-cache')
    env.update(UV_PYTHON=str(Path(sys._base_executable).resolve()), UV_PYTHON_DOWNLOADS='never')
    records = []

    def run(argv, check=True):
        argv = [str(a) for a in argv]
        result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
        row = {'argv': argv, 'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
        records.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if check:
            result.check_returncode()
        return result

    argv = ['uv', 'run', 'python', ROOT / 'scripts/install_intervention.py', '--rules', rules,
            '--state-dir', state, '--launch-agents-dir', agents, '--label', label]
    try:
        first = json.loads(run(argv).stdout)
        second = json.loads(run(argv).stdout)
        assert second['action'] == 'unchanged' and first['pid'] == second['pid']
        # #174：生成的 plist 带 PATH，送达命令找得到 lark-cli；原样贴出。
        plist = (agents / (label + '.plist')).read_text()
        print(json.dumps({'plist': str(agents / (label + '.plist')), 'content': plist}, ensure_ascii=False), flush=True)
        path = plistlib.loads(plist.encode())['EnvironmentVariables']['PATH']
        assert shutil.which('lark-cli', path=path), path
        proof = run([first['python'], '-I', '-c', 'import importlib.util,json; '
            'print(json.dumps({"core_available":importlib.util.find_spec("workshop.core") is not None}))'])
        assert json.loads(proof.stdout)['core_available'] is False and not (root / 'core').exists()
        content = {'key': 'installation', 'version': '1', 'stage': '执行', 'question': '隔离安装验证',
                   'reason': '核对重启后记录保留', 'recommend': '保持等待'}
        ref = request(endpoint, 'create', {'operation': 'replay-create', 'point': 'decision',
                                         'owner': 'replay:173', 'content': content})['ref']
        run(['/bin/launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/{label}'])
        third = json.loads(run(argv).stdout)
        record = request(endpoint, 'get', {'ref': ref})
        assert third['pid'] != first['pid'] and record['content'] == content and record['answer'] is None
        with sqlite3.connect((state / 'intervention.db').as_uri() + '?mode=ro', uri=True) as db:
            sends = [json.loads(row[0]) for row in db.execute("SELECT body FROM records WHERE kind='delivery'")]
        assert sends == []
        print(json.dumps({'record_survived': ref, 'old_pid': first['pid'], 'new_pid': third['pid'],
                          'sends': sends}, ensure_ascii=False), flush=True)
        # 只读检查需要核心侧状态；这里构造隔离夹具，不把它冒充现役心跳。
        core = root / 'core'
        log = Log(core / 'workshop.db')
        log.append('control', {'id': '@rules', 'path': str(rules)})
        log.close()
        head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        now = datetime.now(timezone.utc).isoformat()
        (core / 'release.json').write_text(json.dumps({'status': 'ok', 'confirmed_at': now, 'at': now, 'to': head}))
        (core / 'heartbeat.json').write_text(json.dumps({'at': now, 'process': {'pid': os.getpid(), 'version': head}}))
        run(['uv', 'run', 'python', ROOT / 'scripts/verify_intervention_live.py', 'service', '--state-dir', core])
    finally:
        run(['/bin/launchctl', 'bootout', f'gui/{os.getuid()}/{label}'], check=False)
        output.write_text(json.dumps({'directory': str(root), 'runs': records}, ensure_ascii=False, indent=2))
        print('隔离证据目录：' + str(root), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    verify(parser.parse_args().output)
