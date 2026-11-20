"""默认做梦生成器和等价判定：本地、确定性，不连模型。

生成器把事实表新增里「kind in round_result/program_result」的事实各派一条候选（说法是
事实的一句摘要），来源带这个事实序号；判定按说法规范化后比较。
测试和真实路径都能用；要接真实模型，替换本模块的 make_dreamer 即可。
"""
import json
import re

from .dreambase import Dreamer


def normalize(text):
    value = re.sub(r"\s+", "", str(text or ""))
    return value.strip("。.;；，,、").lower()


def summarize(fact):
    body = fact.get("body") or {}
    content = body.get("summary") or body.get("reason") or body.get("stage") or fact.get("kind", "")
    return f"{fact.get('item') or fact.get('ext_id')}：{content}"[:200]


def generate(facts, cursor):
    proposals = []
    for fact in facts:
        if fact.get("kind") not in ("round_result", "program_result"):
            continue
        proposals.append({"kind": "reported", "scope": "*", "claim": summarize(fact),
                          "summary": summarize(fact), "source_fact_seqs": [fact["seq"]],
                          "item": fact.get("item")})
    return proposals


def equivalent(left, right):
    return normalize(left) == normalize(right)


def make_dreamer(target=None):
    return Dreamer(generate, equivalent)
