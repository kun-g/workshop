---
id: E10
summary: GitHub 上的动作用 App 身份；kun-g 只代表 Kun 亲手做的事。grant-console 的 issue 例外，正文写明代办。
written_in: [AGENTS.md]
mechanism: '`workshop release`；规则里各交付对象的 `github_identity`；agent 会话的 gh 包装器对 kun-g/workshop 走 App（`app_routed`）'
status: 在用
approved: —
sources: [20261114-用kun-g替Kun操作]
---

# E10 GitHub 动作用 App 身份

## 怎么做

评论、改 issue、打标签、关 issue、开 PR、合并，都用 lambdatheory-agent。Kun 在会话里说「放」，用 `workshop release` 记原话。签令牌的命令见 `docs/项目经理.md` 的疏通表。grant-console 的 issue 只能用 kun-g，正文写明是哪个会话代办。

## 为什么

用 kun-g 替 Kun 操作，记录上分不清哪些是他本人做的。
