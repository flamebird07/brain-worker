# Claude Code / deepseek-flash 直连入口（claude-direct）

> **当前权威覆盖：手动三档（BW-MANUAL-THREE-MODE-DOCS-20261023，2026-10-23 用户明确指令，
> 覆盖本文件旧「Claude 全天可派」口径）：** 派发档位是全宿主共享的手动三档
> strong／medium／weak，**默认 weak**，唯一策略来源 `scripts/dispatch_mode.py`
> （`~/.brain-worker/dispatch-mode.json`，`BRAIN_WORKER_DISPATCH_MODE_FILE` 可覆盖；切档原子
> 保存、**只影响新派发**，在途任务不中断；缺文件/畸形/非法/不可读一律**安全回落 weak**）。
> 对本池 `claude:deepseek-flash`（容量 2）的含义：**strong 档无时段门**（五个可派池之一，
> 层序 ZCode > Claude > 三 Max）；**medium 档恢复既有工作日高峰门**（工作日 09:00–12:00 /
> 14:00–18:00 不新派，午休/夜间/周末/法定假日放行；层序 三 Max > Claude > ZCode）；**weak 档
> （默认）同样恢复该高峰门**（层序中 Claude=3）。时段判定复用 `scripts/deepseek_offpeak.py`
> 纯函数，其**加载/运行异常一律 fail-closed**（当作窗口关闭），绝不 fail-open；AUTO 候选、
> 显式 `executor=claude`、预留后 `consume`、`claim_due`、`stage_selection` 与浮窗
> `window_blocked` 全部复用同一判定。**时段门之外的语义全部照旧**：1800 秒真实限额短窗、
> 容量 2、总容量守卫、同工作区单写、权限门、轮次预算与连接重试不受档位影响。下文旧
> 「Claude 全天可派」「工作日高峰拒派」等段落均按此归档理解：前者已被三档覆盖，后者在
> medium/weak 仍是现行规则。

> **当前轮次预算覆盖（BW-CLAUDE-TURN-BUDGET-20261021）：** 普通工程默认 120 轮，简单非工程默认 40；任务可显式覆盖，证据记录 `max_turns_configured` 与 `max_turns_source`。一次性 system prompt 指令预留最后 10 轮用于 diff、关键测试和九节报告。`error_max_turns` 是独立的 `turn_budget_exhausted`，不属于 429/认证/权限/网络，也不触发连接重试；有成功工具回执或显式受控文件哈希变化时生成事实型 `continuation.json`，零进展不伪造接续成果。下文旧“40 轮”仅作历史配置档案。

> 本文件描述 brain-worker 的主力执行器 `claude`（容量 2；客户 DeepSeek Anthropic 兼容端点，
> 精确模型 `deepseek-flash`）。实现以 `scripts/claude_direct.py` 与 `scripts/dispatch_pool.py`
> 为准（Claude 时段门经 `scripts/dispatch_mode.py` 复用 `scripts/deepseek_offpeak.py` 纯时间
> 判定，仅 medium/weak 档调用、strong 档不套门；依赖异常 fail-closed），本文件为
> 口径说明；二者不一致时以代码为准并回修文档。
> **离线回归只证协议/绑定/容量/限额语义；真实调用与端到端验收由根脑（root）在本机完成，
> worker 不调 API。官方 Node CLI 2.1.76 真实 Read/Write 已成功（独立事实，见 §9），
> native 2.1.220/296 连接失败单列，不与 Node 事实混写。**

## 0. 默认调用（最简单，直接 bypass）

本机私有 `claude-entry.local.json` 保持 `permission_mode: "bypassPermissions"`、`tools`
为 `Read/Glob/Grep/Edit/Write/Bash`（root 本机同步），即可直接派工；**不需要**
`--required-tools`、`--write-file` 或逐文件登记命令：

```bash
python scripts/claude_direct.py \
  --workspace "C:/absolute/workspace" \
  --prompt-file "C:/absolute/task.md" \
  --output-dir "C:/absolute/new-evidence-dir" \
  --stage "BW-STAGE-ID" \
  --task-id "task-id"
```

真实 argv 会带 `--permission-mode bypassPermissions` 与
`--dangerously-skip-permissions`。回执如实记录 `permission_mode=bypassPermissions`、
`enforcement=cli_bypass`、`exact_file_authorization=false`：精确文件授权预检没有执行，
不能据此宣称存在精确文件保护。任务范围仍由提示词与独立验收界定，**不是沙箱**。
`dontAsk` 仅在用户明确要求或确需严格边界并记录理由时显式选择（见 §4）；主脑不得从旧任务配置机械继承 dontAsk/逐文件清单，覆盖用户最新默认直接模式。新轮启动前核对实际配置与 argv，不能把静态 profile 通过当作 CLI 已可编辑。

