"""Manual Orca smoke test. Does not run during tests; coordinator explicitly invokes it."""
import argparse
from dataclasses import asdict
import json
import hashlib
from pathlib import Path
import sys

from workshop.core.models import Brief
from .adapters import Executor
from .storage import read, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['launch', 'stop', 'resume', 'receipt', 'event'])
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--checkout', type=Path)
    parser.add_argument('--model', default='claude-haiku-4-5')
    parser.add_argument('token', nargs='?')
    parser.add_argument('--file', type=Path)
    parser.add_argument('--json')
    args = parser.parse_args()
    directory = args.directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if args.action in ('receipt', 'event'):
        payload = json.loads(args.json) if args.json else read(args.file)
        write(directory / (args.action + '.json'), payload)
        print('手动假活的回执/事件已记到独立目录')
        return
    executor = Executor('orca-claude', state_dir=directory/'executors')
    record_path = directory/'smoke.json'
    if args.action == 'stop':
        record = read(record_path)
        executor.stop(record['token'], None)
        print('已确认进程退出；session=' + record['started']['session'])
        return
    if args.action == 'launch':
        if record_path.exists():
            raise SystemExit('目录已有实测记录；用 resume，或选一个新目录')
        if not args.checkout:
            parser.error('launch 需要 --checkout')
        key = 'executor-' + hashlib.sha256(str(directory).encode()).hexdigest()[:12]
        item = {'id': 'manual:' + key, 'target': 'manual', 'object_key': key}
        target = {'checkout': str(args.checkout.expanduser().resolve())}
        token = 'manual-launch'
        text = '手动机制实测：记住暗号蓝鲸-4721，只回复暗号与当前会话 UUID；不要改文件或对外发消息。'
    else:
        record = read(record_path)
        item, target = record['item'], record['target']
        item['workdir'] = record['started']['workdir']
        token = 'manual-resume-' + str(record.get('round', 0) + 1)
        text = '手动机制实测：回复之前记住的暗号与当前会话 UUID；不要改文件或对外发消息。'
    # Deliberate local sink: this is a fake item, no events/receipts enter production core state.
    target['runtime'] = {'state_dir': str(directory), 'command': [sys.executable, '-m',
        'workshop.plugins.executors.local.manual', '--directory', str(directory)],
        'env': {'WORKSHOP_STATE_DIR': str(directory), 'WORKSHOP_ROUND_TOKEN': token}}
    config = {'billing': 'subscription', 'model': args.model}
    if args.action == 'launch':
        started = executor.launch(token, Brief(text, []), item, config, target)
    else:
        started = executor.resume(token, record['started']['session'], Brief(text, []), item, config, target)
    write(record_path, {'token': token, 'started': asdict(started), 'item': item, 'target': target,
                        'round': 0 if args.action == 'launch' else record.get('round', 0)+1})
    print(json.dumps(asdict(started), ensure_ascii=False))


if __name__ == '__main__':
    main()
