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

## 3b. ZCode 独立持久 availability 状态（BW-AVAILABILITY-20261009-B2）

现场事故（2026-10-09 03:25:56Z）：真实 SDK `ProviderBusinessError`，HTTP 429，BigModel
Coding Plan，`provider_code=1310`，wrapper 派生 `category=rate_limit`，但服务器真实文本是
“已达到每周/每月使用上限”，且 reset 时间**无时区**。旧口径把它当 `temporary_backoff`（甚至
`quota_no_window` 保守 24h）都是错的：泛化的 `rate_limit` 标签**不得覆盖**真实硬上限语义，
无时区**不得猜** UTC+8，也**不得复用**旧 24h 冷却当恢复窗口。

为此新增一张与旧 `cooldowns` **完全独立**的持久表 `zcode_availability`（同库不同表，
`record_zcode_unavailability` 绝不触碰 `cooldowns`，CodeBuddy 冷却行与 ZCode availability
互不污染）。要点：

- **稳定通道键** `channel_key = {provider}|{quota_group}`；`quota_group` 仍只来自受信任
  路由（§2），配置别名不能拆分同一通道，routes 缺失/更名落入 `unknown-shared` 时**保守连带
  阻断**（改配置不能绕过历史 hold）。
- **硬额度判定**（`classify_zcode_availability`）只认**同一条真实错误条目内绑定**的
  status/provider/code/message：`(受信任 provider 正则 bigmodel|zhipu 且 code∈{1308,1310})`
  **或**窄的硬上限消息语义（`使用上限/用量上限/额度已用尽/…/usage limit/quota limit/limit
  reached|exceeded`），后者优先于 wrapper 派生的 `rate_limit` 标签。同一数字来自非受信任渠道
  **不泛化**为硬额度。报告正文里的 429 数字、另一条不带 429 的 quota 条目、取消退出码
  （`4294967295`/`-1`）**都不触发**记录。
- **availability kind**：`hard_hold`（硬额度且无可信窗口 → `blocked_until_utc=None` 无限期
  持有，等待显式用户重置或新恢复证据，绝不猜 24h/固定短周期）、`hard_reset_window`（硬额度
  且有可信带时区窗口）、`backoff_until_window`（普通 429 + 可信窗口）、`temporary_backoff`
  （普通 429 无窗口 → `min(30s·2^n,1800s)` + 0-25% jitter）。reset/Retry-After **取最晚的
  合法下限**、跳过非法/无时区项（`parse_reset_datetime` latest-valid-wins），绝不返回更早的首个
  匹配。
- **状态机** `backoff / hard_hold / recovery_unverified / healthy`，`epoch` 单调递增。
  窄的幂等手动重置入口 `zcode-reset`（必须 `--provider` + `--evidence-ref`，可选
  `--quota-group`/`--epoch`）把状态置为 `recovery_unverified`——**“可核验”≠healthy**，仍阻断
  完整派工。恢复点到达或新重置证据只**原子放行单次、无副作用的有界 probe**
  （`zcode_availability_gate(purpose='probe')` CAS 单赢家）；probe 失败/超时/取消设再探测退避
  窗口（`min(60s·2^n,3600s)`），不能立即重探；probe 成功必须 `epoch`+`attempt` 双匹配才转
  `healthy`。**迟到成功绝不清除更新失败的 epoch**（`settle_zcode_attempt` 只结算自己的 probe
  占位）。availability 记录**永不宣称真实额度/账务/免费**，只记状态与原因。
- **入口接入**：`zcode_direct.main` 在**容量门禁之前**、任何 Popen 之前调用
  `zcode_availability_gate`（权威最后一道原子闸），被拒打印 `zcode_availability_rejected`
  并退出 2、零建目录；只有受信任 429 且非取消码才在证据引用落定后 `record_zcode_unavailability`。
  证据已确认但接续冻结失败/终态未知时**仍保存不可用事实、且不释放活体调用锁**（fail-closed）。
  `qoder_direct` 读同一持久 availability：`dispatch_pool` 的资格过滤把被阻断的 ZCode 通道从
  可用主力里剔除，Qoder **不会被改道回不可用的 ZCode**，显式 Qoder（有空位）也**不因历史
  committed 计数被改道**（见 [global-dispatch](global-dispatch.md)）。

## 3c. availability 闭环加固（BW-AVAILABILITY-20261009-B4）

