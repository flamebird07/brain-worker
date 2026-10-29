# Qoder CLI 本机直连

**2026-10-29 当前权威（BW-SONUS-REENABLE-20261029-R3 + BW-QODERCN-TOKEN-MAX-DISABLE-20261028）**：`qoder:Sonus` 已恢复常态新派资格（仅 strong，优先级第一、独立 1 槽、无时段门，开关 `SONUS_TEMP_DISABLED=False`；覆盖 2026-10-28 R2 的全档临时禁用，R2 及更早恢复/禁用均只作历史）；与此同时用户要求在所有档位继续禁用 `qodercn:Qwen-3.8-Max`（Qoder CN／token plan／Qwen-3.8-Max）的新派，且不影响 CN 内置 `qodercn:Qwen3.8-Max`。两池都不是 retired/legacy。正式入口通过 `consume_for_entry` 统一容量门：Sonus 在 strong 有空即 claim、满即如实 `capacity_full`，绝不 fallback；CN 自定义 Max 无 claim 时拒绝新建占位，`mode_excluded` 明示用户禁用，sent=false、零 Popen/输出目录，不冒充额度、认证、时段、容量或永久退役。禁用前合法且仍 reserved 的精确 `--dispatch-claim` 可沿既有生命周期启动、结束，配置、模型映射、token、工作区、PID、权限、容量和时段门全部保留；running/unknown 不追杀、不假释放、不改派，例外不泛化。两池容量登记、历史、宿主递归排除不变，物理总容量 16；当前 strong/medium/weak 新派分别 5/4/10 池、6/5/13 槽（medium/weak 显式 Sonus 仍 `mode_excluded`）。浮窗：strong 空闲显示 Sonus 且第一，medium/weak 空闲隐藏；切档前遗留在 medium/weak 标「本档不参与」保留至真实终态。下文“CN 自定义 Max 恢复/三路 Max”及包含 CN 自定义 Max 新派资格的内容均只作历史。

使用已有官方 `@qoder-ai/qodercli` 运行时与官方保存的登录态。复制 `scripts/local-entry.json.example` 为同目录的 `local-entry.json`，填写本机 node 和 `qodercli.js` 的绝对路径；也可通过 `--config` 指定配置。真实配置不会进入 Git。

```text
python <技能目录>/scripts/qoder_direct.py --workspace <项目绝对路径> --prompt-file <UTF-8提示词文件> --output-dir <不存在的新证据目录> --stage <非空阶段编号> --model Qwen3.8-Max --tools Read
```

默认模型请求为 Qwen3.8-Max（本轮用户明确请求 Max，官方模型清单已确认 `Qwen3.8-Max` 为合法 `--model` 值；`Qwen3.8-Flash` 仍可显式选择，作为并发容量池的**溢出**组合）；默认不给工具权限。续接使用 `--resume-session-id`，内部传官方 `--resume`。不自动重装、登录或无限调度。

入口在**建输出目录、Popen 之前**必须消费或校验一个跨会话共享的**并发容量 claim**（`scripts/dispatch_pool.py`，见 [global-dispatch](global-dispatch.md)）：给了 `--dispatch-claim <token>` 就精确校验（task/runtime/model/workspace/prompt_sha256 任一漂移即拒），没给就走 `select_and_claim` 原子路由；非被选中组合 → `routing_required`/`sent=false`/退出 2、零证据目录、不计轮次。可选 `--task-id`、`--dispatch-store`（默认 `BRAIN_WORKER_DISPATCH_STORE` 或 `~/.brain-worker/dispatch-pool.sqlite3`）、`--dispatch-claim`。子进程真实结束立即释放名额；存活未知不释放、不被抢占。**容量并发 ≠ 额度冷却**。

BW-AVAILABILITY-20261009-B2：Qoder 入口以 `executor='qoder'` 消费/claim，并读**同一持久 ZCode availability**（可选 `--quota-store`，默认 `BRAIN_WORKER_QUOTA_STORE`；`--quota-routes`）——`dispatch_pool` 的资格过滤把被阻断的 ZCode 通道剔除，因此 **Qoder 绝不会被改道回不可用的 ZCode**；显式 Qoder 有空位时**不因历史 committed 计数被 1:1 改道**（见 [global-dispatch](global-dispatch.md) 的执行器约束与 availability 资格过滤）。

