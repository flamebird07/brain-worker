# Quota Routing & Persistent Cooldown（BW-QUOTA-20261008-S1，S2/S3 修订）

> **退休说明（2026-10-08 最新决定）：** CodeBuddy/WorkBuddy 直连已改为 human-relay only，生产
> `codebuddy_direct` 已彻底移除可执行传输（不再 `import subprocess`、不再有 `dispatch_core`），
> `main` 对任何新直连在读配置/建输出/进入额度门禁前固定拒绝，不再进入实际额度门禁派发。下文
> 提到的 CodeBuddy 冷却/结算路径现仅由**仅测试**的 `tests/offline_codebuddy_harness.py::
> replay_dispatch` 在离线回放（合成 stub）中保留，历史 sqlite 行、既有占位与 CodeBuddy 记录
> 都不删除、不覆盖、不清冷却。当前实际直连派发的额度门禁只对 Qoder 与 ZCode 生效；ZCode 手动
> 额度、工作区单写入与通道单在途规则不变。ZCode 另有跨会话共享的**并发容量池**
> （`scripts/dispatch_pool.py`，见 [global-dispatch](global-dispatch.md)）：容量并发 ≠ 额度冷却，
> 两个 ZCode workspace 走同一 provider 时经**已校验的容量 claim**放行，同 workspace 单写入与
> unknown/在途保护不变，不删通道行、不禁用门禁、不伪造 claim。

`scripts/quota_control.py` 提供 brain-worker 的持久额度冷却与路由门禁；`zcode_direct.py` 与
`qoder_direct.py` 在**所有 Popen 之前**（含不传 `--dispatch-plan` 的兼容路径）调用它
（CodeBuddy 生产入口已在额度门禁之前固定拒绝，其额度路径只在测试离线回放中保留）。
本文件是口径说明；实现以代码与其内注释为准。

## 1. 持久状态库

- sqlite3 单文件，默认 `~/.brain-worker/quota-state.sqlite3`（跨进程、跨派工、跨工作区、
  跨 repo/安装版一致）。环境变量 `BRAIN_WORKER_QUOTA_STORE` 或入口 `--quota-store` 覆盖。
  **默认 store 必须稳定共享**：不得用任务输出目录、随机 ID 或按客户端名分库来绕过冷却。
- 三张表：
  - `cooldowns(quota_group)`：`state ∈ {cooling, recovery_unverified}`、UTC 截止、原因、
    来源、退避次数、`epoch`、`probe_active`/`probe_attempt`。冷却**单调**：已有更晚冷却
    不被更早事件缩短；清除必须绑定本次 attempt 观察到的 `epoch`（迟到成功不得覆盖新冷却）。
  - `workspace_locks(workspace)`：以 realpath 归一的真实工作区为键，检查+占位在
    `BEGIN IMMEDIATE` 事务内原子完成（两进程争抢只有一个能插入）。
  - `channel_locks(quota_group)`：**每个额度通道单在途**，跨 workspace 亦然；两
    user_confirmed 独立组各自 workspace 可并行接续。
- 在途进程不因“租约到时”被抢占：没有租约超时。占位释放与冷却写入/清除**只在当前真实
  终态**通过 `settle_attempt` 在同一 `BEGIN IMMEDIATE` 事务里原子完成——绝不再“先在
  finally 里 release_lock、终态后另写冷却”（那样会留出 429 已发生但冷却未落库的二次派发
  空窗）。`settle_attempt` 的 terminal：`confirmed_exit`（子进程已退出，成功走带核验清除、
  429 走冷却写入、非 429/取消/probe 超时只结算不写 healthy）、`start_failed`（从未 Popen，
  安全释放占位）、`unknown`（wait/communicate 异常、终态无法确认 → **保留**工作区/通道/probe
  占位、保守阻断）。进程已死时终态未知 → 保守阻断，给出显式查证释放方式
  （`python scripts/quota_control.py release-lock --store ... --workspace ... --owner-pid <pid>
  --confirm-owner-terminal`），绝不默认删锁；pid/attempt 不匹配的释放被拒绝。
