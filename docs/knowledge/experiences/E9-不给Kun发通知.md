---
id: E9
summary: 不给 Kun 发通知；只在事先定好的介入点经介入点服务找他，要他拍板的一件事登记一个等待。
written_in: [AGENTS.md]
mechanism: '核心没有通知和问卡出口（#176 退役通道端口、回执 ask/notice）；消息只经介入点服务的送达命令，按介入点许可放行；上线后 `scripts/verify_intervention_live.py old-exits`、`exits --since-release` 核对'
status: 在用
approved: —
sources: [20261114-Ash发垃圾通知]
---

# E9 不给 Kun 发通知

## 怎么做

进度、完成、验收通过、插件故障，都不推给 Kun；卡住、故障、等你，由项目经理用 `workshop status` 自己发现。真要他定的，流程登记一个取舍等待（问题、理由、推荐、版本），Kun 在会话里答，协调对话用 `workshop answer-wait` 登记。能出消息的只有介入点契约「消息出口」表里那几种，都经介入点服务。

## 为什么

一天 18 条通知，几乎都不需要他做什么，他就不再看了，真要他定的事也跟着被淹掉。
