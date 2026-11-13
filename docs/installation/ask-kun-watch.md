# ask-kun 通道与独立看门

涉及核心需求 R3（交付批准）、R12（等待不占会话）、R14（故障只通知一次）、R16（唯一规则）、R21（通道失败保留）、R22（隔离）、R23（自身故障告警），以及 C3（新写实现）、C4（不新增费用）。未改变需求；修改轮 1、2 经授权给核心增加通道终态处理及心跳投递失败字段。

## 通道

入口点 `workshop.channel:ask_kun`，规则已有 `channel: ask_kun`。`WORKSHOP_ASK_KUN` 可覆盖可执行文件；默认是派活约定的 agent-bin/ask-kun，启动时解析成绝对路径。默认账本在 `WORKSHOP_STATE_DIR/ask-kun-channel`（未设时为 `~/.local/state/workshop/ask-kun-channel`）。直接用 CLI 的 `--state-dir` 时也应设置同值的 `WORKSHOP_STATE_DIR`，因为核心的无参数插件工厂没有状态目录参数；生成的 LaunchAgent 已设置。

Question 的单行字段先将回车、换行、制表符替换为空格。短背景固定为「完整依据见下方详情」；原始卡片字段及完整背景放 details-file（最多 6000 字），完整原文仍保存在核心事实和通道账本。带内部编号、路径、提交号的短字段改成指向详情的说明，标题保留去掉工程标识后的活名，禁用/重复按钮和重复选后结果改写成对应编号的选项说明，不改变 key；工程写法正则与 2026-11-13 的 ask-kun 原表达式一致，标注来源，不跨仓库 import。选项 key 只接受英文字母、数字、下划线或连字符。

只有 ask/notify 退出码 2 且 stderr 带「尚未发送」或 `usage: ask-kun` 才抛 ChannelRejected。找不到命令、无执行权限、可识别的 uv 启动错误记「没起来」，对账返回 absent 后原键重试；本地准备失败也不冒充上游拒收。list/wait/cancel 一律只报查询或运行错误。超时、断线和丢回包仍保持待对账。

问题拒收仍算等待：跑着不被覆盖，轮结束自然等你；卡住保留状态，只改恢复后的快照；完成不重开。通知拒收不动活。明确放行会消费等待标记，即使当时仍跑着或卡住，也不留残标记。固定拒收通知自身失败会记入心跳投递失败信号，由独立看门兜底。

每次 ask 都带 `--no-wait --answer-format key`，不设默认答案。`wait Qn --timeout 0` **不能带** `--answer-format`（当前 CLI 不支持）；wait 读取 ask 时保存在上游的格式。只把 kind=approval 且 key=yes 的回答标为 approved，推荐项和按钮文字都不算批准。核心自己的关口也独立只认 yes。

发送前持久保存 key、卡片、唯一 `车间:<哈希>` 发问方。丢回包后用 `list --all` 的 JSON 按 from+subject 查原卡，还检查 answer_format=key；无记录、多个匹配、非 JSON、不完整列表都返回不确定，不重发。没有 request-id 的 ask 无法保证自动解除所有歧义，持续不确定由核心升级通知。通知正文走 stdin，request-id 加状态目录与数据库文件身份的哈希前缀，后接核心 key（超长则哈希），原样重放对账；notify 成功没有 stdout，external 保存实际传给 --request-id 的稳定键；通知回复归件后续另做。

poll 每批先查一次 list --all，只对已答或已作废卡调用 wait，普通待答卡不再重复起外部进程。逐卡失败记 stderr，其余答案正常返回，全部失败才抛错。退出码 3 待答，0 返回 key，6 返回 cancelled=true。完整列表缺号视为已清理；wait 退出 4 且 stderr 带 HTTP 403 时重查完整列表，仍有编号就保留为错误，确实没有才作废。非完整 JSON、分页、截断或查询故障不能推断作废。核心将作废当无答案的终态，不冒充批准或拒绝。

## 看门

独立进程：`/usr/bin/python3 -I <仓库>/src/workshop/watch/__init__.py --state-dir <目录>`，只用标准库，不需要车间虚拟环境或安装 workshop。每 60 秒运行一次，成功心跳超过 180 秒或 starting/running/finishing 的轮超过自身 deadline 加 60 秒结算余量时记录异常；旧轮无 deadline 时取数据库规则快照 round_timeout_seconds（核心缺省 7200 秒）。stopped=true 的 finishing 仍检查，坏 JSON 只跳过该行并记 stderr，不误报整库不可读。

