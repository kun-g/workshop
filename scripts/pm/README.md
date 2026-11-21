# 项目经理的工具

项目经理手工干预车间时用的脚本，从 11-15 项目经理 6c36ee03 的临时目录挪进来。手册见 `docs/项目经理.md`。

每个工具都说明车间少一个机制：做成 `workshop` 命令或车间自己会处理之后，删掉对应的工具。每用一次，记进问题记录（`docs/knowledge/lessons/` 里带 `status` 的教训，见 `docs/knowledge/README.md`）。

改状态的工具直接调车间核心写线上状态库，不经过 `workshop` 命令。它们只动没在跑的活，原因记成「项目经理：…」，在 `workshop why` 里查得到。

在仓库根目录用 `uv run python scripts/pm/<工具>` 跑。

| 工具 | 什么时候用 | 缺的机制 |
|---|---|---|
| `unstick.py <活> <原因>` | 卡住的活，原因已排除，放回原阶段重排 | 解卡命令 |
| `reask.py <活> <原因>` | 问卡问错了或不用问了，撤掉问卡并放回原阶段 | 撤问命令 |
| `add_note.py <活> <键> <文件>` | 给下一轮补资料：执行回执里的证据、要交代的情况 | 加资料命令；验收直接拿到执行回执（ws#121） |
| `rework_done.py <活> <原话> <会话号>` | 已完成的活要返工，Kun 有原话 | `recover --mode rework` 收已完成的活 |
| `close_batch.py <批次的活> --production … --verified-by …` | 集群发版批次停在等授权或卡住，线上已只读核对无误；先加 `--dry-run` 看一遍 | gc#179；「核对后收批次」命令 |
| `watch.py --items … --expect … [--by HH:MM]` | 后台盯几件活：有该看的变化、或到了预期时间还没变化，就叫醒 | 车间按预期时间提醒项目经理 |
| `render_gc_card.py <申请记录.json>` | 发卡给 Kun 前，用 grant-console 线上渲染代码看第一屏（用 grant-console 的 venv 跑） | 发卡前自动检查第一屏 |