BW-MAX-WINDOW-20261010-S1（用户可理解的分配规则）：Qoder 国际内置 `Qwen3.8-Max` **只在北京时间
22:00（含）至次日 08:00（不含）作为主力**。这段时间以外：`AUTO` 新派工自动跳过国际 Max（改走
仍合格的 ZCode 主力，ZCode 也不可用/满时落到同级 `Qwen3.8-Flash`）；主脑显式把新任务 `reserve`
到国际 Max → `dispatch_pool` 如实拒绝 `main_force_window_closed`，**绝不偷偷换成别的模型**。
若 Max 曾在时段内预留、到启动时已跨过 08:00，`consume_for_entry` 会把该 reserved 占位真实释放为
`start_failed`（只释放本任务自己那份，不泄漏、不抢别人），已 `running`/`unknown` 的旧在途绝不误
释放。北京时间按固定 UTC+8 折算、与运行机器本地时区无关；时段门套三个 Qoder Max——两内置 `Qwen3.8-Max` 与 CN 自定义主力 `qodercn:Qwen-3.8-Max`（BW-CUSTOM-MAX-NIGHT-20261010-S1 起纳入同一 22:00 含至 08:00 不含时段门，白天 AUTO 跳过、显式 `reserve`/`consume` 如实拒绝、跨点真实释放本 reserved）；国内合计已由 BW-MAX-WINDOW-20261010-S2 升为 10；Flash/ZCode/其它自定义模型不受该时段门影响。详见 [global-dispatch](global-dispatch.md) §“牛马主力时段门”。

BW-AVAILABILITY-20261009-B4：受信任主脑明确授权的 **`executor='qoder'` + `--model` 组合硬约束**贯穿
`consume_for_entry`——含本阶段真实执行的 `Qwen3.8-Flash`——有空位即直接 claim 该精确组合，
**绝不因别的候选有空位而拒绝已授权组合**；ZCode availability 状态仅用于保证不会被绕回不可用的 ZCode，
不参与 Qoder 组合选择。默认 AUTO 主力优先/容量/1:1 与 Z2/Max2/Flash2 容量不变（并发 2/4 是 Skill
政策、非已核实的 Qoder 进程上限）。（注：此处 “Z2/Max2/Flash2” 是 B4 当时口径；已由 BW-POOL-SPLIT-20261010-S2/S3 定型为五池 Z2/国际内置 Max1/CN 内置 Max1/两区 Flash 各 2、合计 8，并由 BW-MAX-WINDOW-20261010-S2 加 CN 自定义主力 `qodercn:Qwen-3.8-Max`=2 升为六池、合计 10，当前口径见 [global-dispatch](global-dispatch.md) §11。）

BW-SONUS-REENABLE-20261029-R3（用户明确指令，**当前权威**）：`qoder:Sonus` 恢复常态新派资格，唯一权威开关 `scripts/dispatch_mode.py` 的 `SONUS_TEMP_DISABLED = False`，覆盖 BW-SONUS-TEMP-DISABLE-20261028-R2 的全档临时禁用（R2、BW-SONUS-REENABLE-20261028 与 BW-SONUS-TEMP-DISABLE-20261026 均保留为历史）。正式 Qoder 入口的 `--model Sonus` 恢复既有路径：strong 档有空即 claim、满即如实 `capacity_full`（sent=false），绝不 fallback 到 Flash/ZCode/任何其它模型；medium/weak 档显式 Sonus 继续 `mode_excluded`（sent=false、零新占位、绝不静默换模型）。Sonus 唯一权威开关为 `SONUS_TEMP_DISABLED=False`；恢复只影响之后的新派发，既有 reserved/running/unknown 不追杀、不改派、不假释放；R2 的禁用专项例外实现保留，只在开关再次为 True 时生效。Sonus 无任何时段门、不属于 Qwen Max（`_qoder_max_pool_key` 对 `Sonus` 恒返回 `None`）。strong AUTO 层序 Sonus=1 全档第一 > ZCode=2 > Claude=3 > 三路 Max=4；物理总容量仍 16（weak/medium 可新派容量仍 15），容量登记/优先级表/宿主递归排除/历史 attempt 全部保留。

