"""进程边界、独立副本和插件自己的幂等记录，不访问核心数据库。"""
from contextlib import contextmanager
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from urllib.parse import quote

from .requirements import registered_paths

from workshop.core.ports import Retryable
from workshop import github_identity

# 审查只读副本刚建或刚复用后这么久内不清：派活说明（建副本）和预订这一轮在不同的拍，
# 清理作业又和它们并行跑，副本可能还没登进「在用」名单。
REVIEW_GRACE_SECONDS = 3600


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
        result = github_identity.run(argv, cwd=cwd, env=env, text=True, errors="replace", capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Retryable(f"命令未确认完成：{argv[0]}") from exc
    if check and result.returncode:
        raise Retryable(failure(argv, result))
    return result


CACHE_KEEP = 5
STALE_COPY_SECONDS = 6 * 3600
# 检查进程的 HOME/TMPDIR 建在这里，不建在状态目录深处（ws#308）：检查在 TMPDIR 下拼 unix socket
# 路径时（macOS 上限 104 字节）长度不随状态目录的深度涨。副本目录里留一个 home 链接指过来，
# 跑检查的进程中途被杀时 cleanup_copies 顺着它收走。
CHECK_HOME_ROOT = Path("/tmp")
CHECK_HOME_PREFIX = "workshop-check-"


def _chmod(entry, change):
    # 遍历途中文件被别的作业删掉（同时收回同一副本）不算错，跳过即可。
    try:
        if not entry.is_symlink():
            entry.chmod(change(entry.stat().st_mode))
    except FileNotFoundError:
        pass


def writable(path):
    """恢复目录树的属主读写执行位（不追随文件链接），以便删除只读副本。"""
    for entry in [path, *path.rglob("*")]:
        _chmod(entry, lambda mode: mode | 0o700)


def readonly(path):
    for entry in [*path.rglob("*"), path]:
        _chmod(entry, lambda mode: mode & ~0o222)


def worktree_owner(directory):
    """副本是哪个主检出开的 worktree：读 .git 文件里的 gitdir，返回主检出的 .git 目录；看不出来返回 None。

    改名前的临时目录（review-*/repo）也算。"""
    for candidate in (directory / ".git", directory / "repo" / ".git"):
        try:
            text = candidate.read_text()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            continue
        if text.startswith("gitdir:"):
            gitdir = Path(text.split(":", 1)[1].strip())
            if gitdir.parent.name == "worktrees":
                return gitdir.parent.parent.resolve()
    return None


@contextmanager
def check_home(check_dir):
    """检查专用的空 HOME（兼 TMPDIR），用完即删；在检查副本目录 check_dir 下登记一个 home 链接。"""
    with tempfile.TemporaryDirectory(prefix=CHECK_HOME_PREFIX, dir=CHECK_HOME_ROOT) as directory:
        home = Path(directory)
        (Path(check_dir) / "home").symlink_to(home)
        try:
            yield home
        finally:
            writable(home)


def sweep_check_home(check_dir):
    """收走被杀进程留下的检查 HOME：只认 CHECK_HOME_ROOT 下本前缀的目录，别的链接不追。"""
    link = Path(check_dir) / "home"
    if not link.is_symlink():
        return
    home = Path(os.readlink(link))
    if home.parent == CHECK_HOME_ROOT and home.name.startswith(CHECK_HOME_PREFIX) \
            and home.is_dir() and not home.is_symlink():
        writable(home)
        shutil.rmtree(home, ignore_errors=True)


def requested_python(copy, cwd=None):
    """副本里 .python-version 要的解释器（第一条非注释行），同 uv 从命令目录往上找到副本根为止；没有返回 None。"""
    copy = Path(copy).resolve()
    directory = Path(cwd or copy).resolve()
    for folder in (directory, *directory.parents):
        if not folder.is_relative_to(copy):
            break
        try:
            lines = (folder / ".python-version").read_text().splitlines()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError, UnicodeError):
            continue
        return next((line.strip() for line in lines if line.strip() and not line.strip().startswith("#")), None)
    return None