## 1. 容量池：AUTO 可选主力槽 `claude`（容量 2；BW-CLAUDE-CAPACITY2 由 1 扩为 2，原 10 槽一项未减）

- runtime `claude`，池 `claude:deepseek-flash`，`CLAUDE_CAPACITY = 2`（BW-CLAUDE-CAPACITY2 由 1 增至 2），
  `MAIN_FORCE_CAPACITY[CLAUDE_POOL_KEY] = 2`。
- **已登记入主力体系**：`claude:deepseek-flash` 在 `MAIN_FORCE_POOLS`/`MAIN_FORCE_KEYS`/
  `MAIN_FORCE_CAPACITY` 内，`DOMESTIC_TOTAL_CAPACITY = 15`（含 ZCode 恢复的 1 槽与两个
  CodeBuddy 精确模型各 1 槽，由 `MAIN_FORCE+OVERFLOW` 求和自动得出）；
  `_domestic_active` 统计所有非 Luna 在途 attempt，**claude 在途计入国内总额**。
- **独立组 claude**：committed 由 attempts 真实提交历史
  `_count_committed_claude` 统计，**绝不污染** committed_zcode/committed_qoder；
  当前口径（BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023）：两个 CN Max（层=1）>
  `zcode:GLM-5.3`（层=2）> claude（层=3，**全天可派**）> `qoder:Qwen3.8-Max`（层=4，全天）>
  `codebuddy:deepseek-v4.1-flash`（层=5）；旧"三 Qoder Max 同层""codebuddy 层=0"与跨层
  1:1 属历史、已被覆盖，历史 committed 计数照旧保留记账但不再参与跨层竞争。
- `AUTO` **可选** claude：AUTO 的 claude 入口请求**不绕过统一选择**，经同一权威 `_select_main_force`
  优先级排序（先过滤真实空位与限额，再按优先级层选择、同层内在途平衡；claude 无时段门）；
  显式 `executor='claude'` 保持精确组合、绝不跨到其它池或回退模型。
- `_domestic_policy` claude 分支（S4 收口 + BW-CLAUDE-CAPACITY2 + BW-CODEBUDDY-MAIN-S3）：`executor_conflict` → **总 15 守卫先于一切**
  （迁移债务满 15 时 `full`，**绝不派第 16 个**）→ 时段门判定 `_max_window_blocks`
  （BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023 起对 claude 恒为 False，保留调用以维持单一权威路径）→
  限额 `full` → 有空位 `claim` → 满 `full`。
- 未配置（缺 local/example 配置或 executable）时**不得读作就绪**：`build_argv` 缺
  `executable` 直接 `ValueError`（零 Popen）。旧 runs/unknown 记录一律保留、不迁移、不清库。

## 2. 时段门（BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023：已对 claude 整体解除）

**当前权威：claude 无派发时段门，全天可派。** `dispatch_pool._max_window_blocks` 对
`claude:deepseek-flash` 恒返回 False，生产派发不再导入 `deepseek_offpeak`；本节余下内容是该纯
函数的历史口径档案（保留用于读取旧证据与运行其独立测试），**不再决定 Claude 是否可派**。

`scripts/deepseek_offpeak.py`（纯标准库，历史兼容保留）：

- `BEIJING_UTC_OFFSET = 8h`；naive 输入按 UTC 解释，aware 输入先归一到 UTC 再 +8。
- `PEAK_INTERVALS = ((540, 720), (840, 1080))` —— 09:00(含)–12:00(不含)、14:00(含)–18:00(不含) 为峰。
- `WEEKEND_WEEKDAYS = (5, 6)`；`HOLIDAY_RANGES` 硬编码 2026：Jan1-3、Feb15-23、Apr4-6、May1-5、
  Jun19-21、Sep25-27、Oct1-7。
- `classify_dispatch(now)` → `{allow, category, reason}`，category ∈
  `weekend / holiday / weekday_offpeak / weekday_peak / weekday_peak_unknown_year`。
- **未知年份保守**：工作日峰值一律关闭（`weekday_peak_unknown_year`），非峰与周末仍放行；
  绝不把调休上班周六当峰值（周末优先放行）。
