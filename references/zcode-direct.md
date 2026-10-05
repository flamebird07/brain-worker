# ZCode 官方运行时本机直连（SDK-CLI 画像）

复用本机已安装的 ZCode 官方运行时，在同一进程内完成 provider 注册、模型选择预检与
`submitPrompt`，让 brain-worker 不必再靠人工粘贴提示词。入口是纯标准库的
`scripts/zcode_direct.py`，它只负责参数校验、请求落盘、子进程管理与报告绑定；真正调用
官方 API 的是同目录的 `scripts/zcode_sdk_runner.mjs`。

本画像是 **SDK-CLI 直连**，不是通用四接口派发器：它沿用 Qoder 直连的九节兼容报告模板
（见 `references/qoder-direct.md` 末尾代码块），不套用 SKILL.md 的四节通用模板，也不得冒充
通用传输层。协议终态、正文绑定、业务验收三者分离，`business_verified` 恒为 false。

## 安装与配置（真实安装由主脑执行）

1. 复制 `scripts/zcode-entry.json.example` 为同目录 `scripts/zcode-entry.json`（已被
   `.gitignore` 忽略），把五个键填成本机真实绝对路径：
   - `node`：本机 Node 可执行文件；
   - `bootstrap`：官方 `apps/zcode-cli/packages/bootstrap/dist/index.js`；
   - `tsx_loader`：官方 `node_modules/tsx/dist/loader.mjs`（runner 以
     `--import <fileURL>` 注册，缺省自动生成，无需手写）；
   - `builtin_provider_config` / `personal_provider_config`：两个官方 provider 配置文件，
     注入为官方要求的成对环境变量 `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` 与
     `ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`。
2. `environment` 只放明确要透传的变量名与值；配置里**不写 token、Cookie 或账号原文**，
   工具也不读取这些文件内容——证据目录里只出现 `environment_keys` 名单。
3. 不自动安装、不自动登录、不改桌面模型、不读凭据、不编译或修改官方源码；官方源码全程
   只读引用。`version` 可选，只透传官方构建标识（同进程真实调用已验证过 `0.16.9`）。
   `node_args` / `runner` 是离线测试用的执行载体覆盖项，真实派工不要设置。

## 调用

```text
python <技能目录>/scripts/zcode_direct.py --workspace <项目绝对路径> --prompt-file <UTF-8任务文件> --output-dir <不存在的新证据目录> --stage <非空阶段编号> --provider account:bigmodel-individual-coding-plan --model GLM-5.3-Flash --reasoning low --mode plan --tools Read,Glob,Grep
```

- 缺省选择：provider `account:bigmodel-individual-coding-plan`、模型 `GLM-5.3-Flash`、
  `--reasoning low`、`--mode plan`、`--tools` 为空（即禁用全部内置工具）。
- `--mode` 只有 `plan` 与 `edit`：plan 只读，`--tools` 出现 `Write`/`Edit`/`Bash` 直接派工前
  拒绝；edit 需要用户明确授权修改本项目。官方支持的 `yolo` 一律不构造。
- `--preflight-only`：只查注册表可选性，不建 App、不建会话、不提交任何 prompt。它读
  ProviderRegistry 视图与 `validateSelection`，**不能证明可发送**——`disabledReason` 只有
  `App.listModels()` 才暴露，因此该模式如实记录 `disabled_reason_verified=false`。
- `--resume-session-id <官方会话id>`：必须与运行时实际 sessionId 逐字一致，否则 runner 在
  提交前拒绝；绑定环节再做一次精确比对。

派工前拒绝（配置缺项、路径不存在、空 stage、非法 mode/reasoning/tools、输出目录已存在）
不创建证据目录，已有证据字节保持不变；重放同一目录一律拒绝。

## 模型选择预检：不允许回落

runner 依次核对：注册表里 provider 与 model 是否存在 → `validateSelection` →
`App.listModels()` 是否命中且 `disabledReason` 为空 → `setModel` 后
`getCurrentModelOption().ref` 的 provider/model 是否逐字相同 → 推理档位是否一致 →
resume 会话 id 是否一致。任一项不符即写失败的 `preflight.json` 与非零退出，**不提交 prompt、
不改用其他模型**。turn 结束后再次比对选择，漂移即记为失败。`GLM-5.3`（非 Flash）等其它
目标必须显式指定并在该次运行里实测通过，默认值不会自动降级或升级。

## 工具边界（只做整工具开关）

