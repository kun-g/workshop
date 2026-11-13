# W1 核心交接

- 会话：`01a1d30b-e35c-7fe1-aa9a-2baf1ea5ebfc`。
- worktree：`/Users/kun/workspace/runtime/worktrees/workshop/ws-w1-core`。
- 分支：`Kun/ws-w1-core`（Orca 分配，沿用）。
- 接回：`cd /Users/kun/workspace/runtime/worktrees/workshop/ws-w1-core && codex resume 01a1d30b-e35c-7fe1-aa9a-2baf1ea5ebfc`。
- 派活：`task_33633b6256e5` / `ctx_90dfcb57da56`；协调者终端 `term_c0b07d59-1e10-47b9-94fa-0a7e969b7741`。

已实现核心日志/视图、四道关口、回执和轮、定时/退避、暂停恢复、插件隔离、五类端口和注册表、命令行、示例规则与检查脚本。下一波先看 [ports.md](ports.md)；这份文档包含必须持久幂等的启动/关闭/通知/工单/完成钩子约定，以及 SQLite 和心跳读取方式。

验证用 `scripts/check.sh`。`tests/demo.py <空目录>` 会生成真实走完八轮的 SQLite 状态，随后用 `WORKSHOP_STATE_DIR=<目录> uv run workshop status/why/usage` 读取。当前验收输出和演示数据库在 `.local/`（忽略提交），原样输出贴在本分支 PR。

没有实现或调用真实 GitHub、Orca、ask-kun、sb task 插件；没有实现独立看门程序，也未做线上接管；这些按派活范围由下一波完成。没有改需求文档、main，未合并。本单是 Orca 派活，没有可代报 done 的 sb task 单号；通过 orchestration 回报完成。

后续由另一家审查最终提交号，协调者决定是否合并。无需 Kun 在本单补设计决定。默认选择、依赖理由、实现边界详见 PR 正文。