- 历史行为（已作废）：峰期内曾不新派、offpeak 缺失时 fail-closed、跨峰 `consume_for_entry`
  只释放自身 token。现在这些判定**不再参与** claude 可派性；跨峰 consume 正常放行。

## 3. 明确限额标记（受限池）

- `QUOTA_LIMITABLE_POOLS` 含 `claude:deepseek-flash`，复用既有 `main_force_limits` 最小布尔标记。
- 只认**同一条真实错误条目内绑定的结构化 429 / 硬上限**才自动跳过；不查余额、不做额度系统、
  不引定时器/评分/新服务/外部 API。取消退出码（`4294967295` / `-1`）与 SIG **绝不判为 429**。
- 落标须绑定真实 attempt（`task_id`+`attempt_token`）与真实错误证据路径/sha256，任一漂移全拒。
- **无证据绝不自动清标**：仅由用户显式额度恢复/重置后经 `release_main_force_limit` 人工解除。

## 4. CLI 构造（`scripts/claude_direct.py`，钉死不接受偏离）

- `build_argv`：`-p --output-format stream-json --verbose --model deepseek-flash
  --permission-mode bypassPermissions --dangerously-skip-permissions`（默认；真实启用
  bypass 的是后者，`--allow-dangerously-skip-permissions` 不算已启用，入口不使用），
  逐项 `--tools` / `--allowedTools` / `--disallowedTools` /
  `--add-dir`、`--setting-sources`、`--settings`（隔离环境，
  **绝不覆盖 `~/.claude`**）、`--max-turns`；`--append-system-prompt` 附带
  `prompt_contract.build_contract` 完整九节契约。任务原文经 `stdin`（独立写线程写入
  并关闭；stdout 由独立 drain 线程**逐行记录原始行**并送入队列，主线程只消费队列判断
  首次拒绝，已收到的成功/错误原始行一律不裁剪；stderr 并发 drain，防管道死锁），绝不
  拼进 argv。首次权限拒绝后主线程**不再阻塞等待下一行**，只做有界 drain 与有界
  `wait(timeout)`；reader 未到 EOF 时保留已收快照并标 `raw_stdout_partial`，terminate
  失败/进程仍活 → `exit_code=None`（unknown），不假释放。回执区分 `stop_requested`、
  `terminated_by_worker` 与原始流中实际观察到的 `observed_followup_tool_use`，不掩盖竞态。
- **拒绝式钉死（S4 保留）**：`--bare` 出现即 `ValueError`（官方 Node CLI 2.1.76
  不支持该开关）；权限模式不属于 `bypassPermissions` / `dontAsk`（例如 `acceptEdits`）
  即 `ValueError`；模型 ≠ `deepseek-flash` 即 `ValueError`；`base_url` ≠
  `https://api.deepseek.com/anthropic` 即 `ValueError`——绝不静默改道或回退。
- **默认 bypass 的权限口径（BW-CLAUDE-SIMPLE-20261018-S1）**：普通工程入口**不要求**
  `required_tools`/`write_files`，**不执行**精确路径/逐文件授权预检；`--tools` 实参仍是
  工具可见性来源（本机配置为 Read/Glob/Grep/Edit/Write/Bash），`--allowedTools` 默认
  为空、不附逐文件规则。回执写入 `mode=bypassPermissions`、`enforcement=cli_bypass`、
  `exact_file_authorization=false`，不得伪装精确文件保护有效。真实宿主的权限拒绝/系统
  拒绝仍如实停止，不承诺 bypass 可绕过宿主系统。任务范围是提示词范围 + 独立验收，
  不是沙箱。
- **仅显式 `dontAsk` 保留旧严格需求（BW-CLAUDE-PATH-20261018-S2 原样）**：
  `run(..., required_tools=[...], write_files=[...])` 与 CLI
  `--required-tools Read,Edit,Write`（逗号分隔）+ 可重复 `--write-file` 是该模式下的
  需求契约。工程任务必须声明 `required_tools`（至少 `Read`；写任务同时声明
  `Edit`/`Write` 与具体 `write_files`），非工程纯文本显式 `--not-engineering-task` 才
  保留 `skipped=true`。若 cfg 实际带可见且被允许的 `Edit`/`Write` 规则，该标志不能绕过
  路径预检。核对在凭据读取/容量 consume/Popen 之前：只读工具必须（a）在 `--tools`
  可见、（b）被 `--allowedTools` 授权、（c）未被 `--disallowedTools` 拒绝；显式拒绝
  优先于允许。缺少声明/缺 Read/缺写范围/范围外授权 → 零 Popen、零凭据读取、零容量占位
  （`required_tools_missing` / `required_tools_missing_read` /
  `required_tools_not_granted`）。
