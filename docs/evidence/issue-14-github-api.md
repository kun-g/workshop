# #14 GitHub 更新时间实测

本轮使用 `lambdatheory-agent[bot]`。除本任务的实验评论、review、issue 正文和草稿 PR 外，未修改其他仓库对象。

## 结论与限制

- 编辑 issue 评论会异步更新 issue 时间。上轮即时读取未变化，后来时间已追上评论；此前“不会更新时间”的判断过早，现已更正。
- 提交 review 后，10 秒延迟回查的 issue 和 PR 时间已更新为提交时间。
- 编辑行内评论后，评论时间为 01:48:45Z，10 秒及数分钟后父对象时间仍为 01:48:32Z；仓库增量评论列表能返回它。需要补查 `pulls/comments?since=`，不能只靠对象变化列表。
- CI 行为尚未核实。授权中心仓库的 App 令牌只有 pull_requests:write、metadata:read；读取 check-runs 返回 403。车间仓库也未配置 CI。公开仓库匿名 API 备选读取遇到速率限制，未继续重试。
- Kun 已批准必要增量补查（事实 7543，incremental-feeds=allow-feeds）；同一 issue 标准已更新。保留事件格式、ext_id、放行/返工规则与核心边界。

## 原样命令与输出

以下是 gh 的 `--jq` 投影命令及其原样 stdout/stderr。投影保留本次比较所需字段；本轮完整记录另外保存在执行轮目录 `api-probes.json`。未把失败或未执行的 CI 实验记成通过。

### 2026-11-14T01:48:02.418402+00:00

```json
["gh", "api", "--method", "GET", "repos/kun-g/workshop/issues/14", "--jq", "{number,updated_at,state}"]
```

退出码：0

stdout：

```text
{"number":14,"state":"open","updated_at":"2026-11-13T16:32:36Z"}
```

stderr：

```text
```

### 2026-11-14T01:48:03.645299+00:00

```json
["gh", "api", "--method", "GET", "repos/kun-g/workshop/issues/comments/6858886816", "--jq", "{id,created_at,updated_at,user:.user.login}"]
```

退出码：0

stdout：

```text
{"created_at":"2026-11-13T16:32:32Z","id":6858886816,"updated_at":"2026-11-13T16:32:36Z","user":"lambdatheory-agent[bot]"}
```

stderr：

```text
```

### 2026-11-14T01:48:04.782001+00:00

```json
["gh", "api", "--method", "GET", "repos/kun-g/workshop/issues/72", "--jq", "{number,updated_at,state}"]
```

退出码：0

stdout：

```text
{"number":72,"state":"open","updated_at":"2026-11-14T01:47:45Z"}
```

stderr：

```text
```

### 2026-11-14T01:48:06.706077+00:00

```json
["gh", "api", "--method", "POST", "repos/kun-g/workshop/pulls/72/reviews", "--jq", "{id,state,submitted_at,user:.user.login}", "--input", "-"]
```

退出码：0

stdout：

```text
{"id":5900580429,"state":"COMMENTED","submitted_at":"2026-11-14T01:48:16Z","user":"lambdatheory-agent[bot]"}
```

stderr：

```text
```

### 2026-11-14T01:48:07.862012+00:00

```json
["gh", "api", "--method", "GET", "repos/kun-g/workshop/issues/72", "--jq", "{number,updated_at,state}"]
```

退出码：0

stdout：

```text
{"number":72,"state":"open","updated_at":"2026-11-14T01:47:45Z"}
```

stderr：

```text
```

### 2026-11-14T01:48:19.432769+00:00

```json
["gh", "api", "--method", "GET", "repos/kun-g/workshop/issues/72", "--jq", "{number,updated_at,state}"]
```

退出码：0

stdout：

```text
{"number":72,"state":"open","updated_at":"2026-11-14T01:48:16Z"}
```

stderr：

```text
```

### 2026-11-14T01:48:21.107685+00:00

```json
["gh", "api", "--method", "GET", "repos/kun-g/workshop/pulls/72", "--jq", "{number,updated_at,state}"]
```

退出码：0

stdout：

```text
{"number":72,"state":"open","updated_at":"2026-11-14T01:48:16Z"}
```

stderr：

```text
```

### 2026-11-14T01:48:23.022951+00:00

```json
["gh", "api", "--method", "POST", "repos/kun-g/workshop/pulls/72/comments", "--jq", "{id,created_at,updated_at,user:.user.login}", "--input", "-"]
```

退出码：0

stdout：

