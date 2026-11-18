"""写 issue 时的只读自检；复用接单的文档读取与核对，不打开状态库。"""
import json
import re

from workshop import github_identity
from . import dod
from .io import IO


def check_issue(args, rules):
    if args.file is not None:
        if args.issue or not args.target:
            raise ValueError("查草稿须用 --file <路径> --target <短名>，不能同时给 issue")
        name, number = args.target, None
        body = args.file.expanduser().read_text(encoding="utf-8")
    else:
        match = re.fullmatch(r"([^\s:#]+)([#:])([1-9][0-9]*)", args.issue or "")
        if not match or args.target:
            raise ValueError("请给 <短名>#<号> 或 <交付对象>:<号>；查草稿用 --file 和 --target")
        name, number, body = match[1], int(match[3]), None
    targets = rules.data["targets"]
    matches = [key for key, target in targets.items() if name in (key, target.get("short_name"))]
    if len(matches) != 1:
        raise ValueError(f"交付对象不存在或不唯一：{name}")
    # 取 issue 流程覆盖后的配置，与接单使用同一份 required_checks / requirement_docs / 身份。
    target = rules.target(matches[0], "issue_delivery")
    problems, checks = inspect(target, number, body)
    if problems:
        print("\n".join(problems))
        return 1
    print("接单核对通过，能跑的检查：")
    for argv in checks:
        print(json.dumps(argv, ensure_ascii=False))
    return 0


@github_identity.scoped
def inspect(target, number=None, body=None):
    io = IO(gh=target.get("github_identity", {}).get("gh"), git="git")
    if number is not None:
        issue = io.api(f'repos/{target["repo"]}/issues/{number}')
        if issue.get("pull_request"):
            raise ValueError(f"#{number} 是 PR，请给 issue 号")
        body = issue.get("body") or ""
    documents = io.requirement_documents(target)
    return dod.registration(body, target, documents)
