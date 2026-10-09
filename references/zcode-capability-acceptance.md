# ZCode 能力验收与现场放行（共享专项参考）

本文件是 brain-worker 的 **ZCode 能力验收专项参考**，与仓库版/安装版同一份文本，随技能
原样安装。它只做**文档层的验收口径**：把「提交、工具可见、审批、闸门、启动、真实退出与
输出回执、独立业务验收」逐层拆开，规定每层引用哪些**既有**原始证据字段、现场放行的最小
条件、失败如何分型、可信 deny 如何派生，以及评分台账如何通过 evidence 引用独立能力快照。

- **不新造运行时字段**：本文引用的字段名取自 `scripts/zcode_direct.py`、
  `scripts/zcode_sdk_runner.mjs`、`scripts/zcode_permission_broker.mjs`、
  `scripts/zcode_execution_evidence.py` 与 `scripts/execution_control.py` 已经产出的
  `summary.json`、`permission-events.jsonl` 审计事件、`events.jsonl`、`execution-evidence.json`
  和登记测试执行器记录。本文**逐层拆开与放行判定属于主脑验收口径**，不是运行时逐个原样吐出的
  状态名；不声明任何尚未实现的运行时能力，也不把主脑分类冒充成运行时字段。
- **不新建调度/台账代码**：本文不引入常驻 scheduler、跨进程锁或新的评分后端。
- 操作细节仍以 [ZCode 官方运行时本机直连](zcode-direct.md) 与
  [并行执行控制面](parallel-execution.md) 为准；本文只补「验收如何逐层取证、何时放行、失败如何分型」。

## 一、分层能力记录（字段 / 证据位置）

能力必须逐层拆开记录，任何一层都不越权替下一层背书。下表**第一列是层级、第二列是运行时
已产出的原始字段**（含义不变），第三列是承载它的原始审计/证据文件，最后一列是**主脑验收
结论**（本文口径，独立于原始字段与原始审计状态名）。三者分开：原始字段（`summary.json`
里的布尔/计数）、`permission-events.jsonl` 里的原始审计状态（如 `execution_started`/
`execution_receipt`/`rule-denied`）与主脑分类结论，不混为一谈。
某层为 `false` **限定两种情形**：任务本就**不授权/不要求该层**时 false 为预期（不当失败）；但任务
**明确要求**的必要执行层为 false/缺失时，须据此**拒绝完成或拒绝放行**、按证据分类归因，不能一概
「不当作执行失败或模型故障」。未知一律写 `null` 或「未验证」。

| 层 | 既有原始字段（来源） | 证据位置 | 主脑验收结论 |
| --- | --- | --- | --- |
| 提交 submitted | `envelope.submitted` / `preflight_ok` | `preflight.json`、`stdout.json` | 本机 App 提交调用已进入 / 零提交（预检或拒绝）；**不独证服务端模型请求**，服务端须回读 `model_request` 与终态 |
| 工具可见 | `tool_visible_bash`（活 catalog 命中 Bash ∧ effective 不含 Bash） | `summary.json`、`observed_tool_catalog`、`tool_disallowlist_effective` | 可见 / 不可见（不推断审批或执行） |
| 审批就绪 | `approval_client_ready` | `summary.json`、`permission_states` | ready / not-ready（缺客户端 ≠ 拒绝） |
| 闸门就绪 | `controlled_pre_exec_gate_ready`、`command_contract_present` | `summary.json`、`request.json` | ready / not-ready（缺契约则显式未验，不默认 true） |
| 启动 started | `command_started`（`_startedCount`，单列透传，为累计值） | `summary.json`、`permission-events.jsonl` `execution_started`（按 toolCallId） | 该命令自身有独立 started 审计才算 started；仅累计>0 不作逐命令证明 |
| 真实退出+回执 | `command_attempted`、`command_actually_executed`、`command_executed_receipts` | `summary.json`、`permission-events.jsonl` `execution_receipt`（status/exit/cancelled） | started+真实回执即可证「已执行」；非零退出（如 exit7）也是已执行只是命令/测试失败；exit0+完整取证才确认成功并放行 |
| 独立业务验收 | `execution_evidence_ok`（true/false/null）、`business_verified`（恒 false 由主脑判） | `execution-evidence.json`、`summary.json` | 主脑独立结论，运行时不自证 |

