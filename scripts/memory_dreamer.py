"""用执行者模型做做梦和等价判定；失败关闭。

由记忆插件按规则 `memory.dreamer` 启动，命令形如：
  python memory_dreamer.py propose <模型命令…>   # stdin: {"facts": [...]}，stdout: {"proposals": [...]}
  python memory_dreamer.py same <模型命令…>      # stdin: {"left","right"}，stdout: {"same": bool}

`<模型命令…>` 后面会接 `-p <提示词>`；环境变量由规则 `memory.dreamer.env` 给出（例如网关）。
任何超时、非零退出或格式不对都抛异常，插件按拒绝处理。
"""
import json
import subprocess
import sys

PROPOSE = """你是车间的做梦器。阅读下面事实表新增（JSON），只提炼值得长期记住的说法。
输出一个 JSON 数组，每项是 {{"kind": "reported"|"note"|"progress", "scope": "*",
"claim": "一条说法", "summary": "短摘要", "source_fact_seqs": [用到的 seq]}}。
来源事实必须是输入里真实出现的 seq；没有值得记的就输出 []。只输出 JSON，不要解释。

新增事实：
{facts}
"""

SAME = """这两个说法在意思上是不是同一个？左边：{left!r}；右边：{right!r}。
只输出 JSON {{"same": true}} 或 {{"same": false}}，不要解释。"""


def extract(text, kind):
    start = text.find("{") if kind is dict else text.find("[")
    if start < 0:
        raise ValueError("模型没有给出 JSON")
    decoder = json.JSONDecoder()
    value, _ = decoder.raw_decode(text[start:])
    return value


def call(prompt, model_command):
    if not model_command:
        raise ValueError("没有配置做梦模型命令")
    result = subprocess.run([*model_command, "-p", prompt], capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise ValueError(f"做梦模型退出 {result.returncode}：{result.stderr[-500:]}")
    return result.stdout


def main():
    action, model_command = sys.argv[1], sys.argv[2:]
    payload = json.load(sys.stdin)
    if action == "propose":
        facts = json.dumps(payload.get("facts") or [], ensure_ascii=False)[:20000]
        value = extract(call(PROPOSE.format(facts=facts), model_command), list)
        if not isinstance(value, list):
            raise ValueError("做梦模型没有返回候选数组")
        print(json.dumps({"proposals": value}, ensure_ascii=False))
    elif action == "same":
        value = extract(call(SAME.format(left=payload.get("left", ""), right=payload.get("right", "")),
                             model_command), dict)
        same = value.get("same")
        if type(same) is not bool:
            raise ValueError("等价判定没有返回布尔 same")
        print(json.dumps({"same": same}))
    else:
        raise ValueError(f"不认识的做梦动作：{action}")


if __name__ == "__main__":
    main()