- **确定性精确写 profile（`scripts/claude_permissions.py`）**：由 workspace + 具体文件
  清单生成且只接受 `Edit(./规范正斜杠相对文件)` / `Write(./同文件)`。`C:/...`、`/...`、
  裸 `Edit`/`Write`、通配符、`..` 逃逸、盘符不一致、目录目标、范围外兄弟文件、
  写授权超出清单、未声明写工具却携带的 `Edit(...)`/`Write(...)` 一律明确拒绝，
  **不猜匹配、不静默改写外来 cfg**。输入尾分隔符/重复分隔符明确拒绝（`newdir/` 不会被
  剥成新文件）；Windows 歧义/非法段（冒号 ADS、保留设备名如 `NUL`、尾空格/点）拒绝。
  现有文件和新文件都按最近存在祖先 `resolve` 核对（符号链接越界拒绝；平台不支持
  符号链接时该平台不宣称已核验，绝不据此放宽）。真实 host 解析失败（如
  `PermissionError`）诚实拒绝，不做 best-effort 放行。大小写和反斜杠归一化在 Windows
  profile 上与平台一致，纯词法转换可在任一宿主离线测试。
  该检查只在**显式 dontAsk** 下执行，是**配置一致性检查，不是 OS 沙箱**；真实 Claude CLI
  的精确授权仍须 root 用正反例现场验证。默认 bypass 不执行该检查，回执明确
  `exact_file_authorization=false`。
- 畸形规则（`Edit(/file.py))`、`Edit()`、`(/x)`、`Edit(...`）绝不按已授权处理；无法
  解释且指向显式需求工具的拒绝规则按 `invalid_rules` 保守拒绝。声明成功后，标准化
  `required_tools`/`write_files`、期望规则与最小 `permission_preflight` 结果写入
  `request.json` 与 `run` 返回（不含凭据）。默认 bypass 同时写入
  `permission_mode`/`permission_enforcement`/`exact_file_authorization`。
- `_PINNED_MODEL_ENVS` 把 default opus/sonnet/haiku/small-fast/subagent 全部钉成
  `deepseek-flash`，**没有任何模型回退**；`CLAUDE_CODE_SIMPLE` **绝不设置**，
  `build_subprocess_env` 对任何试图覆写被清除凭据 env（含 `CLAUDE_CODE_SIMPLE`）的配置项
  直接 `ValueError`，并在注入前二次清除继承值。

## 5. 凭据与配置边界

- 私有配置 `scripts/claude-entry.local.json`（DPAPI 指针 / token env）**由 root 本机填写、
  worker 工具不读**；`.gitignore` 已加入该文件；仓库只提供并只提交
  `scripts/claude-entry.json.example`（占位符、无密钥；示例结构为 `node.exe` +
  `launcher_args` 指向绝对 `cli.js`，`credential_source.path` 是 **DPAPI 密文文件指针**
  而非 pwsh 本身）。`load_entry_config` 默认加载受信任宿主的该私有路径；对名为
  `claude-entry.local.json` 的受保护路径抛 `PermissionError`（worker 侧文件名拒绝）；
  合法入口配置须为显式绝对路径且非受保护名（如离线注入的临时配置）。
- `resolve_token` 两种来源：`mode='token_env'`（离线 stub 明确环境变量，默认
  `ANTHROPIC_API_KEY`）、`mode='credential_file'`（Windows DPAPI `ConvertFrom-SecureString`
  密文文件，经官方宿主 shell 无日志解密；默认 `powershell`，本机 `powershell.exe`
  因模块加载冲突不可用时由 root 改指 `pwsh.exe`）。缺失/空一律拒绝启动（零 Popen），
  绝不猜或占位。
- token 明文**只进子进程 env**（`ANTHROPIC_API_KEY`），绝不进
  argv/request/异常/证据；`write_evidence` 前 `_redact` 兜底抹除任何 token 明文。

## 6. 生命周期（`run`，S4 收口）

- Popen **之前**完成全部预检（argv 构造、**prompt SHA 从文件真实回读**与传入值比对、
  dispatch plan 精确预检、凭据解析；显式 `dontAsk` 才另有 `required_tools`/`write_files`
  与精确写 profile 核对）：任一失败 ⇒ **零 Popen、零占位泄漏**；dontAsk 的需求核对先于
  凭据读取与容量 consume。默认 bypass 不跑精确写 profile，回执如实标
  `exact_file_authorization=false`。build_argv / plan / actual 使用同一份最终 cfg 规则，
  不悄悄替换旧 `C:/` 规则再声称预检正确。
