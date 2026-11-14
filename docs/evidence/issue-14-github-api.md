# #14 GitHub 更新时间实测

执行中的取证记录。实验用 `lambdatheory-agent[bot]`，不使用 Kun 的个人身份。

## 编辑 issue 评论

2026-11-13 的即时读取：编辑前 issue.updated_at 为 `16:32:32Z`；编辑返回的 comment.updated_at 为 `16:32:36Z`；紧接的 issue GET 仍为 `16:32:32Z`，since=`16:32:33Z` 的对象列表没有 #14，但仓库评论列表已有该评论。

随后车间读到 issue.updated_at=`16:32:36Z`，本轮再次读取也是该值。因此上轮“编辑评论不会更新 issue 时间”的结论不成立，只证明存在传播延迟。后续核实必须包含延迟回查。

Kun 已通过 `incremental-feeds=allow-feeds`（事实 7543）批准必要的仓库级增量补查。只有经实验证明必要的补查才加入，现有放行和返工规则保持。
