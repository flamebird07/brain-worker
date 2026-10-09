---
name: brain-worker
description: 按主脑与执行 Agent 分工完成大量阅读、开发、批量修改、测试、迁移或审计；支持已就绪且已授权的 Qoder、ZCode 直连及可选 GPT-6 Luna 云端原生子 Agent，Luna 遵循当前调用审批；CodeBuddy/WorkBuddy 改为 human-relay only（只生成可复制提示词、人工交外部 Agent，不再直接派发）；本机未另行指定时按可用主力 `zcode:GLM-5.3`/`qoder:Qwen3.8-Max` 各 2 的 best-effort 1:1 轮换、明确 executor+model 授权组合优先，主力不可用时改用溢出 Flash 或已授权接续，各入口回读原始报告并独立验收；也支持 human-relay。简单规划、推理与独立验收可由 GPT 主脑直接处理；工程实施由用户事先明确同意的执行器承担。
---

# 主脑与苦力 Agent

执行器范围（2026-10-08 用户决定，同日最新追加）：WorkBuddy 与独立 CodeBuddy CLI 均改为 human-relay only，不再从当前 Skill/CLI/控制面提交新的直接派发调用；选择它们时只生成完整可复制提示词，由客户人工交给外部 Agent，生成提示词不记成已派发。当前直连派发只保留 Qoder 与 ZCode，默认按可用主力 `zcode:GLM-5.3`/`qoder:Qwen3.8-Max` 的 1:1 轮换、明确 executor+model 组合授权优先，主力不可用时改用溢出 Flash 或已授权接续（不再表述为“ZCode 默认优先、Qoder 唯一模型”）。不再启动 WorkBuddy 的认证或测试。历史能力、原错误与模型信息与测试证据保留，仅作离线证据回放/档案，不作为重新启用授权。

## GPT-6 Luna 原生子 Agent（可选）

GPT-6 Luna（模型标识 `gpt-6-luna`）是可选执行端，通过所在宿主实际提供的原生子 Agent 工具调用。本机未另行指定时按可用主力 `zcode:GLM-5.3`/`qoder:Qwen3.8-Max` 各 2 的 1:1 轮换、明确 executor+model 授权组合优先，主力不可用时改用溢出 Flash 或已授权接续；Qoder 沿用其既有入口配置（不再表述为“ZCode 默认优先、Qoder 唯一模型”）。Luna 不自动接替任何在途任务，也不改变适用任务/在途任务已有的执行选择；非容量类 Luna 需明确授权、绝不凭超时自动接替；GPT/Luna 的工程实施须用户事先明确同意，不自动代做。

每次考虑调用 Luna 时，检查当前环境工具是否提供该模型、当前用户授权及审批要求，并遵循当次生效的 custom rule 与平台规则；需要审批时取得对应批准后才启动。能力登记、历史启动成功或工具存在都不是后续调用授权；不把动态规则、过去批准或某次测试参数固化为永久授权或默认参数。

原生子 Agent 只在所属云环境中执行，不能直接操作用户电脑。本云端主聊涉及用户电脑的本地工作仍通过已授权的 `cloud_threads` 进入对应本机环境，并遵循该环境的工具与权限边界；不得把中转能力归给 Luna。此项不提供统一 CLI，不把 `gpt-6-luna` 填进 Qoder、ZCode、CodeBuddy 的模型配置，也不宣称通用 CLI/四接口协议已接通。

已有启动证据：根线程报告于 2026-10-07 使用 `collaboration.spawn_agent(model="gpt-6-luna", reasoning_effort="xhigh", fork_turns="none")` 完成一次合成排序验证，子 Agent 正常返回 `[2,5,7]`。这只支持该次原生启动与小任务返回；本机仅按根线程提供的结果登记，未独立回读原始云端调用日志，未重新调用。不证明本地桌面能力、完整业务闭环或套餐/额度节省。后续任务按实际任务标识保留原始交付、核对环境和副作用并由主脑独立验收；详细边界见 [Luna 原生执行端参考](references/luna-native.md)。

Skill 使用与执行派发分别记录：`brain_worker=used/not-used`；外部执行仍记 `external_dispatch=dispatched/not-dispatched`，原生调用另记 `native_dispatch=dispatched/not-dispatched`、实际模型、任务标识与次数。仅更新指导或复用历史证据时，两种派发都记未派发，不计作新的执行轮次。

## 本机 DOTS 默认入口与使用记录（用户已明确选择时）

用户通过本机 DOTS 安排任务时，默认先实际读取并应用 brain-worker Skill，完成适用性判断、规划与分工；已授权且具备所需能力的具体执行优先交给已就绪的国内桌面 agent（CLI 或 API），GPT 保留必要的逻辑推导、任务分解和独立验收。简单低成本任务仍先做判断；不适合委派、工具不可用或权限受限时，明确说明原因和实际处理方式。

每次执行前及最终汇报明确记录：brain_worker=used/not-used、实际 SKILL.md 路径、已应用的步骤与证据；另记 external_dispatch=dispatched/not-dispatched 及原因。used 仅用于已实际读取并按 Skill 执行本次适用步骤；只写说明、理解理念或准备提示词不能冒称已使用或已派工。未读取或未应用时记 not-used，并主动提醒用户原因；已使用 Skill 做判断但未派工时，分开说明，不能掩盖执行状态。

此偏好不扩大用户授权、不覆盖安全要求；实际派工仍须确认运行时、模型、工具授权、隔离工作区和报告回读条件。CodeBuddy/WorkBuddy 相关只生成可复制提示词人工转交、不再直接派发；历史 Flash-only 模型口径（GLM 5.3 Flash，调用前核实实际可用模型 ID，不可用则报告障碍、不静默回退 GLM 5.3）仅适用于旧 CodeBuddy 直连档案，不作当前派发依据。

已落实范围是本项目说明、对应 Obsidian 笔记、本机 brain-worker 安装版与仓库版的指导及 UI 默认提示。现有对话若缓存旧 Skill，需重新读取更新后的安装版；本规则不是已接通的 DOTS 全局自动路由，也不能保证未加载该 Skill 的其他入口自动执行。

## 本机显式 local 台账模式（适用任务，优先于旧飞书专属要求）

适用任务由用户在本机当次会话明确选择 local 台账时，不调用 Hermes 配置或飞书认证/读写。仓库版与安装版 scripts/local_ledger.py、scripts/ledger_core.py 采用同一实现；调用前须由用户显式设置 `BRAIN_WORKER_LEDGER=<本机隔离台账绝对路径>`，禁止落到默认用户目录，也不把这一路径当通用默认值固化给其他人。

该次任务及其验收后的接续阶段，台账门禁使用该本地入口的 status/record/resolve，取代旧飞书专属命令、模板和共享来源要求；后文飞书指令仅用于另外明确选择并授权 feishu 的任务。local 就绪和本地回读足以满足该任务的台账门禁，不要求为此读取或写入飞书，也不把远程台账未同步当成本地记录失败。

这是独立的新本地事件链，未迁移、导入、重算或覆盖历史远程评分；初始50分/步长1/样本0仅是本地初值。mode.json 明确 remote_score_synced=false、remote_write_performed=false、remote_history_imported=false。没有真实执行报告时只做status，不提前评价或计分；验收后以稳定event-id本地record，身份未证实不传--identity-confirmed，补证后本地resolve，写后回读。同阶段幂等，不因补修或重试新建加分样本。

后续明确授权远程同步前，不运行 capability_ledger.py，不配置认证，不外发评分、报告或消息。派工与台账分开验证：台账就绪不是模型已调用或业务完成；执行端仍按 Qoder/ZCode 直连兜底，CodeBuddy 相关测试改为生成提示词人工转交（不再直接派发）。

## 当前本机跨项目调度约定（仅适用于用户当次指定的并行链路）

目标仍是 GPT 主脑负责必要推导、规划与验收，国内执行器承担具体执行，以节省 GPT 额度。用户在本机并行推进的多条链路（例如某业务任务与 brain-worker/CodeBuddy 修复链路）经父线程互传已验证结果，业务不等待适配器修复；具体的任务标识与台账路径由用户当次指定，本仓库不固化任何单一任务、目录或授权。

用户指定的业务任务顺序（2026-10-08 最新决定）：小范围只读定位一类任务若选 CodeBuddy，则改为生成完整可复制提示词由客户人工交外部 Agent，不再从入口直接派发；按可用主力 `zcode:GLM-5.3`/`qoder:Qwen3.8-Max` 的 1:1 轮换、明确 executor+model 授权组合优先作主路径，主力不可用时改用溢出 Flash 或已授权接续。ZCode/Qoder 的模型按各自档案与授权核对。切换先确认原调用终态，或明确停止并核对副作用；未知状态先查，不把“立即接续”变成同一目标的重复在途派发。成功与失败的原件保留。历史曾由独立 CodeBuddy CLI 用 GLM 5.3 Flash 先做定位、故障后 ZCode 接续，该直连步骤已停用、仅存档。

执行端角色（2026-10-07 用户补充，2026-10-08 更新）：本机未另行指定执行者时按可用主力 1:1 轮换（用户付费套餐，用户反馈有额度且速度快，未独立核实账单或剩余额度）、明确 executor+model 授权组合优先，主力不可用时改用溢出 Flash 或已授权接续；Qoder 沿用其既有入口配置，不覆盖配置、不新增或替换模型，也不重复询问模型名称；用户反馈当前活动无消耗，尚未独立核实，不当作免费事实。明确用户选择与在途任务不替换：选择变更只应用于验收后的下一轮。GPT 主脑可做规划、推理、确定性输入准备和独立验收；GPT/Luna 工程实施须用户事先明确同意，不自动代做。外派被授权校验、自动审批或环境限制拦住时报告原拒绝，不改成 GPT 代做，也不换执行器或其他入口绕过；普通已终态执行失败是否兜底，按已授权顺序决定。WorkBuddy/CodeBuddy 不再直接派发，只做提示词人工转交；历史 Flash-only 模型口径仅适用于旧 CodeBuddy 直连档案，不强制其他执行端切换 Flash。

牛马修复链路按错误证据是否到达分级处置，不预设“当前没有新报告”这一状态：若本阶段确有新的真实错误证据，据其定位适配器/Skill 缺口，在独立范围内准备、验收后经父线程反馈；若此刻尚无新的错误报告，则等待父线程转交，不凭历史 429 或猜测重跑测试，不重复调用适用任务，也不修改适用任务占用的文件、工作区或运行对象。修复仅针对报告所证实的适配器/Skill 缺口；双方成果互推，但不把一方未验收状态当作另一方成功基线。

交接按同一 task/stage、request/prompt哈希和session/turn标识串联 request、process退出码、原始stdout/stderr、summary、report-state、已有response/产物及其哈希；保留错误码、权限拒绝、用量和缓存口径、实际副作用。先查可恢复结果，再决定是否需要新调用；消耗、CLI退出0或运行结束均不等于完成。不传凭据，也不凭空补写worker报告。

主脑负责理解需求、确认边界、以可独立验收的成果划分阶段、审查风险、形成明确指令、验收和决定下一步。执行 Agent 负责阶段内调查、修改、测试、迁移、审计及其他实质执行。主脑驱动不具备直接执行能力时，由传输层机械执行主脑明确给出的控制面指令；传输层不参与任务判断，也不是执行 Agent。委派和传输均不转移主脑的验收责任，也不扩大用户授权。

本 Skill 的角色以实际职责而不是进程、产品名称或界面名称判定。同一宿主可以承载主脑驱动和传输层，但承载关系不允许角色边界合并：宿主若代替执行 Agent 阅读、修改、测试、迁移、审计或实施主脑给出的修改方案，就是承担了阶段实质工作，不能称为“传输”。

主脑须锁定客户当前要求完成的具体目标，以客户的最新明确指令为准。执行 Agent 发现相邻问题时可以报告证据；除非该问题直接阻断目标或用户已授权，不得自行把它变成修改、测试或交付范围。

任务下发后，只锁定该任务的追加、替换和返工提示词，不锁定其他无依赖、无写入冲突的任务。这条门禁同样适用于用户中途更换 Agent、模型、步长、范围或提出新需求；先用普通回复确认并记录变更，留待验收后的下一任务使用。“刚刚那步也换成某模型”等追溯表述本身不构成撤回已发提示词。即使用户要求立即调整执行说明，也不得向执行中的任务追加提示词；必须先由用户明确停止当前任务，核对执行状态与已产生的副作用，再重新界定任务。仅要求“修改说明/记录偏好”时，只修改说明或记录，不生成提示词。收到交付并完成验收，或客户明确取消后，方可解除该任务门禁。

## 计划复杂度复核

主脑输出执行计划后、注册或派发任何执行任务前，必须复核并反问：

1. 这个计划是否有必要这么重？
2. 最小可用版本是什么？
3. 哪些部分可以砍掉？
4. 保留部分为什么必须保留？

主脑必须向客户简述复核结论，不得只在内部完成复核后直接派发。

主脑优先选择能交付目标的最小方案，删去无必要的阶段、模板和状态机制。沿用已有授权，独立成果并行本身不触发再次确认。只有缺少影响执行的必要信息、新增授权或不可逆外部动作需要客户决定时，先准备可审核结果，再就该具体缺项询问；其余已授权工作继续。

轻量单任务也必须完成复杂度复核，但允许用一句话说明计划已是最小可用版本及其理由。

## 任务注册与并行看板

每个独立执行和验收的成果都使用稳定且不复用的任务 ID，例如 `T01`、`T02`。任务 ID 必须出现在提示词、状态消息、交付报告和验收结论中。

任务只使用五种状态：

- `待派发`：任务已登记，尚未确认下发。
- `执行中`：已经接单，正在执行或等待约定检查点。
- `卡住`：因阻碍、失败、状态未知或需要决策，无法按原路径继续。
- `待验收`：已收到足以验收的交付或失败证据。
- `完成`：生命周期已关闭；验收结果另记为通过、不通过后终止或客户取消。

主要转换为：

`待派发 → 执行中 → 待验收 → 完成`

`执行中 → 卡住 → 执行中`

`执行中 → 卡住 → 待验收`

`待验收 → 执行中` 仅用于验收后明确要求的补证或补修。

任务下发后，门禁只锁定该任务。其他任务在依赖满足、范围不冲突且并发能力允许时可以继续下发。

并行前检查：

1. 是否修改相同文件、配置、数据或外部对象。
2. 是否操作同一工作区、分支、服务、进程、部署目标或测试环境。
3. 是否依赖其他任务尚未验收的结果。
4. 是否超过已确认的并发能力。

存在共享写入、环境冲突或未满足依赖时，应串行执行。未确认并发能力时，并发上限为 1。

主脑维护唯一任务看板；能力评分台账不承担此职责。具备控制面归档能力时，可把看板保存在业务仓库之外；否则以主聊中最近一次完整看板为准。

TASK_BOARD_START
项目标识：
更新时间：
并发约定：

| 任务ID | 目标 | 状态 | 验收结果 | 最后进展（含证据/备注） | 阻碍 |
| --- | --- | --- | --- | --- | --- |

TASK_BOARD_END

看板在任务注册、下发、重要里程碑、卡住、恢复、收到交付和完成验收时更新。完成任务可以折叠，但保留任务 ID、验收结果和证据位置。

## 执行方式

本 Skill 的外部执行支持下列两种方式；另有上方 Luna 云端原生可选档案，不套用外部 CLI/通用四接口协议。外部执行的**核心规则、阶段门禁、WORKER_REPORT 模板、安全边界、验收标准与范围反思完全同一套**，差别只在”提示词怎么送达、报告怎么回来”：

- `human-relay`（人工转发，默认，零 API 配置；GPT 桌面原有用法即此方式）：主脑生成完整、可复制的阶段提示词，由用户转发给外部桌面 Agent（例如 ZCode、Xiaomi Mimo）；执行 Agent 把 WORKER_REPORT 保存为 UTF-8 原始文件，放到会话中约定、主脑可访问的路径，用户通知保存完成，主脑直接读取原文件验收；主脑无法访问该路径或文件缺失时才由用户上传原文件。

