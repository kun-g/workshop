# 核心与插件接入契约（W1、W2-A）

类型以 `core/ports.py`、`models.py`、`cards.py` 为准。Kun 11-13 决定：第一版不接工单系统，issue 就是活的登记；`TaskHome` 保留为可选端口。挑选逻辑有 `ordered` 和 `quota` 两段，业务流程、来源、执行者、通道和独立看门程序由下一波实现。

## 注册、规则和运行入口

插件通过入口点 `workshop.<port>` 注册无参数工厂，名字对应唯一规则 `rules/workshop.yaml`。端口名为 `source`、`flow`、`executor`、`channel`、`taskhome`、`selector`。例如：

守护进程里每次插件调用都在独立的作业进程里跑（见 [作业.md](作业.md)）：插件不能靠实例里的内存在两次调用之间传东西，状态放文件；参数和返回值只用 `models.py` 的值类型、字典、列表和标量。

```toml
[project.entry-points."workshop.selector"]
ordered = "workshop.plugins.selectors.ordered:make_selector"
quota = "workshop.plugins.selectors.quota:make_selector"
[project.entry-points."workshop.flow"]
issue_delivery = "workshop.plugins.issue_delivery:make_flow"
```

规则中 `selector: ordered` 指定默认挑选逻辑；`stage_selectors` 可按阶段覆盖，交付对象也可设置 selector 和 stage_selectors。优先级：对象的阶段覆盖 > 对象默认 > 全局阶段覆盖 > 全局默认。现行规则全局和各交付对象都用 quota。`channel` 必填，`taskhome` 可省略。`limits.round_timeout_seconds` 是每轮期限，默认/示例为 7200 秒。`stage_executors` 可按中文阶段给候选，交付对象自己的同名配置优先；省略时接单/说清楚/执行/证据/交付/收尾/观察沿用 writer，审查沿用 reviewer，验收沿用 acceptor。例如：

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

## 一个交付对象的多个流程

`targets.<对象>.flows` 以流程名为键；默认用同名 `workshop.flow` 入口点，也可用本流程的 `plugin` 指定插件。对象公共字段与流程字段浅合并，流程字段优先；`name` 始终是交付对象，`flow` 是本流程名，`plugin` 是选定的插件。阶段、执行者候选、挑选逻辑、身份和关口配置均取该流程的合并配置。共享条件用 YAML 锚点，不增加继承机制。例如：

```yaml
targets:
  workshop:
    default_flow: issue_delivery
    flows:
      issue_delivery:
        triggers:
          - source: github
            when: {kind: label, actor: kun-g, label: Ready for Agent, by_workshop: false, edited: false}
            action: ready
          - {source: github, action: update}
```

每个流程的 `triggers` 是有序列表；首个匹配决定动作，后续兜底不覆盖。`source` 精确相等，`when` 各顶层字段同时满足；标量按类型和值相等，列表匹配任一标量，以 `^` 开头的字符串是正则，字段缺失不匹配，省略 `when` 匹配该来源全部事件。不支持嵌套路径或表达式求值。加载时校验结构、动作和正则，错误包含对象、流程及字段。来源事件在每个流程各匹配一次；CLI 和 agent 事件即使命中 ready/rework，也先强制 update，保留原有可信分类及连锁限制。带轮次令牌的子事件若没有匹配触发器，仍跟随原流程；跨对象时进入对方兼容流程，不丢失原有回执事件能力。

`Event.flow` 由核心填写，`flow_default` 表示兼容编号空间。`default_flow` 首次运行时固定到事实 `@flow-defaults`；旧规则升级时取旧 `plugin`，新规则未指定时取首个流程。重排 flows 不改已有归属，显式换兼容流程或移除它会报错。兼容流程的编号和别名仍为 `对象:对象号`，其他流程为 `对象:流程:对象号`。旧事实缺少 flow 仍可读取和重建，旧轮次令牌和回执不改；定时、会话放行定向原流程，不再次分流。`why` 和短名查找保留旧用法，其他流程可用完整编号。流程故障按所用插件隔离；同对象其他插件的流程继续，来源故障仍影响整个对象。

来源事件的去重记录使用 `[target, flow, ext_id]` 的 JSON 字符串；Event 本身和交给流程的资料保留原始 ext_id，以便回源查消息。可选 `action_id` 是一个不可重复控制动作的稳定来源标识，ready/rework 用它去重，普通更新仍用版本 ext_id；`previous_ids` 仅供兼容流程核对旧去重记录。GitHub 来源输出 kind、actor、author、label、text、by_workshop、edited 及原始 data，不判断 ready/rework；已知事件编辑只更新，游标丢失后的重新读取仍由稳定动作标识阻止重复返工。零命中记 unmatched 事实、不建活，在 `why --unfiled` 可查。来源可实现 `normalize(batch, target)` 兼容旧作业结果：`Poll.normalized` 缺省为 false，核心先在独立作业中补规范字段，成功后才提交事件和原游标；新 GitHub poll 返回 normalized=true，不多起作业。旧 classify 作业继续按持久原事件的身份认回。