- **BW-ZCODE-MANUAL-QUOTA-20261008-S1 政策例外（ZCode 取消自动额度冷却）**：额度门禁由
  真实 runtime 固定策略决定，只有 `runtime='zcode'` 生效，不可被任务书关闭，也不提供关闭
  CodeBuddy 的开关。此时 `settle_attempt` 以**已核验占位携带的 runtime**（而非自报）判定，
  ZCode 的 `confirmed_exit`/`start_failed` **只释放本次工作区/通道占位**，绝不新增/延长/清除
  任何冷却，也绝不触碰同组（如 `unknown-shared`）里 CodeBuddy 记录的冷却；`unknown` 与所有
  runtime 一样保守保留占位。`gate_dispatch(runtime='zcode')` 同样跳过全部额度冷却/到期/probe
  判定，但仍执行工作区单写入 + 通道单在途并发占位、活/未知 owner 不抢占（跨 CB/Z 同一真实
  工作区仍串行、同组跨工作区仍单在途）。`import_terminal(runtime='zcode')` 仍提取并返回 429
  分类/原因供审计，但**不写 cooldown**。CodeBuddy 及其它 runtime 的冷却规则与低层 API
  （`record_quota_event`/`classify_quota_failure`）完全不变；sqlite 里的旧 ZCode 冷却行作为
  历史保留、不再挡 ZCode，也不需删除。

## 2. 额度分组（quota_group）

- 分组只来自**受信任本机路由配置**（默认 `scripts/quota-routes.json`，可用
  `BRAIN_WORKER_QUOTA_ROUTES` 或 `--quota-routes` 覆盖；随包结构示例见
  `scripts/quota-routes.example.json`，本机真实配置被 `.gitignore` 排除，不入库）。
  worker 的任意 plan/自报组名不参与解析。
- 通道标识是套餐不敏感的运行时事实：CodeBuddy 用解析后的**运行时入口 `entry_cli`**
  （`Path(cfg['cli']).resolve()`，并附 `node`）而非可被复制/改名的 entry config 路径，
  ZCode 用 provider id（如 `account:bigmodel-individual-coding-plan`）。不读凭据。
- 独立性来源必须写明：本轮 CodeBuddy/ZCode 独立来自 **user_confirmed（用户 2026-10-08
  明确确认），不是服务商/账单核实**。通用配置仍支持 `shared_declared` 共享组。
- **未知关系不可默认独立**：未匹配路由的通道一律落入共享保守组 `unknown-shared`。
  未匹配通道**不得绕过同 runtime 已知的任何未清除冷却**：只要 store 里还有已知组的
  冷却行（cooling 未到、cooling 已到期、或 recovery_unverified），未知通道都被阻断——
  到期未核验≠已恢复，routes 被切换/缺失也不能抹掉 store 里的既有 known 冷却。跨 runtime
  未知关系仍共享 `unknown-shared` 互相阻断。历史“共享尚未知”记录保留、不覆盖。
  **ZCode 政策例外（见 §1）**：`runtime='zcode'` 取消自动额度冷却——缺 routes、落入
  `unknown-shared`、或共享组的历史冷却**都不再挡 ZCode 派工**（CodeBuddy 侧仍按上述被阻断），
  但 ZCode 的**通道单在途锁与工作区单写入照常生效**，同组跨工作区仍单在途。

## 3. 429 分类（复用 execution_control.explicit_429，6004/category 不是唯一判据）

只认错误结构里明确 status/code=429 或错误文本**开头**的独立 429 码；正文偶然出现 429、
仅 `category=quota`、权限错误一律不产生冷却。ZCode 包装层异常另走白名单提取（见 §3a）。
命中后按优先级：

1. **quota_reset**：带时区的服务器 reset 时间（如 `2026-10-08 18:14:13 UTC+8`）换算 UTC
   （本例 = `2026-10-08T10:14:13Z`）。无时区/非法时间不猜，不用固定 5 分钟替代。
