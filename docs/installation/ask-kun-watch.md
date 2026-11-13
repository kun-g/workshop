# ask-kun 通道与独立看门

涉及核心需求 R3（交付批准）、R12（等待不占会话）、R14（故障只通知一次）、R16（唯一规则）、R21（通道失败保留）、R22（隔离）、R23（自身故障告警），以及 C3（新写实现）、C4（不新增费用）。未改变需求或核心。

## 通道

入口点 `workshop.channel:ask_kun`，规则已有 `channel: ask_kun`。`WORKSHOP_ASK_KUN` 可覆盖可执行文件；默认是派活约定的 agent-bin/ask-kun，启动时解析成绝对路径。默认账本在 `WORKSHOP_STATE_DIR/ask-kun-channel`（未设时为 `~/.local/state/workshop/ask-kun-channel`）。直接用 CLI 的 `--state-dir` 时也应设置同值的 `WORKSHOP_STATE_DIR`，因为核心的无参数插件工厂没有状态目录参数；生成的 LaunchAgent 已设置。

Question 的 subject、decision、choices、recommend、reason、kind 原样映射。context 前 100 字进入短背景，前 6000 字进入 details-file；完整内容保留在核心事实和通道账本中。通道进一步要求选项 key 是英文字母、数字、下划线或连字符，符合 ask-kun CLI 的约束。

每次 ask 都带 `--no-wait --answer-format key`，不设默认答案。`wait Qn --timeout 0` **不能带** `--answer-format`（当前 CLI 不支持）；wait 读取 ask 时保存在上游的格式。只把 kind=approval 且 key=yes 的回答标为 approved，推荐项和按钮文字都不算批准。核心自己的关口也独立只认 yes。

发送前持久保存 key、卡片、唯一 `车间:<哈希>` 发问方。丢回包后用 `list --all` 的 JSON 按 from+subject 查原卡，还检查 answer_format=key；无记录、多个匹配、非 JSON、不完整列表都返回不确定，不重发。没有 request-id 的 ask 无法保证自动解除所有歧义，持续不确定由核心升级通知。通知正文走 stdin，携带核心 key 作为 request-id，原样重放对账；notify 成功没有 stdout，所以将该 request-id 作为外部关联编号。

poll 退出码 3 保留待答；0 返回选项 key；6 报投递故障，保留原关口，交由核心报告（Answer 端口没有作废状态，不能伪造拒绝答案）。取消前查原卡，已答/作废视为终态；其他状态调用 cancel，重启后可重放。

## 看门

独立进程：`python -m workshop.watch --state-dir <目录>`。每 300 秒运行一次，成功心跳超过 180 秒或 starting/running/finishing 的轮超过 7200 秒未结算即告警；stopped=true 的 finishing 仍检查。心跳不存在/损坏、数据库不存在/损坏也告警。仅用只读 SQLite 连接读核心 rounds 表，不 import 其他插件。

`watch.json` 保存故障段、首次发现时间、稳定 request-id、是否已通知，文件锁防重入。跨天持续故障不重发；观察到健康后再次异常，或 last_ok_at 前进（证明中间成功过）后再次过期，建立新故障段。通知失败用 attention.py 写等你清单，推飞书=false，避免坏通道上再循环。任一通道确认接收便视为已报告；二者均失败，程序非零退出，下次使用相同 id 和正文重试。

等你清单行不会自动删，恢复后的既有行由 Kun 按清单机制处理。状态目录损坏到不能写看门账本时仅非零退出并写 stderr；本机完全关机时看门也不能运行，这是第一版本机看门的边界。

## launchd 安装交接

仓库提供 `scripts/launch_agents.py` 生成两份 plist，**生成不安装、不启动**。使用主检出的 `.venv/bin/python`，避免 launchd 中 uv 临时同步或下载。守护进程 KeepAlive、看门 StartInterval=300，各自 stdout/stderr 在状态目录。

工作区安装器当前没有通用 plist 模板端口；未知 label 会误运行 stuck_watch，因此不能只加 manifest。派活方需在 `~/workspace` 应用本仓库的 `docs/installation/workspace-setup.patch`，并给 manifest 的 launch_agents 添加：

```json
"com.kun.workshop": {"keep_alive": true},
"com.kun.workshop-watch": {"interval_seconds": 300, "run_at_load": true}
```

顺序：

1. 合并本 PR，主检出更新到合并后的提交，在主检出运行 `uv sync --frozen`；等待其他插件到齐、有效规则已配好。
2. 在工作区仓库运行 `git apply --check <车间仓库>/docs/installation/workspace-setup.patch`，再 `git apply`；添加上面的 manifest 条目。
3. `python3 -I ~/workspace/runtime/tools/workspace_setup/install.py` 预览；安装器会先建状态/日志目录。
4. 经派活方决定后运行同一命令加 `--apply` 安装，查看 `launchctl print gui/$(id -u)/com.kun.workshop` 和 `com.kun.workshop-watch`。此步骤会启动真实车间，不能当离线测试。

只看 plist：

```sh
python3 scripts/launch_agents.py com.kun.workshop
python3 scripts/launch_agents.py com.kun.workshop-watch
```

## 派活方手动实测（会真发飞书，此次未执行）

从车间主检出运行，构造只有一个过期心跳故障的临时目录，不改真实核心状态：

```sh
workshop_watch_trial=$(mktemp -d /tmp/workshop-watch.XXXXXX)
export WORKSHOP_WATCH_TRIAL="$workshop_watch_trial"
uv run python - <<'PY'
import json, os, sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
p = Path(os.environ['WORKSHOP_WATCH_TRIAL'])
p.joinpath('heartbeat.json').write_text(json.dumps({'last_ok_at': (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat()}))
with sqlite3.connect(p / 'workshop.db') as db:
    db.execute('CREATE TABLE rounds(id TEXT PRIMARY KEY, body TEXT NOT NULL)')
PY
uv run python -m workshop.watch --state-dir "$workshop_watch_trial"
uv run python -m workshop.watch --state-dir "$workshop_watch_trial"
cat "$workshop_watch_trial/watch.json"
```

验收：第一次一条飞书通知，第二次零新通知；watch.json 的 reported=true、via=ask-kun、两次 request-id 相同。若 via=attention，飞书实测未通过，只能算降级记录成功。
