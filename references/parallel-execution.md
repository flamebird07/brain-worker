# 并行执行控制面（BW-PARALLEL-UPGRADE）

> **当前运行时口径（2026-10-08 最新决定）：直连派发只保留 Qoder 与 ZCode。WorkBuddy 与
> CodeBuddy 改为 human-relay only。** `execution_control.preflight` 对 plan 或 actual 真实
> runtime 为 `codebuddy`/`workbuddy` 的计划一律明确拒绝（返回 `sent=false`、
> `manual_relay_only`，不派发）；生产 `codebuddy_direct.main` 亦在读配置/建输出/Popen 前固定
> 拒绝。下文 `runtime` 枚举与能力表中出现的 `codebuddy`、以及旧“三入口”派发示例，均保留为
> **历史/离线说明**：选择 CodeBuddy/WorkBuddy 时只生成完整可复制提示词、由人工交外部 Agent，
> 生成提示词不记成已派发。Qoder/ZCode 的实际并行、重叠、grants 精确比对与额度结算继续有效。

本文件描述 brain-worker 新增的任务级控制面：`scripts/execution_control.py`（纯标准库，
**无常驻调度服务**）与两个直连执行入口（Qoder/ZCode）新增的可选 `--dispatch-plan`。控制面做的是**主脑快照
预检**，不是原子跨进程锁：它比对“计划 JSON”与“实际入口选择/工具 grant/cwd/argv”，
拒绝缺 grant 或扩大权限，并在 Popen 之前退出 2。真正的进程串行化仍靠主脑看板与人工
纪律，不靠本模块抢锁；也不提供跨轮自动唤醒——并行推进依赖主脑在会话内主动派工、观察
与验收，本文档不声明任何常驻 scheduler 或自动调度能力。旧入口（不传 plan）继续兼容。

## 何时必须给 plan

- 新并行派工：每个任务都要给 `--dispatch-plan`，把 task_id、stage、runtime、model、
  workspace、cwd、grants、依赖和并发约定写死，便于逐任务隔离与回读。
- 单任务或旧流程：可不给 plan，入口行为不变；只追加 `summary.diagnostics` 与请求/摘要中
  的计划块（若有）。

## 计划 JSON schema（与实现字段一致）

顶层必填：`task_id`、`stage`、`runtime`、`model`、`workspace`、`cwd`、`prompt_sha256`、
`grants`。可选（缺省即下列默认）：`depends_on`/`shared_writes`（默认 `[]`）、
`tool_visibility`/`visible_tools`/`allowed_tools`/`disallowed_tools`（默认 `[]`）、
`max_concurrency`（默认 `1`）、`isolation`（默认 `"independent_workspace"`）、
`active_tasks`。`active_tasks` 是任务快照数组；**并行（`max_concurrency>1`）必须显式声明**
（哪怕确认没有在途也写 `[]`），缺失不等于“没有在途工作”，`validate_plan` 直接拒。

```jsonc
{
  "task_id": "T01",                       // 稳定不复用；绑定提示词/看板/报告
  "stage": "BW-PARALLEL-UPGRADE-20261006-01",  // 与 --stage 精确一致
  "runtime": "qoder",                     // qoder | codebuddy | zcode
  "model": "Qwen3.8-Flash",               // 与 --model 精确一致（请求值，不证明后端版本）
  "workspace": "C:\\abs\\ws-T01",          // 与 --workspace 解析后精确一致
  "cwd": "C:\\abs\\ws-T01",                // 实际工作目录，与入口 cwd 一致
  "prompt_sha256": "<64hex>",             // 下发的完整任务 prompt 的 SHA-256（锁定，不可改）
  "grants": {
    "edits": ["Edit(/scripts/qoder_direct.py)"],   // 逐文件 Edit 规则（原样字符串）
    "bash": ["Bash(python -m unittest discover -s tests)"],  // 原样命令 Bash 规则
    "read_dirs": ["C:\\abs\\read-only\\ref"]       // 外部只读目录（--add-dir）
  },
  "tool_visibility": ["Read", "Glob", "Grep"],  // bare 允许规则推出的整工具名（不含 Edit(/Bash()）
  "visible_tools": ["Read", "Edit"],      // 实际 --tools 精确集合（真实 CLI 可见性）
  "allowed_tools": ["Read", "Glob", "Grep", "Edit(/scripts/qoder_direct.py)"],  // 全量 allow 规则
  "disallowed_tools": [],                 // 全量 deny 规则
  "active_tasks": [                        // 并行必填：在途任务快照（主脑看板口径）
    {"task_id": "T00", "state": "executing", "workspace": "C:\\abs\\ws-T00",
     "prompt_sha256": "<64hex>", "writes": true, "shared_writes": []}
  ],
  "depends_on": ["T00"],                  // 依赖任务 ID；必须 completed 且 acceptance_result=passed
  "shared_writes": [],                    // 会写的共享服务/外部对象标识；非空即需隔离
  "max_concurrency": 4,                   // 显式并发上限；缺省 1
  "isolation": "independent_workspace"    // independent_workspace | shared
}
```