2. **retry_after**：结构化 Retry-After（秒数或 HTTP-date，只从错误结构的 dict 字段/headers
   键取，不扫正文）；该下限**不被指数上限截断**；与 reset 同时存在取**更晚**者。
3. **quota_no_window**：quota 类 429 但完全无窗口 → 保守 24h 阻断，不重派完整任务、
   不猜已恢复。
4. **temporary_backoff**：临时 429 → `min(30s·2^n, 1800s)` 指数退避 + 0-25% jitter，
   只由 CLI/调度其中**一层**承担（见 §5）。

## 3a. ZCode 额度信号：包装层 + 本运行 stderr 白名单抽取（zcode_direct，缺陷 B / S3 需求 1 / S5 缺陷 2、3、8）

S2 停摆来自 ZCode 独立套餐 HTTP 429 / provider_code 1308（非“共享 CodeBuddy 证据”）。
额度信号有两个来源，二者都**只在已知失败结构**里**按白名单有界提取**
`provider / response_status(==429) / provider_code / message`（及引用性字段
`reset_timezone / source_stderr_sha256 / original_source`），归一化为 errors_info 项复用
§3 分类：

1. `zcode_direct._wrapper_quota_error`：从 envelope 的 `quota_error`/`wrapper_error` 或
   `errors` 里的 dict 项抽取。SDK runner 只把错误串放进 `envelope.errors`，真实 status/code
   落在**本次运行自己的 stderr.log**，所以 envelope 分支常缺码，需要下面的 stderr 分支补上。
2. `zcode_direct._provider_business_error_from_stderr`：对**本次运行的 stderr 原文**做
   有界白名单抽取（≤64KB、关键字命中、`responseStatus: 429`、`code:` 正则、首行 message）。
   **保留原始行序、绝不重排去迁就代码**：先出现的 `code: 'PROVIDER_BUSINESS_ERROR'` 是外层
   wrapper_code，其后的纯数字 `code: '1308'` 才是内层真实 provider_code。抽取字段
   `wrapper_code`/`provider_code`（数值）/`code`（字符串）/`status`/`category`。

分类与保守性：
- `category='quota'` **只**在内层数字码 =1308、显式 `category=='quota'` 或确为额度耗尽事实时
  成立；普通 429（非 1308、无耗尽/窗口）归 `rate_limit`，走 §3 的 `temporary_backoff`，
  保留 Retry-After 下限，**绝不误升 24h `quota_no_window`**。
- reset 无时区 → `quota_no_window`（保守 24h），不猜 UTC+8、不自动重派。
- 失败路径的**脱敏摘要/证据引用只保存白名单字段**（`status/provider_code/wrapper_code/
  category/provider` 及引用性 `reset_timezone/source_stderr_sha256/original_source`），
  **绝不把 HTTP headers 或原始 stderr 字节写进摘要**；此处“不落盘 headers”是**摘要范围**的
  约束，不是删除载体——本次子进程自己的**原始 stderr 仍原样保存在本机 `out/stderr.log`**
  （仅按 path+SHA 引用、绝不回读原文或外泄字节），代码只对 stderr 做有界白名单抽取。正常报告
  正文里偶然出现的 429/1308 字样一律不产生额度信号。**（ZCode 政策例外：分类结果仍被提取并
  返回/记入摘要，但对 ZCode 不写 cooldown、不做自动恢复探测——见 §1/§4。）**
- 原始错误引用（S6 缺陷 2 更正）：ZCode 的**本次结构化失败信封**就是 `out/stdout.json`（S2
  同形 string-only errors / errors_info），故 `original_error_ref` **主载体指向本次 stdout.json，
  SHA 对其原始字节计算**——绝不因 stderr 为空就漏记原错误。SDK `ProviderBusinessError` 的**本次
  stderr 帧**（`out/stderr.log`）以 `sdk_stderr_frame`（path+SHA+白名单分类字段）**一并保留**，
  但不复制 headers/raw 字节。**空 stderr 或仅 warning（无白名单错误帧）绝不冒充原错误**（解析器
  返回 None 即不绑 stderr）。`report-state.json` 只是独立格式诊断、**永不作 original_error_ref**；
  历史 spec 引用另列，绝不替代本次 refs。CodeBuddy 同理：其失败载体为本次 `out/stdout.jsonl`。
