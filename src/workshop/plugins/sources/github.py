"""GitHub 的条件读取；游标与事件由核心一起确认，不另写来源账本。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import fcntl
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
from workshop.core.ports import Retryable, TargetBlocked
from workshop.github_identity import scoped, run as identity_run


class _YieldPoll(Exception):
    pass


class GitHubSource:
    def __init__(self, gh=None, state_dir=None):
        self.state_dir = Path(state_dir) if state_dir else None
        self.gh = gh or shutil.which("gh")
        if not self.gh:
            raise RuntimeError("找不到 gh；请在启动 PATH 中配置")

    @staticmethod
    def normalize(batch, target):
        """升级时认回旧 poll 作业：补事实字段，再交核心按新规则匹配，游标不变。"""
        events = []
        for event in batch.events:
            if event.action_id:
                events.append(event)
                continue
            data = event.body.get('data', {})
            actor, author = data.get('actor') or {}, data.get('user') or {}
            stable = event.ext_id
            if event.action == 'update' and re.search(r':[0-9a-f]{64}$', stable):
                stable = stable.rsplit(':', 1)[0]
            body = {**event.body, 'actor': actor.get('login', ''), 'author': author.get('login', ''),
                    'label': (data.get('label') or {}).get('name', ''), 'text': (data.get('body') or '').strip(),
                    'by_workshop': actor.get('type') == 'Bot' or author.get('type') == 'Bot'
                        or author.get('login') in target.get('github_bot_logins', [])
                        or '<!-- workshop-comment:' in (data.get('body') or ''),
                    # 旧作业明确只更新的事件不得在升级时变成一次新的控制动作。
                    'edited': event.action == 'update'}
            events.append(replace(event, body=body, action='update', action_id=stable,
                                  previous_ids=[event.ext_id]))
        return Poll(events, batch.cursor, normalized=True)

    def _cached(self, record):
        path = self.cache_root / (record.get("fingerprint", "missing") + ".json")
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            # 旧共享目录仅作迁移回退；缺文件时 _get 会无条件读取，不把缓存当事实。
            try:
                value = json.loads((self.legacy_root / path.name).read_text())
            except (OSError, ValueError):
                return None
            self._save_response(value)
            return value

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
            result = identity_run(args, text=True, capture_output=True, timeout=min(12, remaining))
        except TargetBlocked as exc:
            # 此处只发 GET；签发失败和读取结果未确认均可重试，写入仍保留原阻塞语义。
            raise Retryable(str(exc)) from None
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

    @scoped
    def poll(self, target, cursor):
        self.legacy_root = self.state_dir or Path(target.get(
            "github_cache_dir", "~/.local/state/workshop/github-responses")).expanduser()
        scope = hashlib.sha256(json.dumps([target["repo"], target["name"]]).encode()).hexdigest()
        self.cache_root = self.legacy_root / scope
        self.cache_root.mkdir(parents=True, exist_ok=True)
        # 同一交付对象的读取与回收串行；各交付对象不能清理彼此的缓存。
        with (self.cache_root / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._poll(target, cursor)

    def _poll(self, target, cursor):
        state = deepcopy(json.loads(cursor)) if cursor else {"cache": {}, "known": {}, "seen": {}}
        cache, known, seen = state["cache"], state["known"], state["seen"]
        for record in cache.values():
            if "data" in record:
                record["fingerprint"] = self._save_response(record.pop("data"))
        # 缺水位的旧游标必须重新完整建立快照，不能沿用旧扫描的 refreshed。
        if state.get("version") != 2:
            state["refreshed"] = []
            state["version"] = 2
            state["legacy_cleanup"] = "copy"
            # 先保留旧游标引用的每份响应，待新目录的游标确认后才清理共享目录。
            for record in cache.values():
                self._cached(record)
        self.refreshed = set(state.get("refreshed", []))
        self.requests = 0
        self.limit = target.get("github_requests_per_tick", 20)
        self.deadline = time.monotonic() + target.get("github_poll_seconds", 15)
        overlap = target.get("github_overlap_seconds", 60)
        if type(self.limit) is not int or self.limit < 1:
            raise ValueError("github_requests_per_tick 必须是正整数")
        if type(overlap) is not int or overlap < 0:
            raise ValueError("github_overlap_seconds 必须是非负整数")
        repo, name = target["repo"], target["name"]
        base = f"repos/{repo}"
        label = target.get("label", "Ready for Agent")
        bots = set(target.get("github_bot_logins", []))
        listed = state.setdefault("listed", {})
        pr_keys = state.setdefault("pr_keys", {})
        open_prs = state.setdefault("open_prs", {})
        # 磁盘回收延迟到下一次成功轮询，当前已确认的游标在返回/确认之间仍可重放。
        pinned = {r["fingerprint"] for r in cache.values() if r.get("fingerprint")}
        events = []

        def endpoint(path, watermark, **params):
            if watermark:
                since = datetime.fromisoformat(watermark.replace("Z", "+00:00")) - timedelta(seconds=overlap)
                params["since"] = since.isoformat().replace("+00:00", "Z")
            return base + path + "?" + urlencode({**params, "per_page": 100})

        def latest(rows, previous):
            # 空仓库也能结束首次建立快照；以后没有变化不把水位推进到本机时间。
            return max([previous or "1970-01-01T00:00:00Z", *[r["updated_at"] for r in rows]])

        def marker(pr):
            matches = re.findall(r"<!-- workshop-item: ([^\s<>]+) -->", pr.get("body") or "")
            if len(matches) == 1 and matches[0].startswith(name + ":"):
                return matches[0][len(name) + 1:]
            return None

        def emit(kind, number, key, identity, value, initial=False):
            signature = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            identity_key = f"{kind}:{number}:{identity}"
            previous = seen.get(identity_key)
            if previous == signature:
                return
            ext = f"{repo}:{identity_key}"
            seen[identity_key] = signature
            actor, author = value.get('actor') or {}, value.get('user') or {}
            body = {'kind': kind, 'repo': repo, 'number': number, 'data': value,
                    'actor': actor.get('login', ''), 'author': author.get('login', ''),
                    'label': (value.get('label') or {}).get('name', ''),
                    'text': (value.get('body') or '').strip(), 'edited': previous is not None,
                    'by_workshop': initial or actor.get('type') == 'Bot' or automated(value)}
            events.append(Event('github', f'{ext}:{signature}', name, key, f'{repo} #{number}', body,
                                action_id=ext, previous_ids=[ext, f'{ext}:{signature}']))

        def automated(value):
            user = value.get("user") or {}
            return (user.get("type") == "Bot" or user.get("login") in bots
                    or "<!-- workshop-comment:" in (value.get("body") or ""))

        def comments(endpoint, kind, number, key):
            for comment in self._list(endpoint + "?per_page=100", cache):
                if automated(comment):
                    continue
                emit(kind, number, key, comment['id'], comment)

        def read_issue(number):
            issue, _ = self._get(f"{base}/issues/{number}", cache)
            changes = self._list(f"{base}/issues/{number}/events?per_page=100", cache)
            emit("issue", number, number, "body", issue)
            own_intake = re.search(r"<!-- workshop-intake: [^<>]+ -->", issue.get("body") or "")
            initial = next((c.get("id") for c in changes if c.get("event") == "labeled"
                            and c.get("label", {}).get("name") == label), None) if own_intake else None
            own_labels = state.setdefault("own_labels", {})
            if initial is not None:
                own_labels[number] = initial
            initial = own_labels.get(number)
            for change in changes:
                if change.get('event') == 'labeled':
                    emit('label', number, number, change['id'], change, change['id'] == initial)
            comments(f"{base}/issues/{number}/comments", "issue-comment", number, number)

        def read_pr(number):
            pr, _ = self._get(f"{base}/pulls/{number}", cache)
            key = marker(pr)
            if key is None:
                open_prs.pop(number, None)
                pr_keys.pop(number, None)
                return
            sha = pr["head"]["sha"]
            pr_keys[number] = key
            emit("pr-head", number, key, "head", {"sha": sha, "body": pr.get("body"), "state": pr["state"]})
            comments(f"{base}/issues/{number}/comments", "pr-comment", number, key)
            comments(f"{base}/pulls/{number}/comments", "pr-review-comment", number, key)
            for review in self._list(f"{base}/pulls/{number}/reviews?per_page=100", cache):
                if not automated(review):
                    emit('pr-review', number, key, review['id'], review)
            if pr["state"] == "open":
                open_prs[number] = {"sha": sha, "key": key}
            else:
                open_prs.pop(number, None)

        try:
            issues = self._list(endpoint("/issues", state.get("watermark"), state="all", sort="updated"), cache)
            for issue in issues:
                number = str(issue["number"])
                signature = hashlib.sha256(json.dumps(issue, sort_keys=True).encode()).hexdigest()
                # 重叠会反复列出最后一批对象；内容未变时不能反复查询详情。
                if listed.get(number) == signature:
                    continue
                if "pull_request" in issue:
                    if marker(issue) is None:
                        open_prs.pop(number, None)
                        pr_keys.pop(number, None)
                        listed.pop(number, None)
                        continue
                    read_pr(number)
                elif number in known or issue.get("state") != "closed":
                    known[number] = True
                    read_issue(number)
                else:
                    continue
                # 全部详情完成后再记摘要，中途让出请求预算时下次仍会补完。
                listed[number] = signature

            review_comments = self._list(endpoint("/pulls/comments", state.get("review_watermark"),
                                                  sort="updated", direction="asc"), cache)
            for comment in review_comments:
                parent = re.fullmatch(r"https://api\.github\.com/" + re.escape(base) + r"/pulls/(\d+)",
                                      comment.get("pull_request_url", ""))
                if parent is None or parent[1] not in pr_keys or automated(comment):
                    continue
                number = parent[1]
                emit('pr-review-comment', number, pr_keys[number], comment['id'], comment)


            # CI 的变化不必推进 PR.updated_at；只保留开放标记对象的头提交。
            for number, pr in open_prs.items():
                sha = pr["sha"]
                checks, _ = self._get(f"{base}/commits/{sha}/check-runs?per_page=100", cache)
                status, _ = self._get(f"{base}/commits/{sha}/status", cache)
                emit("pr-ci", number, pr["key"], sha, {"sha": sha, "checks": checks, "status": status})
        except _YieldPoll:
            state["refreshed"] = sorted(self.refreshed)
        else:
            state["watermark"] = latest(issues, state.get("watermark"))
            state["review_watermark"] = latest(review_comments, state.get("review_watermark"))
            state["cache"] = {k: v for k, v in cache.items() if k in self.refreshed}
            state["refreshed"] = []
            keep = pinned | {r["fingerprint"] for r in state["cache"].values()}
            for path in self.cache_root.glob("*.json"):
                if path.stem not in keep:
                    path.unlink()
            if state.get("legacy_cleanup") == "ready":
                # 旧共享目录仅存可重建响应。尚未迁移的其他游标缺缓存会重新 GET，
                # 不丢 seen/known/事件；各交付对象的新目录永不互相清理。
                for path in self.legacy_root.glob("*.json"):
                    if re.fullmatch(r"[0-9a-f]{64}", path.stem):
                        path.unlink(missing_ok=True)
                state.pop("legacy_cleanup")
            elif state.get("legacy_cleanup") == "copy":
                state["legacy_cleanup"] = "ready"
        return Poll(events, json.dumps(state, ensure_ascii=False), normalized=True)


def make_source():
    return GitHubSource()
