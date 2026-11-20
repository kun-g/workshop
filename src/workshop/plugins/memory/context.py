"""每轮开头按作用域加载固定部分：用户指示、未结案进度、笔记索引、近期告知。

预算按条数和字符数两道上限；超限按 用户指示 > 未结案进度 > 近期告知 截断，用户指示不能被挤出。
笔记只给索引（id + 摘要），正文由查看命令按 id 读。已结案的进度不进开头加载。
"""
import json

DEFAULTS = {"max_entries": 40, "max_chars": 6000, "reported_recent": 10}


def limits(config):
    budgets = (config or {}).get("budgets") if isinstance(config, dict) else None
    budgets = budgets if isinstance(budgets, dict) else {}
    result = {}
    for key, value in DEFAULTS.items():
        given = budgets.get(key, value)
        if type(given) is not int or given < 0:
            raise ValueError(f"记忆预算 {key} 必须是非负整数")
        result[key] = given
    return result


def size(entry):
    return len(entry.get("claim", "")) + len(entry.get("summary", ""))


def trim(entries, budget, used):
    """按顺序取，直到条数或字符数会用超；前面一段用掉的预算，后面就没有了。"""
    kept = []
    for entry in entries:
        if budget["max_entries"] - used["entries"] <= 0:
            break
        cost = size(entry)
        if used["chars"] + cost > budget["max_chars"]:
            if kept:
                break
            # 第一条就超预算：截短它，让人知道已被截断。
            entry = {**entry, "claim": entry["claim"][:max(0, budget["max_chars"] - used["chars"])]}
            cost = size(entry)
        kept.append(entry)
        used["entries"] += 1
        used["chars"] += cost
    return kept


def instructions(store, scope):
    rows = store.effective(kind="instruction")
    return [{k: row[k] for k in ("id", "scope", "claim", "created_at")} for row in rows
            if row["scope"] in (scope, "*")]


def progress(store, scope):
    """一件活只取最后一条进度；已结案的不进开头加载。"""
    rows = [row for row in store.latest_progress()
            if row["scope"] in (scope, "*") and row.get("state") != "已结案"]
    return [{k: row[k] for k in ("id", "item", "scope", "state", "claim", "created_at")}
            for row in sorted(rows, key=lambda row: row["seq"])]


def notes(store, scope):
    """笔记只给索引：id 和摘要，正文只经 `workshop memory show <id>` 读。"""
    rows = store.effective(kind="note", scope=scope)
    return [{"id": row["id"], "summary": row["summary"] or row["claim"][:80]} for row in rows]


def reported(store, scope, budget):
    rows = store.effective(kind="reported", scope=scope)
    rows.sort(key=lambda row: row["seq"], reverse=True)
    return [{k: row[k] for k in ("id", "scope", "claim", "summary", "created_at")}
            for row in rows[:budget["reported_recent"]]]


def context(store, scope, config):
    """本轮作用域要带的记忆；条数和字符预算严格不超，用户指示永远完整保留。"""
    budget = limits(config)
    used = {"entries": 0, "chars": 0}
    items = instructions(store, scope)
    kept_instructions = trim(items, budget, used)
    progress_items = progress(store, scope)
    kept_progress = trim(progress_items, budget, used)
    reported_items = reported(store, scope, budget)
    kept_reported = trim(reported_items, budget, used)
    return {"scope": scope, "budget": budget, "instructions": kept_instructions,
            "progress": kept_progress, "notes": notes(store, scope), "reported": kept_reported,
            "truncated": {"instructions": len(kept_instructions) < len(items),
                          "progress": len(kept_progress) < len(progress_items),
                          "reported": len(kept_reported) < len(reported_items)}}


def counts(value):
    """这一节的条数和字符数，供断言和 evidence。"""
    return {key: {"entries": len(value[key]),
                  "chars": sum(len(e.get("claim", "")) + len(e.get("summary", "")) for e in value[key])}
            for key in ("instructions", "progress", "notes", "reported")}


def brief_section(memory):
    """派活说明里的「记忆」一节，和 R33 的批次一节同位置；内容给机器读的 JSON。"""
    return {"scope": memory["scope"], "instructions": memory["instructions"], "progress": memory["progress"],
            "notes": memory["notes"], "reported": memory["reported"], "truncated": memory["truncated"],
            "budget": memory["budget"]}


def summary(value):
    return json.dumps(brief_section(value), ensure_ascii=False)
