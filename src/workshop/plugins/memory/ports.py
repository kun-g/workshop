"""只读端口：核心状态库（事实）和介入点发件表；都以 sqlite mode=ro 打开。

实现在本插件里；核心只定义 `FactsRead`、`OutboxRead` 两个只读端口接口。
这里不 import 介入点插件，也不 import 其他插件。
"""
from contextlib import closing
from pathlib import Path
import json
import sqlite3


def _open(path):
    path = Path(path).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"库不存在：{path}")
    return closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5))


def facts_since(database, seq, limit=500):
    """只读回收事实表里序号大于 seq 的事实，按序号升序。"""
    if type(seq) is not int or seq < 0:
        raise ValueError("事实游标必须是非负整数")
    if type(limit) is not int or limit <= 0:
        raise ValueError("单次读取条数必须为正整数")
    with _open(database) as db:
        rows = db.execute("SELECT seq,kind,source,ext_id,item,body,at FROM facts WHERE seq>? ORDER BY seq LIMIT ?",
                          (seq, limit)).fetchall()
    return [{"seq": row[0], "kind": row[1], "source": row[2], "ext_id": row[3], "item": row[4],
             "body": json.loads(row[5]), "at": row[6]} for row in rows]


def facts_max(database):
    with _open(database) as db:
        return db.execute("SELECT coalesce(max(seq),0) FROM facts").fetchone()[0]


def facts_exist(database, seqs):
    if not isinstance(seqs, list) or not all(type(v) is int for v in seqs):
        raise ValueError("事实序号须为整数列表")
    if not seqs:
        return {}
    with _open(database) as db:
        found = {row[0] for row in db.execute(
            "SELECT seq FROM facts WHERE seq IN (" + ",".join("?" * len(seqs)) + ")", seqs)}
    return {seq: seq in found for seq in seqs}


def outbox_rows(database):
    """只读介入点发件表；字段名 `status`、`item`、`items` 就是契约。"""
    path = Path(database).expanduser().resolve()
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT status, item, items FROM message_outbox ORDER BY rowid").fetchall()
    return [{"status": row["status"], "item": row["item"],
             "items": json.loads(row["items"]) if row["items"] is not None else []} for row in rows]


def round_record(database, token):
    """只读轮次视图；令牌就是轮次号。"""
    if not isinstance(token, str) or not token:
        return None
    with _open(database) as db:
        row = db.execute("SELECT body FROM rounds WHERE id=?", (token,)).fetchone()
    return json.loads(row[0]) if row else None


def round_status(database, token):
    record = round_record(database, token)
    return record.get("status") if record else None


class Facts:
    def __init__(self, database):
        self.database = str(database)

    def since(self, seq, limit=500):
        return facts_since(self.database, seq, limit)

    def exists(self, seqs):
        return facts_exist(self.database, seqs)

    def max_seq(self):
        return facts_max(self.database)


class Outbox:
    def __init__(self, database):
        self.database = str(database)

    def messages(self):
        return outbox_rows(self.database)

    def write_probe(self):
        """经同一只读连接尝试写；必须报错，不吞异常。"""
        return outbox_write_probe(self.database)


def outbox_write_probe(database):
    """经同一只读连接尝试写发件表：必须抛 sqlite3.OperationalError；不吞异常。"""
    path = Path(database).expanduser().resolve()
    uri = path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        db.execute("UPDATE message_outbox SET items=items")
    return False