- **时间字段统一 UTC**：`started_at_utc`/`finished_at_utc` 落盘前统一转为同一时刻的
  `+00:00` ISO 字符串。现场 `datetime.now(timezone.utc)` 本就正确；显式注入带 +08 等
  偏移的 aware datetime 时也保存同一时刻的 UTC，保持离线注入可重复性与生命周期时段
  含义。此规范化不追溯修改任何旧证据。
- `consume_for_entry`（容量门 + 时段门 + 限额复检，executor=`claude`）→ `mark_running`
  （记真实 wrapper PID）→ `bind_child` 且 **`bound` 必须显式为 `true`** 才算子进程核验 →
  stdout 实时逐行监控 + stdin/stderr 并发线程 → 解析 → 报告绑定。
- 解析/分类/限额记录全在受保护流程内；**无条件**以真实退出做终态释放并**保留真实释放结果**
  （`capacity_release`，不再 except-pass 吞掉释放返回值）；已启动但真实退出未知 →
  `mark_unknown` 保留（`launch_unknown`），绝不假释放、绝不误判 429/cancel；
  尚未拿到句柄 → `start_failed` 释放本 token 并如实返回其释放结果。同 task+workspace 不重复写。
- **真实错误提取与回执配对（S4）**：`is_error=true` 时即使 `subtype=success` 也把 result
  字符串取入错误载体；`errors` 非空或 `parse_error` ⇒ 绝不 `protocol_success`；
  `tool_use.id` ↔ `tool_result.tool_use_id` 真实配对，成功回执 ≥1 才算工程成功
  （`use_count>0` 不够），缺失回执、全部权限拒绝、孤儿 result 分别记录
  （`missing_tool_receipts` / `permission_denials` / `orphan_tool_results`）；
  正文普通 429 文本与取消退出码 `4294967295`/`-1` 绝不判限额；普通可恢复错误只作诊断。
  0 工具的工程任务绝不冒成功（`zero_tool_engineering`）。
- **证据落盘**：请求（`request.json`）、进程（`process.json`）、发送任务
  （`sent-task.md`）、发送契约（`sent-contract.md`）、raw stdout/stderr 的**脱敏原件**均持久化；
  `request.json` 与 `run` 返回同时含权限模式与最小 `permission_preflight`
  （`mode/enforcement/exact_file_authorization/skipped/tools/required_tools/write_files/
  expected_write_rules/missing_*/denied/invalid_rules/reasons`，不含凭据）：bypass 下
  `enforcement=cli_bypass`、`exact_file_authorization=false`，不伪装精确文件保护有效；
  dontAsk 下保留旧标准化 `required_tools`/`write_files` 与精确范围核对；
  最终改写后**刷新 `result_sha256`** 并重算 `evidence_sha256`；429 限额以**最终 SHA**
  先 `record_main_force_limit` 再释放本占位。
- 报告绑定复用 `qoder_direct.analyze_report` + `finalize_binding`（正文九节 + 阶段 token +
  绝对路径精确匹配 + 协议成功 + 非空 session_id + 落盘回读 sha256 一致），不整份复制 Qoder。
  `write_evidence` 用 `report_path.write_bytes(...encode)` 避免 Windows CRLF 转换破坏回读哈希。
- **退出码**：通过=0、未派发=2、失败=3；离线结果 `business_verified=false`
  （只证协议与绑定，不证真实业务）。

### 6.1 传输故障（ECONNRESET 等）诊断口径

- **只认真实 terminal result 错误载体**：`result_errors`/`errors`、`is_error=true` 的
  result 文本、明确的 `errors_info`。绝不扫描成功报告/assistant 正文、成功 Read 回执或
  任意 stdout/stderr 关键词；`ECONNRESET_HANDLER` 之类标识符不算独立网络码。
- **字符串形态门**：仅当文本呈 CLI API/连接失败形态（如 `API Error: Unable to connect
  to API (ECONNRESET)`）或开头就是独立网络错误码时才成立；终态业务测试报告里引用的
  `ECONNRESET`（如 `FAILED ... expected ECONNRESET ...`）不算 CLI 传输失败。
- **结构化载体只读白名单**：只取 `code` 与明确的 `message`/`error`/`detail`/`msg`
  字符串（缺 message 用 code 本身），绝不序列化/复制整个 dict，headers/body/request
  等字段不检查、不落诊断；同一事件原 error carrier 仍保留在 events 原件中。