要点：
- `attempted`（放行并入 inner）、`started`（观测到基础端口 `execution_started` 事件）、
  `executed`（`status==='completed'` 完成回执）三个计数**诚实且分离**；`spawn_error` 只计入
  attempted（已进入 inner），`started` 严格按实际 `execution_started` 审计单列，**可能为 0**；
  三者互不折算。`completed` 与 exitCode 无关：非零退出仍会写 `status==='completed'` 回执、
  仍属真实执行，只是命令/测试失败，**不得据此抹掉执行事实**。
- 协议字段 `protocol_success`/`report_bound` 含义不变；`business_verified` 与 `free_quota_verified`
  始终 false，由主脑独立验收；主脑结论只写进**独立能力快照**（见第五节），**绝不改写运行时字段**。

## 二、现场放行条件（on-site release，逐条最小）

一次 ZCode Bash「现场能力」只有在同一次隔离真实验收里同时取得下列证据后才可**放行成功**；
started+真实回执即可证「已执行」（即便 exit0 之外的失败退出），未取得完整取证记**缺证/未放行**，
不得写成现场成功。**「离线实现已备、现场未验」只描述 REPAIR3 当前版本这一既有事实，不给任意缺
条件案例套用**：某次真跑失败就是「已执行但失败」，不是「未验」或「零动作」：

1. **同一 session/turn/toolCallId 串联**：approval（适用时）、gate 判定、started 事件、exit0 完成
   回执都归属同一 `sessionId`/`turnId`/`toolCallId`，且 `attributes.toolName==='Bash'`；跨
   session/turn 事件不参与本次放行判断。
2. **command_contract 是一切 Bash 调用的前提（含只读）**：任何实际 Bash 调用都必须有当次
   `--command-contract` 登记的逐字命令并显式 cwd，且通过 gate；`zcode_direct.py` 在「Bash 可用但无
   command_contract」时**派发前即拒绝**。只读命令可能绕过 permissionBroker 的 ask 路径，故 approval
   对只读**可能不适用**，但**绝不免除 command_contract 与 gate**。`toolName` 严格为 `Bash`，其它
   工具携同一命令一律 `rule-denied`。登记且走审批路径的无副作用命令，`permission_states` 命中
   `approved-registered` 或人工 `approved`。
3. **gate 通过**：该次执行的 `permission-events.jsonl` gate 审计 `allowed=true`，且
   `controlled_pre_exec_gate_ready=true`，未落在任一 `deny-*` 拒绝分支（含
   `deny-bash-prelude-mismatch`、`deny-bash-trace-unverified`、`deny-extra-exec-channel`、
   `deny-unregistered-command`、`deny-input-*`、`deny-cwd-*`）。
4. **started（逐命令）**：必须按**该命令自己的** `sessionId`/`turnId`/`toolCallId` 在
   `permission-events.jsonl` 里回读到对应的 `execution_started` 审计；`summary.command_started`
   是 `_startedCount` 累计值、`execution_receipt.started` 是 `startedEvents>0` 的累计布尔，
   **二者都不能作逐命令证明**——缺该命令独立 started 审计时，即便 summary 显示 started>0 也
   **不得放行**。（`execution-evidence.json` 的「不强制 started」口径只适用于普通读写取证，
   **不适用于**本文的 Bash 现场放行。）
5. **真实退出码 0 + 输出哈希回读**：`command_actually_executed=true` 且该命令的
   `execution_receipt` 记录 `status==='completed'`、`exitCode` 为**严格整数 0**，并对该次
   stdout/stderr 字节做 SHA-256 独立回读一致；非零退出只说明命令/测试失败，仍属真实执行。
6. **绑定运行环境**：放行记录须绑定 runner/broker 版本 SHA、SDK/官方运行时绝对路径（realpath）、
   实际请求模型与 `model_request` 身份、命令原样字符串与其 cwd 的 realpath，以及
   `input_sha256` 的事前/事后一致复核（输入未漂移）。
7. **未登记命令须证明未启动**：对未登记命令，须按**其自身 toolCallId** 在 `permission-events.jsonl`
   回读到 `deny-*` 拒绝审计且**没有** `execution_started`/completed 回执，即逐命令证明「拒绝且
   零启动」；**不得**用 summary 的累计数字反推 inner=0 或未启动。

不足条件（必须显式排除的错误推断）：
- **单个 completed 回执不足以放行成功**，**单个 approval `approved` 也不足以放行**：二者只各覆盖一层，
  缺该命令独立 started 或缺 exit0+哈希回读就**不确认成功、不放行**；但 started+真实回执已足以证「已执行」，
  非零退出是已执行但失败，不得据此抹成「未验」或「零动作」。
