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


def final_message(output: Path, start: int = 0):
    """The agent's last answer text (codex agent_message / stream-json result); None if it never answered."""
    if not output.exists():
        return None
    with output.open('rb') as stream:
        stream.seek(start)
        text = stream.read().decode(errors='replace')
    final = None
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get('type') == 'item.completed' and row.get('item', {}).get('type') == 'agent_message':
            final = row['item'].get('text', '')
        elif row.get('type') == 'result' and isinstance(row.get('result'), str):
            final = row['result']
    return final if final is not None else text


def result_receipt(output: Path, kind: str, start: int = 0):
    """Read-only agents return receipt JSON as their final answer; supervisor submits it.

    Agents often wrap it in prose or a ```json fence, so take the last JSON object in the
    final answer that has an outcome. Field values are not judged here: the core's own
    validation decides and its reason goes back to the agent.
    """
    text = final_message(output, start)
    if not isinstance(text, str):
        return None
    decoder = json.JSONDecoder()
    found = None
    index = text.find('{')
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find('{', index + 1)
            continue
        if isinstance(value, dict) and 'outcome' in value:
            found = value
        index = text.find('{', end)
    return found


RETRIES = 1  # 同一轮里把拒绝原因发回会话重交的次数
VENDOR_SESSION = '<vendor-session>'  # retry_argv 里待换成厂商会话号的占位
RECEIPT_REJECTED = 'receipt-rejected.json'  # 这一轮改过仍没交出合格回执；下一轮不接回这个会话
FIELDS = 'outcome、summary、advance、evidence、ask、notice、later、events、leave'
FINAL_ROUND = ('该轮已交', '该轮不能再交', '未知轮次')  # 核心说这一轮已经收过或结束了，重交无用


def submit(directory, spec, env, name, receipt):
    write(directory / name, receipt)
    try:
        result = subprocess.run(spec['receipt_command'] + ['receipt', spec['token'], '--file', str(directory / name)],
                                env=env, capture_output=True, text=True, timeout=30)
        return {'code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'code': -1, 'stdout': '', 'stderr': str(exc)}


def rejection(submitted):
    text = (submitted['stderr'] or submitted['stdout']).strip()
    return text.removeprefix('车间：').strip() or f'交回执命令退出码 {submitted["code"]}'


def retry_argv(spec, output):
    template = spec.get('retry_argv')
    vendor = vendor_session(output, spec['kind']) or spec.get('vendor_id')
    if not template or not vendor:
        return None
    return [vendor if part == VENDOR_SESSION else part for part in template]


def proxy_receipt(directory, spec, env, output, code, run):
    """代交最后一条消息里的回执；被拒就在同一轮把原因发回会话重交，仍不行再代交带原因的 failed。"""
    problems = []
    start = 0
    for attempt in range(RETRIES + 1):
        receipt = result_receipt(output, spec['kind'], start) if code == 0 else None
        if receipt is None:
            problem = (f'进程退出 ({code})，未交回执' if code != 0 else
                       '最后一条消息里没有回执 JSON 对象（要有 outcome、summary）')
        else:
            name = 'fallback-receipt.json' if attempt == 0 else f'fallback-receipt-{attempt + 1}.json'
            submitted = submit(directory, spec, env, name, receipt)
            reason = rejection(submitted)
            if submitted['code'] == 0 or any(mark in reason for mark in FINAL_ROUND):
                return code, submitted
            problem = '回执被拒：' + reason
        problems.append(problem)
        argv = retry_argv(spec, output)
        if code != 0 or attempt == RETRIES or not argv or (directory / 'stopping.json').exists():
            break
        prompt = directory / f'retry-{attempt + 1}.txt'
        prompt.write_text(
            f'车间没收下这一轮的回执：{problem}。\n'
            '照这个原因改对，最后一条消息只输出回执 JSON 对象本身，不加说明文字。'
            f'回执顶层只认 {FIELDS}，别的字段放进 evidence 那条 JSON；outcome 只能是 done、failed、nothing；'
            'advance 照派活说明里写的本阶段可填值。已经做完的工作不用重做。\n')
        start = output.stat().st_size if output.exists() else 0
        code = run(argv, prompt)
    if code == 0:
        # 会话答了话却连着交不出合格回执：下一轮换新会话，不再接回。
        write(directory / RECEIPT_REJECTED, {'problems': problems})
    tail = output.read_text(errors='replace')[-8000:] if output.exists() else ''
    final = final_message(output, start)
    summary = problems[0] if len(problems) == 1 else '；'.join(
        f'第 {index} 次：{problem}' for index, problem in enumerate(problems, 1))
    failed = {'outcome': 'failed', 'summary': summary,
              'evidence': ['最后一条消息（末尾）：\n' + (final or '')[-4000:], '最后输出（末尾）：\n' + tail]}
    return code, submit(directory, spec, env, 'failed-receipt.json', failed)


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
        def run(argv, prompt_path):
            nonlocal child
            try:
                with output.open('a') as stream, Path(prompt_path).open('rb') as prompt_input:
                    child = subprocess.Popen(argv, cwd=spec['workdir'], env=env,
                        stdin=None if spec['interactive'] else prompt_input,
                        stdout=None if spec['interactive'] else stream,
                        stderr=None if spec['interactive'] else subprocess.STDOUT)
                    from workshop.plugins.executors.local.storage import identity
                    write(directory / 'child.json', {'pid': child.pid, 'birth': identity(child.pid), 'group': me()['group']})
                    return child.wait()
            except Exception as exc:
                with output.open('a') as stream:
                    stream.write('\n启动或等待失败: ' + repr(exc))
                return -1
        code = run(argv, spec['prompt'])
        real_id = vendor_session(output, spec['kind'])
        if real_id:
            write(Path(spec['session_file']), {'vendor_id': real_id, 'model': spec.get('model')})
        if not spec['interactive']:
            code, submitted = proxy_receipt(directory, spec, env, output, code, run)
            write(directory / 'receipt-result.json', submitted)
        write(directory / 'exited.json', {'code': code})


if __name__ == '__main__':
    main(Path(sys.argv[1]))