> B4 在不改动 §3b 语义、不扩大额度统计范围、不新增宣称真实额度/账务/免费的前提下，
> 把 B2 落地时 reproduced 的 7 个实现缺口逐一收口，并用 `tests/test_availability_routing.py`
> 的先失败后修复回归固化。全部离线确定性、显式临时 sqlite + 临时 routes，绝不触碰真实
> `~/.brain-worker` 或共享池。

- **代际/CAS 与真实行键（缺陷 1）**：`zcode_availability_gate` 无论命中现有行还是无行 stateless
  默认，都回传**被授予行的精确 `granted_channel_key`/`granted_quota_group`/`granted_provider`**
  与 `availability_epoch`（无行时明确为代际 `0`，不再返回 `None`）；`settle_zcode_attempt` 接受
  `channel_key=` 并**总是按代际比较**（`attempt_epoch != row['epoch']` 即拒），因此 A 落 429
  推进 epoch=1 后，B 的迟到成功（代际 0）绝不把 A 的新失败清成 healthy。同 provider 的配置别名/
  group 改名映射到同一 `channel_key`，不分裂限制；healthy 保留代际单调性防 ABA。
- **provider 与分类（缺陷 2）**：硬额度判定绑定**已核验的请求 provider**（`is_trusted_hard_quota_provider`），
  不再凭条目自报的 provider_code 泛化；`_HARD_LIMIT_MESSAGE_RE` 收窄到周期/套餐真实耗尽语义
  （“使用上限/用量上限/额度用尽/配额耗尽/周期额度上限/quota exhausted”），普通“Rate limit exceeded”
  “每分钟请求上限”等频率 429 → `temporary_backoff`。1308/1310 仅在该真实渠道 + 同一 429 载体内生效；
  非受信任渠道、报告正文 429、独立非 429 quota 条目、取消退出码一律不升格。
- **单次恢复资格与幂等重置（缺陷 3）**：新增持久列 `recovery_eligibility_consumed`——一个失败
  probe 真实执行（`executed=True`）即消耗掉**该代际唯一的一次恢复资格**（失败/超时/取消也消耗），
  退避窗口到期不再在同一 epoch 放行第二个定时 probe；要再探必须有**新恢复证据**。`zcode_manual_reset`
  绑定 `manual_reset_epoch`：同证据重放**它自己创建的那一代** reset 幂等——无论该行现处
  `recovery_unverified` 还是已被合法 probe 成功核验为 `healthy`，都幂等返回**实际**状态、不推进
  epoch、不重开核验资格（BW-AVAILABILITY-20261009-B6：早期只对 `recovery_unverified` 幂等，
  healthy 代际的同证据省略-epoch 重放会误落入下面的新 reset，把已核验通道重开并推进 epoch）；旧事件
  回放（`row.epoch>manual_reset_epoch`）被拒、省略 epoch 不能重置更新的失败；重置只到
  `recovery_unverified`（可核验），**永不直接 healthy**，幂等重放也绝不把非 healthy 谎称 healthy。
  未启动即被拒/异常的 attempt（`executed=False`）只清自己的 `probe_active`、不消耗资格，也不让无限重放复活。
- **正式 probe 入口与容量闸特殊资格（缺陷 4）**：capacity 门不再信任可自报的 `probe=true`；
  `zcode_direct` 在拿到 availability 授予后组装**精确绑定的 `probe_ticket`**
  （provider/channel_key/attempt_id/epoch），经 `dispatch_pool` 的 `_probe_allowed`→
  `zcode_probe_ticket_valid`（只读、任一读取错误 fail-closed）核验真实 CAS 授予行，才放行这**唯一**
  一次 `Popen`；普通派工仍被拒，workspace/容量/任务去重/模型+工具边界不变，显式预留 token 消费前再核验。
- **真只读与统一默认状态（缺陷 5）**：`zcode_blocking_rows`/`zcode_availability_status` 改用
  `_open_readonly`（`mode=ro` URI）——缺库/缺表返回空、**绝不 mkdir/建表/迁移/WAL**，其它读错误抛出
  交由调用方 fail-closed，绝不静默 healthy。pool 的 `_zcode_availability_blocked` 与 CLI 默认
  quota_store 统一走 param-or-env（Q 入口传参或 `BRAIN_WORKER_QUOTA_STORE`，Z 入口缺省
  `qc.default_store_path()`），使 AUTO 无需额外参数即过滤不可用 ZCode；派工写事务内绝不调用会写额度的
  helper，杜绝双库嵌套反向写锁。