- `spawn_error`/`timed_out`/`cancelled` 不抹掉已启动事实：这三态**保留「可能已启动」的记录**，
  `started` 仍单列透传，绝不折叠成「零动作」，也不归为 GLM/登录/配额故障。
- `approval_client_ready=true` 只表示本机装了受控审批组件，**不等于人工审批界面已接入**；在无
  `command_contract` 或 Bash 主动禁用时该值为 `false` 是**预期**，单凭 `false` **不判**
  `permission_client_missing`；实际缺客户端错误按**原始来源**读。诊断自动性须区分：
  `execution_control.py` **已消费** `events.jsonl` 里官方 `permission_resolved`/工具失败结构中的
  缺客户端标记（`CLIENT_MISSING_MARKERS`），这部分**会自动**进入 diagnostics；而
  `permission-events.jsonl` 里的细项（rule-denied/cancelled/timeout 等）**未全部自动映射**，须主脑
  读原始事件后归因。不得一概说「官方旧错误/所有 client-missing 都不会自动 diagnostics」，也不得
  一概说「都会」。仅当声明需要受控审批却缺证据时，另记「未就绪/缺证」。
- 本次文档优化任务**不要求为取得现场模型重跑现场执行**：现场放行证据由主脑在独立隔离验收中
  取得，与本文档改动分属不同成果。

## 三、失败分型（独立验收分类，不冒充运行时已支持）

下列细项是**主脑验收时的独立分类口径**，用于把「未启动/未放行」的根因分开记录。它们
**不等于** `scripts/execution_control.py` 的 `summary.diagnostics.failure_types` 已经全部支持——
现有 diagnostics 只有六类（`quota_429`、`permission_rule_denied`、`permission_client_missing`、
`protocol_parse_failure`、`model_execution_failure`、`test_failure`）加上由执行证据门禁单列的
`no_required_execution` 与 `execution_claim_mismatch`。**本文不修改这六分类，也不把它扩写成
虚假能力**；下列细项引用原始事件/状态由主脑分别归因。

| 独立验收分类 | 原始事件/状态依据（既有） | 与既有六分类的关系 |
| --- | --- | --- |
| 缺客户端 | `events.jsonl` 里官方 `permission_resolved`/工具失败含 `CLIENT_MISSING_MARKERS`（已自动进 diagnostics）；`permission-events.jsonl` 的 `client-missing` 细项（须主脑读原始事件）；`approval_client_ready=false` 只是本地组件未就绪 | 归 `permission_client_missing`：官方 events.jsonl 缺客户端结构会自动分型，audit 细项与 ready=false 不自动，须按原始来源读；ready=false 单独不归此，缺客户端 ≠ 规则拒绝 |
| 规则拒绝 | `permission_states=rule-denied`（`deny-tool-not-bash`/`deny-unregistered-command`/`deny-composite-or-variant`）、`decision=="deny"` | 归 `permission_rule_denied` |
| prelude 不匹配 | 闸门 `deny-bash-prelude-mismatch`/`deny-bash-trace-unverified`，`embedded_search_prelude_*` | 归 permission 拒绝侧，**不是**模型/配额；单列记录 |
| 未启动 | 该命令 toolCallId 在 `permission-events.jsonl` 无 `execution_started`、无 completed 回执 ∧ gate 审计 `allowed=false` | 不属于六分类的模型失败；是「未执行」证据，须逐命令回读，不据 summary 累计数字反推 inner=0 |
| 输入漂移 | `deny-input-target-moved`/`deny-input-sha-mismatch`/`deny-input-missing`/`deny-input-path-unresolved` | 拒绝侧独立记录，不改判执行失败 |
| 审批/执行取消超时 | `cancelled`/`approval-cancelled-by-caller`/`approval-timeout`；执行侧 `timed_out`/`cancelled` | 保留「可能已启动」，不归 quota/模型 |
| TLS/模型 | 信封错误/`model_execution_failure` 结构、原始错误码保留 | 归 `model_execution_failure`（仅在已排除 quota/permission/parse 后） |
| 报告格式 | `report-state.json` 的 `body_ok=false`、首尾标记/九节不符 | 格式问题**不**自动算代码/测试失败，单列 |
| 工程测试失败 | 登记测试执行器真实 `exit_code!=0`（实际跑出的非零退出） | 归 `test_failure`，**只**来自登记测试的实际非零退出码；`no_required_execution`（应执行却零执行）与 `execution_claim_mismatch`（自述与证据矛盾）是缺执行/口径冲突，另由执行证据门禁单列，**不写成实际测试失败** |

