"""车间命令行；JSON 从 --json、--file 或标准输入接收。"""
import argparse
import json
import os
import sqlite3
from pathlib import Path
import sys

from .core.engine import Engine, finder
from .core.jobs import Jobs, execute
from .core.log import Log
from .core.models import Event
from .core.ports import WaitRejected, WaitUnavailable
from .core.holding import resolve
from .reports.report import overview, status, unfiled, usage, why
from .core.rules import Rules
from .core.wake import Mailbox, ring
from .registry import Registry, load_rules


def parser():
    result = argparse.ArgumentParser(prog="workshop")
    result.add_argument("--state-dir", type=Path, default=Path(os.environ.get("WORKSHOP_STATE_DIR", "~/.local/state/workshop")).expanduser())
    result.add_argument("--rules", type=Path, default=Path(os.environ["WORKSHOP_RULES"]).expanduser().resolve() if os.environ.get("WORKSHOP_RULES") else None)
    commands = result.add_subparsers(dest="command", required=True)
    check_issue = commands.add_parser("check-issue", help="只读核对 issue 或草稿，缺项措辞与接单一致")
    check_issue.add_argument("issue", nargs="?", help="<短名>#<号> 或 <交付对象>:<号>")
    check_issue.add_argument("--file", type=Path, help="本地 issue 草稿")
    check_issue.add_argument("--target", help="草稿所属交付对象的短名或名字")
    for name in ("event", "receipt"):
        command = commands.add_parser(name)
        if name == "receipt":
            command.add_argument("token")
        data = command.add_mutually_exclusive_group()
        data.add_argument("--json")
        data.add_argument("--file", type=Path)
    commands.add_parser("abort").add_argument("token")
    status_command = commands.add_parser("status", help="当前的活：默认只列没结束的；--all 连已结束的一起列")
    status_command.add_argument("--all", dest="everything", action="store_true", help="连完成、放弃的一起列")
    status_command.add_argument("--state", help="只列这些状态，逗号分隔，例如 卡住,等你；给了就不再默认隐藏已结束的")
    status_command.add_argument("--target", help="只列这些交付对象，逗号分隔")
    status_command.add_argument("--flow", help="只列这些流程，逗号分隔")
    status_command.add_argument("--html", nargs="?", const="", metavar="文件",
                                help="写成一页网页；不给文件名时写到 --state-dir/status.html，打印路径")
    usage_command = commands.add_parser("usage", help="花费和用量：今天、最近 7 天、按活、按实际模型、按执行者配置档")
    usage_command.add_argument("--since", help="按执行者配置档只算这一天（YYYY-MM-DD）及以后开工的活；默认最近 7 天")
    commands.add_parser("compact-managers", help="瘦身 managers.db：备份、把 history 转成变化记录并 VACUUM；可重复跑")
    metrics_command = commands.add_parser("metrics", help="只读查看供给、流动、负载及小时明细")
    metrics_command.add_argument("--since", help="回看时长，默认 24h；支持 s/m/h/d")
    metrics_command.add_argument("--from", dest="start", help="含时区的 ISO 起点")
    metrics_command.add_argument("--to", dest="end", help="含时区的 ISO 终点")
    metrics_command.add_argument("--database", type=Path, help="状态库副本路径，默认读取 --state-dir/workshop.db")
    why_command = commands.add_parser("why", help="列出一件活每一轮的起因和结果；活写成 <短名>#<编号> 或 <交付对象>:<对象键>")
    why_command.add_argument("item", nargs="?")
    why_command.add_argument("--raw", action="store_true", help="每条事件后原样打印原始资料")
    why_command.add_argument("--unfiled", action="store_true", help="列出未归件：外部入口事件里判为丢弃的和还没判定的")
    commands.add_parser("drop", help="清掉一件还没跑过轮次的活（例如没放行就被误建的）；活写成 <短名>#<编号> 或 <交付对象>:<对象键>").add_argument("item")
    abandon = commands.add_parser("abandon", help="放弃一件活：停在当前阶段、让出串行路径，要再做就返工；活写成 <短名>#<编号> 或 <交付对象>:<对象键>")
    abandon.add_argument("item")
    abandon.add_argument("--reason", required=True, help="为什么不做了，记进这件活")
    recover = commands.add_parser("recover", help="恢复一件停止的活；记录原因，不删除历史或代替审批")
    recover.add_argument("item")
    recover.add_argument("--mode", required=True, choices=("rework", "wait", "verify"),
                         help="rework 标准返工；wait 超限后续等；verify 追加一次核验（流程支持时）")
    recover.add_argument("--reason", required=True, help="本次恢复原因，不能留空")
    recover.add_argument("--quote", default="", help="Kun 原话；rework 必填")
    recover.add_argument("--ref", choices=("main",), help="verify 时在包含原合并提交的当前 main 上验收")
    waiting = commands.add_parser('answer-wait', help='按等待号和问题版本登记会话回答；不代替授权中心批准')
    waiting.add_argument('reference', help='等待号；存量转换前的旧卡号（车间卡号或飞书 Q 号）也认，按转换时记下的对应找到等待号')
    waiting.add_argument('value')
    waiting.add_argument('--version', required=True)
    waiting.add_argument('--quote', required=True)
    waiting.add_argument('--session', required=True)
    waiting.add_argument('--provider', default='intervention')
    release = commands.add_parser('release', help='登记 Kun 在会话里的放行；支持新对象或已有活')
    release.add_argument('item', help='<短名>#<对象键> 或 <交付对象>:<对象键>')
    release.add_argument('--quote', required=True, help='Kun 的原话，逐字')
    release.add_argument('--session', required=True, help='Kun 说这句话的会话号')
    commands.add_parser("release-now", help="不等下个周期，立刻比较在跑的版本与远端；有新合并就走同一发版流程").add_argument(
        "target", nargs="?", default="workshop", help="交付对象，默认 workshop")
    prune = commands.add_parser("prune", help="按保留规则清一次执行者轮次、经理作业与会话、已结束活的工作区")
    prune.add_argument("--dry-run", action="store_true", help="只列出会删什么，不动文件")
    prune.add_argument("--verbose", action="store_true", help="逐个列出目录")
    prune.add_argument("--executor-state-dir", type=Path,
                       help="执行者状态目录；默认 WORKSHOP_EXECUTOR_STATE_DIR 或 ~/.local/state/workshop-executors")
    commands.add_parser("pause").add_argument("target")
    resume = commands.add_parser("resume")
    resume.add_argument("target")
    resume.add_argument("--select", help="all / none / 事实编号，逗号分隔；省略时只列清单，保持暂停")
    resume.add_argument("--seen", help="上次看过的全部事实编号，逗号分隔；空清单填 none")
    run = commands.add_parser("run", help="守护进程：每拍只读写状态库和本地文件，插件调用放到后台作业；--once 只跑一拍，插件调用就地进行")
    run.add_argument("--once", action="store_true")
    run.add_argument("--interval", type=float, default=None, help="兼容旧调用；已改为事件唤醒，不再使用固定间隔")
    job = commands.add_parser("job", help="（守护进程内部用）在独立进程里跑一个作业，结果写回作业目录")
    job.add_argument("directory", type=Path)
    return result


