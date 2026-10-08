# brain-worker

`brain-worker` 是一个 Codex Skill，用来固化“主脑 + 苦力 Agent”的分阶段协作方式。

## 项目核心

「牛马」项目以 GPT 作为主脑，负责逻辑推导、规划、任务分解及验收；国内桌面端 agent 通过 CLI 或 API 接入，承担具体执行，以节省 GPT 额度。

后续规划、任务分解、执行器接入和范围判断均以这一核心为依据，避免项目偏离节省 GPT 额度的主旨。

执行器范围（2026-10-08 用户决定，同日最新追加）：WorkBuddy 与独立 CodeBuddy CLI 均改为 human-relay only，不再从当前 Skill/CLI/控制面提交新的直接派发调用；选择它们时只生成完整可复制提示词，由客户人工交给外部 Agent，生成提示词不记成已派发。当前直连派发只保留 Qoder 与 ZCode，默认仍为 ZCode 优先、Qoder 沿用现有唯一模型及入口兜底。不再启动 WorkBuddy 的认证或测试。历史能力、原错误与模型信息与测试证据保留，仅作为离线证据回放/档案，不作为重新启用授权。

### 持久额度冷却与路由门禁（2026-10-08，BW-QUOTA-20261008-S1）

CodeBuddy/ZCode 的所有真实派发在 Popen 前经过 `scripts/quota_control.py` 的持久门禁（sqlite3 默认共享 store）：明确 429 按带时区 reset/Retry-After/保守无窗口分级冷却，跨进程持久生效；额度分组只来自受信任本机 `quota-routes.json`（CodeBuddy/ZCode 独立为本机 user_confirmed，非账单核实），未知关系进保守共享组；同一真实工作区单一写入执行器；到期仅 recovery_unverified，需单次有界 probe 通过才清。**2026-10-08 用户决定（BW-ZCODE-MANUAL-QUOTA-20261008-S1）：ZCode 额度改由用户手动管理/重置，取消自动额度冷却——`gate_dispatch(runtime='zcode')` 及 ZCode 的 `settle_attempt`/`import_terminal` 跳过全部额度冷却/到期/自动恢复判定（429 分类仍如实提取，但不写 cooldown），缺 routes、落 unknown-shared 或共享组历史冷却都不再挡 ZCode；这是按真实 runtime 的固定策略、不可由任务书关闭，也不提供关闭 CodeBuddy 冷却的开关；CodeBuddy 及其它 runtime 冷却规则完全不变，ZCode 也不得清除/覆盖同组里 CodeBuddy 的冷却。此处只取消额度冷却门禁，ZCode 的工作区单一写入与通道单在途并发占位仍保留（跨 CB/Z 同一真实工作区仍串行、同组跨工作区仍单在途），普通 ZCode 派工不要求 recovery probe。** 中断接续用 `scripts/continuation_contract.py` 冻结受控文件清单与测试证据。详见 [quota-routing](references/quota-routing.md)。

### 全局并发容量池与 1:1 路由（2026-10-08，BW-GITHUB-CLOSEOUT-S4）

