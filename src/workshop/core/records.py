"""大载荷和后台读取引用；轮次视图只留定位信息，事实仍可完整重放。"""
from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from .models import Brief


def compact(row, seq):
    if "brief" not in row:
        return row
    row = dict(row)
    brief = row.pop("brief")
    return {**row, "brief_ref": seq, "workdir": brief.get("workdir")}


def compact_history(item, first, through):
    # 核心关口只需知道到过哪些阶段；完整结论仍在事实中，插件读取时再展开。
    return {**item, "history": [{"stage": stage} for stage in dict.fromkeys(
        entry.get("stage") for entry in item.get("history", []))],
        "history_ref": {"first": first, "through": through}}


def brief_value(db, row):
    if "brief" in row:  # 旧事实和调用方尚未投影的记录。
        return row["brief"]
    found = db.execute("SELECT json_extract(body, '$.brief') FROM facts WHERE seq=? AND kind='round_reserved'",
                       (row["brief_ref"],)).fetchone()
    if found is None or found[0] is None:
        raise ValueError(f'派活说明不存在：{row["brief_ref"]}')
    return json.loads(found[0])


def connect(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


@dataclass(frozen=True)
class StoredBrief:
    database: str
    key: int

    def load(self):
        with closing(connect(self.database)) as db:
            return Brief(**brief_value(db, {"brief_ref": self.key}))


@dataclass(frozen=True)
class RoundHistory:
    database: str
    item: str
    through: int
    count: int
    version: int

    def load(self):
        # 在后台展开，保留挑选端口的完整 list[dict]，不截断历史或改变顺序。
        with closing(connect(self.database)) as db:
            rows = db.execute("SELECT body FROM rounds WHERE json_extract(body, '$.item') IS ? "
                              "AND rowid<=? ORDER BY rowid", (self.item, self.through))
            result = []
            for (raw,) in rows:
                row = json.loads(raw)
                row["brief"] = brief_value(db, row)
                result.append(row)
            return result


@dataclass(frozen=True)
class ResultHistory:
    database: str
    item: str
    first: int
    through: int

    def load(self):
        with closing(connect(self.database)) as db:
            return [json.loads(raw) for (raw,) in db.execute(
                "SELECT body FROM facts WHERE item=? AND kind='round_result' AND seq>=? AND seq<=? ORDER BY seq",
                (self.item, self.first, self.through))]


def defer_items(args, database):
    return [{**arg, "history": ResultHistory(str(database), arg["id"], **arg["history_ref"])}
            if isinstance(arg, dict) and "id" in arg and "history_ref" in arg else arg for arg in args]


def materialize(args):
    def value(arg):
        if isinstance(arg, (StoredBrief, RoundHistory, ResultHistory)):
            return arg.load()
        if isinstance(arg, dict) and isinstance(arg.get("history"), ResultHistory):
            return {**arg, "history": arg["history"].load()}
        return arg
    return [value(arg) for arg in args]
