"""清单检查、私有预览、补发周报；只有显式 resend-weekly 会发送。"""
import argparse
import json
import sys

from .flow import Flow
from .io import IO, path, read
from .operations import prepare_preview, preview_inventory, render_preview, resend_weekly


def configured(args):
    rules = read(path(args.rules))
    if args.target not in rules.get('targets', {}):
        raise ValueError('规则中不存在交付对象：' + args.target)
    return {**rules['targets'][args.target], 'name': args.target}


def main(argv=None):
    parser = argparse.ArgumentParser()
    # 保留原独立检查命令，Flow.checks 和旧脚本仍可使用。
    parser.add_argument('--watchlist')
    parser.add_argument('--needs')
    commands = parser.add_subparsers(dest='command')
    prepare = commands.add_parser('prepare-preview', help='从真实状态导出 1–2 个版本的新判断资料，不发送')
    prepare.add_argument('--version', action='append', required=True)
    prepare.add_argument('--output', required=True, help='尚不存在的私有输出目录')
    render = commands.add_parser('render-preview', help='校验新判断并渲染隔离周报，不发送')
    render.add_argument('--preview', required=True)
    render.add_argument('--judgments', required=True)
    resend = commands.add_parser('resend-weekly', help='显式补发本周周报；同 request 重试不重复发')
    resend.add_argument('--request', required=True, help='稳定请求标识；重试必须用相同标识')
    inventory = commands.add_parser('preview-inventory', help='校验并应用采集提案到隔离目录，不改现役清单')
    inventory.add_argument('--proposal', required=True)
    inventory.add_argument('--output', required=True)
    migration = commands.add_parser('migrate-checks', help='停机窗口备份并合并活动检查活；不升级、不发送')
    migration.add_argument('--database', required=True)
    migration.add_argument('--watch-state', required=True, help='与数据库同一快照的盯更新状态目录')
    migration.add_argument('--offline', action='store_true', required=True, help='确认该数据库没有运行中的守护进程')
    for sub in (prepare, resend, inventory, migration):
        sub.add_argument('--rules', default='rules/workshop.yaml')
        sub.add_argument('--target', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'migrate-checks':
            from .migration import migrate
            target = configured(args)
            target['watch_updates']['state_dir'] = args.watch_state
            result = migrate(args.database, target)
        elif args.command == 'preview-inventory':
            result = preview_inventory(configured(args), args.proposal, args.output)
        elif args.command == 'prepare-preview':
            result = prepare_preview(configured(args), args.version, args.output)
        elif args.command == 'render-preview':
            result = render_preview(args.preview, args.judgments)
        elif args.command == 'resend-weekly':
            result = resend_weekly(configured(args), args.request)
        else:
            if not args.watchlist or not args.needs:
                parser.error('检查清单须提供 --watchlist 和 --needs')
            target = {'watch_updates': {'watchlist': args.watchlist, 'needs': args.needs}}
            io = IO()
            rows, needs = io.watchlist(target)['software'], io.needs(target)
            Flow.validate_needs(needs)
            print(f'清单与需要台账检查通过：{len(rows)} 个软件、{len(needs)} 条需要')
            return 0
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] != 'pending' else 2
    except (ValueError, OSError, KeyError, TypeError) as exc:
        context = f' request={args.request}' if args.command == 'resend-weekly' else ''
        print(f'盯更新失败{context}：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
