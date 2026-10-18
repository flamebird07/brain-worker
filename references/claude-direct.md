# Claude Code / deepseek-flash 直连入口（claude-direct）

> 本文件描述 brain-worker 的主力执行器 `claude`（容量 2；客户 DeepSeek Anthropic 兼容端点，
> 精确模型 `deepseek-flash`）。实现以 `scripts/claude_direct.py`、`scripts/deepseek_offpeak.py`
> 与 `scripts/dispatch_pool.py` 为准，本文件为口径说明；二者不一致时以代码为准并回修文档。
> **离线回归只证协议/绑定/时段门/容量语义；真实调用与端到端验收由根脑（root）在本机完成，
> worker 不调 API。官方 Node CLI 2.1.76 真实 Read/Write 已成功（独立事实，见 §9），
> native 2.1.220/296 连接失败单列，不与 Node 事实混写。**

## 1. 容量池：AUTO 可选主力槽 `claude`（容量 2；BW-CLAUDE-CAPACITY2 由 1 扩为 2，原 10 槽一项未减）

- runtime `claude`，池 `claude:deepseek-flash`，`CLAUDE_CAPACITY = 2`（BW-CLAUDE-CAPACITY2 由 1 增至 2），
  `MAIN_FORCE_CAPACITY[CLAUDE_POOL_KEY] = 2`。
- **已登记入主力体系**：`claude:deepseek-flash` 在 `MAIN_FORCE_POOLS`/`MAIN_FORCE_KEYS`/
  `MAIN_FORCE_CAPACITY` 内，`DOMESTIC_TOTAL_CAPACITY = 13`（含 `codebuddy:deepseek-v4.1-flash`
  精确模型 1 槽，由 `MAIN_FORCE+OVERFLOW` 求和自动得出；**原 10 槽一项未减**）；
  `_domestic_active` 统计所有非 Luna 在途 attempt，**claude 在途计入国内总额**。
- **独立组 claude**：committed 由 attempts 真实提交历史
  `_count_committed_claude` 统计，**绝不污染** committed_zcode/committed_qoder；
  当前口径（BW-CODEBUDDY-MAIN-S3 更新层序）：`codebuddy:deepseek-v4.1-flash`（层=0）>
  三 Qoder Max（层=1）> claude（层=2）> `zcode:GLM-5.3`（层=3）；Max 全不可用时 claude
  允许窗且有空位即优先于 Z；旧“平票排 Z2/Qoder 之后（`_grp_rank`）”的跨层 1:1 属历史、
  已被覆盖，历史 committed 计数照旧保留记账但不再参与跨层竞争。
- `AUTO` **可选** claude：AUTO 的 claude 入口请求**不绕过统一选择**，经同一权威 `_select_main_force`
  优先级排序（先过滤真实空位与时段门/限额，再按优先级层选择、同层内在途平衡）；显式 `executor='claude'` 保持
  精确组合、绝不跨到其它池或回退模型。
- `_domestic_policy` claude 分支（S4 收口 + BW-CLAUDE-CAPACITY2 + BW-CODEBUDDY-MAIN-S3）：`executor_conflict` → **总 13 守卫先于一切**
  （迁移债务满 13 时 `full`，**绝不派第 14 个**）→ 时段门 `main_force_window_closed` →
  限额 `full` → 有空位 `claim` → 满 `full`。
- 未配置（缺 local/example 配置或 executable）时**不得读作就绪**：`build_argv` 缺
  `executable` 直接 `ValueError`（零 Popen）。旧 runs/unknown 记录一律保留、不迁移、不清库。

## 2. 时段门（固定 UTC+8，无网络日历）

`scripts/deepseek_offpeak.py`（纯标准库）：

- `BEIJING_UTC_OFFSET = 8h`；naive 输入按 UTC 解释，aware 输入先归一到 UTC 再 +8。
- `PEAK_INTERVALS = ((540, 720), (840, 1080))` —— 09:00(含)–12:00(不含)、14:00(含)–18:00(不含) 为峰。
- `WEEKEND_WEEKDAYS = (5, 6)`；`HOLIDAY_RANGES` 硬编码 2026：Jan1-3、Feb15-23、Apr4-6、May1-5、
  Jun19-21、Sep25-27、Oct1-7。
- `classify_dispatch(now)` → `{allow, category, reason}`，category ∈
  `weekend / holiday / weekday_offpeak / weekday_peak / weekday_peak_unknown_year`。
- **未知年份保守**：工作日峰值一律关闭（`weekday_peak_unknown_year`），非峰与周末仍放行；
  绝不把调休上班周六当峰值（周末优先放行）。
- 峰期内**不新派**：`dispatch_pool._max_window_blocks` 让 claude 经 `offpeak.can_dispatch` 判定，
  offpeak 不可用则 **fail-closed**（拒绝新派）。`_window_closed_reason(pk)` 给出池感知原因。
- **跨峰预留只释放自身 token**：已 `reserve` 的 claude attempt 若在 `consume_for_entry` 时已进峰，
  只把本任务自己的 reserved 真实释放为 `start_failed`，绝不启动、绝不抢占别人名额、绝不泄漏。

