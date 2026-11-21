#!/usr/bin/env python3
"""发版时安装独立介入点环境及 LaunchAgent；失败保留原服务，检查阶段不调用。"""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import subprocess
import sys
import tempfile
import threading
import time

import yaml

from workshop.plugins.intervention.client import request

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'com.kun.workshop-intervention'


def command(argv, *, check=True):
    result = subprocess.run([str(v) for v in argv], capture_output=True, text=True, timeout=180)
    if check and result.returncode:
        raise RuntimeError(f'{argv[0]} 退出 {result.returncode}: {result.stderr[-1500:]}')
    return result


BOOTSTRAP_ATTEMPTS = 3
BOOTSTRAP_INTERVAL = 1.0


def sleep(seconds):
    """重试间隔；测试替换成不真等的替身（E8）。"""
    time.sleep(seconds)


def bootstrap(domain, path, *, attempts=BOOTSTRAP_ATTEMPTS):
    """launchctl bootstrap 偶发 I/O 错误（退 5），最多试三次；都不成才算这一步失败。"""
    if type(attempts) is not int or attempts < 1:
        raise ValueError('重试次数须为正整数')
    result = None
    for attempt in range(attempts):
        result = command(['/bin/launchctl', 'bootstrap', domain, str(path)], check=False)
        if not result.returncode:
            return result
        if attempt + 1 < attempts:
            sleep(BOOTSTRAP_INTERVAL)
    message = (result.stderr or result.stdout).strip()
    raise RuntimeError(f'launchctl bootstrap 连试 {attempts} 次仍退出 {result.returncode}: {message[-1500:]}')



def expand(value):
    # 发版准备命令在临时 HOME 中运行，现役安装位置属于当前系统用户。
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return (home / str(value)[2:] if str(value).startswith('~/') else Path(value)).resolve()


def private(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError(f'安装目录必须归当前用户且为私有目录：{path}')


def environment(state, rules):
    digest = hashlib.sha256()
    files = [ROOT / 'standalone/intervention/pyproject.toml', ROOT / 'src/workshop/wait_client.py',
             *sorted((ROOT / 'src/workshop/plugins/intervention').glob('*.py'))]
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    digest.update(str(Path(sys._base_executable).resolve()).encode())
    version = digest.hexdigest()
    directory = state / 'environments' / version
    if not (directory / 'installed.json').exists():
        directory.parent.mkdir(exist_ok=True)
        if directory.exists():
            raise ValueError('独立环境目录未完成，拒绝覆盖：' + str(directory))
        with tempfile.TemporaryDirectory(prefix='.build-', dir=directory.parent) as temporary:
            build = Path(temporary) / 'environment'
            command(['uv', 'venv', '--python', sys._base_executable, build])
            command(['uv', 'pip', 'install', '--python', build / 'bin/python', ROOT / 'standalone/intervention'])
            (build / 'installed.json').write_text(json.dumps({'source': version}))
            # 使用 python -m 启动，不依赖移动前写入的 console-script shebang。
            build.rename(directory)
    python = directory / 'bin/python'
    proof = command([python, '-I', '-c',
        'import importlib.util,json,sys; from pathlib import Path; '
        'from workshop.plugins.intervention.service import Service; '
        'assert importlib.util.find_spec("workshop.core") is None; '
        'service=Service.__new__(Service); service.rules=Path(sys.argv[1]); service.configuration(); '
        'print(json.dumps({"core_available":False,"rules_valid":True}))', rules])
    return python, {'source': version, **json.loads(proof.stdout)}


def ready(endpoint, rules, python, timeout=15):
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    error = '未就绪'
    expected = hashlib.sha256(rules.read_bytes()).hexdigest()
    while True:
        try:
            result = request(endpoint, 'ping', {}, timeout=min(1, max(.01, deadline - time.monotonic())))
            if result.get('rules') != str(rules) or result.get('rules_sha256') != expected:
                raise ValueError('介入点服务没有使用本次现役规则')
            if result.get('python') != str(python):
                raise ValueError('响应来自其他环境的介入点服务')
            return result
        except (OSError, ValueError) as exc:
            error = str(exc)
        if time.monotonic() >= deadline:
            raise RuntimeError('介入点服务未就绪：' + error)
        pause.wait(min(.05, max(0, deadline - time.monotonic())))


def search_path():
    """和 scripts/launch_agents.py 共用一份 PATH：送达命令要找得到 lark-cli（/opt/homebrew/bin）。"""
    spec = importlib.util.spec_from_file_location('launch_agents', ROOT / 'scripts/launch_agents.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # 发版在临时 HOME 里跑，PATH 按当前系统用户的家目录算。
    return module.search_path(Path(pwd.getpwuid(os.getuid()).pw_dir))


def activate(state, rules, endpoint, python, label, agents):
    domain = f'gui/{os.getuid()}'
    service = domain + '/' + label
    agents.mkdir(parents=True, exist_ok=True)
    path = agents / (label + '.plist')
    payload = plistlib.dumps({'Label': label,
        'ProgramArguments': [str(python), '-I', '-m', 'workshop.plugins.intervention',
            '--state-dir', str(state), '--rules', str(rules), '--endpoint', str(endpoint), 'serve'],
        'EnvironmentVariables': {'PATH': search_path()},
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 5,
        'StandardOutPath': str(state / 'service.log'), 'StandardErrorPath': str(state / 'service.log')})
    previous = path.read_bytes() if path.exists() else None
    loaded = command(['/bin/launchctl', 'print', service], check=False).returncode == 0
    if previous == payload and loaded:
        return {'action': 'unchanged', **ready(endpoint, rules, python)}
    if loaded:
        command(['/bin/launchctl', 'bootout', service])
    try:
        # 不留下半份 plist；失败时恢复旧文件与旧进程定义。
        with tempfile.NamedTemporaryFile(dir=agents, delete=False) as output:
            output.write(payload)
            temporary = Path(output.name)
        temporary.replace(path)
        bootstrap(domain, path)
        return {'action': 'installed', **ready(endpoint, rules, python)}
    except Exception:
        command(['/bin/launchctl', 'bootout', service], check=False)
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(previous)
            if loaded:
                bootstrap(domain, path)
        raise


def install(rules, state, agents, label=LABEL):
    if not re.fullmatch(r'com\.kun\.workshop-intervention(?:\.[a-zA-Z0-9-]+)?', label):
        raise ValueError('只能安装介入点服务或其隔离演练实例')
    rules, state, agents = expand(rules), expand(state), expand(agents)
    config = yaml.safe_load(rules.read_text())
    endpoint = expand(config['wait_services']['intervention']['endpoint'])
    private(state)
    private(endpoint.parent)
    with (state / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        python, proof = environment(state, rules)
        result = activate(state, rules, endpoint, python, label, agents)
        return {'label': label, 'state': str(state), 'endpoint': str(endpoint),
                'python': str(python), **proof, **result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rules', type=Path, required=True)
    parser.add_argument('--state-dir', default='~/.local/state/workshop-intervention')
    parser.add_argument('--launch-agents-dir', default='~/Library/LaunchAgents')
    parser.add_argument('--label', default=LABEL)
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.rules, args.state_dir, args.launch_agents_dir, args.label), ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