所有会话共享同一持久并发容量池 `scripts/dispatch_pool.py`（纯标准库 sqlite3，默认 `~/.brain-worker/dispatch-pool.sqlite3`，`BRAIN_WORKER_DISPATCH_STORE` 覆盖）：主力 `zcode:GLM-5.3` 与 `qoder:Qwen3.8-Max` 各最多 2 个真实在途执行器、按 committed 计数做 **best-effort 1:1** 轮换（非严格均衡）；溢出 `qoder:Qwen3.8-Flash` 最多 2 个，**只有两主力池都满**才允许；国内合计 6。Luna（`luna:native`）只做救援、**无数量上限**：只有六名额全满、宿主真的问过用户、且 300 秒无回复后，经 `claim-due` 原子竞争裁决才可由宿主原生调用（等待超时≠默认无限授权；国内名额释放优先国内并原子取消同 task 的 pending 票据，不双派；原生工具启动后崩溃在写出 agentID 前 → `launch_unknown` 待人工核验、绝不自动重复启动）。Qoder/ZCode 入口在建输出目录、Popen 前**必须消费/校验一个容量 claim**（不能靠提示词或传入布尔跳过），非被选中组合 → `routing_required`/`sent=false`/退出 2、零证据目录、不计轮次；子进程真实结束立即释放名额，存活未知不释放、不被抢占，PID 复用绑定创建时刻。**容量并发 ≠ 额度冷却**：额度门禁仍在 `quota_control`。CodeBuddy/WorkBuddy 已退休为 human-relay only、不进池，生产 `codebuddy_direct` 已彻底移除可执行传输（不再 `import subprocess`/`dispatch_core`），真实传输管线整体迁到仅测试的 `tests/offline_codebuddy_harness.py::replay_dispatch`。详见 [global-dispatch](references/global-dispatch.md)。

### GPT-6 Luna 原生子 Agent（可选）

GPT-6 Luna（模型标识 `gpt-6-luna`）是可选执行端，通过所在宿主实际提供的原生子 Agent 工具调用。本机未另行指定时 ZCode 优先；Qoder 沿用现有唯一模型及入口配置兜底。Luna 不自动接替任何在途任务，也不改变适用任务/在途任务已有的执行选择；GPT/Luna 的工程实施须用户事先明确同意，不自动代做。

每次考虑调用 Luna 时，检查当前环境工具是否提供该模型、当前用户授权及审批要求，并遵循当次生效的 custom rule 与平台规则；需要审批时取得对应批准后才启动。能力登记、历史启动成功或工具存在都不是后续调用授权；不把动态规则、过去批准或某次测试参数固化为永久授权或默认参数。

原生子 Agent 只在所属云环境中执行，不能直接操作用户电脑。本云端主聊涉及用户电脑的本地工作仍通过已授权的 `cloud_threads` 进入对应本机环境，并遵循该环境的工具与权限边界；不得把中转能力归给 Luna。此项不提供统一 CLI，不把 `gpt-6-luna` 填进 Qoder、ZCode、CodeBuddy 的模型配置，也不宣称通用 CLI/四接口协议已接通。

已有启动证据：根线程报告于 2026-10-07 使用 `collaboration.spawn_agent(model="gpt-6-luna", reasoning_effort="xhigh", fork_turns="none")` 完成一次合成排序验证，子 Agent 正常返回 `[2,5,7]`。这只支持该次原生启动与小任务返回；本机仅按根线程提供的结果登记，未独立回读原始云端调用日志，未重新调用。不证明本地桌面能力、完整业务闭环或套餐/额度节省。后续任务按实际任务标识保留原始交付、核对环境和副作用并由主脑独立验收；详细边界见 [Luna 原生执行端参考](references/luna-native.md)。

Skill 使用与执行派发分别记录：`brain_worker=used/not-used`；外部执行仍记 `external_dispatch=dispatched/not-dispatched`，原生调用另记 `native_dispatch=dispatched/not-dispatched`、实际模型、任务标识与次数。仅更新指导或复用历史证据时，两种派发都记未派发，不计作新的执行轮次。

### 本机 DOTS 默认工作流（2026-10-07 用户要求）

用户通过本机 DOTS 安排任务时，默认先实际读取并应用 brain-worker Skill，完成适用性判断、规划与分工；已授权且具备所需能力的具体执行优先交给已就绪的国内桌面 agent（CLI 或 API），GPT 保留必要的逻辑推导、任务分解和独立验收。简单低成本任务仍先做判断；不适合委派、工具不可用或权限受限时，明确说明原因和实际处理方式。

