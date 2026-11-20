# 推拉共用归一键的固件（核心 R37）

`rest-*.json` 是 2026-11-21 用 App 身份 `gh api` 只读取回的 kun-g/workshop 原样响应：

- `rest-issue-315.json`：`GET repos/kun-g/workshop/issues/315`
- `rest-issue-315-events.json`：`GET repos/kun-g/workshop/issues/315/events`（含 2026-11-20T13:04:36Z 的 `labeled`）
- `rest-issue-168.json`：`GET repos/kun-g/workshop/issues/168`
- `rest-comment-6969514937.json`：`GET repos/kun-g/workshop/issues/comments/6969514937`（kun-g 在 #168 的评论）

`webhook-*.json` 是同一次变化的 webhook 载荷，按 GitHub webhook 的字段（`action`、`issue`、`label`/`comment`、
`repository`、`sender`）用上面同一批真实对象拼成：这一单不对外开端口（#315 约束），车间还收不到 GitHub 的真实投递。
`repository` 来自 `GET repos/kun-g/workshop`，`sender` 是 labeled 事件的 `actor`、评论的 `user`。
