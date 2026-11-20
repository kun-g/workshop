# issue 交付插件

入口点为 `workshop.flow/issue_delivery`、`workshop.source/github`。不添加依赖；外部命令在进程边界经 argv 调用，测试用独立的假可执行文件。交付对象规则仍是唯一配置入口。

## 资料与可信归件

GitHub 来源用 `gh api --include` 在游标保存每个分页 endpoint 的 ETag、指纹和下一页链接；完整响应按内容指纹原子写入插件自己的 github_cache_dir（默认 ~/.local/state/workshop/github-responses）；304 重用缓存但不重复产生事件。游标与事件由核心同事务确认。认证的 304 不计主配额，仍可能受次级限流约束，见 [GitHub 条件请求说明](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#use-conditional-requests)。每 tick 默认最多 20 次请求、15 秒；分页和整仓扫描未完成时把已读取页记入游标，下次接着读，避免大仓库一次占住守护进程（可用 `github_requests_per_tick` / `github_poll_seconds` 调整）。初次扫描带标签且未关闭的 issue，此后即使移除标签或关闭仍追踪评论。PR 的 `<!-- workshop-item: 活id -->` 标记指回原活。机器人、配置 `github_bot_logins` 中的身份、带 `<!-- workshop-comment: ... -->` 的程序评论均忽略。

程序建单带 `workshop-intake` 标记，来源记录该 issue 初次标签事件 id 到 own_labels，此事件只算 update；只有之后 `kun-g` 再打的标签可放行。评论和审查正文以独立一行的“不对”开头，后面为空或标点/空白加说明，才返工。引用、代码块或叙述中的“不对”不生效。控制事件 ext_id 固定为仓库、类型、对象编号、评论/标签事件 id，不含内容指纹；评论编辑、表情变化只发 update，游标丢失后重放控制事件仍可由核心去重。CLI 自称 GitHub 的输入永远是普通更新。

**飞书群入口已停（#176）**：`cli:feishu` 事件归类为 discard（「飞书群入口已停，群消息不建活、不推进活」），不建活、不返工、不开 issue；工作台那头也不再把绑定车间的群消息转进来（oh-my-assistant 另一 PR）。需求和返工在协调对话里说。下面三段是停用前的做法，留作历史。

（历史）飞书仅处理 `cli:feishu`，按 `om_` 消息号回查 [获取指定消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/get)，使用 `lark-cli --profile <规则> api GET /open-apis/im/v1/messages/<om_> --as bot`。这里使用原始消息接口，是为了核对 sender 的 id/type、chat_id、parent_id/root_id 和原始文本，避免消息展示快捷命令转换丢失引用。身份与群编号取规则，绝不信输入 `from_kun`、sender_id、quote 或 text。

真实文本先去开头 @Ash。群内写法 `<short_name>#<issue号> 不对`（也接受无空格，如 `gc#12不对`） 先核对规则 feishu_chat_id，再经核心只读查已有活（含别名），核实发送者后才 rework，未知活丢弃。通知要求写出这个群内返工办法；例如 grant-console 为 `gc#123 不对`。引用卡回复可位于 Kun/Ash 私聊：真实引用消息号经只读 ask_kun_state 的 asks/notices 映射到 Q/N 编号，原消息 chat_id 必须等于这张卡记录的 chat_id，经核心回查所属活后，才识别“不对”或“继续”“可以继续”“说清楚了”“Ready for Agent”。`ask-kun list --all` 当前不提供 message_id，暂时依赖状态文件；读失败按查不到，不无限重试、不猜归件。核实不了 Kun、非目标群且无可核实卡片的消息返回 discard，记事实后不建活；同目标群的普通消息仍作为新资料。

飞书接单程序开 issue 时写活标记，先持久记创建意图，异常后按标记回查。不能确定是否创建成功且查不到时返回 pending，不重复创建，也不隔离整个流程。核心绑定 issue 号别名；恢复未结算的 after 期间先完成别名，再轮询新来源，避免崩溃窗口里新开一件活。

Orca 插话钩子去掉轮次令牌后以 `cli:orca` 进入：流程用核心已回查的活快照核对 `body.item_id` 与目标，归到该活，始终只记普通更新。原话即使包含“不对”或“继续”也不放行、不返工，不增加事件连锁深度，不改派或拦住正在运行的会话。

## 阶段（ws#215）

四段，只有执行起模型轮（issue交付 R13）：

- **接单（程序）**：回查 issue，核对合并前 DoD（逐项列表、无待定项）、「约束」（去掉空行后第一行单独是「无」，或至少一条列表项；不合报 `dod.CONSTRAINT`）、「要不要亲眼看」写了的第一行是/否、需求变更（`requirements.requirement_basis`：「背景和目标」里段首是「需求变更：」的第一段，没有时兼容读旧「依据」节；要写原文、规则 `requirement_docs` 登记的文档位置，「Kun 的原话」节有原话；只列编号的不核对）；写了上线后 DoD 的，每项要有 `workshop-checks`。要跑的检查不从 issue 读：`dod.required_checks` 取规则 `required_checks`，默认 `[["scripts/check.sh"]]`，格式在规则加载时由 `IssueDelivery.validate` 核对；接单、交付和派活说明都用它，活上以前存下的检查不再用。旧写法的「能跑的检查」「不可逆或对外的动作」「依据」不再要求（兼容：写了不报错，前两节不生效）。自动接单按正文摘要记下的核对结论带规则版本（`intake.RULES`），版本不同的不沿用，同一版正文按新规则再核对一次。缺一项就返回 `Evidence(awaits_update=True)`：活停在接单、状态「等资料」，next 写出缺哪几项，不派轮、不发卡；来源有任何新事件（改 issue 正文就有）时视图把活放回空闲，下一拍重核。飞书进来的需求先开 issue（只有原话），同样停在接单等写 issue 的会话补齐。代码见 `dod.py` 的 `registration`。
- **执行（模型一轮）**：从写代码做到 PR 去掉 Draft。交接以 PR 状态为准：assess 返回 `Evidence(handoff="交付")`，核心用它代替回执的 advance；PR 没有、已合并/关闭、还是 Draft、缺活标记或带自动关 issue 的语句，都判没过，同阶段重来（核心 `gate_retry`，同因 3 次记卡住）。
- **交付（程序）**：先查合并前 DoD——在干净副本重跑登记检查，`dod.evidence_problems` 核对 PR 正文「合并前 DoD」节逐项以 issue 合并前 DoD 每项第一行原文开头、项下附登记检查名（反引号）或原样输出代码块；需求变更的还要 PR 改了登记的需求文档。不过：记下原因和原样输出，`Evidence(handoff="执行")`，after 把 PR 改回 Draft（`gh pr ready --undo`）并在 PR 下留原样输出。过了：合并（沿用下面的合并守卫和 release 分支），写了上线后 DoD 的等上线（规则 `deployment`；车间自己是 `{kind: running}`，在跑的守护进程版本包含合并提交就算上线）后在干净副本对合并提交跑上线后检查；不过时在 issue 留原样输出、活退回执行，不回滚，修复开新 PR。合并失败和两段 DoD 的退回合计 3 次（`RETURN_LIMIT`），第 4 次记卡住；项目经理 `recover --mode rework` 后重新计。
- **收尾（程序）**：交付完成、子单全关就关 issue；收尾记录写进程序结果的 reason（合并提交、PR、两段 DoD 已过）。issue 写了「要亲眼看：是」的，收尾先起一轮按 issue 写的方式送一次（介入点服务的亲眼看送达，见介入点契约），Kun 答通过后程序收尾，答不通过退回执行。

不再有说清楚、证据、审查、验收轮。规则 `stages` 只能写 `[接单, 执行, 交付, 收尾]`。在途的活：闲着停在已去掉阶段的，核心按流程的 `stage_moves` 落位（说清楚→接单，证据、审查→交付，验收→已交付的进收尾、没交付完的回交付）；换代码时还在跑的旧阶段轮结算时判 superseded，活空闲后同样落位。没有一次性转换脚本。

总单（issue交付 R12）：关系汇合阶段是收尾，总单自己照常执行、交付，走到收尾还有子单没关时核心显示「等子单」、不派轮；插件在收尾的 assess 和 after 里再回查一次子单全关。

串行路径（`serial_paths`）现在只按 PR 的实际改动判：以前说清楚轮登记的「预计会改的路径」没有了，执行开始前不排队。

交付查合并前 DoD、上线后 DoD 都在干净副本（见下节）的 detached 完整 SHA 上真实重跑登记检查；环境白名单清除 GitHub/SSH/云凭据，原样 stdout/stderr、退出码、SHA 写入核心证据 reason。每条检查默认限时 60 秒，可在目标配置 `check_timeout`；超时记退出码 124、timed_out=true 和已得到的原样输出，判检查不过，不隔离流程。准备命令由 prepare 的 argv/cwd 指定，cwd 限定在干净副本中；grant-console 先在 services/devops-console 跑 uv sync --locked。这个检查没有业务写入，但会建立临时副本并运行仓库脚本；不是恶意代码安全沙箱。

## 干净副本、依赖缓存和执行者 worktree 怎么管

**副本**（ws#48，Kun 11-13：「这个 worktree 它本身不是已经隔离的吗？」）：检查、审查、验收用的副本都在交付对象主检出（规则 `checkout`）上开 detached worktree，不另 clone、不另建仓库。

- 开：`git -c core.hooksPath=/dev/null worktree add --detach <目录> <完整提交号>`。主检出里已有这个提交就不 fetch；没有才 `fetch --no-tags --no-write-fetch-head https://github.com/<repo>.git <sha>`，只取这一个提交，不写主检出的 FETCH_HEAD，不动它的分支、HEAD 和工作区。耗时随文件数涨，不随历史涨。
- 只含该提交的受跟踪文件，没有上一次的遗留。同一系统用户本来就能写任何文件，单独 clone 隔离不了多少，所以不再改 origin、不另做配置隔离；检查环境里用 `GIT_CONFIG_*` 把 `core.hooksPath` 设成 `/dev/null`，检查里跑的 git 也不执行主检出的钩子。凭据白名单（`clean_env`）照旧。
- 位置：检查副本在 `issue_delivery_state_dir/copies/check-*/repo`；检查进程的 HOME 兼 TMPDIR 不建在状态目录里，而是每轮新建的 `/tmp/workshop-check-*`，`copies/check-*/home` 是指过去的链接（ws#308：状态目录深时 TMPDIR 下拼的 unix socket 路径超过 macOS 的 104 字节上限，switchboard 的检查因此失败）。审查/验收只读副本在 `issue_delivery_state_dir/reviews/<活哈希>-<sha>`（先在临时目录开，再 `worktree move` 过去，最后去写位）。
- 收：检查不论通过、失败、超时还是准备命令出错，跑完都 `worktree remove --force`，删不掉（只读）就先恢复写位再删目录，然后 `worktree prune`。审查副本在轮停止后由 `cleanup` 收；daemon 启动时 `cleanup` 再清一遍：审查副本保留未停止轮的目录，检查副本超过 6 小时的视为进程被杀留下的，删掉（连同 `home` 链接指向的 `/tmp/workshop-check-*`）并收回登记。
- 同一主检出上的 fetch、worktree add/move/remove/prune 用 `issue_delivery_state_dir/copies/<主检出哈希>.lock` 串行；跑检查本身不持锁，几件活可以同时跑。

**依赖缓存**：检查仍在每轮新建的空 HOME、无凭据环境里跑，但副本里有 `uv.lock` 时，环境多一个 `UV_CACHE_DIR=issue_delivery_state_dir/uv-cache/<uv.lock 内容哈希前 16 位>`。选按锁文件分、不选只读共享缓存，是因为只读缓存要另有人负责预热；按锁文件分时，改了依赖的 PR 只写自己那一份，不会动到别的锁文件在用的缓存。同一锁文件的检查共用一份，第二次 `uv sync --locked` 不再下载（11-13 本机：冷缓存 2.7 秒，热缓存 0.14 秒）。每次取缓存时只留最近用过的 5 份，其余超过一天没用的删掉；要整个清，停掉车间后删 `uv-cache/` 即可，下次检查重新下载。没有 `uv.lock` 的仓库不设缓存，沿用空 HOME。

**Python 版本**：车间仓库自身提交 `.python-version`，固定 3.13。不能依赖工作区上层目录的版本文件：空 HOME 的检查副本位于状态目录，读不到 `/Users/kun/workspace/.python-version`，此前 `uv sync` 实际选到了 PATH 上的 3.14。复测耗时时应走 `IO.verify` 的同一套副本、空 HOME、缓存和 prepare；单看开发 worktree 的虚拟环境不足以证明流程里的耗时。

**执行者 worktree**：活收尾完成后，核心先问流程 `hold_workdir`：这件活还有打开的 PR 就留着，约一小时后再问。流程不留时，核心请为这件活开过会话的执行者 `release`。orca-claude 和 orca-codex 只认自己按活 id 开的 `ws-<哈希>` 目录：worktree 有未提交改动（不算被忽略的文件），或本地分支上有任何远端分支都没有的提交，就留着，核心给 Kun 发一条通知，不再重试；否则 `orca worktree rm` 删 worktree，再 `git branch -D` 删本地分支，活上的 workdir 清空。返工开新一轮时按同一个名字重新开 worktree。守护进程照常每一拍检查一遍，已收尾但没收过的老活（升级前收尾的）也按同一判断收。不是车间按活 id 开的 worktree（比如别的流程或人手开的）不碰。

上面的 `hold_workdir` 和 `release` 在守护进程中都由后台作业执行，一拍只提交调用或读取结果；同一次收回重启后认回，一小时后的复查和返工后的再次收尾用新的调用身份。见 [作业.md](作业.md)。

## 交付和崩溃恢复

插件自己在 `issue_delivery_state_dir`（默认 `~/.local/state/workshop/issue_delivery`）保存每件活的 side-effect key、issue、登记核对结果（registered、visual）、合并前 DoD 查过的提交（dod_head）、合并提交、tag、上线后检查结果（post_runs）、交付完成的提交（delivered）、待退回的原样输出（pending_return、last_return）和退回次数（returns）。文件锁加原子替换；不读写核心数据库。每个 after key 已成功结果重放返回原结果；pending 允许重查条件，远端动作还要回查实际状态，防止“远端成功，本地没记成功”的重复。

执行的派活说明带上上一次没交接或没过关的原因（gate_reason）和上次退回执行的原样输出（last_return）；上线后 DoD 退回的，说明里写明原 PR 已上线不回滚、从最新 main 开新 PR。

执行说明写明边界：只改本 issue 要的东西；遇到车间流程或环境的问题（检查在车间里跑不起来、关口不认等），交 `outcome=nothing`，summary 写清原因，evidence 的 JSON 写 `blocked={reason,next}`；不改车间自己的流程代码（本 issue 要改的除外），不动主检出和车间正在跑的代码，不重启车间。明确的 blocked 会停成卡住，不落进 nothing 的五分钟复看循环。11-13 ws#5 的执行者自己快进主检出、kickstart 守护进程，ws#18 的执行者在自己的活里重写了别的活的修正，这两件都由这段说明拦。

after 遇到缺 PR、头提交变化、未交付等业务不满足时返回 pending，而不是把所有同流程交付对象隔离。核心所有前进都要求 Evidence.passed；交付、收尾判 handoff="执行" 时往回走。

`io.run` 失败时错误信息带 stderr 结尾（没有 stderr 用 stdout，最多 400 字），解不了码的字节换成替代字符，不让坏输出变成插件崩溃。这些信息进 plugin_retry/plugin_failed 事实和故障通知，Kun 不用翻日志就能判断。

合并前回查同一 PR、合并前 DoD 查过的 head、规则 CI；`gh pr merge --match-head-commit` 保证提交不偷换。合并失败分三类，只有说不清原因的才算插件故障：

- 合并前就看得出要执行者动手的：PR 已关闭没合并、还是草稿、`mergeable=false` 或 `mergeable_state=dirty`（冲突）。交付程序步直接按合并前 DoD 退回执行：PR 改回 Draft、评论附原因和 GitHub 状态，不调 `gh pr merge`，不在交付里定时重查；执行者合最新 main、解冲突、重跑检查后推新提交、去掉 Draft，重新查合并前 DoD。
- `gh pr merge` 失败后回查 PR：已合并按成功走；回查到上面几种、或 `mergeable_state` 是 dirty/behind/blocked/unstable（冲突、落后 main、检查没过、分支保护），保存失败事实（PR、头提交、GitHub 状态和 gh 输出结尾），下一次交付程序步经同一个退回关口退回执行，不再尝试合并；合并失败与两段 DoD 共用退回上限，执行重新交接时清掉失败事实。头提交变了则重新查合并前 DoD；`mergeable` 为 null 或 `mergeable_state=unknown`（GitHub 没算完）时 pending、rerun=false，稍后回查。
- 回查看着能合并却合并失败（网络、GitHub 5xx 这类）：抛 Retryable，错误信息带 stderr；连续三次才由核心隔离。

11-13 ws#18 的 #23 和 main 冲突，`gh pr merge` exit 1 被当成 Retryable，重试三次后核心隔离了 issue_delivery，grant-console 和 workshop 一起停（事实 511–522）；按上面的分类，这次只会把 ws#18 退回执行。PR 标题和正文均拒绝自动关 issue 的语句（包括 `Closes: #7`），squash 正文固定 Related to。每个目标配置 `ci: none` 或 required check 名单；名单的所有项须 success/skipped/neutral 且至少一项 success，空结果、失败、取消或进行中均等待。等待 CI 用 rerun=false，不起执行者。

需要 bot 批准的目标配置 bot_review.login/signer。合并前 after(交付) 回查同一 head 的该 bot APPROVED review，已存在不再发；否则调用规则中的 agent_gh_token.py 签发，只在内存中用 token POST commit_id 绑定的 APPROVE，不落 token、不记录响应中的凭据。grant-console 配置 lambdatheory-agent[bot]，供主分支规则集的一票批准使用。

- `release: none`：合并即交付；issue 写了上线后 DoD 的，按规则 `deployment` 等上线后跑完上线后检查才算交付。
- `release: self`：交付对象就是车间自己（规则里 workshop 用这个，不按仓库名判断）。合并后在规则 `checkout`（主检出）里：`git status --porcelain` 为空（含未跟踪文件）且当前分支是 main 时，从 `https://github.com/<repo>.git` 取 main 到 `origin/main`，`git merge --ff-only <合并提交>`（已包含就跳过），再在主检出跑规则 `prepare`（workshop 是 `uv sync --locked`，依赖或入口点变了不装好，拉起后起不来）。都成功才返回 `After(restart=True)`：核心记下重启请求，这一拍照常结算并推进，`workshop run` 在这一拍结束后正常退出，launchd 的 KeepAlive 拉起新代码。执行者会话是独立进程组（`start_new_session`），守护进程退出不影响它们，重启后按 token 接回。主检出不干净、不在 main 或快进不了（例如主检出 main 上有本地提交、和合并提交分叉）时不动它、不重启；准备命令失败或没跑成时，主检出已经快进到新代码、环境可能只装了一半，同样不重启。两种都返回 `After(notice=…)`：核心经通道给 Kun 发一条不用回的通知（同一个完成钩子只发一次），写明合并提交、原因和要跑的命令；准备命令失败的那条会说主检出已是新代码、要重跑准备命令再重启，而且在那之前车间因别的原因重启会用新代码起来。合并提交照常记为交付完成。（规则里车间自己现在用 `release: none` 加整点定时发版，这一段是旧路径。）取 main 失败是网络故障，抛 Retryable。结果记在插件状态 `self_update`。
- `release: tag`：在插件状态目录 `issue_delivery_state_dir/releases/<目标及仓库的哈希>` 建普通专用 clone，沿用 Kun 的 git 凭据；fetch 后 detached 到合并 SHA，运行该提交里的 tag_command（grant-console 的 scripts/tag-release.sh）并传合并 SHA。checkout 规则仍只供读取主检出，发布操作不在该目录做。专用检出按目标加锁，避免两件活同时切 HEAD；首次 clone 在临时目录完成后原子就位。先查远端已有 tag，恢复不重复打 tag；不从去凭据的副本推送。tag 推完后定时回查线上版本包含合并提交，不再起执行者发版（ws#215 起交付是程序步；现在没有交付对象用 `release: tag`）。

线上回查命令由 deployment.context/namespace/app 指定，用 `kubectl --request-timeout=20s --context central-admin-agent -n argocd get application grant-console-centaladmin -o json`。要求 Synced/Healthy，deployment.revision_field（status.sync.revision）等于 deployment.branch（production）的远端头提交，并读取该 revision 的 deployment.release_file（release.json），通过 `git merge-base --is-ancestor` 确认合并提交被 deployment.source_field（source_commit）包含。再用 `kubectl --request-timeout=20s --context <deployment.context> -n <rollout.namespace> get deployment <rollout.name> -o json` 读 deployment.rollout 指定的 Deployment（grant-console 是 agent-ops/grant-console），按 kubectl rollout status 的判定要求换版完成：observedGeneration 不小于 generation、Progressing 不是 ProgressDeadlineExceeded、updatedReplicas 不少于 spec.replicas、replicas 等于 updatedReplicas、availableReplicas 不少于 updatedReplicas；规则没配 rollout 时回查不过。fetch 和 merge-base 均在同一专用检出中做，HEAD 仍 detached 到本活合并提交；不会给 Kun 的主检出留下 fetch 记录或本地 tag。允许之后的其他已交付提交，不要求源码 SHA 精确相等。

2026-11-13 只读实查：本机 Argo CLI 列出 argocd-central.hillingo.top、argocd.hillingo.top，但应用查询登录过期；没有改认证。经协调者确认改用上述 kubectl 只读路径，查到 source 为 git@github.com:kun-g/grant-console.git / production / manifests，revision 为 a7b09d7d2d39dcdeca0837f518708adf8ad421f8，Synced/Healthy。参数已写规则；查询未触发同步或发版。

pending=false 才放行；rerun=false 用定时工序和指数退避回查，rerun=true 让交付程序步重跑（不起模型轮）。同阶段连续 pending 达 pending_limit（默认 5；车间自己的 issue_delivery 为 24，够等整点发版）卡住并通知一次，不再循环。收尾判过时 Evidence.done_key 给合并提交号；两段 DoD 过了不通知 Kun（issue交付 R6）。

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

测试覆盖假 gh/git/lark-cli/kubectl/tag-release.sh 的 JSON 回放（默认在测试进程内调用同一份替身代码，省掉每次起解释器），以及本地真实 git 提交上的检查与只读副本。测试不连接 GitHub、飞书、Orca 或集群。跨块测试将只读副本通过 Brief.workdir 交给 Codex/Orca Claude 执行者插件，用假的 CLI 记录实际子进程 cwd/env，断言没有 GH_TOKEN、SSH_AUTH_SOCK 等凭据。

## GitHub 身份与会话放行（issue #31）

`targets.<对象>.github_identity` 配置身份：`inherit` 保持原来的身份链（grant-console）；`app` 指定 `signer` argv 和真实 `gh` 的绝对路径（workshop）。装配时验证配置。签发器接收 `--repo owner/repo`，stdout 只返回令牌；异常输出不进入诊断。内置 `python -m workshop.github_token` 从既有身份配置读取仓库白名单和权限，从 Keychain 读密钥，经内存管道签 JWT，取得仅限此仓库的 installation token，无磁盘缓存，也不修改旧身份配置。使用标准库与本机 security/openssl，无新增 Python 依赖。

执行者的 spec 只保存非秘密配置和仓库。runner 在实际启动 agent 前签发并装配环境；只读执行者继续清理全部部署凭据，不签写令牌。真实 gh 优先于全局身份 wrapper；git helper 只为 HTTPS github.com 上的同一仓库提供本进程令牌，不存凭据。签发失败时 runner 不起 agent，交等你回执；程序作业的签发失败通过通用 TargetBlocked 端口只隔离本交付对象、通知一次，不影响共用流程的其他交付对象。通知沿用 notices 开关。程序动作的令牌只在一次插件调用的内存范围内复用，离开即释放；返回的命令输出先遮去令牌。检查脚本仍使用干净环境。

`workshop release ws#31 --quote '放' --session <会话号>`（也支持 `workshop:31`）写入可信 session 放行事件；新对象从接单开始，同一对象不重复建活。原话和会话号不能为空，why 显示 channel=session、原话、会话号。外部 event 自报 session 不提升权限。已有卡住/等你的活按放行状态规则继续；接单保留原阶段，已有未答审批不被替代。


## 整点批次上线（issue #122）

`release: scheduled` 合并后不打 tag、不推 production、不启动发布执行者；只通过既有线上检查确认 Argo Synced/Healthy、production 源提交包含本次合并、Deployment rollout 完成，满足后记录 delivered（写了上线后 DoD 的再跑完上线后检查）并进收尾。同一版可以包含多件 issue 的合并提交，每件分别收尾。发布由独立 scheduled_release 流程处理，等待和恢复见[定时发版](定时发版.md)。