每次执行前及最终汇报明确记录：brain_worker=used/not-used、实际 SKILL.md 路径、已应用的步骤与证据；另记 external_dispatch=dispatched/not-dispatched 及原因。used 仅用于已实际读取并按 Skill 执行本次适用步骤；只写说明、理解理念或准备提示词不能冒称已使用或已派工。未读取或未应用时记 not-used，并主动提醒用户原因；已使用 Skill 做判断但未派工时，分开说明，不能掩盖执行状态。

此偏好不扩大用户授权、不覆盖安全要求；实际派工仍须确认运行时、模型、工具授权、隔离工作区和报告回读条件。CodeBuddy 后续测试选择 GLM 5.3 Flash，调用前核实接入端实际可用模型 ID，不可用则报告障碍，不静默回退 GLM 5.3；这不是当前支持、价格或额度的验证结果。

已落实范围是本项目说明、对应 Obsidian 笔记、本机 brain-worker 安装版与仓库版的指导及 UI 默认提示。现有对话若缓存旧 Skill，需重新读取更新后的安装版；本规则不是已接通的 DOTS 全局自动路由，也不能保证未加载该 Skill 的其他入口自动执行。

### 本机显式 local 台账模式（适用任务，2026-10-07 用户决定）

适用任务由用户在本机当次会话明确选择 local 台账时，不调用 Hermes 配置或飞书认证/读写。仓库版与安装版 scripts/local_ledger.py、scripts/ledger_core.py 采用同一实现；调用前须由用户显式设置 `BRAIN_WORKER_LEDGER=<本机隔离台账绝对路径>`，禁止落到默认用户目录，也不把这一路径当通用默认值固化给其他人。

该次任务及其验收后的接续阶段，台账门禁使用该本地入口的 status/record/resolve，取代旧飞书专属命令、模板和共享来源要求；后文飞书指令仅用于另外明确选择并授权 feishu 的任务。local 就绪和本地回读足以满足该任务的台账门禁，不要求为此读取或写入飞书，也不把远程台账未同步当成本地记录失败。

这是独立的新本地事件链，未迁移、导入、重算或覆盖历史远程评分；初始50分/步长1/样本0仅是本地初值。mode.json 明确 remote_score_synced=false、remote_write_performed=false、remote_history_imported=false。没有真实执行报告时只做status，不提前评价或计分；验收后以稳定event-id本地record，身份未证实不传--identity-confirmed，补证后本地resolve，写后回读。同阶段幂等，不因补修或重试新建加分样本。

后续明确授权远程同步前，不运行 capability_ledger.py，不配置认证，不外发评分、报告或消息。派工与台账分开验证：台账就绪不是模型已调用或业务完成；执行端仍按 Qoder/ZCode 直连兜底，CodeBuddy 相关测试改为生成提示词人工转交（不再直接派发）。

### 本机并行协作调度约定（2026-10-07 用户补充，泛化描述）

目标仍是 GPT 主脑负责必要推导、规划与验收，国内执行器承担具体执行，以节省 GPT 额度。用户在本机并行推进的多条链路（例如某业务任务与 brain-worker/CodeBuddy 修复链路）经父线程互传已验证结果，业务不等待适配器修复；具体的任务标识与台账路径由用户当次指定，本仓库不固化任何单一任务、目录或授权。

用户指定的业务任务顺序（2026-10-08 最新决定）：小范围只读定位一类任务若选 CodeBuddy，则改为生成完整可复制提示词由客户人工交外部 Agent，不再从入口直接派发；ZCode 优先直接派工作为主路径，Qoder 沿用唯一模型兜底。ZCode/Qoder 的模型按各自档案与授权核对。切换先确认原调用终态，或明确停止并核对副作用；未知状态先查，不把"立即接续"变成同一目标的重复在途派发。成功与失败的原件保留。历史曾由独立 CodeBuddy CLI 用 GLM 5.3 Flash 先做定位、故障后 ZCode 接续，该直连步骤已停用、仅存档。