BW-SONUS-TEMP-DISABLE-20261028-R2（用户明确指令，历史档案，已被 BW-SONUS-REENABLE-20261029-R3 恢复使用覆盖）：用户要求再次**暂时禁用** `qoder:Sonus` 参与任何新派发。国际 Qoder 入口的显式 `--model Sonus` 新请求（以及任何 AUTO/claim-due 新派发）在 strong/medium/weak **全部档位**一律如实 `mode_excluded`（sent=false、零新占位、绝不静默换模型、绝不 fallback），拒绝文案明确写「暂时禁用（用户指令）」——不是额度、认证、时段、容量或永久退役。唯一权威开关 `scripts/dispatch_mode.py` 的 `SONUS_TEMP_DISABLED = True`，可逆：改回 False 即恢复三档常态口径。strong AUTO 首选因此变为 ZCode=2（strong 实际 4 池/5 槽），其余四池优先级/无时段门不变。Sonus 的池字面量、独立 1 槽容量登记、优先级历史层号、宿主递归排除、物理总容量 16 与历史 attempt 全部保留；禁用前遗留的 reserved/running/unknown Sonus 不追杀、不改派、不假释放，浮窗保留整行、标「暂时禁用（用户指令）」到真实终态。遗留生命周期：禁用只针对**新派发**——禁用前已合法取得 claim、仍为 reserved 的旧 Sonus token 可继续经 `consume_for_entry`（正式入口精确 `--model Sonus`）沿既有完整校验（token/task/workspace/PID/容量/权限）启动/mark running；新 reserve/select/claim-due 一律零占位拒绝，该例外绝不泛化到其它 mode_excluded 池。下方恢复启用与旧禁用段均为历史档案。

BW-STRONG-SONUS-20261024（用户明确指令，常态口径；BW-SONUS-REENABLE-20261029-R3 起恢复生效）：国际 Qoder 入口维持 **strong 档专属**精确模型 `Sonus`（官方 CLI `--list-models` 只读核验的字面名，绝不改写/猜 ID）——正式派发用 `--model Sonus` 绑定/消费 `qoder:Sonus` 独立 1 槽 claim：strong 档有空即 claim、满即如实 `capacity_full`（sent=false），**绝不 fallback 到 Flash/ZCode/任何其它模型**；medium/weak 档显式 Sonus 一律 `mode_excluded`（sent=false、零新占位、绝不静默换模型）。Sonus 无任何时段门、**不属于** Qwen Max：不套 Max 北京夜间门，`_qoder_max_pool_key` 对 `Sonus` 恒返回 `None`（绝不吃 Max 限额标记/落标）。strong 档 AUTO 层序 Sonus=1 全档第一 > ZCode=2 > Claude=3 > 三路 Max=4。物理总容量升为 16（weak/medium 可新派容量仍 15）。详见 [global-dispatch](global-dispatch.md) 顶部三档覆盖段。

BW-SONUS-REENABLE-20261028（用户明确指令，历史档案，已被上方 BW-SONUS-TEMP-DISABLE-20261028-R2 再次禁用覆盖）：`qoder:Sonus` 恢复原有三档口径——strong 专属、层=1 全档第一、独立 1 槽、无时段限制；strong AUTO 在 Sonus 合格且有容量时首选 Sonus，显式 `--model Sonus` 有空即 claim、满即如实 `capacity_full`、绝不 fallback；medium/weak 显式 Sonus 一律如实 `mode_excluded`。唯一权威开关 `scripts/dispatch_mode.py` 的 `SONUS_TEMP_DISABLED = False`（True 时的禁用特殊分支只在 True 时生效）。物理总容量仍 16，容量登记/优先级表/宿主递归排除/历史 attempt 全部保留。下方 2026-10-26 禁用段为历史档案，不再覆盖当前策略。