执行 Agent 使用统一的下发确认、状态汇报和交付模板。若宿主不能后台观察或跨回合主动通知，须在下发确认中说明可观察方式和限制。
- `api-direct`（受控工具直调）：主脑直接操作用户提供的受控工具/接口，或在主脑驱动不具备执行能力时经传输层机械调用该工具/接口，下发阶段任务；按任务 ID 持续观察状态；非终态结果用于更新看板和触发状态汇报，终态结果进入交付提取与验收（对应“任务级等待”），从对应任务的事件中提取 WORKER_REPORT 块，保存为“API 提取报告存档”后再验收。该存档不是 API 返回事件的原始载荷；省掉人工转发环节，但**不省掉任何验收**。

已配置的 ZCode 官方运行时按下方 ZCode 专项档案直接派工；桌面人工转发仅在用户选择 human-relay 时使用。

### 持久额度冷却与路由门禁（2026-10-08，BW-QUOTA-20261008-S1）

ZCode 真实派发入口（以及已退休为人工转交、现仅由 `tests/offline_codebuddy_harness.py` 在合成 stub 下离线回放的 CodeBuddy 直连流水线；生产 CodeBuddy 入口对任何新直连在读配置/建输出/Popen 前固定拒绝，不再进入实际派发）在所有 Popen 前经过 `scripts/quota_control.py` 的 sqlite 持久门禁（含不传 dispatch-plan 的兼容路径）：明确 429 按带时区 reset/结构化 Retry-After/无窗口 quota/临时退避分级冷却，跨进程、跨派工、跨工作区生效，冷却截止前不得跨任务重派该通道；同一真实工作区保持单一写入执行器；冷却到期仅 recovery_unverified，需单次有界核验通过才清，不把客户端切换当额度恢复。额度分组只来自受信任本机 quota-routes 配置（CodeBuddy 与 ZCode 额度独立为本机 user_confirmed，非服务商/账单核实；通用配置仍支持共享组；未知关系不默认独立）。**额度冷却政策例外（2026-10-08 用户决定，BW-ZCODE-MANUAL-QUOTA-20261008-S1）：ZCode 额度改由用户手动管理/重置，取消自动额度冷却——`gate_dispatch(runtime='zcode')` 与 ZCode 的 `settle_attempt`/`import_terminal` 跳过全部额度冷却/到期/自动恢复判定（429 分类仍如实提取，但不写 cooldown），缺 routes、落 unknown-shared 或共享组历史冷却都不再挡 ZCode；这是按真实 runtime 的固定策略、不可由任务书关闭，也不提供关闭 CodeBuddy 冷却的开关。CodeBuddy 及其它 runtime 的额度冷却规则完全不变，ZCode 也不得清除/覆盖同组里 CodeBuddy 的冷却。此处取消的只是额度冷却门禁，ZCode 的工作区单一写入与通道单在途并发占位仍保留（跨 CB/Z 同一真实工作区仍串行、同组跨工作区仍单在途、活任务不被抢占），普通 ZCode 派工不要求 recovery probe。** 中断接续用 `scripts/continuation_contract.py` 冻结受控文件清单与最后测试证据，下一执行器从既有产出继续，不回滚整批、不补写缺失报告、不用旧绿测证明改后通过。详见 [references/quota-routing.md](references/quota-routing.md)。

### 全局并发容量池与 1:1 路由（2026-10-08，BW-GITHUB-CLOSEOUT-S4）

所有会话共享同一持久**并发容量池** `scripts/dispatch_pool.py`（纯标准库 sqlite3、`BEGIN IMMEDIATE` 原子事务，默认 `~/.brain-worker/dispatch-pool.sqlite3`，`BRAIN_WORKER_DISPATCH_STORE` 覆盖；测试必须传显式临时 store）。分配口径：主力 `zcode:GLM-5.3` 与 `qoder:Qwen3.8-Max` 各最多 2 个真实在途执行器、按 committed 计数做 **best-effort 1:1** 轮换（非严格均衡，文档不宣称严格相等）；溢出 `qoder:Qwen3.8-Flash` 最多 2 个，**只有两主力池都满**才允许；**国内合计 6**；未授权组合容量为 0 一律拒。Luna（`luna:native`）**只做救援、无数量上限**：只有六名额全满、宿主**真的问过用户**（外部 agent 还是 Luna）、且 **300 秒无回复**后，经 `claim-due` 在同一事务内原子竞争裁决才可由**宿主原生调用**——Python 只落库票据、绝不谎称已问或已派生 Luna；一旦用户回复即不再自动裁决（等待超时≠默认无限授权）；等待期间任何国内名额释放**优先国内并原子取消同 task 的 pending 票据**（不双派国内+Luna）；原生工具已启动但在写出 agentID 前崩溃 → `launch_unknown` 待人工核验、**绝不自动重复启动**；Luna 沿用原任务文件/命令/副作用范围，不借救援绕过权限拒绝、额度错误或部署审批。

Qoder/ZCode 入口在**建输出目录、Popen 之前**必须消费/校验一个容量 claim（`consume_for_entry`），**不能靠提示词或一个传入布尔跳过**：给了 `--dispatch-claim` 就精确校验（task/runtime/model/workspace/prompt_sha256 任一漂移即拒），没给就走 `select_and_claim` 原子路由，非被选中组合 → `routing_required`/`sent=false`/退出 2、**零证据目录、零 Popen、不计轮次**；路由选择绝不把权限拒绝/登录缺失/额度限流伪装成容量溢出。**子进程真实结束立即释放名额**（不等报告绑定/业务验收），启动失败/取消/异常结束释放本 attempt，wrapper 死但子进程仍活（或存活未知）不释放、不被抢占，PID 复用绑定创建时刻（Windows 只读 `GetProcessTimes`），Qoder 派生的工具子进程不算另一个 worker。**容量并发 ≠ 额度冷却**：额度门禁仍在 `quota_control`；两个 ZCode workspace 走同一 provider 时经**已校验的容量 claim**放行，同 workspace 单写入与 unknown/在途保护不变，不删通道行、不禁用门禁、不伪造 claim；CodeBuddy 旧限流与旧占位完全不变、不进池。详见 [references/global-dispatch.md](references/global-dispatch.md)。

### ZCode 独立 availability 与执行器资格路由（2026-10-09，BW-AVAILABILITY-20261009-B2）

现场事故（真实 SDK HTTP 429、BigModel Coding Plan、`provider_code=1310`、wrapper 派生 `category=rate_limit`，但服务器真实文本是“已达到每周/每月使用上限”且 reset **无时区**）暴露：泛化的 `rate_limit` 标签不得覆盖真实硬上限语义、无时区不得猜 UTC+8、不得复用旧 24h 冷却当恢复窗口。为此新增与旧 `cooldowns` **完全独立**的持久表 `zcode_availability`（同库不同表，`record_zcode_unavailability` 绝不触碰 `cooldowns`，CodeBuddy 冷却与 ZCode availability 互不污染）：稳定通道键 `{provider}|{quota_group}`（配置别名不能拆分、routes 缺失/更名落 `unknown-shared` 保守连带阻断）；硬额度只认**同一条真实错误条目内绑定**的 `(受信任 provider bigmodel|zhipu 且 code∈{1308,1310})` 或窄硬上限消息语义（优先于 `rate_limit` 标签），同一数字来自非受信任渠道不泛化，报告正文 429、另一条非 429 quota 条目、取消退出码 `4294967295`/`-1` 都不触发。kind 分 `hard_hold`（硬额度无可信窗口 → `blocked_until_utc=None` 无限期，等显式用户重置/新恢复证据，绝不猜 24h）、`hard_reset_window`、`backoff_until_window`、`temporary_backoff`；reset/Retry-After **取最晚合法下限、跳过非法/无时区项**。窄幂等手动重置入口 `zcode-reset`（必须 `--provider`+`--evidence-ref`）只置 `recovery_unverified`——**“可核验”≠healthy**；恢复点/新证据只**原子放行单次有界无副作用 probe**（CAS 单赢家），probe 失败/超时/取消设再探测退避、不能立即重探，成功须 `epoch`+`attempt` 双匹配才 `healthy`，**迟到成功绝不清除更新失败 epoch**；availability 记录**永不宣称真实额度/账务/免费**。ZCode 入口在**容量门之前**过 `zcode_availability_gate`（权威最后一道原子闸），被拒打印 `zcode_availability_rejected`/退出 2/零建目录；证据已确认但接续冻结失败/终态未知时**仍存不可用事实、不释放活体锁**（fail-closed）。`dispatch_pool` 新增 `executor`（auto|qoder|zcode）+ `quota_store`/`quota_routes` 透传：**先过滤合格候选再在可用主力间 1:1**，availability 过滤只在显式传 `quota_store` 时生效（读取异常 fail-closed 阻断），显式 Qoder 有空位直接 claim Max、**绝不因历史 committed 计数被改道到 ZCode**，auto 只在合格主力间轮换且不清库，消费 token 前**复检** ZCode availability 并释放本任务自己的 reserved 占位（不泄漏）。Luna 侧：回复 `domestic` 任何时刻都绝不启动 Luna（只等待/原子 reclaim 合格国内主力），重复 ask 绝不重置已 replied/cancelled/settled/claimed 的回复/scope/deadline，scope 损坏 fail-closed；国内未满 6 但 availability 不足时允许带独立客观理由（`quota`/`auth`/`capacity`）的降级票据（`quota` 需客观证据），**非 capacity 超时绝不可替代显式授权**，六名额全满 300 秒授权规则不变。离线回归见 `tests/test_availability_routing.py`（每用例临时 SQLite、合成 stub、`sent=false`/零模型调用），Windows/Linux CI 矩阵都跑。详见 [references/quota-routing.md](references/quota-routing.md) §3b、[references/global-dispatch.md](references/global-dispatch.md)、[references/luna-native.md](references/luna-native.md)。

### ZCode availability 闭环加固与 combo 硬约束（2026-10-09，BW-AVAILABILITY-20261009-B4）

B4 不改 B2 语义、不扩额度统计范围、不新增真实额度/账务/免费宣称，只把 B2 落地时 reproduced 的 7 个实现缺口收口，并加受信任主脑的明确 `executor+runtime+model` combo 硬约束（含本阶段真实执行的 `qoder:Qwen3.8-Flash`）。**代际/CAS 与真实行键**：`zcode_availability_gate` 无论命中现有行还是无行 stateless 默认，都回传被授予行的精确 `granted_channel_key`/`availability_epoch`（无行为代际 `0` 不再 `None`），`settle_zcode_attempt` 按该键**总是**比较代际（不等即拒）——A 落 429 推 epoch=1 后 B 的迟到成功（代际 0）绝不清成 healthy，同 provider 别名/改名映射同键不分裂，healthy 保代际单调防 ABA。**provider 与分类**：硬额度绑定**已核验请求 provider**，`_HARD_LIMIT_MESSAGE_RE` 收窄到周期/套餐真实耗尽语义，普通“Rate limit exceeded”“每分钟请求上限”→ `temporary_backoff`，1308/1310 仅在该真实渠道 + 同一 429 载体内生效，非受信任渠道/正文 429/独立非 429 条目/取消码不升格。**单次恢复资格与幂等重置**：新增持久列 `recovery_eligibility_consumed`，一个真实执行（`executed=True`）的失败/超时/取消 probe 消耗该代际**唯一一次**恢复资格，退避到期不再同 epoch 放行第二个定时 probe，须新证据；`zcode-reset` 绑定 `manual_reset_epoch`，同证据同 epoch 幂等、旧事件回放（`epoch>manual_reset_epoch`）被拒、省略 epoch 不能重置更新失败，重置只到 `recovery_unverified`（可核验）绝不直接 healthy；未启动即被拒/异常（`executed=False`）只清自己 `probe_active` 不消耗资格。**正式 probe 与容量闸**：容量门不接受自报 `probe=true`，`zcode_direct` 把 availability 授予的 `probe_ticket`（provider/channel_key/attempt_id/epoch）经 `_probe_allowed`→`zcode_probe_ticket_valid` 只读核验真实 CAS 授予行才放行唯一一次启动，读取异常 fail-closed，普通派工仍拒、边界不变。**真只读与统一默认**：`zcode_blocking_rows`/`zcode_availability_status` 改用 `_open_readonly`（`mode=ro`）——缺库/缺表返回空、绝不建表/迁移/WAL、其它读错抛出 fail-closed 绝不静默 healthy；pool `_zcode_availability_blocked` 与 CLI quota_store 统一 param-or-env（Q 传参或 env、Z 缺省 `default_store_path`）使 AUTO 无需额外参数即过滤不可用 ZCode；派工写事务内绝不写额度 helper，杜绝双库嵌套反向写锁。**终态发布与容量释放竞态**：`zcode_direct` 确认受信任 429 后**先 `record_zcode_unavailability`、后做容量释放**（接续冻结失败也保事实、state 已提交而容量释放失败绝不伪造 healthy）；`_preclaim_reconcile` 要求 child 死**且** wrapper 死**且**创建身份匹配（PID 复用即跳过），auto 回收只放执行容量**不清 availability**，原 owner 显式 finish 仍可释放，两库独立短事务。**combo/回收尊重原 scope**：明确组合贯穿 `reserve/select_and_claim/consume_for_entry/claim_due` 作候选硬边界——已授权组合不因别的候选有空位而被拒，默认 AUTO 主力优先/容量/1:1 与 Z2/Max2/Flash2 不变（并发 2/4 是 Skill 政策非已核实进程上限）；`claim_due` 用 `_scope_allowed_candidates` 依原 scope 收窄（绑 Qoder 只回收 QMax/Flash，绑 ZCode 且被阻断→保持 pending 绝不改派/降级 Luna）。**Luna/原始候选**：`reply` 保护 claimed/**settled**/cancelled/launch_unknown、只接受合法首次回复、重复确认幂等、settled 票据不被迟到/重复回复重开；`ask_record` capacity 降级客观核验 `_scope_allowed_candidates`（不再仅凭字符串+detail 判全不可用），**物理六满与合格候选耗尽分列**、误报不自动 Luna、非 capacity 仍需显式授权 + 六真满 300s、domestic 永不 Luna、external/cancel 不自动 Luna。负例先于修复固化（当前 B2 上失败），修复后全绿，离线回归仍在 `tests/test_availability_routing.py`（每用例临时 SQLite、合成 stub、`sent=false`/零模型调用），Windows/Linux CI 矩阵都跑。详见 [references/quota-routing.md](references/quota-routing.md) §3c、[references/global-dispatch.md](references/global-dispatch.md)、[references/zcode-direct.md](references/zcode-direct.md)、[references/qoder-direct.md](references/qoder-direct.md)、[references/luna-native.md](references/luna-native.md)。

### ZCode availability B5 收口（2026-10-09，BW-AVAILABILITY-20261009-B5）

