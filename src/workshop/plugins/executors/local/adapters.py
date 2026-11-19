"""Profile-scoped journals and transport; CLI behavior belongs to the driver."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import uuid

from workshop.core.models import Release, Started
from workshop.core.ports import Retryable, TargetBlocked, Unavailable
from workshop.github_identity import configuration as identity_configuration
from workshop.github_identity import run as identity_run, scoped
from .storage import alive, identity, lock, read, write
from .runner import RECEIPT_REJECTED, VENDOR_SESSION
from .processes import stop_round
from .briefs import prepare as prepare_brief
from .drivers import for_spec
from .isolation import environment
from .retention import git_roots, maybe_sweep, nested_worktrees


def recorded_model(spec_path):
    """会话上一轮启动时要求的模型，从那一轮的 argv 读（旧轮的 spec 也有）；不写为 None。"""
    try:
        argv = read(spec_path)['argv']
    except (OSError, ValueError, KeyError):
        return None
    return argv[argv.index('--model') + 1] if '--model' in argv[:-1] else None


def executable(name):
    found = shutil.which(name, path=os.environ.get('PATH', '') + ':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin')
    return str(Path(found).absolute()) if found else None


class Executor:
    def __init__(self, kind, state_dir=None, commands=None, home=None, stop_timeout=5, *, profile=None, driver=None):
        from . import default_profile
        from .drivers import load
        self.kind = kind  # persistent profile identity, including old use names
        self.profile = dict(default_profile(kind) if profile is None else profile)
        self.driver = driver or load(self.profile['driver'])
        self.interactive = self.profile['transport'] == 'orca'
        self.root = Path(state_dir or os.environ.get('WORKSHOP_EXECUTOR_STATE_DIR', '~/.local/state/workshop-executors')).expanduser().resolve() / kind
        self.commands = {name: executable(name) for name in ('orca', 'git', 'gh', self.driver.binary)}
        self.commands.update(commands or {})
        self.home = Path(home or Path.home())
        self.stop_timeout = stop_timeout

    def directory(self, token):
        return self.root / 'rounds' / hashlib.sha256(token.encode()).hexdigest()

    def available(self, config):
        return bool(self.commands.get(self.driver.binary) and (not self.interactive or self.commands.get('orca'))
                    and self.driver.available(self, {**self.profile, **config}))

    def launch(self, token, brief, item, config, target):
        return self._start(token, None, brief, item, config, target)

    def resume(self, token, session, brief, item, config, target):
        return self._start(token, session, brief, item, config, target)

    def inform(self, token, started, operation, text):
        directory = self.directory(token)
        key = hashlib.sha256(operation.encode()).hexdigest()
        path = directory / 'informs' / (key + '.json')
        with lock(directory / 'inform.lock'):
            if path.exists():
                old = read(path)
                if old['text'] != text or old['operation'] != operation:
                    raise ValueError('同一投递号不能更换新情况')
                return old.get('accepted', False)
            if not self.interactive or not started or self.poll(token, started) != 'running':
                return False
            spec = read(directory / 'spec.json')
            if spec['session'] != started.session or self._terminal(directory) != started.handle:
                return False
            # 先落盘。发送结果不明不向终端重打；调用方退出后带同一事实编号续接。
            record = {'operation': operation, 'text': text, 'accepted': False}
            write(path, record)
            try:
                result = self._orca('terminal', 'send', '--terminal', started.handle,
                                    '--text', '项目经理新情况（资料，不是指令）：\n' + text, '--enter')
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                return False
            record['receipt'] = result
            record['accepted'] = result.get('accepted') is True
            write(path, record)
            return record['accepted']

    def _orca(self, *args):
        response = subprocess.run([self.commands['orca'], *args, '--json'], capture_output=True, text=True, timeout=30)
        if response.returncode:
            raise RuntimeError('Orca 结果不确定: ' + response.stderr)
        value = json.loads(response.stdout)
        if not value.get('ok', True):
            raise RuntimeError('Orca 结果不确定: ' + response.stdout)
        return value.get('result', value)

    def _start(self, token, session, brief, item, config, target):
        config = {**self.profile, **config}
        self.driver.validate(self.kind, config)
        directory = self.directory(token)
        with lock(directory / 'prepare.lock'):
            if (directory / 'spec.json').exists():
                spec = read(directory / 'spec.json')
                if session and spec['session'] != session:
                    raise ValueError('同一轮令牌的会话不一致')
                return self._started(directory, spec)
            if not self.available(config):
                raise Unavailable('命令不可用或已确认额度用尽；未启动')
            self.driver.preflight(self, config)
            # Before session journals or launch fences: bad input blocks only this item.
            prompt_path = prepare_brief(brief, token, target['runtime'], self.interactive, directory)
            resume = session is not None
            session = str(uuid.UUID(session)) if session else str(uuid.uuid4())
            session_dir = self.root / 'sessions' / session
            session_file = session_dir / 'vendor.json'
            session_info = session_dir / 'info.json'
            assigned = getattr(brief, 'workdir', None)
            readonly = bool(getattr(brief, 'readonly', False)) or not self.interactive
            if assigned and not Path(assigned).is_absolute():
                raise ValueError('Brief.workdir 必须是绝对路径')
            fresh = False
            if resume:
                previous = read(session_info)
                if not (self.root / 'rounds' / previous['round'] / 'stopped.json').exists():
                    raise RuntimeError('原轮尚未确认停止，不接回会话')
                workdir = assigned or item.get('workdir') or previous['workdir']
                # 上一轮在同一轮里改过一次仍交不出合格回执：不再接回这个会话，换新会话从说明重来。
                fresh = not self.interactive and (self.root / 'rounds' / previous['round'] / RECEIPT_REJECTED).exists()
                previous_dir = self.root / 'rounds' / previous['round']
                previous_spec = read(previous_dir / 'spec.json') if (previous_dir / 'spec.json').exists() else {}
                if previous_spec:
                    for_spec(previous_spec).recover_session(self, previous_dir, previous_spec)
                else:
                    fresh = True
                if self.driver.allocated_session:
                    vendor = read(session_file) if session_file.exists() else {}
                    fresh |= not vendor.get('vendor_id') or vendor.get('model') != self.model(config)
                else:
                    fresh |= previous_spec.get('model', recorded_model(previous_dir / 'spec.json')) != self.model(config)
                fingerprint = previous_spec.get('profile_fingerprint')
                if fingerprint and fingerprint != self.fingerprint(config):
                    fresh = True
            else:
                workdir = assigned or item.get('workdir')
            if (not workdir or not assigned and not Path(workdir).exists()) and self.interactive and not readonly:
                # 收尾时收回过的工作区，返工时按同一个名字重新开。
                workdir = self._create_worktree(directory, item, target)
            if not workdir:
                raise ValueError('执行者没有工作目录；流程应提供 Brief.workdir')
            trust_workdir = os.path.abspath(workdir)
            workdir = str(Path(workdir).resolve())
            runtime = target['runtime']
            # Persist only trusted per-round additions. Interactive runner inherits its
            # own login shell environment; headless runner inherits the launch environment.
            env = {**runtime['env'], **self.driver.environment(config)}
            if readonly:
                env = environment(directory / 'gh-config', env)
            env.update(WORKSHOP_ITEM=item['id'], WORKSHOP_EXECUTOR_ROUND=str(directory))
            if self.driver.allocated_session:
                vendor_id = read(session_file)['vendor_id'] if resume and not fresh else None
                if vendor_id is None and session_file.exists():
                    session_file.unlink()
            else:
                vendor_id = (str(uuid.uuid4()) if fresh else read(session_file).get('vendor_id', session)
                             if resume and session_file.exists() else session)
                if fresh:
                    write(session_file, {'vendor_id': vendor_id, 'model': self.model(config)})
            argv = self._argv(config, workdir, vendor_id, resume and not fresh, directory, readonly, trust_workdir)
            retry_argv = None
            fresh_argv = None
            if not self.interactive:
                # 回执被拒时，包装进程在同一轮接回厂商会话，把原因发回去重交一次。
                retry_argv = self._argv(config, workdir, VENDOR_SESSION, True, directory, readonly)
                if self.driver.stdin_arg:
                    retry_argv += ['-']
                if resume and not fresh:
                    new_id = None if self.driver.allocated_session else str(uuid.uuid4())
                    fresh_argv = self._argv(config, workdir, new_id, False, directory, readonly, trust_workdir)
                    if self.driver.stdin_arg:
                        fresh_argv += ['-']
            spec = {'kind': self.kind, 'driver': config['driver'], 'profile_fingerprint': self.fingerprint(config), 'token': token, 'session': session, 'session_file': str(session_file),
                    'github_identity': identity_configuration(target), 'repo': target.get('repo'),
                    'workdir': workdir, 'trust_workdir': trust_workdir,
                    'public_workdir': (item.get('workdir') if readonly else workdir),
                    'readonly': readonly, 'gh_config': str(directory / 'gh-config'),
                    'env': env, 'argv': argv, 'retry_argv': retry_argv, 'fresh_argv': fresh_argv, 'vendor_id': vendor_id,
                    'fresh_session': fresh, 'prompt': str(prompt_path),
                    'receipt_command': runtime['command'], 'interactive': self.interactive,
                    'billing': config.get('billing'), 'model': self.model(config), 'item': {k: item[k] for k in ('id', 'target', 'object_key')}}
            write(session_info, {'workdir': workdir, 'round': directory.name})
            write(directory / 'spec.json', spec)
            command = [sys.executable, str(Path(__file__).with_name('runner.py')), str(directory)]
            # spec is the durable launch fence. Never repeat creation on an uncertain return.
            if self.interactive:
                result = self._orca('terminal', 'create', '--worktree', 'path:' + workdir,
                    '--title', '车间 ' + item['id'], '--command', shlex.join(command))
                handle = result.get('handle') or result.get('terminal', {}).get('handle')
                if not handle:
                    raise RuntimeError('Orca 未返回终端 handle')
                write(directory / 'terminal.json', {'handle': handle})
            else:
                with (directory / 'supervisor.log').open('a') as log:
                    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                birth = identity(proc.pid)
                write(directory / 'supervisor.json', {'pid': proc.pid, 'birth': birth,
                                                     'group': {'pid': proc.pid, 'birth': birth}})
            return self._started(directory, spec)

    def model(self, config):
        return self.driver.model({**self.profile, **config})

    @staticmethod
    def fingerprint(config):
        launch = {k: v for k, v in config.items() if k not in ('quota', 'billing', 'first_when_writer')}
        return hashlib.sha256(json.dumps(launch, sort_keys=True).encode()).hexdigest()

    def _argv(self, config, workdir, session, resume, directory, readonly, trust_workdir=None):
        # readonly controls credential stripping, never CLI permissions.
        return self.driver.argv(self, {**self.profile, **config}, workdir, session, resume, directory, trust_workdir)

    def _started(self, directory, spec):
        if spec['interactive']:
            handle = self._terminal(directory)
            if not handle:
                raise RuntimeError('启动尚未返回终端且包装进程未登记；保留占位')
        else:
            if not any((directory / name).exists() for name in ('claimed.json', 'supervisor.json', 'exited.json')):
                raise RuntimeError('尚无已启动进程的记录；保留占位')
            handle = str(directory)
        return Started(spec['session'], handle, spec['public_workdir'])

    def _terminal(self, directory):
        if (directory / 'terminal.json').exists():
            return read(directory / 'terminal.json')['handle']
        if (directory / 'claimed.json').exists():
            return read(directory / 'claimed.json').get('terminal')
        return None

    def slug(self, item):
        return 'ws-' + hashlib.sha256(item['id'].encode()).hexdigest()[:16]

    @scoped
    def _create_worktree(self, directory, item, target):
        slug = self.slug(item)
        intent = directory / 'worktree-intent.json'
        if intent.exists():
            raise RuntimeError('工作区创建结果不确定，需要对账: ' + str(intent))
        checkout = str(Path(target['checkout']).expanduser().resolve())
        # Orca 会快进命名基线所在的主检出；固定提交号只用于新分支。
        self._git('-C', checkout, 'fetch', 'origin', '+refs/heads/main:refs/remotes/origin/main')
        result = self._git('-C', checkout, 'rev-parse', '--verify', 'refs/remotes/origin/main^{commit}')
        commit = result.stdout.strip()
        if not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise Retryable('git 未返回有效的 origin/main 提交号: ' + (result.stdout + result.stderr).strip()[-300:])
        # git 失败还没有创建工作区，可以重试；调用 Orca 前才写占位。
        write(intent, {'slug': slug, 'commit': commit})
        result = self._orca('worktree', 'create', '--repo', 'path:' + checkout,
            '--name', slug, '--base-branch', commit, '--branch', 'ws/' + slug, '--no-parent', '--setup', 'skip')
        return result['worktree']['path']

    def _git(self, *args, check=True):
        try:
            result = identity_run([self.commands['git'], *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Retryable('git 结果不确定: ' + ' '.join(args[:3])) from exc
        if check and result.returncode:
            raise Retryable('git 失败: ' + ' '.join(args[:3]) + ': ' + result.stderr.strip()[-300:])
        return result

    def _changes(self, path, exclude=()):
        # 嵌套工作区在外层看来是一堆未跟踪文件；它们单独查，别算成外层的改动。
        result = self._git('-C', str(path), 'status', '--porcelain', '-z', '-uall', '--no-renames', check=False)
        if result.returncode:
            raise Retryable('git 查未提交改动失败：' + result.stderr.strip()[-300:])
        bases = [Path(item).resolve() for item in exclude]
        changes = []
        for record in filter(None, result.stdout.split('\0')):
            full = Path(path) / record[3:]
            if any(full.resolve() == base or full.resolve().is_relative_to(base) for base in bases):
                continue
            changes.append(record)
        return changes

    def _prs(self, repo, branch):
        gh = self.commands.get('gh')
        if not gh:
            raise Retryable('找不到 gh，无法确认 PR 是否已合并')
        try:
            result = identity_run([gh, 'pr', 'list', '--repo', repo, '--head', branch,
                                   '--state', 'all', '--limit', '20',
                                   '--json', 'number,state,mergedAt,url'],
                                  capture_output=True, text=True, timeout=60)
        except TargetBlocked as exc:
            raise Retryable(str(exc)) from exc
        if result.returncode:
            raise Retryable('查询 PR 状态失败：' + (result.stderr or result.stdout).strip()[-300:])
        try:
            value = json.loads(result.stdout)
        except ValueError:
            raise Retryable('gh 返回的 PR 状态不是 JSON')
        if not isinstance(value, list):
            raise Retryable('gh 返回的 PR 状态不是列表')
        return value

    def _remove_worktree(self, path):
        try:
            self._orca('worktree', 'rm', '--worktree', 'path:' + str(path))
        except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise Retryable('Orca 删除工作区结果不确定') from exc
        if Path(path).exists():
            raise Retryable('Orca 返回成功但工作区还在')

    @scoped
    def release(self, item, target, dry_run=False):
        """活收尾后收回本执行者为它开的 Orca worktree 和本地分支。

        只认 _start 按活 id 开的那个目录（Orca 可能给同名目录加 -2 后缀）。分支上的提交
        已随 PR 压缩合并进 main，所以不能看分支是否已在远端：要 PR 状态为已合并，且外层和
        嵌套工作区都没有未提交改动。有 PR 开着、没合并、或查不到 PR 时留着并说明原因。
        dry_run 只给判断，不删。
        """
        if not self.interactive or not item.get('workdir') or not target.get('checkout'):
            return None
        workdir = Path(item['workdir'])
        if not re.fullmatch(re.escape(self.slug(item)) + r'(-\d+)?', workdir.name):
            return None
        checkout = str(Path(target['checkout']).expanduser().resolve())
        resolved = str(workdir.resolve()) if workdir.exists() else str(workdir)
        entries, entry = self._git('-C', checkout, 'worktree', 'list', '--porcelain').stdout.split('\n\n'), None
        for block in entries:
            fields = dict(line.split(' ', 1) if ' ' in line else (line, '') for line in block.splitlines())
            if fields.get('worktree') and str(Path(fields['worktree']).resolve()) == resolved:
                entry = fields
        if workdir.exists() and entry is None:
            return None  # 目录在，但不是这个主检出上的 worktree：不是自己开的，不碰。
        branch = (entry or {}).get('branch', 'refs/heads/ws/' + workdir.name)
        short = branch.removeprefix('refs/heads/')
        # 探针工作区常开在活的工作区里面；里面脏了也不能删外层。克隆和链接工作区都查。
        nested = nested_worktrees(workdir) if workdir.exists() else []
        roots = git_roots(workdir) if workdir.exists() else []
        if entry is not None and workdir.exists():
            changes = self._changes(workdir, exclude=roots)
            if changes:
                return Release(False, f'{workdir} 有 {len(changes)} 处未提交改动，没删')
        for path in roots:
            if not Path(path).exists():
                continue
            try:
                changes = self._changes(path)
            except Retryable as exc:
                return Release(False, f'{path} 查不了未提交改动（{exc}），没删')
            if changes:
                return Release(False, f'{path} 有 {len(changes)} 处未提交改动，没删')
        exists = self._git('-C', checkout, 'rev-parse', '--verify', '-q', branch, check=False).returncode == 0
        if entry is None and not exists:
            return Release(True)  # 工作区和分支都不在了，没有可收的。
        repo = target.get('repo')
        if not repo:
            return Release(False, f'{workdir} 的交付对象没配置仓库，无法确认 PR 已合并，没删')
        if not self.commands.get('gh'):
            return Release(False, f'{workdir} 找不到 gh，无法确认 PR 已合并，没删')
        prs = self._prs(repo, short)
        opened = [pr for pr in prs if pr.get('state') == 'OPEN']
        if opened:
            return Release(False, f'{workdir} 的 PR #{opened[0]["number"]} 还开着，没删')
        merged = [pr for pr in prs if pr.get('state') == 'MERGED']
        if not merged:
            # 另一条安全的合并证据：分支已经在 origin/main 里（例如工作区只是从 main
            # 快进出来的，没有自己的 PR）。压缩合并的分支不会命中这一条，所以仍以 PR 为主。
            ancestor = self._git('-C', checkout, 'merge-base', '--is-ancestor', branch,
                                 'refs/remotes/origin/main', check=False).returncode == 0
            if not ancestor:
                closed = [pr['number'] for pr in prs if pr.get('state') == 'CLOSED']
                why = f'PR #{closed[0]} 已关闭但没合并' if closed else '找不到已合并的 PR'
                return Release(False, f'{workdir} 的 {why}，没删')
            evidence = '分支已并入 main'
        else:
            evidence = f'PR #{merged[0]["number"]} 已合并'
        if dry_run:
            action = f'删 {workdir if entry is not None else "（目录已不在）"}'
            if exists:
                action += f'，删分支 {short}'
            return Release(True, action + f'（{evidence}）')
        if entry is not None:
            # 先删里面的探针工作区，外层才删得干净。
            for path in nested:
                self._remove_worktree(path)
            self._remove_worktree(resolved)
        if exists:
            self._git('-C', checkout, 'branch', '-D', short)
        maybe_sweep(self.root)
        return Release(True)

    def poll(self, token, started):
        directory = self.directory(token)
        if (directory / 'exited.json').exists() or (directory / 'stopped.json').exists():
            return 'exited'
        paths = [directory / name for name in ('claimed.json', 'supervisor.json', 'child.json')]
        records = [read(p) for p in paths if p.exists()]
        if any(alive(r) for r in records):
            return 'running'
        # Child may have spawned in the tiny window before child.json. No exit proof then.
        if (directory / 'child.json').exists():
            return 'exited'
        raise Retryable('启动结果或进程状态无法确认；保留占位')

    def stop(self, token, started):
        directory = self.directory(token)
        spec = read(directory / 'spec.json') if (directory / 'spec.json').exists() else {}
        if spec:
            for_spec(spec).recover_session(self, directory, spec)
        def close_terminal():
            handle = self._terminal(directory)
            if spec.get('interactive', self.interactive) and handle:
                self._orca('terminal', 'close', '--terminal', handle)
        stop_round(directory, close_terminal, self.stop_timeout)
        # 停稳后顺手清一次过期的轮次；正在停的这一轮由 protect 留住。
        maybe_sweep(self.root, protect=(directory.name,))

    def usage(self, token, started, since):
        directory = self.directory(token)
        if not (directory / 'spec.json').exists():
            return None
        spec = read(directory / 'spec.json')
        return for_spec(spec).usage(self, directory, spec, since)
