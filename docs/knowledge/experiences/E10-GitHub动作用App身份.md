---
id: E10
summary: GitHub 上的动作用 App 身份；kun-g 只代表 Kun 亲手做的事，agent 要以他的名义做只能走 gh-human 批准卡。
written_in: [AGENTS.md]
mechanism: '`workshop release`；agent 会话的 gh 和 git 只用 App（workspace `runtime/tools/agent_github_identity` 严格模式），拿不到 App 令牌就失败，不回落到 kun-g；SSH 连 GitHub 被拒'
status: 在用
approved: —
sources: [20261114-用kun-g替Kun操作]
---

# E10 GitHub 动作用 App 身份

## 怎么做

评论、改 issue、打标签、关 issue、开 PR、合并，都用 lambdatheory-agent。Kun 在会话里说「放」，用 `workshop release` 记原话。agent 会话里直接用 `gh` 和 git，身份包装器自动用 App；App 没装的仓库只能读。确实要以 Kun 的名义做的，用 `gh-human`，他在批准卡上批了才执行。

## 为什么

用 kun-g 替 Kun 操作，记录上分不清哪些是他本人做的。
