# CodeBuddy Code CLI 本机直连（最小传输入口）

使用已登录的官方 CodeBuddy Code CLI（本阶段核对版本 2.161.1）。复制
`scripts/codebuddy-entry.json.example` 为同目录的 `codebuddy-entry.json`，填写本机
node 与 CodeBuddy CLI 入口文件（`bin/codebuddy`）的绝对路径；也可用 `--config`
指定。真实配置含本机路径与安装位置，不得提交 Git。

```text
python scripts/codebuddy_direct.py --workspace <项目绝对路径> --prompt-file <UTF-8提示词文件> --output-dir <不存在的新证据目录> --stage <非空阶段编号> --model <模型ID>
```

`--model` 必填，无默认、无 auto、无 fallback。`--resume-session-id` 可选，内部传
官方 `--resume`。不自动重装、登录、重试、后台取消或通知；主脑不开第二个 CLI 主脑。

## 统一契约与哈希口径

- 三入口调用前使用 `scripts/prompt_contract.py` 的完整九节契约，含精确标题、阶段/路径同一行、首末独占标记和结语。完整契约与任务拼接后经 stdin 发送。契约不能保证模型服从；原始响应照存，不合格仍保留 `bound=false`，不裁剪、不代写。
- 新调用的 `prompt_sha256` 与 dispatch-plan 均绑定原提示词文件的原始字节；另外保存实际任务文本、契约和完整发送通道的字节与哈希。换行转换口径显式记录，不把 JSON 文件哈希当 SDK prompt 哈希；Qoder 两通道不冒称一个后端合成载荷。留证边界只到本地 CLI/SDK 提交，不证明服务端处理后的文本。
- 不改提示词原文件或在途计划；旧调用记录沿用原入口版本的含义，升级后的新调用使用新计划。完整字段和留证位置见 [并行执行控制面](parallel-execution.md)。协议、报告绑定和独立业务验收分别报告，不能由本次传输回归推断真实业务恢复。

## 调用形状（本机 CLI 2.161.1 已核对）

```text
node <cli/bin/codebuddy> -p --verbose --output-format stream-json --model <ID> \
  --permission-mode dontAsk --tools <清单> --strict-mcp-config \
  --mcp-config '{"mcpServers":{}}' --setting-sources '' \
  --settings '{"disableAllHooks":true}' --agent cli --no-session-persistence
```

- 提示词（契约 + 用户原文，不裁剪）经 stdin 完整传送；无 shell 拼接；cwd 指定
  workspace；env 仅 os.environ 副本加 `DISABLE_AUTOUPDATER=1`；不修改机器全局设置。
  提示词原文不落盘进 `request.json`（避免重复暴露），仅在 `request.json` 记录其
  UTF-8 字节 SHA-256；CRLF 等原文字节经 stdin 原样传输、不做换行翻译。
- `--tools` 永远恰好出现一次，值是一个 comma-joined 字符串（空即 `''`）；不得逐项
  追加参数，也不得省略。空 `--tools ''` 表示零授权，同时不追加任何 `--allowed-tools`
  回退规则，避免“看似限权实则全放”。
- `--allowed-tools` / `--disallowed-tools` 为 variadic：一次 flag 后跟全部规则
  （list，不拼字符串）；每条规则原样传入，不按逗号拆分，非字符串/空/仅空白拒绝。
- 无显式 allow 时按 `--tools` 逐项回退为允许规则；显式列表（含空列表）完全替代回退。
- `--permission-mode` 固定 `dontAsk`；永不启用 bypass 或 auto-review 代理；不开
  子 Agent 或联网工具，tools 白名单保证这些不可见。

## 权限范围限制（重要）

权限规则不是文件 sandbox：dontAsk 模式下规则影响提示与自动放行，不构成对进程文件
访问的强制隔离。敏感目录保护必须依靠主脑侧环境隔离，不能只依赖本入口。

## Windows scoped 文件授权（2.161.1 版本差异）

