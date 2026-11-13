"""CLI 替身：所有发送只写测试临时目录，不访问网络。"""
import argparse
import json
from pathlib import Path
import sys
import re
from datetime import datetime, timedelta, timezone

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


if args[0] in config.get("errors", {}):
    code, error = config["errors"][args[0]]
    print(error, file=sys.stderr)
    raise SystemExit(code)

if args[0] == "ask":
    technical = re.compile(
        r"(?:[a-z][a-z0-9+.-]*://|www\.)"
        r"|(?<![a-z0-9])(?:MR|PR|CI|Q)\s*[#!！＃]?\s*[0-9０-９]+(?![a-z0-9])"
        r"|(?<![a-z0-9])(?=[a-f0-9]{7,40}(?![a-z0-9]))(?=[a-f0-9]*[a-f])[a-f0-9]{7,40}(?![a-z0-9])"
        r"|(?:~[/\\]|[a-z]:\\)[^\s，。；！？]+"
        r"|(?<![a-z0-9])/[a-z_~.][a-z0-9_.~-]*(?:/[\w./~-]+)?"
        r"|[a-z_.~-][a-z0-9_.~-]*/[\w./~-]+"
        r"|[\w.-]+\.(?:py|md|json|ya?ml|toml|sh|js|ts|tsx|html|txt|csv|log)(?![a-z0-9_])"
        r"|(?<![a-z0-9_])(?:worktree|commit|merge|rebase|cherry-pick|diff|pipeline|"
        r"deploy|rollback|repo|pull\s+request|merge\s+request|CI|CD|SHA|CLI|SDK|stdout|stderr)(?![a-z0-9_])",
        re.I,
    )
    def readable(text, maximum):
        if not text.strip() or len(text) > maximum or any(c in text for c in "\r\n\t") or technical.search(text):
            raise ValueError("请写人话；尚未发送。技术依据移到 --details-file")
    try:
        for flag, maximum in (("--subject", 30), ("--decision", 42), ("--context", 100), ("--reason", 60)):
            readable(value(flag), maximum)
        choices = [args[i+1].split("=", 1) for i, arg in enumerate(args) if arg == "--choice"]
        effects = dict(args[i+1].split("=", 1) for i, arg in enumerate(args) if arg == "--effect")
        labels = set()
        for key, label in choices:
            readable(label, 18)
            readable(effects[key], 60)
            if label in labels or label in {"确定", "同意", "确认", "是", "否", "处理"} or effects[key].rstrip("。.!！") == label.rstrip("。.!！"):
                raise ValueError("按钮或选后结果不合规；尚未发送")
            labels.add(label)
        if config.get("ask_exit") == 2:
            raise ValueError("上游新增校验；尚未发送")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    assert value("--answer-format") == "key"
    assert "--no-wait" in args
    qid = "Q" + str(len(questions) + 1)
    details = Path(value("--details-file")).read_text()
    assert len(details) <= 6000 and len(value("--context")) <= 100
    questions.append({"id": qid, "from": value("--from"), "subject": value("--subject"),
                      "answer_format": "key", "status": "waiting", "details": details, "sent_at": datetime.now(timezone.utc).isoformat()})
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
    expired = [q["id"] for q in questions if "sent_at" in q and datetime.fromisoformat(q["sent_at"]) < datetime.now(timezone.utc) - timedelta(days=30)]
    questions = [q for q in questions if q["id"] not in expired]
    path.write_text(json.dumps(questions))
    if parsed.id in expired or config.get("check_presence") and not any(q["id"] == parsed.id for q in questions):
        print("HTTP 403: forbidden", file=sys.stderr)
        raise SystemExit(4)
    code, answer = config.get("answers", {}).get(parsed.id, [3, ""])
    if code == 4:
        print("HTTP 403: forbidden", file=sys.stderr)
        if config.get("remove_on_wait"):
            path.write_text(json.dumps([q for q in questions if q["id"] != parsed.id]))
            config.setdefault("missing_ids", []).append(parsed.id)
            (root / "config.json").write_text(json.dumps(config))
    print(answer)
    raise SystemExit(code)
elif args[0] == "list":
    assert args == ["list", "--all"]
    for qid, (code, answer) in config.get("answers", {}).items():
        existing = next((q for q in questions if q["id"] == qid), None)
        if existing is None:
            existing = {"id": qid}
            questions.append(existing)
        existing["status"] = "waiting" if code == 3 else "cancelled" if code == 6 else "answered"
    questions = [q for q in questions if q["id"] not in config.get("missing_ids", [])]
    if config.get("bad_list"):
        print("Q1\twaiting\t旧格式不含完整标题")
    else:
        print(json.dumps({"code": 0, "questions": [] if config.get("empty_list") else questions, "has_more": config.get("has_more", False)}))
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