旧 `plugin:` 配置仍支持来源自带动作。装配层可调用流程的可选 `configure_target(target)`，给旧文件补迁移数据；issue 交付仅在旧写法下提供 `legacy_triggers`，保留旧标签、Kun 身份及返工边界。新 flows 完全使用 YAML，不混入旧默认。暂停补回保留已选流程，不重新按当前条件分流。查找端口的持久 lookup 兼容旧字符串对象名和 `{target, flow, flow_default}`，后者只看本流程别名与卡片。

## 资料、回答与跨轮历史

`Event(source, ext_id, target, object_key, title, body, depth=0, action="update")`。旧单流程按 source/ext_id 去重；多流程按上节命名空间去重、归件。正文仅为资料。旧规则下只有进程内可信 `Source.poll` 可直接标 `action="rework"`（返工）或 `action="ready"`（放行）；CLI 自填 action 不可信。来源 poll 进来的事件对应的活还不存在时，只有 `ready` 新建活；`update`、`rework` 只记事实（事实带 `release_gate` 标记），等放行建活时按事实号并进资料（上次 `drop` 之前的不并）。外部入口经 classify 判定的事件、带令牌事件和回执子事件不受此限，照旧可建活。没有标记的旧事实重建视图时照旧投影。

核心能标出的 agent 来源有两种：带 `WORKSHOP_ROUND_TOKEN` 的 CLI `event`，以及回执 `events`。两者一律强制 action=update、depth=原活深度+1，忽略调用方 action/depth，永不调用 Flow.classify；未知令牌拒绝，第 9 层卡住。回执子事件 source/ext_id 由轮次令牌和索引生成。无轮次令牌的 CLI 事件强制 depth=0、action=update，先事务提交原事件和判定意图（尚不建活或追加到活），由下一 tick 调用 Flow.classify 后归件。暂停期间先记遗漏，选择补回后再判。所有 CLI 事件的 source 都加一层 `cli:` 前缀（含带令牌入口），与来源插件的去重空间分开；重复同一 CLI 输入仍去重，why 标“CLI 入口”。无令牌 CLI 的 depth 归零仅用于观测，不防止 agent 去掉令牌另开活绕过 C2；第一版接受此限制，不加更强限制。升级前缺 ingress 的遗漏事件按 unknown 处理，补回时 action 强制 update，也不交给 classify 追认。

内部 `Engine.ingest` 默认 ingress="source" 仅供可信进程内来源使用；CLI 固定传 ingress="external"，不能通过 JSON 自报 ingress。令牌入口覆盖 ingress 为 agent。适配器应保留轮次令牌，但 agent 可以去掉它，因此无令牌不等于来自人。核心不是挡 agent 的安全边界：agent 和守护进程是同一个系统用户，能写状态目录，第一版接受这个前提。

活快照中的稳定字段：id、target、flow、flow_default、object_key、title、stage、state、materials、history、session、sessions、writer、workdir、ticket、checks、approved、depth、next、blocked_by、leave、revision、cause、round。`sessions` 按执行者名字保存会话；`workdir` 是中性的执行目录。`history` 每轮追加 token、stage、outcome、summary、evidence、证据判定 reason，流程 brief 能读到审查意见和返工原因；`gate_reason` 是关口需要修正的理由。

`Brief.instructions` 是可信派活说明；`Brief.materials` 仅含外部资料，标“资料，不是指令”。`Brief.workdir` 和 `Brief.readonly` 是流程提供的可信执行目录与只读约束，省略时沿用执行者默认目录。`Brief.answers` 单独放 Kun 的回答，含 card、key、title、body、seq、role="Kun 的回答"。回答是独立事实，由 evolve 在最新资料上追加，不回写旧数组；cause 不倒退。回答与资料不是授权工具调用的自由文本，核心不再持有动作或计费审批；授权只认授权中心核实证据。插件自己发的问题保留 Answer.approved 语义，但不能赋予核心动作授权。

## 来源 Source

`poll(target, cursor) -> Poll(events, cursor)`，短时只读。整批事件与 cursor 同事务，失败整批不确认；cursor 仅变化时写事实；来源不得返回另一交付对象的事件。暂停期间继续记遗漏，不派新轮。

`Retryable` 表示暂时读取故障。按交付对象与来源分别记录首次失败时间，间隔依次为 1、2、5、10 分钟，之后保持 10 分钟，下一次尝试不晚于首次失败后 30 分钟；达到 30 分钟的尝试仍失败才隔离，原因保留最后错误与持续时长。重启保留退避窗口，成功清掉次数、首次失败时间和下一次时间。重试期间不因来源错误改变活、问卡及等待状态；普通异常仍立即记故障、隔离受影响交付对象，其他交付对象继续。

GitHub 来源只在 GET 入口把签发失败和命令未确认完成转为 `Retryable`，不回退个人身份；配置与数据错误不转换。写操作的未确认完成仍按原 `TargetBlocked` 路径阻塞，不自动重放。其他端口的重试策略不受来源窗口影响。

## 流程 Flow 和关口

