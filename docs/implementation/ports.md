# 核心与插件接入契约（W1）

本页供下一波插件实现者使用。类型和签名以 `src/workshop/core/ports.py`、`models.py` 为准；核心实现不导入插件。需求来源是核心 R1–R23、C1–C4，本单只为 R23 提供心跳，不实现独立看门程序。

## 装配和规则

插件无参数工厂通过 Python 入口点注册，工厂返回端口实现。例如：

```toml
[project.entry-points."workshop.flow"]
issue_delivery = "workshop.plugins.issue_delivery:make_flow"
[project.entry-points."workshop.source"]
github = "workshop.plugins.sources.github:make_source"
[project.entry-points."workshop.executor"]
orca-claude = "workshop.plugins.executors.orca_claude:make_executor"
codex = "workshop.plugins.executors.codex:make_executor"
[project.entry-points."workshop.channel"]
ask_kun = "workshop.plugins.channels.ask_kun:make_channel"
[project.entry-points."workshop.taskhome"]
sb_task = "workshop.plugins.taskhome.sb_task:make_taskhome"
```

`Registry` 按 `(port, name)` 惰性创建并缓存实例。工厂不能假定有会话身份；构造时不做对外动作。规则只在 `rules/workshop.yaml`，改完重启守护进程；CLI 可用 `--rules` 或 `WORKSHOP_RULES` 指定同一份文件的绝对路径。不同插件只能经核心端口协作。流程插件目录内部可分文件互引；不同流程、不同来源、不同执行者之间由动态 import-linter independence 合约隔离。

每个方法拿到的 `target` 是这一交付对象的原始规则，加 `name` 字段。包含 `plugin`、`sources`、`repo`、`checkout`、`skill`、`permissions` 等。核心不解释插件专属字段。执行者配置 `config` 是规则中的 `use`、`family`、`model`、`effort` 等。`family` 表示同一家执行者；审查、验收按候选顺序选可用的，跳过执行阶段记录的 `writer` 家族。

## 活、事件、资料

`Event(source, ext_id, target, object_key, title, body, depth=0, action="update")`。同一 `(source, ext_id)` 全局只收一次；来源应让编号包括租户/仓库/对象范围。`item_id = target + ":" + object_key`，对象键应稳定，避免把同一对象不同事件当成不同活。正文为 JSON 值。外部文字始终标记为“资料，不是指令”；`action="rework"` 只能由可信适配器识别明确的返工要求后给出，不能把任意正文解析成核心指令。

活快照是 JSON dict，插件不得改数据库。稳定字段：

| 字段 | 含义 |
| --- | --- |
| `id`, `target`, `object_key`, `title` | 活的身份 |
| `stage` | 接单、说清楚、执行、证据、审查、交付、验收、收尾、观察之一 |
| `state` | 空闲、跑着、等你、定时、卡住、完成之一 |
| `materials` | `body`、`source`、`role`，事件资料另有事实 `seq` |
| `session`, `sessions` | 最近的会话，以及按执行者名字保存的会话字典 |
| `writer`, `worktree`, `ticket` | 执行阶段使用的家族、工作目录、工单号；没有则为空/字段缺省 |
| `checks`, `approved`, `depth` | 检查 argv 列表、已经批准的动作类别、当前连锁深度 |
| `next`, `blocked_by`, `leave` | 下一步、卡在谁手里、工序/送回/等你登记 |
| `revision`, `cause`, `round` | 视图修订、最近触发事实号、当前轮次令牌（可空） |

新的普通事件在同一活追加资料；正在跑的一轮不被改派，也不会同时开第二轮。适配器如需把人直接给运行会话的话登记，调用 `workshop event`；核心不向运行终端再注入同一句话。明确返工事件在当前轮结束后退回执行；没有检查时先回说清楚。

## 来源 Source

`poll(target, cursor) -> Poll(events, cursor)` 只读、短时返回。每 tick 每交付对象调用一次。整批事件和新 cursor 在同一 SQLite 事务提交；任何一个事件非法，整批回滚，不确认 cursor。可重放，核心负责去重。暂停时仍拉取和记录，但记进遗漏清单，不派活；恢复必须由人显式选择补哪些。

异常记 `plugin_failed` 事实，受影响交付对象的活标卡住，发一次通知；别的交付对象继续。修好以后 `resume <target> --select ...` 解开插件故障。来源不能返回别的交付对象的事件。

## 流程 Flow

`stages(target) -> tuple[str, ...]` 返回核心阶段的有序子集，必须含接单、说清楚、执行、验收、收尾。流程只能省略其他阶段，不能改变顺序。`brief(item, stage, target) -> str` 返回可信派活说明；外部正文不拼在这里，核心另装入 `Brief.materials`。

