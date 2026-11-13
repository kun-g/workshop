"""Credential-stripped review copies, not a security boundary for the OS user."""
import os
from pathlib import Path
import shutil
import subprocess

INVALID_ORIGIN = 'https://invalid.invalid/workshop-review'


def environment(source=None):
    source = os.environ if source is None else source
    prefixes = ('AWS_', 'AZURE_', 'GOOGLE_', 'GCLOUD_', 'GIT_', 'GH_', 'GITHUB_', 'SSH_', 'KUBE')
    result = {k: v for k, v in source.items() if not k.startswith(prefixes)}
    result.update(GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_NOSYSTEM='1',
                  GIT_TERMINAL_PROMPT='0', GIT_CONFIG_COUNT='1',
                  GIT_CONFIG_KEY_0='credential.helper', GIT_CONFIG_VALUE_0='',
                  GH_TOKEN='disabled-in-workshop-review', GITHUB_TOKEN='disabled-in-workshop-review',
                  KUBECONFIG='/dev/null', CLOUDSDK_CONFIG='/dev/null')
    # Prevent tools from falling back to cloud credential files in the real HOME.
    result['AWS_CONFIG_FILE'] = '/dev/null'
    result['AWS_SHARED_CREDENTIALS_FILE'] = '/dev/null'
    result.pop('WORKSHOP_RULES', None)
    return result


def copy_repository(git: str, source: Path, dest: Path):
    env = environment()
    def run(*args):
        subprocess.run([git, *args], env=env, check=True, capture_output=True, text=True)
    if dest.exists():
        raise RuntimeError('审查副本已存在但未完成登记，需核对: ' + str(dest))
    # Independent objects/config; no alternates, worktree links, hooks or credentials.
    run('-c', 'core.hooksPath=/dev/null', 'clone', '--no-hardlinks', '--no-local', str(source), str(dest))
    run('-C', str(dest), 'remote', 'set-url', 'origin', INVALID_ORIGIN)
    run('-C', str(dest), 'config', 'credential.helper', '')
    run('-C', str(dest), 'config', 'core.hooksPath', '/dev/null')
    # Include current tracked edits and nonignored files, without copying .git/config.
    names = subprocess.check_output([git, '-C', str(source), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], env=env).split(b'\0')
    for raw in names:
        if not raw:
            continue
        name = os.fsdecode(raw)
        src, out = source / name, dest / name
        if src.is_symlink():
            raise RuntimeError('审查副本不跟随符号链接: ' + name)
        if src.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, out)
        elif out.is_file():
            out.unlink()