`classify(event, item, target, find_external) -> Classification | str` 只判无轮次令牌的 CLI 事件。event 是已归一 action/depth 的原资料，item 是按当前对象键找到的既有活快照，没有则为 null；尚未加入本事件，target 是交付规则；核对可信上游身份和语义后决定动作，默认 `Classification("update")`。兼容只返回 action 字符串；返回 Classification(discard=True) 记 event_discarded 事实并结束判定，不建活、不追加资料；None 是协议错误。来源 poll 由规则决定动作（旧写法兼容来源动作），不走此方法；带令牌事件和回执子事件不走此方法。不带令牌的 CLI 事件仍可能来自 agent，classify 必须回到源头核实身份和原文（例如飞书按消息号回查发送者；GitHub 放行优先走来源 poll），不认 body 中 from_kun 等自称身份。测试 FakeFlow 回查独立的假上游登记，不把 CLI 正文当身份凭据。该方法短时只读，可能因重启重放；返回动作和判定意图完成同事务落事实，evolve 更新活。Retryable 退避、连续 3 次故障，非法返回值或普通异常隔离流程；修复恢复后继续未完成判定。W2-A 可在这里识别可信的 Kun 返工意见、再次放行。

`Classification(action="update", object_key=None, discard=False, summary=None)` 的可选 summary 是流程核实上游后给的一句话（谁说的、原话摘要，例如「飞书 Kun：「gc#109 不对」」），丢弃时写丢弃原因；核心照存进 event_classified / event_discarded 事实，只供 why 显示，不参与判定和相等比较。可选 object_key 只能指向同一交付对象及流程的已有活（先解析别名），不能跨目标或凭空指定新活。没有指定则用输入对象键；归件、资料追加和完成判定同事务。`find_external(external)` 是只读函数，按通道外部编号查 `{card, item}` 快照；跨交付对象、重复编号或未找到都返回 null。`find_external(object_key=...)` 按同目标同流程已有对象键查 `{item}`，先解析别名，找不到返回 null。流程仍须回源核对引用关系，不能把 CLI 自报外部编号当凭据。

`after` 返回的 aliases 把目标下的另一个 object_key 绑定到当前活。核心追加 alias_bound 事实；重复 key 幂等，别名已指向其他活或已有同名活则隔离流程，不覆盖。来源、agent、外部事件和暂停补回都先查别名。已开始但尚未结算的完成钩子会暂缓该目标来源轮询，恢复别名后再收新事件；避免远端对象创建成功、核心绑定前崩溃造成重复活。

`stages(target)` 返回阶段有序子集，必须包含接单、说清楚、执行、验收、收尾。`brief(item, stage, target)` 返回可信说明字符串，或 `FlowBrief(instructions, workdir=None, readonly=False)`。workdir 必须是绝对路径，核心放入执行者收到的 `Brief.workdir`、`Brief.readonly`；外部材料、人的回答仍由核心独立组装，不准流程混入这两个字段。只读副本由流程准备，去凭据的进程环境由执行者落实。

`assess(item, receipt, target) -> Evidence` 必须短时只读，不靠回执 done 自证完成。

Evidence 字段：checks（检查 argv 列表）、passed（布尔）、actions（拟交付动作类别）、reason（判定解释）、paths（本次改动路径列表）、reviewers（1 或 2）、done_key（这件活这次完成的标识；供通用通知兼容路径使用，issue 交付不要求通知）。actions 中任何类别不在 `ask_first ∪ skip_ask`，都是插件错误，记事实、卡住这一类并通知，不放行。所有往前推进（含收尾完成）都要求 passed；不通过留在当前阶段走 gate_retry，同一关口连续三次才问 Kun。显式退回执行不受此前进条件阻拦。到过验收（history 里有验收轮）又回到执行的活，执行可以提议直接回验收（advance=验收），表示已交付的结果不用改、重走验收；核心只认「到过验收」这一个阶段事实，流程证据过了才放行，结果没变、能走几次由流程判。没到过验收的活照旧不能越过阶段；执行紧接验收的流程不走这条，照常处理交付授权。执行前必须有能跑的检查；交付前处理授权；验收证据不过无论 done/nothing/failed 都退回执行。

交付对象的 `two_reviewers` 路径前缀命中 paths，或 Evidence.reviewers=2 时，核心在审查阶段依次找两家不同家族，两家都过才能交付。已通过家族记入活的 reviews；写代码那家仍排除。第一次审查带 ask 时不计作通过，答完接回同一审查会话。返工清空本次审查记录。


