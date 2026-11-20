"""GitHub 推拉共用的归一键和事件正文（核心 R37 判据二）；轮询来源和推送来源都只经这里拼键。

同一次外部变化，轮询读到的 API 响应和推送收到的 webhook 载荷要拼出逐字符相同的 (source, ext_id)：
键取「对象号 + 变化类型 + 两边都有的标识（标签名、评论号）+ 对象版本（updated_at、标签事件时刻）」，
不用只有轮询有的 GitHub 事件号，也不用只有推送有的 delivery id。
同一秒里同一类型、同一标签的重复变化会撞键，这是接受的限制：状态以轮询读到的当前快照校正。
缺时间字段的旧响应（只出现在旧测试和升级前的缓存里）沿用旧的摘要键。
"""
import hashlib
import json
import re

from workshop.core.models import Event

SOURCE = "github"
COMMENTS = ("issue-comment", "pr-comment", "pr-review-comment")
MARKER = "<!-- workshop-comment:"


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def keys(repo, kind, number, identity, value):
    """返回 (ext_id, action_id)：ext_id 认这一次变化，action_id 认同一对象上的同一个动作。"""
    base = f"{repo}:{kind}:{number}:{identity}"
    if kind in ("label", "unlabel"):
        name, at = (value.get("label") or {}).get("name"), value.get("created_at")
        if name and at:
            key = f"{repo}:{kind}:{number}:{name}:{at}"
            return key, key
    elif kind == "issue" and identity == "body" and value.get("updated_at"):
        return f"{base}:{value['updated_at']}", base
    elif kind in COMMENTS and value.get("updated_at"):
        return f"{base}:{value['updated_at']}", base
    return f"{base}:{signature(value)}", base


def state(issue):
    """状态覆盖类字段（开关、标签、负责人）和对象版本；核心按版本判陈旧。"""
    return issue["updated_at"], {"state": issue.get("state"),
                                 "labels": sorted(v["name"] for v in issue.get("labels") or []),
                                 "assignees": sorted(v["login"] for v in issue.get("assignees") or [])}


def automated(value, bots=()):
    user = value.get("user") or {}
    return (user.get("type") == "Bot" or user.get("login") in bots
            or MARKER in (value.get("body") or ""))


def marker(body, name):
    matches = re.findall(r"<!-- workshop-item: ([^\s<>]+) -->", body or "")
    if len(matches) == 1 and matches[0].startswith(name + ":"):
        return matches[0][len(name) + 1:]
    return None


def event(target, kind, number, key, identity, value, *, edited, by_workshop, snapshot=False, legacy=False):
    """一条来源事件；推拉两边的正文字段一致，触发器看到的是同一份。"""
    repo = target["repo"]
    ext_id, action_id = keys(repo, kind, number, identity, value)
    actor, author = value.get("actor") or {}, value.get("user") or {}
    body = {"kind": kind, "repo": repo, "number": number, "data": value,
            "actor": actor.get("login", ""), "author": author.get("login", ""),
            "label": (value.get("label") or {}).get("name", ""),
            "text": (value.get("body") or "").strip(), "edited": edited, "by_workshop": by_workshop}
    extra = {}
    if kind == "issue" and identity == "body" and value.get("updated_at"):
        version, overwrite = state(value)
        extra = {"version": version, "overwrite": overwrite, "snapshot": snapshot}
    # 轮询还认得升级前没有交付对象前缀的旧键（legacy=True）；推送没有旧键，靠核心的别名表认回。
    old = f"{repo}:{kind}:{number}:{identity}"
    previous = [old, f"{old}:{signature(value)}"] if legacy else []
    return Event(SOURCE, ext_id, target["name"], key, f"{repo} #{number}", body,
                 action_id=action_id, previous_ids=previous, **extra)


def from_webhook(name, payload, target):
    """一份 webhook 投递 → 来源事件；只认 issues 和 issue_comment，其余（ping 等）不产生事件。"""
    if not isinstance(payload, dict) or (payload.get("repository") or {}).get("full_name") != target["repo"]:
        return []
    issue = payload.get("issue")
    if not isinstance(issue, dict) or "number" not in issue:
        return []
    number = str(issue["number"])
    action = payload.get("action")
    sender = payload.get("sender") or {}
    events = []
    if name == "issues" and "pull_request" not in issue and issue.get("updated_at"):
        events.append(event(target, "issue", number, number, "body", issue,
                            edited=action != "opened", by_workshop=False))
        if action in ("labeled", "unlabeled") and isinstance(payload.get("label"), dict):
            # 和标签事件 API 同形：created_at 就是这次变化的时刻（issue.updated_at）。
            value = {"event": action, "label": payload["label"], "actor": sender, "created_at": issue["updated_at"]}
            events.append(event(target, "label" if action == "labeled" else "unlabel", number, number,
                                payload["label"].get("name"), value,
                                edited=False, by_workshop=sender.get("type") == "Bot"))
    elif name == "issue_comment" and action in ("created", "edited"):
        comment = payload.get("comment")
        if not isinstance(comment, dict) or automated(comment, target.get("github_bot_logins", [])):
            return []
        if "pull_request" in issue:
            key = marker(issue.get("body"), target["name"])
            if key is None:
                return []
            kind = "pr-comment"
        else:
            kind, key = "issue-comment", number
        events.append(event(target, kind, number, key, comment["id"], comment,
                            edited=action == "edited", by_workshop=False))
    return events


def rekey(stored):
    """升级前事实的正文 → 这次变化现在的键；拼不出（缺时间字段、只有轮询才有的种类）返回空列表。"""
    body = stored.get("body") if isinstance(stored, dict) else None
    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        return []
    kind, repo, number, data = body.get("kind"), body.get("repo"), body.get("number"), body["data"]
    if not repo or number in (None, ""):
        return []
    if kind == "issue" and f":issue:{number}:body:" not in str(stored.get("ext_id", "")) + ":":
        return []  # 接单首查的独立事件号只有轮询有，沿用旧键
    if kind == "label" or kind in COMMENTS or kind == "issue":
        identity = "body" if kind == "issue" else data.get("id")
        ext_id, _ = keys(repo, kind, number, identity, data)
        return [] if ext_id.endswith(signature(data)) else [ext_id]
    return []