官方原样 `matchFileRule` / `normalizePath` / `normalizeFilePathPattern` 加官方 bundled
minimatch 的无上游探针表明：Windows 下 scoped 文件规则
（`Read(...)`/`Edit(...)`/`Write(...)`）使用**完整驱动器绝对路径**可以命中目标：

- `C:/isolated/work/probe.txt` 命中，`C:/isolated/work/other.txt` 不误命中；
- `/probe.txt` 被 `normalizeFilePathPattern` 当字面**根路径**、不解析到项目根，**不命中**
  `C:/isolated/work/probe.txt`（旧文档“`/path`=项目根起”与实现不一致）；
- 裸相对 `probe.txt` 经 `path.resolve` 重新引入**反斜杠**，与正斜杠规范化目标不等；
  点相对 `./probe.txt`、`../probe.txt` 保持相对，不解析到工作区，均不命中绝对目标。
- 完整反斜杠绝对路径也会在匹配前转为正斜杠，已通过同一官方匹配函数的正例验证。

官方 Edit 已覆写 `resolveNeedPermissionArgs` 返回 `{type:FilePath, value:file_path}`，因此不能用基类的默认 Unknown 参数推断 Edit 缺少文件路径参数。

因此本版本入口在 Windows 下对 scoped Read/Edit/Write 规则做**派工前校验**：仅接受完整
驱动器绝对字面单文件路径（推荐正斜杠，完整反斜杠绝对路径同样支持）；相对、`/` 根样式、
UNC（`\\...` / `//...`）、`~`、含通配元字符或 extglob（例如 `@(a|b)`）的模式，
都在**创建证据目录与 Popen 之前退出 2**、
零目录创建、零模型额度消耗，并给出该 workspace 的实际绝对正斜杠示例（示例用占位相对段，
不擅自把 `/secret.txt` 改写成某个具体项目文件、也不静默转写）。`--allowedTools` 与
`--disallowedTools` 两侧同等校验；Bash 等命令规则与裸工具名维持既有原样行为；原始允许/
拒绝列表仍逐字传给官方 CLI，计划 grant 来源与既有 exact-argv/集合比对不变。非 Windows
维持原样行为（该校验不生效）。

正确最小单文件授权示例（Windows，路径须为该 workspace 下的实际绝对正斜杠路径）：

```text
--allowed-tools "Read" --allowed-tools "Edit(C:/绝对/到/workspace/目标/文件.txt)"
```

**边界（尚未证明）**：本轮只修派工前的授权形态校验，避免耗费额度后才被拒；未证明
CodeBuddy 真实业务编辑已恢复，也未证明 429 配额恢复。完整 CLI 编辑链路、后端模型身份、
配额窗口仍需主脑真实派工回读验证，不得据此声称业务已修复。429 失败信封、重复 init、
报告绑定、usage 解析与既有并行升级均不变。

## 协议终态（不只看 exit 0）

stdout 为 stream-json JSONL，逐字以字节保存 `stdout.jsonl`（stderr 同样逐字保存
`stderr.log`）。真实终态要求：