交付对象的 `serial_paths`（前缀；登记的路径可以是目录，和规则前缀互为前缀就算碰到）：核心把每轮 Evidence.paths 并进活的 paths——说清楚时流程交预计会改的路径，之后交实际改动。阶段在执行至交付之间（含两端及启用的审查）、paths 碰到串行前缀的活，同一交付对象同一时间只有一件占着（serial_hold）：别的活占着时停在当前阶段前，状态「排队」，下一步和工序写在等哪件，卡在谁手里写那件的 id。占位者交付完成进入验收、退回说清楚或被放弃后，按活的登记顺序自动放下一件；验收、收尾和观察不占位，也不因其他活占位而排队。执行至交付区间内退回执行仍占位；从验收及以后返工则重新判断，有人占就排队，无人占才重新占位，旧状态残留的占位不能沿用。说清楚漏登记的，在执行交回实际改动后、证据轮开始前同样排队。issue 交付所说的“合并后让出”以阶段为准：车间自己合并即交付，进入验收就让出；其他交付对象若仍在交付阶段等待上线，继续占位，交付条件不变。提前让出不等于完成，活仍以收尾为完成；全局并发上限照旧。

`workshop abandon <活> --reason <原因>`：Kun 决定不做的活停在当前阶段，状态「放弃」，取消没答的问题，让出串行路径，记录都留着；还有一轮没结束时先 abort。放弃前收到还没处理的返工、插件故障时留的恢复快照都作废；放弃前进来、放弃后才分类或暂停后才补放的事件，返工和放行降为普通更新（按原来收到时的事实序号比放弃记录；暂停后补放的外部入口事件，分类记录带原序号 received_seq）；插件故障和恢复不再动它，放弃前约的工序和送回到点只记事实不发卡。之后的新返工（「不对」）回到执行，重新排队。

`after(item, completed_stage, key, target)` 在停止本轮且核心准许推进后调用，必须持久 key 幂等。传入 item 额外含 `result`（当前已核验回执 JSON）；history 仍是此前轮次。返回 `After(aliases=[], pending=False, reason="", next="", rerun=False, recheck_after="+5m")`；兼容 None 或别名字符串列表。

`restart=True` 表示完成钩子需要重启：核心持久登记原活、阶段、key、回执和请求进程实例，暂停该钩子的结算；这一拍结束后 `workshop run` 正常退出，由外部守护拉起新进程。新进程第一拍成功写出心跳后，同拍发起原 `after` 的重调，不经过 `pending` 的退避。只有这次重调的 `target.runtime` 带 `daemon`（和心跳 `process` 同一形状，含 pid、操作系统启动时间 start、version、first_at、previous）与 `first_ok_at`（新进程第一拍成功时间）；其他钩子调用不带这两个字段。首次一拍失败时不重调，直到成功心跳；多个请求逐件重调，异步作业完成后再结算原阶段。进程再重启仍能从事实库恢复请求，作业身份按新进程实例区分。重调不能再次返回 `restart=True`，须重新核验状态；异常沿用流程故障隔离或 Retryable 重试，不误记确认完成。

`notice` 非空时按现有通道规则处理，卡片 key 为 `after-notice:<after key>`，同一个完成钩子只记一次。`restart` 与 `notice` 类型分别须为布尔值和字符串。

pending=true 表示尚未完成这个阶段，须带非空 reason 和 next，不算执行失败。rerun=true 才在同一阶段接回执行者；rerun=false 登记工序，到 recheck_after（相对时间或 ISO 时间）只重调 after，沿用同一 key 与 item.result，不再 assess 或启动执行者。核心按插件给的间隔回查，指数退避只用于故障重试。pending_limit 达上限仍卡住。wake_on_completed=true 表示同目标其它活完成时提前回查原 after；只唤醒，不能代替部署或授权核实。授权中心的原申请、动作、目标和检查点由流程持久保存，等待期间不占执行者；批准且核实生效才继续，已执行的动作不重做。

可选 `cleanup(target, keep)` 只清流程管理的临时副本；守护进程启动时和轮停止后调用，keep 包含所有未停止轮的 brief.workdir，必须保留。其他流程可不实现。

可选 `hold_workdir(item, target) -> str`：活收尾完成后，执行者开过的工作目录还要不要留。返回留着的原因（核心记在活的 release_hold，约一小时后再问），空串表示可以收回。短时只读；Retryable 稍后再问，其他异常按流程故障处理。不实现等于不留。

可选 `describe(event, target) -> str | None` 给 `workshop why` 每条事件一句话说明（例如谁对哪个对象做了什么）。只读事件本身，不回查上游、不写状态；返回 None、空串或抛异常时核心退回「来源 + 事件种类」（body.kind，没有则为「资料」）。核心把说明压成一行、截到 200 字；原始资料只在 `why --raw` 时打印。

外部入口事件判定后：归到某件活（含因此新建的活），那件活的 why 有一行「归件 来源 动作（更新/返工/放行）：摘要（原事件 号）」，摘要先用判定时流程给的 summary，没有（旧事实）再用 describe。判为丢弃的、还没判定完的、归到的活已被清掉的，列在 `workshop why --unfiled`（未归件）里，带丢弃原因和事件号，不静默消失。

普通关口未过先同阶段接回同一个执行者，带 gate_reason；同一关口连续拦 3 次才问 Kun。最终完成必须登记工序、送回或等你。其他人负责的工序到点仅发一次通知；只有 owner="车间" 才唤醒活。

## 执行者 Executor

