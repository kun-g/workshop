2026-11-13 本机会话格式的脱敏缩小片段；消息内容与会话标识已移除/改名，token 数缩小方便手算。
Claude: assistant.message.id/model/usage，cache_creation TTL 细分；子会话 isSidechain/agentId。
Codex: session_meta.source.subagent.thread_spawn.parent_thread_id，turn_context.model，event_msg token_count.info。
保留旧轮、同一消息多块/重复 token_count，验证接回增量与去重；不包含提示词或凭据。

claude-synthetic.jsonl 从本机真实 Claude Code assistant `<synthetic>` 消息裁出：保留原 model/usage 字段（用量均为 0），只改时间与 ID、移除正文。用于验证占位消息不污染本轮真实模型与费用。
