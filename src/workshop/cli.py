"""车间命令行；JSON 从 --json、--file 或标准输入接收。"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

from .core.engine import Engine
from .core.log import Log
from .core.models import Event
from .core.report import resolve, status, usage, why
from .core.rules import Rules
from .registry import Registry


def parser():
    result = argparse.ArgumentParser(prog="workshop")
    result.add_argument("--state-dir", type=Path, default=Path(os.environ.get("WORKSHOP_STATE_DIR", "~/.local/state/workshop")).expanduser())
    result.add_argument("--rules", type=Path, default=Path(os.environ["WORKSHOP_RULES"]).expanduser().resolve() if os.environ.get("WORKSHOP_RULES") else None)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("event", "receipt"):
        command = commands.add_parser(name)
        if name == "receipt":
            command.add_argument("token")
        data = command.add_mutually_exclusive_group()
        data.add_argument("--json")
        data.add_argument("--file", type=Path)
    commands.add_parser("abort").add_argument("token")
    commands.add_parser("status")
    commands.add_parser("usage")
    why_command = commands.add_parser("why", help="列出一件活每一轮的起因和结果；活写成 <短名>#<编号> 或 <交付对象>:<对象键>")
    why_command.add_argument("item")
    why_command.add_argument("--raw", action="store_true", help="每条事件后原样打印原始资料")
    commands.add_parser("drop", help="清掉一件还没跑过轮次的活（例如没放行就被误建的）；活写成 <短名>#<编号> 或 <交付对象>:<对象键>").add_argument("item")
    commands.add_parser("pause").add_argument("target")
    resume = commands.add_parser("resume")
    resume.add_argument("target")
    resume.add_argument("--select", help="all / none / 事实编号，逗号分隔；省略时只列清单，保持暂停")
    resume.add_argument("--seen", help="上次看过的全部事实编号，逗号分隔；空清单填 none")
    run = commands.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--interval", type=float, default=20)
    return result


def current_rules(args, saved):
    if args.rules:
        return Rules.load(args.rules)
    if saved and args.command == "run" and saved.get("path"):
        return Rules.load(Path(saved["path"]))
    if saved:
        return Rules(Rules.validate(saved["data"]).data, saved.get("path"))
    return Rules.load(Path(__file__).resolve().parents[2] / "rules/workshop.yaml")


def main(argv=None):
    args = parser().parse_args(argv)
    log = Log(args.state_dir / "workshop.db")
    try:
        if args.command == "status":
            print(status(log))
        elif args.command == "why":
            try:
                rules = current_rules(args, log.get("controls", "@rules")).data
            except (ValueError, OSError):
                rules = None  # 规则读不到时只认 <交付对象>:<对象键>，不挡查看。
            print(why(log, args.item, rules, Registry(), raw=args.raw))
        elif args.command == "usage":
            print(usage(log))
        else:
            saved = log.get("controls", "@rules")
            if args.command == "receipt":
                if not saved:
                    raise ValueError("状态库没有规则快照")
                rules = Rules(Rules.validate(saved["data"]).data, saved.get("path"))
            else:
                rules = current_rules(args, saved)
            engine = Engine(log, rules, Registry())
            if args.command in ("event", "receipt"):
                data = json.loads(args.json if args.json is not None else args.file.read_text() if args.file else sys.stdin.read())
                added = engine.ingest(Event(**data), round_token=os.environ.get("WORKSHOP_ROUND_TOKEN"), ingress="external") if args.command == "event" else engine.submit(args.token, data)
                print("已收到并提交" if added else "已收到过，未重复记录")
            elif args.command == "abort":
                engine.abort(args.token)
                print("已登记结束请求，下一 tick 确认退出并记失败")
            elif args.command == "drop":
                item_id = resolve(log, rules.data["targets"], args.item)
                engine.drop(item_id)
                print(f"已清掉 {item_id}；同一对象要再放行才会重新建活")
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
                if args.interval <= 0:
                    raise ValueError("interval 必须为正数")
                while True:
                    engine.tick()
                    if engine.restart_requested:
                        # 交付改了车间自己的代码；正常退出，由 launchd KeepAlive 拉起新代码。
                        print("车间代码已更新，这一拍结束，退出等守护拉起", flush=True)
                        break
                    if args.once:
                        break
                    time.sleep(args.interval)
        return 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(f"车间：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
