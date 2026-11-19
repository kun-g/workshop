#!/usr/bin/env python3
"""用规则里的真实身份读关系，只在全新本地状态目录运行核心准入，不启动执行者。"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import time

from workshop.core.engine import Engine
from workshop.core.log import Log
from workshop.core.models import Event, STAGES
from workshop.core.relations import refresh, admit
from workshop.reports.report import status, why
from workshop.deployment import assemble
from workshop.registry import Registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rules', type=Path, default=Path('rules/workshop.yaml'))
    parser.add_argument('--target', default='workshop')
    parser.add_argument('--objects', nargs='+', default=['168', '181', '182', '183'])
    parser.add_argument('--stages', nargs='+', choices=STAGES, default=['接单'],
                        help='在隔离库中设置各阶段并实测准入，不代表执行或完成这些阶段')
    parser.add_argument('--output', type=Path, required=True, help='必须是不存在的新目录；不使用生产状态库')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rules, plugins = assemble(args.rules.resolve()).rules, Registry()
    with closing(Log(args.output / 'workshop.db')) as log:
        engine = Engine(log, rules, plugins)
        for key in args.objects:
            engine.ingest(Event('readonly-relation-check', f'probe:{key}', args.target, key,
                                f'真实关系探针 #{key}', {'mode': '只读，不启动执行者'}, action='ready'), directed=True)
        assert len(log.open_items()) == len(args.objects), '探针对象未全部进入独立状态库'
        started = time.monotonic()
        refresh(engine, log.open_items())
        admissions = []
        for stage in args.stages:
            for item in log.open_items():
                engine.change(item, stage=stage, next=stage)
                item = log.get('items', item['id'])
                allowed = admit(engine, item) is not None
                current = log.get('items', item['id'])
                admissions.append({'item': item['id'], 'stage': stage, 'admitted': allowed,
                                   'state': current['state'], 'next': current['next'],
                                   'occupied': engine.occupied(), 'rounds': len(log.all('rounds')),
                                   'cards': len(log.all('cards')), 'serial_hold': bool(current.get('serial_hold'))})
        (args.output / 'admissions.json').write_text(json.dumps(admissions, ensure_ascii=False, indent=2))
        elapsed = time.monotonic() - started
        snapshots = {item['id']: item.get('relations') for item in log.open_items()}
        (args.output / 'snapshots.json').write_text(json.dumps(snapshots, ensure_ascii=False, indent=2))
        lines = [f'真实 App/API → 关系插件 → 核心准入，耗时 {elapsed:.3f}s',
                 '阶段在隔离库中设置，只测准入；没有运行执行者或伪造阶段完成。',
                 json.dumps(admissions, ensure_ascii=False, indent=2), status(log)]
        for item in log.open_items():
            lines.append(why(log, item['id'], rules.data, plugins, raw=True))
        lines.append(f'执行轮={len(log.all("rounds"))}，卡片={len(log.all("cards"))}，执行位置={engine.occupied()}')
        output = '\n'.join(lines)
        (args.output / 'output.txt').write_text(output)
        print(output)
        if any(not value or value.get('error') for value in snapshots.values()):
            raise SystemExit(1)


if __name__ == '__main__':
    main()