**四个列表口径的语义区别（不能互相冒充）**：`visible_tools` 是真实 `--tools` 的可见工具集
合；`tool_visibility` 是从 bare 允许规则（非 `Edit(...)`/`Bash(...)`）推出的整工具名，
Qoder/CodeBuddy 用它承载“看到哪些工具”，ZCode 只有这一层且计划须把它与 `visible_tools`
对齐口径；`allowed_tools`/`disallowed_tools` 是完整 allow/deny 规则原文集合。缺 Read 也要
被拒（不能只比“新增”而漏“缺失”）。真实 `--tools` 为空却需要 Read 的错误配置一律拒。

ZCode 的 `disallowed_tools` 应从**受信任本机入口** `build_tool_disallowlist(tools)` 派生（默认禁
全目录、仅显式名单放行），并与预检 actual 集合精确相等核对；runner 提交时把活注册表里**未被显式
放行**的项追加 deny，得到 `tool_disallowlist_effective`（= base deny ∪（`observed_tool_catalog` −
`allowed_tools`）），是**更严格的运行时边界**，单列回读，**不得说预检已经见过 live
catalog**。被派发前拒绝（`sent=false`/退出 2）不计模型轮次；仅计划口径缺漏且尚未派发、不扩权时
才可重建新计划，保留原计划与原拒绝，不在途改提示词或删 deny。可复用示例见
[ZCode 能力验收与现场放行](zcode-capability-acceptance.md)。

比对规则（`preflight`）：
- 身份绑定：`task_id/stage/runtime/model/workspace/cwd/prompt_sha256` 与实际逐项相等，
  任一不符即拒。`prompt_sha256` 不符表示在途提示词被改 → 拒绝改 prompt/重发。
- 能力表达：`runtime` 能力表见下；`edits`/`bash` 非空要求 `fine_grained`，`read_dirs` 非空
  要求 `read_dirs` 能力，否则拒绝。
- grant 精确集合：`grants.edits/bash/read_dirs` 与实际 argv 推出的同名集合**必须双向完全
  相等**（既无未计划新增、也无缺计划）。`bash` 逐字比对（原样命令）。
- 可见性/规则口径：`tool_visibility/visible_tools/allowed_tools/disallowed_tools` 四项
  同样**双向精确集合相等**；bare `Edit/Write/Bash` 全局授权若未计划也按扩大权限拒绝。
- `argv` 必须是先建好的 `list[str]`；出现 `shell=True` 一律拒。`argv` 以 `argv_sha256`
  记录供追溯，但**不宣称**全 argv 与计划逐字比对——精确授权由上面的 grants/可见性集合
  相等来保证。
- 并发/冲突（见“并行检查”）。

所有拒绝都发生在 Popen 之前，入口打印
`{"dispatch_plan_rejected": true, "sent": false, "exit_code": 2, "reasons": [...],
  "plan_hash": "..."}` 并返回 2，证据目录零创建。

## 原文件、契约与实际发送载荷

新调用的 `request.prompt_sha256` 与 dispatch-plan 的 `prompt_sha256` 统一绑定提示词文件原始字节，不对文件改换行。实际任务文本、报告契约和发送载荷另外取证，不能混用：

