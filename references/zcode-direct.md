# ZCode 官方运行时本机直连（SDK-CLI 画像）

复用本机已安装的 ZCode 官方运行时，在同一进程内完成 provider 注册、模型选择预检与
`submitPrompt`，让 brain-worker 不必再靠人工粘贴提示词。入口是纯标准库的
`scripts/zcode_direct.py`，它只负责参数校验、请求落盘、子进程管理与报告绑定；真正调用
官方 API 的是同目录的 `scripts/zcode_sdk_runner.mjs`。

本画像是 **SDK-CLI 直连**，不是通用四接口派发器：它沿用 Qoder 直连的九节兼容报告模板
（见 `references/qoder-direct.md` 末尾代码块），不套用 SKILL.md 的四节通用模板，也不得冒充
通用传输层。协议终态、正文绑定、业务验收三者分离，`business_verified` 恒为 false。

分层能力记录（submitted/工具可见/审批/闸门/started/真实退出+回执/业务验收）、现场放行最小条件、
独立失败分型与可信 deny 派生的验收口径见 [ZCode 能力验收与现场放行](zcode-capability-acceptance.md)；
本文只描述运行时操作细节，验收取证以该专项参考为准。

## 统一契约与哈希口径

- Qoder 与 ZCode 两个当前直连入口调用前使用 `scripts/prompt_contract.py` 的完整九节契约（已退休为人工转交/离线回放的 CodeBuddy 历史流水线同样复用该契约），含精确标题、阶段/路径同一行、首末独占标记和结语。`request.prompt` 的完整契约与任务进入 SDK `submitPrompt`。契约不能保证模型服从；原始响应照存，不合格仍保留 `bound=false`，不裁剪、不代写。
- 新调用的 `prompt_sha256` 与 dispatch-plan 均绑定原提示词文件的原始字节；另外保存实际任务文本、契约和完整发送通道的字节与哈希。换行转换口径显式记录，不把 JSON 文件哈希当 SDK prompt 哈希；Qoder 两通道不冒称一个后端合成载荷。留证边界只到本地 CLI/SDK 提交，不证明服务端处理后的文本。
- 不改提示词原文件或在途计划；旧调用记录沿用原入口版本的含义，升级后的新调用使用新计划。完整字段和留证位置见 [并行执行控制面](parallel-execution.md)。协议、报告绑定和独立业务验收分别报告，不能由本次传输回归推断真实业务恢复。

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
python <技能目录>/scripts/zcode_direct.py --workspace <项目绝对路径> --prompt-file <UTF-8任务文件> --output-dir <不存在的新证据目录> --stage <非空阶段编号> --provider account:bigmodel-individual-coding-plan --model GLM-5.3 --reasoning low --mode plan --tools Read,Glob,Grep
```

- 缺省选择：provider `account:bigmodel-individual-coding-plan`、模型 `GLM-5.3`（与并发容量池的
  主力组合 `zcode:GLM-5.3` 对齐；`GLM-5.3-Flash` 仍可显式选择并逐次预检）、
  `--reasoning low`、`--mode plan`、`--tools` 为空（即禁用全部内置工具）。
- `--mode` 只有 `plan` 与 `edit`：plan 只读，`--tools` 出现 `Write`/`Edit`/`Bash` 直接派工前
  拒绝；edit 需要用户明确授权修改本项目。官方支持的 `yolo` 一律不构造。
- `--preflight-only`：只查注册表可选性，不建 App、不建会话、不提交任何 prompt。它读
  ProviderRegistry 视图与 `validateSelection`，**不能证明可发送**——`disabledReason` 只有
  `App.listModels()` 才暴露，因此该模式如实记录 `disabled_reason_verified=false`。
- `--resume-session-id <官方会话id>`：必须与运行时实际 sessionId 逐字一致，否则 runner 在
  提交前拒绝；绑定环节再做一次精确比对。
- `--command-contract <JSON>`：可选，声明一组**逐字登记、显式 cwd** 的可执行命令；只有登记
  的精确命令能越过执行闸门，未登记命令（含看似只读的）一律拒绝。与 `--preflight-only` 互斥
  （预检不提交 prompt，无需审批）。未提供本契约却又在 `--tools` 里授予 `Bash` 时，入口在派工
  前直接拒绝，绝不默认放宽权限。真实 `zcode-entry.json` 里没有 `adapters_entry`，本阶段不修改、
  不复制该 entry，也不重装/改动官方 CLI：runner 从**已核对的 bootstrap 绝对路径**在同一棵公共
  SDK 树里定位同级 `adapters/src/index.ts`（校验存在且导出 `createNodeExecutionAdapter` 后绑定到
  请求），找不到即 fail-closed；配置里若显式给了 `adapters_entry` 则沿用（compat 保留，须为已存在
  绝对文件）。inner 适配器按官方语义以 `processEnv` 透传原 bootstrap 环境，不扩权继承。同理
  REPAIR3 由 bootstrap 同树派生公开 backend 解析器 `bootstrap/src/app/embedded-search-backend.ts` 的定位写入
  `embedded_search_backend_entry`（供 runner 冻结宿主可信 prelude），缺失即 fail-soft（不绑定、无 prelude 放行）。
  详见下文「受控命令审批与执行闸门」。

派工前拒绝（配置缺项、路径不存在、空 stage、非法 mode/reasoning/tools、输出目录已存在）
不创建证据目录，已有证据字节保持不变；重放同一目录一律拒绝。

入口在**建输出目录、Popen 之前**还必须先过跨会话共享的**并发容量门**（`scripts/dispatch_pool.py`，
见 [global-dispatch](global-dispatch.md)）：`consume_for_entry` 给了 `--dispatch-claim <token>` 就精确
校验（task/runtime/model/workspace/prompt_sha256 任一漂移即拒）并 `mark_running`，没给就走
`select_and_claim` 原子路由；非被选中组合 → `routing_required`/`sent=false`/退出 2、零证据目录、
不计轮次。可选 `--task-id`、`--dispatch-store`（默认 `BRAIN_WORKER_DISPATCH_STORE` 或
`~/.brain-worker/dispatch-pool.sqlite3`）、`--dispatch-claim`。容量门先于额度门；额度门拒绝时释放
容量名额（`finish(start_failed)`）。子进程真实结束立即释放名额，存活未知不释放、不被抢占，PID 复用
绑定创建时刻。**容量并发 ≠ 额度冷却**：ZCode 手动额度政策不变，两个 ZCode workspace 走同一 provider
时经**已校验的容量 claim**放行（`quota_control._verify_pool_claim` 只认 `runtime=='zcode'` 且
`dp.validate_claim(...)['ok']`），同 workspace 单写入与 unknown/在途保护不变，不删通道行、不禁用门禁、
不伪造 claim。

入口在**容量门之前**、任何 Popen 之前还要先过 §3b 的 **ZCode availability 权威闸**
（`quota_control.zcode_availability_gate`，见 [quota-routing](quota-routing.md)）：稳定通道键
`{provider}|{quota_group}` 被 `hard_hold`/`backoff`/`recovery_unverified` 阻断时，打印
`zcode_availability_rejected`/`sent:false`/退出 2、**零证据目录**；`--quota-recovery-probe` 走
`purpose='probe'` 的 CAS 单次有界放行。被拒/额度门拒/准备阶段中止都会 `_settle_availability(executed=False)` 归还本次
probe 占位（不算失败）。只有**受信任 429**（同一条真实错误条目内绑定的 status/provider/code/message，
非取消退出码 `4294967295`/`-1`）才在证据引用落定后 `record_zcode_unavailability`，写入
`summary['zcode_availability_recorded']`；真实终态再 `settle_zcode_attempt`
（`summary['zcode_availability_settled']`）。**证据已确认但接续冻结失败/终态未知时仍保存不可用事实、
且不释放活体调用锁**（fail-closed）。可选 `--quota-store`（默认 `BRAIN_WORKER_QUOTA_STORE`）、
`--quota-routes`。availability 记录**永不宣称真实额度/账务/免费**。

> **B4 加固（BW-AVAILABILITY-20261009-B4）**：`settle_zcode_attempt` 按 gate 回传的
> `granted_channel_key` 精确结算被授权行（不分裂别名/改名，代际不匹配一律拒）；一个真实执行的
> 失败 probe 消耗掉该代际**唯一一次**恢复资格（`recovery_eligibility_consumed`），退避到期不再同
> epoch 自动放行第二个定时 probe。`--quota-recovery-probe` 把 availability 授予的 `probe_ticket`
> （provider/channel_key/attempt_id/epoch）交给 `dispatch_pool` 容量门做**只读绑定核验**，取代可自报的
> `probe=true`——只有 CAS 授予的精确票据才放行这唯一一次启动。终态处理顺序收口：确认受信任 429 后
> **先 `record_zcode_unavailability`、后做容量释放**（state 已提交而容量释放失败绝不伪造 healthy）。

## 模型选择预检：不允许回落

runner 依次核对：注册表里 provider 与 model 是否存在 → `validateSelection` →
`App.listModels()` 是否命中且 `disabledReason` 为空 → `setModel` 后
`getCurrentModelOption().ref` 的 provider/model 是否逐字相同 → 推理档位是否一致 →
resume 会话 id 是否一致。任一项不符即写失败的 `preflight.json` 与非零退出，**不提交 prompt、
不改用其他模型**。turn 结束后再次比对选择，漂移即记为失败。默认请求 `GLM-5.3`（对齐主力组合
`zcode:GLM-5.3`）；`GLM-5.3-Flash` 等其它目标必须显式指定并在该次运行里实测通过，默认值不会自动降级或升级。

## 工具边界（只做整工具开关）

`--tools` 是**逐个内置工具的启用/禁用**，不是 Qoder 那种路径或命令规则，也不宣称机器级路径
限定；prompt 里的路径约束只是文字要求，没有文件级 sandbox。实现方式：

- 运行时 `runtimeConfig.toolDisallowlist` 装入已核对的官方内置工具全目录（含注册名为 `js`
  的 node_repl、`Task`/`Agent` 子 agent、`WebFetch`/`WebSearch` 联网、workflow、cron、
  off-peak、Skill、Todo 等），`--tools` 显式名单才逐项放行；未知名单项派工前拒绝。
- `submitPrompt({toolDisallowlist})` 再叠加运行时活注册表 `runtime.getToolRegistry().list()`
  里**未被显式放行**的项，避免遗漏动态注册项；活目录查询失败即在提交前拒绝，不回退静态目录。
  这条 `tool_disallowlist_effective`（= base deny ∪（`observed_tool_catalog` − `allowed_tools`），
  活 catalog 只做运行时追加 deny）是**比计划更严格的运行时边界**，须单列回读，
  不得宣称预检已经见过 live catalog；新并行计划的 `disallowed_tools` 应从受信任本机入口
  `build_tool_disallowlist(tools)` 派生并与预检 actual 精确相等，见
  [ZCode 能力验收与现场放行](zcode-capability-acceptance.md)。
- 隐藏通道按官方 API 证据关闭：`mcp:{enabled:false}` 不构造 mcpPort、
  `subagents:{enabled:false}` 不注册 SubagentPort、`dynamicWorkflowEnabled:false` 移除
  workflow 工具组、不传 `browserControlPort` 故 Browser 不可用、项目 hooks 依赖默认关闭的
  workspace trust、`memory.extractionEnabled:false` 关闭自动记忆抽取。
- 已核对的支持面只有 `Read`/`Glob`/`Grep`/`Write`/`Edit`/`Bash`；Task/子 agent、联网与
  Browser 工具不对本入口开放。若后续发现某隐藏通道无关闭证据，按本文档限制处理，不宣称已禁。

## 受控命令审批与执行闸门（--command-contract）

官方事实：`createZCodeApp` 支持注入 `permissionBroker`（`PermissionBrokerPort.requestPermission`
含 `options.signal`/`timeoutMs`/`claimResponse`）与 `executionPort`（`create-app.ts` 里
`options.executionPort ?? createNodeExecutionAdapter`）。**只读 Bash 会绕过 permissionBroker，
但仍必然经过 `executionPort.run`**，所以真正可执行的强制控制是**装饰官方公开执行端口**，
在派生子进程前逐字比较 `command` 与实际 `cwd`（并 realpath 复核）；单注入 broker 不足以覆盖只读
绕过。另有官方事实：`PermissionBrokerRequest`（`permission-flow.ts` 构造）**不含 cwd**，只有
`input`/`mode`/`reason`/`requestId`/`riskLevel`/`ruleId`/`sessionId`/`sideEffectScope`/`toolCallId`/
`toolName`/`traceId`/`turnId` 等字段，故 broker 的工作目录上下文必须由宿主（runner）realpath 冻结并
注入 `trustedWorkingDirectory`，绝不依赖不存在的 `request.cwd`/`input.workdir`；无法确认工作目录即
拒绝（`no-working-directory`）。全程不改写、不 monkey-patch 官方运行时。

契约结构（严格、类型化，未知/缺字段与越界一律派工前拒绝，退出 2）：

```json
{"privacy_free": false,
 "commands": [{"command": "ls scripts/", "cwd": "<工作区内绝对路径>",
               "input_sha256": {"<相对路径>": "<64位hex>"}}]}