- stdout 必须严格 UTF-8；出现非法字节保留原件并拒绝，禁止 `errors='replace'` 伪造原文。
- 终态 result 分两类，且必须区分“解析有效”与“交付失败”：
  * **成功终态**：`subtype=success`、`is_error=false`、非空字符串 `result`，且与
    最后 assistant 的最后一个 text 分片逐字一致（不 trim）。成功信封若 `errors`
    非空则不得绿灯（记结构错误）。
  * **失败信封**：仅 `subtype=error_during_execution` **AND** `is_error=true` 且带
    非空 `errors`（官方形状：字符串列表）时才算**合法失败信封**——可结构解析
    （`parse_success` / `failure_envelope_valid` 为真）而 `protocol_success=false`；
    缺 `errors`、空 `errors`、`subtype`/`is_error` 矛盾或未知 subtype 不得声明为
    合法已解析失败，一律记为结构错误（`parse_success=false`）。合法信封提取
    `primary_failure`、原始 `errors`/`errors_info`、`reset_hint`（仅当**实际从全部
    `errors` 与 `errors_info` 文本**中匹配到 “将在 … 重置” 时段才有；不只扫第一个
    primary_failure）、`failure_stage=result_terminal` 与 `recoverability`。429 只认
    明确的 `errors_info.status==429` 或独立错误码 `code==429`，或原始错误文本**开头**
    的独立 429 错误码（如 `429 ...`、`429：`、`HTTP 429`），**不以正文里的 429
    数字、id 或单纯 `category=quota` 推断限流**；仅 quota 而无 429 记 `unknown`。
    recoverability **只在实际提取到 reset 提示时提窗口**，无 reset 明确标窗口未知，
    不臆造时间框架。**已识别的交付失败根因（退出码等）单独归类，不再混成
    `parse_errors` 结构错误**。失败信封若携带 `result` 文本，原样落盘
    `failure-result.pending.txt` 待验、**不绑定、不生成 `response.md`**；无 result 不
    生成报告。**protocol_success 与 report_bound 仍恒为 false**，CLI 退出码 0 也不得
    伪报成功（解析正确 ≠ 交付成功）。
- init.session_id 非空；协议事件**凡带 `session_id` 字段就必须是非空字符串**，否则
  一律拒绝（不得因 sid 为空而跳过身份核对）。所有带 session_id 的事件必须与 init
  完全一致，不得漂移。
- init 模型及所有 `assistant.message.model` 与请求模型精确一致。
- 至少一个 assistant.message 存在非空 text 分片（仅对成功终态强制）。
- `init.mcp_servers` 必须严格为 `[]`；非空一律拒绝，不得宣称零 MCP。
- `init.tools` 是整个工具注册表，不代表本次有效工具面；实际 `tool_use.name` 记录为
  已调用证据，若超出请求 `--tools` 白名单即判越权失败。
- **重复 init（同会话重初始化）**：本机真实流（`inputs/stdout.jsonl` 第 1、146 行）
  在同一次请求作用域内发出两次 init，除 `__timestamp` 外全字段逐值一致。官方运行时
  证据见 `inputs/independent-runtime-provenance.json` 的 `wasInitEmittedForTurn(` /
  `createSystemMessage(` / `createMinimalSystemMessage(` 等 request-scope 上下文
  （该文件里的 `character_offset`/`byte_offset` 是相对
  `@tencent-ai/codebuddy-code/dist/codebuddy-headless.js` 的偏移，且 `official-runtime-snippets.json`
  旧 snippets 记录的 offset 是**字符 offset、不是字节 offset**，引用须标清）。注意
  `renderHistory` 让 model 变 `unknown` 只能证明历史模式会发 init，**不能独立证明
  两次稳定 init 的因果**；重复发生的**具体原因未知，不断言是上下文压缩**。适配器
  **只接受全字段身份稳定的重复 init**：除白名单可变传输字段 `__timestamp` 外，比较
  两个 init 的**全字段键集与逐值**（含 `session_id`、`model`、`mcp_servers`、`tools`、
  `permissionMode` 以及 `cwd`、`apiKeySource`、`agent` 等既有字段），任何值漂移、
  **新增/删除的未知字段**一律拒绝；每次 init 仍校验必备字段/类型（`tools` 为
  `list[str]`、`mcp_servers` 为 `[]`、`permissionMode` 为 `dontAsk`）且既有安全字段
  不能缺；差异/缺失记录到 `reinit_events`（`changed_fields`/`added_fields`/
  `removed_fields`/`missing_identity_fields`），不把所有元数据忽略。不放宽所有未知
  字段、也不删所有重复校验（未知事件、空/重复 result、尾随垃圾、不匹配文本、越权
  tool_use、session/model 漂移仍严格拒绝）。