`available(config) -> bool` 仅查现有额度，不自动加购。每个规则候选明写 billing。按量授权类别尚未支持，只有订阅候选进入挑选；没有订阅候选时标卡住，明确缺授权类型，不发计费卡。`Unavailable` 表示无额度且确认没启动，可挑下一候选；`Retryable` 表示暂时启动失败且确认没启动，按退避重试，连续第 3 次隔离。普通启动异常表示结果不确定，保留占位，不能猜没启动而换一家。

`launch(token, brief, item, config, target) -> Started(session, handle, workdir)` 和 `resume(token, session, brief, item, config, target)` 必须按 token 持久幂等。核心启动前记意图，崩溃后重放同 token 须找到同一轮；resume 返回同一个 session。target 包含完整交付规则及上述 runtime。环境中传轮次令牌，结束时用 runtime.command 交回执，提交后不再做业务动作。

`target.runtime.brief_max_chars` 是当前唯一规则给出的最终派活文本字符预算，缺省 524288。执行者可在确认模型尚未启动时抛 `InputBlocked`，表示输入无法安全送出；错误消息须写明失败项、实际长度、预算与修复动作。作业往返保留此异常类型，核心只结束本轮并阻塞本活，不把它当作暂时故障重试或插件故障隔离。已存在启动意图、无法确认是否起过模型的情况不得使用此异常。

`poll(token, started) -> "running" | "exited"` 提供明确的存活状态，不把静默当退出。`stop(token, started)` 幂等关闭，确认退出后才返回。启动后未拿到 Started 的崩溃窗口里 started 可为 null，适配器必须按 token 对账、查询/关闭；不得制造一个假 handle。退出未交回执、超时、`workshop abort <token>` 都生成失败结果并退避。abort 先登记请求，下一 tick 停止；不会为结束一个未确认的启动再重开一轮。无法确认退出时仍保留占位。核心探测前重读轮；已有回执就不再 poll，poll 期间收到回执也不会被自动失败覆盖。执行者停止与流程结算分开：交付对象故障期间仍处理 poll、超时、abort、stop 和用量采集；确认停止即记 stopped 并释放并发位，但轮保持 finishing、活保留卡住与 before_plugin_failure，恢复后才 assess/decide/after。已确认停止和采过的用量不重复调用。stop 与流程结算各有独立退避，Retryable 第 3 次隔离；stop 在故障期间仍按 5/10/20/40/60 分钟退避继续尝试，普通 stop 异常立即隔离并同样退避，绝不能在确认停止前释放位。故障对象不启动新轮，也不再调用流程判定/完成钩子；恢复后接上原回执。

`usage(token, started, since) -> RoundUsage | None` 从本轮开始时刻 since 读取增量，不能返回整个接回会话累计值。started 在上述崩溃窗口也可为 null，无法取得则返回 None。RoundUsage 含 main（一条 Usage）、children（子 agent 按实际模型合计，各模型一条 Usage）、children_reported（是否已完整核对子 agent，默认 false）。Usage 每项均可 null：


可选 `release(item, target) -> Release | None`：活收尾完成、流程 `hold_workdir` 不留、这件活没有在跑的轮次时，核心对活的 sessions 里的执行者逐个调用，收回该执行者为这件活开的工作目录。不是自己开的或已经不在，返回 None，核心问下一家；收回了返回 `Release(True)`，核心清空活的 workdir（返工时执行者重新开）；有没交出去的改动等原因不能收，返回 `Release(False, 原因)`，核心通知 Kun 一次，不再重试。暂时故障抛 Retryable，约一小时后再试；其他异常按执行者故障隔离。所有执行者都不认领时只记一笔，不再问。

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

`select(target, stage, round_number, history, candidates) -> Selection(config, reason) | None`。`Selection(None, reason)` 表示一个都没选并说明原因：核心记 `selection_none` 事实（why 显示「挑选逻辑没选出执行者 …」），把原因写进卡住的 blocked_by 和建议动作；返回 None 时使用通用的执行者不可用原因，不发问卡。history 是这件活之前所有轮的快照，包括执行者、规则模型、实际主/子用量和结果；candidates 依唯一规则的阶段候选顺序排列，每项含 config、available=true。核心已调用 available，并筛掉写代码家族（审查/验收）、无额度候选、尚未批准的按量候选，也落实两家审和关口重试的家族/执行者限制。target.selection 仍提供 writer、reviewed、retry_executor、excluded 背景供解释；硬规矩不委托插件。

`workshop.plugins.selectors.ordered` 只返回获准候选的第一项和理由。