执行端角色（2026-10-07 用户补充，2026-10-08 更新）：本机未另行指定执行者时 ZCode 优先（用户付费套餐，用户反馈有额度且速度快，未独立核实账单或剩余额度）；Qoder 沿用现有唯一模型及入口配置兜底，不覆盖配置、不新增或替换模型，也不重复询问模型名称；用户反馈当前活动无消耗，尚未独立核实，不当作免费事实。明确用户选择与在途任务不替换：选择变更只应用于验收后的下一轮。GPT 主脑可做规划、推理、确定性输入准备和独立验收；GPT/Luna 工程实施须用户事先明确同意，不自动代做。外派被授权校验、自动审批或环境限制拦住时报告原拒绝，不改成 GPT 代做，也不换执行器或其他入口绕过；普通已终态执行失败是否兜底，按已授权顺序决定。WorkBuddy/CodeBuddy 不再直接派发，只做提示词人工转交；历史 Flash-only 模型口径仅适用于旧 CodeBuddy 直连档案，不强制其他执行端切换 Flash。

牛马修复链路按错误证据是否到达分级处置，不预设“当前没有新报告”这一状态：若本阶段确有新的真实错误证据，据其定位适配器/Skill 缺口，在独立范围内准备、验收后经父线程反馈；若此刻尚无新的错误报告，则等待父线程转交，不凭历史 429 或猜测重跑测试，不重复调用适用任务，也不修改适用任务占用的文件、工作区或运行对象。修复仅针对报告所证实的适配器/Skill 缺口；双方成果互推，但不把一方未验收状态当作另一方成功基线。

交接按同一 task/stage、request/prompt哈希和session/turn标识串联 request、process退出码、原始stdout/stderr、summary、report-state、已有response/产物及其哈希；保留错误码、权限拒绝、用量和缓存口径、实际副作用。先查可恢复结果，再决定是否需要新调用；消耗、CLI退出0或运行结束均不等于完成。不传凭据，也不凭空补写worker报告。

### CodeBuddy 模型口径（2026-10-07 用户指定，现为历史/人工转交背景）

CodeBuddy 直连已退休为 human-relay only，不再由入口直接派发。历史档案：后续 CodeBuddy 测试使用 GLM 5.3 Flash，不使用此前的 GLM 5.3，以减少测试消耗；人工转交提示词给外部 Agent 时若涉及模型，须由客户从接入端资料核实实际可用模型 ID，若 Flash 不可用应报告障碍，不得静默回退 GLM 5.3。

这是历史测试约束，不代表当前已配置或已测试，也不证明模型当前支持、价格或额度；当前仅生成可复制提示词，不启动新的 CodeBuddy 模型调用。

主脑负责理解需求、确认项目路径和授权边界、拆分当前阶段、审查风险、验收证据并决定下一步。大量代码阅读、批量修改、测试、迁移、审计和其他耗时执行工作交给执行 Agent——`human-relay` 下由用户转发提示词给外部桌面 Agent，`api-direct` 下由主脑经用户配置的受控工具下发任务。Luna 原生可选执行遵循上方档案与当前调用审批，不替代本机外部执行。

## 快速上手

两个使用场景，共用同一套核心规则：

- **GPT 桌面（Windows，原有用法）**：零配置——GPT 做主脑，按 `human-relay` 生成完整可复制的阶段提示词，用户转发外部桌面 Agent，报告保存为原始文件后通知主脑，主脑直接读取原文件验收（无法访问时上传）。不需要创建 `config.yaml`，不需要任何 API。
- **Linux**：主脑运行环境 2 选 1（`muse` / `codex-cli`，仅此时需要配置）。`muse` 是用户指定的 Linux 主脑运行环境名称，本仓库不假定其厂商、安装命令或接口。

```bash
git clone https://github.com/flamebird07/brain-worker.git
cd brain-worker
cp config.yaml.example config.yaml   # 仅 Linux 场景选择主脑、或需调整执行方式/台账时；缺省一律按 human-relay + local
```