- **权限拒绝**：实际出现在 `user.message.content[].type=tool_result` 的文本里
  （真实形状下 `is_error=false`、`result.permission_denials=[]`）。空
  `permission_denials` 不作为无拒绝证据：从 tool_result 文本扫描明确的
  “Permission to use … has been denied” 文案，命中即 fail-closed 并原样保存拒绝
  字段与 tool_use_id。只检测 tool_result，不匹配模型报告正文里的普通引用；
  权限拒绝单独记录，不落入普通 tool_failures。
- **普通工具失败**：`user.tool_result` 里的 `File does not exist`、
  `<tool_use_error>` 或 `is_error=true` 记录到 `tool_failures`（行号、
  tool_use_id、`flags` 与摘录）并汇总到 `tool_failure_stats`，**仅诊断**，
  不单独令协议失败，也不会把后续合法修复的成功误判为假失败；`is_error=true` 但
  `content` 缺失/空也仍记为失败（`excerpt` 可为 None，行/id/flags 正确），不再被
  静默丢弃；不设置机械固定次数让业务任务永久失败。
- 未知事件/缺项/重复或缺失终态/尾随非 JSON/`terminal_reason` 为 cancelled、
  aborted、max_turns / `result.permission_denials` 非空一律判协议失败并保留证据。
  事件字段版本不同不猜测，缺项记 `parse_errors`，由主脑拿真实事件回读后再修。

## 失败恢复与派工效率

- **429 / 限流**：CLI 明确返回 `error_during_execution` + `errors_info.status=429`（或
  独立错误码 `code==429`）时才算 `rate_limited`；仅 `category=quota` 而无 429、或正文/id
  里出现 429 数字都**不足以断言限流**，一律标 `unknown`，不据此推断。此时必须保留原
  `stdout.jsonl`、已生成文件、阶段进度与工具结果；**不立即重跑整项重任务**、不静默换
  模型；只有实际从 errors/errors_info 提取到重置时段才保存窗口，无 reset 时明确标窗口
  未知、不推断已重置；无最终报告不补写 WORKER_REPORT。评分同阶段不重复计分。
- **恢复派工**：在**新证据目录**进行；开工前核对文件基线（HEAD/SHA-256）、授权
  范围与最近的限流历史提示；不覆盖他人工作或原证据；未验证代码不加载。
- **文件定位**：主脑派工优先给出**精确文件清单**，并显式开放 `Glob` / `Grep`
  在允许目录下做搜索（本适配器 `ALLOWED_TOOLS` 已包含），避免反复猜文件名或
  重复读整份大文件；同类定位失败会累计记录，供主脑调整检索策略。工具权限不是
  文件沙箱，凭据仍不进派工目录。

计费口径未知：`usage` / `modelUsage` 仅原样保存 raw 值；`total_cost_usd=0` 不视为
免费证据；缓存分项不与 `input_tokens` 重复累加；`business_verified`、
`free_quota_verified`、`model_backend_identity_verified` 恒为 false。CLI 路由标识
不证明底层模型身份，零 token 不证明免费。

## 证据与退出码

输出目录含 `request.json`（argv、提示词 UTF-8 字节哈希、stage、cwd、模型、工具规则；
不含提示词正文）、`process.json`（pid、状态、真实退出码）、`stdout.jsonl`、`stderr.log`、
`response.md`（result 原文字节，不 trim 不删前言）、`report-state.json`（九节正文
核对 + 最终绑定）、`summary.json`。preflight 校验错误退出码 2 且证据目录零创建；
协议成功且报告绑定均为真才退出 0；其余失败退出 3。失败证据同样完整保存，原件不删改。

官方文档：https://www.codebuddy.cn/docs/cli/cli-reference 、
https://www.codebuddy.cn/docs/cli/iam 。字段若与真实 CLI 回读不同，以主脑真实
回读为准，本入口标未知待修。

运行时参数名称为 `--allowedTools` / `--disallowedTools`（camelCase）；入口对外的重复 `--allowed-tools RULE` / `--disallowed-tools RULE` 由脚本转换，不能原样传给官方 CLI。已实测 kebab-case 会在模型调用前退出。
