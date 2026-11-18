"""授权中心的只读核实。只查询已持久保存的申请，绝不申请或重做动作。"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys


def target_key(target):
    return json.dumps(target, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def validate(spec):
    if not isinstance(spec, dict) or not all(isinstance(spec.get(k), str) and spec[k].strip()
            for k in ('request', 'action', 'provider_action', 'checkpoint')):
        raise ValueError('授权缺原申请号、动作、中心动作或检查点')
    if not isinstance(spec.get('target'), dict) or not spec['target']:
        raise ValueError('授权缺确定的目标')
    if spec.get('kind') not in ('exec', 'grant'):
        raise ValueError('授权 kind 必须为 exec 或 grant')
    return spec


def verify(spec, record):
    """批准与代执行完成分开；只支持能够核实绑定目标和生效结果的中心动作。"""
    validate(spec)
    evidence = {'request': spec['request'], 'action': spec['action'],
                'target': target_key(spec['target']), 'result': 'unknown', 'applied': False}
    if not isinstance(record, dict) or record.get('id') != spec['request']:
        return evidence
    status = record.get('status')
    if status in ('denied', 'rejected', 'expired', 'expired-unfetched', 'revoked', 'failed', 'error', 'grant_failed'):
        return {**evidence, 'result': status}
    if status in ('pending', 'decision_in_progress', 'approved-executing'):
        return {**evidence, 'result': 'pending'}
    result = record.get('result') or {}
    if (spec['kind'] == 'exec' and spec['provider_action'] == 'argocd/sync_app'
            and spec['action'] in ('集群同步', '有别人在用的交付对象上线')
            and record.get('broker') == 'argocd' and record.get('action') == 'sync_app'
            and status in ('completed', 'executed')
            and set(spec['target']) == {'instance', 'app', 'revision'}
            and all(result.get(k) == v for k, v in spec['target'].items())
            and result.get('operation_id') == spec['request'] and result.get('operation_matches') is True
            and result.get('operation_phase') == 'Succeeded' and result.get('dry_run') is False
            and result.get('submitted') is True
            and result.get('after', {}).get('revision') == spec['target']['revision']
            and result.get('after', {}).get('sync') == 'Synced' and result.get('after', {}).get('health') == 'Healthy'):
        return {**evidence, 'result': 'approved', 'applied': True}
    return evidence


def read(spec, client_path, *, url=None):
    """复用中心自己的身份客户端；输出只保留核实字段，凭据及任意执行输出不离开客户端。"""
    validate(spec)
    path = Path(client_path).expanduser().resolve(strict=True)
    module_spec = importlib.util.spec_from_file_location('_workshop_grant_reader', path)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    try:
        record = module.fetch_status(module.ConsoleClient.from_env(url=url), spec['request'], kind=spec['kind'], timeout_seconds=10)
        verified = verify(spec, record)
        result = record.get('result') or {}
        execution = {k: result[k] for k in ('instance', 'app', 'revision', 'operation_id', 'operation_matches', 'operation_phase', 'after') if k in result}
        return {'verification': verified, 'execution': execution, 'status': record.get('status')}
    except (OSError, ValueError, RuntimeError, SystemExit) as exc:
        # 错误可能含响应正文，不持久化凭据或任意服务端输出。
        return {'verification': verify(spec, {}), 'execution': {}, 'status': 'unknown', 'error': type(exc).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', required=True)
    parser.add_argument('--url')
    args = parser.parse_args()
    print(json.dumps(read(json.load(sys.stdin), args.client, url=args.url), ensure_ascii=False))


if __name__ == '__main__':
    main()
