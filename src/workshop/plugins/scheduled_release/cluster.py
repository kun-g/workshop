"""集群发版批次；接单即占位，只有明确终态才释放下一批。"""
from dataclasses import asdict
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from workshop.core.models import After
from workshop.authorization import read as read_authorization
from .io import grant_record, read, write, release_path


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
                return {'status': 'awaiting_grant' if phase == 'deploy' and grant.get('id') else 'blocked', 'reason': f"批次进程退出但结果不明；授权 {grant['id'] or '编号未知'}；不重新执行，核对 {directory}",
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

    def grant_outcome(self, state, target):
        grant = state['result']['grant']
        directory = self.io.root(target) / 'batches' / hashlib.sha256(state['item'].encode()).hexdigest() / 'deploy'
        checkpoint = read(directory / 'production.json')
        production = checkpoint.get('revision') or state['result'].get('production')
        if not grant.get('id') or not production or checkpoint.get('source_commit', state['to']) != state['to']:
            state.update(status='blocked', reason='原同步缺原申请号或固定目标检查点，不能推测或重做')
            return After(blocked=state['reason'], next='核对原批次 production.json 和原申请')
        spec = {'request': grant['id'], 'kind': grant.get('kind', 'exec' if grant['id'].startswith('exec-') else 'grant'),
                'action': '集群同步', 'provider_action': 'argocd/sync_app', 'checkpoint': str(directory),
                'target': {'instance': target.get('grant_instance', 'central'),
                           'app': target['deployment']['app'], 'revision': production}}
        if state.get('authorization') and state['authorization'] != spec:
            return After(blocked='批次原申请或目标检查点变了', next='核对原申请；禁止改目标或重跑同步')
        state['authorization'] = spec
        # 在网络调用前落盘，进程重启只从同一检查点恢复。
        path = directory.parent.with_suffix('.json')
        write(path, state)
        result = read_authorization(spec, target.get('authorization_client') or
                                    str(Path(state['built']['checkout']) / 'scripts/request_grant.py'))
        state['grant_check'] = result
        outcome = result['verification']['result']
        if outcome == 'approved':
            state.update(status='ok', deployed={'status': 'ok', 'production': production, 'grant': grant,
                         'verification': result['verification'], 'execution': result['execution']})
            return After()
        reason = f"授权中心 {grant['id']}：{outcome}；只核实原同步检查点"
        if outcome in ('pending', 'unknown'):
            state['status'] = 'awaiting_grant'
            return After(pending=True, reason=reason, next=f"只查询原申请 {grant['id']}，不重跑同步", recheck_after='+5m')
        state.update(status='rejected', reason=reason)
        return After()

    def after(self, item, stage, key, target, intent):
        with self.io.locked(target):
            root = self.io.root(target)
            path = root / 'batches' / (hashlib.sha256(item['id'].encode()).hexdigest() + '.json')
            state = read(path) or {**intent, 'item': item['id'], 'status': 'reserved', 'effects': {}}
            active = read(release_path(root, cluster=True))
            if (active and active['item'] != item['id']
                    and active['status'] not in ('ok', 'rejected', 'failed')):
                return After(pending=True, reason='上一批发版尚未结束', next='等上一批结束后再处理', recheck_after='+1m')
            if key in state['effects']:
                return After.stored(state['effects'][key])
            if 'initial' not in state:
                state['initial'] = not active
            write(path, state)
            if not active or active['item'] == item['id'] or state['status'] == 'reserved':
                write(release_path(root, cluster=True), {k: state[k] for k in ('item', 'to', 'status')})
            result = After()
            if state['status'] == 'awaiting_grant' or (state['status'] == 'blocked' and state.get('result', {}).get('grant', {}).get('id')):
                result = self.grant_outcome(state, target)
            elif state['status'] == 'blocked':
                result = After(blocked=state['reason'], next='项目经理核对批次日志和已有授权；不得重新调用同步脚本')
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
                            if phase == 'deploy' and finished.get('grant', {}).get('id'):
                                state['status'] = 'awaiting_grant'
                            if state['status'] == 'awaiting_grant':
                                result = After(pending=True, reason=finished['reason'],
                                               next='只查询已有授权状态，不重新发卡', recheck_after='+5m')
                            elif state['status'] == 'blocked':
                                result = After(blocked=finished['reason'], next='项目经理核对批次日志和已有授权；不自动回滚或重新发卡')
                            else:
                                result = After()
            if stage == '收尾' and state['status'] == 'ok':
                result = After()
            if not result.pending:
                state['effects'][key] = asdict(result)
            write(path, state)
            # 来源只读摘要，不读核心状态库；占位从接单持续到实际完成或明确终止。
            current = read(release_path(root, cluster=True))
            if current.get('item') == item['id']:
                write(release_path(root, cluster=True), {k: state[k] for k in ('item', 'to', 'status')})
            return result
