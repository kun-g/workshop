"""按固定提交检查、上线并确认；所有阶段由程序完成。"""
from dataclasses import asdict
import hashlib
from pathlib import Path
import re

from workshop.core.models import After, Classification, Evidence
from workshop.core.ports import Retryable
from workshop.github_identity import scoped
from .io import IO, read, write

STAGES = ('接单', '说清楚', '执行', '证据', '交付', '验收', '收尾')
CHECKS = [['scripts/check.sh']]


class ScheduledRelease:
    def __init__(self, io=None):
        self.io = io or IO()

    def stages(self, target):
        return STAGES

    def automatic(self, item, stage, target):
        return True

    def classify(self, event, item, target, find_external=None):
        # CLI 不能自行构造一次上线；只有规则中来源的事件触发。
        return Classification(discard=True)

    def describe(self, event, target):
        return f"整点比较：{event.body.get('from', '')[:12]} → {event.body.get('to', '')[:12]}"

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

    def fail(self, state, reason):
        state['status'] = 'failed'
        state['failure'] = reason
        return After(notice=f"定时发版 {state['to']} 未上线：{reason}")

    def check(self, item, target, state, save):
        if state['status'] == 'checking':
            return self.fail(state, '上次检查进程退出但结果未确认，不重跑同一提交')
        root = self.io.root(target)
        checkout = Path(target['checkout']).expanduser().resolve()
        candidate = root / 'scheduled_release' / ('check-' + hashlib.sha256(item['id'].encode()).hexdigest())
        if not candidate.exists():
            self.io.git(checkout, 'fetch', '--no-tags', f"https://github.com/{target['repo']}.git", state['to'])
            self.io.git(checkout, 'worktree', 'add', '--detach', str(candidate), state['to'])
        if self.io.git(candidate, 'rev-parse', 'HEAD') != state['to']:
            return self.fail(state, '检查副本不是目标提交')
        state['candidate'] = str(candidate)
        state['status'] = 'checking'
        save()
        result = self.io.command(CHECKS[0], cwd=candidate, timeout=target.get('check_timeout', 300), isolated=True)
        state['check'] = result
        state['checked'] = state['to']
        state['status'] = 'checked'
        result_after = self.fail(state, f"检查退出 {result['exit_code']}；{result['stderr'][-500:]}") if result['exit_code'] else After()
        save()
        # 只收回自己创建的临时检查副本，不碰执行者 worktree。
        cleanup = self.io.command(['git', '-C', str(checkout), 'worktree', 'remove', '--force', str(candidate)])
        if cleanup['exit_code']:
            state['cleanup_error'] = cleanup
        return result_after

    def deploy(self, item, target, state, save):
        root = self.io.root(target)
        release = read(root / 'release.json')
        if release.get('item') == item['id'] and release.get('to') == state['to'] and release.get('status') in ('restarting', 'ok'):
            state['status'] = release['status']
            # 已重启则不要再次请求；同一旧进程尚未消费返回值则重放重启请求。
            return After(restart=release['status'] == 'restarting' and self.io.daemon() == release['daemon'])
        if release.get('status') == 'restarting':
            return After(pending=True, reason='上一件发版尚未确认', next='等上一件确认或退回后再检查主检出')
        if state.get('checked') != state['to'] or state.get('check', {}).get('exit_code') != 0:
            return self.fail(state, '目标提交缺少通过的检查')
        checkout = Path(target['checkout']).expanduser().resolve()
        if self.io.git(checkout, 'status', '--porcelain'):
            return self.fail(state, '主检出有未提交的改动或未跟踪的文件')
        branch = self.io.command(['git', '-C', str(checkout), 'symbolic-ref', '--short', 'HEAD'])
        if branch['exit_code'] or branch['stdout'].strip() != 'main':
            return self.fail(state, '主检出不在 main')
        head = self.io.git(checkout, 'rev-parse', 'HEAD')
        ancestor = self.io.command(['git', '-C', str(checkout), 'merge-base', '--is-ancestor', head, state['to']])
        if ancestor['exit_code']:
            return self.fail(state, '主检出不能快进到检查过的提交')
        daemon = self.io.daemon()
        if 'deploy_from' not in state:
            state['deploy_from'] = head
            save()
        self.io.git(checkout, 'merge', '--ff-only', state['to'])
        prepare = target.get('prepare', [])
        for index, step in enumerate(prepare):
            cwd = (checkout / step.get('cwd', '.')).resolve()
            if not cwd.is_relative_to(checkout):
                return self.fail(state, '准备命令目录超出主检出；已快进但未重启')
            results = state.setdefault('prepare_runs', {})
            old = results.get(str(index))
            if old and old.get('exit_code') == 0:
                continue
            if old:
                return self.fail(state, '准备命令未确认完成；已快进但未重启，需人工处理')
            results[str(index)] = {'argv': step['argv'], 'status': 'running'}
            save()
            result = self.io.command(step['argv'], cwd=cwd, timeout=target.get('release_timeout', 300), isolated=True)
            results[str(index)] = result
            save()
            if result['exit_code']:
                return self.fail(state, f"准备命令退出 {result['exit_code']}；已快进但未重启：{result['stderr'][-500:]}")
        record = {'from': state['deploy_from'], 'to': state['to'], 'at': self.io.now(),
                  'status': 'restarting', 'prepare': [s['argv'] for s in prepare],
                  'prepare_cwds': [s.get('cwd', '.') for s in prepare], 'item': item['id'], 'daemon': daemon}
        write(root / 'release.json', record)
        state['status'] = 'restarting'
        return After(restart=True)

    def confirm(self, item, target, state):
        root = self.io.root(target)
        release = read(root / 'release.json')
        if release.get('item') != item['id'] or release.get('to') != state['to']:
            raise Retryable('发版记录与本活不匹配')
        if release['status'] == 'ok':
            state['status'] = 'ok'
            return After()
        if release['status'] != 'restarting':
            return self.fail(state, '发版已停止：' + release['status'])
        daemon = self.io.daemon()
        old = release['daemon']
        if daemon == old or self.io.process(old['pid']) == old:
            return After(pending=True, reason='原守护进程尚未退出', next='等新进程恢复心跳后确认')
        checkout = Path(target['checkout']).expanduser().resolve()
        if self.io.git(checkout, 'rev-parse', 'HEAD') != state['to']:
            return self.fail(state, '重启后的主检出不是检查过的提交')
        if not self.io.heartbeat_ready(root, daemon):
            return After(pending=True, reason='新守护进程心跳尚未恢复', next='回查新进程心跳')
        release.update(status='ok', confirmed_at=self.io.now(), confirmed_daemon=daemon)
        write(root / 'release.json', release)
        state['status'] = 'ok'
        return After()

    @scoped
    def after(self, item, completed_stage, key, target):
        with self.io.locked(target):
            path = self.path(item, target)
            state = read(path) or {**self.intent(item), 'status': 'new', 'effects': {}}
            def save():
                write(path, state)
            if key in state['effects']:
                result = After(**state['effects'][key])
                if result.restart and self.io.daemon() != read(self.io.root(target) / 'release.json').get('daemon'):
                    return After()
                return result
            result = After()
            if state['status'] != 'failed':
                if completed_stage == '证据' and state.get('checked') != state['to']:
                    result = self.check(item, target, state, save)
                elif completed_stage == '交付':
                    result = self.deploy(item, target, state, save)
                elif completed_stage == '验收':
                    result = self.confirm(item, target, state)
                elif completed_stage == '收尾':
                    result = After(notice=f"定时发版完成：{state['to']}，新进程心跳已恢复。")
            if not result.pending:
                state['effects'][key] = asdict(result)
            save()
            return result


def make_flow():
    return ScheduledRelease()
