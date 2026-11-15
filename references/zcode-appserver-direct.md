# ZCode app-server 传输直连参考（BW-MAC-COMPAT-20261114-S2）

`scripts/zcode_appserver_direct.py` 是 ZCode 的**第二个正式传输**：经官方 CLI 的
`app-server` 子命令（ZCode Protocol stdio 服务器）驱动本机已登录桌面端运行时。
它与既有 SDK bootstrap 传输（[zcode-direct](zcode-direct.md)，`zcode_direct.py`
+ `zcode_sdk_runner.mjs`）指向**同一个池**（`zcode:GLM-5.3`，容量与宿主排除集
照旧），区别只在传输载体：

| | SDK bootstrap 传输（zcode_direct.py） | app-server 传输（本文件） |
|---|---|---|
| 载体 | 官方源码树构建的 bootstrap/dist，同进程 SDK | 桌面端自带官方 CLI `zcode app-server`（子进程 stdio） |
| 平台 | Windows（macOS 需自行构建源码树） | **macOS 优先**（Windows 明确拒绝并指向 SDK 传输） |
| 工具门 | SDK 整工具目录 + 装饰执行端口 + permissionBroker | 官方 `interaction/requestPermission` 权限请求，fail-closed deny |
| 文件门 | `--file-scope-contract` 逐文件端口 | 本传输无对应能力（如实缺失，绝不冒称） |

## 协议事实（2026-11-14 有界探针实测，证据见仓库外 protocol-probe）

- **帧型**：行分隔 JSON，**无 `jsonrpc` 键**。客户端请求 `{"id":<int>,"method":...,"params":{...}}`；
  响应 `{"id":N,"result":{...}}` / `{"id":N,"error":{"code","message"}}`；
  服务器→客户端请求 `{"id":"server-N","method":...,"params":{...}}`（string id，必须应答）；
  通知无 id。
- **生命周期**：`runtime/capabilities` → `session/create {workspace:{workspacePath,workspaceKey}}`
  → `session/setMode {sessionId,mode}` → `session/setModel {sessionId,model:{providerId,modelId,options:{reasoningLevel}}}`
  → `session/send {sessionId,content}` →（泵通知直到 `turnEnded`）→ `session/messages {sessionId}`
  → `session/stop`/`session/close`。
- **官方模式枚举**：`plan|build|edit|yolo|guarded|auto`；本入口只提供 `plan|edit`
  （yolo/auto/guarded 一律不提供，与 SDK 传输同纪律）。
- **会话身份与时序（实测）**：`session/create` 结果里的 `projection.sessionId` 是
  字面量 `'unknown'`——真实 sessionId 只来自 `session/lifecycle/changed` 通知；
  `session/setMode`/`setModel` 必须等 lifecycle `state=ready` 之后才发（提前发得
  `-32004 runtimeNotReady`）。本入口据此以 lifecycle 通知为权威身份源。
- **`session/setModel` 的模型绑定**：`model` 只接受对象
  `{providerId, modelId, options?: {reasoningLevel}}`（字符串被 Zod 拒绝）；官方以
  provider registry `validateSelection` 核对，`provider-not-found` 即拒绝
  （`Provider Registry 中不存在 Model`）。**provider registry 只装账户型 provider**：
  需经 `ZCODE_BUILTIN_PROVIDER_CONFIG_FILE` 注入桌面端生成的运行时配置（实测路径形如
  `~/.zcode/v2/runtime/provider/darwin-aarch64/<version>/endpoint-*/zcode-builtin.json`，
  内容含 `account:*` 各套餐与各自 `builtinModelIds`）；`provider/refreshRegistry`
  刷新成功**不会**把缺失的 provider 补进注册表。套餐决定模型可用性（实测：本机
  start-plan 只有 `GLM-5.3-Flash/GLM-5.2/GLM-5-Turbo`，无 `GLM-5.3`）——池要求的
  模型不存在时本入口在 setModel 处如实拒绝（start_failed、零提交），绝不静默换模型。