B5 只把 B4 仍 reproduced 的 7 个必要缺口用集中回归固化后最小收口，不改 registry/可信 runner/台账、不新增真实额度/账务/免费宣称、不扩框架。**默认持久来源统一**：pool 的 availability 回核经 `_resolve_quota_store` 走“显式参数 → 环境变量 `BRAIN_WORKER_QUOTA_STORE` → `qc.default_store_path()`”，默认 AUTO 无需额外参数即读取共享 availability 过滤不可用 ZCode；只读回核绝不建库/建表/迁移、绝不在 pool 写事务里初始化额度库；缺库/缺表=无阻断（可用），其它读错 fail-closed。Q 正式入口按 env 或同一默认源读取（非仅测试参数）。**唯一恢复资格**：失败 probe 落受信任 429 时 `record_zcode_unavailability` 与 `settle_zcode_attempt` **原子** `consumed=1`（attempt 匹配在飞 probe 即结算并消费）；同一已过期可信 reset 的重放（`epoch>manual_reset_epoch`）被判 stale、绝不给新失败重造资格，只有新的显式 reset（推进 epoch、重置 consumed）才重新授一次 probe；固定短退避绝不反复试硬额度。普通 frequency-429 有界退避策略不变。**真实 probe 启动异常**：拿到合法资格后 Popen 抛错/child=None，必须对本次绑定的 row/epoch/attempt 以 `executed=False` 结算——归还 `probe_active`、不消耗资格、绝不留 active 或假称恢复；真实有界 probe 恰好一次启动，由完整 mock 入口证明（非仅直接调 settle）。**自动回收统一 wrapper 身份**：`reconcile` 与 `_preclaim_reconcile` 都要求原 `wrapper_pid`+`wrapper_created` 齐备、child 与 wrapper **双方确认死亡且创建身份匹配**才自动 `reconciled_exit`；缺身份、wrapper 仍活/未知、PID 复用一律保守保留，owner 显式 finish 仍可释放；每入口临时 SQLite 负例覆盖、释放空窗不放行同 workspace 新 writer。**终态冻结→释放顺序**：429 不可用事实先提交；容量名额释放推迟到接续冻结/原错误与输入 hash 保存**完成之后**，整个 `cc.evaluate/build_handoff` 期间持有同 workspace writer guard；冻结抛错/终态未知保留占位并真实标注 `placeholders_retained`，绝不 `capacity_released=true` 又 `placeholders_retained=true`；正常冻结完成/非 429 终态才释放。两库仍各自独立短事务。**combo 用 executor+model**：受信组合以 `executor`+`model` 表达，`runtime` 可接受但绝不再是必需的冗余字段——`scope={executor:qoder, model:Qwen3.8-Flash}`（无 runtime）在六满 + 释放一个 Max 后 reply domestic/`claim_due` **只回收 Flash**，绝不误选 Max；select/reserve/consume/ask/reclaim 校验同一 combo；executor/runtime 冲突或未知 model fail-closed（空候选、不猜池、绝不 Luna）。保留 QMax 有空位不被历史 1:1 阻断、AUTO 合格主力 1:1、Z2/Max2/Flash2 容量、六满 300s 规则。**说明**：本段的“无时区不猜 UTC+8、不复用旧 24h 当恢复窗口”仍指**旧 `cooldowns` 表**的历史冷却政策，绝不写成取消新真实失败反重放（`recovery_eligibility_consumed` 单次资格消耗）机制。非容量类 Luna 永远需显式授权、绝不凭超时自动接替；Qoder 并发 2/4 是本 Skill 政策、非官方已核实 CLI 上限。负例先固化、修复后全绿，离线回归仍在 `tests/test_availability_routing.py`，Windows/Linux CI 矩阵都跑。



### 派工范围、观测边界与 ZCode 错误终态归并（2026-10-08，BW-ZCODE-MANUAL-20261008-S2）

范围按独立可验收成果（状态机/副作用/必要接口/验证面/读取量）收敛，不靠“两文件/步长 1”机械阈值，耦合必要时保留完整闭环；提示词给精确函数与 harness、优先分段定位、避免重复整读，字节不等于 tokens。文字路径范围、工具可见性、实际授权分别记录——裸 Read 不是文件 sandbox，未经本机验证的单文件规则不得声称已生效，不静默扩权或读凭据。状态分型为已派发/真实Read/等待消息/真实写入/报告绑定/独立测试/业务回读；静默或心跳不等于死锁/429/工具执行，Windows 退出码 4294967295 不是 HTTP 429，`CODEBUDDY_MAX_RETRIES` 只是子进程有界重试、非总模型次数或全程时限；用户选择继续等待的在途任务不追溯套用新超时/换模型/重派，超时与增量流参数只作未来显式配置、须先授权且兼容验证后启用（本轮不实现）。能力分面（模型路由/工具可见/审批组件端口 ready/command_started/退出/输出回执）独立记录：当前 `command_contract` 路径已注入 permissionBroker，不声称 SDK 全无审批客户端，主动屏蔽 Bash 不算新失败，未登记命令被拒不等于整套测试不可用、更不因此放宽权限；CodeBuddy 直连已退休为人工转交、其额度冷却仅在离线回放中保留，S1 ZCode 手动额度政策保持现状。有 Read/Glob/Grep 时不额外跑 ls/date/cat/cd/echo/包装命令，未登记命令被权限拒绝须如实记“未执行”、不写成环境故障；未知写未知、时间由主脑记录。最终代码对应本次测试指纹，不把旧绿测当新、专项与全量不相加，报告格式失败与代码/测试验收分开、不为仅修格式重派，错误/部分成果保留、GPT/Luna 工程不自动替代，WorkBuddy 继续排除。证据侧：`scripts/zcode_execution_evidence.py` 现将 ZCode `tool_call_error` 作为明确失败终态按 toolCallId 归并（普通→`tool_result_failed`、权限→`permission_denied` 且 `is_error` 缺失也不掩盖拒绝），保留 `tool_call_result` 严格布尔 `success` 检查、孤立/畸形/身份漂移/成功与错误冲突检查与合法写入证据验收；`streaming_tool_ledger_updated` 的 `tool_result_committed` 仅为状态字段、绝不当成功，仅 committed 无 result/error 终态仍 `tool_result_missing`；原始事件全部保留不改写、不伪造报告、不放行业务。详见 [references/codebuddy-direct.md](references/codebuddy-direct.md)、[references/zcode-direct.md](references/zcode-direct.md) 的“派工范围与观测边界”与“执行证据门禁”。本段是本机 brain-worker 的观测口径，不作为通用 Skill 固化任何具体业务的授权或参数。

### 传输层

传输层（transport）是主脑驱动不具备所需命令执行、工具调用、附件读取或存档能力时，由宿主/运行环境提供的、只负责机械执行主脑明确指令的载体。传输层可以由宿主能力、受控工具代理或等价的薄封装承担；它没有独立任务目标，不拥有阶段范围的判断权，也不是执行 Agent。

是否需要传输层按“本次操作所需能力”判定，而不是只看产品名称：

- 主脑驱动能亲自调用所需命令、工具和接口并取得真实结果时，传输层退化为主脑自身的直接操作，不另设角色或伪造一次中转。
- 主脑驱动不能亲自完成某项调用时，必须显式声明该项操作由传输层承载；不得用“主脑已执行”掩盖实际由宿主代为调用，也不得让宿主自由理解主脑意图后自行开展工作。
- 嵌套部署中，`runtime_environment` 可以承载传输层，`brain_driver` 仍承担主脑职责。例如 `runtime_environment: muse`、`brain_driver: codex-cli` 且 Codex CLI 本身不能调用命令或工具时，Muse 只能作为传输层机械执行 Codex CLI 的明确控制面指令，不能代替执行 Agent 实施阶段任务。

传输层只允许执行下列动作：

1. 按主脑给出的确切命令、接口、参数、工作目录和停止条件执行能力预检，并原样返回退出码、标准输出、标准错误或机器可读响应。
2. 按主脑给出的确切命令或等价结构化调用执行 `dispatch`、`poll`、`events`；只有主脑已明确给出授权依据、目标任务标识和调用条件时，才可执行 `cancel`。
3. 原样中转主脑已经定稿的阶段提示词、任务标识、事件载荷和工具输出；可以执行接口契约已声明的协议解码，但不得摘要、补写、修正、重排或语义改写执行 Agent 的输出。
4. 按主脑给出的确切来源、目标、编码和校验要求保存报告或审计材料，计算哈希并回读；回读校验仅能检查主脑预先指定的机械条件，例如文件是否存在、字节数、SHA-256、首尾标记、任务标识和事件范围是否相符。
5. 按主脑给出的确切命令执行台账的 `status`、`record`、`resolve` 及写后回读；传输层不得自行决定评价、分数、问题等级、事件身份或命令参数。

传输层绝不能执行下列动作：

1. 不解释需求、不划分阶段、不选择修复方案、不判断证据是否充分、不作验收结论，也不决定下一步。
2. 不阅读项目并形成调查结论，不修改业务文件、测试、配置、数据或运行状态，不运行以完成阶段成果为目的的构建、测试、迁移、审计、部署或其他实质工作。
3. 不替执行 Agent 补做、返工或完善任务，不修改、拼接、润色、纠错或补全执行 Agent 的产出。
4. 不把主脑提供的补丁、逐字修改方案、代码片段或操作步骤“顺手”应用到项目。主脑即使已经给出精确到逐字的修改方案，该方案也只能进入下发给执行 Agent 的阶段提示词，不能转化成传输层的文件修改命令。
5. 不根据模糊请求自行选择命令、文件、参数、任务、事件游标或输出位置；遇到缺项、歧义、输出不符或未知状态时停止并原样回报主脑。

主脑交给传输层的每次指令必须显式、最小化且可审计，至少写明：

```text
TRANSPORT_INSTRUCTION
操作标识：
操作目的：仅限控制面、传输、存档或机械校验目的。
允许执行者：
确切命令或接口：
工作目录：
确切参数：
输入来源及任务/事件标识：
期望输出形式：退出码、标准输出、标准错误、机器可读响应、文件路径或哈希中的适用项。
机械校验条件：
停止条件与禁止动作：
TRANSPORT_INSTRUCTION_END
```

不得使用“帮我改一下”“按上面的方案处理”“把问题修好”“你看着执行”等需要传输层作实质判断的指令。传输层返回时必须绑定操作标识，原样提供实际命令或调用、参数、退出码、标准输出、标准错误、响应、产物路径和回读值中的适用项；不得把计划、模拟输出或自行概括的成功说明冒充实际结果。

**任何情况下不得以“传输”“协助”“代调用”“宿主能力”或类似名义行执行 Agent 之实。传输层一旦承担阶段内调查、修改、测试、迁移、审计、部署或其他实质工作，即视为主脑亲自执行，构成对本 Skill 分工和“下发 → 等待汇报 → 主脑验收”闭环的违反。**

### 能力预检

本 Skill 不假定宿主或工具的厂商、安装命令、专有 API，也不因配置中出现某个名称就认定能力可用。每种执行方式与后端启用前，主脑必须设计一次无副作用或隔离的探测，并依据实际接口说明逐项确认所需能力契约。主脑驱动能直接执行时由主脑操作；不能直接执行时，主脑必须通过 `TRANSPORT_INSTRUCTION` 给出确切探测命令、参数、期望输出和停止条件，由传输层机械执行并原样返回结果。能力是否通过仍由主脑判断，传输层不得根据探测结果自行启用执行方式或后端：

- `command_execution`：能以受控参数执行所配置命令，取得实际退出码、标准输出和标准错误；不得把命令存在等同于调用成功。
- `api_call`：能调用目标接口并识别认证、权限、限流和服务端错误；不得把网络可达等同于接口可用。
- `polling`：能使用同一任务标识查询状态，区分非终态、成功终态、失败终态、取消终态与未知状态。
- `attachment_read`：能读取用户约定路径或上传附件的完整字节内容，并可核对文件边界；仅在需要读取文件交付时要求。
- `writable_archive`：能在批准的存档位置创建并回读 UTF-8 文件、计算 SHA-256，且不会覆盖无关文件。
- `ledger_write`：能对所选台账执行 `status`、`record`、`resolve` 及写后回读，并保持事件幂等与身份门禁。

预检须确认能否观察任务的非终态进展，以及宿主能否在客户不再次询问时主动发送更新；不支持时必须明确声明降级方式。

`human-relay` 启用前至少预检实际交付链路需要的 `attachment_read`；需要主脑落地报告或写台账时，再分别要求 `writable_archive`、`command_execution` 和 `ledger_write`。`api-direct` 启用前必须预检 `command_execution` 或 `api_call`、`polling`、`writable_archive`，并验证适配器的 `dispatch`、`poll`、`events`、`cancel` 协议；若该阶段声明支持取消，`cancel` 也必须通过验证。`local` 台账启用前预检 `command_execution` 与 `ledger_write`；`feishu` 台账还须预检 `api_call` 并完成飞书章节规定的端到端就绪验证。

某项预检未通过时，由主脑只把依赖该能力的执行方式或后端标为“未就绪/不能认定为可用”，不禁用整个 Skill，也不影响已经通过预检的其他方式或后端。传输层只返回原始结果，不得自行把失败解释为通过、切换执行方式、改用替代命令或补做配置。主脑和传输层均不得把未验证能力写成“已支持”。

`api-direct` 的前提与异常处理：

- 必须由用户显式选择，并提供实际受控工具与接口说明（`config.yaml` 的 `worker.api_direct`，示例全为中性占位值）。用户未选择执行方式且未配置适配器时，默认使用 `human-relay` 并如实说明；用户已显式选择 `api-direct` 时，必须先完成适配器协议与能力预检，未通过则报告该方式"未就绪/不能认定为可用"，列出缺项并等待用户修复配置或明确切换执行方式，不得静默退回 `human-relay`。通用配置由主脑按本 Skill 解释执行，仓库没有自动消费配置的调度器；已验证的 Qoder CLI 专项调用见下一节。
- 适配器最小协议：`worker.api_direct` 必须提供 `dispatch`、`poll`、`events`、`cancel` 四个命令或语义等价接口；所有响应必须是机器可读格式。`dispatch` 返回非空任务标识，`poll` 使用该标识返回规范化状态，`events` 返回可按事件 ID 或稳定序号去重和追溯的事件流，`cancel` 返回取消请求是否已受理及取消后的可查询状态。配置还必须声明任务标识字段、状态字段、成功/失败/取消终态映射、错误码字段及超时行为。缺少任一必需契约，或实测响应与声明不符时，该适配器不能认定为可用。
- 有限轮询：按 `poll_interval_sec` 间隔查询同一任务标识，累计等待不超过 `poll_timeout_sec`（示例默认 1800 秒，按用户环境调整）；轮询超时按配置声明返回，不把超时自动映射为失败或取消。
- 超时/失败/中断：保留 provider、任务标识和最后事件游标，先查任务状态再决定动作，不盲目重发。轮询超时只表示主脑停止本轮等待，不自动调用 `cancel`，不等于任务失败或取消成功，也不进入下一阶段。只有用户授权、本阶段停止条件要求止损，或接口契约明确要求时才调用 `cancel`；调用后仍须轮询或回读确认实际取消终态及已产生的副作用。任务确认失败或被明确取消时按异常/止损路径处理，报告缺失本身若已能证明交付失败则按问题等级评价。
- 报告绑定与完整性：报告必须来自当前阶段下发的同一 provider 和任务标识，并记录实际使用的 session/turn ID、事件 ID 或事件范围；无关任务或范围外输出中的 WORKER_REPORT 标记不作为本阶段报告。提取后回读存档，复算 SHA-256，并核对首尾标记、阶段编号、项目路径及事件范围。报告缺失、出现多个无法唯一判定的报告块、截断、边界破损、来源不明或无法完整提取时，验收状态只能是"待补验证"或"阻塞"，不得凭部分内容验收。
- 终态判定只使用配置中已声明且通过预检的映射：成功终态才进入报告提取；失败终态进入异常验收；取消终态先核对已产生的副作用。适配器返回未映射状态、缺少状态字段、轮询中断、状态不可读或任务生死不明时，结果均为未知：停止相关推进，保留任务标识与最后事件游标，先查状态，不重发任务，不提前进入下一阶段，也不得把未知状态猜成成功、失败或取消。

选择执行方式时确认一次即可。同项目切换执行方式只能发生在当前阶段已经验收或被用户明确取消/中断并核对状态之后，按“执行 Agent 交接”处理；切换后先对新方式重新执行能力预检，再生成或下发下一阶段任务。新方式未就绪时报告缺项并等待用户决定，不得继续使用旧方式冒充切换成功，也不得静默改用另一方式。无论哪种方式，主脑不得声称调用了实际未调用的 Agent 或工具，也不得把“提示词已生成 / 任务已下发”等同于完成。

