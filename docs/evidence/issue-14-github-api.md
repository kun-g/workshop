# #14 GitHub 更新时间实测

本轮使用 `lambdatheory-agent[bot]`。除本任务的实验评论、review、issue 正文和草稿 PR 外，未修改其他仓库对象。

## 结论与限制

- 编辑 issue 评论会异步更新 issue 时间。上轮即时读取未变化，后来时间已追上评论；此前“不会更新时间”的判断过早，现已更正。
- 提交 review 后，10 秒延迟回查的 issue 和 PR 时间已更新为提交时间。
- 编辑行内评论后，评论时间为 01:48:45Z，10 秒及数分钟后父对象时间仍为 01:48:32Z；仓库增量评论列表能返回它。需要补查 `pulls/comments?since=`，不能只靠对象变化列表。
- CI 行为已补齐只读实测：负责会话补齐车间 App 的 checks/actions/statuses 读权限后，使用该 App 身份读取公开仓库 astral-sh/ruff 的 PR #30422。PR.updated_at 为 2026-11-13T21:42:17Z，多项 CI 在之后完成（最晚 21:45:55Z），当前 PR 时间仍旧。因此 CI 完成不必推进 PR 时间，必须单独轮询开放标记 PR 的头提交。没有修改公开仓库。下文保留早期 403 原始记录，不能将其当成当前状态。
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

## CI 补充证据（本轮已恢复读取）

样本：https://github.com/astral-sh/ruff/pull/30422 。方法为只读比较已完成检查的时间与当前 PR 更新时间，不是主动重跑对方 CI。该样本证明存在 CI 完成但 PR 更新时间未随之推进的情况。

### 2026-11-14T03:35:04.319445+00:00

```json
["gh", "api", "repos/astral-sh/ruff/commits/7ef30f0415ccdda33d4b5c0d2f02caf0c60170b3/check-runs?per_page=100", "--jq", "{total_count,check_runs:[.check_runs[]|{id,name,started_at,completed_at,status,conclusion}]}"]
```

退出码：0

stdout：

