# 车间（workshop）

一套自动运转的交付体系：事件进来变成一件活，活按阶段走到收尾，阶段之间有关口。核心管接单、阶段和关口、派活、等待、找 Kun 拍板、记录；每一类活、每个来源、执行者和通道都是装在核心上的插件。第一个插件是 issue 交付。

- 要求：[docs/requirements/核心.md](docs/requirements/核心.md)、[docs/requirements/issue交付.md](docs/requirements/issue交付.md)
- issue 写法：[docs/issue写法.md](docs/issue写法.md)（车间内外开 issue 都照这页）
- 用词：[GLOSSARY.md](GLOSSARY.md)
- 给 agent 的规矩：[AGENTS.md](AGENTS.md)

核心与命令行已实现；随包提供 ordered 挑选逻辑、GitHub 来源和 issue 交付流程；执行者与通道另行接入。Kun 11-13 定：第一版不接工单系统，TaskHome 端口保留为可选。

```sh
uv sync
scripts/check.sh
uv run workshop status
uv run workshop --rules /absolute/path/to/rules/workshop.yaml run --once
```

运行需要规则引用的插件入口点已安装；当前仓库提供 ordered、github、issue_delivery，其余端口需安装对应插件，直接 run 会将缺少插件的交付对象登记为故障。查看状态、历史和用量不要求插件存在。

- [插件接入契约](docs/implementation/ports.md)：五类端口、回执、幂等、恢复和看门读取方式。
- 状态目录：默认 `~/.local/state/workshop/`，用 `WORKSHOP_STATE_DIR` 改。
- `event`、`receipt <token>` 支持 `--json`、`--file` 或标准输入。
- `pause <target>` 暂停交付对象；`resume <target>` 列遗漏，同时带 `--select all|none|事实号,事实号 --seen 上次已看事实号` 才恢复（空清单填 none）。

本地离线演示（目录须为空）：

```sh
uv run python tests/demo.py .local/demo
WORKSHOP_STATE_DIR="$PWD/.local/demo" uv run workshop status
WORKSHOP_STATE_DIR="$PWD/.local/demo" uv run workshop why alpha:one
WORKSHOP_STATE_DIR="$PWD/.local/demo" uv run workshop usage
```

演示只用假插件，八轮从接单走到收尾，不调用网络、Orca 或飞书。

`abort <token>` 登记人工结束请求；执行者退出未交回执或超过规则期限也记失败并退避。`why` 的活可写 `<交付对象>:<对象键>` 或 `<短名>#<编号>`（短名取规则里的 `short_name`，如 `ws#5`），找不到时列出可用短名、退出码 2；每条事件只打一行摘要（流程实现 `describe` 时用它的说明），`--raw` 才原样打印资料。`why` 显示每轮实际模型、耗时、四种 token、API 折合花费和选择理由；`usage` 另按实际模型汇总，缺失值保持未知；主/子 agent 按实际模型分别计量，未读到子 agent 时显式标记。挑选逻辑可按对象/阶段覆盖，候选先由核心落实家族、额度和按量授权约束。

- [issue 交付接入与手动验收](docs/implementation/issue-delivery.md)：回源身份核验、检查、发版等待与幂等。
