"""GitHub 的条件读取；游标与事件由核心一起确认，不另写来源账本。"""
from copy import deepcopy
import hashlib
import json
import re
import shutil
import subprocess
import time
import os
from pathlib import Path
import tempfile
from urllib.parse import urlencode

from workshop.core.models import Event, Poll
from workshop.core.ports import Retryable


class _YieldPoll(Exception):
    pass


class GitHubSource:
    def __init__(self, gh=None, state_dir=None):
        self.state_dir = Path(state_dir) if state_dir else None
        self.gh = gh or shutil.which("gh")
        if not self.gh:
            raise RuntimeError("找不到 gh；请在启动 PATH 中配置")

    def _cached(self, record):
        path = self.cache_root / (record.get("fingerprint", "missing") + ".json")
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def _save_response(self, value):
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        path = self.cache_root / (fingerprint + ".json")
        if not path.exists():
            with tempfile.NamedTemporaryFile("w", dir=self.cache_root, delete=False) as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(output.name, path)
        return fingerprint

    def _get(self, endpoint, cache):
        old = cache.get(endpoint, {})
        saved = self._cached(old)
        if endpoint in self.refreshed and saved is not None:
            return saved, old.get("next")
        remaining = self.deadline - time.monotonic()
        if self.requests >= self.limit or remaining < 1:
            raise _YieldPoll()
        self.requests += 1
        args = [self.gh, "api", "--method", "GET", "--include", endpoint]
        if old.get("etag") and saved is not None:
            args += ["-H", f'If-None-Match: {old["etag"]}']
        try:
            result = subprocess.run(args, text=True, capture_output=True, timeout=min(12, remaining))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Retryable("GitHub 读取暂时不可用") from exc
        raw = result.stdout.replace("\r\n", "\n")
        headers, sep, body = raw.partition("\n\n")
        status = re.match(r"HTTP/\S+ (\d+)", headers)
        if not status:
            raise Retryable("gh api 没有返回 HTTP 状态")
        code = int(status[1])
        if code == 304:
            self.refreshed.add(endpoint)
            return saved, old.get("next")
        if code != 200 or result.returncode:
            raise Retryable(f"GitHub 读取失败：HTTP {code}")
        fields = {line.split(":", 1)[0].lower(): line.split(":", 1)[1] for line in headers.splitlines()[1:] if ":" in line}
        # Link 的 URL 大小写不能随 header 名一起转换。
        link = next((line.split(":", 1)[1] for line in headers.splitlines()[1:]
                     if line.lower().startswith("link:")), "")
        next_page = re.search(r'<https://api.github.com/([^>]+)>; rel="next"', link)
        value = json.loads(body)
        cache[endpoint] = {"etag": fields.get("etag", "").strip(), "fingerprint": self._save_response(value),
                           "next": next_page[1] if next_page else None}
        self.refreshed.add(endpoint)
        return value, cache[endpoint]["next"]

    def _list(self, endpoint, cache):
        rows = []
        while endpoint:
            page, endpoint = self._get(endpoint, cache)
            if not isinstance(page, list):
                raise ValueError("GitHub 列表响应无效")
            rows.extend(page)
        return rows

    def poll(self, target, cursor):
        state = json.loads(cursor) if cursor else {"cache": {}, "known": {}, "seen": {}}
        state = deepcopy(state)
        self.cache_root = self.state_dir or Path(target.get("github_cache_dir", "~/.local/state/workshop/github-responses")).expanduser()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        for record in state["cache"].values():
            if "data" in record:
                record["fingerprint"] = self._save_response(record.pop("data"))
        self.refreshed = set(state.get("refreshed", []))
        self.requests = 0
        self.limit = target.get("github_requests_per_tick", 20)
        self.deadline = time.monotonic() + target.get("github_poll_seconds", 15)
        if type(self.limit) is not int or self.limit < 1:
            raise ValueError("github_requests_per_tick 必须是正整数")
        cache, known, seen = state["cache"], state["known"], state["seen"]
        repo, name = target["repo"], target["name"]
        base = f"repos/{repo}"
        label = target.get("label", "Ready for Agent")
        bots = set(target.get("github_bot_logins", []))
        events = []

        def emit(kind, number, key, identity, value, action="update"):
            signature = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            identity_key = f"{kind}:{number}:{identity}"
            previous = seen.get(identity_key)
            if previous == signature:
                return
            if previous is not None:
                action = "update"
            ext = f"{repo}:{identity_key}" + ("" if action in ("ready", "rework") else f":{signature}")
            seen[identity_key] = signature
            events.append(Event("github", ext, name, key, f"{repo} #{number}",
                                {"kind": kind, "repo": repo, "number": number, "data": value}, action=action))

        def automated(value):
            user = value.get("user") or {}
            return (user.get("type") == "Bot" or user.get("login") in bots
                    or "<!-- workshop-comment:" in (value.get("body") or ""))

        def comments(endpoint, kind, number, key):
            for comment in self._list(endpoint + "?per_page=100", cache):
                if automated(comment):
                    continue
                text = (comment.get("body") or "").strip()
                # 固定写法；引文或叙述中的“不对”不放行、不返工。
                rework = re.fullmatch(r"不对(?:[，,:：。！!\s].*)?", text, re.S)
                action = "rework" if comment.get("user", {}).get("login") == "kun-g" and rework else "update"
                emit(kind, number, key, comment["id"], comment, action)

        try:
            issues = self._list(base + "/issues?" + urlencode({"state": "all", "labels": label, "per_page": 100}), cache)
            for issue in issues:
                if "pull_request" not in issue and (issue.get("state") != "closed" or str(issue["number"]) in known):
                    known[str(issue["number"])] = True
            # 保留已接单对象，即使移除标签或收尾，仍能接受评论和返工。
            for number in known:
                issue, _ = self._get(f"{base}/issues/{number}", cache)
                changes = self._list(f"{base}/issues/{number}/events?per_page=100", cache)
                emit("issue", number, number, "body", issue)
                # 程序建单的初次标签与 issue 同次创建，不能冒充 Kun 再放行。
                own_intake = re.search(r"<!-- workshop-intake: [^<>]+ -->", issue.get("body") or "")
                initial = next((c.get("id") for c in changes if c.get("event") == "labeled" and c.get("label", {}).get("name") == label), None) if own_intake else None
                own_labels = state.setdefault("own_labels", {})
                if initial is not None:
                    own_labels[number] = initial
                initial = own_labels.get(number)
                for change in changes:
                    if change.get("event") == "labeled" and change.get("label", {}).get("name") == label:
                        action = "ready" if change.get("actor", {}).get("login") == "kun-g" and change["id"] != initial else "update"
                        emit("label", number, number, change["id"], change, action)
                comments(f"{base}/issues/{number}/comments", "issue-comment", number, number)

            for pr in self._list(f"{base}/pulls?state=all&per_page=100", cache):
                marker = re.findall(r"<!-- workshop-item: ([^\s<>]+) -->", pr.get("body") or "")
                if len(marker) != 1 or not marker[0].startswith(name + ":"):
                    continue
                key = marker[0][len(name) + 1:]
                number, sha = str(pr["number"]), pr["head"]["sha"]
                emit("pr-head", number, key, "head", {"sha": sha, "body": pr.get("body"), "state": pr["state"]})
                comments(f"{base}/issues/{number}/comments", "pr-comment", number, key)
                comments(f"{base}/pulls/{number}/comments", "pr-review-comment", number, key)
                for review in self._list(f"{base}/pulls/{number}/reviews?per_page=100", cache):
                    if not automated(review):
                        rework = re.fullmatch(r"不对(?:[，,:：。！!\s].*)?", (review.get("body") or "").strip(), re.S)
                        action = "rework" if review.get("user", {}).get("login") == "kun-g" and rework else "update"
                        emit("pr-review", number, key, review["id"], review, action)
                # 已关闭对象继续接收评论；CI 只轮询仍打开的对象。
                if pr["state"] == "open":
                    checks, _ = self._get(f"{base}/commits/{sha}/check-runs?per_page=100", cache)
                    status, _ = self._get(f"{base}/commits/{sha}/status", cache)
                    emit("pr-ci", number, key, sha, {"sha": sha, "checks": checks, "status": status})
        except _YieldPoll:
            state["refreshed"] = sorted(self.refreshed)
        else:
            state["refreshed"] = []
        return Poll(events, json.dumps(state, ensure_ascii=False))


def make_source():
    return GitHubSource()
