"""来源换了键的规矩以后，旧事实按新键认回（核心 R37 判据二）。

来源可选实现 rekey(事件正文) → 同一次变化现在的键。别名表只从事实表算出来：删掉重算逐行一致，
不改写事实表旧行（核心 R7）；算过的序号记在表里，之后每次只补算新增的事实。
同一次变化按新键再来时，事实表照常去重：不再落一行，也不再叫醒旧活。
"""
import json
from inspect import signature

KINDS = ("event", "deferred_event")


def scoped(ext_id, key):
    """按旧事实的键的形状套新键：多流程目标的键是 [交付对象, 流程, 来源键]。"""
    try:
        value = json.loads(ext_id)
    except ValueError:
        return key
    if isinstance(value, list) and len(value) == 3 and all(isinstance(v, str) for v in value):
        return json.dumps([value[0], value[1], key], ensure_ascii=False)
    return key


def refresh(engine, sources=None):
    """来源名同事实上的 source；没装上或没有 rekey 的来源不建别名。纯本地计算，不起作业。"""
    if sources is None:
        sources = {name for rule in engine.rules.data["targets"].values() for name in rule.get("sources", [])}
    for source in sorted(sources):
        try:
            method = getattr(engine.plugins.get("source", source), "rekey", None)
        except LookupError:
            continue
        if not callable(method) or not signature(method).parameters:
            continue
        through = engine.log.aliased_through(source)
        rows, last = [], through
        for seq, ext_id, raw in engine.log.unaliased(source, through, KINDS):
            last = seq
            stored = json.loads(raw)
            for key in method(stored):
                alias = scoped(ext_id, key)
                if alias != ext_id:
                    rows.append((alias, seq))
        if last != through:
            engine.log.alias(source, rows, last)
