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

阶段开关：规则里交付对象写 `stages`（按顺序列出阶段）时，`stages()` 照它返回；本流程只允许省掉审查，少了证据或交付就报错。省掉审查时，能合并的头提交是证据检查过的 `evidence_head`；合并前若配了 bot_review，在交付的 after 里补一条 bot 批准，正文写明「未审查」。核心按全局阶段顺序取规则里还在的下一阶段：规则改掉某阶段时，在跑的那一轮交回的推进仍认；闲着的活派轮前直接挪到下一阶段，不起轮。

说清楚时回查 issue 五项，并核对回执检查与 `workshop-checks` 内容一致，包含 `required_checks`（默认 `[["scripts/check.sh"]]`）。回执 JSON 的 `paths` 登记预计会改的路径（仓库根起的相对路径，目录以 / 结尾），作为说清楚的 Evidence.paths 交给核心，规则配了 `serial_paths` 时按它在执行前排队；不是字符串列表时说清楚不过。之后各阶段的实际改动取 PR 文件列表，改名的文件新旧路径都算（冻结、串行、两家审都按它判）。缺少标准时用 ask 或 notice+等你，Kun 重打标签后原活继续。

证据轮在干净副本（见下节）的 detached 完整 SHA 上真实重跑登记检查；环境白名单清除 GitHub/SSH/云凭据，原样 stdout/stderr、退出码、SHA 写入核心证据 reason。每条检查默认限时 60 秒，可在目标配置 `check_timeout`；超时记退出码 124、timed_out=true 和已得到的原样输出，判检查不过，不隔离流程。准备命令由 prepare 的 argv/cwd 指定，cwd 限定在干净副本中；grant-console 先在 services/devops-console 跑 uv sync --locked。这个检查没有业务写入，但会建立临时副本并运行仓库脚本；不是恶意代码安全沙箱。

审查和验收副本同样是主检出上的 detached worktree（见下节），文件与目录去写位。`FlowBrief.workdir/readonly` 交给执行者，执行者负责启动环境去凭据。审查只读，必须改与建议分开；核心按改动路径和规则要求两家不同家族。审查轮结束并停止执行者后清理副本；daemon 启动清残留时保留未停止轮的目录。头提交变了，证据、审查、合并守卫均拒绝推进。验收由核心排除写代码家族，插件核对原样输出与实际交付提交。验收轮的 `FlowBrief` 给合并提交的只读副本；生成说明时在干净副本对合并提交重跑登记检查（按提交号缓存为 merge_runs，只跑一次），原样附进说明，因为验收者是只读会话，跑不了命令，也不能落在停在 PR 头的活 worktree 里。写了亲眼看才要求 `visual-acceptance` 卡的 yes 答案。

## 干净副本、依赖缓存和执行者 worktree 怎么管

**副本**（ws#48，Kun 11-13：「这个 worktree 它本身不是已经隔离的吗？」）：检查、审查、验收用的副本都在交付对象主检出（规则 `checkout`）上开 detached worktree，不另 clone、不另建仓库。

- 开：`git -c core.hooksPath=/dev/null worktree add --detach <目录> <完整提交号>`。主检出里已有这个提交就不 fetch；没有才 `fetch --no-tags --no-write-fetch-head https://github.com/<repo>.git <sha>`，只取这一个提交，不写主检出的 FETCH_HEAD，不动它的分支、HEAD 和工作区。耗时随文件数涨，不随历史涨。
- 只含该提交的受跟踪文件，没有上一次的遗留。同一系统用户本来就能写任何文件，单独 clone 隔离不了多少，所以不再改 origin、不另做配置隔离；检查环境里用 `GIT_CONFIG_*` 把 `core.hooksPath` 设成 `/dev/null`，检查里跑的 git 也不执行主检出的钩子。凭据白名单（`clean_env`）照旧。
- 位置：检查副本在 `issue_delivery_state_dir/copies/check-*/repo`，审查/验收只读副本在 `issue_delivery_state_dir/reviews/<活哈希>-<sha>`（先在临时目录开，再 `worktree move` 过去，最后去写位）。
- 收：检查不论通过、失败、超时还是准备命令出错，跑完都 `worktree remove --force`，删不掉（只读）就先恢复写位再删目录，然后 `worktree prune`。审查副本在轮停止后由 `cleanup` 收；daemon 启动时 `cleanup` 再清一遍：审查副本保留未停止轮的目录，检查副本超过 6 小时的视为进程被杀留下的，删掉并收回登记。
- 同一主检出上的 fetch、worktree add/move/remove/prune 用 `issue_delivery_state_dir/copies/<主检出哈希>.lock` 串行；跑检查本身不持锁，几件活可以同时跑。