BW-SONUS-TEMP-DISABLE-20261026（用户明确指令，历史档案，语义已被 BW-SONUS-TEMP-DISABLE-20261028-R2 按原文重新启用）：用户要求**暂时禁用** `qoder:Sonus` 参与任何新派发。国际 Qoder 入口的显式 `--model Sonus` 新请求（以及任何 AUTO/claim-due 新派发）在 strong/medium/weak **全部档位**一律如实 `mode_excluded`（sent=false、零新占位、绝不静默换模型、绝不 fallback），拒绝文案明确写「用户要求的临时禁用」——不是额度、认证、时段或永久退役。开关是 `scripts/dispatch_mode.py` 的 `SONUS_TEMP_DISABLED = True`，可逆：改回 False 即恢复原三档口径。Sonus 的池字面量、独立 1 槽容量登记、优先级历史层号、宿主递归排除、物理总容量 16 与历史 attempt 全部保留；禁用前遗留的 reserved/running/unknown Sonus 不追杀、不假释放，浮窗保留整行到真实终态。遗留生命周期（S2 收紧）：禁用只针对**新派发**——禁用前已合法取得 claim、仍为 reserved 的旧 Sonus token 可继续经 `consume_for_entry`（正式入口精确 `--model Sonus`）启动/mark running（Sonus 专项最小例外，置于精确 claim 校验之后，工作区/容量/PID/限额等门全部照旧）；新 reserve/select/claim-due 一律零占位拒绝，该例外绝不泛化到其它 mode_excluded 池。strong AUTO 首选因此变为 ZCode=2，其余五池优先级/无时段门不变。

## 统一契约与哈希口径

- 当前直连入口（Qoder/ZCode）调用前使用 `scripts/prompt_contract.py` 的完整九节契约，含精确标题、阶段/路径同一行、首末独占标记和结语。任务经 stdin，完整契约经官方 `--append-system-prompt`，两个通道各自留证。契约不能保证模型服从；原始响应照存，不合格仍保留 `bound=false`，不裁剪、不代写。
- 新调用的 `prompt_sha256` 与 dispatch-plan 均绑定原提示词文件的原始字节；另外保存实际任务文本、契约和完整发送通道的字节与哈希。换行转换口径显式记录，不把 JSON 文件哈希当 SDK prompt 哈希；Qoder 两通道不冒称一个后端合成载荷。留证边界只到本地 CLI/SDK 提交，不证明服务端处理后的文本。
- 不改提示词原文件或在途计划；旧调用记录沿用原入口版本的含义，升级后的新调用使用新计划。完整字段和留证位置见 [并行执行控制面](parallel-execution.md)。协议、报告绑定和独立业务验收分别报告，不能由本次传输回归推断真实业务恢复。

## 工具可见性与授权规则

`--tools` 与 `--allowed-tools` 承担不同职责，不能混用：

- `--tools` 控制可见的工具名（逗号分隔）。需要限定文件或命令权限时，同时显式提供允许规则；规则不能让未列入 `--tools` 的工具变得可用。
- `--allowed-tools RULE` 与 `--disallowed-tools RULE` 是可重复单值参数：每出现一次承载一条规则，规则原样传入，不按逗号拆分（Bash 规则内可以有逗号），不去空白，不做 shell 拼接；非字符串、空或仅空白的规则一律拒绝，不静默过滤。
- 官方匹配器把 `Edit` 与 `Write` 都按 Edit 类型文件规则查询。因此对文件写权限使用 `Edit(/具体路径)` 形式同时约束两者，例如 `Edit(/scripts/qoder_direct.py)`：前置单斜杠表示工作区根相对路径；`./scripts/...` 会被匹配器按无根文件名处理而不匹配。外部绝对路径规则需依据官方 `//` 语法另行验证，本文档不给未验证示例。`Bash(原样命令)` 已通过真实隔离验证；`Write(/path)` 独立规则未验证，不宣称已生效。根通配符 `Edit(/**)` 在本次验证未放行文件写入，使用逐文件规则。
- `--add-dir DIR` 也是可重复参数，用于把工作区外的目录纳入 Qoder 可见范围（例如让 Read 能访问外部文件）；它只扩展可见性，不自动授予编辑权限。
- 兼容回退：未提供 `--allowed-tools` 时，脚本把 `--tools` 的工具名逐项转成允许规则，这会允许该工具的广泛使用；需要限定范围的派工必须显式传规则，不能只传 `--tools`。显式规则完全替代回退，不追加裸工具权限。Python 调用 `build_argv(..., allowed_tools=[])` 可指定零允许规则；CLI 默认空工具也不生成允许规则。
- `--permission-mode` 固定为 `dont_ask`，绝不启用 `bypass_permissions`。

