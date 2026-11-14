"""SQLite 只追加事实日志；提交成功才确认收到。"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid
from .models import stamp, utcnow
from .view import TABLES, evolve
from .records import RoundHistory, brief_value, compact, compact_history, defer_items, materialize


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
        self.upgrade()

    def upgrade(self):
        """一次性迁移派生视图；旧事实不改、不删。与其他进程的迁移互斥。"""
        with self.transaction():
            if self.db.execute("PRAGMA user_version").fetchone()[0] >= 1:
                return
            self.db.execute("CREATE TABLE round_counts(id TEXT PRIMARY KEY, count INTEGER NOT NULL, version INTEGER NOT NULL)")
            self.db.execute("CREATE INDEX rounds_item ON rounds(json_extract(body, '$.item'))")
            self.db.execute("CREATE INDEX rounds_status ON rounds(json_extract(body, '$.status'))")
            self.db.execute("CREATE INDEX facts_item_kind ON facts(item,kind,seq)")
            self.db.execute("CREATE INDEX rounds_active ON rounds((json_extract(body, '$.status') IS NOT 'ended')) "
                            "WHERE (json_extract(body, '$.status') IS NOT 'ended') = 1")
            self.db.execute("CREATE INDEX rounds_active_item ON rounds(json_extract(body, '$.item')) "
                            "WHERE (json_extract(body, '$.status') IS NOT 'ended') = 1")
            self.db.execute("INSERT INTO round_counts SELECT json_extract(body, '$.item'), count(*), 0 "
                            "FROM rounds GROUP BY json_extract(body, '$.item')")
            for operation, expression, delta in (("INSERT", "NEW", 1), ("DELETE", "OLD", -1)):
                self.db.execute(f"CREATE TRIGGER rounds_{operation.lower()} AFTER {operation} ON rounds BEGIN "
                                f"INSERT INTO round_counts(id,count,version) VALUES (json_extract({expression}.body, '$.item'),{delta},1) "
                                f"ON CONFLICT(id) DO UPDATE SET count=count+{delta}, version=version+1; END")
            self.db.execute("CREATE TRIGGER rounds_update AFTER UPDATE ON rounds BEGIN "
                            "UPDATE round_counts SET version=version+1 WHERE id=json_extract(NEW.body, '$.item'); END")
            # 转换只在版本升级做一次；之后启动不扫描历史。
            origins = {token: seq for seq, token in self.db.execute(
                "SELECT seq,json_extract(body, '$.token') FROM facts WHERE kind='round_reserved'")}
            for key, raw in self.db.execute("SELECT id,body FROM rounds").fetchall():
                row = json.loads(raw)
                if "brief" in row:
                    if key not in origins:
                        raise ValueError(f"轮次缺少原始事实：{key}")
                    self.db.execute("UPDATE rounds SET body=? WHERE id=?",
                                    (json.dumps(compact(row, origins[key]), ensure_ascii=False), key))
            ranges = {key: (first, through) for key, first, through in self.db.execute(
                "SELECT item,min(seq),max(seq) FROM facts WHERE kind IN ('round_result', 'program_result') GROUP BY item")}
            for key, raw in self.db.execute("SELECT id,body FROM items").fetchall():
                item = json.loads(raw)
                if item.get("history"):
                    if key not in ranges:
                        raise ValueError(f"活缺少原始结论：{key}")
                    self.db.execute("UPDATE items SET body=? WHERE id=?",
                                    (json.dumps(compact_history(item, *ranges[key]), ensure_ascii=False), key))
            self.db.execute("PRAGMA user_version=1")

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

    def all(self, table, *, where=None, unless=None):
        """where=(字段, 值) 只要该字段等于值的行，unless=(字段, 值) 筛掉等于值的行；在库里先筛，
        不在 Python 里逐行解析（例如已结束的轮、别的活的轮）。"""
        if table not in TABLES:
            raise ValueError("未知视图")
        clauses, params = [], []
        for (field, value), test in ((where or (None, None), "IS"), (unless or (None, None), "IS NOT")):
            if field:
                if table == "rounds" and field in ("item", "status"):
                    # SQLite 表达式索引要求路径是同一个字面量，参数化路径不会匹配索引。
                    expression = f"json_extract(body, '$.{field}')"
                    if field == "status" and test == "IS NOT" and value == "ended":
                        clauses.append(f"({expression} IS NOT 'ended') = 1")
                    else:
                        clauses.append(f"{expression} {test} ?")
                        params.append(value)
                else:
                    clauses.append(f"json_extract(body, ?) {test} ?")
                    params += [f"$.{field}", value]
        query = f"SELECT body FROM {table}" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY rowid"
        return [json.loads(row[0]) for row in self.db.execute(query, params)]

    def brief(self, row):
        return brief_value(self.db, row)

    def full_item(self, item):
        return materialize(defer_items([item], self.path.resolve()))[0]

    def history(self, item):
        count = self.db.execute("SELECT count,version FROM round_counts WHERE id=?", (item,)).fetchone()
        last = self.db.execute("SELECT rowid FROM rounds WHERE json_extract(body, '$.item') IS ? "
                               "ORDER BY rowid DESC LIMIT 1", (item,)).fetchone()
        return RoundHistory(str(self.path.resolve()), item, last[0] if last else 0,
                            count[0] if count else 0, count[1] if count else 0)

    def recent_rounds(self, item):
        """倒着逐行读；调用方遇到截断条件即停，不取整份历史或说明。"""
        rows = self.db.execute("SELECT json_extract(body, '$.stage'), json_extract(body, '$.outcome'), "
                               "json_extract(body, '$.ended'), json_extract(body, '$.executor') "
                               "FROM rounds WHERE json_extract(body, '$.item') IS ? ORDER BY rowid DESC", (item,))
        try:
            for row in rows:
                yield dict(zip(("stage", "outcome", "ended", "executor"), row))
        finally:
            rows.close()

    def facts(self, item=None):
        query = "SELECT * FROM facts" + (" WHERE item=?" if item else "") + " ORDER BY seq"
        rows = self.db.execute(query, (item,) if item else ())
        return [{**dict(row), "body": json.loads(row["body"])} for row in rows]

    def rebuild(self):
        with self.transaction():
            evolve(self.db, {"kind": "reset", "body": {}, "item": None})
            for fact in self.facts():
                evolve(self.db, fact)