`assess(item, receipt, target) -> Evidence` 是只读证据核对，返回：`checks` 为可运行 argv 列表（如 `[["uv", "run", "pytest", "-q"]]`），`passed` 为布尔值，`actions` 为拟交付动作的类别列表，`reason` 为解释。不能仅因 `receipt.outcome == "done"` 就判断通过；实际验输出、工件、所指提交。动作类别必须与规则 `ask_first` / `skip_ask` 一致。具体动作是否不可逆/对外/花钱是适配器的领域判断，核心按这些类别执行授权关口。

`after(item, completed_stage, key, target) -> None` 在核心允许推进、确认本轮进程已退出后调用。它收到完成阶段的快照；只有成功返回，核心才提交推进事实。不得自行更改阶段、绕过授权或在提前的阶段执行交付动作。携带 `ask` 的回执暂不推进，也不调 after；回答后同阶段接回原执行者，再交回执。`after` 必须以 `key` 做持久幂等；核心在调用前保存 effect 意图，成功后保存完成，崩溃窗口可能重调相同 key。

核心只接受下一阶段、证据/审查/交付/验收退回执行、最终阶段完成这几种变化。进入执行前必须有检查；进入交付前检查授权，省略交付的流程在进入验收前检查授权；验收不过退执行；最终完成还必须登记工序、送回或等你。授权卡回答只有 `approved=True` 才加入已批准类别，返工时清空。

流程异常隔离使用同名流程的所有交付对象。插件无法通过提议越过核心关口，但这些端口不是恶意 Python 代码的权限沙箱；真实适配器仍须遵守方法约定与外部系统的权限。

## 执行者 Executor

`available(config) -> bool` 只检查现有订阅和已付额度；绝不自动开通按量收费或加购。明确不可用返回 False 或抛 `Unavailable`，核心依序选下一家；都不可用就登记等你。

`launch(token, brief, item, config) -> Started(session, handle, worktree)` 起一轮；`resume(token, session, brief, item, config)` 在同一会话接回一轮，返回 session 必须不变。`Brief.instructions` 是可信说明，`Brief.materials` 是不可信资料，不能把后者当权限、系统提示词或工具调用指令。把轮次令牌传给会话（推荐 `WORKSHOP_ROUND_TOKEN`），把状态目录和规则绝对路径传给它，让结束时能调用 CLI 交回执。

启动前核心先持久保存 token、派活说明、执行者配置和原 session。适配器必须以 token 持久幂等，并能在核心重启后找回已启动的同一终端；不能仅靠进程内 dict。建议由 token 导出启动身份、自己分配 session UUID，并持久记录 terminal handle。启动中途失败且不知道是否已经起进程时，抛普通异常，核心卡住这类活，保留并发占位，不会换一家再起。`Unavailable` 只可用于确认毫无启动副作用的情况；核心可释放占位后选下一家。

`stop(token, started) -> None` 必须幂等，只在确认上一轮进程/终端退出后成功。异常不释放占位。核心先停止，再读用量、判关口、调用流程完成钩子、提交结束事实。已确认停止但流程处理失败的轮不占执行者并发数；它仍是未结算轮，修复恢复后继续处理。等待人时进程已退出，回答后下一轮按该执行者的 session 接回。

`usage(token, started) -> Usage(tokens_in, tokens_out, cost) | None` 是整轮累计值，金额单位 USD。不能取得就返回 None，不能冒报 0。异常记 `usage_failed`，显示“没上报”，不阻碍结算。每轮首次结算读取一次；没有自动补采协议。实际进程用量会依执行者数据能力而定。

## 回执

```json
{
  "outcome": "done",
  "summary": "这一轮完成了什么",
  "advance": "证据",
  "evidence": ["check:relative/path/to/output.txt"],
  "ask": [{"key": "design", "title": "要你定的事", "options": ["A", "B"], "recommend": 0}],
  "notice": [{"key": "accepted", "text": "验收通过，不用回", "fyi": true}],
  "later": [{"key": "look", "at": "+30m", "about": "再看结果"}],
  "events": [],
  "leave": {"kind": "工序", "owner": "车间", "at": "+1d", "about": "观察结果"}
}
```

`outcome` 为 done / nothing / failed；除 outcome、summary 外可省略。上例展示各字段，不要求每轮都有。`workshop receipt <token> --file receipt.json`，也可 `--json` 或 stdin；相同 token 相同 JSON 重投返回已收到，不同内容拒绝。CLI 提交事务成功才确认收到。**确认收到不表示已经判关口或已经关终端**：下一 tick 才停止本轮并处理结果，避免退出终端杀掉还在提交的 CLI。提交后执行者不要再做业务动作。