CN 专项（BW-CN-PERMISSION-STOP-20261015T155930Z-S1，只动 `--runtime qodercn` 分支）：CN 正式入口在
Popen 前额外拒绝裸/整工具通配 Edit/Write/NotebookEdit/Bash **allow** 规则（含 `--tools` 兼容回退
生成的同名规则），要求 `Edit(/<工作区相对具体文件>)` 与 `Bash(<登记原样命令>)`；并经官方
`--session-id` 只读增量监视本会话 jsonl（现场路径
`projects/<workspace-key>/<精确sessionId>.jsonl`；只认事件顶层/块内的结构化
`toolDenialKind=permission-rule`，或工具结果**起始处**直接构成的固定拒绝文案，允许可选 `Error:`
前缀，输出中间引用同一句不误停），真实审批拒绝即停本 child（详见
[Qoder CN 本机原生直连](qodercn-direct.md)）。国际路径的 argv 与通信一字不变；静态门只证明规则
形状，不声称真实权限成功。

遇到 Qoder 拒绝某动作时，执行 Agent 报告具体未执行的动作、被拒规则和证据，停止同类无效尝试；不重复询问已有授权，也不宣称修复成功——是否放行由主脑按隔离验证判断，不由执行 Agent 自报。扩展纪律（含认证失败分型、拒绝处置与隔离输出审查）见 [执行中断分类与接续](execution-interruption.md)。

### 精确文件任务：scope -> helper -> plan -> actual

Qoder 界面/任务 scope 里列出的文件只是**准备输入**，不自动等于 CLI 授权。客户只读取证确认
（Qoder 国际 1.1.64，`dont_ask`）：任务 scope 有 17 个文件，但 `plan.grants.edits/bash=[]`，
实际 `allowed_tools` 只有 Read/Glob/Grep/Edit/Write/Bash，2 次 Edit 被拒且同一目标未改。
这只能证明“空 grants + 裸工具名”不能被当成已落实的文件清单，不能据此断言所有 bare 规则
无效或官方 CLI 存在通用 bug。CN 已有精确规则路径经验；国际同版本隔离验证过
`Edit(/工作区根相对文件)`，Write 按 Edit 型规则处理。

主脑生成 Qoder 精确文件任务时固定走四步：

正式入口可以直接使用 `--task-scope <scope.json>` 完成这四步中的规则生成：入口在容量 claim 和 Popen 之前读取 scope、调用同一个 `qoder_permissions.build_permissions`，并把生成结果写入 `request.json.task_scope`。使用该参数时禁止混传 `--tools`、`--allowed-tools`、`--disallowed-tools` 或 `--add-dir`，避免 scope 与实际 argv 漂移。`stage_selection` 选中 Qoder/Qoder CN 时也会在回执写出同源 `qoder_permission_plan`，主脑可在正式派发前直接核对。

1. 从任务书/已确认范围写 scope JSON：`edits` 是工作区根相对的具体文件，`bash` 是完整
   原样命令，`read_dirs` 只放已授权只读目录。
2. 运行 `python <技能目录>/scripts/qoder_permissions.py --scope <scope.json>`。helper 始终
   可见 Read/Glob/Grep；有 edits 才可见 Edit+Write 且 allow 只生成 `Edit(/路径)`（不生成
   `Write(路径)`、裸 Edit/Write）；有 bash 才可见 Bash 且原样生成 `Bash(命令)`。
3. 用输出的 `visible_tools`/`allowed_tools`/`disallowed_tools`/`add_dirs`/`grants` 构造
   计划与实际 argv（`--tools` 用 `visible_tools` 逗号连接，`--allowed-tools` 逐项用
   `allowed_tools`，`--add-dir` 用 `add_dirs`）。