- Qoder：任务 stdin 与官方 append-system-prompt 契约各自保存实际 UTF-8 字节和哈希，不冒称已知服务端如何合并两通道。
- CodeBuddy：契约与任务拼接后的 stdin 字节留证。
- ZCode：JSON 的 `request.prompt` 解码文本所对应的 UTF-8 字节留证，SDK 消费该文本；JSON 文件本身的哈希不是 prompt 的哈希。

换行转换及契约注入写入请求元数据；磁盘原文件、任务文本和完整载荷的哈希可能不同，也可能相同。预检按原文件字节一致性核对，错误计划在创建证据目录和 Popen 前拒绝。

旧版 Qoder/ZCode 计划采用 LF 文本口径，旧 CodeBuddy 采用原文件字节口径；这属于历史版本记录。保留旧提示词、计划和原始事件，升级后仅为新调用生成新计划，不修订在途任务。历史 CRLF/LF 预检拒绝没有 CLI 派发，不计轮次。

本地 CLI/SDK 载荷证据不证明服务端最终文本，也不证明业务产出或模型恢复。实际留证文件与字段由各入口的请求元数据指明。

请求中的 `prompt_payload` 保存 `task_text_utf8_sha256`、`contract_sha256`、`sent_payload_sha256`、`readback_match`、`newline_conversion` 与 `channels`。Qoder 保存 `sent-payload-stdin.bin` 和 `sent-contract-system-prompt.txt`，CodeBuddy 保存 `sent-payload-stdin.bin`，ZCode 保存 `sent-task-payload.bin`。Qoder/CodeBuddy 任务保留原字节；ZCode CRLF/CR 归一 LF，不回写原文件。ZCode 纯预检的文件是计划载荷，`submit_channel=none`、`payload_role=planned_request_prompt`，不算实际提交。


## 各运行时真实能力（不要臆造官方参数）

| runtime | fine_grained（逐文件 Edit/原样 Bash） | read_dirs（外部只读目录） |
| --- | --- | --- |
| qoder | 是（`--allowed-tools Edit(...)`/`Bash(...)`、`--add-dir`） | 是 |
| codebuddy | 是（`--allowedTools` 规则；无外部只读目录参数） | 否 |
| zcode | **否**（只有整工具开关） | 否 |

ZCode 的工具名白名单**不能假称细粒度文件权限**。当计划要求 ZCode 表达不了的逐文件/逐命令
限制或外部只读目录时，`preflight` 直接拒绝，并要求：改用**隔离 workspace**、只按
`tool_visibility`（整工具开关）授权，或换用能表达该限制的运行入口。此「改隔离/换入口」
**只适用于尚未派发的能力表达不支持**，且须在**原授权内、不降格约束**（不能借机放宽 deny/可见性）；
**不能套用于实际授权拒绝**（运行时真跑被 `permission_rule_denied`/`permission_client_missing` 拦下时，
按原拒绝报告，不换入口绕过）。不得给 ZCode 编造 `--allowed-tools` 之类官方没有的参数。

## 六类 failure_types（`summary.diagnostics`）

> 本节六类是运行时 `execution_control.py` **已实现**的诊断分类，不因文档改动而扩大。主脑
> 验收时另用一套**独立失败分型**（缺客户端、规则拒绝、prelude 不匹配、未启动、输入漂移、
> 审批/执行取消超时、TLS/模型、报告格式、工程测试失败）分别归因，这些只是验收口径、引用上面
> 的原始事件/状态，**不宣称 diagnostics 已全部支持**，也不修改本节六分类。分层放行条件与
> 可信 deny 派生见 [ZCode 能力验收与现场放行](zcode-capability-acceptance.md)。

分类只读错误/拒绝/工具失败的**结构**，绝不扫描正常 prompt/report 正文里的 429 字样。
多类可共存；证据原文、错误码、reset 提示原样保留；平台/渠道/账号来源未知即不推断；
账单未知，Qoder 的 0 占位不等于免费，不加缓存 token，不假称身份确认；报告格式不合格
不自动算代码失败。

