"""集群发版批次；接单即占位，只有明确终态才释放下一批。"""
from dataclasses import asdict
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from workshop.core.models import After
from .io import grant_record, read, write


class ClusterRelease:
    def __init__(self, io):
        self.io = io

    def worker(self, target, state, phase):
        directory = self.io.root(target) / 'batches' / hashlib.sha256(state['item'].encode()).hexdigest() / phase
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / 'worker.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            # 只有持锁进程已退出才能消费结果；旧进程还可能有子进程或写操作。
            result = read(directory / 'result.json')
            if result:
                return result
            if (directory / 'spec.json').exists():
                grant = grant_record(directory)
                return {'status': 'blocked', 'reason': f"批次进程退出但结果不明；授权 {grant['id'] or '编号未知'}；不重新执行，核对 {directory}",
                        'log': str(directory), 'grant': grant}
            spec = {'item': state['item'], 'to': state['to'], 'phase': phase,
                    'initial': state.get('initial', False),
                    'target': target, 'from': state['from'], 'built': state.get('built')}
            write(directory / 'spec.json', spec)
            # flock 随 fd 交给独立进程；父作业退出、守护进程重启都不释放它。
            with (directory / 'worker.log').open('a') as output:
                try:
                    child = subprocess.Popen([sys.executable, '-m',
                        'workshop.plugins.scheduled_release.cluster_worker', str(directory / 'spec.json'), str(lock.fileno())],
                        stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                        start_new_session=True, pass_fds=(lock.fileno(),))
                except OSError as exc:
                    return {'status': 'blocked', 'reason': f'批次进程未能确认启动：{exc}；核对 {directory}'}
                write(directory / 'pid.json', {'pid': child.pid})
        return None

    def grant_outcome(self, state):
        grant = state['result']['grant']
        checkout = Path(state['built']['checkout'])
        result = self.io.command(['python3', str(checkout / 'scripts/request_grant.py'), 'status', grant['id']], timeout=30)
        try:
            status = json.loads(result['stdout']).get('status') if result['exit_code'] == 0 else None
        except (ValueError, AttributeError):
            status = None
        state['grant_check'] = result
        if status in ('denied', 'rejected', 'expired', 'expired-unfetched'):
            state.update(status='rejected', reason=f"授权 {grant['id']} {status}；日志 {grant['log']}")
            return After(notice=state['reason'])
        if status is None or status in ('pending', 'decision_in_progress'):
            return After(pending=True, reason=f"已有授权 {grant['id']} 仍未确认终止；日志 {grant['log']}",
                         next='只查询这次申请，不重新发卡', recheck_after='+5m')
        state.update(status='blocked', reason=f"授权 {grant['id']} {status}，原同步程序已退出；日志 {grant['log']}")
        return After(blocked=state['reason'], next='负责会话核对已有授权和线上状态；同步脚本不支持续接，不可直接重跑')

    def after(self, item, stage, key, target, intent):
        with self.io.locked(target):
            root = self.io.root(target)
            path = root / 'batches' / (hashlib.sha256(item['id'].encode()).hexdigest() + '.json')
            state = read(path) or {**intent, 'item': item['id'], 'status': 'reserved', 'effects': {}}
            active = read(root / 'cluster-release.json')
            if (active and active['item'] != item['id']
                    and active['status'] not in ('ok', 'rejected', 'failed')):
                return After(pending=True, reason='上一批发版尚未结束', next='等上一批结束后再处理', recheck_after='+1m')
            if key in state['effects']:
                return After(**state['effects'][key])
            if 'initial' not in state:
                state['initial'] = not active
            write(path, state)
            if not active or active['item'] == item['id'] or state['status'] == 'reserved':
                write(root / 'cluster-release.json', {k: state[k] for k in ('item', 'to', 'status')})
            result = After()
            if state['status'] == 'awaiting_grant':
                result = self.grant_outcome(state)
            elif state['status'] == 'blocked':
                result = After(blocked=state['reason'], next='负责会话核对批次日志和已有授权；不得重新调用同步脚本')
            elif state['status'] not in ('ok', 'failed', 'rejected'):
                phase = 'build' if stage == '证据' else 'deploy' if stage == '交付' else None
                if phase:
                    if phase == 'deploy' and not state.get('built'):
                        state.update(status='blocked', reason='缺少固定提交的 tag CI 成功证据')
                        result = After(blocked=state['reason'], next='核对批次构建证据')
                    else:
                        finished = self.worker(target, state, phase)
                        if finished is None:
                            result = After(pending=True, reason='批次程序正在等待 CI、授权或上线结果',
                                           next='认回同一程序并读取结果，不发起第二次申请', recheck_after='+1m')
                        elif finished['status'] == 'ok':
                            state['built' if phase == 'build' else 'deployed'] = finished
                            state['status'] = 'built' if phase == 'build' else 'ok'
                        else:
                            state.update(status=finished['status'], reason=finished['reason'], result=finished)
                            if state['status'] == 'awaiting_grant':
                                result = After(pending=True, reason=finished['reason'],
                                               next='只查询已有授权状态，不重新发卡', recheck_after='+5m')
                            elif state['status'] == 'blocked':
                                result = After(blocked=finished['reason'], next='负责会话核对批次日志和已有授权；不自动回滚或重新发卡')
                            else:
                                result = After(notice=f"整点发版 {state['to']} 停止：{finished['reason']}")
            if stage == '收尾' and state['status'] == 'ok':
                result = After(notice=f"整点发版完成：{state['to']}，production {state['deployed']['production']} 已上线。")
            if not result.pending:
                state['effects'][key] = asdict(result)
            write(path, state)
            # 来源只读摘要，不读核心状态库；占位从接单持续到实际完成或明确终止。
            current = read(root / 'cluster-release.json')
            if current.get('item') == item['id']:
                write(root / 'cluster-release.json', {k: state[k] for k in ('item', 'to', 'status')})
            return result
