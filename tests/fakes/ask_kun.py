"""CLI 替身：所有发送只写测试临时目录，不访问网络。"""
import argparse
import json
from pathlib import Path
import sys

root = Path(sys.argv[0]).parent
config = json.loads((root / "config.json").read_text())
args = sys.argv[1:]
wire = sys.stdin.read() if args[-1:] == ["-"] else ""
with (root / "calls.jsonl").open("a") as f:
    f.write(json.dumps({"args": args, "input": wire}) + "\n")
path = root / "questions.json"
questions = json.loads(path.read_text()) if path.exists() else []


def value(flag):
    return args[args.index(flag) + 1]


if args[0] == "ask":
    assert value("--answer-format") == "key"
    assert "--no-wait" in args
    qid = "Q" + str(len(questions) + 1)
    details = Path(value("--details-file")).read_text()
    assert len(details) <= 6000 and len(value("--context")) <= 100
    questions.append({"id": qid, "from": value("--from"), "subject": value("--subject"),
                      "answer_format": "key", "status": "waiting", "details": details})
    path.write_text(json.dumps(questions))
    if config.get("drop_ask_reply"):
        raise SystemExit(1)
    print(qid)
elif args[0] == "wait":
    # 真 CLI 仅 ask 接受 --answer-format；wait 沿用卡片保存的格式。
    p = argparse.ArgumentParser()
    p.add_argument("command")
    p.add_argument("id")
    p.add_argument("--timeout")
    parsed = p.parse_args(args)
    assert parsed.timeout == "0"
    code, answer = config.get("answers", {}).get(parsed.id, [3, ""])
    print(answer)
    raise SystemExit(code)
elif args[0] == "list":
    assert args == ["list", "--all"]
    if config.get("bad_list"):
        print("Q1\twaiting\t旧格式不含完整标题")
    else:
        print(json.dumps({"code": 0, "questions": [] if config.get("empty_list") else questions}))
elif args[0] == "notify":
    assert value("--request-id")
    raise SystemExit(config.get("notify_exit", 0))
elif args[0] == "cancel":
    for row in questions:
        if row["id"] == args[1]:
            row["status"] = "cancelled"
    path.write_text(json.dumps(questions))
else:
    raise SystemExit(2)