- **服务器→客户端请求**（本入口的固定应答策略，全部落证据）：
  - `session/requestRuntimePreferences` → 固定保守应答
    `{nativeSearchEnhancementsEnabled:false, toolSearchMode:'off', memoryEnabled:false,
    askUserQuestionAutoResolutionEnabled:true, modelContextBudgetStrategy:'preflight-v1'}`；
  - `interaction/requestPermission` → 默认选官方 `deny` 选项（kind=deny）；
    `--allow-tools` 名单内的 toolName 才选 `allowOnce`；请求形状异常（无 deny 选项）→
    error 应答，绝不猜选项；
  - `interaction/requestUserInput` 及一切未知方法 → error 应答（headless 无交互，
    不支持的请求绝不假装成功）。
- **turn 结束信号**：`turnEnded`（通知或事件行，`method=='turnEnded'`）。

## 门序列（与 zcode_direct 同口径，不另起炉灶）

Popen/建目录前依次：入口配置加载 → `validate_selection`（复用 zcode_direct）→
availability 权威门（`quota_control.zcode_availability_gate`）→ 容量 claim
（`dispatch_pool.consume_for_entry`，executor='zcode'，同池同容量）→ 额度门
（`quota_control.gate_dispatch`，ZCode 手动额度政策照旧）→ 建证据目录。
Popen 后立即 `dispatch_pool.bind_child` 绑定真实子进程 PID + 创建身份（macOS 用
libproc 身份探针，BW-MAC-COMPAT-20261114-S1）；任何“已启动但终态未知”路径
`mark_unknown` 保留占位，绝不假释放。

结算：子进程真实退出 → availability 结算（executed=True）→ `dp.finish('finished',
success=…)` 释放容量 → `qc.settle_attempt(terminal='confirmed_exit')`。
Popen 后、提交前的拒绝（setModel 被官方拒绝等）→ graceful kill + wait →
`start_failed` 结算 + 释放。提交后 wall-clock 超时 → kill + 真实退出结算
（**超时 ≠ 429**，绝不写 healthy、绝不伪装限额）。

## 证据契约（与 zcode-direct 同形）

输出目录（拒绝覆盖已有目录）：

- `request.json` — 全部输入与策略（含 permission_policy、quota_gate、executor_identity）
- `sent-task-payload.bin` — 实际发送载荷（九节契约前置任务文本）+ 回读哈希
- `preflight.json` — `runtime/capabilities` 实测结果
- `events.jsonl` — 协议报文原件（send/recv 逐行，含全部权限请求与应答）
- `messages.json` — `session/messages` 原始结果
- `response.md` — 提取的最后一条助手消息文本 + SHA256 回读
- `process.json` — 子进程 PID/创建身份/终态
- `summary.json` — 终态汇总（protocol_success/turn_ended/timed_out/permission 计数/
  report_state 绑定/capacity_release）

退出码：0=协议成功；2=派发前拒绝（sent=false、零目录或零提交）；3=失败/未知。

## Mac 入口配置

`cp scripts/zcode-appserver-entry.json.example scripts/zcode-appserver-entry.json`
后填本机路径（已被 .gitignore 忽略）。两种可行形态：

1. **桌面端 Electron 二进制**（推荐，无需额外安装）：
   `command[0] = ~/Applications/ZCode.app/Contents/MacOS/ZCode`（或
   `/Applications/ZCode.app/...`），`command[1] = ~/.local/share/zcode-cli/zcode.cjs`，
   `command[2] = "app-server"`，`environment.ELECTRON_RUN_AS_NODE = "1"`。
2. **官方 CLI 启动脚本**：`command[0] = ~/.local/bin/zcode`，`command[1] = "app-server"`，
   不设置 ELECTRON_RUN_AS_NODE。