**依赖缓存**：检查仍在每轮新建的空 HOME、无凭据环境里跑，但副本里有 `uv.lock` 时，环境多一个 `UV_CACHE_DIR=issue_delivery_state_dir/uv-cache/<uv.lock 内容哈希前 16 位>`。选按锁文件分、不选只读共享缓存，是因为只读缓存要另有人负责预热；按锁文件分时，改了依赖的 PR 只写自己那一份，不会动到别的锁文件在用的缓存。同一锁文件的检查共用一份，第二次 `uv sync --locked` 不再下载（11-13 本机：冷缓存 2.7 秒，热缓存 0.14 秒）。每次取缓存时只留最近用过的 5 份，其余超过一天没用的删掉；要整个清，停掉车间后删 `uv-cache/` 即可，下次检查重新下载。没有 `uv.lock` 的仓库不设缓存，沿用空 HOME。

**执行者 worktree**：活收尾完成后，核心先问流程 `hold_workdir`：这件活还有打开的 PR 就留着，约一小时后再问。流程不留时，核心请为这件活开过会话的执行者 `release`。orca-claude 和 orca-codex 只认自己按活 id 开的 `ws-<哈希>` 目录：worktree 有未提交改动（不算被忽略的文件），或本地分支上有任何远端分支都没有的提交，就留着，核心给 Kun 发一条通知，不再重试；否则 `orca worktree rm` 删 worktree，再 `git branch -D` 删本地分支，活上的 workdir 清空。返工开新一轮时按同一个名字重新开 worktree。守护进程照常每一拍检查一遍，已收尾但没收过的老活（升级前收尾的）也按同一判断收。不是车间按活 id 开的 worktree（比如别的流程或人手开的）不碰。

## 重走验收

已合并的活验收没过、回到执行后，执行者判断问题不在代码（例如验收者拿不到合并提交、只读环境跑不了检查），可以不开 PR 直接回验收，同一合并提交只给一次。

