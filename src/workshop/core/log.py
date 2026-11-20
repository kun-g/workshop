"""SQLite 只追加事实日志；提交成功才确认收到。"""
from contextlib import contextmanager
import json
import re
from pathlib import Path
import sqlite3
import uuid
from .models import stamp, utcnow
from .view import TABLES, evolve
from .records import RoundHistory, brief_value, compact, compact_history, defer_items, materialize


# 部分索引与查询必须使用同一个表达式，不能退回扫描整份正文。
OPEN_ITEMS = ("(json_extract(body, '$.state') NOT IN ('完成','放弃') "
              "OR json_extract(body, '$.pending_rework') = 1 OR json_extract(body, '$.program') IS NOT NULL "
              "OR json_extract(body, '$.after_wait') IS NOT NULL OR json_extract(body, '$.pending_advance') IS NOT NULL)")

RELEASE_ITEMS = ("(json_extract(body, '$.state') IN ('完成', '放弃') AND coalesce(json_extract(body, '$.workdir'), '') != '' "
                 "AND (coalesce(json_extract(body, '$.workdir_released'), '') = '' "
                 "OR json_extract(body, '$.workdir_released') = 0))")


class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.read_only = False
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
            -- 核心 R37：来源换了键的规矩以后，旧事实按新键认回的别名；纯派生、可从事实表重算，不改事实。
            -- ext_id 为空串的一行记已经算到哪个事实序号。
            CREATE TABLE IF NOT EXISTS fact_aliases (
                source TEXT NOT NULL, ext_id TEXT NOT NULL, seq INTEGER NOT NULL,
                PRIMARY KEY(source, ext_id));
        """)
        for table in TABLES:
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {table}(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.depth = 0
        self.upgrade()
        with self.transaction():
            for table, field in (("effects", "done"), ("timers", "fired")):
                self.db.execute(f"CREATE INDEX IF NOT EXISTS {table}_pending ON {table}((json_extract(body, '$.{field}') IS NOT 1)) "
                                f"WHERE (json_extract(body, '$.{field}') IS NOT 1) = 1")
            self.db.execute("CREATE INDEX IF NOT EXISTS rounds_ended ON rounds(json_extract(body,'$.ended'))")
            self.db.execute(f"CREATE INDEX IF NOT EXISTS items_open ON items({OPEN_ITEMS}) WHERE {OPEN_ITEMS} = 1")
            self.db.execute(f"CREATE INDEX IF NOT EXISTS items_release ON items({RELEASE_ITEMS}) WHERE {RELEASE_ITEMS} = 1")
            self.db.execute("CREATE INDEX IF NOT EXISTS items_home ON items(json_extract(body, '$.state'), id, "
                            "json_extract(body, '$.ticket'), json_extract(body, '$.revision'))")
            self.db.execute("CREATE INDEX IF NOT EXISTS items_program ON items((json_extract(body, '$.program') IS NOT NULL)) "
                            "WHERE (json_extract(body, '$.program') IS NOT NULL) = 1")

    @classmethod
    def readonly(cls, path):
        """只读打开已有的当前版本；升级只能由写入方启动时完成。"""
        log = cls.__new__(cls)
        log.path = Path(path).expanduser().resolve()
        log.depth = 0
        log.read_only = True
        log.db = sqlite3.connect(log.path.as_uri() + '?mode=ro', uri=True,
                                 timeout=0, isolation_level=None)
        log.db.row_factory = sqlite3.Row
        try:
            if log.db.execute('PRAGMA user_version').fetchone()[0] < 1:
                raise ValueError('状态库版本较旧，请先启动守护进程升级后再查询')
        except BaseException:
            log.close()
            raise
        return log

    @contextmanager
    def read_transaction(self):
        """一次查询共用一个快照；嵌套查询沿用调用方已有的事务。"""
        outer = not self.db.in_transaction
        if outer:
            self.db.execute('BEGIN')
        try:
            yield self
        finally:
            if outer:
                self.db.rollback()

    def state_since(self, item):
        row = self.db.execute("SELECT at FROM facts WHERE item=? AND kind='item_changed' "
                              "AND json_extract(body,'$.state') IS NOT NULL ORDER BY seq DESC LIMIT 1",
                              (item,)).fetchone()
        return row[0] if row else None

    def item_count(self):
        return self.db.execute('SELECT count(*) FROM items').fetchone()[0]

    def fact_at(self, seq):
        row = self.db.execute("SELECT at FROM facts WHERE seq=?", (seq,)).fetchone()
        return row[0] if row else None

    def event_at(self, seq):
        row = self.db.execute("SELECT body FROM facts WHERE seq=? AND kind='event'", (seq,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def selected_body(fields):
        if not fields or any(not re.fullmatch(r'[a-z_][a-z_0-9]*', field) for field in fields):
            raise ValueError('字段须为非空的简单名称列表')
        return 'json_object(' + ','.join(f"'{field}',json_extract(body,'$.{field}')" for field in fields) + ')'

    def selected_facts(self, kinds, fields, *, after=-1, item=None):
        """按种类、序号（可再按活）只取指定字段，避免加载无关的大块正文。"""
        columns = self.selected_body(fields)
        marks = ','.join('?' for _ in kinds)
        where, values = ('', ()) if item is None else (' AND item=?', (item,))
        rows = self.db.execute(f'SELECT seq,kind,item,at,{columns} AS body FROM facts '
                               f'WHERE seq>? AND kind IN ({marks}){where} ORDER BY seq', (after, *kinds, *values))
        return [{**dict(row), 'body': {k: v for k, v in json.loads(row['body']).items() if v is not None}}
                for row in rows]

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
        if self.read_only:
            raise sqlite3.OperationalError("attempt to write a readonly database")
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
            if ext_id and self.db.execute("SELECT 1 FROM fact_aliases WHERE source=? AND ext_id=?",
                                          (source, ext_id)).fetchone():
                return False  # 旧事实已按旧键记过这一次变化（核心 R37）
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

    def open_items(self):
        """没结束的活（含还有后续动作的）：走部分索引，不解析已结束的。"""
        return [json.loads(row[0]) for row in self.db.execute(f"SELECT body FROM items WHERE {OPEN_ITEMS} = 1 ORDER BY rowid")]

    def all(self, table, *, where=None, unless=None, fields=None):
        """where=(字段, 值) 只要该字段等于值的行，unless=(字段, 值) 筛掉等于值的行；在库里先筛，
        不在 Python 里逐行解析（例如已结束的轮、别的活的轮）。"""
        if table not in TABLES:
            raise ValueError("未知视图")
        clauses, params = [], []
        for (field, value), test in ((where or (None, None), "IS"), (unless or (None, None), "IS NOT")):
            if field:
                if ((table == "rounds" and field in ("item", "status"))
                        or (table, field) in (("effects", "done"), ("timers", "fired"), ("items", "state"))):
                    # SQLite 表达式索引要求路径是同一个字面量，参数化路径不会匹配索引。
                    expression = f"json_extract(body, '$.{field}')"
                    if field == "status" and test == "IS NOT" and value == "ended":
                        clauses.append(f"({expression} IS NOT 'ended') = 1")
                    elif table in ("effects", "timers") and test == "IS NOT" and value is True:
                        clauses.append(f"({expression} IS NOT 1) = 1")
                    else:
                        clauses.append(f"{expression} {test} ?")
                        params.append(value)
                else:
                    clauses.append(f"json_extract(body, ?) {test} ?")
                    params += [f"$.{field}", value]
        columns = self.selected_body(fields) if fields is not None else "body"
        query = f"SELECT {columns} FROM {table}" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY rowid"
        return [json.loads(row[0]) for row in self.db.execute(query, params)]

    def open_items(self):
        return [json.loads(row[0]) for row in self.db.execute(
            f"SELECT body FROM items WHERE {OPEN_ITEMS} = 1 ORDER BY rowid")]

    def ended_home_items(self):
        # 结束的活只在工单尚未同步当前修订时读取正文；覆盖索引不读历史资料。
        rows = self.db.execute("SELECT id, json_extract(body, '$.ticket'), json_extract(body, '$.revision') "
                               "FROM items INDEXED BY items_home WHERE json_extract(body, '$.state') IN ('完成','放弃')")
        ids = []
        for key, ticket, revision in rows:
            effect = self.get("effects", f"ticket:{key}:{revision}" if ticket else f"ticket:{key}")
            if not (effect or {}).get("done") or not ticket:
                ids.append(key)
        return [self.get("items", key) for key in ids]

    def release_items(self):
        return [json.loads(row[0]) for row in self.db.execute(
            f"SELECT body FROM items WHERE {RELEASE_ITEMS} = 1 ORDER BY rowid")]

    def kept_workdirs(self):
        """收尾时执行者说不能收、且目录还在的工作目录；原因记在活上，项目经理要看见。

        目录已经不在（手工删了、存量命令收回了）就不用再登记事项。
        """
        rows = [json.loads(row[0]) for row in self.db.execute(
            "SELECT body FROM items WHERE json_extract(body, '$.state') IN ('完成', '放弃') "
            "AND coalesce(json_extract(body, '$.workdir'), '') != '' "
            "AND coalesce(json_extract(body, '$.workdir_released'), '') LIKE '留着：%' ORDER BY rowid")]
        return [item for item in rows if Path(item['workdir']).exists()]

    def program_items(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT body FROM items WHERE (json_extract(body, '$.program') IS NOT NULL) = 1 ORDER BY rowid")]

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

    def aliased_through(self, source):
        row = self.db.execute("SELECT seq FROM fact_aliases WHERE source=? AND ext_id=''", (source,)).fetchone()
        return row[0] if row else 0

    def unaliased(self, source, after, kinds):
        """序号大于 after 的这个来源的事实，供算别名；只取种类、键和正文。"""
        marks = ",".join("?" for _ in kinds)
        return self.db.execute(f"SELECT seq,ext_id,body FROM facts WHERE seq>? AND source=? AND kind IN ({marks}) "
                               "ORDER BY seq", (after, source, *kinds))

    def alias(self, source, rows, through):
        """登记别名（已有的不覆盖），并记下算到的事实序号。"""
        with self.transaction():
            self.db.executemany("INSERT OR IGNORE INTO fact_aliases(source,ext_id,seq) VALUES (?,?,?)",
                                [(source, ext_id, seq) for ext_id, seq in rows])
            self.db.execute("INSERT INTO fact_aliases(source,ext_id,seq) VALUES (?,'',?) "
                            "ON CONFLICT(source,ext_id) DO UPDATE SET seq=excluded.seq", (source, through))

    def facts(self, item=None):
        query = "SELECT * FROM facts" + (" WHERE item=?" if item else "") + " ORDER BY seq"
        rows = self.db.execute(query, (item,) if item else ())
        return [{**dict(row), "body": json.loads(row["body"])} for row in rows]

    def rebuild(self):
        with self.transaction():
            evolve(self.db, {"kind": "reset", "body": {}, "item": None})
            for fact in self.facts():
                evolve(self.db, fact)
