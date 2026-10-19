# OpenCode CLI 正式直连入口（opencode-direct，BW-OPENCODE-20261019-S1/S3）

本文件描述 `scripts/opencode_direct.py`：OpenCode CLI 作为**正式派工入口**，只服务两个
**精确免费模型**，各自占用容量池里**独立 1 槽**（BW-OPENCODE-20261019-S3 计入
`MAIN_FORCE_CAPACITY`，国内总容量 14→**16**，原 14 槽一项未减）。实现以
`scripts/opencode_direct.py` 为准，不一致时以代码为准并回修文档。

## 无进展看门狗（BW-DISPATCH-WAIT-20261019-S1）

总墙钟上限仍为 1800 秒。正式入口同时按 stdout JSONL 观察有效事件；连续 300 秒没有任何可解析且带 `type` 的事件时终止本次子进程并记录 `stalled_no_progress=true`、`termination_reason=stalled_no_progress`，保留原始 stdout/stderr，已确认终态后释放本槽。普通噪声、半行和不可解析文本不冒充进展。用户明确要求本次继续等待时可传 `--no-progress-timeout-seconds 0` 关闭无进展门，但总墙钟仍必须保留；负值在 Popen 前拒绝。

## 精确模型硬门（仅两个免费 ID，绝不付费回退）

- `opencode:deepseek-v4.1-flash-free`（`--model deepseek-v4.1-flash-free`）与
  `opencode:glm-5.3-flash-free`（`--model glm-5.3-flash-free`）；argv 内模型 ID 拼为
  `opencode/<ID>`。
- 其它任何 model 在**任何副作用之前**拒绝（`cli_model_rejected`，exit 2）：不读配置后继续、
  不建 outdir、不 claim、不 Popen。**绝不自动回退付费模型或别的 OpenCode 模型**；每个 run
  只调用一次 CLI。
- 两池各自独立 1 槽（不同工作区可并发各 1 在途，同模型第 2 个仍拒绝，结束只释放自己的槽）；
  **无时段门**（不套 Claude 离峰窗、也不套 Max 北京夜间窗）、**无 CodeBuddy 当轮额度前提**，
  也不承载 `main_force_limits` 限额标记（`QUOTA_LIMITABLE_POOLS` 不含 OpenCode）；仍受
  总 16 守卫、同 `task_id` 防重、同真实 workspace 单写入与 unknown 保占位约束。
- 调度优先级：层=4（ZCode 之后、条件 CodeBuddy GLM=5 之前、两区 Flash 之前），两池同层按
  真实在途数平衡、平票 pool_key stable tie；详见 [global-dispatch](global-dispatch.md)。

## CLI 形态与传输

- argv：`<executable> run --pure --model opencode/<ID> --format json`，`shell=False`；
  `--pure` 禁外部插件。
- **完整九节契约 + 任务原文经 stdin 发送**（`prompt_contract` 组合后的 payload 原字节，
  不裁剪、不进 argv）；每个 run 恰好一次 `communicate(input=payload, timeout=...)`。
- 有界墙钟：默认 `DEFAULT_TIMEOUT_SECONDS=1800`，可 `--timeout-seconds` 显式覆盖（正整数），
  0/负值拒绝（`timeout_out_of_bounds`）。OpenCode 独占一个真实写槽，墙钟必须有界，超时后
  kill + 收集真实输出，**如实定为失败，绝不记 healthy**。
- stdin 可用性核验状态（与生产声明分开）：root 已核验官方 v1.19.3 `packages/opencode/src
  /run.ts` 第 401 行使用 `Bun.stdin.text()`（读至 EOF），据此本入口采用 stdin 传任务；
  该核验属**验证记录**，入口不宣称其它未逐行核验的 CLI 特性为事实。

## 可执行文件与私有配置（公开示例用通用路径）

- 解析顺序：入口配置 `executable`/`exe`（绝对路径或 PATH 名）→ 环境变量 `OPENCODE_EXE` →
  PATH 中的 `opencode`；缺失即抛错、0 副作用。**公开 example 一律用通用可配置名**
  （`scripts/opencode-entry.json.example`：`{"executable": "opencode", "allowed_tools": []}`），
  不写真实用户安装路径；私有安装路径差异由 root 本机以非 example 配置持有，worker 不读取。
- `--config` 显式路径不存在/非 JSON 对象一律拒绝；缺省路径不存在时返回空配置（走 PATH 发现）。
- **不复制、不注入任何现有 OpenCode（含 OpenCode Go）凭据**；凭据不进 argv/request/异常/证据。

## 环境隔离（每 run 强制）

- `OPENCODE_CONFIG_CONTENT`：`{"share":"disabled","permission":{"*":"deny","read":"allow",
  "edit":"allow","external_directory":"deny", …显式 allowed_tools},"mcp":{},"plugin":[]}`。
  `external_directory` 恒为 `deny`，配置打不开；`agent`/`task`/`subagent` 永远禁止出现在
  `allowed_tools`（畸形即抛）。默认仅 read/edit（原生 write 由 edit 权限控制）；配置可显式
  追加已授权工具（如 bash），但**这是整工具开关，不是逐文件沙箱**——本入口不宣称任何
  per-file 隔离能力。
- `OPENCODE_CONFIG_DIR`、`XDG_CONFIG_HOME`、`XDG_DATA_HOME` 指向每 run 在 outdir 下新建的
  **私有空目录**（已存在或非空即抛），绝不指向用户全局配置/数据目录。