## 3. 明确限额标记（受限池）

- `QUOTA_LIMITABLE_POOLS` 含 `claude:deepseek-flash`，复用既有 `main_force_limits` 最小布尔标记。
- 只认**同一条真实错误条目内绑定的结构化 429 / 硬上限**才自动跳过；不查余额、不做额度系统、
  不引定时器/评分/新服务/外部 API。取消退出码（`4294967295` / `-1`）与 SIG **绝不判为 429**。
- 落标须绑定真实 attempt（`task_id`+`attempt_token`）与真实错误证据路径/sha256，任一漂移全拒。
- **无证据绝不自动清标**：仅由用户显式额度恢复/重置后经 `release_main_force_limit` 人工解除。

## 4. CLI 构造（`scripts/claude_direct.py`，钉死不接受偏离）

- `build_argv`：`-p --output-format stream-json --verbose --model deepseek-flash
  --permission-mode dontAsk`，逐项 `--tools` / `--allowedTools` / `--disallowedTools` /
  `--add-dir`（**绝不 bypass/yolo**）、`--setting-sources`、`--settings`（隔离环境，
  **绝不覆盖 `~/.claude`**）、`--max-turns`；`--append-system-prompt` 附带
  `prompt_contract.build_contract` 完整九节契约。任务原文经 `stdin`
  （`proc.communicate(input=…)`），绝不拼进 argv。
- **拒绝式钉死（S4）**：`--bare` 出现即 `ValueError`（官方 Node CLI 2.1.76 不支持该开关）；
  权限模式 ≠ `dontAsk`（任何其它模式，含 bypassPermissions）即 `ValueError`；
  模型 ≠ `deepseek-flash` 即 `ValueError`；`base_url` ≠
  `https://api.deepseek.com/anthropic` 即 `ValueError`——绝不静默改道或回退。
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
  dispatch plan 精确预检、凭据解析）：任一失败 ⇒ **零 Popen、零占位泄漏**。
- `consume_for_entry`（容量门 + 时段门 + 限额复检，executor=`claude`）→ `mark_running`
  （记真实 wrapper PID）→ `bind_child` 且 **`bound` 必须显式为 `true`** 才算子进程核验 →
  `communicate` → 解析 → 报告绑定。
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

## 7. execution_control 接受 claude

- `CAPABILITY['claude'] = {'fine_grained': True, 'read_dirs': True}`，可校验 claude plan /
  exact tools；**保留退休入口拒绝**（不因新增 claude 而放宽 retired-entry 判定）。
- `_counts_domestic_concurrency` 排除 `claude`（low=='claude' 不计入本快照并发闸的
  qoder/zcode 活进程计数；claude 的容量 2 由 `dispatch_pool` 原子池权威强制）。
  **注意与总额守卫区分**：claude 在途**仍计入** `dispatch_pool._domestic_active`
  的国内总在途（总 13，含 CodeBuddy 精确模型 1 槽），二者不矛盾——前者是二级业务快照闸、后者是原子总额守卫。

## 8. 离线回归

- `tests/test_deepseek_offpeak.py`：09/12/14/18 边界、Sat/Sun 周末、节日覆盖峰值、
  未知年 2027 关峰放行非峰、时区折算、date 输入。
- `tests/test_dispatch_pool_claude.py`：`CapacityAndRouting`（显式落第 11–12 槽、AUTO 经公平轮转
  可选中 claude、总 13 语义）、`FairnessAndTotalGuard`（AUTO claude 请求不绕公平选择、
  AUTO 峰值诚实关闭、**迁移债务满 13 时 claude 有空位也绝不派第 14**）、`WindowGate`
  （含跨峰 consume 只释放本 token）、`QuotaLimitMarker`
  （reserve→record→consume limited→release）、`WindowGateCaliber`。
- `tests/test_claude_direct.py`：纯函数（argv 无 token / 无 bypass / 模型·权限模式·base_url
  钉死且偏离即拒、`--bare` 拒绝）、真实错误提取与回执配对（`is_error` + `subtype=success`
  取 result 错误载体、正文普通 429 与取消 `4294967295` 不判限额、`parse_error` 不判协议成功、
  缺失回执/全部权限拒绝不判工程成功）、生命周期（FakePopen 真实 PID、绑定回读、脱敏、
  预检失败零 Popen、`launch_unknown` 保留）。
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
- **Bash 工具**：默认配置不因此扩大。2026-10-18（UTC），root 按相同 Node 2.1.76、
  模型、清代理与零重试设置完成两次正式隔离诊断：纯文本成功；Read→Edit→登记离线测试
  →九节报告绑定成功（7 次工具调用，测试文件哈希不变）。其中额外两次只读 `ls -la`
  如实单列，不作为精确命令沙箱或评分依据；成功往返不能定位历史 ECONNRESET 的重置方。
- **费用**：**未知**——CLI 自报的 token/成本估算 ≠ 账单，绝不据此宣称额度或账务结论。
- **真实一次新入口 Read/Write 验收由主脑（root）执行**；worker 不调 API、不读凭据。
