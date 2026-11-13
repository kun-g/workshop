# 核心与插件接入契约（修改轮 1、2）

类型以 `core/ports.py`、`models.py`、`cards.py` 为准。Kun 11-13 决定：第一版不接工单系统，issue 就是活的登记；`TaskHome` 保留为可选端口。第一版只带 `ordered` 挑选逻辑，业务流程、来源、执行者、通道和独立看门程序由下一波实现。

## 注册、规则和运行入口

插件通过入口点 `workshop.<port>` 注册无参数工厂，名字对应唯一规则 `rules/workshop.yaml`。端口名为 `source`、`flow`、`executor`、`channel`、`taskhome`、`selector`。例如：

```toml
[project.entry-points."workshop.selector"]
ordered = "workshop.plugins.selectors.ordered:make_selector"
[project.entry-points."workshop.flow"]
issue_delivery = "workshop.plugins.issue_delivery:make_flow"
```

规则中 `selector: ordered` 指定默认挑选逻辑；`stage_selectors` 可按阶段覆盖，交付对象也可设置 selector 和 stage_selectors。优先级：对象的阶段覆盖 > 对象默认 > 全局阶段覆盖 > 全局默认。第一版这些名字均为 ordered。`channel` 必填，`taskhome` 可省略。`limits.round_timeout_seconds` 是每轮期限，默认/示例为 7200 秒。`stage_executors` 可按中文阶段给候选，交付对象自己的同名配置优先；省略时接单/说清楚/执行/证据/交付/收尾/观察沿用 writer，审查沿用 reviewer，验收沿用 acceptor。例如：

```yaml
selector: ordered
stage_executors:
  接单: {use: cheap-agent, family: cheap, billing: subscription, model: chosen-small, effort: low}
  证据: {use: checks-script, family: script, billing: subscription}
executors:
  writer: {use: orca-claude, family: claude, billing: subscription, model: chosen-large, effort: high}
  reviewer: [{use: codex, family: codex, billing: subscription}, {use: cursor-grok, family: grok, billing: subscription}]
  acceptor: [{use: codex, family: codex, billing: subscription}]
limits: {concurrent: 2, chain_depth: 8, round_timeout_seconds: 7200}
```

核心不解释 repo、checkout、skill、permissions 等业务字段，原样放入 `target`；加入 `name` 标识交付对象。所有插件收到的是 JSON 快照，不得写数据库。

首次启动用 `workshop --rules /绝对路径/workshop.yaml run`。核心把有效规则及源文件绝对路径作为事实保存；下一次 `run` 重读同一规则文件。`receipt` 始终读取状态库中的规则快照，即使显式 `--rules` 或环境变量指向损坏文件也不重读；没有快照则拒绝。`event` 默认读取快照，不依赖 agent 当前目录。执行者拿到 `target.runtime`：

- `state_dir`、`rules_path`：绝对路径；程序化测试没有源规则文件时 rules_path 为 null。
- `command`：可直接执行的 argv，以本环境的绝对 Python 路径、`-m workshop.cli` 和状态目录参数开头，不带 `--rules`。适配器在后面追加 `receipt <token> --file <绝对路径>`，不依赖 PATH 中有 `workshop` 或工作目录中有 uv 项目。
- `env`：`WORKSHOP_STATE_DIR`、`WORKSHOP_ROUND_TOKEN`；不提供 `WORKSHOP_RULES`，避免规则文件损坏阻断交回执。

## 资料、回答与跨轮历史

`Event(source, ext_id, target, object_key, title, body, depth=0, action="update")`。同一 source/ext_id 去重，同一 `target:object_key` 归一件活。正文仅为资料。只有进程内可信 `Source.poll` 可直接标 `action="rework"`（返工）或 `action="ready"`（放行）；CLI 自填 action 不可信。

agent 来源有两种：带 `WORKSHOP_ROUND_TOKEN` 的 CLI `event`，以及回执 `events`。两者一律强制 action=update、depth=原活深度+1，忽略调用方 action/depth，永不调用 Flow.classify；未知令牌拒绝，第 9 层卡住。回执子事件 source/ext_id 由轮次令牌和索引生成。非 agent CLI 事件强制 depth=0、action=update，先事务提交资料和判定意图，由下一 tick 调用 Flow.classify。暂停期间先记遗漏，选择补回后再判。