`workshop.plugins.selectors.quota` 按候选顺序取第一个额度够的。候选上 `quota: {provider, window, min_remaining, label}` 是门槛（剩余百分比 ≥ min_remaining 才选），没写门槛的候选不看额度，所以全无门槛时结果和 ordered 一样。额度只读本机 ai_quota 快照库（交付对象上 `quota_snapshot: {path, max_age_minutes}`，默认 `~/.local/state/ai_quota/quota.db`、45 分钟），取该家最近一次成功采集；库读不到、缺窗口或超过过期线时跳过有门槛的候选并写进理由。候选上 `first_when_writer: [家]` 表示写代码的是这些家时把它排到最前（核心已排除写代码那家，这里只调顺序）。理由例：「Codex 周额度剩 50%（门槛 15%），选 orca-codex；阶段 执行，第 1 轮」。所有候选都被跳过时返回 `Selection(None, 理由)`，理由列出每个候选为什么被跳过。核心校验输出仍属于候选，违例记 selection_failed，why 能看到原因，按暂时起轮失败退避，连续 3 次升级故障；不会调用违例执行者。挑选逻辑可以全局/对象/阶段配置。

`workshop usage` 仅统计 status=ended 的轮，在跑、启动中、收尾中的轮不计入未上报或未知模型。保留今天、最近 7 天、按活，并按实际模型分组，未知模型单列。组内列轮数、审查/验收一次过、平均耗时、四种 token、API 折合花费和缓存命中率。一次过的统计单位为“活、阶段、执行者家族”的首次尝试；后续重试不把首次失败改成成功，两家审分别计各自首次尝试。缺判定的首次尝试排除并显示缺失数。平均耗时只取有起止数据的轮；缓存命中率只取输入/缓存读/缓存写三项完整的轮，公式为缓存读总和 ÷ 三项总和，分母 0 显示未知。每项只累加已知值，全部缺失显示未知；模型行同时列未上报轮数，各比率另外列缺失数。主会话和子 agent 的 token/花费按实际模型分别汇总，同一模型在同一轮出现两处，轮数与耗时只计一次。模型耗时是它参与的车间整轮耗时，不冒充子 agent 独立执行时长；审查/验收一次过只归因主执行模型，子 agent 贡献只算用量。各行另列「子 agent 未计 N 轮」。

## 回执和等待

outcome=done 表示本轮拿到了供流程核验的结果，核心仍判证据和关口；nothing 表示没有新结果，不自行推进，没有安排则登记五分钟后复看；failed 表示这一轮没跑成，按 5/10/20/40/60 分钟退避、之后封顶 60 分钟。连续失败第 3 次通知一次，第 10 次卡住。验收实际证据不过回执行；操作阻塞的 Evidence.blocked 则保留阶段，转为卡住。

`receipt <token> --file <JSON>`、`--json` 或 stdin；提交成功后下一拍停进程并结算。相同 token 相同 JSON 幂等，不同回执拒绝。字段 outcome、summary 必填，其余可省略：advance、evidence（字符串列表）、ask、notice、later、events、leave。

等你只用于关联有效问卡的等待；ask 必须有选项、推荐和理由。旧版 notice+等你 回执兼容读入，但落成卡住，通知不代替问卡。普通 update 事件只追加资料，历史上登记过 wait_for_ready 的活仍可由可信 ready 继续；它不伪造问卡答案。缺工具、权限、次数用完等操作阻塞使用 Evidence.blocked 和 Evidence.next，详见 [恢复与重验](恢复与重验.md)。

leave 工序须有 owner、at、about；送回须有 at、about；等你须有 about 和有效问卡。ask/later 的 key 在一轮内去重。时间支持 +Ns/+Nm/+Nh/+Nd 或带时区 ISO 8601，相对时间按提交时刻算。

回执里的 notice 只在 Kun 需要知道时发卡（ws#46，11-13 Kun 嫌卡太多）：这一轮 leave 是等你且存在有效问卡时照发，卡片 key 为 `notice:<回执 key>`，在一件活内去重；这一轮让活完成（最后一个阶段判过并推进），或流程给出非空 Evidence.done_key 且这一轮判过并向前推进时，只发第一条，卡片 key 为 `notice:完成:<Evidence.done_key>`（done_key 空时为 `notice:完成`），所以同一完成标识不论执行者用什么 key、交几次，只出一张卡。其余的不发卡，记 `notice_held` 事实（阶段、key、subject、正文、原因），`workshop why` 显示为「通知没发」。进度在 `workshop status` / `why` 里看，不推送。核心自己发的告警（连续失败、卡住、插件故障、卡发不出去、对账不确定、工序到点、完成钩子的 notice）不受影响。正常推进自动登记下一工序。回执里有 ask 时不推进，问题解决后接回原阶段。

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

`poll(cards) -> ChannelPoll(answers, checked, errors, token)`，其中 answers 是 `list[Answer(card_id, value, approved=False)]`，checked 是本次已读的核心卡号，errors 按卡号记录错误；兼容旧通道直接返回回答列表。token 标识一次完成的读取，后台缓存重复返回时不重复计数。card_id 是核心卡号，value 必须是用户所选的 key。核心动作/计费审批只认 value="yes"，不信 approved；其他插件问题的明确批准才给 approved=True。kind=approval 的卡收到不在 choices.key 中的 value，记一次投递故障并继续等原卡，不当作批准或拒绝；有效回答到达后清故障。`cancel(key, external)` 按核心 key 幂等作废，外部编号不明时先对账。返工时核心先把旧问题标 cancelled，再通知通道作废；晚到答案忽略，取消失败会保留待处理记录。每张卡每次故障只记一条 delivery_failed，恢复后再坏会再记；通道整体查询失败也按事件段去重。通道故障不阻塞其他活。