- 外部执行方式 2 选 1（另有上方 Luna 云端原生可选档案，不在该配置枚举中）：`human-relay`（默认，零 API 配置：主脑生成提示词，用户转发给外部桌面 Agent）/ `api-direct`（主脑经用户显式配置的受控工具下发；未配置时不得启用；显式选择后缺配置则报告未就绪，不静默回退；仓库不含通用调度器；已有 Qoder CLI 专项直连入口，见下方）。
- 台账后端 2 选 1：`local`（默认，本地 JSONL，纯标准库零配置，开箱即用）/ `feishu`（可选，需自建飞书应用与表格，见 SKILL.md“飞书后端接入（可选）”）。
- Codex CLI 当主脑时，用 `codex exec` 加载本 skill，按 `config.yaml` 的 dispatch 命令下发任务；不得以 Codex 子 Agent 冒充外部执行；Luna 原生执行须按当前宿主工具、授权与审批单独核对。

## 核心规则

### Windows ZCode 自动派工

已登录的本机 ZCode 官方运行时可通过 `scripts/zcode_direct.py` 直接派工、接收原始报告并绑定验收，无需来回复制提示词和报告。把 `scripts/zcode-entry.json.example` 复制为本机配置并填写运行时路径；默认 GLM-5.3-Flash，GLM-5.3 须显式指定和预检。调用方法及限制见 [ZCode 直连参考](references/zcode-direct.md)。它是同进程官方 SDK 入口，不是裸 headless CLI 或通用后台调度器。

Read/Edit/Write 已实测。Bash 命令审批已实现（`--command-contract` 逐字登记且显式 cwd 的精确命令，
以装饰官方公开执行端口为前置闸门 + 官方 `permissionBroker`；只读 Bash 绕过 broker 仍必经
`executionPort.run`，故以装饰执行端口为强制点。官方 `PermissionBrokerRequest` 无 cwd，broker 改用宿主
realpath 冻结的 `trustedWorkingDirectory`；闸门在启动 inner 前用**原声明路径重新解析**并比对登记物理目标
（junction/alias 改指即 `deny-input-target-moved`，登记期穿越别名的声明路径保守拒绝），再真实复核当前 input
文件字节/边界/SHA，登记后文件被改写仍被拦；审批只约束 `toolName === 'Bash'`，其它工具携带同一登记命令也
规则拒绝；拒绝用官方有效错误形状（不谎报 denied/spawn），spawn_error 不计入已执行、attempted/started 单列，
`command_started` 在 started 审计写失败时仍保留；`claimResponse` 至多一次、自动与人工分支输掉即 race-lost 绝不放行；
REPAIR3：官方 Bash handler 硬编码注入 embedded-search `bashPrelude`（无既有会话级开关），旧闸门把它与 `env`/`stdin` 同列
一律 `deny-extra-exec-channel` 会让合法登记命令也无法执行（LIVE-POSITIVE 即 broker 判 `approved-registered` 但
attempted/started/receipt 全 0——不是 GLM/登录故障、不是已执行）；现由 runner 用同树公开解析器
`resolveDefaultEmbeddedSearchBackend` 冻结宿主可信 backend、回灌 `runtimeConfig.embeddedSearchBackend`+
`nativeSearchEnhancementsEnabled:false`，把期望 prelude 规范 SHA 绑进闸门，仅**逐字节匹配冻结 prelude 且真实 Bash trace
（sessionId/turnId/toolCallId 非空、toolName==='Bash'）**才放行，否则 `deny-bash-prelude-mismatch`/`deny-bash-trace-unverified`；
`env`/`stdin`/无绑定 prelude 仍拒、inner=0，`captureCwdAfterSuccess` 内部字段不据此拒、审计只记存在性布尔与结构 hash），但目前仅离线（真实 runner +
bootstrap 双身、真实官方 request 形状含 prelude+captureCwd）验证，**未取得隔离真实现场回执**，
不把离线桩通过写成现场能力，也不宣称执行 Agent 已运行。其“代码/测试范围”靠声明式 input+SHA 证明，
**不是操作系统文件沙箱**，文档不宣称文件级隔离。当 Bash 现场能力尚未独立验收放行、或审批/闸门未就绪
（`ready=true` 只表示组件就绪、不等于现场已验证）时，计划阶段显式把该任务限定为 ZCode 只做读写
（Edit/Write 是写入不是「只读」）、再退回既有 Qoder 串行跑登记测试并保留结果：保留 ZCode 改动、
不反复重试、不靠换入口/模型绕过真实授权拒绝、不自动替换成 GPT/Luna（GPT/Luna 工程实施须用户事先
明确同意）；`fine_grained=false` 限制派发计划无法表达 `grants.edits` 与 `grants.bash` 两种细粒度规则
（非只逐文件），`command_contract` 是另一套本地逐字命令闸门、不授文件沙箱。
Start Plan / Trust Build 免费额度未证实。最终汇报须展示来回总数及每轮任务、返回结果和验收结论。

