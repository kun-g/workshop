# 执行者配置档与驱动

规则的 `executor_profiles` 集中定义配置档，`executors` 和 `stage_executors` 的候选只写 `use`。原五个名字保留，用来接回在途会话；不要为了改模型而改名。换名字就是换配置档，开新会话。同一名字改启动选项也会开新的厂商会话，避免旧提示词或账号串用。

```yaml
executor_profiles:
  diagnose:
    driver: claude
    transport: orca
    billing: subscription
    model: claude-opus-5-5
    effort: high
    prompt: 先用证据定位根因，再改代码。
    skills: [/absolute/path/diagnosing/SKILL.md]
    tools: [Read, Glob, Grep, Bash]
    permission_mode: dontAsk
    mcp_config: /absolute/path/diagnosing-mcp.json
    plugins: [/absolute/path/plugin]
    settings: {language: Chinese}
    config_dir: /absolute/path/account-config
executors:
  writer: [{use: diagnose}]
```

这是字段示例；完整规则还要包含交付对象、reviewer、acceptor 等原有必填项。`family` 可省略，由驱动提供，不能伪装成另一家。`billing`、`quota`、`first_when_writer` 是车间的计费和挑选设置；其他字段才是 CLI 启动设置。`transport` 为 `orca` 或 `headless`，与 `driver` 分开。

| 字段 | Claude | Codex | Cursor |
|---|---|---|---|
| `model` / `effort` | `--model` / `--effort` | 模型参数 / `model_reasoning_effort` | 模型参数，强度编码为 `[effort=...]` |
| `prompt` / `skills` | 追加 system prompt | 追加 `developer_instructions` | 不支持 |
| `tools` | 工具名列表或逗号分隔字符串 | `{shell: false, web_search: disabled}`；分别映射 `features.shell_tool` 和 `web_search` | 不支持 |
| `permission_mode` | `acceptEdits/auto/bypassPermissions/manual/dontAsk/plan` | `untrusted/on-request/never`（approval_policy） | `plan/ask/agent`；agent 不传 `--mode` |
| `sandbox` | 不支持 | `read-only/workspace-write/danger-full-access` | 不支持 |
| `mcp_config` | 原生 JSON 对象或 JSON 文件路径 | `mcp_servers` 原生对象或 TOML/JSON 文件路径 | 不支持 |
| `plugins` | 插件目录/zip 路径列表，传 `--plugin-dir` | 原生插件表，例如 `{"review@local": {enabled: true}}` | 不支持 |
| `settings` | 原生 settings 对象或 JSON 文件 | 原生配置对象或 TOML/JSON 文件，作为 `-c` 参数传递 | 不支持 |
| `config_dir` | `CLAUDE_CONFIG_DIR` | `CODEX_HOME` | 不支持 |

`skills` 是本地 SKILL.md 文件或含该文件的目录列表；内容连同绝对资源目录进入启动指令，不需要全局安装。插件和 MCP 配置的内部结构使用各家原生格式。路径使用绝对路径或 `~`，相对资源引用以 skill 所在目录为准。

Claude 另支持 `restricted`、`strict_mcp_config`（布尔）和 `allowed_tools`、`disallowed_tools`（列表）。

Claude 另支持 `gateway: {base_url, token}`：让 Claude Code 的壳经本机网关跑别家的模型（核心 R25，ws#287）。启动时变成 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_AUTH_TOKEN`，不设 `ANTHROPIC_API_KEY`（交互模式带它会停在确认框）。订阅计费的会话照旧清掉进程环境带来的密钥，配置档声明的这两个变量保留。规则：

- 两项都必须写，多写、拼错或 `base_url` 不是 http(s) 地址，加载规则时报出配置档名和字段；不能同时写 `require_subscription_login`（登录用网关的）。
- 必须写 `family`，值是模型那一家；审查和验收照这一家回避。没写 `gateway` 的配置档照旧由驱动定哪一家。
- 每次挑选时可用性检查不带认证访问 `base_url`，有 HTTP 回应算连得上；连不上就不可用，挑选跳过它，地址写进执行者状态目录的 `driver.log`。
- 会话记录里的模型名带来源前缀（`codex/gpt-6.2-astra`），记用量时去掉前缀，花费按车间价格表折算；价格表没有的记未知。