- 什么时候给这条路：活到过验收（`item.history` 里有验收轮），本活没有打开的 PR，最近一次合并的带活标记 PR 存在，而且这个合并提交交付完成过。合并提交取 GitHub 上该 PR 的 `merge_commit_sha`，不只靠插件状态：执行阶段开新 PR 时会清掉状态里的 `merge`，新 PR 后来被关掉时（11-13 的 ws#18 和 #23）仍能找回上一次交付。
- 执行说明：满足上面的条件时写出 PR 号、合并提交，以及两条路：要改代码照常开新 PR；不用改就交下面的回执。这个合并提交已经重走过时，改写成「不能再走，交 nothing、notice、leave 等你」。
- 回执：`outcome=done`、`advance="验收"`，evidence 里 issue-delivery/v1 的 JSON 加 `rerun_acceptance={"merge": "<合并提交>", "reason": "为什么不用改"}`。
- 交付完成按提交号记：after(交付) 返回不 pending 的结果时，把合并提交加进插件状态 `delivered`。release=none/self 合并后就算；release=tag 要线上核对通过（`_deployed`）才走到这一步，合并了没上线不算。旧版本写的状态没有 `delivered`：none/self 按合并算，tag 当场核对线上是否包含该提交（ws#5、ws#18 是 release=none 时合并的，按合并算）。
- assess(执行)：回执没写 `merge`、有打开的 PR、没有合并过、`merge` 不是最近的合并提交、最近的合并提交没交付完成、这个提交已验收通过、或已在 `acceptance_reruns` 里，都判不过，理由写清原因和下一步。审查 PR #25 时复现过的反例：A 上线后验收没过，执行者改出 B 合并了但还没上线，交付轮退回执行后请求重走 B——现在判「还没交付完成」，请求 A 判「不是最近的合并提交」，都进不了验收。
- assess(验收)：除了验收输出和交付提交一致，还要求这个提交交付完成过，防止别的路把没上线的提交送进验收。
- 核心关口：执行提议直接到验收，只有活到过验收、且流程证据过了才放行（核心改动，见 [ports.md](ports.md)）；没到过验收照旧「不能越过流程阶段和关口」。
- after(执行)：结算时按回执请求的提交再查一遍（不改选别的提交），把它记进 `acceptance_reruns`，`merge`/`pr` 改成该 PR，`acceptance_after` 移到当前最新资料，清掉 `accepted` 和 `merge_runs`（流程在交付提交上的检查也重跑一次，环境问题可能就出在那一步）。返回空的 `After()`，核心把活推到验收。
- 第二次再不过：说明里已写明交 nothing + notice + leave 等你，由 Kun 重打标签放行，或在 issue 下评论「不对」（workshop 规则还没配 feishu_chat_id 和 kun_feishu_open_id，群里的「ws#X 不对」认不出来，配上之前只能走 GitHub）；执行者若还提议回验收，assess 每次判不过，同一理由连拦 3 次核心发问卡。都不会无限循环。

## 交付和崩溃恢复

插件自己在 `issue_delivery_state_dir`（默认 `~/.local/state/workshop/issue_delivery`）保存每件活的 side-effect key、issue、证据提交、审过的提交、合并提交、tag、验收记录、重走过验收的合并提交（acceptance_reruns）和自更新结果（self_update）。文件锁加原子替换；不读写核心数据库。每个 after key 已成功结果重放返回原结果；pending 允许重查条件，远端动作还要回查实际状态，防止“远端成功，本地没记成功”的重复。

派活说明带上上一轮 gate_reason；检查失败、头提交变化、合并冲突或 PR 已关闭没合并时（理由里都写「退回执行」），明确让执行者交 advance="执行"，不发问卡、不问 Kun。交付 assess 失败会写实际审过的 SHA 和当前 SHA；检查输出保留在 reason，供重派时定位。核心仍只处理通用关口，不新增业务退回规则。

执行说明写明边界：只改本 issue 要的东西；遇到车间流程或环境的问题（验收拿不到提交、检查在车间里跑不起来、关口不认等），交 `outcome=nothing`，summary 写清原因，notice 发 Kun 一条 fyi，leave 等你；不改车间自己的流程代码（本 issue 要改的除外），不动主检出和车间正在跑的代码，不重启车间。leave 等你是为了不落进 nothing 的五分钟复看循环。11-13 ws#5 的执行者自己快进主检出、kickstart 守护进程，ws#18 的执行者在自己的活里重写了别的活的修正，这两件都由这段说明拦。

after 遇到缺 PR、头提交变化、未验收等业务不满足时返回 pending，而不是把所有同流程交付对象隔离。核心所有前进都要求 Evidence.passed；显式退回执行、验收失败仍能往回走。

`io.run` 失败时错误信息带 stderr 结尾（没有 stderr 用 stdout，最多 400 字），解不了码的字节换成替代字符，不让坏输出变成插件崩溃。这些信息进 plugin_retry/plugin_failed 事实和故障通知，Kun 不用翻日志就能判断。

合并前回查同一 PR、审过的 head、规则 CI；`gh pr merge --match-head-commit` 保证提交不偷换。合并失败分三类，只有说不清原因的才算插件故障：