4. 主脑先逐项核对已授权 scope 与 helper 输出，再据输出构造 plan 与实际 argv；
   既有 `execution_control.preflight` 只比较 plan 与 actual 的权限集合，缺项或新增均拒绝。
   它不读取原始 scope，不能替代前一步核对；预检通过后才 Popen。

示例 scope（两个合成文件，仅示形）：

```json
{"edits": ["scripts\\模块\\我的 文件.py", "tests/test_x.py"],
 "bash": ["python -m unittest tests.test_x"], "read_dirs": []}
```

输出 `allowed_tools` 为 `["Read","Glob","Grep","Edit(/scripts/模块/我的 文件.py)","Edit(/tests/test_x.py)","Bash(python -m unittest tests.test_x)"]`，`visible_tools` 为 `["Read","Glob","Grep","Edit","Write","Bash"]`，`grants.edits/bash` 非空。

helper 只做字符串校验与组合：不启动进程、不执行命令、不派工、不写 DB/配置，也不修改计划
或 scope。`bash` 逐字保留，不追加 `cd`/`echo`/通配符或自行包装；命令完整性由任务书/主脑
核对，不把 `node --test` 等完整命令擅自判成不完整。Bash 规则中的括号按原样承载，本 helper
不实现 shell 解析器。既有 legacy 裸规则行为保留，不声称所有版本都无效；现场真实拒绝仍停止
受影响动作，不改 `dont_ask`、不绕过。

认证失败的边界（BW-INTERRUPTION-20261015-S1）：真实终态文案如 "Custom model authentication failed. Please check if your API Key is correctly configured and valid."（exit 1）属**认证失败**，与上文“明确限额落标与识别”中 `401`/`invalid api key`/`unauthorized` 同类**绝不落限额标记**；只确认 auth failed、保留原文与 exit code，不断言密钥失效，本任务内不重复同 `runtime:model`/config 版本，处理与接续规则见 [执行中断分类与接续](execution-interruption.md)。

```text
python <技能目录>/scripts/qoder_direct.py --workspace <项目绝对路径> --prompt-file <...> --output-dir <...> --stage <...> --tools Read,Edit,Write,Bash --allowed-tools Read --allowed-tools 'Edit(/scripts/qoder_direct.py)' --allowed-tools 'Bash(python tests/test_qoder_direct_permissions_offline.py)' --disallowed-tools 'Edit(/protected.txt)' --add-dir 'C:/Users/me/notes'
```

`request.json` 会记录 `allowed_tools` / `disallowed_tools` / `add_dirs` 有效规则与目录列表以及 `argv` 实际参数数组，供主脑审计。

输出包含 request.json、process.json、stdout.json、stderr.log、response.md、summary.json、report-state.json。保留原始信封与最终回复，核对协议终态、实际会话、阶段、路径、九节正文、续接与 SHA-256。先检查 protocol_success 与最终 bound，再独立验收业务。失败原件保留，不替执行者改写。

本机真实隔离验收已验证：多条允许规则、逐文件 Edit 和 Write、外部目录 Read、指定 Bash 离线命令、显式拒绝优先于允许、随机校验码回读、原文报告绑定。81 项直连离线检查覆盖配置、参数、协议、正文、哈希、续接及系统提示携带阶段/路径；另有 94 项既有台账回归通过。真实验证使用 Qoder CLI 1.1.64；其他版本需按实际工具回读确认。CLI 路由标识无法独立证明底层模型版本，零 token/credits 不证明免费。验证未涉及生产或其他业务项目。

## 九节兼容报告模板

本驱动机械校验此模板。阶段字段与路径字段必须填写提示词中的精确值，首尾标记各独占一行，只在边界处各出现一次，正文不得再次引用标记字符串；不加前后说明或围栏。主脑从本文件正确提取以下完整代码块，原样嵌入任务说明，不仅引用模板名。入口同时通过官方 `--append-system-prompt` 强调报告边界、字段同一行和阶段/路径约束；这不保证模型遵守，仍按原文拒绝不合格报告。执行者最终回复完整报告，由调用器原样落盘。

