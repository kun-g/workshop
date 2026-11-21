"""守护进程在跑的版本：启动时记一次，并把那份代码导出给后台作业。

主检出可能被别的程序提前快进（例如 Orca 建工作区时快进本地 main，#126），
作业子进程又从磁盘加载代码，所以「在跑的版本」只能由守护进程启动时记下。
作业进程用 PYTHONPATH 指向导出的那份代码，主检出快进后也不会加载更新的模块。
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

VARIABLE = "WORKSHOP_RUNNING_COMMIT"
EXPORTS = "running-code"
KEEP_EXPORTS = 3


def tree():
    """本模块所在的那份 workshop 包目录（导入期已确定，之后主检出再变也不影响）。"""
    return Path(__file__).resolve().parent


def _head(source):
    try:
        result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and re.fullmatch("[0-9a-f]{40}", commit) else None


def record(source=None):
    """守护进程启动时调用：记下本进程加载的代码所在提交；读不到就清掉，不留旧值。"""
    os.environ.pop(VARIABLE, None)
    commit = _head(Path(source) if source else tree())
    if commit:
        os.environ[VARIABLE] = commit
    return commit


def current():
    commit = os.environ.get(VARIABLE, "")
    return commit if re.fullmatch("[0-9a-f]{40}", commit) else None


def _from_git(source, commit, destination):
    """按提交导出源码；返回包目录。不是 git 仓库、提交不在主检出或导出失败时返回 None。"""
    try:
        toplevel = Path(subprocess.run(["git", "-C", str(source), "rev-parse", "--show-toplevel"],
                                       capture_output=True, text=True, timeout=10, check=True)
                        .stdout.strip())
        relative = source.resolve().relative_to(toplevel.resolve())
        packed = subprocess.run(["git", "-C", str(toplevel), "archive", "--format=tar", commit, str(relative)],
                                capture_output=True, timeout=60, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(packed)) as archive:
            members = [m for m in archive.getmembers()
                       if m.isfile() and not Path(m.name).is_absolute() and ".." not in Path(m.name).parts]
            archive.extractall(destination, members=members, filter="data")
        built = destination / relative
        if (built / "core" / "models.py").is_file():
            return built
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, tarfile.TarError, ValueError):
        pass
    return None


def _from_tree(source, destination):
    """没有可用的提交时复制当前进程加载的代码。"""
    built = destination / "workshop"
    shutil.copytree(source, built, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return built


def _digest(directory):
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        digest.update(str(path.relative_to(directory)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:40]


def export(state_dir, source=None, commit=None):
    """把在跑的那份代码导出到状态目录；返回可直接放进 PYTHONPATH 的目录（内含 workshop 包）。

    同一提交只导出一次。导出中途退出时没有 ready 标记，下次重导，不认半份代码。
    """
    source = Path(source or tree()).resolve()
    commit = commit or current() or _head(source)
    name = commit or _digest(source)
    root = Path(state_dir).expanduser().resolve() / EXPORTS
    target = root / name
    ready = target / "ready.json"
    if not ready.exists():
        root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        with tempfile.TemporaryDirectory(prefix=".build-", dir=root) as temporary:
            building = Path(temporary)
            built = (_from_git(source, commit, building) if commit else None) or _from_tree(source, building)
            built.rename(building / "package")
            target.mkdir()
            (building / "package").rename(target / "workshop")
            ready.write_text(json.dumps({"source": str(source), "commit": commit}, ensure_ascii=False))
    _prune(root, target)
    return target


def _prune(root, keep):
    try:
        candidates = sorted((path for path in root.iterdir()
                              if path.is_dir() and not path.name.startswith(".")),
                             key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    for path in candidates[KEEP_EXPORTS:]:
        if path != keep:
            shutil.rmtree(path, ignore_errors=True)


def prepare(state_dir, source=None):
    """守护进程启动时调用：记版本并导出代码，作业子进程经继承的 PYTHONPATH 加载它。"""
    source = Path(source or tree())
    # 从本进程加载的代码算版本（读 git），不吃启动环境里可能残留的旧值。
    commit = record(source)
    try:
        exported = export(state_dir, source=source, commit=commit)
    except OSError as exc:
        # 没有导出就无法保证前后台同版本；宁可起不来，也不混用新旧代码。
        raise RuntimeError(f"导出在跑的代码失败，不能启动后台作业：{exc}") from exc
    parts = [str(exported)] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    os.environ["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
    return commit


def process_identity(pid):
    """进程号和操作系统启动时间，防止把复用的 pid 当成同一进程。"""
    if pid <= 1:
        return None
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
                            capture_output=True, text=True, timeout=5)
    start = result.stdout.strip()
    return {'pid': pid, 'start': start} if result.returncode == 0 and start else None