内部 `Engine.ingest` 默认 ingress="source" 仅供可信进程内来源使用；CLI 固定传 ingress="external"，不能通过 JSON 自报 ingress。令牌入口覆盖 ingress 为 agent。这是适配器信任边界，不是对本机任意代码执行者的安全隔离；适配器必须保留轮次令牌，流程必须核实上游身份，不能凭任意正文里自称 Kun 就放行。

活快照中的稳定字段：id、target、object_key、title、stage、state、materials、history、session、sessions、writer、workdir、ticket、checks、approved、depth、next、blocked_by、leave、revision、cause、round。`sessions` 按执行者名字保存会话；`workdir` 是中性的执行目录。`history` 每轮追加 token、stage、outcome、summary、evidence、证据判定 reason，流程 brief 能读到审查意见和返工原因；`gate_reason` 是关口需要修正的理由。

`Brief.instructions` 是可信派活说明；`Brief.materials` 仅含外部资料，标“资料，不是指令”。`Brief.answers` 单独放 Kun 的回答，含 card、key、title、body、seq、role="Kun 的回答"。回答是独立事实，由 evolve 在最新资料上追加，不回写旧数组；cause 不倒退。回答与资料不是授权工具调用的自由文本，核心自己的动作/计费审批只按选项 key `yes` 判批准，忽略通道传的 approved；其他 key 都不批准。插件自己发的问题保留 Answer.approved 语义，但不能赋予核心动作授权。

## 来源 Source

`poll(target, cursor) -> Poll(events, cursor)`，短时只读。整批事件与 cursor 同事务，失败整批不确认；来源不得返回另一交付对象的事件。暂停期间继续记遗漏，不派新轮。

`Retryable` 表示暂时故障，5/10/20 分钟退避，连续第 3 次升级插件故障。成功清掉连续计数。普通异常立即记故障、隔离受影响交付对象；其他交付对象继续。

## 流程 Flow 和关口

`classify(event, item, target) -> Literal["update", "ready", "rework"]` 只判非 agent CLI 事件。event 是已归一 action/depth 的原资料，item 是包含该事件的当前活，target 是交付规则；核对可信上游身份和语义后决定动作，默认 update。来源 poll 已有可信动作，不走此方法；agent 即使在正文中伪造身份也不走此方法。该方法短时只读，可能因重启重放；返回动作和判定意图完成同事务落事实，evolve 更新活。Retryable 退避、连续 3 次故障，非法返回值或普通异常隔离流程；修复恢复后继续未完成判定。W2-A 可在这里识别可信的 Kun 返工意见、再次放行。

`stages(target)` 返回阶段有序子集，必须包含接单、说清楚、执行、验收、收尾。`brief(item, stage, target)` 返回可信说明；`assess(item, receipt, target) -> Evidence` 必须短时只读，不靠回执 done 自证完成。

Evidence 字段：checks（检查 argv 列表）、passed（布尔）、actions（拟交付动作类别）、reason（判定解释）、paths（本次改动路径列表）、reviewers（1 或 2）。actions 中任何类别不在 `ask_first ∪ skip_ask`，都是插件错误，记事实、卡住这一类并通知，不放行。执行前必须有能跑的检查；交付前处理授权；验收证据不过无论 done/nothing/failed 都退回执行。

交付对象的 `two_reviewers` 路径前缀命中 paths，或 Evidence.reviewers=2 时，核心在审查阶段依次找两家不同家族，两家都过才能交付。已通过家族记入活的 reviews；写代码那家仍排除。第一次审查带 ask 时不计作通过，答完接回同一审查会话。返工清空本次审查记录。

`after(item, completed_stage, key, target)` 在停止本轮且核心准许推进后调用，必须持久 key 幂等。授权问题保存 receipt、Evidence、原阶段、下一阶段；明确批准后直接用这些证据过关口并调用同一个 after key，不再多起一轮审查。拒绝则回执行，清空本次批准/审查记录，追加“Kun 没批准”和所选项的 key/label 为核心关口反馈资料，再按意见修改。after 不得越权推进或提前做交付动作。

普通关口未过先同阶段接回同一个执行者，带 gate_reason；同一关口连续拦 3 次才问 Kun。最终完成必须登记工序、送回或等你。其他人负责的工序到点仅发一次通知；只有 owner="车间" 才唤醒活。

