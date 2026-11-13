"""Best-effort Claude folder trust, using the CLI's adjacent mkdir lock protocol."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from .storage import write

STALE_SECONDS = 10
RETRY_DELAYS = (.05, .1, .2, .25)


def config_path(env):
    home = Path(env.get('HOME') or Path.home())
    config_dir = Path(env['CLAUDE_CONFIG_DIR']) if env.get('CLAUDE_CONFIG_DIR') else home/'.claude'
    legacy = config_dir/'.config.json'
    return legacy if legacy.exists() else (config_dir if env.get('CLAUDE_CONFIG_DIR') else home)/'.claude.json'


def read_config(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or ('projects' in value and not isinstance(value['projects'], dict)):
        raise ValueError('配置或 projects 不是对象')
    return value


def trusted(config, keys):
    projects = config.get('projects', {})
    return any(isinstance(projects.get(key), dict) and projects[key].get('hasTrustDialogAccepted') is True
               for key in keys)


def same_lock(path, identity):
    current = path.stat()
    return (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)


@contextmanager
def config_lock(path):
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            path.mkdir(mode=0o700)
            break
        except FileExistsError:
            try:
                before = path.stat()
                if time.time() - before.st_mtime > STALE_SECONDS:
                    current = path.stat()
                    if same_lock(path, before) and current.st_mtime_ns == before.st_mtime_ns:
                        path.rmdir()  # Never delete contents of an unexpected lock directory.
                        path.mkdir(mode=0o700)
                        break
            except (FileNotFoundError, FileExistsError):
                pass
            if attempt == len(RETRY_DELAYS):
                raise TimeoutError('配置锁被占用')
            time.sleep(RETRY_DELAYS[attempt])
    identity = path.stat()
    try:
        def check():
            if not same_lock(path, identity) or time.time() - path.stat().st_mtime > STALE_SECONDS:
                raise TimeoutError('配置锁已失效')
        yield check
    finally:
        try:
            if same_lock(path, identity):
                path.rmdir()
        except FileNotFoundError:
            pass


def grant(workdir, env):
    original = Path(os.path.abspath(workdir))
    keys = list(dict.fromkeys((str(original), str(original.resolve()))))
    config = config_path(env)
    target = config.resolve(strict=True)  # Preserve a config symlink rather than replacing it.
    if trusted(read_config(target), keys):
        return 'unchanged'
    with config_lock(Path(str(config) + '.lock')) as check_lock:
        if config.resolve(strict=True) != target:
            raise OSError('配置路径已改变')
        data = read_config(target)  # Re-read under lock so concurrent edits are retained.
        if trusted(data, keys):
            return 'unchanged'
        projects = data.setdefault('projects', {})
        for key in keys:
            entry = projects.get(key)
            if not isinstance(entry, dict):
                entry = projects[key] = {}
            entry['hasTrustDialogAccepted'] = True
        fd, temporary = tempfile.mkstemp(prefix='.workshop-trust-', dir=target.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), stat.S_IMODE(target.stat().st_mode))
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            check_lock()
            if config.resolve(strict=True) != target:
                raise OSError('配置路径已改变')
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return 'granted'


def prepare(workdir, env, directory):
    try:
        grant(workdir, env)
    except Exception as exc:
        # Never make launching Claude depend on this optional preference write.
        record = {'message': '没能预先信任目录', 'reason': type(exc).__name__}
        try:
            write(directory/'trust-warning.json', record)
        except OSError:
            import sys
            print(record['message'], file=sys.stderr)