上次看门运行在 90 秒内（机器持续醒着），本次心跳已超过 180 秒就立即告警，不要求前次也过期。第一次运行或刚唤醒后上次运行已超过 90 秒，仅保存观察，给守护进程一次恢复机会。60 秒调度下，从停机到检查命中最多 180+60=240 秒；加此前实测的上游约 23 秒发送延迟，约 263 秒（4.4 分钟）。本地只读查询最多等待 5 秒，通知调用最多 30 秒，通知通道不可用则转等你清单；网络排队或飞书断开时不能保证手机收到，须分别报告飞书和兜底结果。

核心心跳另含 delivery_failed_count 和 delivery_failed_since，持续超过 180 秒投递失败也告警/兜底；包括拒收通知自己发不出去的情况。四类兜底文字（心跳、未结算轮、数据库、通道投递）都含 attention 认可的文件指向及明确请求，真实脚本回归验证不会降成待分。

`watch.json` 保存故障段、首次发现时间、稳定 request-id、是否已通知，文件锁防重入。request-id 显式带状态目录哈希，同一次故障跨天不重发，恢复后再坏建新故障段。stdout 只在故障状态变化时输出摘要。

通知失败用 attention.py 写等你清单，文字含具体文件和「需要你检查」请求，推飞书=true，避免被降成待分。清单同一故障只写一次，但下次继续以同一 request-id 重试 notify，直到成功才 reported=true。二者均失败时非零退出，下次仍复用原 id 和正文。

等你清单行不会自动删，恢复后的既有行由 Kun 按清单机制处理。状态目录损坏到不能写看门账本时仅非零退出并写 stderr；本机完全关机时看门也不能运行，这是第一版本机看门的边界。

## launchd 安装交接

仓库提供 `scripts/launch_agents.py` 生成两份 plist，**生成不安装、不启动**。守护进程使用主检出的 `.venv/bin/python`，看门直接用系统 Python 加 `-I` 执行独立文件。守护进程 KeepAlive、看门 StartInterval=60，各自 stdout/stderr 在状态目录。

工作区安装器当前没有通用 plist 模板端口；未知 label 会误运行 stuck_watch，因此不能只加 manifest。派活方需在 `~/workspace` 应用本仓库的 `docs/installation/workspace-setup.patch`，并给 manifest 的 launch_agents 添加：

```json
"com.kun.workshop": {"keep_alive": true},
"com.kun.workshop-watch": {"interval_seconds": 60, "run_at_load": true}
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
/usr/bin/python3 - <<'PY'
import json, os, sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
p = Path(os.environ['WORKSHOP_WATCH_TRIAL'])
p.joinpath('heartbeat.json').write_text(json.dumps({'last_ok_at': (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat()}))
with sqlite3.connect(p / 'workshop.db') as db:
    db.execute('CREATE TABLE rounds(id TEXT PRIMARY KEY, body TEXT NOT NULL)')
PY
/usr/bin/python3 -I src/workshop/watch/__init__.py --state-dir "$workshop_watch_trial"
sleep 60
/usr/bin/python3 -I src/workshop/watch/__init__.py --state-dir "$workshop_watch_trial"
/usr/bin/python3 -I src/workshop/watch/__init__.py --state-dir "$workshop_watch_trial"
cat "$workshop_watch_trial/watch.json"
```

验收：首次运行仅观察；相隔 60 秒再运行，上次运行在 90 秒内，发一条飞书通知，第三次零新通知；watch.json 的 reported=true、via=ask-kun、两次 request-id 相同。若 via=attention，飞书实测未通过，只能算降级记录成功。


已安装服务的两项实测（派活方执行，会停止真实守护进程并发飞书）：

1. 停机：KeepAlive 会重启被 kill 的进程，因此用 bootout 只卸载守护进程，保留看门。记录开始时间，五分钟内应收到一条告警；重复看门无新消息。随后恢复加载，确认心跳更新且 watch.json 不再有该故障后再做下一项。

```sh
date -Iseconds
launchctl bootout "gui/$UID/com.kun.workshop"
# 观察五分钟内飞书告警，以及 ~/.local/state/workshop/watch.json
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.kun.workshop.plist"
```

2. 卡死：暂停现有进程，验证进程还在但成功心跳不再更新同样会告警；测完恢复进程。

```sh
workshop_daemon_pid=$(launchctl print "gui/$UID/com.kun.workshop" | awk '/pid =/{print $3; exit}')
test -n "$workshop_daemon_pid"
date -Iseconds
kill -STOP "$workshop_daemon_pid"
# 观察五分钟内一条新故障通知；此前停机故障须已恢复
kill -CONT "$workshop_daemon_pid"
```

若通知通道自身断开，五分钟指标只能验到 attention 兜底；需恢复通道，确认同一 request-id 的 notify 重试成功，不能把兜底成功写成飞书成功。