## 执行者 Executor

`available(config) -> bool` 仅查现有额度，不自动加购。每个规则候选必须明写 billing=subscription 或 metered；按量候选在这件活获明确批准前不交给挑选逻辑。若另有可用订阅候选则直接使用，避免为用不上的按量候选提问；否则发 C4 授权卡，批准后按配置指纹放入候选。拒绝则保留等你，等待调整后放行，不立即重复发卡。`Unavailable` 表示无额度且确认没启动，可挑下一候选；`Retryable` 表示暂时启动失败且确认没启动，按退避重试，连续第 3 次隔离。普通启动异常表示结果不确定，保留占位，不能猜没启动而换一家。

`launch(token, brief, item, config, target) -> Started(session, handle, workdir)` 和 `resume(token, session, brief, item, config, target)` 必须按 token 持久幂等。核心启动前记意图，崩溃后重放同 token 须找到同一轮；resume 返回同一个 session。target 包含完整交付规则及上述 runtime。环境中传轮次令牌，结束时用 runtime.command 交回执，提交后不再做业务动作。

`poll(token, started) -> "running" | "exited"` 提供明确的存活状态，不把静默当退出。`stop(token, started)` 幂等关闭，确认退出后才返回。启动后未拿到 Started 的崩溃窗口里 started 可为 null，适配器必须按 token 对账、查询/关闭；不得制造一个假 handle。退出未交回执、超时、`workshop abort <token>` 都生成失败结果并退避。abort 先登记请求，下一 tick 停止；不会为结束一个未确认的启动再重开一轮。无法确认退出时仍保留占位。核心探测前重读轮；已有回执就不再 poll，poll 期间收到回执也不会被自动失败覆盖。收尾 stop/assess/after 的 Retryable 按 5/10 分钟退避，连续第 3 次隔离对应插件；故障期间不重复收尾，恢复后接续原意图。

`usage(token, started, since) -> RoundUsage | None` 从本轮开始时刻 since 读取增量，不能返回整个接回会话累计值。started 在上述崩溃窗口也可为 null，无法取得则返回 None。RoundUsage 含 main（一条 Usage）、children（子 agent 按实际模型合计，各模型一条 Usage）、children_reported（是否已完整核对子 agent，默认 false）。Usage 每项均可 null：

```json
{
  "model_actual": "从会话记录读出的实际模型",
  "input": 100,
  "cache_read": 200,
  "cache_write": 50,
  "output": 20,
  "cost_usd": 0.01,
  "billing": "subscription"
}
```

main 必须只算主会话，children 只算子 agent，不可把同一子 agent 同时算进主会话和子项；上游累计量不能可靠拆分时相应字段留 null。子 agent 模型由执行者自己决定，不归挑选逻辑管。读不到/只读到部分子 agent 时 children_reported=false，why 和 usage 明确显示「子 agent 未计」；确认无子 agent 时用空 children 和 true。

input 是不含缓存读写的普通输入；billing 为 subscription / metered / null。cost_usd 是按对应模型 API 标价折算的 USD，订阅同样折算，不能混入实际订阅账单分摊。执行者负责已知标价的核算与来源，核心不猜价格；拿不到实际模型、缓存细分或价格就给 null，不能填 0 或照抄规则模型。0 仅表示确实观测为 0。usage 异常记事实，未取得的字段保持 null，不阻塞结束。

rounds 同时保存规则要求的 executor/model/effort、挑选理由、开始结束与 elapsed_seconds、主会话 Usage、分模型 usage_by_model（scope=main/children）、children_reported、outcome、审查/验收 passed（未知为 null）。实际退出未交回执等故障不伪造 passed。`why` 展示每轮这些记录。

## 挑选逻辑 Selector

`select(target, stage, round_number, history, candidates) -> Selection(config, reason) | None`。history 是这件活之前所有轮的快照，包括执行者、规则模型、实际主/子用量和结果；candidates 依唯一规则的阶段候选顺序排列，每项含 config、available=true。核心已调用 available，并筛掉写代码家族（审查/验收）、无额度候选、尚未批准的按量候选，也落实两家审和关口重试的家族/执行者限制。target.selection 仍提供 writer、reviewed、retry_executor、excluded 背景供解释；硬规矩不委托插件。