- facts 增加独立 `failure_kind='transport_error'` 与 `transport_error={code, source,
  message, completed_tool_receipts, completed_tool_receipt_ids, session_id}`；
  已完成工具数复用 `ok_receipt_ids`，因此能区分“零工具”和“2 个成功工具后网络失败”。
  无网络码证据时不假造 transport，也不从消息猜 HTTP 状态、请求 ID、重试次数、后端或重置方。
- 明确 **HTTP 401/402 认证/支付错误优先**：即便错误文本夹带 `ECONNRESET` 也不改判
  transport；既有 429/权限/业务测试错误同样不被覆盖。已有终态释放与 `launch_unknown`
  语义不变。失败后核对部分成果、原 session 与终态，按既有轮次授权换下一入口，不能
  归因为额度或永久停用；不新增定时器，也不自动重放工程。
- `CLAUDE_CODE_MAX_RETRIES=0` 由本地 CLI 读取支持，但**实际请求尝试次数**仍需动态
  证据（CLI 版本、错误载体），不夸大。服务端根因未知；现场诊断尚未完成，由 root 后续
  回读，离线诊断通过不等于现场网络已修复。

### 6.2 传输阶段被动观测（默认关闭；只由宿主为子进程显式启用）

- 模块 `scripts/claude_transport_trace.cjs`（纯 Node 内置模块 `diagnostics_channel`/
  `fs`/`path`）**只订阅** Undici 官方诊断通道的 request 五事件
  （`create`/`bodySent`/`headers`/`trailers`/`error`）与 client 四事件
  （`beforeConnect`/`connected`/`connectError`/`sendHeaders`），通道前缀为 `undici:`。
  不包装或改写 fetch/https、dispatcher、重试、socket；不读 request/response 的
  headers、body、trailers 内容，也不读 URL 凭据/query/token。
- **child-only 预加载**：仅由宿主在启动 Claude CLI **子进程**时显式设置
  `NODE_OPTIONS=--require <仓库绝对路径>/scripts/claude_transport_trace.cjs` 与
  `BRAIN_WORKER_CLAUDE_TRACE_FILE=<绝对路径>.jsonl`。**禁止**写入全局/用户级
  `NODE_OPTIONS`、`~/.claude` 设置或任何默认配置；env 未设时模块零订阅、零写、
  不产生任何日志。Windows 下预加载路径使用正斜杠并在路径含空格时加双引号，例如
  `--require "C:/path with spaces/claude_transport_trace.cjs"`；反斜杠可能被 NODE_OPTIONS 解析吞掉，
  此类预加载失败不是 API 通信故障。日志父目录由 root 预建，模块不建目录、不扫描路径；文件以独占方式
  创建，已存在即停止采集，绝不覆盖既有证据。
- 既有代理清理（`_CLEARED_CHILD_ENV` 清除 `HTTP(S)_PROXY`/`ALL_PROXY` 等）、
  `CLAUDE_CODE_MAX_RETRIES=0`、`API_TIMEOUT_MS=60000`、
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 全部保持原样：本观测不改代理、
  重试、额度或调度。
- 只记 `api.deepseek.com` HTTPS 443 且 path 属于 `/anthropic/v1/messages`、
  `/anthropic/v1/messages/count_tokens`（忽略 query）的请求；字段限 UTC 时间、
  相对耗时、固定事件名、本机匿名递增 request_id/socket_id、method 枚举、固定端点、
  数字状态码、socket 实际使用次数与是否复用、白名单 error code/syscall 与有限层
  cause。上限 1000 条 / 256 KiB，超出即停止；不轮转、不起后台任务、不写
  stdout/stderr、不抛诊断异常中断 CLI。
- **证据边界**：连接事件本身不是请求关联，只有 `client:sendHeaders` 同时给出
  request 与 socket 时才建立关联；复用必须由同一 socket 的实际使用次数证明，不能按
  “本轮第几次请求”推断。CLI 若走非 Undici transport 或未发这些事件，空日志**只说明
  证据不足**，绝不据此推断链路健康。本观测只补诊断支持，不定位也不修复根因。

### 6.3 首次真实权限拒绝立即停止（BW-CLAUDE-PATH-20261018-S2）

- `_exchange_streaming` 在 stdin 独立线程写入原任务并关闭；stderr 独立线程持续 drain
  （防管道死锁）；主线程逐行读取 stdout stream-json，**实时**识别结构化权限拒绝：
  `permission_denials` 数组、`is_error` 的 `tool_result` 且文本开头是审批拒绝语、
  result 错误载体里开头独立的拒绝文本。成功 Read 回执正文、assistant 文本、普通可恢复
  工具错误**不参与**识别。