## 自然派工与默认执行者

- **何时主动派工**：任务呈现大量阅读、批量修改、开发、长测试或需要独立执行证据等适合委派的特征时，主脑读取最新能力台账与各直连档案的就绪状态，主动选择已就绪、已授权的外部执行器实际直连派工；不等待用户每轮点名执行者或指定固定配比（如 1:1:2）。简单规划、推理与单步检查可由 GPT 主脑直接处理；工程实施仍按上述分工与授权执行。只有缺少会影响执行的必要信息（项目绝对路径、授权范围、验收标准等）时才询问，其余不追问。
- **怎么选**：以“该成果由谁执行最合适”为准——台账评分、步长、限制与身份确认状态，直连档案预检是否通过，任务类型与隔离需求。本机未另行指定时按可用主力 1:1 轮换、明确 executor+model 授权组合优先，可在已有授权内承担相互独立的成果；主力不可用时改用溢出 Flash 或已授权接续，Qoder 沿用其既有入口配置、不覆盖配置。GPT 主脑只做规划、推理、确定性输入准备和独立验收；GPT/Luna 的工程实施须用户事先明确同意，不自动代做。GPT-6 Luna 是云端可选执行端，按上方原生档案检查当前工具、授权与调用审批，不自动成为兜底。用户明确指定执行者时遵从；选择变更只应用于验收后的下一轮，不替换在途任务或适用任务既有选择。
- **无可用直连执行器时**：如实说明缺项（未配置、预检未通过或配额终态），按授权改走 `human-relay` 或等待用户配置，不假调用、不冒称已派工、不自行用另一执行者冒充默认执行者。
- **成本口径**：个人版免费积分目标保留（例如个人版 Trae 免费积分）；企业 CLI、成本未知或零占位用量的调用一律不得冒称免费，账单未知就写未知。本仓库没有已验证的 Trae 直连入口，未验证前不宣称可用。
- **并行边界**：并行派工按“独立可验收成果”划分，允许有价值的并行、不凑并发数量；任务图/看板快照、计划预检、单项失败兜底与合并门禁见 [并行执行控制面](references/parallel-execution.md)。

## Qoder CLI 直连（api-direct 的受限 CLI 档案）

用户明确指定 Qoder、要求本机自动派工或免复制粘贴时，主脑可以使用已验证的 `scripts/qoder_direct.py`；不另开 Codex 主脑；Luna 原生可选档案独立核对当前授权与审批，不混入本 Qoder 调用。桌面 ZCode/Mimo 的人工转发方式及通用 API 适配器规则继续保留。

此档案是一次前台 CLI 调用，不实现通用 `dispatch/poll/events/cancel` 调度器，因此不按上节四接口协议冒充通用适配器已通过。其能力预检为：本机入口路径与官方登录态有效、受控参数调用、进程与输出可观察、原文存档和哈希回读、会话绑定及实际业务验收。脚本不提供取消 API、后台恢复或跨回合主动通知；若宿主不能观察运行中进程或在其返回时恢复验收，须报告该限制，不能宣称持续值守。模型任务已发送后不盲目重发，不自动杀进程。

- 配置：使用技能目录下的 `scripts/local-entry.json`，或显式传 `--config`。示例见 `scripts/local-entry.json.example`；真实文件只存本机，node 与 qodercli 必须是现有官方运行时文件的绝对路径。缺失即拒绝，不自动安装、不自动登录或回落另一执行者。
- 调用：`python <技能目录>/scripts/qoder_direct.py --workspace <项目绝对路径> --prompt-file <UTF-8提示词文件> --output-dir <不存在的新证据目录> --stage <非空阶段编号> [--model Qwen3.8-Flash] [--tools Read] [--allowed-tools RULE ...] [--disallowed-tools RULE ...] [--add-dir DIR ...] [--resume-session-id <已确认会话>]`。主脑或传输层只派工和机械回读，不代替 Qoder 做阶段实质工作。
- 模型：默认请求 Qwen3.8-Flash，不默认回落 GLM；观察到的 `modelUsage` 路由包括 `gfmodel`、`qfmodel`，它们不能独立证明后端模型版本。零 token/credits 字段不证明调用免费。
- 权限：`--tools` 控制工具可见性；需要限定范围时，逐项传 `--allowed-tools RULE`、`--disallowed-tools RULE`，外部读取另传 `--add-dir DIR`。显式允许规则完全替代兼容回退，不附加裸工具权限；不传规则时，旧工具名清单逐项转成允许规则，授权范围较宽。文件编辑与创建使用已验证的 `Edit(/工作区根相对文件)`，命令使用 `Bash(原样命令)`；不要用 `./路径` 或未经验证的根通配符。默认空工具，保持 `dont_ask`。权限拒绝须报告具体未执行动作，停止同类无效尝试，不重复索要已有授权。完整语法、回退行为和验证边界见 [Qoder 操作参考](references/qoder-direct.md)，直连不扩大生产、GitHub、凭据或外部写入授权。
- 报告：该已验收驱动的机械检查沿用九节兼容模板，主脑必须从 [Qoder 操作参考](references/qoder-direct.md) 原样嵌入完整报告块，不能误用下方通用四节交付模板。项目/任务标识与阶段对应关系在提示词和看板中明确登记；通用任务状态、主动汇报、下发确认与验收逻辑继续适用。
- 存档：`response.md` 是最终回复载荷的原样字节存档；同时保留 `stdout.json` 原始信封，记录请求、阶段、实际 session_id、哈希及续接请求。`body_ok` 只是正文核对，`bound` 还要求协议成功、非空会话、续接会话精确匹配、哈希回读及正文合格。协议退出码 0 不代表业务或绑定通过。
- 循环：收到该任务原始报告并完成主脑验收后，才可在既有授权内补修或下发依赖任务。每轮汇报真实调用、返回、验收和下一步；不把下发、进程结束或机械绿测说成完成。

## ZCode 官方运行时直连（api-direct 的受限 CLI 档案）

用户明确指定 ZCode、要求用官方运行时自动派工免粘贴时，主脑可使用 `scripts/zcode_direct.py`
（执行器 `scripts/zcode_sdk_runner.mjs`）。它与 Qoder 档案并列，不替换 Qoder 逻辑，也不改变
桌面 ZCode/Mimo 的 `human-relay` 转发方式与通用四接口适配器规则；Muse/Codex 主脑选择不变。

- 档案性质：同进程官方 SDK 前台调用（`startProcessProviderRegistryRuntime` +
  `createZCodeApp` + `submitPrompt`），不是 `dispatch/poll/events/cancel` 调度器，不得冒充通用
  适配器已通过。无取消、无后台恢复、无跨回合主动通知；宿主不能观察运行中进程时如实声明。
- 配置：`scripts/zcode-entry.json`（示例 `scripts/zcode-entry.json.example`，真实文件不入库），
  或显式 `--config`。五项必填都是本机官方文件的绝对路径：node、bootstrap dist 入口、tsx loader
  （以 `--import <fileURL>` 注册）、builtin 与 personal provider 配置。缺项或路径不存在即派工前
  拒绝；不自动安装、不自动登录、不读 token、不改桌面模型、不编译或修改官方源码。
- 调用：`python <技能目录>/scripts/zcode_direct.py --workspace <项目绝对路径> --prompt-file <UTF-8提示词文件> --output-dir <不存在的新证据目录> --stage <非空阶段编号> [--provider account:bigmodel-individual-coding-plan] [--model GLM-5.3-Flash] [--reasoning low|high|max] [--mode plan|edit] [--tools Read,Glob,Grep] [--resume-session-id <已确认会话>] [--preflight-only]`。
- 模型：默认请求 `GLM-5.3-Flash`；预检逐项核对注册表、`validateSelection`、`App.listModels()`
  的 `disabledReason`、`setModel` 后 `getCurrentModelOption().ref` 的 provider/model 与推理档位，
  任一项不符即零提交并按失败保留证据，绝不回落到别的模型。已验证的同进程证据只加载
  `account:bigmodel-individual-coding-plan`；StartPlan、TrustBuild 与免费额度均无证据，
  不得宣称个人套餐免费，`free_quota_verified` 恒为 false。
- 工具：只有整工具开关，没有 Qoder 式路径/命令规则，也不得仅凭 prompt 宣称机器限定路径或
  文件级沙箱。默认禁用官方内置工具全目录（含注册名为 `js` 的 node_repl、`Task`/`Agent` 子
  agent、`WebFetch`/`WebSearch` 联网、workflow、cron、Skill、Todo），`--tools` 显式名单才放行，
  未知名或空白项拒绝；提交时再叠加运行时活注册表并集。`--mode plan` 只读，出现 `Write`/`Edit`/
  `Bash` 派工前拒绝；`--mode edit` 需用户明确授权修改本项目；`yolo` 一律不构造。mcp、subagents、
  dynamic workflow、Browser、项目 hooks 与自动记忆抽取按官方 API 证据关闭。Task/子 agent、联网与
  Browser 工具对本入口不开放。
- 报告：沿用九节兼容模板（与 Qoder 档案同一份模板文本，见 [ZCode 操作参考](references/zcode-direct.md)
  与 [Qoder 操作参考](references/qoder-direct.md)），主脑原样嵌入完整代码块，不得误用下方通用四节
  交付模板，也不得把本档案说成通用 API 适配器。格式指令拼在原任务之前、任务逐字保留；
  `response.md` 是模型回复的原样字节，带前言时原文照存并判正文不合格，不替执行者删改。
- 存档与绑定：`request.json`、`process.json`、`preflight.json`、`stdout.json`、`stderr.log`、
  `result.json`、`events.jsonl`、`response.md`、`summary.json`、`report-state.json`。`body_ok` 只是
  正文核对；`bound` 还要求协议成功、SHA-256 回读一致、非空 session_id 与 resume 精确一致；
  `business_verified` 与 `free_quota_verified` 恒为 false，由主脑独立验收。退出码非零、预检失败、
  信封缺失或异常退出都不得说成完成。用量取本轮 `result.usage`，官方口径的缓存读取已计入输入，
  不再相加；`events.jsonl` 只落盘不整份打印。每轮须汇报真实调用次数、token 计数、事件数与
  session/turn 标识，验收后才能下发依赖任务。

ZCode 专项补充：活工具目录读取失败即零提交；完成以匹配 session/turn 的成功 turn_complete 和全部 model_request 精确模型记录为准，SDK 的 idle 投影不能单独判断成败。Read/Edit/Write 已实测；Bash 已实现受控命令审批（装饰官方公开执行端口为前置闸门 + 官方
`permissionBroker`，只放行 `--command-contract` 逐字登记且显式 cwd 的命令；只读 Bash 绕过 broker
仍必经 `executionPort.run`，故以装饰执行端口为强制点；官方 broker request 无 cwd，改用宿主 realpath 冻结
的 `trustedWorkingDirectory`，闸门在启动 inner 前用原声明路径重新解析并比对登记物理目标（junction/alias
改指即 `deny-input-target-moved`，登记期穿越别名的声明路径保守拒绝），再复核当前 input 文件字节/边界/SHA
（登记后改写仍被拦），审批只约束 `toolName === 'Bash'`（其它工具携同一登记命令亦规则拒绝），拒绝用官方有效错误形状、
spawn_error 不计已执行、`command_started` 在 started 审计写失败时仍单列保留、`claimResponse` 至多一次；REPAIR3：官方 Bash handler 硬编码注入 embedded-search `bashPrelude`（无既有会话级开关），旧闸门把它与 `env`/`stdin` 同列一律 `deny-extra-exec-channel` 会让合法登记命令也无法执行（LIVE-POSITIVE 即 broker 判 `approved-registered` 但 attempted/started/receipt 全 0——不是 GLM/登录故障、不是已执行）；现改为 runner 用同树公开解析器 `resolveDefaultEmbeddedSearchBackend` 冻结宿主可信 backend、回灌 `runtimeConfig.embeddedSearchBackend`+`nativeSearchEnhancementsEnabled:false` 并把期望 prelude 规范 SHA 绑进闸门，仅**逐字节匹配冻结 prelude 且真实 Bash trace（sessionId/turnId/toolCallId 非空、toolName==='Bash'）**才放行，否则 `deny-bash-prelude-mismatch`/`deny-bash-trace-unverified`；`env`/`stdin`/无绑定 prelude 仍拒、inner=0，`captureCwdAfterSuccess` 内部字段不据此拒，审计只记存在性布尔与结构 hash），但目前**仅离线（真实 runner + bootstrap
双身、真实官方 request 形状含 prelude+captureCwd）验证，未取得隔离真实现场回执**，不得把离线桩通过写成现场能力，也不宣称执行 Agent 已运行。
当次 Bash 现场能力未验或组件未 ready 时的路由，见下方「ZCode 能力验收与现场放行（决策规则）」默认路由
一条（计划阶段限定 ZCode 只做读写 → 既有 Qoder 串行登记测试；实际授权/环境/自动审批拒绝不能换执行器
绕过）；ZCode 的 CAPABILITY `fine_grained` 维持 `false`（无法表达 `grants.edits` 与 `grants.bash` 两种
细粒度规则，`command_contract` 是另一套本地逐字命令闸门、不授文件沙箱）。执行证据与报告绑定分离：`protocol_success`/`report_bound` 原含义不变，`business_verified` 始终由主脑独立验收；可选 `--execution-contract` 按“最小任务执行契约 × 同 session/turn 工具事件 × 磁盘回读”产出 `execution_evidence_ok` 与具体状态（无契约时显式 null，不默认 true），规则见 [ZCode 操作参考](references/zcode-direct.md) 的执行证据门禁一节。工程任务派工应声明并使用执行契约，业务完成仍由主脑独立验收；CLI 缺省无契约仅是为兼容旧调用的显式未验状态，不能当作工程完成的证据。

## ZCode 能力验收与现场放行（决策规则）

- **分层取证**：submitted（本机 App 提交调用已进入，**不独证服务端模型请求**，服务端须回读
  `model_request` 与终态）、工具可见（`tool_visible_bash`）、审批就绪（`approval_client_ready`）、
  闸门就绪（`controlled_pre_exec_gate_ready`/`command_contract_present`）、started（`command_started`）、
  真实退出+回执（`command_actually_executed`/`command_executed_receipts`）、独立业务验收
  （`execution_evidence_ok`、主脑判 `business_verified`）逐层分开记录，任一层不越权替下一层背书；
  某层 `false` 限定两种情形：任务**主动不授权/不要求该层**时 false 为预期、不当失败，任务**明确要求**
  的必要执行层 false/缺失时要**拒绝完成或拒绝放行**、按证据分类归因，未知写 `null`/「未验证」；
  原始 `business_verified` 仍恒 false，主脑独立结论只写进能力快照，绝不改写运行时字段。
- **现场放行**：同一 session/turn/toolCallId 的 gate `allowed=true`、该命令**自身独立**
  `execution_started` 审计、严格整数 exit0 完成回执与 stdout/stderr SHA 独立回读、输入事前事后
  未漂移齐全才可**放行成功**；**一切 Bash 调用（含只读）都须有当次 `command_contract` 并过 gate**，
  `zcode_direct.py` 在 Bash 无契约时派发前拒绝；只读可能绕过 permissionBroker 使 approval 不适用，但
  **绝不免除契约**。`summary.command_started`/回执 `started`（累计值）**不作逐命令证明**，缺该命令独立
  started 审计时即便累计>0 也不放行；单个 completed 或单个 `approved` 都不足。未登记命令须按其
  toolCallId 逐命令回读「拒绝且无 started/completed」，**不得**用累计数字反推 inner=0；
  started+真实回执即证「已执行」：非零退出（如 exit7）是**已执行但失败**，不抹成未验或零动作；未取得
  完整取证记缺证/未放行。「离线-only」只描述 REPAIR3 当前版本既有事实，不给任意缺条件案例套用。