第一版唯一实现 `workshop.plugins.selectors.ordered` 只返回获准候选的第一项和理由。核心校验输出仍属于候选，违例记 selection_failed，why 能看到原因，按暂时起轮失败退避，连续 3 次升级故障；不会调用违例执行者。挑选逻辑可以全局/对象/阶段配置，但本版不带其他逻辑。

`workshop usage` 仅统计 status=ended 的轮，在跑、启动中、收尾中的轮不计入未上报或未知模型。保留今天、最近 7 天、按活，并按实际模型分组，未知模型单列。组内列轮数、审查/验收一次过、平均耗时、四种 token、API 折合花费和缓存命中率。一次过的统计单位为“活、阶段、执行者家族”的首次尝试；后续重试不把首次失败改成成功，两家审分别计各自首次尝试。缺判定的首次尝试排除并显示缺失数。平均耗时只取有起止数据的轮；缓存命中率只取输入/缓存读/缓存写三项完整的轮，公式为缓存读总和 ÷ 三项总和，分母 0 显示未知。每项只累加已知值，全部缺失显示未知；模型行同时列未上报轮数，各比率另外列缺失数。主会话和子 agent 的 token/花费按实际模型分别汇总，同一模型在同一轮出现两处，轮数与耗时只计一次。模型耗时是它参与的车间整轮耗时，不冒充子 agent 独立执行时长；审查/验收一次过只归因主执行模型，子 agent 贡献只算用量。各行另列「子 agent 未计 N 轮」。

## 回执和无卡等你

outcome=done 表示本轮拿到了供流程核验的结果，核心仍判证据和关口；nothing 表示没有新结果，不自行推进、不发空卡，没有安排则登记五分钟后复看；failed 表示这一轮没跑成，按 5/10/20/40/60 分钟退避、之后封顶 60 分钟。每次连续失败第 3 次通知一次，第 10 次卡住；成功后新一段连续失败会重新通知。验收实际证据不过优先于 failed 退避，直接回执行。进程退出/超时且无回执时没有验收证据，按运行失败退避，passed 保持 null，不伪造验收结论。

`receipt <token> --file <JSON>`、`--json` 或 stdin；提交成功才确认收到，下一 tick 停进程并结算。相同 token 相同 JSON 幂等，不同回执拒绝。时间支持 +Ns/+Nm/+Nh/+Nd 或带时区 ISO 8601，相对时间按提交时刻算。字段 outcome、summary 必填，其余可省略：advance、evidence（字符串列表）、ask、notice、later、events、leave。

```json
{
  "outcome": "done",
  "summary": "需要先确定验收标准",
  "notice": [{"key": "clarify-needed", "subject": "先说清楚验收标准", "text": "已停在说清楚，补充后放行即可，不用回卡", "fyi": true}],
  "leave": {"kind": "等你", "about": "等待明确的放行事件"}
}
```

这是 Kun 11-13 补定的“无卡等你”：停在原阶段，标等你，只发一次不用回的通知，不起执行者。普通 update 事件仅追加资料；来源明确标记的 `action="ready"` 事件才解开同一件活，从原阶段继续。暂停/插件故障期间收到的放行也按各自恢复规则保留。若有 ask，则等全部问题回答，不把 ready 当作卡片答案。

leave 工序须有 owner、at、about；送回须有 at、about；等你须有 about，并且有 ask 或 notice。notice 的 key 在一件活内去重，ask/later 的 key 在一轮内去重。正常推进自动登记下一工序。回执里有 ask 时不推进，问题解决后接回原阶段。

## 卡片、通道与对账

`Question`、`Choice`、`Notice` 的 TypedDict 和运行时校验在 `core/cards.py`，由端口模块导入。回执 ask 元素是 Question 加 key：

```json
{
  "key": "design",
  "subject": "方案选择",
  "decision": "采用当前方案还是调整",
  "context": "完整上下文与证据",
  "choices": [
    {"key": "yes", "label": "继续", "effect": "按当前方案继续"},
    {"key": "no", "label": "调整", "effect": "保留阶段并调整方案"}
  ],
  "recommend": "yes",
  "reason": "依据已登记证据",
  "kind": "reversible"
}
```