```text
{"check_runs":[{"completed_at":"2026-11-13T21:45:55Z","conclusion":"success","id":132189656420,"name":"CodSpeed Performance Analysis","started_at":"2026-11-13T21:45:55Z","status":"completed"},{"completed_at":"2026-11-13T21:42:29Z","conclusion":"success","id":132187839458,"name":"all required checks passed","started_at":"2026-11-13T21:42:18Z","status":"completed"},{"completed_at":"2026-11-13T21:42:21Z","conclusion":"success","id":132187618214,"name":"publish-ecosystem-report / publish","started_at":"2026-11-13T21:41:52Z","status":"completed"},{"completed_at":"2026-11-13T21:44:40Z","conclusion":"success","id":132187616371,"name":"benchmarks walltime ruff (x86_64)","started_at":"2026-11-13T21:42:19Z","status":"completed"},{"completed_at":"2026-11-13T21:41:04Z","conclusion":"skipped","id":132187248509,"name":"security-review / report","started_at":"2026-11-13T21:41:04Z","status":"completed"},{"completed_at":"2026-11-13T21:41:04Z","conclusion":"success","id":132187168952,"name":"security-review / prepare","started_at":"2026-11-13T21:40:57Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184703340,"name":"custom-notify-dependents","started_at":"2026-11-13T21:35:58Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184703316,"name":"custom-publish-mirror","started_at":"2026-11-13T21:35:58Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184702610,"name":"custom-publish-docs","started_at":"2026-11-13T21:35:58Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184702280,"name":"custom-publish-playground","started_at":"2026-11-13T21:35:58Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184702279,"name":"custom-publish-versions","started_at":"2026-11-13T21:35:58Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184702005,"name":"custom-publish-crates","started_at":"2026-11-13T21:35:57Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184701971,"name":"announce","started_at":"2026-11-13T21:35:57Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184701478,"name":"generate-checksum-manifest","started_at":"2026-11-13T21:35:57Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184701312,"name":"custom-publish-wasm","started_at":"2026-11-13T21:35:57Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184701186,"name":"custom-publish-pypi","started_at":"2026-11-13T21:35:57Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184700909,"name":"sign-release-binaries","started_at":"2026-11-13T21:35:57Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184700736,"name":"build-playground","started_at":"2026-11-13T21:35:57Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184700729,"name":"host","started_at":"2026-11-13T21:35:57Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184700353,"name":"build-global-artifacts","started_at":"2026-11-13T21:35:57Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184700078,"name":"custom-build-docker","started_at":"2026-11-13T21:35:57Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184699492,"name":"custom-build-binaries","started_at":"2026-11-13T21:35:57Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"skipped","id":132184699458,"name":"custom-build-wasm","started_at":"2026-11-13T21:35:57Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"skipped","id":132184465300,"name":"publish-ty-fuzz-report","started_at":"2026-11-13T21:35:30Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"skipped","id":132184464648,"name":"Minimize ty fuzz failures","started_at":"2026-11-13T21:35:30Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"skipped","id":132184464483,"name":"benchmarks instrumented ty (${{ matrix.mode }}: ${{ matrix.name }})","started_at":"2026-11-13T21:35:30Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"skipped","id":132184464112,"name":"notify-ty-fuzz","started_at":"2026-11-13T21:35:30Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"skipped","id":132184464095,"name":"benchmarks walltime ty (x86_64)","started_at":"2026-11-13T21:35:30Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"skipped","id":132184463383,"name":"formatter instabilities and black similarity","started_at":"2026-11-13T21:35:29Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"skipped","id":132184463017,"name":"ty completion evaluation","started_at":"2026-11-13T21:35:29Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"skipped","id":132184462942,"name":"benchmarks walltime ty (build)","started_at":"2026-11-13T21:35:29Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"skipped","id":132184462864,"name":"PGO","started_at":"2026-11-13T21:35:29Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"skipped","id":132184462845,"name":"Fuzz for new ty panics/timeouts","started_at":"2026-11-13T21:35:29Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"skipped","id":132184462705,"name":"check playground","started_at":"2026-11-13T21:35:29Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"skipped","id":132184462459,"name":"fuzz parser","started_at":"2026-11-13T21:35:29Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"skipped","id":132184461832,"name":"benchmarks instrumented ty (build)","started_at":"2026-11-13T21:35:29Z","status":"completed"},{"completed_at":"2026-11-13T21:40:54Z","conclusion":"success","id":132184460445,"name":"security-review / security review","started_at":"2026-11-13T21:35:32Z","status":"completed"},{"completed_at":"2026-11-13T21:37:22Z","conclusion":"success","id":132184460422,"name":"prek","started_at":"2026-11-13T21:35:31Z","status":"completed"},{"completed_at":"2026-11-13T21:41:49Z","conclusion":"success","id":132184460420,"name":"benchmarks walltime ruff (build)","started_at":"2026-11-13T21:35:31Z","status":"completed"},{"completed_at":"2026-11-13T21:37:39Z","conclusion":"success","id":132184460386,"name":"mkdocs","started_at":"2026-11-13T21:35:32Z","status":"completed"},{"completed_at":"2026-11-13T21:35:45Z","conclusion":"success","id":132184460283,"name":"crates cooldowns","started_at":"2026-11-13T21:35:32Z","status":"completed"},{"completed_at":"2026-11-13T21:36:47Z","conclusion":"success","id":132184460243,"name":"python package","started_at":"2026-11-13T21:35:32Z","status":"completed"},{"completed_at":"2026-11-13T21:42:18Z","conclusion":"success","id":132184460220,"name":"benchmarks instrumented (ruff)","started_at":"2026-11-13T21:35:31Z","status":"completed"},{"completed_at":"2026-11-13T21:40:21Z","conclusion":"success","id":132184460173,"name":"cargo test (linux)","started_at":"2026-11-13T21:35:31Z","status":"completed"},{"completed_at":"2026-11-13T21:41:49Z","conclusion":"success","id":132184460151,"name":"ecosystem","started_at":"2026-11-13T21:35:31Z","status":"completed"},{"completed_at":"2026-11-13T21:40:42Z","conclusion":"success","id":132184460096,"name":"cargo test (namespace-profile-macos-15)","started_at":"2026-11-13T21:35:52Z","status":"completed"},{"completed_at":"2026-11-13T21:40:26Z","conclusion":"success","id":132184460079,"name":"cargo test (namespace-profile-windows-2022-x86-64-16x32)","started_at":"2026-11-13T21:35:55Z","status":"completed"},{"completed_at":"2026-11-13T21:36:06Z","conclusion":"success","id":132184460064,"name":"cargo shear","started_at":"2026-11-13T21:35:33Z","status":"completed"},{"completed_at":"2026-11-13T21:38:06Z","conclusion":"success","id":132184460059,"name":"cargo test (wasm)","started_at":"2026-11-13T21:35:32Z","status":"completed"},{"completed_at":"2026-11-13T21:36:55Z","conclusion":"success","id":132184460056,"name":"test scripts","started_at":"2026-11-13T21:35:33Z","status":"completed"},{"completed_at":"2026-11-13T21:36:03Z","conclusion":"success","id":132184460031,"name":"cargo deny","started_at":"2026-11-13T21:35:33Z","status":"completed"},{"completed_at":"2026-11-13T21:38:15Z","conclusion":"success","id":132184460026,"name":"cargo build (msrv)","started_at":"2026-11-13T21:35:31Z","status":"completed"},{"completed_at":"2026-11-13T21:44:12Z","conclusion":"success","id":132184460014,"name":"cargo fuzz build","started_at":"2026-11-13T21:35:33Z","status":"completed"},{"completed_at":"2026-11-13T21:38:00Z","conclusion":"success","id":132184459731,"name":"cargo clippy","started_at":"2026-11-13T21:35:32Z","status":"completed"},{"completed_at":"2026-11-13T21:42:15Z","conclusion":"success","id":132184459649,"name":"cargo hawk","started_at":"2026-11-13T21:35:33Z","status":"completed"},{"completed_at":"2026-11-13T21:35:53Z","conclusion":"success","id":132184459611,"name":"cargo fmt","started_at":"2026-11-13T21:35:32Z","status":"completed"},{"completed_at":"2026-11-13T21:36:04Z","conclusion":"success","id":132184459530,"name":"shellcheck","started_at":"2026-11-13T21:35:32Z","status":"completed"},{"completed_at":"2026-11-13T21:40:56Z","conclusion":"success","id":132184459507,"name":"cargo test (linux, release)","started_at":"2026-11-13T21:35:31Z","status":"completed"},{"completed_at":"2026-11-13T21:40:46Z","conclusion":"success","id":132184459472,"name":"cargo publish dry-run","started_at":"2026-11-13T21:35:31Z","status":"completed"},{"completed_at":"2026-11-13T21:35:29Z","conclusion":"success","id":132184250095,"name":"Determine changes","started_at":"2026-11-13T21:35:07Z","status":"completed"},{"completed_at":"2026-11-13T21:35:04Z","conclusion":"skipped","id":132184250046,"name":"release-gate","started_at":"2026-11-13T21:35:05Z","status":"completed"},{"completed_at":"2026-11-13T21:35:57Z","conclusion":"success","id":132184247197,"name":"plan","started_at":"2026-11-13T21:35:06Z","status":"completed"}],"total_count":62}
```

