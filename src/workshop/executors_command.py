"""workshop executors check / reload：部署目录改了以后，先校验再重启车间（#293）。

check 只读：把仓库规则和部署目录完整加载、校验一遍，不启动执行者、不写状态库。
reload 先 check，过了才用 launchctl kickstart -k 重启守护进程，等心跳证明新进程跑通了一拍。
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from .core import ladder
from .core.log import Log
from .deployment import assemble
from .registry import Registry

LABEL = 'com.kun.workshop'
ROLE_ORDER = ('writer', 'reviewer', 'acceptor', 'managers')
NONE = '—'


def roles(data, name):
    """一个执行者当哪些角色：writer/reviewer/acceptor 看 executors，managers 看项目经理候选。"""
    found = []
    for role in ROLE_ORDER[:3]:
        entries = data.get('executors', {}).get(role, [])
        entries = entries if isinstance(entries, list) else [entries]
        if any((e if isinstance(e, str) else e.get('use')) == name for e in entries):
            found.append(role)
    if name in (data.get('managers') or {}).get('candidates', []):
        found.append('managers')
    return found


def inspect(rules_path, deployment, state_dir, registry=Registry):
    """返回 (行, 警告, 阶梯行)；不通过时抛 ValueError（错误里有执行者名和文件名）。"""
    assembly = assemble(rules_path, deployment)
    configured = registry().configure(assembly.rules)
    profiles = configured.data.get('executor_profiles', {})
    lines = []
    for name, profile in profiles.items():
        lines.append('\t'.join([name, profile['driver'], profile.get('model') or NONE, profile['transport'],
                                ','.join(roles(configured.data, name)) or NONE, assembly.origins.get(name, '仓库')]))
    warnings = [f'{name}：同名替换后不再隔离（仓库里的配置档有执行者目录，部署目录里的没有 settings.json、rules.md）'
                for name in assembly.unisolated]
    try:
        log = Log.readonly(Path(state_dir) / 'workshop.db')
        try:
            running = {}
            for row in log.all('rounds', unless=('status', 'ended'), fields=('executor', 'item')):
                if row.get('executor'):
                    running.setdefault(row['executor'], []).append(row.get('item') or '')
        finally:
            log.close()
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        warnings.append(f'状态库读不了（{exc}），没查在跑的轮')
    else:
        for name in sorted(set(running) - set(profiles)):
            warnings.append(f'{name}：状态库里还有在跑的轮（{"、".join(sorted(set(running[name])))}），'
                            '加载结果里已没有这个执行者')
    return lines, warnings, ladder_lines(configured.data)


def ladder_lines(data):
    """配了执行者阶梯时每条一段：段头一行，下面每级一行，制表符分隔：级号、成员、接手者；没配时为空。

    旧写法 ladder（没名字的一条）段头不带名字，输出和以前一样（ws#301）；ladders 按阶梯名分段（ws#318）。
    """
    lines = []
    for name, table in ladder.tables(data).items():
        lines.append(f'阶梯 {name}（级号、成员、接手者）' if name else '阶梯（级号、成员、接手者）')
        lines.extend(f'{number}\t{",".join(level["members"])}\t{",".join(level["takeover"])}'
                     for number, level in enumerate(table, 1))
    return lines


def check(rules_path, deployment, state_dir, out=None, err=None, registry=Registry):
    out, err = out or sys.stdout, err or sys.stderr
    try:
        lines, warnings, ladders = inspect(rules_path, deployment, state_dir, registry)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        print(f'车间：{exc}', file=err)
        return 2
    for line in lines:
        print(line, file=out)
    for warning in warnings:
        print('警告：' + warning, file=out)
    for line in ladders:
        print(line, file=out)
    return 0


def kickstart():
    """重启车间守护进程；launchd 没有这个任务或命令失败时抛 RuntimeError。"""
    result = subprocess.run(['/bin/launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/{LABEL}'],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-300:] or f'launchctl 退出 {result.returncode}')


def heartbeat(state_dir):
    """心跳里的进程号和最近一次成功时间；读不到时都为 None。"""
    try:
        value = json.loads((Path(state_dir) / 'heartbeat.json').read_text())
        pid = value.get('process', {}).get('pid')
        ok = datetime.fromisoformat(value['last_ok_at']) if value.get('last_ok_at') else None
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return None, None
    return pid, ok


def reload(rules_path, deployment, state_dir, out=None, err=None, *, registry=Registry,
           restart=kickstart, clock=lambda: datetime.now(timezone.utc), pause=time.sleep,
           timeout=180.0, interval=2.0):
    """校验不过就停下；过了才重启，等心跳里换了进程号且成功时间晚于重启时刻。

    等待期间看门又重启了一次也算成功：只看最后起来的进程有没有跑通一拍。
    """
    out, err = out or sys.stdout, err or sys.stderr
    if check(rules_path, deployment, state_dir, out, err, registry):
        print('车间：校验没过，没有重启；改好部署目录再跑一次', file=err)
        return 2
    before, _ = heartbeat(state_dir)
    moment = clock()
    try:
        restart()
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f'车间：重启命令失败：{exc}', file=err)
        return 1
    while True:
        pid, ok = heartbeat(state_dir)
        if pid is not None and pid != before and ok is not None and ok > moment:
            print(f'车间已重启：进程 {before} → {pid}，{ok.isoformat()} 跑通一拍', file=out)
            return 0
        waited = (clock() - moment).total_seconds()
        if waited >= timeout:
            print(f'车间：重启后 {timeout:g} 秒内车间没起来（心跳进程 {pid}，最近成功 {ok.isoformat() if ok else "无"}）。'
                  f'这种重启没有自动回退，要人处理：看 {Path(state_dir) / (LABEL + ".stderr.log")}', file=err)
            return 1
        pause(min(interval, timeout - waited))
