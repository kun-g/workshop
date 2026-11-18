"""接入新流程时在观察阶段调用的固定检查（R19）。"""
from datetime import datetime
from pathlib import Path
import subprocess


def extension_metrics(log, repository: Path, base: str, head: str, target: str, started: str) -> dict:
    # 先解引用成提交号，防止把传入文本当成命令参数。
    commits = [subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        cwd=repository, text=True).strip() for ref in (base, head)]
    output = subprocess.check_output(
        ["git", "diff", "--numstat", *commits, "--", "src/workshop/core"], cwd=repository, text=True)
    lines = sum(int(value) for row in output.splitlines() for value in row.split("\t")[:2] if value.isdigit())
    completed = [fact["at"] for fact in log.facts() if fact["kind"] == "round_finished"
                 and fact["body"].get("state") == "完成"
                 and (log.get("items", fact["item"]) or {}).get("target") == target]
    elapsed = ((datetime.fromisoformat(min(completed)) - datetime.fromisoformat(started)).total_seconds() / 3600
               if completed else None)
    return {"core_lines": lines, "first_item_hours": elapsed}
