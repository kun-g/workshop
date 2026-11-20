"""记忆库：独立于事实表，只追加；纠正行同表带权重。

一行是一条说法（claim）的某个版本；同一说法、同一来源集合只写一次（唯一索引）。
纠正也是一行，weight=-1，corrects 指向要下线的条目，corrected_seq 是纠正时的来源水位，
phrase 是等价判定用的说法。原条目不改也不删：下线由纠正行派生出来。
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone

KINDS = ("instruction", "note", "reported", "progress")
CORRECTION = "correction"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    claim TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    claim_hash TEXT NOT NULL,
    source_fact_seqs TEXT NOT NULL CHECK (json_type(source_fact_seqs)='array'),
    batch TEXT NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1,
    corrects TEXT,
    corrected_seq INTEGER,
    phrase TEXT,
    item TEXT,
    state TEXT,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS memory_no_update BEFORE UPDATE ON memory
    BEGIN SELECT RAISE(ABORT, 'memory is append only'); END;
CREATE TRIGGER IF NOT EXISTS memory_no_delete BEFORE DELETE ON memory
    BEGIN SELECT RAISE(ABORT, 'memory is append only'); END;
CREATE UNIQUE INDEX IF NOT EXISTS memory_entry ON memory(kind, scope, claim_hash, source_fact_seqs)
    WHERE kind IN ('instruction','note','reported');
CREATE UNIQUE INDEX IF NOT EXISTS memory_progress ON memory(item, state, claim_hash)
    WHERE kind='progress';
CREATE UNIQUE INDEX IF NOT EXISTS memory_correction ON memory(corrects, corrected_seq)
    WHERE kind='correction';
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def text_hash(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def identifier(prefix, *parts):
    return prefix + text_hash(*parts)[:16]


def connect(path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript(_SCHEMA)
    return db


def encode_seqs(seqs):
    if not isinstance(seqs, list) or not seqs:
        raise ValueError("来源事实不能为空")
    values = []
    for value in seqs:
        if type(value) is not int or value <= 0:
            raise ValueError("来源事实序号须为正整数")
        values.append(value)
    return json.dumps(sorted(set(values)))


def decode_seqs(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return []
    return [v for v in value if type(v) is int] if isinstance(value, list) else []


class Store:
    def __init__(self, path, *, now=now):
        self.path = Path(path).expanduser()
        self.db = connect(self.path)
        self.now = now

    @classmethod
    def readonly(cls, path):
        store = cls.__new__(cls)
        store.path = Path(path).expanduser()
        store.now = now
        store.db = sqlite3.connect(store.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        store.db.row_factory = sqlite3.Row
        return store

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def append(self, *, kind, scope, claim, summary="", source_fact_seqs, batch, item=None, state=None,
               created_at=None):
        if kind not in KINDS:
            raise ValueError(f"记忆种类只能是 {'、'.join(KINDS)}")
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError("作用域不能为空")
        if not isinstance(claim, str) or not claim.strip():
            raise ValueError("说法不能为空")
        if not isinstance(batch, str) or not batch.strip():
            raise ValueError("生成批次不能为空")
        seqs = encode_seqs(source_fact_seqs)
        digest = text_hash(claim)
        entry_id = identifier("m-", kind, scope, digest, seqs)
        createdAt = created_at or self.now()
        with self.transaction():
            cursor = self.db.execute(
                "INSERT INTO memory(id,kind,scope,claim,summary,claim_hash,source_fact_seqs,batch,item,state,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (entry_id, kind, scope, claim, summary or "", digest, seqs, batch, item, state, createdAt))
            if not cursor.rowcount:
                # 同一说法的同一来源版本已经写过：重跑不重复；回读既有行。
                return self.entry(entry_id)
        return self.entry(entry_id)

    def append_correction(self, entry_id, phrase, watermark):
        if not isinstance(phrase, str) or not phrase.strip():
            raise ValueError("纠正要带一个等价判定用的说法")
        if type(watermark) is not int or watermark < 0:
            raise ValueError("纠正水位必须是非负整数")
        entry = self.entry(entry_id)
        if entry is None or entry["kind"] == CORRECTION:
            raise ValueError(f"没有这条记忆：{entry_id}")
        correction_id = identifier("c-", entry_id, watermark)
        with self.transaction():
            self.db.execute(
                "INSERT INTO memory(id,kind,scope,claim,summary,claim_hash,source_fact_seqs,batch,weight,"
                "corrects,corrected_seq,phrase,item,state,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (correction_id, CORRECTION, entry["scope"], phrase, "", text_hash(phrase),
                 json.dumps(entry["source_fact_seqs"]), f"correct:{watermark}", -1, entry_id, watermark, phrase,
                 entry.get("item"), entry.get("state"), self.now()))
        return self.correction(correction_id)

    def entry(self, entry_id):
        row = self.db.execute("SELECT * FROM memory WHERE id=? ORDER BY seq DESC LIMIT 1", (entry_id,)).fetchone()
        return self._row(row)

    def correction(self, correction_id):
        row = self.db.execute("SELECT * FROM memory WHERE id=?", (correction_id,)).fetchone()
        return self._row(row)

    @staticmethod
    def _row(row):
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            row = dict(row)
        result = dict(row)
        result["source_fact_seqs"] = decode_seqs(result.get("source_fact_seqs"))
        return result

    def corrected(self):
        """被纠正下线的条目：纠正行 corrects 指向的 id 和纠正水位。"""
        rows = self.db.execute("SELECT corrects, corrected_seq FROM memory WHERE kind='correction'").fetchall()
        return {row[0]: row[1] for row in rows if row[0]}

    def corrections(self):
        return [self._row(row) for row in self.db.execute(
            "SELECT * FROM memory WHERE kind='correction' ORDER BY seq")]

    def effective(self, kind=None, scope=None):
        hidden = set(self.corrected())
        clauses, values = [], []
        if kind is not None:
            clauses.append("kind=?")
            values.append(kind)
        if scope is not None:
            clauses.append("scope IN (?, '*')")
            values.append(scope)
        query = "SELECT * FROM memory" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY seq"
        rows = [self._row(row) for row in self.db.execute(query, values)]
        return [row for row in rows if row and row["kind"] != CORRECTION and row["id"] not in hidden
                and not row.get("corrects")]

    def latest_progress(self):
        rows = self.db.execute(
            "SELECT * FROM memory WHERE kind='progress' AND id NOT IN "
            "(SELECT corrects FROM memory WHERE kind='correction' AND corrects IS NOT NULL) ORDER BY seq").fetchall()
        latest = {}
        for row in rows:
            entry = self._row(row)
            latest[entry.get("item") or entry["id"]] = entry
        return list(latest.values())

    def progress_signature(self, item):
        row = self.db.execute(
            "SELECT claim_hash,state FROM memory WHERE kind='progress' AND item=? ORDER BY seq DESC LIMIT 1",
            (item,)).fetchone()
        return (row[0], row[1]) if row else None

    def max_seq(self):
        return self.db.execute("SELECT coalesce(max(seq),0) FROM memory").fetchone()[0]

    def batches(self):
        rows = self.db.execute("SELECT batch, count(*) FROM memory GROUP BY batch ORDER BY min(seq)").fetchall()
        return [{"batch": row[0], "entries": row[1]} for row in rows]

    def meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        with self.transaction():
            self.db.execute("INSERT INTO meta(key,value) VALUES (?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def open_store(path):
    store = Store(path)
    store.db.row_factory = sqlite3.Row
    return store


def online_backup(database, destination):
    """把 memory.db 连同 WAL 里的内容在线备份成一份独立文件；源库保持打开也能用。"""
    destination = Path(destination).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = sqlite3.connect(Path(database).expanduser().resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise ValueError(f"备份完整性检查不通过：{result}")
        finally:
            target.close()
        os.replace(temporary, destination)
    finally:
        source.close()
        if temporary.exists():
            temporary.unlink()
    return destination


def restore_backup(backup, database):
    """整体恢复：先删原库连同 -wal/-shm，再把备份文件原子放回。"""
    backup = Path(backup).expanduser()
    database = Path(database).expanduser()
    if not backup.is_file():
        raise ValueError(f"没有这份备份：{backup}")
    check = sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        raise ValueError(f"备份完整性检查不通过：{result}")
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=database.parent)
    os.close(fd)
    try:
        temporary = Path(temporary)
        temporary.write_bytes(backup.read_bytes())
        for suffix in ("", "-wal", "-shm"):
            Path(str(database) + suffix).unlink(missing_ok=True)
        os.replace(temporary, database)
    finally:
        if temporary.exists():
            temporary.unlink()
    return database
