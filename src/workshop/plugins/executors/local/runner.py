"""Durable single-child supervisor. Invoked by absolute file path, including in Orca."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

# A file entry point works even when launchd/Orca cwd is outside the installed project.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from workshop.plugins.executors.local.storage import me, read, write
from workshop.plugins.executors.local.isolation import session_environment


def vendor_session(output: Path, kind: str):
    if not output.exists():
        return None
    for line in output.read_text(errors='replace').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if kind == 'codex' and row.get('type') == 'thread.started':
            return row.get('thread_id')
        if kind == 'cursor-grok' and row.get('session_id'):
            return row['session_id']
    return None


def result_receipt(output: Path, kind: str):
    """Read-only agents return receipt JSON as their final answer; supervisor submits it."""
    if not output.exists():
        return None
    text = output.read_text(errors='replace')
    candidates = [text]
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get('type') == 'item.completed' and row.get('item', {}).get('type') == 'agent_message':
            candidates.append(row['item'].get('text', ''))
        elif row.get('type') == 'result':
            candidates.append(row.get('result', ''))
    for candidate in reversed(candidates):
        if not isinstance(candidate, str):
            continue
        candidate = candidate.strip()
        if candidate.startswith('```'):
            candidate = '\n'.join(candidate.splitlines()[1:-1])
        try:
            result = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(result, dict) and result.get('outcome') in ('done', 'nothing', 'failed') and isinstance(result.get('summary'), str):
            return result
    return None


def main(directory: Path):
    spec = read(directory / 'spec.json')
    with (directory / 'run.lock').open('a') as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # Permanent fence closes the fork-before-journal crash window. Ambiguity never respawns.
        if any((directory / name).exists() for name in ('claimed.json', 'stopping.json', 'stopped.json')):
            return
        write(directory / 'claimed.json', {**me(), 'terminal': os.environ.get('ORCA_TERMINAL_HANDLE')})
        env = session_environment(spec)
        argv = list(spec['argv'])
        if spec['interactive']:
            from workshop.plugins.executors.local.trust import prepare
            prepare(spec.get('trust_workdir', spec['workdir']), env, directory)
            prompt = Path(spec['prompt']).read_text()
            if len(prompt.encode()) > 8192:
                prompt = '请读取派活说明文件 ' + json.dumps(spec['prompt'], ensure_ascii=False) + '，其中说明与资料已分开标注。'
                if spec['readonly']:
                    argv += ['--add-dir', str(directory)]
            (directory / 'initial-prompt.txt').write_text(prompt)
            argv += ['--', prompt]
        elif spec['kind'] == 'codex':
            argv += ['-']
        output = directory / 'output.log'
        child = None
        def forward(signum, frame):
            if child is not None:
                # Do not call Popen.poll/send_signal from a signal handler: wait() may
                # hold its non-reentrant waitpid lock when the signal interrupts it.
                try:
                    os.kill(child.pid, signum)
                except ProcessLookupError:
                    pass
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, forward)
        try:
            with output.open('a') as stream, Path(spec['prompt']).open('rb') as prompt_input:
                child = subprocess.Popen(argv, cwd=spec['workdir'], env=env,
                    stdin=None if spec['interactive'] else prompt_input,
                    stdout=None if spec['interactive'] else stream,
                    stderr=None if spec['interactive'] else subprocess.STDOUT)
                from workshop.plugins.executors.local.storage import identity
                write(directory / 'child.json', {'pid': child.pid, 'birth': identity(child.pid), 'group': me()['group']})
                code = child.wait()
        except Exception as exc:
            code = -1
            with output.open('a') as stream:
                stream.write('\n启动或等待失败: ' + repr(exc))
        real_id = vendor_session(output, spec['kind'])
        if real_id:
            write(Path(spec['session_file']), {'vendor_id': real_id, 'model': spec.get('model')})
        if not spec['interactive']:
            receipt = result_receipt(output, spec['kind']) if code == 0 else None
            if receipt is None:
                tail = output.read_text(errors='replace')[-8000:] if output.exists() else ''
                receipt = {'outcome': 'failed', 'summary': f'进程退出 ({code})，未交回执。最后输出：\n{tail}'}
            write(directory / 'fallback-receipt.json', receipt)
            # Existing receipts are immutable in the core: a direct receipt wins this race.
            try:
                result = subprocess.run(spec['receipt_command'] + ['receipt', spec['token'], '--file',
                    str(directory / 'fallback-receipt.json')], env=env, capture_output=True, text=True, timeout=30)
                submitted = {'code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
            except (OSError, subprocess.TimeoutExpired) as exc:
                submitted = {'code': -1, 'stdout': '', 'stderr': str(exc)}
            write(directory / 'receipt-result.json', submitted)
        write(directory / 'exited.json', {'code': code})


if __name__ == '__main__':
    main(Path(sys.argv[1]))