分层能力记录、现场放行最小条件、独立失败分型与可信 deny 派生的验收口径见
[references/zcode-capability-acceptance.md](references/zcode-capability-acceptance.md)（随技能原样安装）；
本文只做简短入口说明，运行时闸门/执行证据操作细节见 [ZCode 直连参考](references/zcode-direct.md)。

- 阶段按“可独立验收的成果”划分，不按阅读、改文件、跑测试等动作拆分：常规为“范围内完整执行与交付准备 → 主脑验收及必要补证 → 已授权发布与回读”，数量随风险和依赖增减；不预设后续阶段一定成功。
- 同一锁定目标、授权与隔离环境内，执行 Agent 自行闭环调查 → 复现 → 最小修复 → 相关回归 → 交付准备，允许修正自发现的测试/实现错误并重跑、留证；重复失败、需扩大范围或外部状态改变时停止受影响部分，交主脑决策。
- 整体目标验收完成时，主脑明确告知客户业务结果；随后回读确认项目笔记和对应 GitHub 仓库已更新，未完成的同步事项单独说明。
- 外部 Agent 通过完整的阶段提示词接收任务：`human-relay` 下提示词由用户转发，`api-direct` 下由主脑经用户配置的受控工具真实下发。两种方式都不得假称未实际发生的调用，也不把“任务已下发/已接单”当作完成；Luna 原生执行是独立可选档案，须按当前授权与审批使用，不能冒充外部 CLI。
- 发出提示词、工具接单或进度消息都不等于完成。必须等待实际汇报，再由主脑验收。
- 用户在等待期间补充信息属于常态，不会自动触发下一段提示词；只有用户明确要求立即转告当前 Agent，或明确说明 Agent 异常时，才处理对应交接或止损。
- 苦力 Agent 可以在阶段之间更换。新 Agent 的提示词必须包含交接基线、已验证证据、未验证主张、未完成项和需要重新核对的内容。
- 代码任务的汇报必须给出文件范围、原样测试命令、退出码、未完成验证和实际副作用。
- 苦力 Agent 将固定模板保存为 UTF-8 `.txt` 或 `.md` 原始文件，保存后核对 `WORKER_REPORT_START` 独占首行、`WORKER_REPORT_END` 独占末行，给出绝对路径并通知保存完成；主脑可直接读取同机路径的原文件（无法访问、缺失或跨机时上传），不以聊天粘贴正文作为唯一交付。主脑验收前不得生成下一阶段提示词。
- Skill 按“桌面应用 + 已确认模型”共用能力台账（默认本地 JSONL，开箱即用；飞书后端可选）：每次主脑验收后追加带时间的评分事件、更新共享分数和步长。不同对话在生成下一阶段提示词前重新读取最新状态；步长是审查参考（控制复核强度、检查点密度和允许的副作用范围），不限制同阶段子步骤、文件或测试数量，也不改变高风险动作的独立验收要求。