```text
WORKER_REPORT_START
阶段编号与执行方式：
实际项目绝对路径：
汇报时间与执行环境：

一、当前基线与授权
- 接手时已验证的文件/版本/运行状态：
- 本阶段获得的授权范围：
- 本阶段禁止事项：
- 交接来源（如更换 Agent）：

二、实际执行范围
- 实际执行的动作：
- 变更文件及范围：
- 未修改但检查过的关键文件或状态：

三、已验证事实
1. 事实：
   证据位置/命令/回读：
   验证环境：
2. 事实：
   证据位置/命令/回读：
   验证环境：

四、推断（必须与事实分开）
- 推断及依据：
- 仍未知的内容：

五、测试与验证
- 工作目录：
- 原样命令：
- 实际退出码：
- 关键结果：
- 未运行或未完成的验证及原因：

六、未完成项与剩余风险
- 未完成项：
- 失败项：
- 缺失证据：
- 剩余风险：

七、实际副作用与越界检查
- 实际副作用：
- 越界动作：
- 敏感信息处理：仅写脱敏摘要，不写 Cookie、密钥、令牌或账务原文。

八、本阶段状态
- 状态（只能选一项）：阶段完成 / 部分完成 / 待补验证 / 阻塞 / 异常
- 状态依据：
- 是否满足本阶段验收标准：是 / 否 / 证据不足

九、建议下一步（只提出建议，不执行）
- 建议：

本阶段汇报结束；等待主脑验收。
WORKER_REPORT_END
```

## 离线验证

```text
python tests/test_qoder_direct_offline.py
python tests/test_qoder_direct_binding_offline.py
python tests/test_qoder_direct_permissions_offline.py
```

测试使用本地假执行器，不需要 Qoder 登录或任何网络调用，不证明真实后端能力。真实派工与用量由独立主脑验收记录确认。

## 明确限额落标与识别（2026-10-10，BW-POOL-SPLIT-20261010-S5）

**只有三个 Qoder 主力 Max 池可承载明确限额标记**：`qoder:Qwen3.8-Max`、`qodercn:Qwen3.8-Max`、CN 自定义主力 `qodercn:Qwen-3.8-Max`（BW-MAX-WINDOW-20261010-S2 并入；三池各自 pool_key 独立落标、互不连带，绝不因自定义落标误伤两内置 Max）。ZCode 走 `quota_control` 的 `zcode_availability` 独立口径；任一 Flash、退休 DeepSeek、Luna 永不落标，绝不误挡 Flash、绝不动 ZCode 既有 availability 规则。

**BW-QWEN-MAX-LIMIT15-20261028（当前权威）**：两个 Qoder 内置 Max（`qoder:Qwen3.8-Max`、`qodercn:Qwen3.8-Max`）的明确限额标记改走 **900 秒（15 分钟）只读短窗**：从绑定 attempt 的真实 `ended_at_utc`（终态未落盘回退 `recorded_at_utc`）起 0<=age<900 秒内，AUTO、显式 reserve/select-and-claim、consume、claim-due 与浮窗同口径拒绝/显示“上轮限额”；恰满 900 秒自动消退并按现有优先级重新可选，不删 `main_force_limits` 证据、不宣称额度恢复、无需手动清标。原三池（ZCode/Claude/CodeBuddy deepseek）仍为 1800 秒；带短横线的 CN 自定义 `qodercn:Qwen-3.8-Max` 不进短窗（维持全档禁用现状），`qoder:Sonus` 不吃任何 Max 限额标记。离线回归见 `tests/test_qwen_max_limit15.py`。

