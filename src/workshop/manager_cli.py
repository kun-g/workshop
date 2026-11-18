"""独立项目经理入口；看门只执行此命令，不装配插件。"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from .core.jobs import Jobs, execute
from .core.log import Log
from .core.managers import Store, Coordinator, observe_items, observe_failures
from .core.rules import Rules
from .registry import Registry


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--rules', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    tick = commands.add_parser('tick')
    tick.add_argument('--file', type=Path, required=True)
    receipt = commands.add_parser('receipt')
    receipt.add_argument('token')
    receipt.add_argument('--file', type=Path, required=True)
    commands.add_parser('job').add_argument('directory', type=Path)
    args = parser.parse_args(argv)
    args.state_dir = args.state_dir.expanduser().resolve()
    args.rules = args.rules.expanduser().resolve()
    if args.command == 'receipt':
        store = Store(args.state_dir)
        try:
            print(json.dumps(store.receipt(args.token, json.loads(args.file.read_text())), ensure_ascii=False))
            return 0
        finally:
            store.close()
    registry = Registry()
    rules = registry.configure(Rules.load(args.rules))
    if args.command == 'job':
        execute(args.directory, registry)
        return 0
    if not rules.data.get('managers'):
        raise ValueError('项目经理未配置')
    command = [sys.executable, '-m', 'workshop.manager_cli', '--state-dir', str(args.state_dir), '--rules', str(args.rules)]
    jobs = Jobs(args.state_dir / 'manager-jobs', [*command, 'job'])
    store = Store(args.state_dir)
    try:
        with store.lock() as acquired:
            if not acquired:
                return 0
            message = json.loads(args.file.read_text())
            now = message['at']
            for key, fault in message.get('faults', {}).items():
                store.observe('system:' + key, fault, 'watch', now)
            for row in store.all():
                if row['id'].startswith('system:') and row['id'][7:] not in message.get('faults', {}):
                    store.resolve(row, now)
            business = message.get('business', {})
            idle = business.get('idle_since')
            if business.get('total') == 0 and idle and (datetime.fromisoformat(now) - datetime.fromisoformat(idle)).total_seconds() >= 300:
                store.observe('idle', {'idle_since': idle, 'reason': '业务连续五分钟为零'}, 'watch', now)
            elif business.get('total', 0) > 0:
                for row in store.all():
                    if row['id'] == 'idle':
                        store.resolve(row, now)
            if message.get('takeover'):
                try:
                    log = Log.readonly(args.state_dir / 'workshop.db')
                    try:
                        observe_items(store, log.open_items(), 'watch', now)
                        observe_failures(store, log, 'watch', now)
                    finally:
                        log.close()
                except Exception as exc:
                    store.observe('system:database', {'reason': str(exc)}, 'watch', now)
            # 同一个作业目录和令牌供双方认回，不会再启动一次。
            coordinator = Coordinator(store, rules, registry, jobs, command, now)
            coordinator.run(business)
            print(json.dumps({'managers': store.all()}, ensure_ascii=False))
        return 0
    finally:
        jobs.launcher.shutdown(wait=True)
        store.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