### 通道拒收与上游作废（W2-C 修改轮 3）

`ChannelRejected` 表示适配器的内容预检无法改写成合规卡片，或发送命令明确拒收卡片。ask-kun 适配器仅在 ask/notify 退出码为 2 且 stderr 含「尚未发送」或 `usage: ask-kun` 时抛出，保留 stderr。启动失败（命令缺失、无执行权限、可识别的 uv 启动错误）记 not_started，对账返回 absent 可重试；内容预检失败（例如选项 key 不合法）记本地拒收，不重试；运行环境未启动仍与拒收分开。超时、断线和丢回包继续按 sending 对账。list/wait/cancel 错误一律不抛拒收。

核心记录 `card_rejected`（why 可见），停止对该卡投递/对账。同一张卡只创建一条固定通知「车间有一张卡发不出去，原因已记录，请看 workshop why <活>」，通知自身拒收不递归，但记 delivery_failed，心跳带给看门；固定通知成功则清除原拒收卡的投递失败信号。

通知拒收只记事实/告警，不动活。问题拒收保留 awaiting_release，waiting() 仍算待答：跑着时不改状态，轮结束自然等你；卡住时不覆盖状态，只更新可恢复的等你快照；完成的活不重开。明确 ready 事件消费 awaiting_release 和 wait_for_ready（跑着/卡住时也消费），不伪造其他待答卡的答案，返工也解除旧拒收卡等待。

`Answer.cancelled` 默认 false，明确作废或确认已清理的卡返回 `Answer(card_id, "", cancelled=True)`。核心记 `card_cancelled_external`、标 cancelled/cancel_done，按上述问题终态规则保留等待；如果该卡承载授权，清除旧续接记录，修复放行后重走原阶段，不把没有回答说成 Kun 拒绝。

poll 每批先调用一次 `list --all`，列表中 waiting/sending/queued 的卡不再调用 wait。现代列表的已答卡直接取 choice，核对本通道发送记录的核心卡号、外部编号、原始 payload、from、subject 和 answer_format=key，并要求 choice 是原卡的合法选项；缺失、无效或不匹配只记读取错误。取消不当批准，defaulted 不当人工回答。旧列表不含 answer_format/choice 时才沿用 wait 的鉴权读法。读现代列表不认领答案、不写上游状态，避免 wait 写回被监听文件后使自身作业结果失效。

ask 成功时记录 sent_at，丢回包后找回原卡时保留上游发送时间。list 可能按调用者会话过滤，缺号只有本地发送时间早于 29 天才按已清理作废；未满 29 天、恰好 29 天、发送时间缺失或无效都记逐卡查询失败，不作废。旧读法 wait 返回 4 且 stderr 含 HTTP 403 时，仍须重查完整列表及发送时间，不能把权限故障当作废。列表格式不完整或分页/截断时保留所有卡并逐卡记错。

读取错误与其他卡的答案一起经作业结果传给核心。每卡连续 10 次已完成读取失败，在 status 的卡住清单显示活、卡号、最近错误和建议动作，why 记下达到阈值的那次；正常 waiting 读取会清零，作业尚未完成和缓存重放都不计失败。读取故障保存在卡上，status 把等回答的活显示为卡住，不覆盖活原有状态或无关阻塞；恢复后自动收取原卡、继续原活。沿用投递故障事实与看门通知，不另发问卡或反复通知。

通知的 external 保存实际传给 ask-kun `--request-id` 的稳定键（含状态目录及数据库文件身份哈希），不再只存核心 key；通知回复归件在后续工作接入，本轮不处理。

## 可选工单 TaskHome

第一版规则不配置，核心不发单也不卡活；以后配置才调用 `open(key, item, target) -> ticket`、`update(key, ticket, item, target)`。两者须持久幂等，不能冒充项目经理领单/收单。

## SQLite、恢复与看门

默认 `~/.local/state/workshop/workshop.db`，`WORKSHOP_STATE_DIR` 或 `--state-dir` 改目录。WAL + synchronous FULL；事实追加和视图更新同事务。facts trigger 禁止 UPDATE/DELETE；只有 evolve 写各视图，`Log.rebuild()` 重放验证一致。视图为 `(id, body JSON)`，表包括 items、rounds、cards、timers、controls、cursors、deferred、effects。

`run` 每 2 秒一拍，插件调用都放到后台作业（见 [作业.md](作业.md)），一拍毫秒级；`--once` 只跑一拍，插件调用就地进行，供手工排查。tick 用旁边的文件锁串行；pause/resume 只做短 SQLite 事务，不拿 tick 锁，插件挂起时仍可暂停。`resume <target>` 只列清单并输出已看事实号；必须同时带 `--select all|none|编号列表 --seen 上次已看编号列表` 才恢复，空清单写 none。清单变化拒绝并重新列出，不能用当前新清单代替人看过的清单。

