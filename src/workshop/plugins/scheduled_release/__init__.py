"""按固定提交检查、上线并确认；所有阶段由程序完成。"""
from dataclasses import asdict
from datetime import datetime
import hashlib
from pathlib import Path
import re
import time

from workshop.core.models import After, Classification, Evidence
from workshop.core.ports import Retryable
from workshop.github_identity import scoped
from .io import IO, read, write, release_path
from .cluster import ClusterRelease

STAGES = ('接单', '说清楚', '执行', '证据', '交付', '验收', '收尾')
CHECKS = [['scripts/check.sh']]


class ScheduledRelease:
    def __init__(self, io=None):
        self.io = io or IO()

    def stages(self, target):
        configured = tuple(target.get('stages', STAGES))
        supported = (STAGES,) if target.get('scheduled_release_mode') == 'cluster' else (STAGES, STAGES[3:])
        if configured not in supported:
            raise ValueError('发版阶段必须保留证据、交付、验收、收尾；集群批次还须保留接单占位')
        return configured

    def automatic(self, item, stage, target):
        return True

    def classify(self, event, item, target, find_external=None):
        # CLI 不能自行构造一次上线；只有规则中来源的事件触发。
        return Classification(discard=True)

    def describe(self, event, target):
        return f"定时比较：{event.body.get('from', '')[:12]} → {event.body.get('to', '')[:12]}"

    def path(self, item, target):
        return self.io.root(target) / 'scheduled_release' / (hashlib.sha256(item['id'].encode()).hexdigest() + '.json')

    def assess(self, item, receipt, target):
        state = read(self.path(item, target))
        # 检查失败是这件发版活的终态，其后阶段只跳过动作并收尾，不能返工重试同一提交。
        return Evidence(checks=CHECKS, passed=True, reason=state.get('failure', '程序阶段'), reviewers=1)

    @staticmethod
    def intent(item):
        for material in item.get('materials', []):
            body = material.get('body', {})
            if material.get('source') == 'main_branch' and isinstance(body, dict) and body.get('kind') == 'ahead':
                if body.get('to') == item['object_key'] and all(re.fullmatch('[0-9a-f]{40}', body.get(k, '')) for k in ('from', 'to')):
                    return {'from': body['from'], 'to': body['to']}
        raise ValueError('发版缺少 main_branch 的完整提交证据')

    def fail(self, state, reason, *, phase='delivery'):
        state['status'] = 'failed'
        state['failure'] = reason
        # 检查失败是这件发版活的终态；交付阶段失败可以由项目经理放回后从失败的那一步重做。
        state['failure_phase'] = phase
        # 不发通知（介入点 R13）：发版没成记成卡住，交项目经理（介入点 R11）。
        return After(blocked=f"定时发版 {state['to']} 未上线：{reason}", next="项目经理查明发版没成的原因后处理")

    def record_failure(self, item, target, state):
        """交付阶段失败写入 release.json（目标提交、时刻、原因）；看门认得 failed，不报状态无效。"""
        root = self.io.root(target)
        previous = read(release_path(root))
        record = {**previous,
                  'from': state['from'], 'to': state['to'],
                  'at': previous.get('at') or self.io.now(),
                  'status': 'failed', 'failure': state.get('failure', ''),
                  'item': item['id'], 'daemon': previous.get('daemon')}
        record.setdefault('prepare', [])
        record.setdefault('prepare_cwds', [s.get('cwd', '.') for s in
                                           target.get('prepare', []) + target.get('release_prepare', [])])
        write(release_path(root), record)

    def clear_failed_runs(self, state):
        """项目经理放回后重做：已退出非零的步骤记录不再直接判失败，清掉重跑；成功的保留。"""
        for group in ('prepare', 'release_prepare'):
            runs = state.get(group + '_runs')
            if not isinstance(runs, dict):
                continue
            state[group + '_runs'] = {index: value for index, value in runs.items()
                                      if isinstance(value, dict) and value.get('exit_code') == 0}

    def retry(self, item, target, state, save):
        """交付阶段失败、项目经理放回后：清标记，从失败的那一步重做。"""
        state['status'] = 'retrying'
        state['failure'] = ''
        state['failure_phase'] = ''
        self.clear_failed_runs(state)
        root = self.io.root(target)
        release = read(release_path(root))
        if not (release.get('item') == item['id'] and release.get('to') == state['to']):
            release = {}
        checkout = Path(target['checkout']).expanduser().resolve()
        actual, old = self.io.daemon(), release.get('daemon')
        # 已经换上新进程只是没确认成功：接着做剩下的一步，不再请求重启。
        restarted = bool(release) and old and actual != old and self.io.process(old['pid']) != old
        if restarted:
            if self.io.git(checkout, 'rev-parse', 'HEAD') != state['to']:
                return self.fail(state, '重启后的主检出不是检查过的提交', phase='delivery')
            failure = self.prepare_steps(target, state, save, ('release_prepare',), '已重启但尚未确认成功')
            if failure:
                return failure
            release.update(status='ok', confirmed_at=self.io.now(), confirmed_daemon=actual,
                           retried_from='failed')
            write(release_path(root), release)
            state['status'] = 'ok'
            return After()
        # 还没换上新进程：重新快进、重跑失败的那一步、请求重启。
        if release.get('status') == 'failed':
            write(release_path(root), {})
        return self.deploy(item, target, state, save)

    def check(self, item, target, state, save):
        if state['status'] == 'checking':
            return self.fail(state, '上次检查进程退出但结果未确认，不重跑同一提交', phase='check')
        root = self.io.root(target)
        began = time.monotonic()
        state['check_started_at'] = self.io.now()
        timings = state['check_seconds'] = {}
        checkout = Path(target['checkout']).expanduser().resolve()
        candidate = root / 'scheduled_release' / ('check-' + hashlib.sha256(item['id'].encode()).hexdigest())
        if not candidate.exists():
            self.io.git(checkout, 'fetch', '--no-tags', f"https://github.com/{target['repo']}.git", state['to'])
            self.io.git(checkout, 'worktree', 'add', '--detach', str(candidate), state['to'])
        if self.io.git(candidate, 'rev-parse', 'HEAD') != state['to']:
            return self.fail(state, '检查副本不是目标提交', phase='check')
        timings['checkout'] = time.monotonic() - began
        state['candidate'] = str(candidate)
        state['status'] = 'checking'
        save()
        checking = time.monotonic()
        result = self.io.command(CHECKS[0], cwd=candidate, timeout=target.get('check_timeout', 300), isolated=True,
                                 cache_dir=root / 'dependency-cache' / 'uv')
        timings['command'] = time.monotonic() - checking
        state['check'] = result
        state['checked'] = state['to']
        state['status'] = 'checked'
        result_after = self.fail(state, f"检查退出 {result['exit_code']}；{result['stderr'][-500:]}", phase='check') if result['exit_code'] else After()
        save()
        # 只收回自己创建的临时检查副本，不碰执行者 worktree。
        cleaning = time.monotonic()
        cleanup = self.io.command(['git', '-C', str(checkout), 'worktree', 'remove', '--force', str(candidate)])
        timings['cleanup'] = time.monotonic() - cleaning
        timings['total'] = time.monotonic() - began
        state['check_finished_at'] = self.io.now()
        save()
        if cleanup['exit_code']:
            state['cleanup_error'] = cleanup
        return result_after

    def deploy(self, item, target, state, save):
        root = self.io.root(target)
        release = read(release_path(root))
        if release.get('item') == item['id'] and release.get('to') == state['to'] and release.get('status') in ('restarting', 'ok'):
            state['status'] = release['status']
            # 已重启则不要再次请求；同一旧进程尚未消费返回值则重放重启请求。
            return After(restart=release['status'] == 'restarting' and self.io.daemon() == release['daemon'])
        if release.get('status') in ('restarting', 'rolling_back', 'rollback_failed'):
            return After(pending=True, reason='上一件发版尚未确认', next='等上一件确认或退回后再检查主检出')
        if state.get('checked') != state['to'] or state.get('check', {}).get('exit_code') != 0:
            return self.fail(state, '目标提交缺少通过的检查', phase='delivery')
        checkout = Path(target['checkout']).expanduser().resolve()
        if self.io.git(checkout, 'status', '--porcelain'):
            return self.fail(state, '主检出有未提交的改动或未跟踪的文件', phase='delivery')
        branch = self.io.command(['git', '-C', str(checkout), 'symbolic-ref', '--short', 'HEAD'])
        if branch['exit_code'] or branch['stdout'].strip() != 'main':
            return self.fail(state, '主检出不在 main', phase='delivery')
        head = self.io.git(checkout, 'rev-parse', 'HEAD')
        ancestor = self.io.command(['git', '-C', str(checkout), 'merge-base', '--is-ancestor', head, state['to']])
        if ancestor['exit_code']:
            return self.fail(state, '主检出不能快进到检查过的提交', phase='delivery')
        daemon = self.io.daemon()
        self.io.git(checkout, 'merge', '--ff-only', state['to'])
        state['fast_forwarded'] = True
        prepare = target.get('prepare', [])
        failure = self.prepare_steps(target, state, save, ('prepare', 'release_prepare'))
        if failure:
            return failure
        # 退回点是发版前在跑的版本；主检出可能早被快进到目标提交（#126）。
        # release_prepare 管独立服务，不作为旧版主环境的恢复命令；独立安装器自行保留旧环境。
        record = {'from': state['from'], 'to': state['to'], 'at': self.io.now(),
                  'status': 'restarting', 'prepare': [s['argv'] for s in prepare],
                  'prepare_cwds': [s.get('cwd', '.') for s in prepare], 'item': item['id'], 'daemon': daemon}
        write(release_path(root), record)
        state['status'] = 'restarting'
        return After(restart=True)

    def prepare_steps(self, target, state, save, groups, position='已快进但未重启'):
        root = self.io.root(target)
        checkout = Path(target['checkout']).expanduser().resolve()
        # 独立常驻服务只在发版时装配；检查副本的 prepare 不得启动服务。
        steps = [(group, index, step) for group in groups
                 for index, step in enumerate(target.get(group, []))]
        for group, index, step in steps:
            cwd = (checkout / step.get('cwd', '.')).resolve()
            if not cwd.is_relative_to(checkout):
                return self.fail(state, f'准备命令目录超出主检出；{position}', phase='delivery')
            results = state.setdefault(group + '_runs', {})
            old = results.get(str(index))
            if old and old.get('exit_code') == 0:
                continue
            if old:
                return self.fail(state, f'准备命令未确认完成；{position}，需人工处理', phase='delivery')
            results[str(index)] = {'argv': step['argv'], 'status': 'running'}
            save()
            result = self.io.command(step['argv'], cwd=cwd, timeout=target.get('release_timeout', 300), isolated=True,
                                     cache_dir=root / 'dependency-cache' / 'uv')
            results[str(index)] = result
            save()
            if result['exit_code']:
                return self.fail(state, f"准备命令退出 {result['exit_code']}；{position}：{result['stderr'][-500:]}", phase='delivery')
        return None

    def confirm(self, item, target, state, save):
        root = self.io.root(target)
        release = read(release_path(root))
        if release.get('item') != item['id'] or release.get('to') != state['to']:
            raise Retryable('发版记录与本活不匹配')
        if release['status'] == 'ok':
            state['status'] = 'ok'
            return After()
        if release['status'] != 'restarting':
            return self.fail(state, '发版已停止：' + release['status'], phase='delivery')
        runtime = target.get('runtime', {})
        daemon, first_ok_at = runtime.get('daemon'), runtime.get('first_ok_at')
        if not daemon or not first_ok_at:
            return After(pending=True, reason='等待核心重启确认', next='由核心在新进程首拍成功后重调')
        actual = self.io.daemon()
        old = release['daemon']
        if ({k: daemon.get(k) for k in ('pid', 'start')} != actual
                or actual == old or self.io.process(old['pid']) == old):
            return After(pending=True, reason='原守护进程尚未退出', next='等新进程恢复心跳后确认')
        checkout = Path(target['checkout']).expanduser().resolve()
        if self.io.git(checkout, 'rev-parse', 'HEAD') != state['to']:
            return self.fail(state, '重启后的主检出不是检查过的提交', phase='delivery')
        if datetime.fromisoformat(first_ok_at) < datetime.fromisoformat(release['at']):
            raise ValueError('首拍成功时间早于重启请求')
        # 首次升级由不认识 release_prepare 的旧进程发起；新进程确认前补齐。
        failure = self.prepare_steps(target, state, save, ('release_prepare',), '已重启但尚未确认成功')
        if failure:
            return failure
        release.update(status='ok', confirmed_at=self.io.now(), confirmed_daemon=daemon,
                       first_ok_at=first_ok_at)
        write(release_path(root), release)
        state['status'] = 'ok'
        return After()

    @scoped
    def after(self, item, completed_stage, key, target, services=None):
        if target.get('scheduled_release_mode', 'self') == 'cluster':
            return ClusterRelease(self.io).after(item, completed_stage, key, target, self.intent(item))
        with self.io.locked(target):
            path = self.path(item, target)
            state = read(path) or {**self.intent(item), 'status': 'new', 'effects': {}}
            def save():
                write(path, state)
            release = read(release_path(self.io.root(target)))
            if (release.get('item') == item['id'] and release.get('to') == state['to']
                    and release.get('status') in ('rolling_back', 'rollback_failed', 'rolled_back')):
                # 在旧 after key 重放之前拦住重启请求/成功通知；通知由独立看门负责。
                state.update(status='failed', failure='看门已停止本次发版：' + release['status'],
                             failure_phase='delivery')
                state['effects'][key] = asdict(After())
                save()
                return After()
            # 交付阶段失败、项目经理放回后重做：清掉失败标记，从失败的那一步接着做。
            # 判据是同一阶段又跑了一次（放回）；失败后按原阶段往下走的钩子不算放回。
            if (state.get('status') == 'failed' and state.get('failure_phase') == 'delivery'
                    and completed_stage == state.get('failure_stage')
                    and key not in state['effects']):
                result = self.retry(item, target, state, save)
                if state.get('status') == 'failed' and state.get('failure_phase') == 'delivery':
                    self.record_failure(item, target, state)
                if not result.pending:
                    state['effects'][key] = asdict(result)
                save()
                return result
            if target.get('runtime', {}).get('first_ok_at'):
                result = self.confirm(item, target, state, save)
                if not result.pending:
                    state['effects'][key] = asdict(result)
                save()
                return result
            if key in state['effects']:
                result = After.stored(state['effects'][key])
                if result.restart and self.io.daemon() != read(release_path(self.io.root(target))).get('daemon'):
                    return After()
                return result
            result = After()
            if state['status'] != 'failed':
                if completed_stage == '证据' and state.get('checked') != state['to']:
                    result = self.check(item, target, state, save)
                elif completed_stage == '交付':
                    result = self.deploy(item, target, state, save)
                elif completed_stage == '验收':
                    result = self.confirm(item, target, state, save)
                if state.get('status') == 'failed' and state.get('failure_phase') == 'delivery':
                    state['failure_stage'] = completed_stage
                    if state.get('fast_forwarded'):
                        # 快进之后失败：看门和状态查询都从 release.json 看得到目标和原因。
                        self.record_failure(item, target, state)
            if not result.pending:
                state['effects'][key] = asdict(result)
            save()
            return result


def make_flow():
    return ScheduledRelease()