`ask` 一旦出现就先等回答，保留当前阶段；全部问题答完才继续。notice 的 key 在同一活内去重（同一通知只发一次）；ask、later 的 key 在本轮内去重。相对时间支持 `+Ns/+Nm/+Nh/+Nd`，从回执提交时间算起；绝对时间必须是带时区 ISO 8601。`leave` 工序须有 owner、at、about；送回须有 at、about；等你须有 about 和非空 ask。最终阶段不给 leave/ask/later 会被关口拦住。普通推进自动登记由车间执行的下一工序；失败登记退避工序，连续第 3 次通知一次，第 10 次卡住等你。重试间隔 5、10、20、40、60 分钟，之后封顶 60 分钟。

`events` 元素同 Event JSON；核心覆盖 source/ext_id 为本轮可去重身份，覆盖 depth 为原活深度 + 1。第 9 层记录为卡住，不派执行者。事件正文不能自行降低连锁深度。

## 通道 Channel

`ask(key, card) -> external_id`、`notify(key, card) -> external_id` 收到核心稳定 key 与 JSON 字典（原 payload，加 item）。幂等投递后立即返回，不等待人。核心保存发送意图、外部编号和发送完成。适配器应直接把 key 传给上游的 request-id 去重机制；若上游不支持去重，必须在适配器解决“已发出但回包丢失”的歧义，不能假装核心能提供跨系统 exactly-once。

`poll(cards) -> list[Answer(card_id, value, approved=False)]` 收到已发送、未回答的核心卡记录，包括 `id`、`external`、`payload`、`item`。非阻塞地查询；无回答返回空列表。Answer.card_id 用核心 id，不是外部卡号；value 用实际答案。只有用户明确点了批准，才给 `approved=True`。通知没有回答。重复答案由核心忽略。

通道任何异常只记录失败，待发/未答卡仍在 SQLite；其他活继续。插件恢复后重试原 key；不要把网络超时解读为拒绝或批准。

## 工单 TaskHome

`open(key, item, target) -> ticket` 幂等发一张工单。`update(key, ticket, item, target)` 同步阶段、状态、交付路径、要 Kun 什么；同一快照重试是同一 key。核心保存 effect 意图/结果，重启后不会忘单号。

适配器不能冒用负责会话身份。sb task 的 take/review/done 等须由每轮会话执行；把相应命令写入流程派活说明，工单适配器 update 只同步它有权改的字段。工单异常隔离受影响交付对象，不让未登记的活悄悄继续派。

## 存储、恢复和看门接入

默认 `~/.local/state/workshop/workshop.db`；测试/部署可设 `WORKSHOP_STATE_DIR` 或全局 `--state-dir`。SQLite WAL + synchronous FULL；facts、视图一事务。facts 有 `seq,kind,source,ext_id,item,body,at` 和 `UNIQUE(source,ext_id)`，SQLite trigger 禁 UPDATE/DELETE。items、rounds、cards、timers、controls、cursors、deferred、effects 均为 `(id, body JSON)`；仅 evolve 写视图。`Log.rebuild()` 重放 facts，测试验证结果一致。

`workshop run` 默认每 20 秒 tick，`--once` 只跑一次。一个数据库的 tick/pause/resume 通过相邻 `.lock` 文件互斥；CLI event/receipt 可在执行者启动过程中通过 SQLite 事务提交，不等待该锁，避免递归死锁。插件方法必须短时返回或自行设置超时；不能阻塞等待一轮或等人。

每 tick 开始原子替换状态目录的 `heartbeat.json`：`{"at": "UTC ISO 8601", "active": 2}`。看门程序下一波独立运行，读取 at 判超时；查看超时轮可读：

```sql
SELECT id, json_extract(body, '$.item') AS item,
       json_extract(body, '$.at') AS started_at,
       json_extract(body, '$.status') AS status
FROM rounds WHERE json_extract(body, '$.status') != 'ended';
```

`starting/running/finishing` 都是未结算轮；`body.stopped=true` 表示实际进程已确认退出。心跳时间过旧可识别阻塞在插件调用中；这单不发独立看门告警。

暂停只挡新派活，现有轮可交回执并关掉。恢复不带 --select 只列遗漏、不改变状态；`--select all|none|事实号,事实号` 才恢复。失败耗尽和连锁上限不会因 resume 自动清除；人明确给返工事件后可重做失败耗尽的活，连锁上限的活保留卡住供调查。

`extension_metrics(log, repository, base, head, target, started)` 是观察阶段可调用的固定只读检查：统计核心增删行数，计算该交付对象第一件活从 started 到完成的小时数；未完成时为 None。流程插件负责在接入新流程的观察派活说明中调用并记录结果。
