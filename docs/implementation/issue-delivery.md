# issue 交付插件

入口点为 `workshop.flow/issue_delivery`、`workshop.source/github`。不添加依赖；外部命令在进程边界经 argv 调用，测试用独立的假可执行文件。交付对象规则仍是唯一配置入口。

## 资料与可信归件

GitHub 来源用 `gh api --include` 在游标保存每个分页 endpoint 的 ETag、指纹和下一页链接；完整响应按内容指纹原子写入插件自己的 github_cache_dir（默认 ~/.local/state/workshop/github-responses）；304 重用缓存但不重复产生事件。游标与事件由核心同事务确认。认证的 304 不计主配额，仍可能受次级限流约束，见 [GitHub 条件请求说明](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests)。每 tick 默认最多 20 次请求、15 秒；分页和整仓扫描未完成时把已读取页记入游标，下次接着读，避免大仓库一次占住守护进程（可用 `github_requests_per_tick` / `github_poll_seconds` 调整）。初次扫描带标签且未关闭的 issue，此后即使移除标签或关闭仍追踪评论。PR 的 `<!-- workshop-item: 活id -->` 标记指回原活。机器人、配置 `github_bot_logins` 中的身份、带 `<!-- workshop-comment: ... -->` 的程序评论均忽略。

程序建单带 `workshop-intake` 标记，来源记录该 issue 初次标签事件 id 到 own_labels，此事件只算 update；只有之后 `kun-g` 再打的标签可放行。评论和审查正文以独立一行的“不对”开头，后面为空或标点/空白加说明，才返工。引用、代码块或叙述中的“不对”不生效。控制事件 ext_id 固定为仓库、类型、对象编号、评论/标签事件 id，不含内容指纹；评论编辑、表情变化只发 update，游标丢失后重放控制事件仍可由核心去重。CLI 自称 GitHub 的输入永远是普通更新。

飞书仅处理 `cli:feishu`，按 `om_` 消息号回查 [获取指定消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/get)，使用 `lark-cli --profile <规则> api GET /open-apis/im/v1/messages/<om_> --as bot`。这里使用原始消息接口，是为了核对 sender 的 id/type、chat_id、parent_id/root_id 和原始文本，避免消息展示快捷命令转换丢失引用。身份与群编号取规则，绝不信输入 `from_kun`、sender_id、quote 或 text。

真实文本先去开头 @Ash。群内写法 `<short_name>#<issue号> 不对`（也接受无空格，如 `gc#12不对`） 先核对规则 feishu_chat_id，再经核心只读查已有活（含别名），核实发送者后才 rework，未知活丢弃。通知要求写出这个群内返工办法；例如 grant-console 为 `gc#123 不对`。引用卡回复可位于 Kun/Ash 私聊：真实引用消息号经只读 ask_kun_state 的 asks/notices 映射到 Q/N 编号，原消息 chat_id 必须等于这张卡记录的 chat_id，经核心回查所属活后，才识别“不对”或“继续”“可以继续”“说清楚了”“Ready for Agent”。`ask-kun list --all` 当前不提供 message_id，暂时依赖状态文件；读失败按查不到，不无限重试、不猜归件。核实不了 Kun、非目标群且无可核实卡片的消息返回 discard，记事实后不建活；同目标群的普通消息仍作为新资料。

飞书接单程序开 issue 时写活标记，先持久记创建意图，异常后按标记回查。不能确定是否创建成功且查不到时返回 pending，不重复创建，也不隔离整个流程。核心绑定 issue 号别名；恢复未结算的 after 期间先完成别名，再轮询新来源，避免崩溃窗口里新开一件活。

Orca 插话钩子去掉轮次令牌后以 `cli:orca` 进入：流程用核心已回查的活快照核对 `body.item_id` 与目标，归到该活，始终只记普通更新。原话即使包含“不对”或“继续”也不放行、不返工，不增加事件连锁深度，不改派或拦住正在运行的会话。

## 证据与审查

说清楚时回查 issue 五项，并核对回执检查与 `workshop-checks` 内容一致，包含 `required_checks`（默认 `[["scripts/check.sh"]]`）。缺少标准时用 ask 或 notice+等你，Kun 重打标签后原活继续。

证据轮在独立 clone、detached 完整 SHA 上真实重跑登记检查；环境白名单清除 GitHub/SSH/云凭据，原样 stdout/stderr、退出码、SHA 写入核心证据 reason。每条检查默认限时 60 秒，可在目标配置 `check_timeout`；超时记退出码 124、timed_out=true 和已得到的原样输出，判检查不过，不隔离流程。准备命令由 prepare 的 argv/cwd 指定，cwd 限定在干净副本中；grant-console 先在 services/devops-console 跑 uv sync --locked。这个检查没有业务写入，但会建立临时副本并运行仓库脚本；不是恶意代码安全沙箱。

审查副本独立 clone，不共享对象/配置；去掉 hooks、凭据助手，origin 改不可达地址，文件与目录去写位。`FlowBrief.workdir/readonly` 交给执行者，执行者负责启动环境去凭据。审查只读，必须改与建议分开；核心按改动路径和规则要求两家不同家族。审查轮结束并停止执行者后清理副本；daemon 启动清残留时保留未停止轮的目录。头提交变了，证据、审查、合并守卫均拒绝推进。验收由核心排除写代码家族，插件核对原样输出与实际交付提交；写了亲眼看才要求 `visual-acceptance` 卡的 yes 答案。

