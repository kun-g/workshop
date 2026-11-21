---
refs: gc#214、grant-console PR #215
status: 已开单 ws#325
---

# App作者的gcPR过不了CI和自审

11-18 起 agent 只用 App 身份，App 开的 grant-console PR 被 CI 跳过、审核结论也发不上去。

## 经过

- 11-18 19:45–21:01：gc#214（代执行卡不再显示写死风险分）停在车间之外：App 身份开的 PR #215（head 0badf267，CLEAN）因作者与审核方同为 app/lambdatheory-agent，审核结论发不上去（HTTP 422），CI check-and-build 因作者不是 kun-g 按设计跳过，owner 自动化自 11-12 暂停、负责会话终端不存在；写代码会话 await 循环空等。20:15 以 App 关 #215 并发 gh-human 卡 Q1900（以 kun-g 身份重开），20:59 无人回答超时，21:00 以 App 重开 #215 恢复原状。#215 最终由 kun-g 11-18 21:46 合并，gc#214 关闭
  - 证据：owner.py premerge 215 原文「作者是 app/lambdatheory-agent，CI 只放行 kun-g」「审核结论还没以 lambdatheory-agent[bot] 发到 PR 上」；review_watch status live=false、paused 自 11-12；gh pr view 215 mergedAt 2026-11-18T13:46:21Z、mergedBy kun-g；11-19 12:41 PR #255 同样 no checks reported
  - 影响到的活：gc#214、PR #215、App 作者 PR
  - 当时的状态：未处理（核于 11-20 夜：grant-console `ci.yml:45-57` 仍只放行 kun-g 作者；#215 之后没有 App 作者的 gc PR；没有对应单）

## 根因

两处：grant-console 的 `ci.yml` 只放行 kun-g 作者（CI 全 skipped，车间要求至少一个 success 才合并）；车间的 `bot_review` 用同一个 App 批准自己的 PR，GitHub 返回 422。

## 去向

Kun 11-21「P1 的都开单」。车间这一侧开 ws#325；grant-console 的 CI 由协调对话改好、Kun 选「你在自己终端跑两条命令」以 kun-g 推送开 PR（分支 `Kun/ci-app-author`）。