## 默认安全边界

默认不部署、不操作生产、不发送通知、不提交或推送 Git。上传、迁移、删除、发布、外部写入和凭据处理都要单独分阶段并设置验收点。Cookie、密钥、令牌、账务数据、运行状态和未提交改动受到保护。

mock、启动成功、点击成功或局部测试通过不能被表述为真实业务完成。报告必须区分事实、推断、隔离验证、运行环境加载和真实业务回读；读失败或证据不足时保持未知并停止受影响的动作。

## 当前仓库结构

```text
brain-worker/
├── SKILL.md                      # 主脑 + 执行 Agent 分阶段协作规则
├── config.yaml.example           # 用户配置示例（复制为 config.yaml，gitignore）
├── agents/openai.yaml            # Codex 界面显示信息和默认入口提示
└── scripts/
    ├── ledger_core.py            # 台账共享核心（评分/等级/归一/幂等指纹，纯逻辑）
    ├── local_ledger.py           # 本地能力台账（默认后端，纯标准库）
    ├── capability_ledger.py      # 飞书能力台账（可选后端）
    ├── test_ledger_core.py       # 共享核心与双后端一致性离线测试
    ├── test_local_ledger.py      # 本地台账离线测试（真实子进程与文件锁）
    ├── test_capability_config.py # 飞书配置解析离线测试（虚构标识与网络桩）
    └── test_capability_ledger.py # 飞书评分逻辑离线测试
```

Skill 的阶段协作规则可用于不同项目；本地台账脚本纯标准库、零配置，飞书后端需要自建应用与表格权限（见 SKILL.md“飞书后端接入（可选）”）。

## 验证

三层验证，性质不同，不能互相替代：

1. **结构检查**（Skill 结构/frontmatter）：使用 Codex 内置的 skill-creator 验证器：

   ```text
   python <skill-creator>/scripts/quick_validate.py brain-worker
   ```

2. **脚本离线测试**（从仓库根以 `python -m` 运行；全部离线，不请求真实飞书、不写真实台账）：

   ```text
   python -m scripts.test_ledger_core         # 共享核心与双后端一致性（评分/归一/幂等指纹）
   python -m scripts.test_local_ledger        # 本地台账：真实子进程与文件锁（Windows/Linux）
   python -m scripts.test_capability_config   # 飞书配置解析：入口/优先级/缺配置零网络（虚构标识与网络桩）
   python -m scripts.test_capability_ledger   # 飞书评分逻辑：pending/resolve/幂等
   ```

3. **真实调用验证**（需用户自行配置资源后进行，本仓库不包含也不承诺自动化端到端验证）：飞书后端联调（自建应用与表格）、api-direct 受控工具端到端等，均作为独立验收点。

结构检查通过只说明 Skill 结构有效；离线测试通过不等于真实调用成功（配置测试用虚构标识与网络桩，仅证明请求目标构造与失败路径正确）；三者都不代替具体业务项目的代码测试、部署回读或真实业务验收。

阶段提示词采用可整块复制的文本模板，Agent 的报告保存在原始文件中。汇报必须把事实、推断、测试退出码、未完成项、越界检查和阶段状态分开填写；没有内容写“无”或“未运行（原因）”，不能用自由叙述代替关键证据。若粘贴工具压平或截断报告，主脑以原始报告文件为准（同机直接读取或用户上传），不要求 Agent 为格式传递问题重跑业务测试。

能力评估会记录桌面应用、明确模型名称/版本、任务类型、评估与记录时间、样本数、当前评分、下一阶段步长、证据位置和已知限制。只有主脑验收后的报告才能改变评分；运行时模型身份未确认时不计分。跨对话以所选后端的评分事件链为准；若台账不可用，暂停依赖新分数的提示词并如实报告。