- **终态发布与容量释放竞态（缺陷 6）**：`zcode_direct` 在**确认受信任 429 之后、可再派空槽复用之前**
  立即 `record_zcode_unavailability`（接续冻结失败也保事实），随后才做容量释放——
  `capacity_release`/`capacity_released` 移到记录块之后，state 已提交而容量释放失败**绝不伪造 healthy**。
  `pool._preclaim_reconcile` 现在要求 child 死亡**且** wrapper 死亡**且** wrapper_created 与当前创建匹配
  （PID 复用则跳过），auto 回收只释放执行容量、**绝不清 availability**；原 owner 显式 finish 仍可释放；
  两库各自独立短事务。
- **执行器+模型 combo 硬约束（B4 场景）**：受信任主脑的明确 `executor+runtime+model` 组合
  （含本次 `qoder:Qwen3.8-Flash`）贯穿 `reserve/select_and_claim/consume_for_entry/claim_due`，
  作为候选集合的硬边界——**不因别的候选有空位而拒绝明确已授权组合**；默认 AUTO 主力优先/容量/1:1
  轮换、Z2/Max2/Flash2 容量、CodeBuddy/WorkBuddy 人工中转策略均不变（并发 2/4 是 Skill 政策，
  非已核实的 Qoder 进程上限）。`claim_due` 的国内回收严格遵循原 scope 组合：绑 ZCode 且被阻断→
  保持 pending（绝不改派/降级 Luna）；绑 Qoder 即便 ZCode 有空位也只回收 QMax/Flash。
- **物理满 vs 合格耗尽分列（缺陷 7）**：`ask_record` 的 capacity 降级改用
  `_scope_allowed_candidates(scope, zcode_blocked)` 客观核验**受信任候选是否真的排空**，不再仅凭
  auth/capacity 字符串+detail 就把全部授权候选判为不可用；物理六名额满与“合格候选耗尽”**分开报告**，
  误报不自动 Luna；非容量降级仍需显式 Luna 授权 + 六真满 300s 规则；domestic 回复**永不 Luna**，
  external/cancel 不自动 Luna。`reply` 保护 claimed/settled/cancelled/launch_unknown，只接受合法
  首次回复、重复确认幂等，settled 票据绝不因迟到/重复回复重开。

## 4. 到期恢复：recovery_unverified + 单次有界 probe

> **ZCode 政策例外（见 §1）**：ZCode 取消自动额度冷却，因此**不存在“到期→必须先 probe”
> 的门禁**：`gate_dispatch(runtime='zcode')` 直接跳过本节的冷却/到期/probe 判定，普通 ZCode
> 派工**永不要求 recovery probe**。`--quota-recovery-probe` 仅作为可选的有界只读核验入口保留
> （边界同下），不得变成 ZCode 派工前置条件。本节规则继续适用于 CodeBuddy 及其它 runtime。
>
> **区分（BW-AVAILABILITY-20261009-B2）**：本节讲的是**旧 `cooldowns` 表**的到期/probe 门禁，
> 对 ZCode 已取消。§3b 的 `zcode_availability` 是**另一套独立状态**：它不复活旧冷却，但对
> 真实受信任硬额度/429 事实做无限期或有窗口的 hold，并以 `zcode-reset` + 单次有界 probe 核验
> 恢复。两者互不写对方的表；ZCode 派工同时受 §3b availability 闸（`zcode_availability_gate`）
> 约束，与旧冷却门禁是否跳过无关。

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
# §3b 独立 ZCode availability（与旧 cooldowns 完全分离）：
python scripts/quota_control.py zcode-status --store <db> [--provider <id>]
python scripts/quota_control.py zcode-reset --store <db> --provider <id> \
    --evidence-ref <ref> [--quota-group <g>] [--epoch <n>]
```

测试一律传显式临时 store/routes（`BRAIN_WORKER_QUOTA_STORE`/`BRAIN_WORKER_QUOTA_ROUTES`），
绝不写真实状态，也不得默认禁用门禁或伪造成功让旧测试通过。唯一被**有意**关闭的是 ZCode 的
**自动额度冷却**（用户 2026-10-08 决定手动管理/重置额度，见 §1 固定策略）；ZCode 的并发占位
门禁（工作区单写入、通道单在途）与 CodeBuddy 的全部额度冷却规则都不得据此放宽或删弱。
