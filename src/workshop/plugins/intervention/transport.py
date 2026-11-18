"""飞书私聊送达命令：稳定标识先落盘，结果不明只回查，不盲目重发。"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from xml.etree import ElementTree


def save(path, value):
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)


def command(args, argv, **kwargs):
    result = subprocess.run([args.lark, '--profile', args.profile, *argv, '--as', 'bot', '--format', 'json'],
                            capture_output=True, text=True, timeout=15, **kwargs)
    value = json.loads(result.stdout) if result.stdout else {}
    if result.returncode or value.get('ok') is not True:
        raise ValueError('飞书命令未确认成功')
    return value


def attachment_name(content):
    try:
        value = ElementTree.fromstring(content)
        return value.get('name') if value.tag == 'file' else None
    except (ElementTree.ParseError, TypeError):
        return None


def deliver(args, message):
    if message.get('action') == 'reply':
        payload = message['payload']
        value = command(args, ['im', '+chat-messages-list', '--chat-id', args.chat_id,
            '--start', payload['started'], '--page-all', '--page-limit', '100', '--no-reactions'])
        for row in reversed(value.get('data', {}).get('messages', [])):
            sender = row.get('sender', {})
            if (row.get('reply_to') == payload['external'] and row.get('chat_id') == args.chat_id
                    and sender.get('sender_type') == 'user' and sender.get('id') == args.recipient
                    and sender.get('id_type') == 'open_id' and row.get('msg_type') == 'text' and not row.get('deleted')):
                text = row.get('content', '')
                if text == '过' or text.startswith('不对'):
                    return {'message_id': row['message_id'], 'reply_to': row['reply_to'], 'text': text,
                            'sender': args.recipient, 'chat': args.chat_id}
        return {}
    if message.get('action') not in ('send', 'reconcile') or not isinstance(message.get('key'), str):
        raise ValueError('送达操作无效')
    key, payload = message['key'], message['payload']
    if payload.get('kind') not in ('visual', 'update_report', 'manager_help'):
        raise ValueError('未知送达种类')
    root = Path(args.state_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(key.encode()).hexdigest()
    path = root / (name + '.json')
    with (root / (name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(path.read_text()) if path.exists() else {'key': key, 'payload': payload}
        if record['payload'] != payload:
            raise ValueError('同一送达号不能更换内容')
        if record.get('external'):
            return {'state': 'sent', 'external': record['external']}
        file = None
        if payload['kind'] == 'update_report':
            file = Path(payload['artifact']).expanduser().resolve(strict=True)
            if file.suffix != '.html' or hashlib.sha256(file.read_bytes()).hexdigest() != payload['sha256']:
                raise ValueError('报告文件或快照散列不匹配')
            marker = file.name
        else:
            marker = '[送达 ' + key + ']'
        if message['action'] == 'reconcile' or record.get('started') or payload.get('existing_external'):
            if not record.get('started') and not payload.get('existing_external'):
                return {'state': 'absent'}
            value = command(args, ['im', '+chat-messages-list', '--chat-id', args.chat_id,
                          '--start', record.get('started', payload.get('existing_started', '2026-01-01T00:00:00+00:00')),
                          '--page-all', '--page-limit', '100', '--no-reactions'])
            data = value.get('data', {})
            matches = [m for m in data.get('messages', []) if m.get('chat_id') == args.chat_id
                       and m.get('sender', {}).get('sender_type') in ('app', 'bot') and not m.get('deleted')
                       and m.get('sender', {}).get('id') == args.app_id
                       and ((m.get('msg_type') == 'file' and attachment_name(m.get('content')) == marker)
                            if file else m.get('msg_type') == 'text' and marker in str(m.get('content')))]
            if payload.get('existing_external') not in (None, '', 'unknown'):
                matches = [m for m in matches if m['message_id'] == payload['existing_external']]
            if len(matches) == 1:
                record['external'] = matches[0]['message_id']
                record['reconciled'] = True
                save(path, record)
                return {'state': 'sent', 'external': record['external']}
            # 无匹配也可能尚未可见；不能把未找到当作没发送。
            return {'state': 'unknown'}
        record['started'] = datetime.now(timezone.utc).isoformat()
        record['send_calls'] = 1
        save(path, record)
        argv = ['im', '+messages-send', '--user-id', args.recipient,
                '--idempotency-key', payload.get('idempotency_key', name[:48])]
        if file:
            value = command(args, [*argv, '--file', file.name], cwd=file.parent)
        else:
            value = command(args, [*argv, '--text', payload['text'] + '\n' + marker])
        external = value.get('data', {}).get('message_id')
        if not external:
            return {'state': 'unknown'}
        record['external'] = external
        save(path, record)
        return {'state': 'sent', 'external': external}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lark', default='lark-cli')
    parser.add_argument('--profile', required=True)
    parser.add_argument('--recipient', required=True)
    parser.add_argument('--chat-id', required=True)
    parser.add_argument('--app-id', required=True)
    parser.add_argument('--state-dir', required=True)
    args = parser.parse_args()
    try:
        result = deliver(args, json.load(sys.stdin))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result = {'state': 'unknown'}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