- 会话回执身份核验（S6 缺陷 1）：CodeBuddy 写回执要 `use.session == result.session ==
  parsed['session_id']` **三者齐全且相等**才 `identity_verified`；两事件 session 彼此相同但都属
  旧会话（OLD/OLD）而当前为 CURRENT 时一律 `unverified`，失败 flags 仍排除、缺身份仍 unverified、
  绝不拿全局 session 补造。
  （后者只是独立格式诊断）。
- 历史引用分离保存；SDK runner（`zcode_sdk_runner.mjs`）不改。

## 4. 到期恢复：recovery_unverified + 单次有界 probe

> **ZCode 政策例外（见 §1）**：ZCode 取消自动额度冷却，因此**不存在“到期→必须先 probe”
> 的门禁**：`gate_dispatch(runtime='zcode')` 直接跳过本节的冷却/到期/probe 判定，普通 ZCode
> 派工**永不要求 recovery probe**。`--quota-recovery-probe` 仅作为可选的有界只读核验入口保留
> （边界同下），不得变成 ZCode 派工前置条件。本节规则继续适用于 CodeBuddy 及其它 runtime。

冷却到期只是 `recovery_unverified`，不代表恢复。完整重派被拒；只允许单次、无副作用、
有界的核验占位（入口 `--quota-recovery-probe` + 调度提供的最小提示词；同组同时最多一个
probe，第二个在飞 probe 被 `already in flight` 阻断）。probe 边界由
`validate_probe_dispatch` 程序化强制：禁 `Write/Edit/Bash`、禁 `--resume-session-id`、
禁 `--dispatch-plan`、提示词 ≤ `PROBE_PROMPT_MAX_BYTES`、显式时限
`--quota-probe-timeout-seconds`，实际边界记入 `request.quota_probe_bounds`。probe 终态经
`settle_attempt` 结算：真实成功（`confirmed_exit`+success）才带 epoch/attempt 核验清冷却
（仅表示该通道本次观测成功，不声称服务商额度已核实）；失败/取消/超时 → 绝不写 healthy，
`settle_attempt` 一并结算 `probe_active` 不永久锁死通道。测试用 stub 回放，不发真实 probe。

S5 缺陷 4：跨组到期死锁恢复。gate 的**双向保守跨组阻断**只作用于完整 `dispatch`；对
`purpose=='probe'` 网开一面：当阻断来源的相关组冷却**都已到期且无在途 probe**时，放行本组
进入上面的单次有界 probe 路径（否则“已到期 CB 组 + 已到期 unknown-shared 组”会互相永久阻断、
谁都进不去 probe）。相关（非 user_confirmed 独立）组之间**串行**：只要有一个相关组仍在主动
冷却（`cooldown_until>now`）或有在途 probe，就返回 `serialized against related groups` 拒绝，
绝不同时消耗同一份额度、绝不并发探测/派发。**真实未来 reset 之前不得探测**。probe 成功经
`settle_attempt` 只按被核验的身份/epoch 清除本组冷却，**绝不宣称所有未知组都恢复**、绝不顺带
清除其它行。CB 与 Z 互不污染（user_confirmed 独立）。

## 5. CLI 与调度只有一层临时重试

CodeBuddy 子进程 env 显式设 `CODEBUDDY_MAX_RETRIES=2`、`CODEBUDDY_RETRY_WATCHDOG=0`
（有界生成前重试、关无限 watchdog；只改本次子进程，不动用户全局），并写入
`request.json` 的 `cli_retry_policy`。调度层只记 CLI 最终终态与 `quota_outcome`
（下次可派时间），不循环重跑整个任务、不静默 auto/fallback/换模型，不把客户端切换
当额度恢复。官方依据：https://www.codebuddy.ai/docs/cli/env-vars ；
ZCode 套餐/账号才是额度来源：https://zcode.z.ai/cn/docs/configuration 。

