"""守护进程在跑的版本：启动时记一次，后台作业经环境变量继承。

主检出可能被别的程序提前快进（例如 Orca 建工作区时快进本地 main，#126），
作业子进程又从磁盘加载代码，所以「在跑的版本」只能由守护进程启动时记下。
"""
import os
from pathlib import Path
import re
import subprocess

VARIABLE = "WORKSHOP_RUNNING_COMMIT"


def record():
    """守护进程启动时调用：记下本进程加载的代码所在提交；读不到就清掉，不留旧值。"""
    os.environ.pop(VARIABLE, None)
    try:
        result = subprocess.run(["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    commit = result.stdout.strip()
    if result.returncode or not re.fullmatch("[0-9a-f]{40}", commit):
        return None
    os.environ[VARIABLE] = commit
    return commit


def current():
    commit = os.environ.get(VARIABLE, "")
    return commit if re.fullmatch("[0-9a-f]{40}", commit) else None


def process_identity(pid):
    """进程号和操作系统启动时间，防止把复用的 pid 当成同一进程。"""
    if pid <= 1:
        return None
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
                            capture_output=True, text=True, timeout=5)
    start = result.stdout.strip()
    return {'pid': pid, 'start': start} if result.returncode == 0 and start else None
