"""Keep provider session history, never credentials, when disposing a round HOME."""
import hashlib
from pathlib import Path
import shutil

HISTORY_PATHS = {
    'codex': ('.codex/sessions',),
    'cursor-grok': ('.cursor/chats',),
    'claude': ('.claude/projects',),
    'orca-claude': ('.claude/projects',),
}


def copy_history(source: Path, destination: Path, kind: str):
    for relative in HISTORY_PATHS[kind]:
        path = source / relative
        if not path.exists():
            continue
        if path.is_symlink() or any(p.is_symlink() for p in path.rglob('*')):
            raise RuntimeError('会话历史里有符号链接，不跨目录归档')
        out = destination / relative
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copytree(path, out, dirs_exist_ok=True)


def archive_and_remove_home(directory: Path, kind: str):
    home = directory / 'home'
    if not home.exists():
        return
    if home.is_symlink():
        raise RuntimeError('临时 HOME 不是私有目录')
    # Credentials must be removed even if a history file cannot be archived.
    for pattern in ('.codex/auth.json*', '.cursor/auth.json*', '.claude/.credentials.json*'):
        for credential in home.glob(pattern):
            if credential.is_file() or credential.is_symlink():
                credential.unlink()
    copy_history(home, directory / 'records', kind)
    shutil.rmtree(home)


def restore_history(previous: Path, home: Path, kind: str, old_cwd: str, new_cwd: str):
    copy_history(previous / 'records', home, kind)
    if kind == 'cursor-grok' and old_cwd != new_cwd:
        old_key = hashlib.md5(old_cwd.encode()).hexdigest()
        new_key = hashlib.md5(new_cwd.encode()).hexdigest()
        source = home / '.cursor/chats' / old_key
        if source.exists():
            shutil.copytree(source, home / '.cursor/chats' / new_key, dirs_exist_ok=True)