- 合并前就看得出要执行者动手的：PR 已关闭没合并、还是草稿、`mergeable=false` 或 `mergeable_state=dirty`（冲突）。不调 `gh pr merge`，pending + rerun，理由写「退回执行」，交付轮执行者照说明交 advance="执行"，到执行阶段合 main、解冲突。
- `gh pr merge` 失败后回查 PR：已合并按成功走；头提交变了、或回查到上面几种、或 `mergeable_state` 是 dirty/behind/blocked/unstable（冲突、落后 main、检查没过、分支保护），同样退回执行，理由附 gh 的 stderr 结尾；`mergeable` 还是 null/unknown（GitHub 没算完）时 pending、rerun=false，稍后回查。
- 回查看着能合并却合并失败（网络、GitHub 5xx 这类）：抛 Retryable，错误信息带 stderr；连续三次才由核心隔离。

11-13 ws#18 的 #23 和 main 冲突，`gh pr merge` exit 1 被当成 Retryable，重试三次后核心隔离了 issue_delivery，grant-console 和 workshop 一起停（事实 511–522）；按上面的分类，这次只会把 ws#18 退回执行。PR 标题和正文均拒绝自动关 issue 的语句（包括 `Closes: #7`），squash 正文固定 Related to。每个目标配置 `ci: none` 或 required check 名单；名单的所有项须 success/skipped/neutral 且至少一项 success，空结果、失败、取消或进行中均等待。等待 CI 用 rerun=false，不起执行者。

需要 bot 批准的目标配置 bot_review.login/signer。after(审查) 回查同一 head 的该 bot APPROVED review，已存在不再发；否则调用规则中的 agent_gh_token.py 签发，只在内存中用 token POST commit_id 绑定的 APPROVE，不落 token、不记录响应中的凭据。grant-console 配置 lambdatheory-agent[bot]，供主分支规则集的一票批准使用。

- `release: none`：合并后进入验收。
- `release: self`：交付对象就是车间自己（规则里 workshop 用这个，不按仓库名判断）。合并后在规则 `checkout`（主检出）里：`git status --porcelain` 为空（含未跟踪文件）且当前分支是 main 时，从 `https://github.com/<repo>.git` 取 main 到 `origin/main`，`git merge --ff-only <合并提交>`（已包含就跳过），再在主检出跑规则 `prepare`（workshop 是 `uv sync --locked`，依赖或入口点变了不装好，拉起后起不来）。都成功才返回 `After(restart=True)`：核心记下重启请求，这一拍照常结算并推进到验收，`workshop run` 在这一拍结束后正常退出，launchd 的 KeepAlive 拉起新代码。执行者会话是独立进程组（`start_new_session`），守护进程退出不影响它们，重启后按 token 接回。主检出不干净、不在 main 或快进不了（例如主检出 main 上有本地提交、和合并提交分叉）时不动它、不重启；准备命令失败或没跑成时，主检出已经快进到新代码、环境可能只装了一半，同样不重启。两种都返回 `After(notice=…)`：核心经通道给 Kun 发一条不用回的通知（同一个完成钩子只发一次），写明合并提交、原因和要跑的命令；准备命令失败的那条会说主检出已是新代码、要重跑准备命令再重启，而且在那之前车间因别的原因重启会用新代码起来。活照常进验收（验收在合并提交的只读副本上做，不依赖守护进程跑的版本），合并提交也记为交付完成。取 main 失败是网络故障，抛 Retryable。结果记在插件状态 `self_update`。
- `release: tag`：在插件状态目录 `issue_delivery_state_dir/releases/<目标及仓库的哈希>` 建普通专用 clone，沿用 Kun 的 git 凭据；fetch 后 detached 到合并 SHA，运行该提交里的 tag_command（grant-console 的 scripts/tag-release.sh）并传合并 SHA。checkout 规则仍只供读取主检出，发布操作不在该目录做。专用检出按目标加锁，避免两件活同时切 HEAD；首次 clone 在临时目录完成后原子就位。先查远端已有 tag，恢复不重复打 tag；不从去凭据的副本推送。tag 推完后 rerun=true，执行者照规则 `release_runbook` 指向的手册完成发布：交付说明生成时经 GitHub contents 接口查合并提交里有没有这个文件，有就写「照交付对象 <路径>（<合并提交>上的版本）」；GitHub 明确答 404 才原样用 `release_runbook_fallback`，503、超时等查不到不算不存在，抛 Retryable 由核心退避重试后再派活（grant-console 在 #143 合并前是「照 issue kun-g/grant-console#143 正文的步骤」）。说明里写明上线批准卡和集群授权卡都要 Kun 批，批了到生效有延迟要轮询；换版不删 Pod，只靠 rollout。

