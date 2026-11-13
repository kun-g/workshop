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
from .core.report import status, usage, why
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
    commands.add_parser("why").add_argument("item")
    commands.add_parser("pause").add_argument("target")
    resume = commands.add_parser("resume")
    resume.add_argument("target")
    resume.add_argument("--select", help="all / none / 事实编号，逗号分隔；省略时只列清单，保持暂停")
    resume.add_argument("--seen", help="上次看过的全部事实编号，逗号分隔；空清单填 none")
    run = commands.add_parser("run")
    run.add_argument("--once", action="store_true")
    run.add_argument("--interval", type=float, default=20)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    log = Log(args.state_dir / "workshop.db")
    try:
        if args.command == "status":
            print(status(log))
        elif args.command == "why":
            print(why(log, args.item))
        elif args.command == "usage":
            print(usage(log))
        else:
            saved = log.get("controls", "@rules")
            if args.rules:
                rules = Rules.load(args.rules)
            elif saved and args.command == "run" and saved.get("path"):
                rules = Rules.load(Path(saved["path"]))
            elif saved:
                rules = Rules.validate(saved["data"])
                rules = Rules(rules.data, saved.get("path"))
            else:
                rules = Rules.load(Path(__file__).resolve().parents[2] / "rules/workshop.yaml")
            engine = Engine(log, rules, Registry())
            if args.command in ("event", "receipt"):
                data = json.loads(args.json if args.json is not None else args.file.read_text() if args.file else sys.stdin.read())
                added = engine.ingest(Event(**data), round_token=os.environ.get("WORKSHOP_ROUND_TOKEN")) if args.command == "event" else engine.submit(args.token, data)
                print("已收到并提交" if added else "已收到过，未重复记录")
            elif args.command == "abort":
                engine.abort(args.token)
                print("已登记结束请求，下一 tick 确认退出并记失败")
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