```yaml
  orca-claude-gpt:
    driver: claude
    transport: orca
    billing: subscription
    gateway: {base_url: http://127.0.0.1:3425, token: magpie}
    model: codex/gpt-6.2-astra
    family: codex
```Codex 另支持 `model_provider`，以及 headless 的 `ignore_user_config`、`ignore_rules`。交互 Codex 必须写明 model 和 effort。

启动前的完整校验入口是 `workshop.registry.load_rules(path)`；核心的 `Rules` 只做通用结构校验，装配层通过驱动端口做字段校验，`Engine` 初始化也会验证所有配置档，作业未开始即报错。驱动字段不能在阶段候选上重复覆盖。

只读审查配置档沿用 `codex`、`claude`、`cursor-grok` 这三个名字。CLI 的工具和权限完全来自配置档；流程的 readonly 标记仍强制清部署凭据和保护工作目录的归属记录，不改写启动权限。后台执行保持清凭据。用户终端环境与本轮增量先合并，再统一清理，增量不能把凭据加回。

## 部署目录

仓库的规则（`rules/workshop.yaml`、`rules/executors/`）是一批默认的执行者，合并后发版生效。每个部署可以在仓库之外再叠一层「部署目录」：加减自己的执行者、改谁当哪个角色、关掉 Orca，校验后重启车间就生效，不用发版（核心 R16、R18、R25，ws#293）。部署目录默认是 `~/.config/workshop/`，环境变量 `WORKSHOP_DEPLOYMENT` 可以改；设成空或 `none` 就不叠加。目录不存在时，除了「执行环境不写按 Orca」，加载结果和没有这一层时完全一样。

```
~/.config/workshop/
├── deployment.yaml          可选
└── executors/
    └── claude-review/       目录名就是执行者名
        ├── executor.yaml    定义，字段和上面的配置档一样
        ├── settings.json    可选
        └── rules.md         可选
```

```yaml
# deployment.yaml，两项都可以不写
orca: true                   # 不写按 true；false 时这个部署的执行者全部在后台跑
roles:                       # 写了哪个角色，这个角色的候选和顺序就以它为准；没写的沿用仓库的
  writer: [orca-codex, claude-review]
  managers: [manager-codex]  # managers 只换项目经理的候选名单
```

规则：

- `executors/` 下每个带 `executor.yaml` 的子目录是一个配置档。和仓库里同名的，整个替换仓库那一套（字段不合并）。
- `roles` 只认 `writer`、`reviewer`、`acceptor`、`managers`，每项是执行者名列表；点到的执行者必须在仓库规则或部署目录里有定义。项目经理候选必须是订阅计费。
- 目录里有 `settings.json` 或 `rules.md` 时，按执行者目录隔离启动（不读本机个人配置，只读这个目录和活所在仓库的 `AGENTS.md`），两个都没有时不隔离。`executor.yaml` 里不许写 `executor_dir`（就是这个目录）；有这两个文件时不许再写 `config_dir`；Codex、Cursor 的壳不支持隔离，目录里放了这两个文件加载时报错。
- 以上任何一处不对，加载时报错，错误里有执行者名和文件名；车间不启动任何会话。

执行环境（`transport`）不写按 `orca`（Orca 的终端），仓库规则和部署目录都一样；写了 `headless` 的在后台跑。`deployment.yaml` 写 `orca: false` 时，所有执行环境是 Orca 的配置档（包括仓库里的）一律改按后台启动，启动命令不经过 Orca。注意后台启动沿用现在后台执行者的规矩：清部署凭据、不开工作区，所以 issue 交付的写代码阶段在 `orca: false` 的部署里起不来，只适合审查、验收和读资料的执行者。

### 加一个执行者