def check_python(copy, cwd=None):
    """检查用哪个解释器，返回 (真实路径, None)，本机没有副本要的版本时返回 (None, 原因)；没有 uv 时不定。

    副本有 .python-version 的，在本机已装的解释器里找（uv 管理的和 PATH 上的），不下载；
    没有的，用车间进程自身的基础解释器（同定时发版）。不靠 PATH 上手工建的链接（ws#327）。
    """
    request = requested_python(copy, cwd)
    if request is None:
        return str(Path(sys._base_executable).resolve()), None
    uv = shutil.which("uv")
    if not uv:
        # 没有 uv 就没有谁会去下载解释器；按副本自己的办法找，不替它定。
        return None, None
    # 只找不装：真实 HOME 下 uv 管理的解释器也算本机已装；不读副本里的项目和配置。
    env = {k: v for k, v in os.environ.items() if k != "UV_PYTHON"}
    env["UV_PYTHON_DOWNLOADS"] = "never"
    try:
        found = subprocess.run([uv, "python", "find", request, "--no-python-downloads", "--no-project", "--no-config"],
                               env=env, text=True, errors="replace", capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"找副本 .python-version 要的 Python {request} 没有完成：{exc}"
    path = found.stdout.strip().splitlines()[-1:] if not found.returncode else []
    if not path or not Path(path[0]).is_file():
        return None, (f"本机没有副本 .python-version 要的 Python {request}；检查不下载解释器"
                      f"（UV_PYTHON_DOWNLOADS=never）。请 Kun 装好这个版本，或改 .python-version。"
                      + (f"\n{tail(found.stderr)}" if tail(found.stderr) else ""))
    return str(Path(path[0]).resolve()), None


def clean_env(home, cache=None, python=None):
    # 白名单环境；不能让检查脚本继承守护进程的 GitHub、SSH、AWS 等凭据。
    # 解释器固定、不下载（ws#327）：下载 CPython 会让检查超时，换台机器也不该靠手工链接。
    return {"PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/bin:/bin"),
            "HOME": str(home), "LANG": "en_US.UTF-8", "TMPDIR": str(home),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GH_CONFIG_DIR": str(home / "gh"),
            "GH_TOKEN": "disabled-in-review", "GITHUB_TOKEN": "disabled-in-review",
            "AWS_CONFIG_FILE": "/dev/null", "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "KUBECONFIG": "/dev/null",
            # 副本是主检出的 worktree，共用它的钩子目录；检查里跑的 git 一律不执行钩子。
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null",
            "UV_PYTHON_DOWNLOADS": "never",
            **({"UV_PYTHON": str(python)} if python else {}),
            **({"UV_CACHE_DIR": str(cache)} if cache else {})}


SELF_REVIEW = "作者就是审核身份，不发批准"


def bot_login(login):
    """同一个 App 两种写法：gh 给 app/名字，REST 给 名字[bot]；统一成后者。登录名不分大小写。
    不去掉 [bot]：同名的普通用户和 App 不是一个人。"""
    login = (login or "").strip().lower()
    if login.startswith("app/"):
        login = login.removeprefix("app/") + "[bot]"
    return login