- **可信 deny**：新并行计划的 `disallowed_tools` 从受信任本机入口 `build_tool_disallowlist(tools)`
  派生并与预检 actual 精确相等；runner 的 `tool_disallowlist_effective`（= base deny ∪（活 catalog −
  allowed_tools），只追加 deny）是更严格运行时边界，单列回读，不冒充预检已见 live catalog；
  `sent=false`/退出2 不计轮次；仅计划口径缺漏且尚未派发、不扩权时重建新计划并保留原计划/原拒绝，
  不在途改提示词或删 deny。
- **失败分型**：缺客户端、规则拒绝、prelude 不匹配、未启动、输入漂移、审批/执行取消超时、TLS/模型、
  报告格式、工程测试失败分别归因；`approval_client_ready=false` 单独不判缺客户端，实际错误按原始来源读：
  `execution_control.py` **已消费** `events.jsonl` 官方 `permission_resolved`/工具失败的缺客户端标记
  （会自动进 diagnostics），`permission-events.jsonl` 细项未全部自动映射、须主脑读原始事件归因，不一概
  断定「都会/都不会」。这些是独立验收分类，不宣称 `execution_control.diagnostics` 已全部支持，也不修改
  summary 旧六分类；`test_failure` 只来自登记测试
  实际非零退出，`no_required_execution`/`execution_claim_mismatch` 属缺执行/口径冲突，另列不写成测试失败。
- **默认路由**：本机未另行指定时按可用主力 `zcode:GLM-5.3`/`qoder:Qwen3.8-Max` 的 1:1 轮换、明确 executor+model 授权组合优先，主力不可用时改用溢出 Flash 或已授权接续（不再表述为“ZCode 默认优先、Qoder 唯一模型”）；当 Bash 现场能力
  尚未独立验收放行、或审批/闸门未就绪时，计划阶段显式把该任务限定为 ZCode 只做读写（Edit/Write 是
  写入不是「只读」），再退回既有 Qoder 串行跑登记测试并保留结果。`ready=true` 只表示组件就绪、
  **不等于现场已验证**；**不得**靠换入口/模型绕过真实授权拒绝。REPAIR3 版仅离线验证、尚无该版本
  隔离现场回执，L1 的 Read/Edit/Write 真执行不能当 Bash 现场证明；GPT/Luna 的工程实施须用户事先
  明确同意，规划与独立验收可由主脑直接做。`fine_grained=false` 限制派发计划无法表达 `grants.edits` 与
  `grants.bash` 两种细粒度规则（非只逐文件），`command_contract` 是另一套本地逐字命令闸门、不授文件沙箱。

字段清单、放行最小条件表、失败分型映射与可信 deny 派生的可复用示例见
[ZCode 能力验收与现场放行](references/zcode-capability-acceptance.md)；闸门/执行证据的操作细节仍以
[ZCode 操作参考](references/zcode-direct.md) 与 [并行执行控制面](references/parallel-execution.md) 为准。

## 使用场景与主脑选择

先区分四件独立的事：**主脑所在的宿主/运行环境**（`runtime_environment`）、**承担主脑职责的驱动**（`brain_driver`）、**在主脑驱动缺少执行能力时机械承载调用的传输层**（`transport`）与**提示词怎么送达执行 Agent**（`execution`）。不要把宿主环境、主脑驱动、传输层、执行 Agent 和执行方式混为一谈。实际部署可以是嵌套架构，例如在 `muse` 宿主环境中调用 `codex-cli` 作为主脑驱动，由 Muse 承载传输层。执行 Agent 可为外部桌面 Agent、受控 API 任务，或按 Luna 原生档案获准启动的云端子 Agent；主脑和传输层都不得亲自承担阶段内批量执行或其他实质工作。

- **GPT 桌面场景（原有用法，零配置）**：GPT 所在桌面环境是宿主，GPT 会话承担主脑职责，按 `human-relay` 执行——主脑生成完整、可复制的阶段提示词，用户转发给外部桌面 Agent，报告保存为原始文件后通知主脑直接读取（无法访问时上传）。若 GPT 会话能够直接读取附件或文件，则传输层退化为主脑自身的直接操作；若读取动作实际由宿主代为完成，则宿主仅按主脑明确指定的路径和校验条件机械回传内容。此场景不需要创建 `config.yaml`，也不需要任何 API 配置。
- **Linux 场景**：用户复制 `config.yaml.example` 为 `config.yaml` 后，分别配置 `runtime_environment` 与 `brain_driver`。`runtime_environment` 只记录宿主/运行环境名称；本 Skill 不假定其厂商、安装命令或专有 API。`brain_driver` 记录实际承担本 Skill 主脑职责的驱动。传输层按实际能力关系判定，不因某个环境或驱动名称自动成立。未创建 `config.yaml` 时，任何平台都按 `human-relay` 执行方式与 `local` 台账执行，不默认启用任何 API。
- `runtime_environment: muse`、`brain_driver: codex-cli` 表示在 Muse 宿主环境中调用 Codex CLI 作为主脑驱动；两者不是互斥选项。若该 Codex CLI 实例不能执行 shell 或调用工具，Muse 可以作为传输层，但只能执行 Codex CLI 通过 `TRANSPORT_INSTRUCTION` 给出的确切控制面命令、原样中转结果及完成机械回读校验。
- `brain_driver: muse` 时，由 Muse 中实际承担主脑职责的能力按所选执行方式执行；如果同一 Muse 能力可以直接调用工具，传输层退化为主脑自身的直接操作。启用前仍须通过“能力预检”。
- `brain_driver: codex-cli` 时，用 `codex exec` 加载本 skill；执行方式为 `human-relay` 时生成完整提示词由用户转发，执行方式为 `api-direct` 时由 Codex CLI 直接调用，或在其缺少调用能力时经传输层调用已通过预检的适配器，下发阶段任务、轮询并取回报告。无论调用由谁承载，都遵守阶段门禁、WORKER_REPORT 模板与验收标准，Luna 原生执行单独按可选档案核对；不得以宿主或传输层冒充执行 Agent，也不得以原生子 Agent 冒充外部 CLI。
- 宿主执行了主脑给出的逐字补丁、文件编辑命令、测试命令或其他阶段实质步骤时，宿主承担的是执行 Agent 工作，不是传输层工作；即使主脑完成了全部分析和方案设计，也仍视为主脑亲自执行。
- 无论哪种主脑和传输形态，核心规则、阶段门禁、报告模板、安全边界完全同一套；不按场景复制规则，也不因主脑缺少执行能力而削弱等待汇报与验收门禁。执行方式和传输形态只改变控制面调用由谁承载、提示词如何送达及报告如何取回，不构成绕过“下发 → 等待汇报 → 主脑验收”、自动连跑或让宿主代做的依据。

## 选择流程与执行方式

- 大量阅读、批量变更、长测试或需要独立执行证据的任务适合委派。简单规划、推理与独立验收可由主脑直接处理，工程实施仍须用户事先明确同意的执行器承担；用户明确要求此分工时遵循其选择。
- 确认实际项目绝对路径、目标、完成标准、允许动作、禁止事项及执行方式。沿用会话中仍有效的授权；缺失且影响执行的信息先问清，不能猜测路径。
- 执行方式为 `human-relay` 时，苦力 Agent 只使用用户转发的外部桌面 Agent（例如 ZCode、Xiaomi Mimo）。执行方式为 `api-direct` 时，主脑直接或经传输层使用用户已确认并说明的受控工具/接口下发任务。这两种外部方式不得以 Codex 子 Agent、宿主 shell 或文件工具冒充外部执行；Luna 原生档案是独立的可选执行方式，按当前用户授权与审批调用。不得把未获授权的新建 Codex 任务、宿主代改文件或传输层代跑阶段任务当作苦力 Agent。若用户未指定执行者与执行方式，沿用已确认的；仍不明确时先确认。
- `human-relay` 下，主脑只生成当前阶段完整、可复制的提示词，由用户转发；提示词必须要求外部 Agent 将完整报告保存成 UTF-8 `.txt` 或 `.md` 原始文件，并给出主脑可直接读取的文件绝对路径（同机无法访问、文件缺失或需跨机传输时才由用户上传原文件），不以聊天粘贴正文作为唯一交付。明确“尚未调用，等待用户通知报告已保存或带回报告文件”。`api-direct` 下，主脑能直接调用时真实下发；不能直接调用时先生成包含确切 `dispatch` 命令、参数和期望输出的 `TRANSPORT_INSTRUCTION`，只有传输层返回实际任务标识和原始调用结果后才能明确“已下发，等待执行完成”。两种方式都不得假称已调用、正在运行或编造结果。主脑可在已有授权内做有针对性的独立复核；主脑或传输层均不得代替苦力 Agent 执行阶段内的批量工作，主脑的复核也不得扩展为实施阶段方案。

## 阶段循环

1. 读取适用项目指令，只读核对目录、相关文件、未提交改动和必要运行状态。主脑驱动不能直接执行所需只读调用、但宿主具有传输能力时，只能通过明确的 `TRANSPORT_INSTRUCTION` 要求传输层执行确切的只读命令并原样返回结果；需要浏览、归纳、诊断或形成项目结论的检查必须交给执行 Agent。无法访问外部环境时，将检查交给执行 Agent，不声称已检查。不得借“只读核对”让传输层承担阶段内批量阅读或审计。
2. 以“可独立验收的成果”划分阶段：一个阶段交付一项主脑能整体验收的结果，不按阅读、改文件、跑测试等动作逐项拆分。阶段数量随风险和依赖确定，不强制所有任务固定三段——常规组织为“范围内完整执行与交付准备 → 主脑验收及必要补证 → 已授权发布与回读”，简单任务可更少，新增授权或不可逆外部副作用前后应有独立验收点。涉及状态、数据或外部对象的任务，在第一份阶段提示词中就定义必要的对象生命周期与兼容口径：新建还是已有对象、追加语义、幂等重试、恢复与失败路径、写前/写后核对；按任务选择适用项，不给所有任务强制一套完整清单。发出每一段提示词前，主脑必须完成一次范围反思：逐项对照客户原目标，问清“这项动作是否为达成目标所必需、是否已有证据支持、是否已获授权、能否缩小文件和测试范围”；删去没有必要的旁支修复、额外审计、顺手重构和预防性改造。若发现确有直接阻断项，说明它与目标的因果关系，并限定为解除阻断所需的最小步骤。可说明可能的后续方向，不预发依赖本阶段结果的下一阶段执行提示词。
3. 下发有边界的阶段任务，并在提示词中授予范围内闭环：同一锁定目标、授权与隔离环境内，执行 Agent 自行完成调查 → 复现 → 最小修复 → 相关回归 → 交付准备，允许修正自己发现的测试或实现错误并重跑，不为每个缺口回主脑交接。内部闭环的全部失败、修改与重跑证据照常写入报告，不无限无效重试：重复失败表明当前路径无效、需要扩大范围或授权、或外部状态改变时，停止受影响部分并报告，由主脑决策；不设机械的固定重试次数，也不以重试掩盖判断。交付准备属于执行成果：精确文件范围、测试证据、公开配置核对与拟提交内容在同一阶段完成。涉及真实业务动作、上传、迁移、删除、发布、外部写入或凭据处理的，执行前先核对目标、影响和保护措施，经主脑验收后在明确授权内有限执行并回读验证；已有明确授权的不重复询问，验收点不能跳过。
4. 下发后保持“等待汇报”状态，直到收到当前阶段报告（`human-relay` 下为原始报告文件，`api-direct` 下为 API 提取报告存档）并完成验收，或用户明确取消/中断。用户中途补充目标、背景、限制、Agent 或模型时，只确认并记录，验收后再用于下一阶段；不得把补充信息当作汇报，不得修改该在途任务已发提示词，不得为该被锁定任务生成当前或下一阶段的追加提示词，也不得建议用户中途转发。若用户要求中途改变执行内容，先确认是否已明确停止当前阶段；没有则对该任务保持等待，不另发提示词。此门禁只作用于被锁定的当前任务：其他无依赖、无写入冲突的任务在主脑并行预检通过后仍可照常下发，不因某任务处于“等待汇报”而全局冻结整批发派。提示词已生成、传输层已调用 `dispatch`、工具已接单或进度消息都不代表完成；不得补全缺失结果或提前推进依赖工作。`api-direct` 下“等待汇报”指按适配器终态映射轮询同一任务标识直到成功终态，再从事件流提取报告；主脑驱动不能直接调用时，每项调用必须由主脑以确切的 `TRANSPORT_INSTRUCTION` 交给传输层。轮询、事件读取和机械存档属于控制面操作，不是追加给执行 Agent 的提示词，但其中不得夹带新的任务内容、修改方案或执行要求。传输层只按已声明映射返回状态和原始事件，终态判断、异常处置和是否进入验收仍由主脑决定。轮询期间不得追加新消息打断执行（向客户汇报状态、更新任务看板、读取既有状态事件不属于追加执行提示词）。Qoder 适配器中 `idle` 映射为成功终态，具体映射见适配器协议配置。
5. 取得阶段报告后（`human-relay` 下同机路径直接读取原始报告文件，或用户上传；`api-direct` 下读取 API 提取报告存档及其追溯字段），先检查首尾标记是否各独占一行，再核对阶段编号、项目路径、文件或版本基线及证据。主脑驱动不能直接读取或存档时，传输层只能按主脑指定的路径、事件范围、编码、哈希和首尾标记条件执行原样读取、保存及回读，不能修复边界、补全内容或改写报告。报告内容的真实性、充分性、范围和验收结论由主脑判断。报告过期、路径不符、事件范围不符或基线变化时，先补核对。若应用把粘贴文本压平或截断，优先读取同机路径的原始文件，无法访问或需跨机时才请用户上传；不要仅凭粘贴结果归责 Agent，也不要要求重跑已经验证的测试。
6. 每个提示词阶段结束或被明确中断后，只要有足以判断表现的报告或失败证据，完成主脑验收并先按“桌面 Agent 与模型能力评估”自动更新持久台账，再回复验收结论或生成下一阶段提示词；无需用户另行提醒。报告缺失本身若已能证明交付失败，也应按问题等级评价；阶段仍在执行中则暂不评分。验收时对照锁定的客户目标和本阶段允许范围，指出任何未经授权的扩大，即使测试通过也不把旁支工作算作目标完成。验收缺口集中列出：集中给出本次验收已发现的相关缺口及其根因关联的必要路径（含相邻文件、相邻断言），补修提示词据此覆盖完整修复面，避免只补一处后再逐项发现相邻缺口；旁支改进只报告，不扩成本阶段任务。每个完整验收阶段只记一次评分，补修沿用同一阶段事件 ID，不拆成多个加分样本。通过验收且业务目标未完成时，依据实际结果生成下一阶段提示词，并再次执行第 2 步的范围反思；业务目标已完成但适用收尾未完成时，按“项目完成与收尾”继续推进，不在此处停止。证据不足只下发本阶段补验证任务；失败则限定本阶段修复范围并复验。越界或结果不明时停止相关推进，说明影响与待核对事项，不擅自回滚或清理。
### 任务制执行补充

