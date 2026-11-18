# 项目经理隔离验收记录（2026-11-18）

对应 #175；依据介入点 R11、R12、R13，核心 R12、R14、R20、R21、R23、C4。没有改需求、增加依赖、部署或重启现役，没有向飞书发送消息。

## 运行环境与边界

Python 3.13.14、当前工作树 `ws-727a6064a07fc111`；基线 `923a652a89d0ea7d0c965db81c8b056c28d26b81`。回放时实现还在工作树，心跳的 version 因此是基线，不代表本改动已经上线。GitHub 资料是 App 身份读取的 #175 最新正文；真实 Codex 使用 ChatGPT 订阅。回放中的问题、停止和点击均在隔离目录，外部送达端仅写本地 SQLite 收件箱。

程序计数用受控的真实子进程读取 issue 正文计算 SHA256，FIFO 保持采样窗口；它验证程序作业锁及心跳口径，不宣称已经验证生产流程的业务结果。普通 agent 和项目经理使用真实本地执行者；核心、看门、介入点服务都走各自真实命令入口。五分钟的边界推进假时钟，未等待墙钟五分钟。

## 原始输出

- [replay.jsonl](replay.jsonl)：完整回放，包括实际 argv、pid、会话、计数、接管、回执、请求响应、本地收件箱。
- [observed.jsonl](observed.jsonl)：从完整日志选取的心跳与会话字段，便于逐项看；未替换原始日志。
- [unavailable.jsonl](unavailable.jsonl)：两个真实空登录目录均被 `codex login status` 拒绝，候选耗尽；零 agent 启动、零按量调用、一次本地求助。
- [needs-action.jsonl](needs-action.jsonl)：真实订阅项目经理读取隔离状态后，提交「车间空了，要新需求」，本地收件箱一张卡；[empty-card.json](empty-card.json) 是其实际送达内容。
- [card.json](card.json)：合并两个已知结论的求助卡原始 JSON。
- [offline.json](offline.json)：最后一次用真实服务读取已停止守护进程的回卡；首屏只写原因，异常在详情。
- [check.json](check.json)：固定检查原样输出。

完整回放目录 `/private/tmp/ws175-li6_e44o`；不可用回放目录 `/private/tmp/ws175-unavailable-ss778lic`。空闲求助回放目录 `/private/tmp/ws175-unavailable-hpmrgbtk`。子进程结束后保留数据库和执行者日志。

```sh
uv run python scripts/verify_managers.py --issue /tmp/workshop-175-current.md --output .local/manager-evidence
uv run python scripts/verify_managers.py --unavailable --output .local/manager-evidence
uv run python scripts/verify_managers.py --needs-action --output .local/manager-evidence
scripts/check.sh
```

## 独立核对

独立 agent 核对了事件、真实进程、会话和原样输出：同一 episode 续接到 generation=2、两条事实均送达；看门与核心交接保留同一会话；只有项目经理时普通位占用为零；agent=1、程序作业=1、299/300 秒边界成立；终端、网页与等待栏回卡逐行相同；点击前后核心库未改变；同一空闲段仅一张求助卡；飞书发送为零。

复核发现并已补回归：启动认回前先收到回执会丢结论；业务空闲段变化未更新发布版本；单线程回卡回调等待服务会自锁；主请求解决而合并卡仍待发；单张坏卡挡住全局队列；父环境状态目录误继承。最后调整点击不唤醒核心、缺卡拒绝、异常路径折到详情，均由固定检查覆盖。求助卡副标题按需求原文修正为「项目经理处理不了」，重跑真实空闲求助和固定检查；完整回放保留修正前的原样卡片，最终卡见 empty-card.json。

上线后的现役检查由交付程序执行；工作台接线归 oh-my-assistant#148。真实飞书卡手机外观仍按 issue 的亲眼看约定验收。
