"""SQLite 只追加事实日志；提交成功才确认收到。"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid
from .models import stamp, utcnow
from .view import TABLES, evolve


class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS facts (
                seq INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                source TEXT NOT NULL, ext_id TEXT NOT NULL, item TEXT,
                body TEXT NOT NULL, at TEXT NOT NULL, UNIQUE(source, ext_id));
            CREATE TRIGGER IF NOT EXISTS facts_no_update BEFORE UPDATE ON facts
                BEGIN SELECT RAISE(ABORT, 'facts are append only'); END;
            CREATE TRIGGER IF NOT EXISTS facts_no_delete BEFORE DELETE ON facts
                BEGIN SELECT RAISE(ABORT, 'facts are append only'); END;
        """)
        for table in TABLES:
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.depth = 0

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        outer = self.depth == 0
        if outer:
            self.db.execute("BEGIN IMMEDIATE")
        self.depth += 1
        try:
            yield
            if outer:
                self.db.execute("COMMIT")
        except BaseException:
            if outer:
                self.db.execute("ROLLBACK")
            raise
        finally:
            self.depth -= 1

    def append(self, kind, body, item=None, *, source="core", ext_id=None, at=None) -> bool:
        with self.transaction():
            cursor = self.db.execute(
                "INSERT INTO facts(kind,source,ext_id,item,body,at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(source,ext_id) DO NOTHING",
                (kind, source, ext_id or str(uuid.uuid4()), item,
                 json.dumps(body, ensure_ascii=False, allow_nan=False), at or stamp(utcnow())))
            if not cursor.rowcount:
                return False
            row = dict(self.db.execute("SELECT * FROM facts WHERE seq=?", (cursor.lastrowid,)).fetchone())
            row["body"] = json.loads(row["body"])
            evolve(self.db, row)
            return True

    def get(self, table, key):
        if table not in TABLES:
            raise ValueError("未知视图")
        row = self.db.execute(f"SELECT body FROM {table} WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, table):
        if table not in TABLES:
            raise ValueError("未知视图")
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT body FROM {table} ORDER BY rowid")]

    def facts(self, item=None):
        query = "SELECT * FROM facts" + (" WHERE item=?" if item else "") + " ORDER BY seq"
        rows = self.db.execute(query, (item,) if item else ())
        return [{**dict(row), "body": json.loads(row["body"])} for row in rows]

    def rebuild(self):
        with self.transaction():
            evolve(self.db, {"kind": "reset", "body": {}, "item": None})
            for fact in self.facts():
                evolve(self.db, fact)