1. 建 `executors/<名字>/executor.yaml`，要隔离就放 `settings.json`、`rules.md`。可以从仓库的配置档复制：`rules/executors/orca-claude/` 的两个文件加上规则里 `orca-claude` 的字段（去掉 `executor_dir`）。
2. 要它接活，在 `deployment.yaml` 的 `roles` 里把它写进对应角色（写了就是整个名单）。
3. `workshop executors check` 只读地把仓库规则和部署目录完整加载、校验一遍，不启动执行者、不写状态库。每个执行者一行，制表符分隔：名字、壳、模型、执行环境、角色（没有写「—」）、来源（仓库或部署，同名替换算部署）。不过就打印错误，退出码非零。另有三种警告，不改退出码：状态库里还有在跑的轮、加载结果里却已没有的执行者；同名替换后原来隔离的执行者变成不隔离；状态库读不了。
4. `workshop executors reload` 先做同样的校验，不过就停下不重启；过了才 `launchctl kickstart -k` 重启守护进程，等到心跳 `heartbeat.json` 换了进程号、且最近一次成功晚于重启时刻才算成功。超时退出码非零：车间没起来，这种重启没有自动回退，要人看守护进程的错误日志处理。

### 撤下一个执行者

先从 `roles` 里去掉它（只改了 `roles` 时它的定义还在，只是不再接新活），`workshop executors reload`；等 `workshop status` 里它没有在跑的轮，再删它的目录，再 reload。直接删目录时，`check` 会对还有在跑的轮的执行者给警告；同名替换的执行者删掉目录后回到仓库那一套。

### 改了内容算新的一版

`executor.yaml`、`deployment.yaml` 只在车间启动时读，改了要重启（`workshop executors reload`）才生效。部署目录里一个执行者的 `executor.yaml`、`settings.json`、`rules.md` 任何一个内容变了，重启后它算新的一版：装配时按这三个文件的内容算出 `revision` 写进配置档，`workshop usage` 的「按执行者配置档」分成两行，它之后的轮开新会话、不接回旧会话。改之前已经启动的轮照启动时的内容跑完（启动命令连同规矩、设置都记在本轮目录），同一轮内进程重启也不读新内容。`settings.json`、`rules.md` 在每轮启动时才读，改完不重启的话新开的轮已经用上新内容、却还记在旧一版下，所以改完就 reload。仓库里的执行者目录（`rules/executors/`）没有 `revision`，内容变了不算新的一版，免得发版那一刻所有在途会话换新。`executor.yaml` 里不许自己写 `revision`。

读规则的入口（守护进程、命令行、项目经理入口、`scripts/` 里的脚本）都经同一个装配函数 `workshop.deployment.assemble` 取叠加后的规则；它只读仓库规则、叠部署目录、交核心校验，返回还没配置驱动的规则和每个配置档的来源。`load_rules` 在它之上配置驱动。`src/` 里别处不许直接调核心的 `Rules.load`（`scripts/check_boundaries.py` 拦）。测试一律把 `WORKSHOP_DEPLOYMENT` 设成 `none`（`tests/conftest.py`），要测叠加的测试显式给目录。

## 新增驱动

1. 在执行者插件内新增一个驱动文件，继承 `drivers.base.Driver`，定义 `family`、`binary`、支持字段的 `fields` 和 `argv`。按 CLI 协议覆盖环境、会话号识别、最终回答、用量、启动前检查等方法。
2. 在 `pyproject.toml` 的 `workshop.driver` 组注册工厂/类，之后在唯一规则文件中写配置档和候选。无需新增 `workshop.executor` 注册或改公共包装进程。

`argv` 接收执行者、已解析配置、目录、厂商会话号、是否 resume、本轮目录和信任目录。厂商自己生成会话号的驱动设 `allocated_session=True`，实现 `session_from_output` 或 `hook_session`/`recover_session`。后台需要 `-` 接 stdin 的设 `stdin_arg=True`。用量拿不到时返回未知，不填 0。

旧的 `workshop.executor` 入口仅为已有调用者兼容保留；默认配置仍从唯一规则文件读取。持久目录按配置档名，spec 记录驱动名，因此包装进程、hook、stop 和 usage 都能找到同一驱动。

参数核对依据：本机三家 CLI 的 `--help`；Codex 原生配置参见[配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。回归基线为 `f4193a965c803aa10c3c1da4ed50899fb74620b6`，启动和恢复 argv 固化在测试 fixture；仅归一化 Python、hook 和本轮目录的绝对路径。
