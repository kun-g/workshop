"""做梦：独立的派生过程，只写 memory.db，不改只追加的事实表。

持久游标 `dream_cursor`：事实表无新增时重跑不产生重复条目（没有新事实直接返回）；
每个候选单独一事务，中途失败重跑不丢已写入的、也不重复写同一候选（唯一索引 + 游标只在新事实读完并写完所有候选后推进）。
"""
from .store import Store

DREAM_KINDS = ("round_result", "program_result")


def validate(proposal, facts_port):
    """校验来源事实存在；返回拒绝原因，空串表示可以写入。"""
    if not isinstance(proposal.get("claim"), str) or not proposal["claim"].strip():
        return "候选缺说法"
    seqs = proposal.get("source_fact_seqs")
    if not isinstance(seqs, list) or not seqs or not all(type(v) is int and v > 0 for v in seqs):
        return "候选来源事实无效"
    if not all(facts_port.exists(seqs).get(seq) for seq in seqs):
        return "候选引用了不存在的事实"
    return ""


def blocked(proposal, corrections, same):
    """同时满足两条才拒绝：来源全部不晚于纠正水位，且等价判定认为同一个说法。"""
    top = max(proposal["source_fact_seqs"])
    for correction in corrections:
        watermark = correction.get("corrected_seq") or 0
        if top > watermark:
            continue  # 引入了晚于纠正水位的新事实：放行。
        phrase = correction.get("phrase") or ""
        if same(proposal.get("claim", ""), phrase):
            return correction
    return None


def run(store, facts_port, dreamer, *, batch, limit=500):
    """扫到的事实写进记忆；返回写入和被拒的候选，供原样输出。"""
    cursor = int(store.meta("dream_cursor") or 0)
    entries = facts_port.since(cursor, limit)
    if not entries:
        return {"batch": batch, "cursor": cursor, "written": [], "rejected": [], "next_cursor": cursor,
                "note": "没有新事实，重跑不产生条目"}
    proposals = dreamer.propose(entries, cursor)
    corrections = store.corrections()
    written, rejected = [], []
    for proposal in proposals:
        reason = validate(proposal, facts_port)
        if reason:
            rejected.append({"claim": proposal.get("claim"), "reason": reason})
            continue
        correction = blocked(proposal, corrections, dreamer.same)
        if correction is not None:
            rejected.append({"claim": proposal.get("claim"),
                             "reason": f"和纠正过的 {correction['corrects']} 是同一个说法"})
            continue
        try:
            entry = store.append(kind=proposal.get("kind") or "reported",
                                 scope=proposal.get("scope") or "*",
                                 claim=proposal["claim"], summary=proposal.get("summary", ""),
                                 source_fact_seqs=proposal["source_fact_seqs"], batch=batch,
                                 item=proposal.get("item"), state=proposal.get("state"))
        except ValueError as exc:
            rejected.append({"claim": proposal.get("claim"), "reason": str(exc)})
            continue
        written.append(entry)
    next_cursor = max(cursor, *[entry["seq"] for entry in entries])
    # 写完再推进游标；重跑时来源重叠，唯一索引让已写入的不重复。
    store.set_meta("dream_cursor", next_cursor)
    return {"batch": batch, "cursor": cursor, "written": written, "rejected": rejected,
            "next_cursor": next_cursor}