阶段被明确中断且已有足够失败证据时也要评价；实际模型尚未确认时先记录待归属事件，用户补充身份后由脚本按原阶段幂等补评。项目目标通过后还要主动检查适用的项目笔记和 GitHub 交付，并分别回读确认。

## 使用范围

适合大量阅读、批量变更、长时间测试、迁移、审计，或用户明确要求主脑与苦力 Agent 分工的任务。简单单步任务不需要启用本 Skill。Skill 本身不安装、不连接业务项目，也不授予生产或外部写入权限。GPT 桌面场景零配置直接用；Linux 场景主脑运行环境 2 选 1（muse / codex-cli）、执行方式 2 选 1（human-relay / api-direct），在 config.yaml 中自行配置，示例见 config.yaml.example。

## Qoder 本机直连（免复制粘贴）

GPT 桌面可直接担任主脑，调用已有官方 Qoder CLI 派工，回读原始报告并验收；不需要额外 Codex CLI 主脑。默认请求 Qwen3.8-Flash。

1. 将 `scripts/local-entry.json.example` 复制为 `scripts/local-entry.json`，填写已有官方 node 和 Qoder CLI 文件的绝对路径，登录由官方程序完成。
2. 主脑按 [Qoder 操作参考](references/qoder-direct.md) 嵌入完整九节兼容模板，并调用 `scripts/qoder_direct.py`。
3. 回读协议结果、实际会话、哈希与最终绑定，再核对真实业务。每轮验收后才安排补修或依赖任务。

真实配置、登录态、运行时依赖和业务报告不上传。此入口没有通用取消 API 或后台调度；ZCode 免费额度与接入实验不属于本次交付。新版通用四节模板继续保留，Qoder 驱动按上述九节兼容合同执行。

本地离线回归（无登录、无网络）：

```text
python tests/test_qoder_direct_offline.py
python tests/test_qoder_direct_binding_offline.py
```


## 执行器路由与默认开发者（2026-10-08 最新决定）

**当前直连派发只保留 Qoder 与 ZCode 两个入口。** 按用户最新明确决定，WorkBuddy 与
CodeBuddy 一律改为 human-relay only：选择它们执行时**只**输出完整可复制提示词，由客户
人工交给外部 Agent 处理；Skill/CLI/控制面不再提交任何新的 CodeBuddy 直接调用。生产入口
`scripts/codebuddy_direct.py` 的 `main()` 在读配置/提示词、建输出、额度门禁或 `Popen` 之前
固定拒绝（`sent=false`、`manual_relay_only`、非成功退出码），`execution_control.preflight`
对 CodeBuddy/WorkBuddy 计划同样拒绝；无 plan、带 plan、resume、probe、配置存在/缺失都不能
绕过，也不提供重新启用参数或隐藏入口。生成提示词不记成已派发/已验收。

本机开发任务未指定执行者时 ZCode 优先（用户付费套餐，未独立核实账单/额度）；Qoder 沿用现有
唯一模型及入口配置兜底，不覆盖配置、不新增或替换模型。CodeBuddy 支持个人国内站登录、
`glm-5.3-flash` 显式传模型 ID、原事件流与九节报告验收等**历史能力与解析口径**保留在
[CodeBuddy 历史直连档案](references/codebuddy-direct.md)，仅供离线证据回放，不再是当前可直接
执行的派发指引。机器配置、登录数据及调用日志不入库，cost=0 不作为免费证明。

历史顺序（2026-10-07 及之前，仅作背景，已被上面决定取代）：曾由独立 CodeBuddy CLI 入口先用
GLM 5.3 Flash 做小范围只读定位、故障后 ZCode 接续、Qoder 兜底。现该 CodeBuddy 直连步骤改为
人工转交提示词；ZCode/Qoder 的直接派发不受影响。