def current_rules(args, saved):
    if args.rules:
        return load_rules(args.rules)
    if saved and args.command == "run" and saved.get("path"):
        return load_rules(Path(saved["path"]))
    if saved:
        return Rules(Rules.validate(saved["data"]).data, saved.get("path"))
    return load_rules(Path(__file__).resolve().parents[2] / "rules/workshop.yaml")


def read_command(args):
    try:
        log = Log.readonly(args.state_dir / 'workshop.db')
        try:
            with log.read_transaction():
                if args.command == 'status':
                    split = lambda text: tuple(x.strip() for x in (text or '').split(',') if x.strip())
                    options = dict(everything=args.everything, states=split(args.state),
                                   targets=split(args.target), flows=split(args.flow), plugins=Registry())
                    if args.html is None:
                        print(status(log, **options))
                    else:
                        from .reports.status_page import page
                        from .reports.status_writer import write_page
                        path = Path(args.html).expanduser() if args.html else args.state_dir / 'status.html'
                        write_page(path, page(overview(log, **options)))
                        print(path.resolve())
                elif args.command == 'why':
                    try:
                        rules = current_rules(args, log.get('controls', '@rules')).data
                    except (ValueError, OSError):
                        rules = None
                    if args.unfiled == (args.item is not None):
                        raise ValueError('why 要么给一件活，要么用 --unfiled 看未归件')
                    print(unfiled(log, rules, Registry()) if args.unfiled else
                          why(log, args.item, rules, Registry(), raw=args.raw))
                else:
                    print(usage(log, since=args.since))
            return 0
        finally:
            log.close()
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        print(f'车间：{exc}', file=sys.stderr)
        return 2


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "compact-managers":
        from .core.managers import compact
        try:
            result = compact(args.state_dir)
            if result.get('missing'):
                print(f"没有 managers.db：{Path(args.state_dir) / 'managers.db'}")
                return 0
            before, after = result['before'], result['after']
            size = lambda value: f'{value / 1024 / 1024:.1f} MB'
            print(f"已备份：{result['backup']}")
            print(f"managers.db：{size(before['file_bytes'])} → {size(after['file_bytes'])}")
            print(f"history：{before['history']} 行，正文 {size(before['history_bytes'])} → {size(after['history_bytes'])}"
                  f"（{result['rewritten']} 行旧快照转成变化记录）")
            return 0
        except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
            print(f'车间：{exc}', file=sys.stderr)
            return 2
    if args.command == "check-issue":
        from .plugins.issue_delivery.check_issue import check_issue
        from .core.ports import Retryable, TargetBlocked
        try:
            return check_issue(args, current_rules(args, None))
        except (ValueError, KeyError, OSError, RuntimeError, Retryable, TargetBlocked) as exc:
            print(f"车间：{exc}", file=sys.stderr)
            return 2
    if args.command == "metrics":
        from .reports.metrics import metrics
        try:
            print(metrics(args.database or args.state_dir / "workshop.db", args.since, args.start, args.end))
            return 0
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, OverflowError) as exc:
            print(f"车间：{exc}", file=sys.stderr)
            return 2
    if args.command in ("status", "why", "usage"):
        return read_command(args)
    if args.command == "prune":
        from .prune import prune
        try:
            log = Log.readonly(args.state_dir / 'workshop.db')
        except (OSError, sqlite3.Error, ValueError) as exc:
            print(f'车间：{exc}', file=sys.stderr)
            return 2
        try:
            rules = current_rules(args, log.get('controls', '@rules'))
            if args.executor_state_dir:
                os.environ['WORKSHOP_EXECUTOR_STATE_DIR'] = str(args.executor_state_dir.expanduser().resolve())
            registry = Registry()
            rules = registry.configure(rules)
            return prune(args, log, rules, registry)
        except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
            print(f'车间：{exc}', file=sys.stderr)
            return 2
        finally:
            log.close()
    log = Log(args.state_dir / "workshop.db")
    jobs = None
    try:
        if args.command == "job":
            saved = log.get("controls", "@rules") or {}
            execute(args.directory, Registry(saved.get('data', {}).get('executor_profiles')), lambda target: finder(log, target))
        elif args.command == "release-now":
            # 只写请求；比较和发版仍由守护进程按同一发版流程做（集群模式同样读这个目录）。
            from datetime import datetime, timezone
            rules = current_rules(args, log.get("controls", "@rules"))
            source = Registry().get('source', 'main_branch')
            paths = source.wake_paths(rules.target(args.target))
            if len(paths) != 1:
                raise ValueError('立即发版来源须提供唯一请求路径')
            request = Path(paths[0])
            request.parent.mkdir(parents=True, exist_ok=True)
            request.write_text(datetime.now(timezone.utc).isoformat())
            print("已请求立即发版：守护进程马上比较在跑的版本与远端，有新合并就发版；进度看 workshop status")
        else:
            saved = log.get("controls", "@rules")
            if args.command == "receipt":
                if not saved:
                    raise ValueError("状态库没有规则快照")
                rules = Rules(Rules.validate(saved["data"]).data, saved.get("path"))
            else:
                rules = current_rules(args, saved)
            jobs = None
            if args.command == "run":
                from .running import record, process_identity
                record()  # 作业子进程继承；定时发版拿它和远端比
            if args.command == "run" and not args.once:
                state_dir = str(args.state_dir.resolve())
                jobs = Jobs(args.state_dir / "jobs", [sys.executable, "-m", "workshop.cli", "--state-dir", state_dir, "job"])
            engine = Engine(log, rules, Registry(), jobs=jobs,
                            daemon=process_identity(os.getpid()) if args.command == "run" else None)
            if args.command in ("event", "receipt"):
                data = json.loads(args.json if args.json is not None else args.file.read_text() if args.file else sys.stdin.read())
                added = engine.ingest(Event(**data), round_token=os.environ.get("WORKSHOP_ROUND_TOKEN"), ingress="external") if args.command == "event" else engine.submit(args.token, data)
                ring(args.state_dir)
                print("已收到并提交" if added else "已收到过，未重复记录")
            elif args.command == "abort":
                engine.abort(args.token)
                print("已登记结束请求，下一 tick 确认退出并记失败")
            elif args.command == "drop":
                item_id = resolve(log, rules.data["targets"], args.item)
                engine.drop(item_id)
                print(f"已清掉 {item_id}；同一对象要再放行才会重新建活")
            elif args.command == "abandon":
                item_id = resolve(log, rules.data["targets"], args.item)
                engine.abandon(item_id, args.reason)
                print(f"已放弃 {item_id}；串行路径已让出，要再做就返工")
            elif args.command == 'answer-wait':
                import hashlib
                source = {'channel': 'session', 'session': args.session, 'quote': args.quote, 'version': args.version}
                if not all(v.strip() for v in source.values()) or not args.value.strip():
                    raise ValueError('回答、版本、Kun 原话及会话号都不能为空白')
                config = engine.rules.data.get('wait_services', {}).get(args.provider)
                if config is None:
                    raise ValueError('等待端口未配置')
                # 存量转换（#176）把没答的旧问卡转成了等待，旧卡号到等待号的对应记在 @legacy-card: 控制记录里。
                legacy = engine.control('@legacy-card:' + args.reference).get('wait_ref')
                payload = {'ref': legacy or args.reference, 'value': args.value, 'source': source}
                payload['operation'] = 'session-answer:' + hashlib.sha256(
                    json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                result = engine.plugins.get('wait', args.provider).invoke('answer', payload, config)
                print(json.dumps(result, ensure_ascii=False))
            elif args.command == "recover":
                item_id = resolve(log, rules.data["targets"], args.item)
                recovery = engine.recover(item_id, args.mode, args.reason, args.quote, args.ref)
                print(f'已登记恢复 {item_id}：{recovery["mode"]}；原因及恢复计划见 why')
            elif args.command == 'release':
                item_id = engine.release_here(args.item, args.quote, args.session)
                print(f'已登记会话放行：{item_id}')
            elif args.command == "pause":
                engine.pause(args.target)
                print(f"已暂停 {args.target}；当前一轮继续收尾")
            elif args.command == "resume":
                missed = engine.missed(args.target)
                print(f"暂停期间漏掉 {len(missed)} 个事件：")
                for entry in missed:
                    print(f'{entry["id"]}\t{entry["at"]}\t{entry["event"]["title"]}\t{entry["event"]["body"]}')
                if args.select is None:
                    print("已看清单：" + (",".join(e["id"] for e in missed) or "none"))
                    print("用 --select all / none / 事实编号 并带 --seen 已看清单；尚未恢复")
                else:
                    if args.seen is None:
                        raise ValueError("--select 必须同时带上次看过的 --seen 清单")
                    seen = [] if args.seen == "none" else args.seen.split(",")
                    selected = seen if args.select == "all" else [] if args.select == "none" else args.select.split(",")
                    engine.resume(args.target, selected, expected=seen)
                    print(f"已恢复 {args.target}，补 {len(selected)} 个事件")
            elif args.command == "run":
                if args.interval is not None and args.interval <= 0:
                    raise ValueError("interval 必须为正数")
                if args.once:
                    engine.tick()
                else:
                    paths = engine.wake_paths()
                    from .reports.status_writer import StatusWriter
                    from contextlib import nullcontext
                    from .reports.status_server import StatusServer
                    page_url = engine.rules.data.get('status_page')
                    server = StatusServer(log.path.parent / 'status.html', page_url) if page_url else nullcontext()
                    with StatusWriter(log.path, plugins=Registry()) as writer, Mailbox(args.state_dir, paths) as mailbox, server:
                        received_at = None
                        while True:
                            engine.tick(received_at)
                            writer.wake()
                            if engine.restart_requested:
                                print("车间代码已更新，这一拍结束，退出等守护拉起", flush=True)
                                break
                            wake = mailbox.wait(engine.wait_timeout())
                            engine.awaken(wake, paths)
                            received_at = wake.received_at
        return 0
    except (ValueError, TypeError, KeyError, OSError, WaitRejected, WaitUnavailable) as exc:
        print(f"车间：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        if jobs is not None:
            jobs.launcher.shutdown(wait=True)
        if "engine" in locals() and getattr(engine, "manager_jobs", None):
            engine.manager_jobs.launcher.shutdown(wait=True)
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