- 首次命中后立即 `terminate()` **本 run 创建并绑定的 Popen 对象**（不扫描/不杀其它
  进程），停止后续派工/写探测/自动重派；主线程**不再阻塞读下一行**。stdout 原始行由
  reader 线程在锁内逐行记录（queue 只用于判断首次拒绝），主线程不重复 append、也不
  裁剪已收到的行：拒绝前已发出的后续事件同样进入 raw（`observed_followup_tool_use`
  如实标记竞态）；stderr 与触发 `tool_use_id`/原拒绝回执保留（脱敏后进入
  `events.jsonl`/`raw-*`/`request.json`/facts）。回执区分 `stop_requested` 与
  `terminated_by_worker`。
- 停止后仍按真实退出路径处理：有界 `wait(timeout)` 已知退出码 → 正常终态释放
  （`success=false`）；`wait` 失败/无退出码 → `mark_unknown` 保留占位
  （`launch_unknown`），**绝不假释放**。此未知路径也把**当时已收**raw stdout/stderr、
  `permission_stop` 与进程元数据写入本轮 outdir（`unknown-raw-stdout.txt` /
  `unknown-raw-stderr.txt` / `unknown.json`，`exit_code=null`、
  `raw_stdout_partial=true` 如实标快照、`success_binding_generated=false`），
  不等待 EOF、不生成成功绑定或九节报告正文。
- 传输失败（如 `API Error: Unable to connect to API (ECONNRESET)`）不是权限拒绝，不得
  触发 stop；正常路径的 stdout/stderr 原件、时钟、secret 脱敏与 transport 分型保持不变。
- 离线用可注入的假 Popen 覆盖立即 stop/已发出后续行原样保留/terminate 后可控 fake
  不再发 probe/unknown/stderr 连续输出/误杀负例；
  生产默认 `subprocess.Popen` 走实时监控，不是“结束后分类”。

## 7. execution_control 接受 claude

- `CAPABILITY['claude'] = {'fine_grained': True, 'read_dirs': True}`，可校验 claude plan /
  exact tools；**保留退休入口拒绝**（不因新增 claude 而放宽 retired-entry 判定）。
- `_counts_domestic_concurrency` 排除 `claude`（low=='claude' 不计入本快照并发闸的
  qoder/zcode 活进程计数；claude 的容量 2 由 `dispatch_pool` 原子池权威强制）。
  **注意与总额守卫区分**：claude 在途**仍计入** `dispatch_pool._domestic_active`
  的国内总在途（当前总 15，含 ZCode 1 槽与两个 CodeBuddy 精确模型各 1 槽），二者不矛盾——前者是二级业务快照闸、后者是原子总额守卫。

## 8. 离线回归

- `tests/test_deepseek_offpeak.py`：09/12/14/18 边界、Sat/Sun 周末、节日覆盖峰值、
  未知年 2027 关峰放行非峰、时区折算、date 输入。**该纯函数仅作历史兼容保留，生产派发
  与浮窗不再引用它**（BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023）。
- `tests/test_dispatch_pool_claude.py`：`CapacityAndRouting`（显式落第 1–2 槽、AUTO 经公平轮转
  可选中 claude、总 15 语义）、`WindowGate`（**工作日高峰/周末/假日/未知年度一律可派**、
  跨峰 consume 正常放行、高峰内 1800 秒真实限额短窗仍拒绝）、`QuotaLimitMarker`
  （reserve→record→consume limited→release）、`WindowGateCaliber`（claude 与 offpeak 已解耦）。
- `tests/test_claude_bypass_mode.py`（BW-CLAUDE-SIMPLE-20261018-S1）：默认/显式
  `--permission-mode bypassPermissions` 且真实 argv 含 `--dangerously-skip-permissions`、
  不是 allow-only；默认工程无 required_tools/write_files 仍到达 FakePopen 且回执记录
  `enforcement=cli_bypass`/`exact_file_authorization=false`；六项工具含 Bash 可见；
  显式 `dontAsk` 仍要求 required_tools 且精确 profile 拒绝 `C:/` 规则；其他模型/
  endpoint/未知模式拒绝；bypass 下零工具工程仍不 `execution_success`。