```

- 顶层只允许 `privacy_free`（布尔）与 `commands`（非空列表）；每项只允许 `command`、`cwd`，
  可选 `input_sha256`。
- `command` 必须逐字非空、无空白裁剪；拒绝复合/变体：`;`、`&&`、`||`、`|`、换行、
  `>`、`>>`、`<` 等重定向/管道标记，以及 `cd ` 前缀。`cwd` 必须规范化到工作区内
  （realpath+normcase 精确比较，拒绝邻居前缀/`..`/绝对越界/符号链接逃逸）。
- `input_sha256` 提供“代码/测试输入范围”的声明式证明：键为工作区内相对路径、值为该文件当前
  字节 SHA；文件缺失或哈希不符即拒绝。**登记后文件被改写仍会被拦**：闸门在委派 `inner.run` 之前
  真实读取当前输入文件、再复核 realpath/工作区边界/SHA，缺失/被替换/不一致一律不启动 inner
  （`deny-input-missing` / `deny-input-sha-mismatch` / `deny-input-path-unresolved`）。输入按
  `(command,cwd)` 逐条绑定，同一命令的多个 cwd 各自携带独立 input（不丢弃后续）；注册表构建后
  深冻结（`Object.freeze`），worker 无法篡改注册表来扩权。**别名/重绑定防护**：规范化后的输入
  同时保留“原声明路径 + 登记物理目标 + SHA”，执行前用**原声明路径重新解析**并与登记目标比对，
  目标漂移（junction/alias 改指）即 `deny-input-target-moved`、不启动 inner；登记期若声明路径
  穿越 symlink/junction（`realpath` 与逐字路径不一致）一律**保守拒绝**该登记。**Python 归一化 →
  JS 真实消费**同一形状（JS 重新解析声明路径，不信任只喂旧目标）。**这仍是声明式输入范围，不是
  操作系统文件沙箱**，文档不得宣称文件级隔离。
- 入口把规范化后的契约与 `canonical_sha256`（与 JS 侧同一排序-无空格算法，跨语言逐字节一致）
  一并写入 `request.json`；runner 在内存里独立重算，与实际发送的契约字节比对，不符即 fail-closed。

闸门与留证（`scripts/zcode_permission_broker.mjs`，runner 动态导入真实模块，非自检常量）：

- `buildRegistry(contract, { workspaceRealPath })`：先 realpath 工作区（失败即抛，fail-closed），
  把每条命令编成 `byCommand: Map(cmd → Map(cwdKey → {command_sha256, inputEntries, variant}))`，
  每条 `cwd`/`input` 路径都 realpath 后断言仍在工作区内；`(command,cwd)` 各自携带独立 input，
  返回前 `Object.freeze` 深冻结，worker 无法篡改注册表扩权。
- `createGatedExecutionPort(innerPort, registry, sink, binding)`：公开面**只有 `run`/`close`**——即使 inner
  提供后台方法也不转发，故 `supportsBashBackgroundLifecycle` 为假。据源码：显式 `run_in_background`
  在无后台方法时由 core 抛 `ConfigurationError`（硬错误），而**超时自动转后台在无后台方法时不报错，
  回落到前台 `executionPort.run`（仍过本闸门）**——不是把全部后台都变成错误。在 `inner.run` 之前拒绝：
  argv 通道（`deny-channel-not-shell`）、非 `posix-bash` profile 或 `shellOverride.dialect` 非
  posix/git-bash（`deny-shell-profile-unexpected`）、命令非字符串、复合/变体（`deny-composite-or-variant`）、
  额外执行通道 `env`/`stdin`（`deny-extra-exec-channel`，注册命令不因旁路获得不同执行语义；**先于** prelude
  判定）、关闭 sandbox `enabled=false`/`dangerouslyDisableSandbox=true`（`deny-sandbox-disabled`）、未登记命令
  （`deny-unregistered-command`）、cwd 缺失/越界（`deny-cwd-missing`/`deny-cwd-outside-workspace`，realpath
  复核，Windows 才折叠大小写，拒绝 junction/alias 目标改指或符号链接逃逸）、cwd 不符（`deny-cwd-mismatch`）、
  input 复核失败（见上）。**官方 Bash 内部 `bashPrelude` 的兼容（REPAIR3）**：官方 handler 硬编码注入 typed
  embedded-search prelude（`shouldInjectEmbeddedSearchBashPrelude()` 恒 `true`，且**没有**既关 prelude 又留正常
  Bash 的会话级开关），旧闸门把它与 env/stdin 同列一律拒会让合法登记命令也无法执行；**不删拒绝、不放行全部**，
  而是由 runner 用同一棵公共 SDK 树的公开解析器 `resolveDefaultEmbeddedSearchBackend({env})` 取宿主可信 backend
  并**冻结**，回灌 `runtimeConfig.embeddedSearchBackend=冻结值` + `nativeSearchEnhancementsEnabled:false`（只关
  find/grep），令 handler 生成的 prelude 与绑定值**同源**；期望 prelude 精确为
  `{kind:'embedded-search', backend:冻结值, findAndGrepEnabled:false}`，其规范 SHA 作为 `binding` 传入闸门。
  `request.bashPrelude` 仅在（1）有绑定且逐字节等于该期望（backend 全字段与 `findAndGrepEnabled` 精确一致，无
  额外/缺失字段、不同 kind → `deny-bash-prelude-mismatch`）、（2）请求为**真实 Bash trace**（`sessionId`/`turnId`/
  `attributes.toolCallId` 非空且 `attributes.toolName==='Bash'`，否则 `deny-bash-trace-unverified`）时放行；无绑定
  时任何 prelude 仍 `deny-extra-exec-channel`。**不是只比 kind，更不从模型传来的 prelude 反向登记**。
  `captureCwdAfterSuccess` 是官方内部字段（adapter 内部追加 `pwd -P` 存临时文件、保留退出码，通常不加 stdout，
  命令入口仍是原命令），闸门**不因其存在而拒**，也不据此把内部包装误判为模型复合命令；不宣称 OS 文件沙箱/
  fine_grained。`NodeExecutionAdapterOptions` 无 `shellProfile`/`shellOverride`（二者属 `request.command`），不传
  无效 options；沿用既有可信 SDK 派生 shell 方言限制，不扩成通用 shell 配置器。拒绝回执用**官方有效形状**：status `failed`、error.type `unknown`
  （`ExecutionFailureType` 无 `denied`，绝不谎报 spawn/timeout/sandbox），stderr 带真实字节数，不委派 inner。
  计数诚实且分离：`attempted`（放行并入 inner）、`started`（观测到基础端口 started 事件）、
  `completed` 只等于 `status==='completed'` 完成回执；**spawn_error 计入 attempted/started 但不计入
  executed**；完成计数先落地再写审计，**执行后审计失败无法撤销已发生执行**；inner 抛错记
  `execution_attempt_failed` 后重抛，绝不伪造 `command_actually_executed=false`。`started` 计数在
  `execution_started` 事件里**先自增再写审计**，即便该审计写失败抛出也保留——runner/direct 单独透传
  `command_started`（异常/finally 路径同样同步），不把 failed/timed_out/cancelled 的实际启动说成零动作。
- `createPermissionBroker(registry, sink, { humanClient, trustedWorkingDirectory })`：绑定宿主 realpath
  冻结的工作目录上下文（A），无上下文即 `no-working-directory` 拒绝；**审批只约束 Bash 工具**——
  `toolName` 必须严格等于 `Bash`，缺失或其它工具（如 `Write`）即便携带同一个已登记 `input.command`
  也在 claim/allow **之前**判 `rule-denied: deny-tool-not-bash`，绝不把命令执行权限授予别的工具；
  已登记精确命令默认放行
  （`approved-registered`，不等待人工 UI），**绝不返回 permissionUpdates/sessionPermissionUpdates、
  也不改写 input**（不授予会话/持久 always-allow）；未登记/复合/非字符串 → `rule-denied`。**唯一胜者**
  （B）：`claimResponse` 至多 claim 一次且**在自动/人工分支之前判定**，输掉即 `race-lost`，两分支都绝不
  allow。人工分支遵循 signal（`cancelled`）、**仅正数 `timeoutMs` 才计时**（缺省不制造 0ms 假超时）、
  清理 listener+timer；缺客户端（client 抛 `No permission client available`）→ `client-missing`，其它
  异常 → `client-error`，批准 → `approved`，拒绝 → `rule-denied`。上述独立状态绝不归类为 GLM API/配额。
- 审计写入失败必须传播（`auditOrThrow`），拒绝路径同样先写审计。原始命令文本**只在“登记且匹配”
  且 `privacy_free=true`** 时留证；未登记/异常命令即使 `privacy_free=true` 也只留 `command_sha256`。

独立状态**分别**报告、互不推断（进入 `summary.json` 与信封；成功与 finally 两条路径都经
`syncApprovalStates` 同步，错误路径也如实反映，不伪造）：`tool_visible_bash` = **运行时活目录含
Bash ∧ 有效 disallowlist 不含 Bash**（取自实测目录，不是意图常量，H）；`approval_client_ready`；
`controlled_pre_exec_gate_ready`；`command_actually_executed`（是否真有 completed 完成回执，取自闸门
`_executedCount`）、`command_executed_receipts`、`command_attempted`（`_attemptedCount`）与
`command_started`（`_startedCount`）。REPAIR3 另加三个非敏感 prelude 绑定态：`embedded_search_prelude_bound`
（是否成功冻结并绑定宿主可信 prelude）、`embedded_search_backend_kind`（仅 `kind` 名，不含具体命令串）、
`embedded_search_prelude_sha256`（期望 prelude 规范 SHA）；审计 gate 行记 `has_env`/`has_stdin`/
`has_bash_prelude`/`capture_cwd_after_success` 存在性布尔，prelude 被拒时另记 `bash_prelude_sha256` 结构 hash，
**绝不落原始 env/stdin 或 SDK 账号文件**。无可靠
受控前置闸门来源（如 `adapters_entry` 不可解析）即 fail-closed：标志置假、拒绝提交，不宣称审批能力 ready。

能力确认边界（本阶段现状）：以上是**离线**实现，测试用真实 runner + 本地 bootstrap 双身、以**真实
官方 request 形状（含无 cwd 的 PermissionBrokerRequest；含官方 handler 真实注入的 embedded-search
`bashPrelude` 与 `captureCwdAfterSuccess`）**验证：注入被消费、冻结 prelude 的登记命令成功出哈希/退出码、
backend 差异/额外字段/非 Bash trace/缺 sessionId-turnId-toolCallId/额外 env 或 stdin 均被拒且 inner=0、
未登记/复合/错误 cwd 被拒、SHA 绑定不符 fail-closed、缺客户端/超时/取消/**无工作目录/竞速败（claim=false）**
分类、**exec-time input 改写被拦、spawn_error 不计 executed、attempted/started 单列**、preflight 不提交、
app/adapter/dispose 关闭无泄漏——**双身不调用真实模型、
不联网、不真起子进程**。因此**不得把离线桩通过写成现场能力**；只有主脑在一次隔离真实 SDK 验收里
取得独立命令回执后，才算现场确认。在此之前当次现场能力未验或组件未 ready 时的路由，见
[ZCode 能力验收与现场放行](zcode-capability-acceptance.md) 默认路由（计划阶段限定 ZCode 只做读写 →
既有 Qoder 串行登记测试；实际授权/环境/自动审批拒绝不能换执行器绕过）。
ZCode 的 CAPABILITY `fine_grained` 维持 `false`（dispatch-plan 无法表达 `grants.edits` 与 `grants.bash`
两种细粒度规则；`command_contract` 是另一套本地逐字命令闸门、不授文件沙箱）。

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

- 本机已真实验证 BigModel 的 `account:bigmodel-individual-coding-plan`（历史实测模型为 GLM-5.3-Flash）。入口 `DEFAULT_MODEL` 现为 `GLM-5.3`（对齐并发容量池主力组合 `zcode:GLM-5.3` 的**请求值**），但该默认只是请求口径、不等于已在本机现场实测通过：任何模型（含 GLM-5.3）仍逐次经模型选择预检，漂移即失败，不据此宣称 GLM-5.3 已现场验证。
- Start Plan / Trust Build 免费池没有接入证据，不能把已登录个人 coding plan 的调用认定为免费。
- Read/Edit/Write 已真实派工验证。Bash 只代表工具可见性：本阶段新增受控命令审批（装饰官方
  执行端口为前置闸门 + permissionBroker），但仅离线（bootstrap 双身、真实 runner）验证，
  **尚未取得隔离真实现场回执**，故不宣称现场执行 Agent 已运行，也不把离线桩通过写成现场能力；
  REPAIR3 已修正 LIVE-POSITIVE 里官方 Bash 自动注入 `bashPrelude` 被旧闸门并入 env/stdin 一律
  `deny-extra-exec-channel`（broker 判 `approved-registered` 但 attempted/started/receipt 全 0）的兼容缺口——
  现改为绑定宿主冻结的 SDK 内部 prelude、且真实 Bash trace 才放行；该拒绝**不是** GLM/登录故障，
  也**不能**当作已实际执行；现场真实成功仍待主脑隔离重验。
  未验/未 ready 时路由见 [ZCode 能力验收与现场放行](zcode-capability-acceptance.md) 默认路由
  （读写 → Qoder 登记测试；授权/环境/自动审批拒绝不换执行器绕过）；注：真跑出非零退出属**已执行但失败**，
  不抹成未验或零动作，只有拒绝/未启动分支才未执行。
- SDK 成功后的 projection 可为 idle；协议成功必须同时取得匹配 session/turn 的成功 turn_complete、同文回复和全部真实 model_request 模型记录。单独 idle 或 completed 均不能证明完成。
- 每次结束先等待 App.close，再释放 Registry，最后输出一次信封。原始报告及失败证据保留。

## 执行证据门禁（execution_evidence_ok）

`report_bound` 只证明“协议成功且报告格式合格”，不证明工程任务真实执行。为此入口提供可选的
`--execution-contract <JSON>`，由 `scripts/zcode_execution_evidence.py` 在派工前校验契约并
快照 baseline，在轮次结束后核对“最小任务执行契约 × 同 session/turn 工具事件 × 磁盘回读 ×
派工前 baseline”，产出 `execution_evidence_ok`（true/false/null）与
`execution_evidence_status`，写入 `execution-evidence.json` 并进入 `summary.json`。
`business_verified` 与 `free_quota_verified` 含义不变，仍由主脑独立验收。实际契约与
baseline 哈希随 `request.json` 留证（`execution_contract` / `execution_contract_sha256` /
`execution_contract_baseline`），不依赖执行后可改写的契约文件。

契约字段（调用前声明，未知字段拒绝）：`task_type`（`engineering` / `review_no_change` /
`reasoning`）、`required_reads`（必需读取的工作区相对路径）、`expected_artifacts`（纯新
产物，派工前必须不存在，旧文件不能充当）、`required_modified_files`（必需修改的已有文件，
派工前必须存在）、`allow_no_changes`（是否允许无改动）、`required_test_command`（可选）与
`test_evidence`（可选，精确字段为 name/argv/cwd/evidence_path/inputs；inputs 提供
登记路径的原口径字符串，建议与登记执行器注册表一致的绝对路径——哈希归属比较用
normcase(realpath) 规范化键，指纹复算用原登记路径字符串，两套口径不得混用）。
工程任务不声明任何写成果（expected_artifacts/required_modified_files 皆空）时必须
显式 `allow_no_changes=true`，与是否有必读文件无关。契约路径只能在工作
区内（realpath+normcase 完整规范化精确比较，拒绝邻居前缀、`..`、绝对越界与符号链接逃
逸）；空白路径、布尔/数值伪值、结构矛盾（reasoning 带读取、review_no_change 带产物/修改、
allow_no_changes 带产物/修改、新产物已存在、工程任务既不声明读取/成果也不显式
allow_no_changes=true、required_test_command 不等于 `subprocess.list2cmdline(argv)`）在派工
前拒绝（退出 2），不建证据目录。`required_test_command` 与 argv 的绑定口径是
`subprocess.list2cmdline(test_evidence.argv)`，它不是可选注释。

判定规则（已核对事件结构：`tool_call_scheduled.payload` 有 toolCallId/toolName/input，
Read 的 input.file_path；`tool_call_result.payload` 有 toolCallId/result（含严格布尔
`result.success`）；`tool_call_error.payload`（S2 归并兼容）有 `toolCallId` 与 `error` 对象
（`code`/`type`/`message`/`detail`/`stack`），**没有** `result.success`；结果/错误事件均无
toolName，按 toolCallId 关联，不靠顺序配对；只接受同 sessionId/turnId）：

- 没有契约时 `execution_evidence_ok` 恒为 null（`unverified_no_contract`），绝不默认 true；
  runner 信封缺 session/turn → `unverified_no_session_turn`；契约存在但 preflight-only
  未提交 → `unverified_preflight_only`；缺派工前 baseline → `unverified_no_baseline`。
- 需要读源码的工程任务在匹配 turn 内零工具调用 → `no_required_execution`；
  `required_reads` 必须命中工作区规范化后的完整路径，`other/src.py`、`src.py.bak` 等
  同名/前缀伪匹配不算。
- 成功 Write/Edit 必须有匹配 toolCallId 的成功结果回执且产物磁盘回读存在；`required_
  modified_files` 还要求回读哈希与派工前 baseline 不同（纯改回执不变不算修改），`expected_
  artifacts` 要求派工前不存在、有回执且磁盘存在；任一不满足 → `execution_claim_mismatch`。
- 观察到越界读写路径（工作区外的成功 Read/Write/Edit）直接 `execution_claim_mismatch`，
  不回退读取工作区外文件。
- 合法无改动审查（`review_no_change`）必须有必要的读取证据，允许零写入；出现成功
  Write/Edit 回执即判 `execution_claim_mismatch`。纯推理任务（`reasoning`）不强求工具。
- 工具结果报告授权拒绝 → `permission_denied`：报告原拒绝，不改判为 GPT 代做、传输失败，
  也不换执行器或入口绕过。
- 同 toolCallId 的 scheduled 仅按（toolName, input）判身份，一致重放允许，冲突拒绝；
  result 仅按（严格布尔 success, result 内容）判身份——`success` 缺失、字符串、数值或
  null 一律 `tool_result_malformed`，失败回执绝不能靠 truthy 变成功；payload 非 dict、
  toolCallId/toolName 缺失同样干净拒绝；成功/失败或内容冲突 → `tool_result_conflict`；
  缺 schedule 的 orphan result、scheduled 无结果分别判 `tool_result_orphan` /
  `tool_result_missing`。此处「不强制 started 事件（调度 + 成功回执即可构成证据）」**只限**普通
  Read/Edit/Write 等机械执行证据；**Bash 专项现场放行额外要求该命令同 `toolCallId` 的独立
  `execution_started` 审计**，同一 toolId 回执里的 `started` 累计布尔不算逐命令证明。也不把
  流式事件数当动作数。同 call 的 result/error 终态**只按事件 kind + 规范终态 identity 比较**
  判重放：终态正文一致即幂等，外层 duration/计时等元数据不同不制造冲突；正文（identity）漂移
  或 kind 不同（success result 与 error 并存）一律拒绝，`result.success` 严格布尔不放松。
- **tool_call_error 明确失败终态归并（S2 兼容 / S3 边界收紧）**：`tool_call_error` 与
  `tool_call_result` 一样按 toolCallId 配对其 `tool_call_scheduled` 并计入终态，**不再**因缺
  `result.success` 误判 `tool_result_missing`。归一化为**失败**：`error` 必须是 dict，已知文本
  字段 `code/type/detail/stack` 若存在须为字符串，且必须带**非空 `message` 字符串**才算有效错误
  内容——`{}`、只有陌生键的 dict、`message` 非字符串或已知字段类型错误一律 `tool_result_malformed`，
  绝不用空/任意 dict 伪造终态；合法 message-only 错误仍归失败（不是成功），未知附加元数据被忽略、
  不进入规范正文也不充当错误内容。身份为已知字段的规范序列化 `(False, canon)`；同 call 一致重放
  幂等，身份漂移或 success 与 error 冲突一律 `tool_result_conflict`；孤立 error（无 scheduled）→
  `tool_result_orphan`。分类：普通 error → `tool_result_failed`；**仅查 `code`/`type` 与约定
  `message`/`detail` 文本字段**命中权限拒绝 → `permission_denied`（排除 `stack`、未知键名与无关
  元数据噪音，如 `{'permission': False}` 或 stack 里的 `PermissionBroker` 不误判；真正明确拒绝
  即便外层 `is_error=false` 也不掩盖）。`streaming_tool_ledger_updated` 的
  `payload.status==tool_result_committed` **只是状态字段、不是事件 type、绝不当成功**：仅
  scheduled+committed 而无 result/error 终态仍判 `tool_result_missing`；committed 与 error 共存
  仍按 error 归失败、不放行。原始事件全部保留、不改写，不伪造报告、不放行业务。跨 session/turn
  的 error 不参与本轮判定（其本 turn scheduled 仍无终态 → missing）。
- `required_test_command` 只能由 `test_evidence` 满足，校验复用
  `execution_control.run_registered_test`/`compute_fingerprint` 的真实结构：精确 name、
  精确 argv、cwd 工作区内 realpath 相等、`exit_code` 严格整数 0（bool 不算）、
  `inputs_unchanged is True`、已登记 `inputs` 的**当前**哈希逐项一致（不只信标志）、
  `env_sha256`/`runtime_version` 与 `fingerprint` 内部重算一致、stdout/stderr 日志
  realpath 在工作区内且 SHA 回读一致。任何坏类型/缺字段（含 evidence.json 为 `[]` 之
  类结构错误）返回具体 `tests_not_run` 理由并退出 3，不抛未处理异常。事件里的任意
  Bash 调用不构成目标测试已运行的证明；写过文件不豁免测试检查；测试未要求运行时不
  因 Bash 禁用判权限失败。
- 别的 session/turn 的事件不参与判定；`turn_complete.payload.toolCallCount` 只是计数
  线索，不能单靠 `toolCallCount>0` 证明完成，也不解析中文报告关键词冒充真实性验证。
- 明确契约下 `execution_evidence_ok` 非 true（false 或 null）时，入口不按协议成功返回
  退出 0，而按失败退出 3 交付调度（无契约的旧调用保持原退出语义）；`protocol_success`/
  `report_bound` 原意不变，也不伪报 API/权限/额度错误。`summary.diagnostics.
  failure_types` 单列 `no_required_execution` 与 `execution_claim_mismatch`（仅由结构化
  门禁状态提取，不扫描提示/报告关键词）。

能力边界：本门禁只做机械事实核对（工具回执、磁盘回读、baseline 差异、SHA），自然语言
主张的真实性与业务质量仍由主脑审查；摘要不含源码、令牌或完整工具内容，原始事件只落盘
不整份打印。

## 离线验证

```text
python tests/test_zcode_direct_offline.py
node --check scripts/zcode_sdk_runner.mjs
node --check scripts/zcode_permission_broker.mjs
python tests/test_zcode_sdk_runner.py
python tests/test_zcode_execution_evidence.py
python tests/test_zcode_permission_broker.py
```

测试用真实子进程 stub（`tests/stub_zcode_runner.py`）镜像官方 runner 的 argv 与信封，并把
收到的完整请求 JSON 与实际 submit 次数写进 `stub-receipts.jsonl`，因此“零提交”结论来自子进程
凭证而非文本匹配。测试不登录、不联网、不调用任何模型，也不证明真实后端能力。

## 派工范围与观测边界（BW-ZCODE-MANUAL-20261008-S2 指导）

本节是主脑派工与观测口径的通用指导，不绑定本广告业务的具体授权或参数，不得据此把某项
一次性授权写成通用 Skill。CodeBuddy 直连已退休为人工转交、其冷却门禁仅在离线回放中保留，S1 的 ZCode 手动额度政策保持现状，本节
不改变它们。

- 范围按**独立可验收成果**收敛，判据是状态机、副作用、必要接口与验证面、读取量，而不是
  “两文件”或“步长 1”这类机械阈值；耦合确有必要时保留完整闭环，不为凑数强行拆小。提示词
  给出**精确函数与 harness**，优先分段定位、避免重复完整读取；字节数不等于实际 tokens。
- **文字路径范围、工具可见性、实际授权**三者分别记录：裸 Read 不是文件 sandbox；一条
  绝对单文件规则若未经本机验证，不能声称已生效；不静默扩权，也不读取凭据。
- 状态严格分型：已派发 / 真实 Read / 等待消息 / 真实写入 / 报告绑定 / 独立测试 / 业务回读。
  静默或心跳不等于死锁、429 或工具执行；Windows 退出码 `4294967295` 不是 HTTP 429。
  `MAX_RETRIES` 不是总模型次数，也不是全程时限。用户选择继续等待的**在途任务**不得追溯套用
  新超时、换模型或重派；超时与增量流参数只作为**未来显式配置**，须事先授权并通过兼容性验证
  后才启用——本轮不实现新超时、不新增 CLI 流参数。
- 能力分面独立记录：模型路由、工具可见、审批组件/端口 ready、`command_started`、真实退出、
  输出回执。当前 `command_contract` 路径**已注入 permissionBroker**，不能继续声称当前 SDK
  完全缺失审批客户端；某次任务主动屏蔽 Bash 属于该次边界选择，不算新失败。CodeBuddy、
  ZCode SDK、宿主自动审批分别归型；对未登记命令的拒绝不代表整套测试不可用，更不因此放宽
  权限。
- 已有 Read/Glob/Grep 时**不额外运行** ls/date/cat/cd/echo/包装命令。已知反例：S1 实际尝试
  过 2 条未登记命令（`cd && python -c`、`date`/`echo`）均被权限模式拒绝、**没有执行**，须如实
  记为“未获授权、未执行”，不写成环境故障；本轮不再重试。未知信息写“未知”，时间由主脑记录。
- 最终代码对应**本次测试指纹哈希**，不把旧绿测当新结果、重叠的专项测与全量测不相加；
  原始报告的格式失败与代码/测试验收分开，不为仅修格式而重派；错误或部分成果如实保留，
  GPT/Luna 的工程实施不自动替代执行器。WorkBuddy 继续排除。

上述边界同样约束工具事件证据：`tool_call_error` 归并为明确失败终态（见“执行证据门禁”），
`tool_result_committed` 只是状态字段、绝不充当成功。