`--tools` 是**逐个内置工具的启用/禁用**，不是 Qoder 那种路径或命令规则，也不宣称机器级路径
限定；prompt 里的路径约束只是文字要求，没有文件级 sandbox。实现方式：

- 运行时 `runtimeConfig.toolDisallowlist` 装入已核对的官方内置工具全目录（含注册名为 `js`
  的 node_repl、`Task`/`Agent` 子 agent、`WebFetch`/`WebSearch` 联网、workflow、cron、
  off-peak、Skill、Todo 等），`--tools` 显式名单才逐项放行；未知名单项派工前拒绝。
- `submitPrompt({toolDisallowlist})` 再叠加运行时活注册表 `runtime.getToolRegistry().list()`
  的并集，避免遗漏动态注册项；活目录查询失败即在提交前拒绝，不回退静态目录。
- 隐藏通道按官方 API 证据关闭：`mcp:{enabled:false}` 不构造 mcpPort、
  `subagents:{enabled:false}` 不注册 SubagentPort、`dynamicWorkflowEnabled:false` 移除
  workflow 工具组、不传 `browserControlPort` 故 Browser 不可用、项目 hooks 依赖默认关闭的
  workspace trust、`memory.extractionEnabled:false` 关闭自动记忆抽取。
- 已核对的支持面只有 `Read`/`Glob`/`Grep`/`Write`/`Edit`/`Bash`；Task/子 agent、联网与
  Browser 工具不对本入口开放。若后续发现某隐藏通道无关闭证据，按本文档限制处理，不宣称已禁。

## 报告与验收

runner 只做机械校验：`response.md` 是模型**原样文本**（不修剪、不改写、不补换行），首尾标记
各独占一行且只出现一次，`阶段编号与执行方式` 携带精确阶段 token，
`实际项目绝对路径` 与工作区逐字一致，九节齐全且顺序正确；随后按 SHA-256 回读字节。
格式指令拼在原任务之前，原任务逐字保留；报告带前言时原文照存并判 `body_ok=false`，不删前言。

`bound` 需要正文合格 ∧ `protocol_success` ∧ 回读哈希一致 ∧ 非空 session_id ∧ resume 精确一致。
非零退出、预检失败、信封缺失/非 JSON、异常退出都只能判失败，绝不宣称成功。

每轮计数由主脑记录：`summary.json` 里的 `usage.model_request_count`、`input_tokens`、
`output_tokens`、`total_tokens`（官方口径的 `cacheReadTokens` 已含在输入内，不再相加）、
`reasoning_tokens`、`event_count`、`turn_id`、`session_id` 是本轮事实来源；`events.jsonl`
可能很大，只落盘不整份打印。`free_quota_verified` 恒为 false。

证据目录：`request.json`、`process.json`、`preflight.json`、`stdout.json`、`stderr.log`、
`result.json`、`events.jsonl`、`response.md`、`summary.json`、`report-state.json`。
JSON 序列化失败会写 `serialization-error.txt` 并以退出码 6 判失败，不留空文件掩盖。

## 已验证事实与未证实项

- 本机已真实验证 BigModel 的 `account:bigmodel-individual-coding-plan`，默认 GLM-5.3-Flash；GLM-5.3 可显式选择并逐次预检。
- Start Plan / Trust Build 免费池没有接入证据，不能把已登录个人 coding plan 的调用认定为免费。
- Read/Edit/Write 已真实派工验证。Bash 只代表工具可见性，当前没有命令审批代理，需审批的命令会拒绝；离线测试由主脑运行，不宣称执行 Agent 已运行。
- SDK 成功后的 projection 可为 idle；协议成功必须同时取得匹配 session/turn 的成功 turn_complete、同文回复和全部真实 model_request 模型记录。单独 idle 或 completed 均不能证明完成。
- 每次结束先等待 App.close，再释放 Registry，最后输出一次信封。原始报告及失败证据保留。

## 离线验证

```text
python tests/test_zcode_direct_offline.py
node --check scripts/zcode_sdk_runner.mjs
python tests/test_zcode_sdk_runner.py
```

测试用真实子进程 stub（`tests/stub_zcode_runner.py`）镜像官方 runner 的 argv 与信封，并把
收到的完整请求 JSON 与实际 submit 次数写进 `stub-receipts.jsonl`，因此“零提交”结论来自子进程
凭证而非文本匹配。测试不登录、不联网、不调用任何模型，也不证明真实后端能力。