插件故障卡住前保存 state/next/blocked_by/leave，恢复只还原因该故障叠加的卡住；原来的等你、定时和失败耗尽不被一律改为空闲。故障期间到期定时保留，恢复后再处理；已回答/已放行的事实也保留。每次新的插件故障用新事实号发一次通知，不是终身只发一次。已被该插件故障卡住的活不重复追加 work_blocked，故障期间仅推迟该类流程结算，执行者侧继续安全停轮并释放已停止轮的并发位。

### 看门接入

只有成功走完 tick 才原子替换 `heartbeat.json`，含 UTC at、last_ok_at（两者相同）、tick_ms（这一拍耗时，毫秒）、active，以及 delivery_failed_count（当前未恢复投递失败信号数）和 delivery_failed_since（其中最早开始时刻，无故障为 null）。成功恢复清除对应失败信号；拒收通知自身失败也在此计数。看门在投递故障持续超过 180 秒时告警/兜底。失败 tick 不改心跳；首次 tick 即失败时文件不存在，看门应按缺失/last_ok_at 陈旧判断失联，不能只看守护进程还活着。普通 tick 异常记 tick_failed，同一错误在恢复前只记一次；controls 的 @tick 仅在连续失败达到 1/3/10 次阈值时更新 failures（10 表示至少 10 次），成功恢复时归零；相同故障持续 30 次也仅三条阈值 control。阈值间计数在进程内，重启从最近持久阈值接续，不作为精确失败次数报表；同一错误事实去重可跨重启，再坏会重新记事实。下一 tick 继续；若暂时写不进数据库则输出 stderr 并继续尝试，初始化打不开数据库才无法启动。独立看门下一波实现，可查 rounds JSON 的 at、status、deadline、stopped；starting/running/finishing 是未结算轮，stopped=true 不占执行者位。

`extension_metrics(log, repository, base, head, target, started)` 保留观察阶段的核心改动行数、第一件活用时固定检查，流程负责调用并登记结果。

## 阻塞与恢复

`Evidence.blocked` 是操作阻塞原因，非空时必须有 `Evidence.next` 建议动作；核心保留阶段并登记卡住，不把失败验收或推进混进这条路径。流程可实现只读的 `prepare_recovery(item, options, target) -> dict`，核心把计划写成恢复事实，流程在完成钩子幂等消费。命令与 issue 交付协议见 [恢复与重验](恢复与重验.md)。

## 等待端口（#173，D2/D3/D6）

`wait_services.<名字>` 指定等待端口。核心只核对名字到对象的映射；装配层调用可选 `WaitService.validate(config)`，报错注明配置路径。`wake_paths(config)` 返回本地唤醒路径。独立服务校验自己读取的 `intervention` 段，守护进程不读取业务字段。

`Flow.after(item, stage, key, target, services)` 的最后一个参数是只读句柄：`services(name)` 返回等待端口与配置副本。同步和作业进程都在调用时构造；四参数旧实现仍兼容。流程不自行装配或引用别的插件。直接调用的五条条件及失败处理见 [介入点契约](介入点契约.md)。

`Relations.priority` 为 P0/P1/P2，缺省 P1；issue 流程从正文读取。关系回查失败时该活仍卡住，不沿用旧快照派轮，也不挡住别的可开工活。全局派新轮前稳定排序，不抢停已有轮，前置、并发及串行路径约束保留。

`Evidence.pause` 只表示流程需要在 after 创建等待，不携带业务问题。回执的取舍内容由流程解释并验证；after 返回等待引用后，轮次结束并让出串行位置。结果到达后重新调用原 after 核验，再回原阶段；完整问题与回答由流程带进续接或新开的会话。


### 授权证据（#174，D4）

`Evidence.action_targets: dict[str,str]` 绑定本次动作与确定目标，`Evidence.authorized_actions: list[dict]` 保存流程已在授权中心核实的记录。每项必有非空 action、target、request、result 和布尔 applied。核心在交付/验收前（包括返工直接回验收）核对所需动作和目标，只有 result=approved 且 applied=true 放行。普通会话及旧批准投影不参与计算。流程还须独立保存原 execution 结果，不能用批准代替实际生效证据。

`After.wake_on_completed` 默认 false。true 时核心在暂停位置保存完成事实游标；同目标另一件活完成后，下一拍提前回查，取消原回查定时器。重启可重放，仍核实原完成钩子，不启动模型轮，不绕过故障重试期限。

## 执行者的新情况投递（#175，D5）

可选 `Executor.inform(token, started, operation, text) -> bool` 由项目经理协调器调用。operation 绑定不可变内容；True 表示确认收下，不代表已经处理；False 表示未确认，协调器保留未送达事实，等当前轮退出、确认 stop 后，用完整记录 resume。存活查询不确定时不另开会话，健康会话没有时长上限。

本地适配器仅对仍在运行且会话、终端匹配的 Orca 会话调用 `terminal send --enter`。意图先落盘，同一操作不因回包丢失重复敲入。headless 不支持运行中输入，返回 False；后续走同一会话的 resume。可选方法缺失也走这条退路。
