"""Executor-owned journal; never writes the core database."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read(path: Path):
    return json.loads(path.read_text())


@contextmanager
def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def identity(pid: int):
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=,stat='], capture_output=True, text=True, check=False)
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode:
        raise RuntimeError('无法核实进程状态: ' + result.stderr)
    line = result.stdout.strip()
    if line.split()[-1].startswith('Z'):
        return None
    return ' '.join(line.split()[:-1])


def alive(record):
    return bool(record and record.get('birth') and identity(record['pid']) == record['birth'])


def me():
    return {'pid': os.getpid(), 'birth': identity(os.getpid())}