1. `quota_429`：观测到明确 `status==429`/`code==429`，**或**原始错误文本**开头**的独立
   `429`/`HTTP 429` 错误码（含带 reset 提示的 "429 …将在…重置"，与原 CodeBuddy 口径一致）。
   仅 `category=quota`、或 id/正文中间的 429 数字一律不算；报告正文里的 429 字样从不扫描。
2. `permission_rule_denied`：来自权限规则拒绝结构——CodeBuddy 的
   `permission_denials`/fail-closed 拒绝，以及 ZCode 原始事件
   `permission_resolved` 且 `payload.decision=="deny"` 且 reason **不是**缺客户端 marker。
3. `permission_client_missing`：从 ZCode 原始 `events.jsonl` 提取，即使 summary 的
   `permission_denials` 为 0。真实形状为 `type=="permission_resolved"` 且
   `payload.decision=="deny"` 且 `payload.reason` 含 `No permission client configured for
   Bash`，或 `type=="tool_call_result"` 且 `payload.isError` 且 `payload.error/result` 含该
   marker。缺客户端与规则拒绝分类分开；`type=="model_request"` 携带的历史/prompt/report
   正文从不参与分类（含其中的 429/缺 client 文案也不误报）。
4. `protocol_parse_failure`：信封/流结构解析失败（非“有效失败信封”）。CodeBuddy 仅因权限
   fail-closed 产生的 `parse_errors` **不**算语法失败；反之无效 JSON 行 + 合法失败信封仍
   保留 `protocol_parse_failure`。
5. `model_execution_failure`：**只**用于明确的模型/传输执行失败或剩余未分型的执行错误，
   已知 quota/permission/parse 来源不泛化为模型差；与上面的分型独立共存，原始码/证据/
   reset 提示不丢。
6. `test_failure`：**只**来自已登记测试执行器的真实退出码，不从报告字句猜。

## 已登记测试执行器（`execution_control run-test`）

`run-test` 只跑注册表（`{"runs": {"<name>": {...}}}`）里登记的精确 `argv`、`cwd`、
`inputs`、`env`；日志固定写到 `workspace/handoff/logs/<name>/`；证据写到
`<evidence_root>/<name>/<fingerprint>/`。规则：

- 禁止 `shell=True`、禁止猜命令、禁止覆盖旧证据（同 fingerprint 目录已存在且非 reuse
  即拒绝）；注册 spec 的 `inputs` 必须声明**完整且非空**的 code/test/fixture 文件。
- fingerprint = 全部输入文件 bytes 的 SHA-256 + argv + cwd + 环境口径。继承的 env 只在
  证据里保存其 **hash**（`env_sha256`，不泄明文凭据），并绑定 Python/运行环境必要版本
  （`runtime_version`）；环境或版本变化都算不同指纹。
- 越界检查用 **realpath**（`os.path.realpath`）解析 symlink/junction：`cwd`、`inputs`、
  `evidence_root`、日志目录任一逃出**真实解析后的 `--workspace`** 一律拒绝，不 spawn；
  日志固定写到 `workspace/handoff/logs/<name>/`，不落父目录。
- reuse 严格条件：相同 fingerprint **且**上次退出 0 **且** stdout/stderr **两份日志都存在**
  且 SHA 回读一致 **且** `argv/cwd/inputs 哈希/env_sha256/runtime_version` 元数据全部吻合，
  且输入在该次执行前后未改变。子进程继承环境仅保存哈希，不保存变量值。
  损坏 JSON、空证据、丢任一日志、测试期间输入变动 → 都不 reuse，新建 attempt（带序号，
  绝不覆盖旧原件）。
- 返回真正退出码/stdout/stderr 与 `evidence.json`；`failure_types` 仅在退出码非 0 时含
  `test_failure`。无 pytest 依赖，测试用 `unittest`。

注册表示例：