def self_review(pr, config):
    """PR 作者就是 bot_review 的审核身份：GitHub 不许作者批准自己的 PR（422），返回不发批准的原因。"""
    author = (pr.get("user") or pr.get("author") or {}).get("login") or ""
    if author and bot_login(author) == bot_login(config["login"]):
        return SELF_REVIEW
    return ""


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
        result = github_identity.run(argv + ["--method", "POST", "--input", "-"],
                                input=json.dumps(data), text=True, capture_output=True, timeout=20)
        if result.returncode:
            detail = tail(result.stderr)
            raise Retryable("GitHub 写入结果未确认；重试前回查" + (f"：{detail}" if detail else ""))
        return json.loads(result.stdout)

    def comment(self, target, number, body):
        """在 issue 或 PR 下留一条评论（App 身份由调用方的身份作用域给）。"""
        return self.api(f'repos/{target["repo"]}/issues/{int(number)}/comments', data={"body": body})

    def to_draft(self, target, pr):
        """把 PR 改回 Draft：合并前 DoD 没过，交还执行。"""
        run([self.gh, "pr", "ready", str(pr["number"]), "--repo", target["repo"], "--undo"])

    def pages(self, endpoint):
        pages = json.loads(run([self.gh, "api", "--paginate", "--slurp", endpoint]).stdout)
        return [entry for page in pages for entry in page]

    def requirement_documents(self, target):
        """回查默认分支的需求正文；不读取执行者工作区里可自行改写的版本。"""
        documents, visited = {}, set()

        def read(path, directory=False):
            if path in visited:
                return
            visited.add(path)
            value = self.api(f'repos/{target["repo"]}/contents/{quote(path, safe="/")}')
            if directory:
                if not isinstance(value, list):
                    raise ValueError(f"需求目录 {path} 不是目录")
                for entry in value:
                    child = entry.get("path", "")
                    if not child.startswith(path.rstrip("/") + "/") or "/../" in child:
                        raise ValueError(f"需求目录 {path} 返回了无效路径")
                    if entry.get("type") == "dir":
                        read(child, True)
                    elif entry.get("type") == "file" and child.endswith(".md"):
                        read(child)
                return
            if (not isinstance(value, dict) or value.get("type") != "file"
                    or value.get("encoding") != "base64" or value.get("submodule_git_url")):
                raise ValueError(f"需求文档 {path} 不是可读取的普通文件")
            try:
                documents[path] = base64.b64decode(value["content"]).decode("utf-8")
            except (ValueError, KeyError, UnicodeError) as exc:
                raise ValueError(f"需求文档 {path} 无法解码") from exc

        for path in registered_paths(target):
            read(path.rstrip("/"), path.endswith("/"))
        return documents

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

    def _git_common_dir(self, target):
        out = run(self._checkout_git(target) + ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout
        return Path(out.strip()).resolve()

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
                shutil.rmtree(path, ignore_errors=True)
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
            os.utime(final)
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
        mine = self._git_common_dir(target)
        for directory in root.iterdir():
            if str(directory.resolve()) in keep or not directory.is_dir() or directory.is_symlink():
                continue
            # 所有交付对象的只读副本在同一目录；只收本对象主检出开的，别的对象的留给它自己清。
            owner = worktree_owner(directory)
            if owner is not None and owner != mine:
                continue
            if time.time() - directory.stat().st_mtime < REVIEW_GRACE_SECONDS:
                continue
            # 只清理由本插件管理的目录；先解开只读位，不追随文件链接，再收回主检出上的登记。
            writable(directory)
            self.drop(target, directory)

    def bot_review(self, item, target, pr, body="已按此提交审查"):
        """补 bot 批准；PR 作者就是审核身份时不发，返回原因给调用方记进活里，否则返回空串。"""
        config = target.get("bot_review")
        if not config:
            return ""
        skipped = self_review(pr, config)
        if skipped:
            return skipped
        endpoint = f'repos/{target["repo"]}/pulls/{pr["number"]}/reviews'
        reviews = self.pages(endpoint + "?per_page=100")
        if any(r.get("user", {}).get("login") == config["login"] and r.get("state") == "APPROVED"
               and r.get("commit_id") == pr["head"]["sha"] for r in reviews):
            return ""
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
        return ""

    def cleanup_copies(self, target, older_than=STALE_COPY_SECONDS):
        """进程中途被杀留下的检查副本：超过期限的删掉并收回登记。正在跑的检查远短于这个期限。"""
        root = self.root(target) / "copies"
        if not root.exists():
            return
        for directory in root.iterdir():
            if (directory.is_dir() and not directory.is_symlink() and directory.name.startswith("check-")
                    and time.time() - directory.stat().st_mtime > older_than):
                writable(directory)
                sweep_check_home(directory)
                self.drop(target, directory / "repo")
                shutil.rmtree(directory, ignore_errors=True)

    def verify(self, target, sha, checks):
        parent = self.root(target) / "copies"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="check-", dir=parent) as directory, check_home(directory) as home:
            copy = self.copy(target, sha, Path(directory) / "repo")
            try:
                return self._run_checks(target, copy, home, checks)
            finally:
                self.drop(target, copy)

    def _run_checks(self, target, copy, home, checks):
        cache = self.dependency_cache(target, copy)
        output = []
        steps = [(p["argv"], copy / p.get("cwd", "."), "prepare") for p in target.get("prepare", [])]
        steps += [(argv, copy, "check") for argv in checks]
        pythons = {}
        for argv, cwd, kind in steps:
            if not cwd.resolve().is_relative_to(copy.resolve()):
                raise ValueError("准备命令目录须在干净副本内")
            if cwd not in pythons:
                pythons[cwd], missing = check_python(copy, cwd)
                if missing:
                    # 副本要的解释器本机没有：记成准备步骤没过，写明缺哪个版本，退回执行；不去下载。
                    request = requested_python(copy, cwd)
                    output.append({"argv": ["uv", "python", "find", request], "exit_code": 2, "stdout": "",
                                   "stderr": missing, "kind": "prepare", "missing_python": request})
                    break
            command = list(argv)
            if "/" not in command[0] and not shutil.which(command[0]):
                # 登记的检查要的工具本机没有：记成这条检查没过（退出码 127，同 shell），退回执行去处理；
                # 不能抛出去当插件故障，否则一件活缺工具会把同一插件下所有活一起停掉（11-13 gc#93 缺 promtool）。
                output.append({"argv": argv, "exit_code": 127, "stdout": "",
                               "stderr": f"找不到 {command[0]}：车间启动 PATH 里没有这个命令。要么改用仓库里能跑的检查，要么请 Kun 装好它。",
                               "kind": kind, "missing_tool": command[0]})
                break
            if "/" not in command[0]:
                command[0] = executable(command[0])
            try:
                result = subprocess.run(command, cwd=cwd, env=clean_env(home, cache, pythons[cwd]),
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
