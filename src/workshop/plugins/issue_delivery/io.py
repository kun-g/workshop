"""进程边界、独立副本和插件自己的幂等记录，不访问核心数据库。"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request
import urllib.error

from workshop.core.ports import Retryable


def executable(name):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"找不到 {name}；请在启动 PATH 中配置")
    return str(Path(path).resolve())


def tail(text, limit=400):
    """命令输出的结尾，放进错误信息和通知，够判断原因又不刷屏。"""
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def failure(argv, result):
    detail = tail(result.stderr) or tail(result.stdout)
    return f"命令失败：{argv[0]}（exit {result.returncode}）" + (f"：{detail}" if detail else "")


def run(argv, *, cwd=None, env=None, timeout=20, check=True):
    try:
        # 输出解不了码也不能变成插件故障：坏字节换成替代字符。
        result = subprocess.run(argv, cwd=cwd, env=env, text=True, errors="replace", capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Retryable(f"命令未确认完成：{argv[0]}") from exc
    if check and result.returncode:
        raise Retryable(failure(argv, result))
    return result


CACHE_KEEP = 5
STALE_COPY_SECONDS = 6 * 3600


def writable(path):
    """恢复目录树的属主读写执行位（不追随文件链接），以便删除只读副本。"""
    for entry in [path, *path.rglob("*")]:
        if not entry.is_symlink():
            entry.chmod(entry.stat().st_mode | 0o700)


def readonly(path):
    for entry in [*path.rglob("*"), path]:
        if not entry.is_symlink():
            entry.chmod(entry.stat().st_mode & ~0o222)


def clean_env(home, cache=None):
    # 白名单环境；不能让检查脚本继承守护进程的 GitHub、SSH、AWS 等凭据。
    return {"PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/bin:/bin"),
            "HOME": str(home), "LANG": "en_US.UTF-8", "TMPDIR": str(home),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GH_CONFIG_DIR": str(home / "gh"),
            "GH_TOKEN": "disabled-in-review", "GITHUB_TOKEN": "disabled-in-review",
            "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "KUBECONFIG": "/dev/null",
            # 副本是主检出的 worktree，共用它的钩子目录；检查里跑的 git 一律不执行钩子。
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null",
            **({"UV_CACHE_DIR": str(cache)} if cache else {})}


class IO:
    def __init__(self, gh=None, git=None, state_dir=None, lark=None):
        self.gh = gh or executable("gh")
        self.git = git or executable("git")
        self.lark = lark or shutil.which("lark-cli")
        self.state_dir = Path(state_dir).resolve() if state_dir else None

    def root(self, target):
        return self.state_dir or Path(target.get("issue_delivery_state_dir", "~/.local/state/workshop/issue_delivery")).expanduser().resolve()

    def path(self, item, target):
        digest = hashlib.sha256(item["id"].encode()).hexdigest()
        return self.root(target) / (digest + ".json")

    def read(self, item, target):
        path = self.path(item, target)
        return json.loads(path.read_text()) if path.exists() else {"effects": {}}

    def save(self, item, target, state):
        path = self.path(item, target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as file:
            json.dump(state, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(file.name, path)

    @contextmanager
    def locked(self, item, target):
        path = self.path(item, target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield self.read(item, target)

    def api(self, endpoint, *, data=None):
        argv = [self.gh, "api", endpoint]
        if data is None:
            return json.loads(run(argv).stdout)
        # JSON 经 stdin，外部文字不进入 shell 或参数选项。
        result = subprocess.run(argv + ["--method", "POST", "--input", "-"],
                                input=json.dumps(data), text=True, capture_output=True, timeout=20)
        if result.returncode:
            detail = tail(result.stderr)
            raise Retryable("GitHub 写入结果未确认；重试前回查" + (f"：{detail}" if detail else ""))
        return json.loads(result.stdout)

    def pages(self, endpoint):
        pages = json.loads(run([self.gh, "api", "--paginate", "--slurp", endpoint]).stdout)
        return [entry for page in pages for entry in page]

    def item_prs(self, item, target):
        marker = f'<!-- workshop-item: {item["id"]} -->'
        rows = self.pages(f'repos/{target["repo"]}/pulls?state=all&per_page=100')
        return [row for row in rows if marker in (row.get("body") or "")]

    def pr(self, item, target):
        matches = self.item_prs(item, target)
        opened = [row for row in matches if row["state"] == "open"]
        if len(opened) > 1:
            raise ValueError("同一件活有多个打开的 PR")
        if not opened and not matches:
            raise ValueError("找不到带活标记的 PR")
        chosen = opened[0] if opened else max(matches, key=lambda row: row["number"])
        return self.api(f'repos/{target["repo"]}/pulls/{chosen["number"]}')

    def _checkout_git(self, target):
        checkout = Path(target["checkout"]).expanduser().resolve()
        return [self.git, "-C", str(checkout), "-c", "core.hooksPath=/dev/null"]

    @contextmanager
    def _copies_locked(self, target):
        """同一主检出上的 fetch、worktree add/remove/prune 串行；跑检查不持锁。"""
        root = self.root(target) / "copies"
        root.mkdir(parents=True, exist_ok=True)
        checkout = str(Path(target["checkout"]).expanduser().resolve())
        with (root / (hashlib.sha256(checkout.encode()).hexdigest()[:16] + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def copy(self, target, sha, destination):
        """在交付对象主检出上开 detached worktree：只含该提交的受跟踪文件，耗时只随文件数涨，不随历史涨。

        钩子关掉；需要时只增量 fetch 这一个提交，且不写主检出的 FETCH_HEAD、不动它的分支和工作区。
        """
        if not isinstance(sha, str) or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("必须用完整提交号")
        destination = Path(destination)
        git = self._checkout_git(target)
        with self._copies_locked(target):
            if run(git + ["cat-file", "-e", sha + "^{commit}"], check=False).returncode:
                run(git + ["fetch", "--no-tags", "--no-write-fetch-head",
                           f'https://github.com/{target["repo"]}.git', sha], timeout=120)
            run(git + ["worktree", "add", "--detach", str(destination), sha], timeout=120)
        return destination

    def drop(self, target, path):
        """收回 copy() 开的副本；只读副本先恢复写权限。目录已不在时只清登记。"""
        path = Path(path)
        git = self._checkout_git(target)
        with self._copies_locked(target):
            if path.exists() and run(git + ["worktree", "remove", "--force", str(path)], check=False, timeout=120).returncode:
                writable(path)
                shutil.rmtree(path)
            run(git + ["worktree", "prune"])

    def dependency_cache(self, target, copy):
        """按 uv.lock 内容分的 uv 缓存；同一锁文件的检查复用已下载的包，换锁文件就用新目录。"""
        lock = Path(copy) / "uv.lock"
        if not lock.is_file():
            return None
        root = self.root(target) / "uv-cache"
        cache = root / hashlib.sha256(lock.read_bytes()).hexdigest()[:16]
        cache.mkdir(parents=True, exist_ok=True)
        os.utime(cache)
        # 只留最近用过的几份；一天内用过的不删，免得删掉别的检查正在用的。
        stale = sorted((d for d in root.iterdir() if d.is_dir() and not d.is_symlink()),
                       key=lambda d: d.stat().st_mtime, reverse=True)[CACHE_KEEP:]
        for directory in stale:
            if time.time() - directory.stat().st_mtime > 86400:
                writable(directory)
                shutil.rmtree(directory, ignore_errors=True)
        return cache

    @contextmanager
    def release_checkout(self, target, sha, *, extra_refs=()):
        """交付对象专用可写检出；保留用户的 git 认证，绝不修改主检出。"""
        root = self.root(target) / "releases"
        root.mkdir(parents=True, exist_ok=True)
        identity = hashlib.sha256((target["name"] + ":" + target["repo"]).encode()).hexdigest()
        checkout = root / identity
        # 同目标不同活共享检出，锁覆盖 fetch、切提交及调用仓库脚本全过程。
        with (root / (identity + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not checkout.exists():
                with tempfile.TemporaryDirectory(prefix="clone-", dir=root) as directory:
                    temporary = Path(directory) / "repo"
                    run([self.git, "clone", "--no-checkout", f'https://github.com/{target["repo"]}.git', str(temporary)])
                    temporary.rename(checkout)
            run([self.git, "-C", str(checkout), "fetch", "origin", "--tags", "main", sha, *extra_refs])
            run([self.git, "-C", str(checkout), "checkout", "--detach", sha])
            yield checkout

    def review_copy(self, item, target, sha):
        root = self.root(target) / "reviews"
        root.mkdir(parents=True, exist_ok=True)
        identity = hashlib.sha256(item["id"].encode()).hexdigest()[:16]
        final = root / f"{identity}-{sha}"
        if final.exists():
            # 去写权限最后才动顶层目录；顶层还可写说明上次没做完，补做一遍。
            if final.stat().st_mode & 0o222:
                readonly(final)
            return str(final)
        destination = Path(tempfile.mkdtemp(prefix="review-", dir=root)) / "repo"
        self.copy(target, sha, destination)
        with self._copies_locked(target):
            # worktree 不能直接改名，否则主检出里的登记指向旧路径。
            run(self._checkout_git(target) + ["worktree", "move", str(destination), str(final)])
        destination.parent.rmdir()
        readonly(final)
        return str(final)

    def cleanup_reviews(self, target, keep):
        root = self.root(target) / "reviews"
        if not root.exists():
            return
        keep = {str(Path(p).resolve()) for p in keep if p}
        for directory in root.iterdir():
            if str(directory.resolve()) in keep or not directory.is_dir() or directory.is_symlink():
                continue
            # 只清理由本插件管理的目录；先解开只读位，不追随文件链接，再收回主检出上的登记。
            writable(directory)
            self.drop(target, directory)

    def bot_review(self, item, target, pr, body="已按此提交审查"):
        config = target.get("bot_review")
        if not config:
            return
        endpoint = f'repos/{target["repo"]}/pulls/{pr["number"]}/reviews'
        reviews = self.pages(endpoint + "?per_page=100")
        if any(r.get("user", {}).get("login") == config["login"] and r.get("state") == "APPROVED"
               and r.get("commit_id") == pr["head"]["sha"] for r in reviews):
            return
        env = {**os.environ, "AGENT_KIND": "workshop", "AGENT_SESSION_ID": item["id"]}
        signer = [str(Path(p).expanduser()) for p in config["signer"]]
        signed = run(signer + ["token", "--repo", target["repo"]], env=env)
        token = signed.stdout.strip()
        if not token:
            raise Retryable("bot 签发结果为空")
        request = urllib.request.Request("https://api.github.com/" + endpoint,
            data=json.dumps({"event": "APPROVE", "commit_id": pr["head"]["sha"],
                             "body": f'<!-- workshop-comment: {item["id"]} --> {body}'}).encode(),
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                json.load(response)
        except (OSError, ValueError) as exc:
            raise Retryable("bot 批准结果未确认，重试前回查提交号") from None

    def cleanup_copies(self, target, older_than=STALE_COPY_SECONDS):
        """进程中途被杀留下的检查副本：超过期限的删掉并收回登记。正在跑的检查远短于这个期限。"""
        root = self.root(target) / "copies"
        if not root.exists():
            return
        for directory in root.iterdir():
            if (directory.is_dir() and not directory.is_symlink() and directory.name.startswith("check-")
                    and time.time() - directory.stat().st_mtime > older_than):
                writable(directory)
                self.drop(target, directory / "repo")
                shutil.rmtree(directory, ignore_errors=True)

    def verify(self, target, sha, checks):
        parent = self.root(target) / "copies"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="check-", dir=parent) as directory:
            root = Path(directory)
            copy = self.copy(target, sha, root / "repo")
            try:
                return self._run_checks(target, copy, root, checks)
            finally:
                self.drop(target, copy)

    def _run_checks(self, target, copy, root, checks):
        home = root / "home"
        home.mkdir()
        cache = self.dependency_cache(target, copy)
        output = []
        steps = [(p["argv"], copy / p.get("cwd", "."), "prepare") for p in target.get("prepare", [])]
        steps += [(argv, copy, "check") for argv in checks]
        for argv, cwd, kind in steps:
            if not cwd.resolve().is_relative_to(copy.resolve()):
                raise ValueError("准备命令目录须在干净副本内")
            command = list(argv)
            if "/" not in command[0]:
                command[0] = executable(command[0])
            try:
                result = subprocess.run(command, cwd=cwd, env=clean_env(home, cache),
                    timeout=target.get("check_timeout", 60), text=True, capture_output=True)
                entry = {"argv": argv, "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr, "kind": kind}
            except subprocess.TimeoutExpired as exc:
                def decoded(value):
                    return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
                entry = {"argv": argv, "exit_code": 124, "stdout": decoded(exc.stdout),
                         "stderr": decoded(exc.stderr), "kind": kind, "timed_out": True}
            output.append(entry)
            if entry["exit_code"]:
                break
        return output