## 交付和崩溃恢复

插件自己在 `issue_delivery_state_dir`（默认 `~/.local/state/workshop/issue_delivery`）保存每件活的 side-effect key、issue、证据提交、审过的提交、合并提交、tag 和验收记录。文件锁加原子替换；不读写核心数据库。每个 after key 已成功结果重放返回原结果；pending 允许重查条件，远端动作还要回查实际状态，防止“远端成功，本地没记成功”的重复。

派活说明带上上一轮 gate_reason；检查失败或头提交变化时明确让执行者交 advance="执行"，不发问卡、不问 Kun。交付 assess 失败会写实际审过的 SHA 和当前 SHA；检查输出保留在 reason，供重派时定位。核心仍只处理通用关口，不新增业务退回规则。

after 遇到缺 PR、头提交变化、未验收等业务不满足时返回 pending，而不是把所有同流程交付对象隔离。核心所有前进都要求 Evidence.passed；显式退回执行、验收失败仍能往回走。

合并前回查同一 PR、审过的 head、规则 CI；`gh pr merge --match-head-commit` 保证提交不偷换。PR 标题和正文均拒绝自动关 issue 的语句（包括 `Closes: #7`），squash 正文固定 Related to。每个目标配置 `ci: none` 或 required check 名单；名单的所有项须 success/skipped/neutral 且至少一项 success，空结果、失败、取消或进行中均等待。等待 CI 用 rerun=false，不起执行者。

需要 bot 批准的目标配置 bot_review.login/signer。after(审查) 回查同一 head 的该 bot APPROVED review，已存在不再发；否则调用规则中的 agent_gh_token.py 签发，只在内存中用 token POST commit_id 绑定的 APPROVE，不落 token、不记录响应中的凭据。grant-console 配置 lambdatheory-agent[bot]，供主分支规则集的一票批准使用。

- `release: none`：合并后进入验收。
- `release: tag`：在插件状态目录 `issue_delivery_state_dir/releases/<目标及仓库的哈希>` 建普通专用 clone，沿用 Kun 的 git 凭据；fetch 后 detached 到合并 SHA，运行该提交里的 tag_command（grant-console 的 scripts/tag-release.sh）并传合并 SHA。checkout 规则仍只供读取主检出，发布操作不在该目录做。专用检出按目标加锁，避免两件活同时切 HEAD；首次 clone 在临时目录完成后原子就位。先查远端已有 tag，恢复不重复打 tag；不从去凭据的副本推送。tag 推完后 rerun=true，执行者照目标仓库 docs/runbooks/release.md 完成发布。

grant-console 的 ECR/render/push-production/sync 仍由执行者申请集群授权后做；只用仓库 scripts/request_grant.py，requested-by 带活 id，保存申请号，不复用过期授权、不换身份。真正 sync 完且确认上线后才给 release_submitted=true；还在等授权卡的回执只给 later，不带 advance 或 release_submitted，之后转 rerun=false，只定时核对线上状态。

线上回查命令由 deployment.context/namespace/app 指定，用 `kubectl --request-timeout=20s --context central-admin-agent -n argocd get application grant-console-centaladmin -o json`。要求 Synced/Healthy，deployment.revision_field（status.sync.revision）等于 deployment.branch（production）的远端头提交，并读取该 revision 的 deployment.release_file（release.json），通过 `git merge-base --is-ancestor` 确认合并提交被 deployment.source_field（source_commit）包含。fetch 和 merge-base 均在同一专用检出中做，HEAD 仍 detached 到本活合并提交；不会给 Kun 的主检出留下 fetch 记录或本地 tag。允许之后的其他已交付提交，不要求源码 SHA 精确相等。

2026-11-13 只读实查：本机 Argo CLI 列出 argocd-central.hillingo.top、argocd.hillingo.top，但应用查询登录过期；没有改认证。经协调者确认改用上述 kubectl 只读路径，查到 source 为 git@github.com:kun-g/grant-console.git / production / manifests，revision 为 a7b09d7d2d39dcdeca0837f518708adf8ad421f8，Synced/Healthy。参数已写规则；查询未触发同步或发版。

pending=false 才放行；rerun=false 用定时工序和指数退避回查，rerun=true 才接回执行者。同阶段连续 pending 达 pending_limit（默认 5）卡住并通知一次，不再循环。验收不过回执行；收尾只有验收记录、leave、含群内返工活名的通知才能关 issue，通知经核心通道发。

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

测试覆盖假 gh/git/lark-cli/kubectl/tag-release.sh 的 JSON 回放，以及本地真实 git 提交上的检查与只读副本。测试不连接 GitHub、飞书、Orca 或集群。跨块测试将流程生成的只读审查副本通过 Brief.workdir 交给 Codex/Orca Claude 执行者插件，用假的 CLI 记录实际子进程 cwd/env，断言没有 GH_TOKEN、SSH_AUTH_SOCK 等凭据。
