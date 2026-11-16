"""集群发版批次卡住、线上已只读核对无误时，标成已上线：
close_batch.py <批次的活> --production <production 提交> --verified-by '<怎么核对的>'

例：close_batch.py 'grant-console:scheduled_release:51c0379…' --production 5cd7375… \
      --verified-by 'Argo grant-console-centaladmin 5cd7375 Synced/Healthy；release.json source_commit=51c0379；rollout 完成'

先自己核对：Argo 已同步到 production 提交且健康、production release.json 的 source_commit 是这一批、rollout 完成。
脚本会备份批次文件和 cluster-release.json，原状态存到 before_pm_verify，清掉缓存的「卡住」结果，
把批次和 cluster-release.json 标成 ok。活如果是卡住，再用 unstick.py 放回原阶段；在定时等授权的，下次回查会自己收尾。
不重新同步、不发卡。缺的机制：gc#179（授权中心给自己发版会丢结果）；之前要有「核对后收批次」命令。
"""
import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from workshop.plugins.scheduled_release.io import read, write

from _common import find, open_engine

p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
p.add_argument("item")
p.add_argument("--production", required=True, help="线上 production 分支的完整提交")
p.add_argument("--verified-by", required=True)
p.add_argument("--dry-run", action="store_true")
p.add_argument("--root", type=Path, help="发版状态目录；默认取规则里的 scheduled_release_state_dir")
a = p.parse_args()
engine, rules = open_engine()
item = find(engine, rules, a.item)
target = rules.target(item["target"], item.get("flow") or "scheduled_release")
root = a.root or Path(target.get("scheduled_release_state_dir", "~/.local/state/workshop")).expanduser().resolve()
path = root / "batches" / (hashlib.sha256(item["id"].encode()).hexdigest() + ".json")
state, active = read(path), read(root / "cluster-release.json")
if not state:
    raise SystemExit(f"找不到批次文件 {path}")
if state["status"] not in ("awaiting_grant", "blocked"):
    raise SystemExit(f"没动：批次状态是 {state['status']}，只处理 awaiting_grant 和 blocked")
now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
before = {k: state.get(k) for k in ("status", "reason", "result")}
grant = (state.get("result") or {}).get("grant")
state.update(status="ok", before_pm_verify=before,
             deployed={"status": "ok", "production": a.production, "grant": grant, "verified_by": "项目经理只读核对：" + a.verified_by})
dropped = [k for k, v in state.get("effects", {}).items() if v.get("blocked")]
for k in dropped:
    del state["effects"][k]
print(json.dumps({"批次": str(path), "原状态": before["status"], "清掉缓存的卡住结果": dropped}, ensure_ascii=False, indent=1))
if a.dry_run:
    raise SystemExit("试运行，没写")
with (root / "scheduled-release.lock").open("a") as lock:
    import fcntl
    fcntl.flock(lock, fcntl.LOCK_EX)
    shutil.copy(path, path.with_name(path.name + f".before-{now}"))
    write(path, state)
    if active.get("item") == item["id"]:
        shutil.copy(root / "cluster-release.json", root / f"cluster-release.json.before-{now}")
        write(root / "cluster-release.json", {**active, "status": "ok"})
print("已标成 ok，备份后缀", f".before-{now}")
