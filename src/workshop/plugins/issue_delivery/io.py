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
import urllib.request
import urllib.error

from workshop.core.ports import Retryable


def executable(name):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"找不到 {name}；请在启动 PATH 中配置")
    return str(Path(path).resolve())


def run(argv, *, cwd=None, env=None, timeout=20, check=True):
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Retryable(f"命令未确认完成：{argv[0]}") from exc
    if check and result.returncode:
        raise Retryable(f"命令失败：{argv[0]}（exit {result.returncode}）")
    return result


def clean_env(home):
    # 白名单环境；不能让检查脚本继承守护进程的 GitHub、SSH、AWS 等凭据。
    return {"PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/bin:/bin"),
            "HOME": str(home), "LANG": "en_US.UTF-8", "TMPDIR": str(home),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GH_CONFIG_DIR": str(home / "gh"),
            "GH_TOKEN": "disabled-in-review", "GITHUB_TOKEN": "disabled-in-review",
            "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "KUBECONFIG": "/dev/null"}


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
            raise Retryable("GitHub 写入结果未确认；重试前回查")
        return json.loads(result.stdout)

    def pages(self, endpoint):
        pages = json.loads(run([self.gh, "api", "--paginate", "--slurp", endpoint]).stdout)
        return [entry for page in pages for entry in page]

    def pr(self, item, target):
        marker = f'<!-- workshop-item: {item["id"]} -->'
        rows = self.pages(f'repos/{target["repo"]}/pulls?state=all&per_page=100')
        matches = [row for row in rows if marker in (row.get("body") or "")]
        opened = [row for row in matches if row["state"] == "open"]
        if len(opened) > 1:
            raise ValueError("同一件活有多个打开的 PR")
        if not opened and not matches:
            raise ValueError("找不到带活标记的 PR")
        chosen = opened[0] if opened else max(matches, key=lambda row: row["number"])
        return self.api(f'repos/{target["repo"]}/pulls/{chosen["number"]}')

    def copy(self, target, sha, destination):
        if not isinstance(sha, str) or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("必须用完整提交号")
        destination = Path(destination)
        # 不共享 objects/config/hooks，也不从交付对象工作区拷贝未跟踪文件。
        run([self.git, "-c", "core.hooksPath=/dev/null", "clone", "--no-checkout", "--no-local",
             str(Path(target["checkout"]).expanduser()), str(destination)])
        run([self.git, "-C", str(destination), "-c", "core.hooksPath=/dev/null", "fetch",
             f'https://github.com/{target["repo"]}.git', sha])
        run([self.git, "-C", str(destination), "-c", "core.hooksPath=/dev/null", "checkout", "--detach", sha])
        run([self.git, "-C", str(destination), "remote", "set-url", "origin", "https://invalid.invalid/read-only"])
        for name, value in (("core.hooksPath", "/dev/null"), ("credential.helper", "")):
            run([self.git, "-C", str(destination), "config", name, value])
        return destination

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
            final.chmod(final.stat().st_mode & ~0o222)
            return str(final)
        destination = Path(tempfile.mkdtemp(prefix="review-", dir=root)) / "repo"
        self.copy(target, sha, destination)
        for path in destination.rglob("*"):
            if not path.is_symlink():
                path.chmod(path.stat().st_mode & ~0o222)
        destination.rename(final)
        final.chmod(final.stat().st_mode & ~0o222)
        destination.parent.rmdir()
        return str(final)

    def cleanup_reviews(self, target, keep):
        root = self.root(target) / "reviews"
        if not root.exists():
            return
        keep = {str(Path(p).resolve()) for p in keep if p}
        for directory in root.iterdir():
            if str(directory.resolve()) in keep or not directory.is_dir() or directory.is_symlink():
                continue
            # 只清理由本插件管理的目录；先解开只读位，不追随文件链接。
            for path in [directory, *directory.rglob("*")]:
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o700)
            shutil.rmtree(directory)

    def bot_review(self, item, target, pr):
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
                             "body": f'<!-- workshop-comment: {item["id"]} --> 已按此提交审查'}).encode(),
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                json.load(response)
        except (OSError, ValueError) as exc:
            raise Retryable("bot 批准结果未确认，重试前回查提交号") from None

    def verify(self, target, sha, checks):
        with tempfile.TemporaryDirectory(prefix="workshop-check-") as directory:
            root = Path(directory)
            copy = self.copy(target, sha, root / "repo")
            home = root / "home"
            home.mkdir()
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
                    result = subprocess.run(command, cwd=cwd, env=clean_env(home),
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