- **失败识别只扫结构化载体**：`_explicit_quota_limit_hit(summary)` 只读 `summary['result_errors']` / `summary['result_errors_info']`（真实 CLI 原始 envelope 的失败字段），绝不扫报告正文、`response.md` 或请求历史消息；`ec.explicit_429(errors, errors_info)` 命中或 `_hard_quota_limit_text` 命中任一 marker 才算真限额。识别大小写不敏感。
- **硬限额 markers**（`_QUOTA_HARD_LIMIT_MARKERS`）：`credits exhausted / credits have been exhausted / out of credits / credit exhausted / credits depleted / insufficient credits / insufficient balance / no credits left / quota exhausted / credit usage limit / 积分用尽 / 积分不足 / 余额不足 / 额度用尽 / 额度不足`。用户真实失败文案 **"You've reached your credit usage limit."** 必识别；CN 内置 Max 实测终态文案 **"Your personal Credits have been exhausted. Please check your plan."** 必识别（“have been” 插在中间，不与旧的连续 `credits exhausted` 片段匹配，故单列 `credits have been exhausted`，大小写不敏感）。此识别**只针对该 runtime:model 的结构化终态错误**，绝不泛化成整个 `qodercn` executor，也绝不误标自定义 `qodercn:Qwen-3.8-Max`（自定义独立评估、互不连坐）。
- **不当限额的情况**（绝不误伤）：正文里 429 / 单独 `quota` 标签 / `permission_denials` 与 `No permission client configured for Bash` / `401` `invalid api key` / `unauthorized` / 成功终态 / 取消退出码 `4294967295`（`-1`）—— 分类器均返 `hit=False`。
- **`_qoder_max_pool_key(runtime, model)`**：内置 `model == 'Qwen3.8-Max'` 时按 runtime 返回 `qoder:Qwen3.8-Max`（`qoder`）或 `qodercn:Qwen3.8-Max`（`qodercn`）；BW-MAX-WINDOW-20261010-S2 新增——自定义 `model == 'Qwen-3.8-Max'` 且 `runtime == 'qodercn'` 返回 `qodercn:Qwen-3.8-Max`；国际 `qoder` 请求该自定义名、任一 Flash / GLM / DeepSeek / 其他自定义名一律 `None`，绝不偷偷落标。真实 `--model` 仍走既有私有 `model_ids` 映射解析，本入口从不读取或打印私有配置内容/真实 UUID。
- **落标时序**（真实失败终态）：`main()` 在 `try` 内解析 stdout → 若 `not protocol_success` 且命中硬限额且 `_qoder_max_pool_key` 非 None → `ev_sha = sha256(stdout_bytes).hexdigest()` → `dp.record_main_force_limit(dispatch_store, pk, task_id=…, attempt_token=claim_token, evidence_path=str(out/'stdout.json'), evidence_sha256=ev_sha)`；随后才在 `finally` 里 `_release_claim('finished', success=child.returncode==0)`。**先落标后释放**，杜绝槽释放后另一 chat 立刻重复提交；`record_main_force_limit` 抛错被 `except Exception` 兜住只落 `limit_rec={'recorded': False, 'error': str(exc)}`，**绝不阻断真实终态释放**（不泄漏锁）。
- **绑定校验（`record_main_force_limit` 侧）**：`dispatch_pool` 事务内回核 attempt 行存在、`pool_key` 与 `task_id` 与传入一致；`evidence_path` 指向真实文件且 `_file_sha256` 匹配 `evidence_sha256`。任一漂移或缺字段 → `recorded=False / drift=True` 拒写；同池重复落标只刷新证据/时间戳并清空旧的 `released_at/release_note`。
- **consume 前查限额**：`consume_for_entry` 在同一事务内若本 attempt 的 pool 已 limited → 只把**本 token** CAS `reserved→start_failed`（`capacity_released=True`），`sent=False / reason='main_force_limited'`；不动其他 task 的 reserved，不泄漏本次占位。
- **恢复只人工**：`release_main_force_limit(store, pk, note=…)` 只在用户明确额度恢复/重置后调用；本入口不查余额、不新增定时器/探针/评分/新服务/外部 API、不改 `quota_control.py`/`zcode_direct.py`、绝不靠成功旧日志自动清标记。

**离线回归**（每用例独立临时 SQLite，零网络，合成 stub）：
```text
python tests/test_qoder_cn_direct_offline.py
python tests/test_dispatch_pool.py
```
两个文件各自包含 `ExplicitQuotaLimitHitTests` 与 `MainForceLimitTests`，覆盖真实文案命中、非限额池拒、task/pool/evidence-sha/attempt-missing drift 全拒、幂等覆盖、`consume_for_entry` 只释放本次 reserved。
