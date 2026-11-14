"""Stop barrier shared with delayed launchers; TERM then KILL for the whole owned group."""
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import time

from workshop.core.ports import Retryable
from .storage import alive, identity, lock, read, write


def members(group):
    leader = identity(group['pid'])
    if leader and group.get('birth') and leader != group['birth']:
        # This identity has exited; the reused number belongs to an unrelated group.
        return []
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,pgid=,stat='], capture_output=True, text=True, check=True)
    return [int(parts[0]) for line in result.stdout.splitlines() if len(parts := line.split()) == 3
            and int(parts[1]) == group['pid'] and not parts[2].startswith('Z')]


def groups(directory):
    result = {}
    for name in ('claimed.json', 'supervisor.json', 'child.json'):
        path = directory / name
        if not path.exists():
            continue
        record = read(path)
        group = record.get('group')
        if group is None and alive(record):  # Records written before process groups were journaled.
            try:
                pgid = os.getpgid(record['pid'])
            except ProcessLookupError:
                continue
            group = {'pid': pgid, 'birth': identity(pgid)}
        if group:
            if group['pid'] == os.getpgrp():
                raise RuntimeError('拒绝停止调用者所在的进程组')
            result[group['pid']] = group
    return result


def stop_round(directory: Path, close_terminal, timeout):
    # Serializes with adapter preparation; run.lock serializes with actual child creation.
    with lock(directory / 'prepare.lock'):
        if (directory / 'stopped.json').exists():
            return
        write(directory / 'stopping.json', {'requested': True})
        try:
            close_terminal()
        except Exception as exc:
            # A stale UI handle is not a process identity. Group reconciliation still works.
            write(directory / 'terminal-close-error.json', {'error': type(exc).__name__})
        known = {}
        with (directory / 'run.lock').open('a') as guard:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                deadline = time.monotonic() + timeout
                signalled = set()
                while True:
                    known.update(groups(directory))
                    live = {pgid: group for pgid, group in known.items() if members(group)}
                    for pgid in live.keys() - signalled:
                        try:
                            os.killpg(pgid, sig)
                        except ProcessLookupError:
                            pass
                        except PermissionError as exc:
                            # macOS may report EPERM for a group that just exited.
                            # Only an empty group can proceed to stop confirmation.
                            if members(known[pgid]):
                                raise Retryable('无权停止仍存活的进程组；保留占位') from exc
                        signalled.add(pgid)
                    try:
                        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                    except BlockingIOError:
                        locked = False
                    if locked:
                        try:
                            # A wrapper delayed until after this returns sees the stopped fence.
                            records = [read(directory / name) for name in ('child.json', 'claimed.json', 'supervisor.json')
                                       if (directory / name).exists()]
                            if not any(alive(record) for record in records) and not any(members(g) for g in known.values()):
                                write(directory / 'stopped.json', {'confirmed': True})
                                return
                        finally:
                            fcntl.flock(guard, fcntl.LOCK_UN)
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(.03)
    raise Retryable('TERM/KILL 后仍不能确认进程组退出；保留占位')