subject ≤30、decision ≤42、reason ≤60；choices 必须 2–3 个，key/label ≤18、effect ≤60，recommend 指向一个 key，kind 为 approval 或 reversible。context 非空。通知使用另一格式 `{subject, text, fyi:true}`，回执再加 key；subject ≤30，text 非空。核心自己的问题也遵守同一校验。

`ask(key, Question)`、`notify(key, Notice)` 短时返回外部编号。**发之前核心先记 sending 意图**；异常/重启后先调用 `reconcile(key, card_record)`：返回已发外部编号表示已发；`"absent"` 表示确定未发才可重试；null 表示不确定，保持待对账，禁止盲目重发。连续 3 次不确定记一次 reconcile_failed、发一次通知并在 status 显示卡号；仍继续对账，确定结果后清掉计数和待对账故障。告警本身也无法对账时只保留状态，不递归发告警。这里按调用次数计，tick 前后各有一次通道处理。

ask-kun ask 没有 request-id，适配器不能声称直接幂等。可先持久保存 key 到唯一 from 标记（如 `车间:<key哈希>`）和 subject 的映射，调用 ask 时使用该 from；重启后用 `ask-kun list` 按 from+subject 对账。列表不完整或请求可能仍在处理中时返回 null，不能返回 absent。相同 subject 的不同轮必须用不同稳定 from 区分。notify 若支持上游幂等标识也照样要实现对账。

`poll(cards) -> list[Answer(card_id, value, approved=False)]`，card_id 是核心卡号，value 必须是用户所选的 key。核心动作/计费审批只认 value="yes"，不信 approved；其他插件问题的明确批准才给 approved=True。`cancel(key, external)` 按核心 key 幂等作废，外部编号不明时先对账。返工时核心先把旧问题标 cancelled，再通知通道作废；晚到答案忽略，取消失败会保留待处理记录。每张卡每次故障只记一条 delivery_failed，恢复后再坏会再记；通道整体查询失败也按事件段去重。通道故障不阻塞其他活。

## 可选工单 TaskHome

第一版规则不配置，核心不发单也不卡活；以后配置才调用 `open(key, item, target) -> ticket`、`update(key, ticket, item, target)`。两者须持久幂等，不能冒充负责会话领单/收单。

## SQLite、恢复与看门

默认 `~/.local/state/workshop/workshop.db`，`WORKSHOP_STATE_DIR` 或 `--state-dir` 改目录。WAL + synchronous FULL；事实追加和视图更新同事务。facts trigger 禁止 UPDATE/DELETE；只有 evolve 写各视图，`Log.rebuild()` 重放验证一致。视图为 `(id, body JSON)`，表包括 items、rounds、cards、timers、controls、cursors、deferred、effects。

`run` 每 20 秒 tick，`--once` 一次。tick 用旁边的文件锁串行；pause/resume 只做短 SQLite 事务，不拿 tick 锁，插件挂起时仍可暂停。`resume <target>` 只列清单并输出已看事实号；必须同时带 `--select all|none|编号列表 --seen 上次已看编号列表` 才恢复，空清单写 none。清单变化拒绝并重新列出，不能用当前新清单代替人看过的清单。

插件故障卡住前保存 state/next/blocked_by/leave，恢复只还原因该故障叠加的卡住；原来的等你、定时和失败耗尽不被一律改为空闲。故障期间到期定时保留，恢复后再处理；已回答/已放行的事实也保留。每次新的插件故障用新事实号发一次通知，不是终身只发一次。已被该插件故障卡住的活不重复追加 work_blocked，故障期间跳过该类收尾。

### 看门接入

只有成功走完 tick 才原子替换 `heartbeat.json`，含 UTC at、last_ok_at（两者相同）和 active。失败 tick 不改心跳；首次 tick 即失败时文件不存在，看门应按缺失/last_ok_at 陈旧判断失联，不能只看守护进程还活着。普通 tick 异常记 tick_failed，同一错误在恢复前只记一次；controls 的 @tick 保存连续 failures，成功后归零，再坏会重新记事实。下一 tick 继续；若暂时写不进数据库则输出 stderr 并继续尝试，初始化打不开数据库才无法启动。独立看门下一波实现，可查 rounds JSON 的 at、status、deadline、stopped；starting/running/finishing 是未结算轮，stopped=true 不占执行者位。

`extension_metrics(log, repository, base, head, target, started)` 保留观察阶段的核心改动行数、第一件活用时固定检查，流程负责调用并登记结果。