- 可独立执行、汇报和验收的成果先注册为任务；提示词携带稳定任务 ID、范围、依赖和状态消息协议。
- 并行任务须确认不存在共享写入、环境冲突或未验收依赖。
- 允许有价值的并行，不凑并发数量：按独立可验收成果维护任务图/看板快照，相互独立的隔离副本可同时执行，存在依赖或共享写入的串行；原始报告验收前，该在途任务的提示词不改、不重复派发。
- 一项任务出现 429 或权限终态失败时，先按证据验收失败并保留原件，再仅在既有授权内对该任务启用兜底；其他独立任务继续执行，不因单项失败冻结整批。
- 新的并行派工必须为每个任务提供 `--dispatch-plan`（计划 JSON），由 `scripts/execution_control.py` 做任务级快照预检：把计划与实际入口选择、工具 grant 与 cwd 做精确比对，真实 argv 记录哈希供回读，缺 grant、扩大权限、同 task_id 在途改 prompt/重发、共享写入不能隔离或依赖未验收一律在 Popen 前拒绝（退出 2）。预检是主脑快照，不是原子跨进程锁，也没有常驻调度器或跨轮自动唤醒；默认并发 1，可显式提升。两个直连执行入口（Qoder/ZCode；CodeBuddy/WorkBuddy 已退役为 human-relay）对旧无计划调用保持兼容，仅追加 `summary.diagnostics`（六类 failure_types）。详见 [并行执行控制面](references/parallel-execution.md)。
- 卡住、失败或换方案时使用“状态汇报”，无需另建模板；收到完整交付后更新为`待验收`。
- 主脑核对任务 ID、交付物、SHA-256 和基线后，将任务更新为`完成`或退回`执行中`。

## 主脑最终汇报：来回总数与逐轮总结

- 项目完成时，最终回复必须明确写出主脑与执行 Agent 共进行了几轮来回，并按实际下发顺序展示每一轮总结。每轮至少包含：任务或补修目标、实际返回结果、主脑验收结论；失败、阻塞、补证和返工轮次都要列出，不能只列成功轮次。执行者或模型切换时，在对应轮次注明，区分模型请求值与已确认身份。
- 一轮指一次实际下发的任务、补修或补证指令，以及对应的报告、明确失败或中断结果；同一阶段或同一会话重新下发新指令也算新一轮。轮内工具调用、执行器自行修改重跑、主脑回读和轮询不另算来回。只有草稿或派工前预检失败的不算实际来回；未委派时写“执行 Agent 来回：0 轮，本次由主脑直接完成”。来回计数不改变按阶段记一次评分的规则。
- 逐轮记录沿用现有阶段记录、任务看板或调用存档，保留轮次、阶段/任务标识、执行者、模型信息、返回证据与验收结论。恢复会话时依据可核对的记录统计；历史记录缺失时写“已核实 N 轮，历史总数待核对”，不得猜测总数。尚无终态的调用单独列为等待返回，不宣称项目完成。
- 最终回复直接显示总数及全部逐轮总结，可用编号列表或表格；不得只给日志链接、只写“经过多轮修复”，或让用户翻看折叠的进度消息。先说明整体结果，再列来回摘要和交付状态；每轮简洁概括，不复制整份执行器报告。

## 项目完成与收尾

- 项目完成以任务看板为准：所有属于客户目标的任务均已进入“完成”，且验收结果为“通过”或客户明确取消。单个任务完成不代表项目完成。
- 当客户锁定的整体目标经主脑验收达成时，明确告诉客户“项目业务目标已完成”，简要列出实际交付、真实验证证据和仍未处理的事项。阶段报告写“阶段完成”或代码测试通过，都不能替代这次面向客户的完成告知。
- **主动收尾门禁**：业务目标通过验收后，立即检查本项目的项目笔记和 GitHub 交付是否适用、是否已完成。适用且未完成时，把它们列为当前项目的待办并持续推进，不得把“代码已修好/测试已通过”当作结束点，也不得等客户提醒才提出收尾。若由外部 Agent 执行，验收当前阶段后主动下发当前收尾阶段提示词；各阶段仍须分别验收，不预发依赖前一阶段结果的提示词。客户明确要求不做某项收尾时遵从其要求。
- 客户只排除某项业务动作（例如“不补发商品，只完成逻辑修复”）时，不把它解释为排除适用的代码交付与项目记录。结束前逐项核对业务动作、GitHub 远端回读和项目笔记写后回读；仅排除客户明确不要的那一项。尤其不能在本地代码已由服务加载、但应交付的修复仍未推送时宣称项目完成。
- 把本项目的结论、关键决策、证据位置、已知限制和后续事项同步到客户已有的项目笔记；优先更新对应的现有笔记，不另建平行知识库，不复制凭据、票据原文或大体量日志。写后回读笔记与链接。
- 更新项目对应的 GitHub 仓库：先核对正确仓库、分支、未提交改动与目标提交范围，只提交和推送本项目应交付的文件；排除凭据、运行数据、备份和无关改动。推送后回读远端提交与目标分支。客户已要求笔记/GitHub 收尾或会话中已有授权时直接推进，不重复索要授权；缺少某项外部写入或推送授权时，先完成只读核对及可审核的准备，再仅就该具体动作取得授权，不让它阻断已授权的其他收尾。目标位置、权限或发布范围不清时先完成可独立完成的工作，并准确报告阻碍。
- 业务完成与收尾完成分开表述。笔记或 GitHub 同步失败时，仍可报告已验证的业务结果，但必须把同步标为未完成并继续处理可解决的失败；不得把本地保存、提交或推送命令返回成功冒充远端更新。外部写入沿用阶段检查点，不能因收尾要求跳过主脑验收。最终回复逐项说明业务结果、笔记回读、GitHub 远端回读及未完成项；只有适用收尾均完成或客户明确排除后，才把整个项目标为完成。

苦力 Agent 汇报必须是一个完整报告。`human-relay` 下，报告保存为 UTF-8 原始报告文件，内容使用下面的固定边界和字段；阶段提示词须明确要求 Agent 保存文件并给出绝对路径，主脑可直接读取同机路径的原文件，无法访问、缺失或需跨机传输时才由用户上传原文件，不复制粘贴正文。提示词、工具接单、进度消息、传输层调用结果或自由叙述都不能替代这份报告。`api-direct` 下，主脑直接或经传输层从 API 事件流中提取 WORKER_REPORT 块，保存为 UTF-8“API 提取报告存档”后再验收，不把提取产物称为原始报告。存档必须附带或在同目录伴随记录：provider、task/session/turn ID、事件 ID 或事件范围、提取时间、存档内容的 SHA-256、是否原样提取。传输层只能执行适配器协议已声明的解码，并保存解码后执行 Agent 实际输出的完整原文；不得换行规范化、摘要、补写、纠错、合并多个候选报告块或实施其他内容转换。若接口限制导致无法原样保存，必须保留原始事件载荷和转换说明，由主脑将验收状态标为“待补验证”或“阻塞”，不能把转换件冒充原样报告。报告正文仍须检查首尾标记各独占一行。

发现卡住、失败、传输异常、越界或状态未知时，主脑应立即更新客户可见状态。汇报异常、请求决策和提出候选方案不等于向执行 Agent 追加提示词；接管、返工、补证或改变执行范围仍须遵守原有门禁：只有用户明确要求处理异常时，才可在未收到正常交付前生成针对该异常的接管、止损或补证提示词。先核对已知状态和可能副作用，标明未验收部分；异常处理不等于上一阶段通过，仍不得预发依赖其结果的下一阶段工作。

在对话中保留阶段编号、目标和授权、已发提示词、实际汇报、证据来源、验收结论、状态及下一步。使用传输层时，还必须保留每次 `TRANSPORT_INSTRUCTION` 的操作标识、确切命令或接口、参数、任务/事件标识、期望输出形式、传输层返回的实际调用与原始结果、存档路径、哈希和回读值；不能只保留“宿主已处理”之类摘要。中断恢复先核对记录与现状，不把历史成功当作当前成功；无需默认创建额外文件。

## 主动进度汇报

主动汇报由主脑负责。执行 Agent、API、共享状态文件或侧聊提供原始证据，主脑据此更新看板并形成客户可见消息。

以下事件触发汇报：

1. 任务实际下发或确认接单。
2. 达到可验证里程碑。
3. 到达任务登记的普通心跳点。
4. 任务卡住或状态未知。
5. 执行失败或准备更换方案、工具、路径、编码或传输方式。
6. 需要客户授权、选择或补充信息。
7. 收到完整交付，任务进入待验收。
8. 主脑完成验收。

每个任务注册时约定：

- 首次检查时间；
- 普通心跳间隔；
- 无进展或超时的卡住判定；
- 主聊聚合频率。

项目可以提供默认值，任务可以覆盖。未约定时，主脑按任务时长、风险和可观察能力选择合理间隔，不把固定的 10 分钟、15 分钟或连续两次无进展作为全局铁律。

普通心跳和一般里程碑可以按项目聚合，并允许使用比轮询频率更稀疏的客户消息频率。以下情况必须即时推送，不等待聚合窗口：

- 任务卡住或状态未知；
- 需要客户决策；
- 任务完成并进入待验收。

安全、凭据、生产、数据损坏或越界风险同样即时推送。

主聊承载客户可见的下发、聚合进度、受阻、待验收和验收结论。侧聊、API 事件和状态文件用于保存原始状态。关键状态不得只留在侧聊。

ETA 只能采用执行 Agent、工具或已有速率证据支持的时间；没有依据时填写“未知”，并给出下一检查时间。

传输失败、重试和换方案写入“状态汇报”。重试前应确认新方案改变了失败变量；重复失败且无法继续时，将任务标记为`卡住`。

若宿主不支持后台观察或跨回合主动通知，应在下发确认中说明限制、可观察渠道和下一次可检查时点。

## 试用进度与执行 Agent 交接

- 使用本 Skill 处理另一个项目时，分别汇报 Skill 试用进度和业务项目结果。每次阶段验收后说明：这套分工实际验证了什么、哪里未按规则工作或需要主脑纠正、Skill 本身是否已修改和复验、当前等待谁的真实汇报。业务代码或生产状态的成功不等于 Skill 已通过试用。
- 执行 Agent 或模型可以在阶段之间更换；交接以主脑验收过的证据为准，不要求新 Agent 继承旧 Agent 的对话。先完成上一 Agent 的实际汇报验收，再为已选定的新 Agent 和模型准备下一阶段提示词。用户中途指定接任者或模型，即使说“刚刚那步也改”，也只记录为待应用选择，不重写或补发当前阶段提示词；明确要求取消当前阶段或处理 Agent 异常时才按上面的例外执行。
- 切换时在下一阶段提示词内写清实际项目路径、当前运行/文件基线及未提交改动、已验证事实和证据位置、旧 Agent 的未验证主张、失败与未完成项、已有授权和禁止事项、当前阶段目标及交付格式。指出哪些内容须新 Agent 重新核对；不把旧 Agent 自报或过期快照当作新环境现状。
- 交接内容只取完成下一阶段必需的证据：上一阶段原始报告的绝对路径、主脑验收结论、相关补丁/测试产物的位置与哈希、已知业务副作用及尚未执行的动作。对进程、服务、Git 工作区、配置和业务状态标注观测时间；这些是接手基线，不是可跳过的现场检查。不要让接任者凭聊天摘要或上一 Agent 的“已完成”字样继续部署、点击或推送。
- 新 Agent 接手后先核对其实际桌面应用与模型身份、项目路径、文件版本和运行状态，再读取交接产物；若无法访问旧 Agent 的临时目录，应从正式仓和原始报告重建必要证据，不得猜测或重做已产生副作用的动作。命令行环境或编码方式变更时先验证传参，不把前一 Agent 的测试退出码和运行权限当作自己的结果。新组合的能力评分与步长按其自身共享台账读取，不继承旧组合的分数。
- `human-relay` 下仍由用户转发；`api-direct` 下由主脑经 API 下发；两种方式都不声称主脑已经调用新 Agent（没调就是没调），不向其转发 Cookie、密钥、令牌或无关业务数据。接任者若发现基线漂移或权限不足，应停在受影响步骤并汇报，不自行补做前阶段或进入后续阶段。

## 安全边界

- 默认不部署、不操作生产、不发送通知、不提交或推送 Git。只有用户明确授权的具体动作才能进入对应阶段；“修好”“完成任务”不自动授权生产或外部写入。
- Cookie、密钥和令牌不进入提示词、聊天、日志或提交；使用受控本地引用，仅汇报脱敏证据。账务资料只披露验收必需的最少信息，不默认向外部 Agent 转发原始敏感数据。
- 保护未提交改动，核对并报告变更范围，不覆盖他人工作。未经授权不停止任务、不重启服务、不改变运行状态。冲突或影响范围不明时先停止相关动作。
- 不得把 mock、启动成功、点击成功或局部测试通过说成真实业务完成。分别说明模拟、隔离验证、运行环境加载、真实业务回读各自证明了什么。
- 执行结果未知时先查状态，避免盲目重试导致重复写入；身份、目标、授权或安全条件不清时不执行受影响动作。
- **任何情况下不得以“传输”“协助”“代调用”“宿主执行”“按主脑方案落地”或类似名义行执行 Agent 之实。传输层只允许执行本 Skill 明列的机械控制面、原样中转、存档和回读动作；一旦由主脑或传输层实施阶段内调查、批量阅读、文件修改、测试、迁移、审计、部署或其他实质工作，均视为主脑亲自执行并判定为流程违规。主脑给出逐字方案不改变这一判断。**

## 提示词交付格式

- 给用户转发的完整阶段提示词，必须直接放在回复中的一个可一键复制的文本块内；在支持 writing block 的界面使用 `variant="standard"` 的 writing block，否则使用单个 fenced text 代码块。不要只提供下载文件或文件链接。
- 同一文本块包含完整任务说明和原样嵌入的 WORKER_REPORT 模板，不拆成多个复制块；解释、进度和等待状态放在块外。提示词文件可以作为附加备份，不能替代回复中的完整文本块。
- 用户仅要求调整显示格式时，只重新展示同一份提示词，不修改阶段内容、范围或授权，也不把格式调整当作新阶段下发。
## 当前阶段提示词模板

填成可独立理解、可复制的提示词。移除不适用项；保留明确禁令。发出前填入真实路径和具体通过条件，不留下需要猜测的关键字段。

```text
任务 ID：
任务目标：
依赖与范围：
当前状态：待派发
执行方式：
进度观察方式：
汇报约定：首次检查、普通心跳、无进展判定和主聊聚合频率
验收标准：
阶段编号与执行方式：human-relay（用户转发）/ api-direct（API 直调），填实际采用的一种。
实际项目绝对路径：
客户锁定的目标：引用或准确转述客户当前要完成的结果；不以 Agent 的建议替代。
背景与当前基线：已知事实、已有改动、相关运行状态；未知项明确列出。
交接（如更换执行 Agent）：上一阶段原始报告与验收结论、必需产物路径/哈希、已知副作用、旧 Agent 自报但未核实内容、带观测时间的运行/文件基线及接手时须重新核对的项目；无需切换时删除本行。
当前阶段目标：仅写本阶段结果。
范围反思结论：本阶段每项动作与客户目标的必要关系；已删去哪些非必要的扩大。
允许动作与文件/数据范围：具体到足以限制执行；同一锁定目标内可自行闭环（调查 → 复现 → 最小修复 → 相关回归 → 交付准备），允许修正自发现的测试/实现错误并重跑；发现旁支问题只报告，未经授权不顺手修改。
对象生命周期与副作用（涉及状态、数据或外部对象时，首阶段即定义）：新建/已有对象、追加语义、幂等重试、恢复与失败路径、写前/写后核对；不适用时删除本行。
禁止事项：默认禁令及任务额外限制；单独列出已有授权例外。
执行前检查：读取适用指令，核对路径、基线、未提交改动和必要状态。
验收标准与检查点：通过所需证据，真实动作前后的边界及回读要求。
停止条件：授权不清、基线冲突、泄露风险、目标不符或执行结果未知。
状态消息协议：接单后发送“下发确认”；到达里程碑、心跳点、卡住、失败或换方案时发送“状态汇报”；结束时发送“交付”。卡住、需要客户决策和完成待验收必须即时发送。`human-relay`：将“统一状态消息协议”一节的“交付”模板完整填写，保存为 UTF-8 `.txt` 或 `.md` 原始文件，放在会话中已约定、主脑可直接访问的路径；不要写入业务仓库或覆盖已有文件，无同机可访问路径时放在用户可上传的位置。回复用户时给出文件绝对路径并通知已保存完成，主脑直接读取该原文件验收，无法访问或文件缺失时才请用户上传，不要让用户复制粘贴报告正文。不要另发下一阶段提示词。`api-direct`：在最终回复中完整输出本阶段所用执行档案对应的 WORKER_REPORT 块（首尾标记各独占一行；Qoder/ZCode 直连使用各自操作参考中的九节模板，CodeBuddy/WorkBuddy 已退休为人工转交、不再直接派发，通用适配器使用下方“交付”模板），由主脑保存为 API 提取报告存档，并记录 provider、task/session/turn ID、事件 ID 或范围、提取时间、SHA-256 及是否原样提取。不要另发下一阶段提示词。
```

