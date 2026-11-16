#!/usr/bin/env python3
"""通过本机 Codex config/read 核对有效配置；不创建会话或调用模型。"""
import json
from pathlib import Path
import selectors
import shutil
import subprocess
from time import monotonic

import yaml
from workshop.plugins.executors.local.adapters import Executor


def main():
    root = Path(__file__).resolve().parents[1]
    profile = yaml.safe_load((root / 'rules/workshop.yaml').read_text())['executor_profiles']['watch-inventory']
    binary = shutil.which('codex')
    if not binary:
        raise SystemExit('需要本机 Codex CLI；不安装、不调用模型')
    executor = Executor('watch-inventory', profile=profile)
    argv = executor._argv(profile, str(root), 'config-check', False, root / '.local', False)
    overrides = []
    for index, value in enumerate(argv[:-1]):
        if value == '-c':
            overrides.extend(['-c', argv[index + 1]])
    process = subprocess.Popen([binary, *overrides, 'app-server', '--stdio'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True)

    def request(value):
        process.stdin.write(json.dumps(value) + '\n')
        process.stdin.flush()

    def response(identity):
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = monotonic() + 30
            while monotonic() < deadline:
                if not selector.select(max(0, deadline - monotonic())):
                    break
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError('Codex app-server 提前退出')
                value = json.loads(line)
                if value.get('id') == identity:
                    if 'error' in value:
                        raise RuntimeError('Codex config/read 失败')
                    return value['result']
            raise TimeoutError('Codex config/read 超时')

    try:
        request({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'workshop-config-check', 'version': '1'},
            'capabilities': {'experimentalApi': True}}})
        response(1)
        request({'method': 'initialized', 'params': {}})
        request({'id': 2, 'method': 'config/read', 'params': {'includeLayers': False, 'cwd': str(root)}})
        config = response(2)['config']
        assert config['developer_instructions'] == profile['prompt'], '只读提示词未生效'
        assert config['sandbox_workspace_write']['network_access'] is True, '网络设置未生效'
        # 只输出本单核对的字段，不输出用户配置中的令牌、MCP 配置或凭据。
        print(json.dumps({'codex_version': subprocess.check_output([binary, '--version'], text=True).strip(),
            'developer_instructions': config['developer_instructions'],
            'network_access': config['sandbox_workspace_write']['network_access'],
            'model_calls': 0}, ensure_ascii=False, indent=2))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()


if __name__ == '__main__':
    main()