```text
{"created_at":"2026-11-14T01:48:32Z","id":4570694910,"updated_at":"2026-11-14T01:48:32Z","user":"lambdatheory-agent[bot]"}
```

stderr：

```text
```

### 2026-11-14T01:48:34.175705+00:00

```json
["gh", "api", "--method", "GET", "repos/kun-g/workshop/issues/72", "--jq", "{number,updated_at,state}"]
```

退出码：0

stdout：

```text
{"number":72,"state":"open","updated_at":"2026-11-14T01:48:32Z"}
```

stderr：

```text
```

### 2026-11-14T01:48:35.672521+00:00

```json
["gh", "api", "--method", "PATCH", "repos/kun-g/workshop/pulls/comments/4570694910", "--jq", "{id,created_at,updated_at,user:.user.login}", "--input", "-"]
```

退出码：0

stdout：

```text
{"created_at":"2026-11-14T01:48:32Z","id":4570694910,"updated_at":"2026-11-14T01:48:45Z","user":"lambdatheory-agent[bot]"}
```

stderr：

```text
```

### 2026-11-14T01:48:37.391759+00:00

```json
["gh", "api", "--method", "GET", "repos/kun-g/workshop/issues/72", "--jq", "{number,updated_at,state}"]
```

退出码：0

stdout：

```text
{"number":72,"state":"open","updated_at":"2026-11-14T01:48:32Z"}
```

stderr：

```text
```

### 2026-11-14T01:48:48.602401+00:00

```json
["gh", "api", "--method", "GET", "repos/kun-g/workshop/issues/72", "--jq", "{number,updated_at,state}"]
```

退出码：0

stdout：

```text
{"number":72,"state":"open","updated_at":"2026-11-14T01:48:32Z"}
```

stderr：

```text
```

### 2026-11-14T01:48:51.109909+00:00

```json
["gh", "api", "--method", "GET", "repos/kun-g/workshop/pulls/72", "--jq", "{number,updated_at,state}"]
```

退出码：0

stdout：

```text
{"number":72,"state":"open","updated_at":"2026-11-14T01:48:32Z"}
```

stderr：

```text
```

### 2026-11-14T01:48:52.248266+00:00

```json
["gh", "api", "--method", "GET", "repos/kun-g/grant-console/issues/156", "--jq", "{number,updated_at,state}"]
```

退出码：1

stdout：

```text
{"message":"Resource not accessible by integration","documentation_url":"https://docs.github.com/rest/issues/issues#get-an-issue","status":"403"}```

stderr：

```text
gh: Resource not accessible by integration (HTTP 403)
```

### 2026-11-14T01:50:16.005831+00:00

```json
["gh", "api", "repos/kun-g/workshop/issues/72", "--jq", "{number,updated_at,state}"]
```

退出码：0

stdout：

```text
{"number":72,"state":"open","updated_at":"2026-11-14T01:48:32Z"}
```

stderr：

```text
```

### 2026-11-14T01:50:17.217954+00:00

```json
["gh", "api", "repos/kun-g/workshop/pulls/comments?since=2026-11-14T01:48:33Z&per_page=100", "--jq", "[.[]|{id,updated_at,pull_request_url}]"]
```

退出码：0

stdout：

```text
[{"id":4570694910,"pull_request_url":"https://api.github.com/repos/kun-g/workshop/pulls/72","updated_at":"2026-11-14T01:48:45Z"}]
```

stderr：

```text
```

### 2026-11-14T01:50:19.170935+00:00

```json
["gh", "api", "repos/kun-g/grant-console/pulls/156", "--jq", "{number,updated_at,state,head:.head.sha}"]
```

退出码：0

stdout：

```text
{"head":"5854f0ddb820bd63466c8f96aac6a4a4ebb07426","number":156,"state":"open","updated_at":"2026-11-13T16:36:21Z"}
```

stderr：

```text
```

### 2026-11-14T01:50:20.653053+00:00

```json
["gh", "api", "repos/kun-g/grant-console/commits/5854f0ddb820bd63466c8f96aac6a4a4ebb07426/check-runs?per_page=100", "--jq", "{total_count,check_runs:[.check_runs[]|{id,name,status,conclusion,started_at,completed_at}]}"]
```

退出码：1

stdout：

```text
{"message":"Resource not accessible by integration","documentation_url":"https://docs.github.com/rest/checks/runs#list-check-runs-for-a-git-reference","status":"403"}```

stderr：

```text
gh: Resource not accessible by integration (HTTP 403)
```