## 统一状态消息协议

主脑生成阶段提示词时，必须把“交付”报告块原样嵌入提示词，不能只写“按模板汇报”、只引用章节名称或改成自由叙述。下发确认与状态汇报在 `human-relay` 下由执行 Agent 按模板写入约定的状态位置或在接单/里程碑消息中使用，`api-direct` 下从事件流提取；只有“交付”使用 WORKER_REPORT_START/END 边界。

本节所说的“3 个模板”仅指执行 Agent 向主脑发送的状态消息；`TRANSPORT_INSTRUCTION`（主脑给传输层的控制面指令格式）、“当前阶段提示词模板”、“能力评估记录模板”分属各自独立协议，不计入状态消息模板数量。

统一规则：

- 只使用下发确认、状态汇报、交付 3 个模板。
- 无内容填写“无”，无法确认填写“未知（原因）”。
- 任务 ID 必须填写。
- 执行者、模型、provider 标识和 event ID 按需写入“证据/备注”。
- 状态汇报不能代替最终交付。

### 1. 下发确认

```text
TASK_DISPATCH_ACK_START
任务 ID：
任务目标：
执行者与方式：
下发状态：待转发 / 已下发 / 已接单
依赖与范围：
进度观察与汇报约定：
下发时间与下一检查：
证据/备注：
TASK_DISPATCH_ACK_END
```

### 2. 状态汇报

```text
WORKER_STATUS_START
报告类型：进度 / 卡住 / 换方案 / 失败 / 待验收
任务 ID：
报告时间与当前状态：
里程碑与已完成：
当前动作：
最新证据/备注：
阻碍、失败尝试或换方案：
下一步与下一检查：
预计完成时间或范围：
需要客户动作：
WORKER_STATUS_END
```

“卡住”“需客户决策”“待验收”必须即时发送。其他普通进度可按项目聚合。

### 3. 交付

```text
WORKER_REPORT_START
报告类型：交付
项目标识：
任务 ID：
报告时间：
当前状态：待验收
执行与来源备注：

一、交付物与 SHA
- 交付物绝对路径或正式链接：
- 交付物 SHA-256：
- 补丁 SHA-256：
- Git commit SHA：
- 基线信息：
- 不适用字段及原因：

二、已验证事实
- 事实：
  证据位置或回读方式：
- 事实：
  证据位置或回读方式：

三、测试结果
- 工作目录：
- 原样命令：
- 实际退出码：
- 关键结果：
- 未运行或失败的验证及原因：

四、未完成项与风险
- 未完成项：
- 缺失证据：
- 剩余风险：
- 已知副作用：
- 是否需要客户决定：

WORKER_REPORT_END
```

没有独立补丁时填写：

```text
- 补丁 SHA-256：不适用（未生成独立补丁；变更由 Git commit SHA 或交付物 SHA-256 标识）
```

没有 Git 提交时填写：

```text
- Git commit SHA：不适用（未创建提交）
```

基线信息至少填写以下一种：

```text
- 基线信息：基线 commit SHA 为……
```

或：

```text
- 基线信息：非 Git 任务；接手时基线为……，验证方式为……
```

报告边界规则：

- 交付报告正文首行只能是 `WORKER_REPORT_START`，末行只能是 `WORKER_REPORT_END`；标记必须各占一行，不能改名、嵌套或拆成多个文件。文件用 UTF-8 编码，不能在标记前后添加说明或代码围栏。
- 事实必须附可定位证据；推断必须明确标注；测试必须给出原样命令、工作目录和实际退出码。
- 报告中不得放入凭据、Cookie、密钥、令牌、账务原文或无关项目数据；无法验证的内容写“未知”。
- 主脑收到完整交付后才开始正式验收；边界破损或关键字段缺证时，状态只能是“待补验证”或“阻塞”。

### 报告与看板归档

- 中间状态可保存为控制面记录，但不强制逐事件归档。
- 最终交付仍可保存为不可变的 `WORKER_REPORT` 存档（`human-relay` 下为原始报告文件，`api-direct` 下为 API 提取报告存档，附带 provider、task/session/turn ID、事件 ID 或范围、提取时间、SHA-256）。
- 任务看板、状态记录和报告文件不得写入业务仓库。
- provider 标识、event ID、执行者和模型按需并入证据/备注，不作为固定独立字段。

## 桌面 Agent 与模型能力评估

为了选择合适的执行 Agent，主脑把“桌面应用”和“实际使用的模型”分开记录。共享评分的唯一标识是 `桌面应用 + 已确认的模型名称/版本`，同一组合的不同项目和任务类型共同影响评分与下一阶段步长；任务类型、阶段、证据和问题留在逐次记录中。**每个已验收的提示词阶段都必须得到一次有依据的能力评价**；模型身份暂缺时先记录评价和拟加减分，待用户或运行证据确认模型后补计到该组合，不能把阶段永久丢为零分。

### 步长与评分

- 新评估对象从 `步长=1`、`评分=50`、`样本数=0` 开始；共享评分表示该应用和模型组合已验收任务的总体试用表现，不代替具体任务类型的限制判断。
- 主脑验收为“无问题”后，评分 `+10`，样本数 `+1`，步长 `+1`：第一次通过为 `2`，下一次连续通过为 `3`，以此类推；默认上限为 `5`。
- “有问题”时必须先判定等级：轻微问题评分 `-5`、步长 `-1`；重大问题评分 `-15`、步长 `-2`；越界、泄密、错误宣称真实完成或其他严重安全问题评分 `-30`、步长降为 `1`，并暂停相关高风险推进。步长最低为 `1`，评分限制在 `0–100`。
- 步长是主脑安排下一阶段的**审查参考**，控制复核强度、检查点密度和允许的副作用范围：步长高时中间检查点可更少、复核粒度可更宽；步长低时检查点更密、证据要求更严、允许的副作用范围更小。步长不是同阶段子步骤、文件或测试种类的数量上限，不能被解释为“最多修一行”或“最多跑一种测试”；执行 Agent 在单一锁定目标内完整闭环（见“阶段循环”第 3 步）。步长也不表示可以跳过阶段验收、合并未来阶段或自动扩大授权；生产、发布、删除、迁移、外部写入和凭据动作始终按一个独立验收点处理。
- “无问题”必须由主脑根据完整报告、证据、测试退出码和越界检查判定，苦力 Agent 自评不能直接加分。报告缺字段、测试不实或其他质量问题时应按实际严重程度减分；模型身份不清只暂缓归属，不免除该阶段的质量评价。关键执行证据缺失时先标记待补验证，补证后再完成评价，不凭空认定通过。

### 能力等级与选型

- `80–100`：推荐；至少有 3 次主脑验收通过，且最近 3 次没有重大或严重问题。
- `60–79`：可用但需逐阶段审查；可以承担常规执行，不能据此放宽安全边界。
- `40–59`：观察；只安排小范围、低风险、证据容易回读的任务。
- `0–39`：受限或暂不推荐；先补验证或更换 Agent。
- 样本少于 3 次时，等级必须标注“试用中”；跨任务选择时同时看任务类型、样本数、最近问题和剩余步长，不能只按分数排序。

更换桌面应用或模型时建立新的评估对象；同一组合在不同对话中共用同一评分。再次使用时重新核对当前模型版本、登录身份、运行环境和具体任务限制。历史阶段若当时缺模型信息而后来由用户明确补充，可在核对原事件和是否已补评后追记一次；不得把已计分样本重新计入。

### 自动记账与复用

- 台账后端二选一，默认 `local`（`scripts/local_ledger.py`：本地 JSONL 文件存储，纯 Python 标准库、零配置，命令与参数兼容 status/record/resolve）；`feishu`（`scripts/capability_ledger.py`：走飞书多维表格）为可选后端，需自行配置飞书应用与表格，见“飞书后端接入（可选）”。在 `config.yaml` 中用 `ledger` 字段选择后端。评分转换、步长、能力等级、身份规则（未知身份不可计分、pending→resolve 只计一次）与事件幂等语义（指纹含 evidence/issues，issues 顺序不敏感；生成字段不参与）由两后端共同调用同一纯逻辑核心 `scripts/ledger_core.py` 实现；存储格式、锁与网络各自适配，事件文件/表格互不通用。名称归一只认核心明确列出的精确等价别名（应用/模型整串相等才替换），不做前缀或包含匹配——未列出的名称与不同版本保持独立身份，不合并不同模型版本。历史事件不改写：单一历史存储键可用同一归一规则兼容匹配且读分不变，写入时新事件沿用该唯一既有存储键作为稳定身份（规范键只用于匹配，不制造第二个存储键）；多个历史存储键映射到同一规范身份时判为身份碰撞，在任何追加/外部写入前明确拒绝并报告，禁止静默汇总、迁移或重算。同一 event-id 内容冲突时保留原 ID，核对原事件后修正重试内容或走明确补证流程，不得换新 ID 重复计分。
- 飞书后端用两张表：模型总账表保存共享状态，评分事件表做追加式事件链；同一飞书应用权限支持跨对话读写。若曾混用本地与飞书，以当前所选后端的事件链为准；核对旧记录后可单独补记，已有新评分事件时不得重算旧基线覆盖它。飞书记录/补评在第一次写入前按同一精确身份规则联合核对事件表与模型总账：两表键不一致、总账重复、总账身份字段与存储键矛盾或仅有总账而缺事件链时，明确拒绝且两表零写入，不自动迁移、改键、合并或覆盖已有评分。
- **每次发出当前或下一阶段提示词之前**，运行所选后端的 status 命令（默认 `python scripts/local_ledger.py status --agent "桌面应用" --model "模型名称/版本"`；飞书后端把脚本换成 `scripts/capability_ledger.py`），以当次回读的共享评分、步长、样本数及限制制定提示词；若草稿已写好，也要在发出前重读并调整。主脑驱动不能执行命令时，由主脑通过 `TRANSPORT_INSTRUCTION` 给出完整命令、工作目录、参数和期望输出，由传输层机械执行并原样回传；传输层不得选择评估对象、改写参数、解释评分或据此生成提示词。不能沿用本对话早先读到的值或仅依赖总账页面缓存。
- 每个提示词阶段结束或被明确中断、且报告或失败证据足以验收时，主脑立即给出 `无问题 / 轻微问题 / 重大问题 / 严重问题` 中一个评价，并运行所选后端的 record 命令（默认 `python scripts/local_ledger.py record --event-id "项目路径+任务类型+阶段编号的稳定唯一标识" --agent "桌面应用" --model "模型名称/版本或未知" --task "任务类型" --stage "阶段编号" --evaluated-at "带时区的 ISO 时间" --outcome "主脑评价" --evidence "报告或失败证据位置"`（飞书后端换脚本名，参数不变）。主脑驱动不能执行命令时，必须先由主脑完成评价和所有参数取值，再通过 `TRANSPORT_INSTRUCTION` 把完整命令交给传输层；传输层只执行、回读并原样返回，不得替主脑选择评价、问题等级、事件 ID、身份状态或证据。模型身份已确认时加 `--identity-confirmed`，立即加减分；尚未确认时不加该选项，脚本写入 `identity_pending` 事件及拟评分变化，暂不归属任何已确认模型。问题或限制用 `--issue` 记录。不能用 `证据不足` 代替“模型暂缺”或掩盖报告质量问题。同一阶段重试保持事件 ID 不变；命令、重跑、多个子步骤不能拆成多个加分样本。
- 用户后来补充实际模型，即使报告当时未写明，也算有效身份补证。先核对原阶段待确认事件和已计分事件，再运行所选后端的 resolve 命令（默认 `python scripts/local_ledger.py resolve --pending-event-id "原事件ID" --agent "桌面应用" --model "确认后的模型名称/版本" --identity-evidence "用户确认或运行证据位置" --identity-confirmed`（飞书后端换脚本名）；按阶段时间顺序处理多条待确认事件。脚本以 `resolve:原事件ID` 追加唯一计分事件并回读，同一阶段只计一次。旧格式 `assessment/证据不足` 事件不能直接用 `resolve` 补评；须先核对是否已有人工补评分或已计入迁移基线，再做一次性补记，严禁重复加减分。
- 脚本使用本机互斥锁串行化不同对话的更新：先追加事件，再回读事件链并更新模型总账；重试同一事件 ID 不重复计分。每条事件写入实际评估时间和机器人记录时间。生成下一阶段提示词前再运行 `status`，新升降分及步长才算生效。总账与事件不一致时以可验证的事件链为准，并修复总账；身份、事件链或回读不清时停止评分和依赖新步长的提示词。
- 无需等待用户提出“记录评分”。已验收阶段缺模型身份时记录待确认评价；执行证据不足时保留待补验证状态并追取证据，不能宣称完成或凭空加分。所选后端不可读取或写入时说明阻塞，不把未落盘的新分数当作下次起点；恢复后用同一事件 ID 补记，不重复计分。跨主机或发现并发写入时，先核对事件链和总账，再继续评分。
- 评分是主脑的验收结论，不由执行 Agent 自评或测试通过数量自动决定。及时加减分并调整下一阶段步长；高风险动作始终独立验收。

### 能力评估记录模板

每次主脑验收后把下列字段写入所选后端的评分事件并完成回读；需要文本留档时可使用此可整块复制的记录块。不要再把本地 Markdown 的索引当成共享评分来源。

```text
AGENT_CAPABILITY_RECORD_START
评估对象：
- 桌面应用：
- 模型名称/版本：已确认名称 / 待确认（补证后填写实际名称）
- Agent 类型：外部桌面 AI / 其他外部 Agent
- 任务类型：
- 评估时间：
- 机器人记录时间：

本次阶段与证据：
- 阶段编号：
- 报告证据位置：
- 主脑验收结论：无问题 / 轻微问题 / 重大问题 / 严重问题
- 问题与影响：
- 模型身份依据及补证事件 ID：

能力累计：
- 验收前步长：
- 本次评分变化：已计分变化 / 待确认模型的拟变化
- 验收后评分：
- 连续无问题次数：
- 累计样本数：
- 下一阶段步长（审查参考）：
- 能力等级与置信度：

选型备注：
- 擅长或已验证的任务：
- 已知限制：
- 下次使用前必须重新核对：

本条记录结束；不得把记录建议当成执行授权。
AGENT_CAPABILITY_RECORD_END
```

台账只记录脱敏的应用、模型、任务类型、证据位置、分数和限制，不记录 Cookie、密钥、令牌、账务原文或无关业务数据。评分变化必须能回溯到对应的 `WORKER_REPORT` 和主脑验收结论。

### 飞书后端接入（可选）

