# issue 交付插件

入口点为 `workshop.flow/issue_delivery`、`workshop.source/github`。不添加依赖；外部命令在进程边界经 argv 调用，测试用独立的假可执行文件。交付对象规则仍是唯一配置入口。

## 资料与可信归件

GitHub 来源用 `gh api --include` 保存每个分页 endpoint 的 ETag、响应和下一页链接；304 重用缓存但不重复产生事件。游标与事件由核心同事务确认。认证的 304 不计主配额，仍可能受次级限流约束，见 [GitHub 条件请求说明](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests)。每 tick 默认最多 20 次请求、15 秒；分页和整仓扫描未完成时把已读取页记入游标，下次接着读，避免大仓库一次占住守护进程（可用 `github_requests_per_tick` / `github_poll_seconds` 调整）。初次扫描带标签的 issue，此后即使移除标签或关闭仍追踪评论。PR 的 `<!-- workshop-item: 活id -->` 标记指回原活。机器人、配置 `github_bot_logins` 中的身份、带 `<!-- workshop-comment: ... -->` 的程序评论均忽略。

GitHub 只有 `kun-g` 的标签事件可放行；评论和审查正文以独立一行的“不对”开头，后面为空或标点/空白加说明，才返工。引用、代码块或叙述中的“不对”不生效。CLI 自称 GitHub 的输入永远是普通更新。

飞书仅处理 `cli:feishu`，按 `om_` 消息号回查 [获取指定消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/get)，使用 `lark-cli --profile <规则> api GET /open-apis/im/v1/messages/<om_> --as bot`。这里使用原始消息接口，是为了核对 sender 的 id/type、chat_id、parent_id/root_id 和原始文本，避免消息展示快捷命令转换丢失引用。身份与群编号取规则，绝不信输入 `from_kun`、sender_id、quote 或 text。

真实文本先去开头 @Ash；“不对”按上面的固定写法返工，“继续”“可以继续”“说清楚了”“Ready for Agent”可放行。真实引用消息号经只读 `ask_kun_state`（默认 `~/.local/state/ask-kun/asks.json`）的 asks/notices 映射到 Q/N 编号，再经核心只读查询找所属活。`ask-kun list --all` 当前不提供 message_id，所以暂时依赖这个状态格式；将来有 `list --json` 再替换。并发写入造成读失败、JSON 不完整时返回未判定，下一 tick 重试；不猜归件，不报告插件故障。查到合法文件但没有匹配卡则按普通新资料处理。

飞书接单程序开 issue 时写活标记，先持久记创建意图，异常后按标记回查。不能确定是否创建成功且查不到时隔离，避免重发。核心绑定 issue 号别名；恢复未结算的 after 期间先完成别名，再轮询新来源，避免崩溃窗口里新开一件活。

## 证据与审查

说清楚时回查 issue 五项，并核对回执检查与 `workshop-checks` 内容一致，包含 `required_checks`（默认 `[["scripts/check.sh"]]`）。缺少标准时用 ask 或 notice+等你，Kun 重打标签后原活继续。

证据轮在独立 clone、detached 完整 SHA 上真实重跑登记检查；环境白名单清除 GitHub/SSH/云凭据，原样 stdout/stderr、退出码、SHA 写入核心证据 reason。每条检查默认限时 60 秒，可在目标配置 `check_timeout`；超时按暂时故障处理。这个检查没有业务写入，但会建立临时副本并运行仓库脚本；不是恶意代码安全沙箱。

审查副本独立 clone，不共享对象/配置；去掉 hooks、凭据助手，origin 改不可达地址，文件与目录去写位。`FlowBrief.workdir/readonly` 交给执行者，执行者负责启动环境去凭据。审查只读，必须改与建议分开；核心按改动路径和规则要求两家不同家族。头提交变了，证据、审查、合并守卫均拒绝推进。验收由核心排除写代码家族，插件核对原样输出与实际交付提交；写了亲眼看才要求 `visual-acceptance` 卡的 yes 答案。

## 交付和崩溃恢复

插件自己在 `issue_delivery_state_dir`（默认 `~/.local/state/workshop/issue_delivery`）保存每件活的 side-effect key、issue、证据提交、审过的提交、合并提交、tag 和验收记录。文件锁加原子替换；不读写核心数据库。每个 after key 重放返回原结果，远端动作还要回查实际状态，防止“远端成功，本地没记成功”的重复。

合并前回查同一 PR、审过的 head、全部 CI；`gh pr merge --match-head-commit` 保证提交不偷换。PR 正文拒绝自动关闭 issue 的引用，squash 提交正文由程序固定成 Related to，防止原提交留言意外关 issue。没有 CI 结果、失败或取消/跳过的检查不当作通过。

- `release: none`：合并后进入验收。
- `release: tag`：程序将合并提交打上 `vYYYY.MM.DD-HHMM`（北京时间）的注解 tag，推一个 tag。先回查远端已有 tag，绝不移动或覆盖。随后 after 返回 pending，下一轮仍在交付；执行者照目标仓库 `docs/runbooks/release.md` 完成发布。

grant-console 的集群授权只走 `~/workspace/runtime/repos/grant-console/scripts/request_grant.py`，`--requested-by` 带活 id；保存申请号，等待时回执 later 后退出，不复用过期授权，不换身份绕过。插件用 `deployment.app`（以及可选 `deployment.context`）只读回查 Argo 的 Synced/Healthy、正确仓库/production 分支/manifests 路径，并读取线上 production revision 的 `release.json`，要求 `source_commit` 等于本活合并提交才进验收。没指定 context 时只读查询用当前 Argo 上下文；部署写操作仍必须使用获批受限身份。

连续 pending 达 `pending_limit`（默认 5）核心只通知一次，不当执行失败。验收没过回执行；收尾只有已登记验收通过且有 leave 和通知才能关 issue，通知经核心通道发，之后不直接发消息。

## 手动只读验收（本轮未执行）

在仓库目录执行一次真实 poll，只打印事件，不保存游标、不写 GitHub：

```sh
uv run python - <<'PY'
from dataclasses import asdict
import json
from pathlib import Path
from workshop.core.rules import Rules
from workshop.plugins.sources.github import GitHubSource
rules = Rules.load(Path('rules/workshop.yaml'))
batch = GitHubSource().poll(rules.target('workshop'), None)
print(json.dumps([asdict(e) for e in batch.events], ensure_ascii=False, indent=2))
PY
```

测试覆盖假 gh/git/lark-cli/argocd 的 JSON 回放，以及本地真实 git 提交上的检查与只读副本。测试不连接 GitHub、飞书、Orca 或集群。
