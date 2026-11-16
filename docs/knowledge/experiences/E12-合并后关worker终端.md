---
id: E12
summary: Orca worker 的 PR 合并后，同一步关掉它的终端。
written_in: [docs/项目经理.md, docs/协调对话.md]
mechanism: 只有文字
status: 在用
approved: —
sources: [20261113-合并后worker终端还开着]
---

# E12 合并后关 worker 终端

## 怎么做

PR 合并、确认不再有修改轮后，同一步 `orca terminal close`。worktree 和交接文件保留。要再派活，用 resume 接回原会话或另起。车间自己起的执行者会话由车间收尾时关。

## 为什么

开着的终端占 Kun 的注意力，也分不清哪些还在干活。
