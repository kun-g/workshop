"""独立启动入口：本环境的解释器、唯一规则路径、独立状态目录。"""
import argparse
import json
import os
import plistlib
from pathlib import Path
import select
import subprocess
import sys
from .client import request
from .server import serve


def start(state, rules, endpoint):
    try:
        return request(endpoint, 'ping', {})
    except (OSError, ValueError):
        pass
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    reader, writer = os.pipe()
    try:
        with (state / 'service.log').open('ab') as output:
            process = subprocess.Popen([sys.executable, '-m', 'workshop.plugins.intervention',
                '--state-dir', str(state), '--rules', str(rules), '--endpoint', str(endpoint),
                'serve', '--ready-fd', str(writer)], pass_fds=(writer,), stdout=output, stderr=output,
                stdin=subprocess.DEVNULL, start_new_session=True)
        os.close(writer)
        writer = None
        ready, _, _ = select.select([reader], [], [], 15)
        if ready and os.read(reader, 64) == b'ready\n':
            return {'started': process.pid, **request(endpoint, 'ping', {})}
        # 并发启动的另一入口可能先取得服务锁；以服务响应确认成功。
        try:
            return request(endpoint, 'ping', {})
        except (OSError, ValueError):
            process.terminate()
            process.wait(timeout=5)
            raise RuntimeError('独立服务未就绪；查看独立状态目录 service.log') from None
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)


def main():
    parser = argparse.ArgumentParser(prog='workshop-intervention')
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--rules', type=Path, required=True)
    parser.add_argument('--endpoint', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('serve').add_argument('--ready-fd', type=int)
    sub.add_parser('start')
    sub.add_parser('restart', help='重启已安装的本用户 launchd 服务')
    sub.add_parser('plist', help='生成独立 launchd 定义，不加载').add_argument('--output', type=Path, required=True)
    call = sub.add_parser('call')
    call.add_argument('action', choices=['ping', 'create', 'get', 'status', 'answer', 'ack', 'permit', 'enqueue', 'dispatch', 'cancel'])
    call.add_argument('--file', type=Path, help='请求 JSON；省略时从 stdin 读')
    args = parser.parse_args()
    try:
        if args.command == 'serve':
            serve(args.state_dir, args.rules, args.endpoint, args.ready_fd)
            return 0
        if args.command == 'plist':
            state = args.state_dir.resolve()
            args.output.write_bytes(plistlib.dumps({
                'Label': 'com.kun.workshop-intervention',
                'ProgramArguments': [sys.executable, '-m', 'workshop.plugins.intervention',
                    '--state-dir', str(state), '--rules', str(args.rules.resolve()),
                    '--endpoint', str(args.endpoint.resolve()), 'serve'],
                'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 5,
                'StandardOutPath': str(state / 'service.log'), 'StandardErrorPath': str(state / 'service.log'),
            }))
            print(args.output)
            return 0
        if args.command == 'restart':
            return subprocess.run(['/bin/launchctl', 'kickstart', '-k',
                                   f'gui/{os.getuid()}/com.kun.workshop-intervention']).returncode
        if args.command == 'start':
            result = start(args.state_dir.resolve(), args.rules.resolve(), args.endpoint.resolve())
        else:
            data = json.loads(args.file.read_text() if args.file else sys.stdin.read())
            result = request(args.endpoint, args.action, data, timeout=25)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
