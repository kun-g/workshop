"""写 issue 时的只读自检；复用接单的文档读取与核对，不打开状态库。"""
import json
import re

from workshop import github_identity
from . import dod, intake
from .hints import hints
from .io import IO
from .requirements import change_documents, changed_requirement, requirement_basis


def check_issue(args, rules, commands=None):
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
    problems, checks, documents, notes = inspect(target, number, body, commands)
    if documents is not None:
        print("进交付时要求 PR 改的需求文档：" + "、".join(sorted(documents)))
    if problems:
        print("\n".join(problems + notes))
        return 1
    print("接单核对通过，要跑的检查（交付对象规则登记）：")
    for argv in checks:
        print(json.dumps(argv, ensure_ascii=False))
    # 提示不改结论（ws#326）：开单前看到明显的路径、命令冲突。
    for line in notes:
        print(line)
    return 0


@github_identity.scoped
def inspect(target, number=None, body=None, commands=None):
    io = IO(gh=target.get("github_identity", {}).get("gh"), git="git")
    if number is not None:
        issue = io.api(f'repos/{target["repo"]}/issues/{number}')
        if issue.get("pull_request"):
            raise ValueError(f"#{number} 是 PR，请给 issue 号")
        body = issue.get("body") or ""
    else:
        # 草稿按协调对话用 App 开单核对；没登记 App 身份时作者留空，核对会报出来。
        logins = target.get("intake_app_logins") or [None]
        issue = {"user": {"login": logins[0]}, "body": body}
    documents = io.requirement_documents(target)
    problems, checks = dod.registration(body, target, documents)
    if target.get("auto_intake"):
        # 和自动接单同一个函数、同一个开关（issue交付 R11）；没开自动接单的交付对象只认人工打标签。
        problems = problems + intake.origin(issue, target)
    required = None
    basis = requirement_basis(body)
    if changed_requirement(basis) is not None:
        try:
            required = change_documents(basis, documents, target)
        except ValueError:
            pass  # 登记核对已给出缺项；没有确定的文档列表可展示。
    return problems, checks, required, hints(body, target, documents, commands)
