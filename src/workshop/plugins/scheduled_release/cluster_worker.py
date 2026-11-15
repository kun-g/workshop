"""固定批次的独立程序；持锁存活，不因守护进程重启重新申请授权。"""
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from workshop.github_identity import configuration, environment
from .io import grant_record, read, write


class Failure(Exception):
    pass


class Runner:
    def __init__(self, spec, directory):
        self.spec, self.directory = spec, Path(directory)
        self.target = spec['target']
        self.repo, self.sha = self.target['repo'], spec['to']
        self.checkout = self.directory.parent / 'checkout'
        self.env = environment(configuration(self.target), self.repo)
        self.runs = []

    def command(self, argv, *, cwd=None, timeout=60, check=True):
        """先记意图，逐命令保存输出；超时收回整组子进程，不遗留发卡者。"""
        index = len(self.runs)
        entry = {'argv': list(map(str, argv)), 'stdout_path': str(self.directory / f'{index}.stdout'),
                 'stderr_path': str(self.directory / f'{index}.stderr'), 'exit_code': None}
        self.runs.append(entry)
        write(self.directory / 'runs.json', self.runs)
        with open(entry['stdout_path'], 'w') as out, open(entry['stderr_path'], 'w') as err:
            try:
                child = subprocess.Popen(entry['argv'], cwd=cwd, env=self.env, stdin=subprocess.DEVNULL,
                                         stdout=out, stderr=err, start_new_session=True)
                try:
                    entry['exit_code'] = child.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                    entry['exit_code'] = 124
                    err.write('\n命令超时，已停止命令进程组\n')
            except OSError as exc:
                entry['exit_code'] = 127
                err.write(str(exc))
        entry['stdout'] = Path(entry['stdout_path']).read_text(errors='replace')
        entry['stderr'] = Path(entry['stderr_path']).read_text(errors='replace')
        write(self.directory / 'runs.json', self.runs)
        if check and entry['exit_code']:
            raise Failure(f"命令退出 {entry['exit_code']}：{entry['argv']}；证据 {self.directory}")
        return entry

    def git(self, *args, check=True):
        return self.command(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(self.checkout), *args], check=check)

    def api(self, path):
        return json.loads(self.command(['gh', 'api', path])['stdout'])

    def build(self):
        self.command(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '--no-checkout',
                      f'https://github.com/{self.repo}.git', str(self.checkout)], timeout=180)
        self.git('checkout', '--detach', self.sha)
        self.git('merge-base', '--is-ancestor', self.sha, 'origin/main')
        self.git('merge-base', '--is-ancestor', self.spec['from'], self.sha)
        # 优先认回既有的固定提交 tag；绝不对同一提交盲目再打 tag。
        refs = self.git('ls-remote', '--tags', 'origin', 'refs/tags/v*')['stdout'].splitlines()
        tags = sorted({line.split()[1].removeprefix('refs/tags/').removesuffix('^{}')
                       for line in refs if line.split()[0] == self.sha})
        if tags:
            tag = tags[0]
        else:
            self.command([*self.target['tag_command'], self.sha], cwd=self.checkout, timeout=300)
            refs = self.git('ls-remote', '--tags', 'origin', 'refs/tags/v*')['stdout'].splitlines()
            tags = sorted({line.split()[1].removeprefix('refs/tags/').removesuffix('^{}')
                           for line in refs if line.split()[0] == self.sha})
            if not tags:
                raise Failure('打 tag 后未确认远端引用')
            tag = tags[0]
        deadline = time.monotonic() + self.target.get('scheduled_ci_timeout', 3600)
        while True:
            from urllib.parse import quote
            data = self.api(f'repos/{self.repo}/actions/workflows/ci.yml/runs?branch={quote(tag, safe="")}&event=push&per_page=100')
            matches = [r for r in data['workflow_runs'] if r['head_sha'] == self.sha
                       and r['head_branch'] == tag and r['event'] == 'push']
            if matches:
                selected = max(matches, key=lambda r: (r['id'], r.get('run_attempt', 1)))
                if selected['status'] == 'completed':
                    if selected['conclusion'] != 'success':
                        raise Failure(f"tag CI {selected['id']}：{selected['conclusion']}")
                    return {'status': 'ok', 'tag': tag, 'run_id': selected['id'],
                            'attempt': selected.get('run_attempt', 1), 'checkout': str(self.checkout)}
            if time.monotonic() >= deadline:
                raise Failure('等待固定 tag CI 超时，未推 production、未申请授权')
            time.sleep(30)

    def deploy(self):
        built = self.spec['built']
        # 独立副本固定到已通过 CI 的提交，不能因等待期间 main 变化而换版。
        if self.git('rev-parse', 'HEAD')['stdout'].strip() != self.sha:
            raise Failure('发版副本提交发生变化')
        for step in self.target.get('prepare', []):
            cwd = (self.checkout / step.get('cwd', '.')).resolve()
            if not cwd.is_relative_to(self.checkout.resolve()):
                raise Failure('准备命令目录越界')
            self.command(step['argv'], cwd=cwd, timeout=300)
        published_dir = self.directory / 'published'
        published_dir.mkdir()
        self.command(['gh', 'run', 'download', str(built['run_id']), '--repo', self.repo,
                      '-n', f'grant-console-{self.sha}-published', '-D', str(published_dir)], timeout=180)
        published = read(published_dir / 'publish.json')
        release = published.get('release', {})
        url = f"https://github.com/{self.repo}/actions/runs/{built['run_id']}/attempts/{built['attempt']}"
        build_url = re.fullmatch(re.escape(f"https://github.com/{self.repo}/actions/runs/{built['run_id']}/attempts/")
                                 + r'([1-9][0-9]*)', release.get('build_run_url', ''))
        if (release.get('source_commit') != self.sha or release.get('source_repository') != self.repo
                or not build_url or int(build_url[1]) > built['attempt'] or published.get('publish_run_url') != url
                or release.get('image_digest') != published.get('destination_digest')
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', published.get('destination_digest', ''))):
            raise Failure('发布记录与固定提交、CI run/attempt 或 digest 不匹配')
        now = datetime.now(timezone.utc)
        approval = {'schema_version': 1, 'source_commit': self.sha,
                    'image_digest': published['destination_digest'], 'destination': published['destination'],
                    'approved_by': 'Kun', 'approved_at': now.isoformat(),
                    'expires_at': (now + timedelta(hours=2)).isoformat(),
                    'approval_reference': 'policy:cluster-card:2026-11-13',
                    'scope': 'ecr-import-and-manual-argo-sync'}
        write(self.directory / 'approval.json', approval)
        output = (self.directory / 'rendered').resolve()
        output.mkdir()
        python = str(self.checkout / 'services/devops-console/.venv/bin/python')
        self.command([python, 'scripts/release.py', '--published', str(published_dir / 'publish.json'),
                      '--approval', str(self.directory / 'approval.json'), '--source-repo', str(self.checkout),
                      '--output-root', str(output)], cwd=self.checkout)
        rendered = output / f"{self.sha}-{published['destination_digest'].split(':')[1]}"
        if self.spec.get('initial'):
            previous = self.git('ls-remote', 'origin', 'refs/heads/production')['stdout'].split()
            if not previous or not re.fullmatch('[0-9a-f]{40}', previous[0]):
                raise Failure('首次切换无法确认 production，未推新版本、未发卡')
            self.verify(previous[0], label='首次切换前旧版本')
        pushed = self.command(['scripts/push-production.sh', str(rendered)], cwd=self.checkout, timeout=180)
        match = re.search(r'production (?:-> |already holds this release: )([0-9a-f]{40})', pushed['stdout'])
        if not match:
            raise Failure('推 production 的结果未确认')
        production = match[1]
        write(self.directory / 'production.json', {'revision': production, 'source_commit': self.sha})
        synced = self.command(['scripts/sync-production.sh', production, '--require-exec', '--session-id', self.spec['item'],
                               '--release-dir', str(self.directory), '--reason', f"车间整点批次 {self.spec['item']}"],
                              cwd=self.checkout, timeout=3600, check=False)
        grant = self.grant()
        if synced['exit_code']:
            # 3 也可能只是本地等待超时；只有服务端确认拒绝/过期才释放批次。
            status = self.grant_status(grant) if synced['exit_code'] == 3 else None
            terminal = status in ('denied', 'rejected', 'expired', 'expired-unfetched')
            return {'status': 'rejected' if terminal else 'awaiting_grant' if status in ('pending', 'decision_in_progress', 'approved-executing') else 'blocked', 'grant': grant,
                    'grant_status': status, 'production': production,
                    'reason': f"同步退出 {synced['exit_code']}；授权状态 {status or '未确认为拒绝/过期'}；日志 {self.directory}"}
        self.verify(production)
        return {'status': 'ok', 'production': production, 'grant': grant}

    def grant(self):
        return grant_record(self.directory)

    def grant_status(self, grant):
        if not grant.get('id'):
            return None
        result = self.command(['python3', 'scripts/request_grant.py', 'status', grant['id'],
                               *(['--kind', 'exec'] if grant.get('kind') == 'exec' else [])],
                              cwd=self.checkout, check=False)
        for stream in ('stdout', 'stderr'):
            try:
                return json.loads(result[stream]).get('status')
            except (ValueError, AttributeError):
                pass
        return None

    def verify(self, production, label='同步脚本完成后'):
        config = self.target['deployment']
        app = json.loads(self.command(['kubectl', '--request-timeout=20s', '--context', config['context'],
                                      '-n', config['namespace'], 'get', 'application', config['app'], '-o', 'json'])['stdout'])
        status = app.get('status', {})
        if (status.get('sync', {}).get('status') != 'Synced' or status.get('health', {}).get('status') != 'Healthy'
                or status.get('sync', {}).get('revision') != production):
            raise Failure(label + ' Argo 回查未通过，不能继续发版')
        # production 可能被其他发布者改动，不能把旧脚本退出 0 当作当前批次上线。
        refs = self.git('ls-remote', 'origin', 'refs/heads/production')['stdout'].split()
        if not refs or refs[0] != production:
            raise Failure('回查时 production 已变化')
        rollout = config['rollout']
        self.command(['kubectl', '--context', config['context'], '-n', rollout['namespace'],
                      'rollout', 'status', 'deployment/' + rollout['name'], '--timeout=30s'])


def main():
    spec_path, inherited_lock = Path(sys.argv[1]), int(sys.argv[2])
    directory = spec_path.parent
    runner = None
    try:
        spec = read(spec_path)
        runner = Runner(spec, directory)
        result = runner.build() if spec['phase'] == 'build' else runner.deploy()
    except Exception as exc:
        result = {'status': 'failed' if read(spec_path).get('phase') == 'build' else 'blocked',
                  'reason': str(exc), 'log': str(directory)}
        if runner and read(spec_path).get('phase') == 'deploy':
            result['grant'] = runner.grant()
    write(directory / 'result.json', result)
    fcntl.flock(inherited_lock, fcntl.LOCK_UN)
    os.close(inherited_lock)


if __name__ == '__main__':
    main()