`zcode_direct.py --suggest-config` 会只读探测上述候选路径并输出报告（绝不代写配置）。
注意：桌面端自带 `zcode.cjs` **不能**作为 SDK bootstrap 模块导入（本机实测不导出 SDK 接口），
SDK 传输在 macOS 仍需官方源码树构建；本传输不需要。

## 本机模型可用性边界（2026-11-14）

本机以独立 `app-server` 启动时，个人 Coding Plan 和 Start Plan 的
`GLM-5.3` 请求都遇到空模型列表，`session/setModel` 拒绝后未提交任务。
桌面宿主通过 `host-account-config` 提供账户授权。这里的预检仅覆盖已测试的
`app-server` 启动方式。API key 型个人 provider 尚未在该路径通过真实派发验证。

现有 SDK 入口使用官方 `standalone` 注册表，可读取官方共享凭据存储中的
个人 Coding Plan OAuth 授权。Mac 验证应使用官方源码构建的 bootstrap，
通过 `BRAIN_WORKER_ZCODE_SOURCE` 指定源码目录后运行
`python3 scripts/zcode_direct.py --suggest-config`。探测将分别查找桌面的内置
provider 配置及用户的个人配置。在 macOS arm64 上构建官方 SDK 后，使用现有 BigModel 个人 Coding Plan OAuth，
并在系统提示时允许访问 ZCode 钥匙串，已分别对 `GLM-5.3` 和 `GLM-5.3-Flash`
完成 SDK 注册表预检。两者均通过模型选择校验，预检未创建会话或提交模型请求。

## macOS SDK 真实派工验收（2026-11-15）

在 macOS arm64 上，沿用 Windows 的 SDK 入口，使用已有 BigModel 个人 Coding Plan OAuth
授权真实调用 `GLM-5.3`，无需新建 API key。两次验证均取得模型回执，进程退出码为 0，
且验证了回执关联、并发名额释放及额度占位结算。

| 北京时间 | 验收环境 | 结果 | 模型记录用量 |
| --- | --- | --- | ---: |
| 10:52 | 独立验收库 | SDK 真实提交、模型返回和结算通过 | 4,231 tokens |
| 11:24 | 正式共享调度库 | 浮窗快照显示 ZCode 运行中、占用 1 槽，退出后确认释放 | 4,643 tokens |

实跑任务为最小文本回执。`GLM-5.3-Flash` 已通过选模预检。
独立 `app-server` 的 OAuth 选模限制仍以本页前一节的验证记录为准。

## 与 SDK 传输的能力边界（如实声明）

- 工具控制只有权限请求层：plan 模式官方原生只读；edit 模式下危险操作以权限请求
  出现，本入口默认全部 deny 并留证。没有 SDK 载体的整工具目录屏蔽、逐命令契约
  （`--command-contract`）与逐文件端口（`--file-scope-contract`）——这些 flag 在
  本入口不存在，需要该级别控制时用 zcode_direct.py。
- `--tools` 在本传输拒绝（非空即拒）：那是 SDK 专属开关，本传输无法如实执行。
- 模型绑定经 `session/setModel` 显式请求（官方拒绝即不提交）；官方运行时对
  provider/model 的实际接受度以该 RPC 真实结果为准，入口绝不宣称后端身份已核实。

## 离线回归

`tests/test_zcode_appserver_direct.py` + `tests/stub_zcode_appserver.py`：
14 项离线用例（零网络/零官方运行时/零模型调用），覆盖 happy path 九节绑定链、
权限默认 deny 与 `--allow-tools` 放行、runtimePreferences 固定应答、未知服务器
请求 error 应答、setModel 拒绝→start_failed、超时→真实退出结算、提交后崩溃、
容量满拒绝（零目录零占位）、preflight-only 零会话零容量、`--tools` 拒绝、
Windows 宿主拒绝、缓冲死锁负例（stub 突发多行）。