- `OPENCODE_DISABLE_AUTOUPDATE=1`、`OPENCODE_DISABLE_CLAUDE_CODE=1`、
  `OPENCODE_DISABLE_LSP_DOWNLOAD=1`。

## 生命周期（复用既有原子容量生命周期，零新增服务/表/后台重试）

1. 预检失败（模型/超时/outdir 已存在/prompt SHA 复核/config/argv/`--dispatch-plan`
   preflight）⇒ **零 claim、零 Popen**。
2. `dp.consume_for_entry(..., executor='opencode')` 原子消费/自动路由 claim → `mark_running`
   （真实 wrapper PID）→ `dp.bind_child`（真实子进程 PID + 创建身份；**`bound` 必须显式
   `true`**）。
3. bind 未明确成功：**绝不写 stdin、不杀进程、不假释放**，`mark_unknown` 保留占位
   （`launch_unknown=True`），交 `reconcile` 对账。
4. 子进程已确认退出（含非零退出、超时 kill、异常路径）：无条件 `dp.finish(terminal=
   'finished', success=<真实判定>)` 释放**本 attempt 自己的槽**，并保留真实释放结果；
   Popen 前失败则 `finish(start_failed)`。绝不泄漏、绝不抢占别人名额。
5. `engineering_success/passed` 仅在 协议成功 ∧ 报告绑定 ∧ `execution_evidence_ok` ∧
   未超时 ∧ 无落盘错误 ∧ bind/释放真实成功 时才为真；CLI exit 0 **不证明**任务成功。

## JSON 流语义（逐行事件）

- 事件类型：`step_start` / `tool_use`（part.type=tool）/ `step_finish` / `text` / 顶层 `error`。
- `step_finish.reason=tool-calls` **不是完成**；协议成功要求：进程 exit 0、最后一个
  `step_finish.reason == 'stop'`、无顶层结构化 error 事件、无逐行 JSON 解析错误。
- 工具 `state.error`/权限拒绝与顶层 error **分开记录**；错误分类（permission/auth/quota/
  transport/protocol_parse_failure/model_execution_failure）**只扫描结构化错误字段**，
  绝不扫描正常 prompt、报告正文或工具 output 内容——正常 Read 正文里出现 429/ECONNRESET
  字样不会误报为失败。
- 最终 `text.part.text` 才是原始报告载体；中间 assistant 文字原样保留进
  `summary.intermediate_text_parts`，**绝不删前言制造绑定**。失败时**绝不生成假九节报告**。

## 证据文件（每 run 落盘，完整保存，不含 token）

`stdout.jsonl`、`stderr.log`、`events.jsonl`、`sent-payload-stdin.bin`（stdin 原字节）、
`response.md`（有报告载体时，写后回读 SHA 比对 `readback_match`）、`report-state.json`、
`summary.json`、`request.json`（argv/shell=false/stdin_payload/超时会话、prompt/contract/
sent_payload SHA-256、entry 配置摘要、`no_paid_fallback:true`、真实起止时间）、
`process.json`（PID/exit_code/timed_out）。`summary.json` 另含逐工具回执（成功/失败/权限拒绝/
缺回执）、`observed_models`、原始 `usage_parts`（`usage_billing_basis='unknown'`，只存不解释，
不当账单）。stdout/stderr/事件/请求/进程/摘要完整保存；不打印 token；证据不含 Cookie、
密钥、令牌或账务原文。

## 诚实边界（不得夸大）

- `execution_evidence_ok` 只表示原始工具调用有成功匹配回执且无失败/权限拒绝/缺回执，
  **不代表业务验收**，也不是独立文件系统真相；零工具纯文本传输可协议合法成功，但没有工程
  执行证据。
- `business_verified` 恒 `false`；`free_quota_verified` 恒 `false`；
  `model_backend_identity_verified` 恒 `false`——无后端身份回执时只写 `requested_model`，
  绝不冒充已验证后端身份或免费账单。`report_bound` 独立报告。
- **已验证的真实世界事实（本轮之前，root 提供）**：OpenCode CLI 1.19.3 于私有路径安装；
  两次**合成**任务调用成功（约 17/19 秒）。这**不等于**本正式入口已现场验证——正式入口的
  真实一次 Read/Write 验收由主脑（root）独立执行；本轮 worker 未运行任何 CLI/命令、未调
  真实模型。离线回归（`tests/test_opencode_direct.py` 等）零网络、零凭据、零真实启动。
- CAPABILITY：`execution_control.CAPABILITY['opencode'] = fine_grained=False、
  read_dirs=False`——整工具 profile；要求逐文件 Edit/逐字 Bash 或外部只读目录的任务在
  preflight 即拒（`cannot express per-file…`/`no external read-only directory…`），
  **不得为过测放宽**。

## CLI

```text
python scripts/opencode_direct.py --workspace <abs ws> --prompt-file <abs> \
  --output-dir <abs, must not exist> --stage S --task-id T \
  --model deepseek-v4.1-flash-free|glm-5.3-flash-free \
  [--config <entry.json>] [--dispatch-claim TOKEN] [--dispatch-store <abs>] \
  [--dispatch-plan <json>] [--active-tasks <json>] [--timeout-seconds N]
```

退出码：0 = 工程通过（含真实释放）；2 = 任何派发前拒绝（零 Popen）；3 = 已派发但工程判定
未通过（真实终态与释放结果仍如实落盘）。