stderr：

```text
```

### 2026-11-14T03:35:06.408274+00:00

```json
["gh", "api", "repos/astral-sh/ruff/pulls/30422", "--jq", "{number,html_url,updated_at,head:.head.sha}"]
```

退出码：0

stdout：

```text
{"head":"7ef30f0415ccdda33d4b5c0d2f02caf0c60170b3","html_url":"https://github.com/astral-sh/ruff/pull/30422","number":30422,"updated_at":"2026-11-13T21:42:17Z"}
```

stderr：

```text
```

## 实现对应

- `issues?state=all&sort=updated&since=` 负责对象、标签、普通评论、review 和头提交变化；`pulls/comments?sort=updated&direction=asc&since=` 补行内评论。两个独立水位均使用所见最大 updated_at，默认重叠 60 秒。
- 列表摘要未变时跳过详情。无变化且无开放标记 PR 时为 2 个列表请求；有 1 个开放标记 PR 时再加 check-runs 与 status，共 4 个请求。列表和详情均按 Link 分页；请求预算不足保留已读页与进度，整轮结束才推进水位。
- 无水位的旧游标先完整读取一次，保留 seen/known/own_labels，后续改为增量。已关闭对象只保留关联和去重信息，不再逐个扫描。
- 缓存按仓库及交付对象隔离。旧共享缓存先复制游标引用的响应；迁移游标确认后的下一次成功读取才回收旧共享响应。其他尚未迁移的游标即使缓存缺失也会重新 GET，已记事件和去重状态不变。各对象的新目录互不清理；失败不回收，当前已确认游标引用的文件保留到下一次成功轮询。

## 本轮验证结果

- `uv run pytest -q tests/test_issue_delivery.py -k 'source or own_label or editing_ordinary or incremental'`：19 passed。
- `scripts/check.sh`：676 passed；2 个核心/看门 import-linter 合约通过；插件互引合约通过；核心用词与插件边界检查通过。
- 使用真实 App GET 对比评论 4570694910：单个 PR 的 comments 与仓库级 pulls/comments 中该评论的 JSON 完全一致（same_payload=true，different_fields=[]），事件签名使用相同输入。
- 检查输出对应本次实现；未合并、未上线，生产标签到事件的延迟复测留到后续验收/观察。
