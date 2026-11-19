#!/usr/bin/env python3
"""#176 转换后副本回放用的送达命令：只写本地收件箱，不联网、不发飞书。

和真实送达命令同一个 stdin/stdout 协议（介入点服务 transport）：
- send：卡片照真实送达命令补上送达号，存进收件箱，回 sent；
- reconcile：本次回放发过的回 sent；没发过的回 unknown（隔离回放查不了飞书，不能说没发过，服务保留结果不明、不重发）；
- reply：Kun 的引用回复只读查询；隔离回放没有新回复，回空。
每次调用（含 reconcile、reply）都记一行 calls，供证据里逐条列出。
"""
import json
from pathlib import Path
import sqlite3
import sys


def main():
    root = Path(sys.argv[1])
    message = json.load(sys.stdin)
    action, key, payload = message['action'], message.get('key'), message.get('payload') or {}
    with sqlite3.connect(root / 'inbox.db') as db:
        db.execute('CREATE TABLE IF NOT EXISTS calls (seq INTEGER PRIMARY KEY, action TEXT, key TEXT, kind TEXT, payload TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS inbox (key TEXT PRIMARY KEY, kind TEXT, payload TEXT)')
        db.execute('INSERT INTO calls (action, key, kind, payload) VALUES (?,?,?,?)',
                   (action, key, payload.get('kind'), json.dumps(payload, ensure_ascii=False)))
        if action == 'reply':
            print('{}')
            return
        found = db.execute('SELECT 1 FROM inbox WHERE key=?', (key,)).fetchone()
        if action == 'send':
            sent = dict(payload)
            if 'card' in sent:
                from workshop.plugins.intervention.transport import with_key
                sent['card'] = with_key(sent['card'], key)
            if not found:
                db.execute('INSERT INTO inbox VALUES (?,?,?)', (key, payload.get('kind'), json.dumps(sent, ensure_ascii=False)))
            state = 'sent'
        else:
            state = 'sent' if found else 'unknown'
    print(json.dumps({'state': state, **({'external': 'local:' + str(key)} if state == 'sent' else {})}))


if __name__ == '__main__':
    main()