```jsonc
{
  "runs": {
    "unittest": {
      "argv": ["C:\\Python312\\python.exe", "-m", "unittest", "discover", "-s", "tests", "-p", "test_execution_control.py"],
      "cwd": "C:\\abs\\ws",
      "inputs": ["C:\\abs\\ws\\scripts\\execution_control.py",
                 "C:\\abs\\ws\\scripts\\qoder_direct.py",
                 "C:\\abs\\ws\\tests\\stub_qodercli.py",
                 "C:\\abs\\ws\\tests\\test_execution_control.py"],
      "env": {"PYTHONIOENCODING": "utf-8"}
    }
  }
}
```

CLI：

```text
python scripts/execution_control.py run-test --registry <reg.json> --name unittest \
  --workspace <abs ws> --evidence-root <abs evidence dir>
python scripts/execution_control.py preflight --plan <plan.json> --actual <actual.json> \
  [--active-tasks <tasks.json>] [--max-concurrency 4]
```

`preflight` 退出 0 表示可派工；退出 2 表示拒绝（含原因与 plan_hash），零 Popen。

## 并行检查与隔离

任务**五状态**：`pending / executing / blocked / awaiting_acceptance / completed`（不再有
第六 `verified` 状态）。其中 `executing / awaiting_acceptance / blocked` 是占用 task_id 与
并发槽位、门禁不能被绕过的“在途”态。两个真实直连入口（Qoder/ZCode；CodeBuddy/WorkBuddy 已
退役为 human-relay，仅提示词人工中继，不进池）接受可选 `--active-tasks <json>`（数组
`{task_id,state,...}`）或在 plan 里带 `active_tasks`，都传给 `ec.preflight`；并行 plan 缺
`active_tasks` 会被 `validate_plan` 拒绝，**缺失不当作“没有在途工作”**。

`ec.check_parallel` 的快照并发闸只统计**国内 qoder/zcode** 的活进程槽（`executing/blocked`）：
Luna 救援通道无上限、绝不占用国内六名额，已退休的 CodeBuddy/WorkBuddy 直连与其它非国内池同样
排除——旧的全局快照 max 绝不把 Luna/旧 CB/其它池当成统一容量闸（否则会出现 domestic=0、6 个
Luna executing 却误报“concurrency limit 6 exceeded”）。真实国内容量上限另由 `dispatch_pool`
原子池权威强制；同 task/同 workspace/共享写/依赖的快照保护仍独立维持，不因容量分离而放宽。

- 允许有价值的并行，不凑并发数量：并发上限服务于独立成果的真实节省；没有独立可验收
  成果就不并行，不为凑数拆任务。
- 相同文件、不同 workspace 的**独立副本**可以并行；合并时逐文件比对，不静默覆盖。
- `shared_writes` 非空且与在途任务重叠、或同一 workspace 有写入 → 不能隔离即拒绝，须串行。
- 依赖 `depends_on`：只有状态 `completed` **且** `acceptance_result=="passed"` 才放行；
  `failed/cancelled/未知` 验收结果、或非 completed 的依赖一律拒（无 `verified` 一说）。
- 同 `task_id` 在途（`executing/awaiting_acceptance/blocked`）：核验**看板记录 hash 与实际
  下发 hash** 双向一致，改 prompt 或重发（即便 prompt 相同）一律拒绝，门禁不可被
  `awaiting_acceptance`/`blocked` 绕过。
- 并发默认 1，可显式 `max_concurrency`（本演练验证 4）。这是主脑快照预检，不是跨进程原子锁。

## authorized fallback 边界

- 失败任务的已授权 fallback（换入口/换模型）只影响**该失败任务**，且必须先记录失败证据
  （对应 failure_type 与原始结构）后才接管；其它在途任务的提示词**不被改动**，独立任务
  也不被单项 429/权限失败冻结——照常继续执行、观察与验收。
- **换入口/换模型不是绕过真实授权拒绝的手段**：一次 `permission_rule_denied` /
  `permission_client_missing` 不猜变体重试，也不借「换个入口」把同一越权动作再发一遍；
  拒绝照原样记录，交主脑决策，不重复索要已有授权。
- ZCode 的 **Bash 现场能力未经验收放行时**，在计划阶段就把该任务限定为 ZCode 只做读写
  （Read/Glob/Grep/Edit/Write，注意 Edit/Write 是写入不是「只读」），把需要跑原样命令的登记测试
  **串行**交给现有 Qoder（沿用其唯一模型与入口），保留其结果；**不把「Bash 未验证」默认转成主脑
  代跑**。执行 Agent 仍不伪称已运行、不用管道/`cd`/改参数绕过规则（否则触发 `permission_rule_denied`）。

