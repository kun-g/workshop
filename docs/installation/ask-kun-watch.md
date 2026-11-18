# ask-kun 通道与独立看门

涉及核心需求 R3（外部动作授权）、R12（等待不占会话）、R14（故障交项目经理）、R16（唯一规则）、R21（通道失败保留）、R22（隔离）、R23（自身故障接管），以及 C3（新写实现）、C4（不新增费用）。未改变需求；修改轮 1–3 经授权给核心增加通道终态处理及心跳投递失败字段。

## 通道

入口点 `workshop.channel:ask_kun`，规则已有 `channel: ask_kun`。`WORKSHOP_ASK_KUN` 可覆盖可执行文件；默认是派活约定的 agent-bin/ask-kun，启动时解析成绝对路径。默认账本在 `WORKSHOP_STATE_DIR/ask-kun-channel`（未设时为 `~/.local/state/workshop/ask-kun-channel`）。直接用 CLI 的 `--state-dir` 时也应设置同值的 `WORKSHOP_STATE_DIR`，因为核心的无参数插件工厂没有状态目录参数；生成的 LaunchAgent 已设置。

Question 的单行字段先将回车、换行、制表符替换为空格。短背景固定为「完整依据见下方详情」；原始卡片字段及完整背景放 details-file（最多 6000 字），完整原文仍保存在核心事实和通道账本。带内部编号、路径、提交号的短字段改成指向详情的说明，标题保留去掉工程标识后的活名，禁用/重复按钮和重复选后结果改写成对应编号的选项说明，不改变 key；工程写法正则与 2026-11-13 的 ask-kun 原表达式一致，标注来源，不跨仓库 import。选项 key 只接受英文字母、数字、下划线或连字符。

适配器内容预检无法改写合规（例如非法选项 key），以及 ask/notify 退出码 2 且 stderr 带「尚未发送」或 `usage: ask-kun`，都抛 ChannelRejected；原因进 why，不重试。找不到命令、无执行权限、可识别的 uv 启动错误记「没起来」，对账返回 absent 后原键重试；「没起来」只用于运行环境问题。list/wait/cancel 一律只报查询或运行错误。超时、断线和丢回包仍保持待对账。

问题拒收仍算等待：跑着不被覆盖，轮结束自然等你；卡住保留状态，只改恢复后的快照；完成不重开。通知拒收不动活。明确放行会消费等待标记，即使当时仍跑着或卡住，也不留残标记。拒收原因记入心跳投递失败信号，交项目经理处理。

每次 ask 都带 `--no-wait --answer-format key`，不设默认答案。`wait Qn --timeout 0` **不能带** `--answer-format`（当前 CLI 不支持）；wait 读取 ask 时保存在上游的格式。只把 kind=approval 且 key=yes 的回答标为 approved，推荐项和按钮文字都不算批准。核心自己的关口也独立只认 yes。

发送前持久保存 key、卡片、唯一 `车间:<哈希>` 发问方。丢回包后用 `list --all` 的 JSON 按 from+subject 查原卡，还检查 answer_format=key；无记录、多个匹配、非 JSON、不完整列表都返回不确定，不重发。没有 request-id 的 ask 无法保证自动解除所有歧义，持续不确定由核心登记故障、交项目经理处理。通知正文走 stdin，request-id 加状态目录与数据库文件身份的哈希前缀，后接核心 key（超长则哈希），原样重放对账；notify 成功没有 stdout，external 保存实际传给 --request-id 的稳定键；通知回复归件后续另做。

poll 每批先查一次 list --all，只对已答或已作废卡调用 wait，普通待答卡不再重复起外部进程。逐卡失败记 stderr，其余答案正常返回，全部失败才抛错。退出码 3 待答，0 返回 key，6 返回 cancelled=true。ask 成功时保存发送时间，找回丢回包的原卡时保留上游时间。由于 list 按调用者会话过滤，缺号必须发送早于 29 天才视为清理；不足或恰好 29 天、没有发送时间的老记录都记查询失败，继续等。wait 退出 4 且 stderr 带 HTTP 403 时重查列表，同样只在缺号且已知发送早于 29 天时作废，不能因换了会话身份而作废新卡。非完整 JSON、分页、截断或查询故障不能推断作废。核心将作废当无答案的终态，不冒充批准或拒绝。

## 看门

看门仍用系统 Python 独立启动，读成功心跳、未结算业务轮、数据库、通道失败和连续慢拍。它不 import 插件或报表，也不直接发通知或写等你清单。检测结果与稳定事项标识先写 watch.json，再把原子输入文件交给配置的项目经理命令。命令失败保留输入及 manager_error，下一次继续；既有发版回退先执行，不被经理入口故障阻止。

业务口径在 heartbeat.json 的 business：真正运行中的业务轮与带具体活的程序调用计数；来源轮询、执行者状态查询、看门、项目经理不算。程序在两拍之间完成也按完成时间打断空闲；看门只在连续为零达到五分钟时登记空闲事项。定时、退避、正常等待不因年龄直接视为失败。

唯一规则的 managers 指定 target、service、manual、candidates；候选必须是已有 subscription 配置。核心与看门共用 managers.db、manager-jobs 和非阻塞事项锁，不同事项可并行，同事项不会重复启动。不确定的启动留在原令牌，不能当作失败另开；已确认失败才尝试下一候选。服务或送达不可用时留结论和待发，不恢复旧推送。

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

## 合并前隔离验证与上线检查

`scripts/verify_managers.py --issue <真实资料快照> --output <证据目录>` 启动隔离守护进程、系统 Python 看门、真实订阅执行者和真实介入点服务。只把送达端换成本地收件箱；不重启现役、不发飞书。FIFO 拉长受控程序调用，判五分钟用假时钟，不用 sleep。真实身份、会话、心跳、按钮响应和收件箱均输出到日志。状态目录和执行者日志保留供复核。

上线后只读运行 `scripts/verify_intervention_live.py` 的 busy-count、managers、pushes --since-release、button、service；临时 HOME 也按现役状态拥有者解析路径。按钮检查只渲染，不发送，不要求工作台部署版本。实际飞书求助卡的手机外观按本单亲眼看约定验收；工作台转发点击归 oh-my-assistant#148。

安装生成器给看门传 --manager-command JSON argv，使用项目虚拟环境的绝对 Python、绝对规则和状态目录；旧 plist 没传时从看门脚本所在仓库推导同一入口。系统 Python 只负责检测，不依赖虚拟环境导入。
