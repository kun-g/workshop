"""做梦生成器和等价判定：配置了 `memory.dreamer.script` 就用真实执行者模型，
否则用本地确定性实现（`check.sh` 里的测试和未配置的部署都不连模型）。

`check.sh` 的测试只走本地确定性实现；真实路径的 `workshop dream` 用配置里的脚本。
"""
import json
import re
import subprocess

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


class ModelDreamer(Dreamer):
    """经配置的脚本起真实执行者模型；超时、非零退出或格式不对都抛异常（fail-closed）。"""

    def __init__(self, script, argv, env, timeout):
        self.script = script
        self.argv = list(argv)
        self.env = dict(env)
        self.timeout = timeout
        super().__init__(self._propose, self._same)

    def _call(self, action, payload):
        import os
        import sys
        command = [sys.executable, self.script, action] if self.script.endswith(".py") else [self.script, action]
        env = {**os.environ, **self.env}
        result = subprocess.run([*command, *self.argv], input=json.dumps(payload, ensure_ascii=False),
                                capture_output=True, text=True, timeout=self.timeout, env=env)
        if result.returncode:
            raise ValueError(f"做梦模型退出 {result.returncode}：{result.stderr[-500:]}")
        return json.loads(result.stdout)

    def _propose(self, facts, cursor):
        return self._call("propose", {"facts": facts, "cursor": cursor}).get("proposals") or []

    def _same(self, left, right):
        value = self._call("same", {"left": left, "right": right}).get("same")
        if type(value) is not bool:
            raise ValueError("等价判定没有返回布尔 same")
        return value


def make_dreamer(target=None, settings=None):
    dreamer = (settings or {}).get("dreamer") if isinstance(settings, dict) else None
    if isinstance(dreamer, dict) and dreamer.get("script"):
        return ModelDreamer(dreamer["script"], dreamer.get("argv") or [], dreamer.get("env") or {},
                            float(dreamer.get("timeout") or 120))
    return Dreamer(generate, equivalent)
