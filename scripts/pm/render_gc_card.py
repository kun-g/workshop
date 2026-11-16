"""发卡前自查：用 grant-console 的线上渲染代码，把一条申请记录渲染成卡片，打出第一屏（折叠区不打）。

~/workspace/runtime/repos/grant-console/services/devops-console/.venv/bin/python render_gc_card.py <申请记录.json>

要用线上那一版的渲染代码：先把 production 对应提交的 services/devops-console 取到一个目录，
PYTHONPATH 指过去再跑。看第一屏：看不懂、有提交号或批次号、权限比要做的事宽，就先拦下，不发给 Kun。
"""
import json
import sys

from grant_console import notifications

if len(sys.argv) != 2:
    raise SystemExit(__doc__)
card = notifications.build_grant_approval_card(json.load(open(sys.argv[1])))


def visible(node):
    if isinstance(node, dict):
        if node.get("tag") == "collapsible_panel":
            return ""
        own = str(node.get("content", "")) if "content" in node else ""
        return own + "\n" + "\n".join(visible(v) for v in node.values() if isinstance(v, (list, dict)))
    if isinstance(node, list):
        return "\n".join(visible(v) for v in node)
    return ""


print(json.dumps(card.get("header", {}), ensure_ascii=False))
print("\n".join(line for line in visible(card.get("body", card)).splitlines() if line.strip()))