grant-console 的 ECR/render/push-production/sync 仍由执行者申请集群授权后做；只用仓库 scripts/request_grant.py，requested-by 带活 id，保存申请号，不复用过期授权、不换身份。真正 sync 完且确认上线后才给 release_submitted=true；还在等授权卡的回执只给 later，不带 advance 或 release_submitted，之后转 rerun=false，只定时核对线上状态。

线上回查命令由 deployment.context/namespace/app 指定，用 `kubectl --request-timeout=20s --context central-admin-agent -n argocd get application grant-console-centaladmin -o json`。要求 Synced/Healthy，deployment.revision_field（status.sync.revision）等于 deployment.branch（production）的远端头提交，并读取该 revision 的 deployment.release_file（release.json），通过 `git merge-base --is-ancestor` 确认合并提交被 deployment.source_field（source_commit）包含。再用 `kubectl --request-timeout=20s --context <deployment.context> -n <rollout.namespace> get deployment <rollout.name> -o json` 读 deployment.rollout 指定的 Deployment（grant-console 是 agent-ops/grant-console），按 kubectl rollout status 的判定要求换版完成：observedGeneration 不小于 generation、Progressing 不是 ProgressDeadlineExceeded、updatedReplicas 不少于 spec.replicas、replicas 等于 updatedReplicas、availableReplicas 不少于 updatedReplicas；规则没配 rollout 时回查不过。fetch 和 merge-base 均在同一专用检出中做，HEAD 仍 detached 到本活合并提交；不会给 Kun 的主检出留下 fetch 记录或本地 tag。允许之后的其他已交付提交，不要求源码 SHA 精确相等。

2026-11-13 只读实查：本机 Argo CLI 列出 argocd-central.hillingo.top、argocd.hillingo.top，但应用查询登录过期；没有改认证。经协调者确认改用上述 kubectl 只读路径，查到 source 为 git@github.com:kun-g/grant-console.git / production / manifests，revision 为 a7b09d7d2d39dcdeca0837f518708adf8ad421f8，Synced/Healthy。参数已写规则；查询未触发同步或发版。

pending=false 才放行；rerun=false 用定时工序和指数退避回查，rerun=true 才接回执行者。同阶段连续 pending 达 pending_limit（默认 5）卡住并通知一次，不再循环。验收不过回执行；收尾只有验收记录、leave、含群内返工活名的通知才能关 issue，通知经核心通道发。验收通过或收尾判过时 Evidence.done_key 给合并提交号，核心按它对每个合并提交只发一张完成卡：验收通过当轮或收尾轮的第一条出卡，随后同一提交多发的、换 key 的都不出卡，只记进 why。各阶段说明（收尾除外）写明不发进度通知，notice 只在留下等你或验收通过当轮发。

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

测试覆盖假 gh/git/lark-cli/kubectl/tag-release.sh 的 JSON 回放（默认在测试进程内调用同一份替身代码，省掉每次起解释器），以及本地真实 git 提交上的检查与只读副本。测试不连接 GitHub、飞书、Orca 或集群。跨块测试将流程生成的只读审查副本通过 Brief.workdir 交给 Codex/Orca Claude 执行者插件，用假的 CLI 记录实际子进程 cwd/env，断言没有 GH_TOKEN、SSH_AUTH_SOCK 等凭据。
