"""「已经告诉过用户的」只跟送达走。

读介入点发件表（只读连接），只认已送达（delivered）或已折叠进已送达摘要（folded）的消息；
pending_digest、failed、unknown 都算没送到，不新增。同一条消息只记一次（说法里带消息号，
同一条消息的说法哈希不变，唯一索引让重跑不重复）。
"""
import json

DELIVERED = ("delivered", "folded")


def delivered_rows(outbox_port):
    rows = outbox_port.messages()
    if not isinstance(rows, list):
        raise ValueError("发件只读端口须返回列表")
    for row in rows:
        if not isinstance(row, dict) or not {"status", "item", "items"} <= set(row):
            raise ValueError("发件行缺 status、item 或 items")
    return [row for row in rows if row["status"] in DELIVERED and row["item"]]


def already(store):
    """已经记过的送达：说法里带消息号，回读比对。"""
    return {row["claim"] for row in store.effective(kind="reported")}


def key(row):
    return f'已送达 {row["item"]}：{json.dumps(row["items"], ensure_ascii=False)}'


def record(store, outbox_port, *, scope, batch, watermark):
    """把新送达的、还没记过的消息写进 reported；水位是这一刻的事实序号，作来源凭据。"""
    seen = already(store)
    written = []
    for row in delivered_rows(outbox_port):
        claim = key(row)
        if claim in seen:
            continue
        try:
            entry = store.append(kind="reported", scope=scope, claim=claim,
                                 summary=json.dumps(row["items"], ensure_ascii=False),
                                 source_fact_seqs=[watermark], batch=batch, item=row["item"])
        except ValueError:
            continue
        seen.add(claim)
        written.append(entry)
    return written