- `tests/test_claude_direct.py`：纯函数（argv 无 token / 默认 bypass 标志 / 模型与
  base_url 钉死且偏离即拒、非支持模式与 `--bare` 拒绝）、真实错误提取与回执配对（`is_error` + `subtype=success`
  取 result 错误载体、正文普通 429 与取消 `4294967295` 不判限额、`parse_error` 不判协议成功、
  缺失回执/全部权限拒绝不判工程成功）、生命周期（FakePopen 真实 PID、绑定回读、脱敏、
  预检失败零 Popen、`launch_unknown` 保留）。
- `tests/test_claude_file_permissions.py`（S2）：`C:/` 规则明确拒绝、正确
  `Edit(./file)`/`Write(./file)`、正反斜杠/大小写、新文件最近祖先、盘符不一致、
  `..` 逃逸、通配、目录、范围外兄弟、写授权超出清单、未声明写工具的写规则、
  写需求缺 Read、只读配置带写授权、尾分隔符、Windows 冒号 ADS/保留设备名/尾点空格、
  host 解析失败诚实拒绝、符号链接越界（平台可用时）；POSIX 大小写负例使用纯词法
  POSIX workspace（不依赖 Windows tempdir）。
- `tests/test_claude_permission_stop.py`（S2）：首个结构化拒绝立即 terminate、零后续
  probe、原始证据保留、成功 Read 正文含拒绝词不误杀、普通工具错误不误杀、stderr 连续
  输出、拒绝后无 EOF 且 terminate 失败时有界返回 unknown（不阻塞下一行、不假释放，
  raw stdout/permission_stop/进程元数据落盘）、unknown 不假释放、传输失败不误判权限。
- `tests/test_claude_permission_preflight.py`（S1/S2）：**显式 dontAsk** 工程入口缺
  `required_tools`/缺 Read 零 Popen 零凭据零 claim、只读合法、精确写范围、CLI
  `--write-file` 契约（fixture 已显式设置 `permission_mode='dontAsk'`，旧断言未放宽）。
- `tests/test_claude_transport_trace.py`（BW-CLAUDE-TRANSPORT-R2）：Node 不在 PATH 时
  skip；用临时目录 + `diagnostics_channel` 合成 publish 验证 §6.2 的观测契约——未启用
  零文件零订阅、独占创建、同 socket 两次请求标 `socket_uses`/`socket_reused`、连接错误
  不绑 request、错误码白名单与 cause 层数、HTTP 状态只记数字、headers/body/query/
  message/stack 与嵌套密钥标记不落盘、其他域/路径过滤、合法 JSON 行与真 UTC、既有文件
  不覆盖、cap 停止不抛、callback 不改请求对象。**合成 publish 不是真实网络观察**。
- 相关套件（fixture 已补第 11–12 claude 槽、安全断言未删未放宽）：`tests/test_dispatch_pool.py`、
  `test_dispatch_pool_revision.py`、`test_dispatch_pool_qodercn.py`、
  `test_dispatch_pool_multiprocess.py`、`test_availability_routing.py`、
  `test_native_lifecycle.py`。
- 全套 `python -m unittest discover -s tests -p "test_*.py"` 离线通过，不访问网络、不读凭据、
  不查真实额度、不启动真实模型（具体通过数以最近一次原样运行为准，见 SKILL.md 与本阶段报告）。

## 9. 真实世界事实（与离线证明分开记录）

- **官方 Node CLI 2.1.76**：真实 Read/Write 调用**已成功**（前一轮跑到 `error_max_turns`，
  文件原件保留、无结构化报告——该事实单独记录，不等同于验收通过）。
- **native 2.1.220 / 2.1.296**：连接失败，单列记录，不与 Node 成功事实混写、不据此改配置。
- **Bash 工具**：BW-CLAUDE-SIMPLE-20261018-S1 起默认 bypass 配置显式开放
  Read/Glob/Grep/Edit/Write/Bash 六项（由 root 同步本机私有配置），不再逐命令登记。
  2026-10-18（UTC），root 按相同 Node 2.1.76、
  模型、清代理与零重试设置完成两次正式隔离诊断：纯文本成功；Read→Edit→登记离线测试
  →九节报告绑定成功（7 次工具调用，测试文件哈希不变）。其中额外两次只读 `ls -la`
  如实单列，不作为精确命令沙箱或评分依据；成功往返不能定位历史 ECONNRESET 的重置方。
- **费用**：**未知**——CLI 自报的 token/成本估算 ≠ 账单，绝不据此宣称额度或账务结论。
- **真实一次新入口 Read/Write 验收由主脑（root）执行**；worker 不调 API、不读凭据。
