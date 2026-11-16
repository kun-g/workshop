"""先提交再确认；操作号绑定完整请求，不覆盖已有回答和发送记录。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def required(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} 必须是非空字符串')
    return value


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        path.chmod(0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS journal (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, id TEXT NOT NULL,
                                               body TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
                BEGIN SELECT RAISE(ABORT, 'append only'); END;
            CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
                BEGIN SELECT RAISE(ABORT, 'append only'); END;
        ''')

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def get(self, key, kind=None):
        row = self.db.execute('SELECT kind,body FROM records WHERE id=?', (key,)).fetchone()
        if row is None or kind and row[0] != kind:
            raise ValueError('记录不存在或类型不匹配')
        return json.loads(row[1])

    def all(self, kind):
        return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM records WHERE kind=? ORDER BY rowid', (kind,))]

    def put(self, kind, key, body, event):
        self.db.execute('INSERT INTO records VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                        (key, kind, encode(body)))
        self.db.execute('INSERT INTO journal(kind,id,body,at) VALUES (?,?,?,?)',
                        (event, key, encode(body), datetime.now(timezone.utc).isoformat()))

    def operation(self, key, action, data, apply):
        required(key, 'operation')
        request = encode({'action': action, 'data': data})
        with self.transaction():
            old = self.db.execute('SELECT request,result FROM operations WHERE id=?', (key,)).fetchone()
            if old:
                if old[0] != request:
                    raise ValueError('同一操作号不能更改请求')
                return json.loads(old[1])
            result = apply()
            self.db.execute('INSERT INTO operations VALUES (?,?,?)', (key, request, encode(result)))
            return result

    @staticmethod
    def identity(kind, key):
        return kind + ':' + hashlib.sha256(key.encode()).hexdigest()