保留项：`quota_429`（必须观测到明确 429 状态/错误码，仅 `category=quota` 不算）、
`protocol_parse_failure` 维持原义；`no_required_execution`（需读源码的工程任务在同 turn 零工具调用）
与 `execution_claim_mismatch`（回执与磁盘/baseline 矛盾）继续由执行证据门禁单列，不由本文替换。

## 四、可信 deny 派生与当前默认路由

新的并行计划在生成 `disallowed_tools` 时，从**受信任的本机技能入口**导入
`build_tool_disallowlist(tools)`（`scripts/zcode_direct.py`，默认禁用全目录、仅显式名单放行），
把结果原样填进计划的完整 deny 集合，并与预检的 actual 集合做**双向精确相等**核对（见
[并行执行控制面](parallel-execution.md) 的 grant/规则口径比对）。

- 运行时的 `tool_disallowlist_effective` = 计划 base deny ∪（`observed_tool_catalog` −
  `allowed_tools`），即活注册表里**未被显式放行**的项才追加为 deny；它仍**只做运行时追加 deny**，
  是**比计划更严格的运行时边界**，须**单独回读**核对。**不得说预检已经见过 live catalog**——
  预检比对的是计划 base deny 与入口 argv 推出的集合，live catalog 只在 runner 提交时叠加。
  独立回读用集合相等核对（**这是主脑独立回读检查，不是 runner 已执行 provenance/effective 等式的
  额外门禁**；当前代码只做集合相等，本文不宣称代码已实现来源/provenance 门控）：
  ```python
  expected = set(base) | (set(catalog) - set(allowed))
  assert set(effective) == expected  # 精确集合相等
  # 该等式不得删掉 base 中任何项，也不得把 allowed 之外的未知工具放进 allowlist。
  ```
- 派发前被拒绝（`dispatch_plan_rejected`、`sent=false`、退出 2）**不计模型轮次**，也不建证据目录。
- 仅「计划口径缺漏」（deny/allow 集合漏项或多余、未对齐 `tool_visibility`）且该任务
  **尚未派发、重建不扩大权限**时，才可重建新计划；重建保留原计划与原拒绝记录，
  **不得在途修改已发提示词、不得删除 deny 或换变体绕过**。

可复用示例（`SKILL_SCRIPTS` 是主脑按当次技能安装位置登记的绝对路径常量，示例中用 `<技能安装目录>` 泛化，**禁止**从模型响应或随意路径 import；**仅用于新计划初始化**）：

```python
# 受信任本机入口导入；SKILL_SCRIPTS 由主脑按当次技能安装位置登记，非模型提供
import os, sys
SKILL_SCRIPTS = r"<技能安装目录>/scripts"
assert os.path.isfile(os.path.join(SKILL_SCRIPTS, "zcode_direct.py"))  # 先核对是既有技能入口
sys.path.insert(0, SKILL_SCRIPTS)
from zcode_direct import build_tool_disallowlist

def init_plan_deny(plan: dict, tools: list) -> dict:
    derived = build_tool_disallowlist(tools)  # 完整 deny，供预检精确核对
    existing = plan.get("disallowed_tools")
    if existing is not None and set(existing) != set(derived):
        # 已有显式 deny 与派生值不同：报差异、停下核对授权，不静默覆盖或删除显式 deny。
        raise ValueError("disallowed_tools 与派生值不一致，交主脑核对授权")
    plan["disallowed_tools"] = derived
    return plan
# 之后由 execution_control.preflight 把 plan 的 disallowed_tools 与实际 argv 推出的集合双向相等核对；
# runner 的 tool_disallowlist_effective（含 live catalog 追加）单列回读，不在预检阶段冒充。
# 「可信派生」只补全/核对，不得当作重写原授权的借口。
```

## 五、评分台账通过 evidence 引用独立能力快照

评分台账（`local_ledger.py` / `capability_ledger.py`）只登记**主脑验收结论**：`--outcome`
取全部合法值之一（`无问题`/`轻微问题`/`重大问题`/`严重问题`/`证据不足`）与 `--evidence`
指向一份**独立能力快照**（现场放行记录、失败分型结果或 `execution-evidence.json`/
`main-acceptance` 的绝对路径），不把这些结论字段塞回运行时信封。