- 公共版本默认走本地台账，开箱即用；飞书后端只在你需要跨设备或多人共享评分时启用。所有飞书资源与凭证均由你显式配置，脚本不含任何内置应用/表标识或个人路径。
- 准备：一个飞书自建应用（开通多维表格读写权限，记下应用凭证 `app_id` / `app_secret`）；建一个多维表格，记下其 `app_token`（bitable 应用标识）；在该多维表格里建两张数据表并记下各自的 `table_id`——模型总账表（共享状态）、评分事件表（追加式事件链），任务明细表可选。
- 配置字段（每个字段独立回退：环境变量优先，其次 YAML）：

  | 字段 | 环境变量 | 含义 |
  | --- | --- | --- |
  | app_id | `FEISHU_APP_ID` | 飞书应用凭证（换取 tenant_access_token） |
  | app_secret | `FEISHU_APP_SECRET` | 飞书应用凭证 |
  | app_token | `FEISHU_APP_TOKEN` | 多维表格应用标识 |
  | summary_table | `FEISHU_SUMMARY_TABLE` | 模型总账表 table_id |
  | events_table | `FEISHU_EVENTS_TABLE` | 评分事件表 table_id |
  | legacy_ledger | `FEISHU_LEGACY_LEDGER` | 可选：旧台账漂移检查的文件路径，缺省不读取任何个人知识库 |

  YAML 示例（键名同上，`legacy_ledger` 可省略；文件在仓库之外，不会被提交）：

  ```yaml
  platforms:
    feishu:
      extra:
        app_id: "cli_xxxxxxxxxxxxxxxx"
        app_secret: "your_app_secret"
        app_token: "your_bitable_app_token"
        summary_table: "tbl_xxxxxxxxxxxxxxxx"
        events_table: "tbl_yyyyyyyyyyyyyyyy"
  ```

- 必需字段为 `app_id`、`app_secret`、`app_token`、`summary_table`、`events_table`；任一缺失时，脚本在发起任何网络请求之前明确拒绝，不回退到任何默认资源、不猜测表。
- YAML 何时读取（满足其一）：任一必需字段未由环境变量提供时（读 `~/.config/brain-worker/platforms.yaml`，可用 `BRAIN_WORKER_PLATFORMS_CONFIG` 指向自己的文件）；或显式设置了 `BRAIN_WORKER_PLATFORMS_CONFIG` 时——此时即使必需字段已由环境变量提供，可选字段（如 `legacy_ledger`）也从该文件回退，且文件缺失、损坏或结构错误会明确报错，不静默忽略。五个必需环境变量齐全且未显式指定 YAML 时，不读取任何 YAML 文件；YAML 各层节点（根节点、`platforms`、`feishu`、`extra`）必须是键值映射，否则给明确的类型错误提示。
- 环境变量方式不需要 PyYAML；只有走 YAML 配置路径（含显式指定文件以获取可选字段）时才按需加载 PyYAML（需先 `pip install pyyaml`）。
- 两表必需字段（名称与类型）：
  - 模型总账表：`模型键`（文本）、`桌面应用`（文本）、`模型/版本`（文本）、`共享评分`（数字）、`累计样本数`（数字）、`当前步长`（数字）、`能力等级`（文本）、`最近评估时间`（文本）；`已知限制`（文本，可选）。
  - 评分事件表：`事件ID`（文本）、`事件类型`（文本：`baseline`/`assessment`/`identity_pending`）、`模型键`（文本）、`桌面应用`（文本）、`模型/版本`（文本）、`任务类型`（文本）、`阶段编号`（文本）、`评估时间`（文本）、`记录时间`（文本）、`主脑结论`（文本）、`证据`（文本）；`问题与限制`（文本，可选）、`评分变化`/`评分后`/`步长后`/`样本数后`（数字，评估事件）、`迁移源哈希`（文本，基线可选）。
- 在 `config.yaml` 里把 `ledger` 设为 `feishu` 后，启用前必须完成一次端到端就绪验证：核对两张表的 schema 与字段类型；验证应用对目标多维表格及两张表的实际读写权限；用隔离的测试身份和稳定事件 ID 依次执行 `status`、`record`、`resolve`；分别回读事件表和模型总账，确认事件只追加一次、待确认身份只结算一次、评分/步长/样本数一致；最后按授权清理或明确保留测试记录。现有的必需字段拒绝、配置类型检查、写前身份/事件门禁和 `status` 预检继续生效，不能由这次就绪验证替代。任一步缺证或失败时只能把 `feishu` 后端标为"未就绪/不能认定为可用"，不得写入真实评分；这不禁用整个 Skill，也不自动改变已选后端。用户明确切换到 `local` 后才可使用本地后端。

## 主脑验收

对每项通过条件给出“通过 / 不通过 / 证据不足”，至少检查：

- **事实**：证据是否可定位，是否对应当前阶段、路径和环境。标明“Agent 自报”“证据支持”或“主脑独立复核”；看不到的文件和输出不能声称已复核。
- **推断**：依据是否充分，是否被错误包装成事实。
- **未完成项**：是否阻止阶段通过或整体完成；无关改进建议不自动扩成任务。
- **测试结果**：代码任务必须报告文件范围、测试命令、退出码和未完成验证。核对实际输出、适用范围和失败原因，不只接受“全绿”。
- **实际链路**：检查测试是否覆盖当前运行入口、真实数据字段或状态取值及关键副作用边界；若结论依赖的路径仅由 mock 覆盖，标出缺口，不凭测试数量通过业务验收。
- **越界动作**：比对实际动作与授权，说明已知和未知影响；业务成功不能抵消越界。
- **传输层合规**：使用传输层时，核对每项操作是否存在完整的 `TRANSPORT_INSTRUCTION`、实际调用原始结果及回读记录；检查传输层是否自行选择参数、解释结果、修改执行 Agent 产出或承担阶段实质工作。缺少可审计指令或发生实质代做时，不得以“宿主协助”豁免，必须按主脑亲自执行和流程违规处理。
- **缺失证据**：精确列出需补的文件、命令结果或回读。补验证仍受原授权限制，不为证明完成擅自执行真实业务动作。
- **缺口集中**：一次性列出同一成果的全部相关缺口及根因关联的必要路径（含相邻文件、相邻断言），补修提示词据此一次覆盖完整修复面，不逐项分批披露；旁支改进只报告，不自动扩成本阶段任务。
- **必需与可选**：区分用户目标所必需的验证与可选集成未覆盖的范围。明确可选、未实现或由用户自行配置的外部服务未联调时，如实写为公开限制，不自动扩成本项目发布阻断；未验证能力不得表述为通过。
- **交付范围**：区分用户要求的产品结果与 Agent 自行制作的演示、截图或测试页面。辅助材料不能被命名或汇报成用户要求的新产品功能，也不自动进入部署范围。
- **任务 ID 与看板一致**：报告任务 ID、阶段编号与看板登记一致。
- **任务范围及并行隔离**：未违反依赖关系、并发上限，未与其他执行中任务修改同一文件、外部对象或运行状态。
- **交付物完整性**：交付物路径、交付物 SHA-256、补丁 SHA-256 和基线信息完整；不适用已明确说明原因。
- **已验证事实**：每条事实有可定位证据。
- **测试结果**：测试命令、退出码与关键结果可信。
- **未完成项与风险**：明确列出，无粉饰。

### 复盘验收规则

以下八条源自真实项目复盘的已证缺口，验收与补修时逐条对照；它们细化而不替代上文检查项。

1. **载荷消费**：新接线或改造管线时，必须验证产出真实进入下游——对实际 worker → runner → CLI 的最终载荷做断言；“函数存在、单测通过、存过哈希”都不构成接线完成。
2. **原文件与发送载荷**：新调用的 `prompt_sha256` 与计划均绑定原文件字节；实际任务文本、报告契约与完整发送通道分别留存 UTF-8 字节、哈希和变换口径。CRLF/LF 或契约注入可使发送哈希不同，不能混用。原文件不改、在途计划不改；旧记录按原版本解释，新调用生成新计划。详见 [并行执行控制面](references/parallel-execution.md)。
3. **业务复用键**：缓存/复用键必须覆盖所有影响结果的输入项（如动作、颜色、衣服纠正、布局、参考字节与顺序等项目内对应字段），仅绑定批次号不够；相同输入的重试与重启才复用，缓存命中分支同样校验当前执行身份；对已有产物的纯复检不再额外调用规划器。
4. **永久变更证据**：停用、淘汰、删除等永久变更须有对象级直接证据，按实际接口核对对象身份并回读确认；审计口径、回读口径与候选剔除口径一致；同步结果未确认的有效对象不得暗排除，不为间接关系捏造键。
5. **测试语义复算**：测试断言本身可能写错（如把覆盖集当变更集、把普通标点当路径）；主脑按真实语义复算断言与失败基线归因，不为迎合错误测试修改业务。多测试集重叠时按去重口径汇报，不相加冒充总数。
6. **报告契约与格式缺口**：直连档案的九节契约（标题、字段同一行、首尾标记、结语）由入口注入并机械校验；原始报告保留（含绑定不合格原件）时，业务验收独立进行并分开表述，不为格式单独多派一轮；必要格式补证与必要业务补修合并到同一补修任务，格式问题与业务问题分别归因。
7. **稳定输入与测试执行者**：主脑确定性准备稳定、非凭据的输入 fixture，记录来源与哈希，区分真实样本和合成示例，不让执行 Agent 逐行重新序列化既有数据；派工时明确测试由谁执行（执行 Agent 无命令权限时由主脑运行），避免重复无权限命令。
8. **失败终态与身份边界**：429/6004 等明确终态即真实失败，不盲跑重试、不静默换模型；零占位用量不冒称免费，缓存 token 不重复累加；身份未确认的模型事件保持待确认，不计入已确认组合评分；旧台账漂移只告警，不阻断当前项目。

验收结论写明依据、局限和当前下一步。无法独立访问外部环境时，说明结论依赖哪些报告证据；关键证据不足时不能宣称通过。

## 状态与结束

任务只使用以下五种状态：

- **待派发**：任务已登记，尚未确认下发。
- **执行中**：已经接单，正在执行或等待约定检查点；原“等待汇报”归入此状态。
- **卡住**：因阻碍、失败、状态未知或需要决策无法继续；原“暂停 / 阻塞”归入此状态。
- **待验收**：已收到足以验收的交付或失败证据；原“待补验证”按实际情况归入`执行中`或`待验收`。
- **完成**：生命周期已关闭；验收结果另记为通过、不通过后终止或客户取消。原“阶段通过 / 阶段未通过”写入验收结果，不再作为独立状态。

项目完成条件：所有属于客户目标的任务均已进入“完成”，且验收结果为“通过”或客户明确取消。单个任务完成不代表项目完成。

这些是交接状态，不自动调用平台的目标、定时任务或配置工具。用户要求等待验收时，交付当前成果后停止。


## CodeBuddy / WorkBuddy：仅人工转交（直连已退休）

按 2026-10-08 用户最新明确决定，CodeBuddy 与 WorkBuddy 一律改为 human-relay only：**不再从
当前 Skill/CLI/控制面提交任何新的直接派发调用**。选择它们执行时，主脑只产出完整、可复制的阶段
提示词，由客户人工交给外部 Agent 处理；生成提示词不记成已派发/已验收，也不冒充 API 返回结果。

- 生产入口 `scripts/codebuddy_direct.py` **已彻底移除可执行传输**：不再 `import subprocess`、
  不再有 `dispatch_core`，因此生产路径结构上无法 `Popen` 任何真实 CLI。`main()` 在读配置/提示词、
  创建输出目录、进入额度门禁之前对任何新直连固定拒绝，返回 `sent=false`、`status=manual_relay_only`
  与非成功退出码；无 plan、带 plan、`--resume-session-id`、`--quota-recovery-probe`、配置存在/缺失
  一律不能绕过，不提供重新启用参数、环境变量或隐藏入口。生产模块只保留纯解析/终态诊断/历史取证
  函数。控制面 `execution_control.preflight` 对 CodeBuddy/WorkBuddy 计划（同时检查 plan 与 actual
  的真实 runtime）同样拒绝。
- 当前操作方式：用 `scripts/prompt_contract.py` 生成含完整九节契约 + 任务原文的可复制提示词，
  交给外部 Agent；执行 Agent 把 WORKER_REPORT 保存为 UTF-8 原始文件后按 `human-relay` 流程回读
  验收（见上文 human-relay 一节）。
- 当前直连派发只保留 Qoder 与 ZCode；两者正常派工不受本次退休影响，并接入跨会话共享的并发容量池
  （见 [全局并发容量池与 1:1 路由](references/global-dispatch.md)）。

以下内容为**历史直连档案**，保留原有解析/诊断/额度/终态口径与原始错误模型信息，仅供离线证据
回放（真实传输管线已整体迁到**仅测试**的 `tests/offline_codebuddy_harness.py::replay_dispatch`，
在隔离合成 stub 下重放旧失败链；harness 只认 `sys.executable` + 仓库可信固定 stub + 声明的内容
SHA-256，禁止真实 node、禁止任意 stub 目录里的真实 CLI、禁止生产默认用户配置），
**不是**当前可执行的操作指引，也不宣称旧 API 已彻底无产出：

- 历史调用形状：`python scripts/codebuddy_direct.py --workspace <绝对路径> --prompt-file <UTF-8
  任务文件> --output-dir <不存在的新证据目录> --stage <非空阶段编号> --model <实际模型ID>
  [--tools ...] [--allowed-tools RULE] [--disallowed-tools RULE] [--resume-session-id <会话>]`；
  要求显式模型、无 auto/fallback，曾选 `glm-5.3-flash`。此调用现被生产入口固定拒绝，仅存档。
- 历史传输细节：官方 `-p --output-format stream-json`，任务经 stdin、不经 shell 拼接；保持
  dontAsk、工具白名单、空 strict MCP、空 setting sources、关闭 hooks 与主 Agent cli；工具允许规则
  是 CLI 权限、非文件沙箱；命令按原样运行，不添加 cd/echo/复合命令绕过规则。
- 历史报告与终态口径（离线回放保留原始失败）：使用九节兼容报告，原文响应落盘后核对协议终态、
  会话、模型记录、完整正文与哈希回读，业务验收独立进行；`init.tools` 不证明有效授权，最终
  `permission_denials` 为空也不能证明无拒绝，须核对真实 tool_result 明确拒绝。仅
  `subtype=error_during_execution` 且 `is_error=true` 且带非空 `errors` 才是合法失败信封；缺/空
  errors、矛盾 subtype/is_error 记结构错误；摘要保留 primary_failure、原始 errors、failure_stage、
  recoverability，reset 只在实际提取到窗口时才有，429 只认明确 status/code=429（仅 quota 标
  unknown）；CLI 退出码 0 不伪成功；重复 init 除白名单 `__timestamp` 外比较全字段键集与逐值；普通
  `File does not exist` / `<tool_use_error>` / `is_error=true` 记入 tool_failures 仅诊断。
- 历史模型/成本口径：精确模型名来自 CLI init/assistant 记录，不独立保证后端版本；用量与
  modelUsage 保存原始口径、缓存分项不与 input_tokens 重复累加，cost=0 不证明免费；账号积分与
  可用模型需登录后核对（本次不查询额度/认证/账单）。
- 历史直连曾是一次前台子进程调用，非通用 dispatch/poll/events/cancel 服务；现该直连已停用。

完整历史解析档案见 [CodeBuddy 历史直连参考](references/codebuddy-direct.md)。


> **Luna 原生任务结束后的收口**：Luna 原生任务没有本地子进程，不能走普通 finish。
> 任务真实结束后，先用原生工具回读该 agentID 的终态并保留原始回执，再执行：
> `dispatch_pool.py settle-native --task-id <task> --token <claim-due 的 token>
> --agent-id <agentID> --terminal finished|native_failed --receipt '{"source_tool":
> ...,"receipt_ref":...,"sha256":...}'`。它会校验票据/agent/scope 与回执元信息，
> 在一个事务里把 attempt 与票据置终态并释放原 task/workspace（不影响国内六名额）。
> 运行中/未知状态、缺回执、信息不匹配一律拒绝且不释放；重复结算只有"同终态+同原
> 回执哈希"才算幂等成功，错误 agent/不同终态/换回执一律拒绝；--success 只能与
> terminal 一致（finished=成功、native_failed=失败）；sha256 必须是 64 位十六进制，
> string 回执原文按 UTF-8 原字节哈希（不加 JSON 引号）；launch_unknown 需人工核验，
> 绝不自动重派；worker 说"我结束了"不算终态，必须宿主回读原生回执；国内 attempt
> 永不经过 settle-native 退出。