## 自然派工案例（策略示例，非真实调用记录）

用户只给业务目标（例：“把这两块资料核对并修成可发布版本”），不点名执行者、不说并行、
不选模型时，主脑按以下顺序决策：

1. **划分独立成果**：按可独立验收的成果切任务（例：“资料 A 核对与修复”“资料 B 核对与
   修复”“合并与发布准备”）。前两项相互独立；第三项依赖前两项验收通过。
2. **读取台账与预检状态**：派工前逐项读取能力台账（评分、步长、限制、身份确认状态）与
   三个直连档案的就绪状态；未就绪的入口不进入候选，不因配置存在就认定可用。简单、低
   成本的规划或独立验收判断主脑可直接做，但**工程实施（写代码/跑命令）须用户事先明确同意**，
   不把「单文件小修」这类工程默认自动交主脑代做。
3. **选择执行者**：本机未另行指定时 **ZCode 优先**（用户已授权直连时）；**Qoder 沿用现有唯一
   模型及入口兜底**，不覆盖配置、不新增或替换模型。两个并行任务使用相互隔离的工作副本。没有
   就绪直连入口时如实告知缺项并按授权改走 `human-relay`，不假调用；个人版免费积分目标保留，
   企业 CLI 或成本未知调用不冒称免费。
4. **每入口一份 plan**：两个并行任务各带 `--dispatch-plan`，逐次刷新 `active_tasks`
   快照：首次确实无在途时用空数组，后发任务登记先发任务；不把尚未派发者写成执行中。
   `prompt_sha256` 统一按原文件字节预计算。技能要求主脑拒绝无计划的新并行派工；旧入口仍兼容无计划调用，不能假称适配器能自行识别未声明的并行。
5. **单项失败兜底**：一项出现 429/普通**已终态、非授权拒绝**的失败时，先按原件与 failure_types
   验收失败证据，再**依既有授权顺序**仅对该任务兜底；另一独立任务继续，不因单项失败冻结整批。
   若是**授权校验/自动审批/环境限制**拦下（`permission_rule_denied`/`permission_client_missing`），
   **按原拒绝报告、不换入口或模型绕过**，只停该任务的无效尝试交主脑决策。
6. **合并门禁**：两份原始报告保留，成果各自完成独立验收、报告绑定另行说明后，才在合并目录做逐文件比对与必要合并检查；
   合并结果由主脑独立验收，不静默覆盖任何一方副本。依赖任务在依赖
   `completed 且 acceptance_result=passed` 后才派发；看板/控制面是快照不是原子锁，验收
   前在途任务的提示词不改、不重复派发。

本案例是行为规范说明：其行为验证由主脑在隔离示例中进行，任何 mock/演练通过都不写成
真实调用成功，也不计入真实派工轮次。

## fixture 与持久 contract 的顺序

- 先锁定**实际注册入口**与其持久 contract（真实生命周期口径），再设计 fixture：
  按适用生命周期使用**真 SQLite**（本地/临时库）或**有状态远端 fake**；不给所有任务强套
  一套清单。控制面不为业务做生命周期模拟，只覆盖真实 brain-worker 入口。
- 累计全派工 diff、报告模板与任务书放 `handoff/`（上下文压缩后也从这里恢复）。
- 格式待验时保留原件并独立验收业务，不重跑已验证代码；必要格式补证集中到必要业务补修，不单独为格式再派一轮。

## 成本与身份边界

六分型只描述失败类别与保留的原始证据，不代表成本或身份结论：账单未知不推断；
Qoder 的 `total_credits=0` 是占位、不叫免费；缓存 token 不额外累加；`qfmodel`/`gfmodel`
路由不证明底层 Qwen 版本，模型请求值 ≠ 已确认身份。评分沿用共享台账，阶段一次评分
idempotent；主脑自身配置/预检失败单独归因，不计入执行 Agent 阶段样本。