- 快照里的**原始字段**（第一、三节的既有状态）与**主脑验收结论**（放行/分型判定）分开存储，
  台账只引用快照位置，不复算运行时字段。
- 现场未验时快照写 `null`/「未验证」，不得因离线桩通过补记现场成功；某层 `false`：任务主动不要求时
  不单独触发减分，但任务明确要求的必要执行层 false/缺失要据证据拒绝完成/放行——是否加减分仍以主脑对整
  阶段的评价为准（每阶段一次、幂等）。
- 更换 runner/broker 版本、模型或入口后，能力快照按新版本重新核对，不继承旧版本放行结论。

## 六、既有实现现状（截至本参考，不夸大）

- REPAIR3 版 runner/broker（本机 context 记录 SHA256
  `2FD759D9848AA2E06D2F062BFA5A594DFD387169DBB854DC48958825B364BA8B`）把官方 Bash 硬编码注入的
  embedded-search `bashPrelude` 纳入绑定：runner 用同树公开解析器 `resolveDefaultEmbeddedSearchBackend`
  冻结宿主可信 backend 并把期望 prelude 规范 SHA 绑进闸门，**仅逐字节匹配冻结 prelude 且真实 Bash trace
  才放行**，否则 `deny-bash-prelude-mismatch`/`deny-bash-trace-unverified`；`env`/`stdin`/无绑定 prelude 仍拒、inner=0。
- 该版本目前**仅离线**（真实 runner + bootstrap 双身、真实官方 request 形状含 prelude+captureCwd）
  验证，**尚未取得该版本隔离真实 Agent 命令回执**；不得把离线桩通过写成现场能力。
- 更早历史版本曾记录过隔离现场 exit0+回读成功，只归**对应历史版本**，不泛化当前版本。
- L1 实测的 GLM-5.3 `model_request` 只证 Read/Edit/Write；Bash 主动禁用且无 `command_contract`，
  四项 Bash `false` 是**预期未授权**，不是新的审批故障；读写真执行**不能当 Bash 现场证明**。
- ZCode 的 `fine_grained` 维持 `false`：这限制**并行派发计划**无法表达 `grants.edits` 与
  `grants.bash` **两种**细粒度规则（不是只限制逐文件）；`command_contract` 是**另一套独立**的
  本地逐字命令闸门，只核对逐字登记命令，**不授予文件沙箱**。二者不混谈。
  当前默认路由见下一节，须与 SKILL/zcode-direct/README 一致。

## 七、当前默认路由（与 SKILL/zcode-direct/README 对齐）

- 直连候选 **ZCode / GLM-5.3** 与 **Qoder / Qwen3.8-Max** 在**合格可用**候选间尽力 **1:1**；用户明确
  给出的已授权 **executor+model 组合是硬约束、优先于历史比例**，有空位的指定 Qoder 绝不因 committed
  比例被改道 ZCode。默认 AUTO 主力优先，两主力均无合格可用槽时可走溢出 Qwen3.8-Flash，用户明确指定
  已授权 Flash 时按其真实容量独立受控派发（主力各 2、Flash 2、国内合计 6 属 Skill 策略，非官方 Qoder
  已知并发上限）。不覆盖配置、不新增或替换模型，模型/权限/工作区/同任务安全守卫一律保留。普通终态
  失败按已授权路由处理，**只影响该任务**，其它独立任务继续。（历史：早期曾表述为“本机未另行指定时
  ZCode 优先、Qoder 沿用现有唯一模型兜底”，现按 BW-AVAILABILITY-20261009-B5/B6 统一为上述规则。）
- 当 ZCode 的 **Bash 现场能力尚未被独立验收放行**，或审批/闸门未就绪时：在**计划阶段**显式把该
  任务限定为 ZCode **只做读写**（Read/Glob/Grep/Edit/Write），把需要精确登记测试的原样命令**串行**
  交给现有 Qoder 执行并保留其结果；`approval_client_ready`/`controlled_pre_exec_gate_ready` 为
  `true` **只表示组件就绪，不等于现场已验证**，不得据此放行现场结论。
- **不得**为了绕过真实授权拒绝而更换入口或模型；拒绝照原样记录。需要人工审批/命令执行的现场验收，
  由主脑在独立隔离环境中按现有授权取得，不由文档改动冒充。
- GPT/Luna 的工程实施（写代码、跑命令）须用户**事先明确同意**；规划与独立验收可由主脑直接完成。