## 6. 中断接续（scripts/continuation_contract.py）

派发前 `freeze` 声明受控文件清单（realpath 校验，越界拒绝），baseline 字节副本（有界
2MB/8MB、敏感配置名拒绝复制）随 `request` 留证；两入口都接：`codebuddy_direct` 与
`zcode_direct` 均在 Popen 前 baseline 冻结、终态后 `evaluate` 生成 `continuation.json`。
`evaluate` 回读前后 SHA + 真实统一 diff（部分写入留存、不回滚），并硬校验同一
`workspace_realpath` 与每文件原物理 target：换 workspace / junction 改指向即 target_drift，
不透过新链接读取。`bind_registered_test` 用**登记时记录在案的 input 哈希**复算指纹，
判定历史证据的内部完整性（name/argv/cwd/严格整数 exit 0/inputs_unchanged/日志 SHA 回读）；
**当前输入文件 SHA 变化保留“历史已验证 + 当前 stale”**，由 `test_inputs_stale` 独立判定，
不以当前输入抹掉原验证；缺证据/空 inputs/退出码伪布尔/日志被动过/指纹不符 → invalid。
`build_handoff` 输出紧凑交接（文件清单 + SHA + 证据引用 + 待办）；**只有成功的 Write/Edit
回执才算写关联**（失败回执留作尝试、不冒充成功）。没有最终 worker report 不补写
（`final_worker_report_present` 只认磁盘存在 + SHA 核验过的引用，不以 `bool(ref)` 冒充）；
未声明清单时如实标 `files_declared=false`，不做全项目无界扫描；无契约路径显式
`unverified_no_contract`。

## 7. Windows 存活查询（缺陷 A / S3-safety）

释放/阻断前需只读判断 owner pid 是否存活。`_pid_alive` **默认宿主判定用 `sys.platform`
（Windows 为 `win32`），不是 `os.name`**（Windows 上 `os.name` 为 `nt`，不以 `win` 开头，
会错误落入 `os.kill` 分支，而 Windows `os.kill` 除 CTRL 事件外走 `TerminateProcess`，
对任意 pid 是真实终止风险）。`'nt'` 亦显式路由到 Windows 分支。Windows 分支用
`ctypes` `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`+`GetExitCodeProcess`+`CloseHandle`，
**绝不调用 `os.kill`**；查询失败一律 `unknown`，调用方保守按存活处理；
`ERROR_INVALID_PARAMETER(87)` → `dead`。`os.kill(pid, 0)` 只出现在 POSIX 分支。离线用例
mock `_windows_pid_state` 并给 `os.kill` 装会抛 `AssertionError` 的哨兵，确保 Windows
分支从不触达它。

## 8. CLI

```
python scripts/quota_control.py status --store <db> [--group g]
python scripts/quota_control.py import --store <db> --terminal <redacted.json> \
    --runtime codebuddy --identity '{"entry_cli": "C:/.../codebuddy-cli.js"}'
python scripts/quota_control.py release-lock --store <db> --workspace <realpath> \
    --owner-pid <pid> --confirm-owner-terminal
```

测试一律传显式临时 store/routes（`BRAIN_WORKER_QUOTA_STORE`/`BRAIN_WORKER_QUOTA_ROUTES`），
绝不写真实状态，也不得默认禁用门禁或伪造成功让旧测试通过。唯一被**有意**关闭的是 ZCode 的
**自动额度冷却**（用户 2026-10-08 决定手动管理/重置额度，见 §1 固定策略）；ZCode 的并发占位
门禁（工作区单写入、通道单在途）与 CodeBuddy 的全部额度冷却规则都不得据此放宽或删弱。
