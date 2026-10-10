# 全局并发容量池与 1:1 路由（global-dispatch）

> 本文件描述 brain-worker 跨会话共享的**并发容量池**：`scripts/dispatch_pool.py`（纯标准库
> `sqlite3`，**无常驻调度服务**）。它解决的是“同一时刻全系统允许几个真实执行器在跑”，与
> `quota_control` 的**额度冷却门禁**是两回事：容量并发 ≠ 额度。落地口径以本文件为准，实现以
> `scripts/dispatch_pool.py` 为准，二者不一致时以代码为准并回修文档。

## 分配决策（当前有效，BW-POOL-SPLIT-20261010-S2/S3 + BW-MAX-WINDOW-20261010-S2 六池定型）

所有会话共享同一个持久池，绝不各自为政：

| 角色 | 组合（`runtime:model`） | 每组合真实在途上限 | 说明 |
| --- | --- | --- | --- |
| 主力 | `zcode:GLM-5.3` | 2 | 与其他主力按 committed 做 best-effort 1:1 轮换 |
| 主力 | `qoder:Qwen3.8-Max` | 1 | 国际 Max 主力；明确限额命中时经 `main_force_limits` 自动跳过，不查余额；**BW-MAX-WINDOW-20261010-S1：作为主力仅北京时间 22:00（含）至次日 08:00（不含）参与新派工，其余时段 AUTO 自动跳过、主脑显式 `reserve` 如实拒绝** |
| 主力 | `qodercn:Qwen3.8-Max` | 1 | CN 内置 Max 主力；独立池，不受国际 Max 标记影响，限额规则同国际 Max；**BW-MAX-WINDOW-20261010-S1：主力时段门同国际 Max（北京时间 22:00 含至 08:00 不含）** |
| 主力 | `qodercn:Qwen-3.8-Max` | 2 | CN 自定义主力（友好名带连字符 `Qwen-3.8-Max`，与内置 `Qwen3.8-Max` 不同）；属 Qoder 主力组、复用既有 committed 1:1 轮换；**BW-CUSTOM-MAX-NIGHT-20261010-S1：纳入主力时段门（作为主力仅北京时间 22:00 含至次日 08:00 不含参与新派工，其余时段 AUTO 自动跳过、主脑显式 `reserve`/`consume` 如实拒绝），并复用 `main_force_limits` 限额判定，各自 pool_key 独立落标、对两内置 Max 无连带** |
| 兜底 | `qoder:Qwen3.8-Flash` | 2 | 与 CN Flash **同级**；`AUTO` 在合格可用主力均不可用或已满时从两区 Flash 选（可跨国际/CN），明确 `executor='qoder'` 时按本地区约束 |
| 兜底 | `qodercn:Qwen3.8-Flash` | 2 | 与 `qoder:Qwen3.8-Flash` 同级；`AUTO` 可跨区选，明确 `executor='qodercn'` 时按本地区约束 |
| 退休 | `qodercn:DeepSeek-Flash` | 0 | 新派发退休（`LEGACY_RETIRED_POOL_KEYS`，`_domestic_policy` 返 `unknown_pool`），仅供已存在旧在途 attempt 按真实 runtime/model 计入总数并真实终态释放 |
| 救援 | `luna:native` | 无上限 | 只经 `claim-due` 竞争裁决，绝不被自动选中 |

- **国内合计上限 = 10**（`DOMESTIC_TOTAL_CAPACITY=10`，`_domestic_active ≥ 10` 时任何新国内 claim 一律 `full`、不派第 11 个）：`zcode:GLM-5.3`=2 + 国际内置 Max=1 + CN 内置 Max=1 + CN 自定义主力=2 + 国际 Flash=2 + CN Flash=2。退休 DeepSeek 新派发不占额度但既有在途仍计入总数。（历史：本表旧版曾记为“国际 Max 各 2 + CN DeepSeek 补充 2、合计 6→8”，S2/S3 收为五池总 8，BW-MAX-WINDOW-20261010-S2 加入 CN 自定义主力升为六池总 10，详见 §11。）
- **Luna 无数量上限**，但它不计入国内名额，也永远不在正常路由里被派生。
- 未列入本表的组合（例如 `qoder:GLM-5.3`、`zcode:Qwen3.8-Max`、任意未授权模型）容量为 0，
  `reserve`/`select_and_claim` 一律以 `unknown_pool` 拒绝，绝不派发未授权组合。

## 牛马主力时段门（BW-MAX-WINDOW-20261010-S1 + BW-CUSTOM-MAX-NIGHT-20261010-S1）

用户 2026-10-10 锁定：Qoder 国际内置 `Qwen3.8-Max`、Qoder CN 内置 `Qwen3.8-Max` 与 CN
自定义 `Qwen-3.8-Max` 三个 Qoder Max **只在北京时间 22:00（含）至次日 08:00（不含）作为主力**
参与新派工；其余时间新派工自动跳过（BW-CUSTOM-MAX-NIGHT-20261010-S1 把自定义纳入相同时段门）。
实现口径（全部在 `dispatch_pool.py`，复用既有注入的 `now`/UTC，绝不依赖宿主系统时区）：

- **北京时间固定按 UTC+8 折算**：`_beijing_hour(now)` 先把 `now` 归一到 UTC 再加 8 小时取
  小时；`_main_force_window_open` 判 `h≥22 或 h<8`——22:00 可选、08:00 不可选（左闭右开）。
- **门控这三个 Qoder Max**（`WINDOW_GATED_MAIN_FORCES = (qoder:Qwen3.8-Max,
  qodercn:Qwen3.8-Max, qodercn:Qwen-3.8-Max)`）；`zcode:GLM-5.3` 主力、两地区
  `Qwen3.8-Flash` 兜底、`luna:native` 救援、已退休 `qodercn:DeepSeek-Flash` 一律不受时段约束。
- **AUTO 白天自动跳过**：`_eligible_main_forces(conn, zcode_blocked, now)` 在非时段把三个 Qoder
  Max 从合格主力里剔除，`_select_main_force` 只在剩余主力（ZCode）里 best-effort 轮换，ZCode 也不
  可用/已满时才落到同级 Flash；三 Max 被跳过后绝不新落。
- **主脑显式预留如实拒绝、绝不静默换模型**：`reserve(..., executor='qoder'/'qodercn',
  model='Qwen3.8-Max'/'Qwen-3.8-Max', now=白天)` → `main_force_window_closed`（`sent=false`、非
  `routing_required`），绝不偷偷换成同区 Flash。自定义分支走同一 `_max_window_blocks` 门。原生入口
  自身的 select/consume（同一 owner lane）**没有墙钟豁免**：`select_and_claim` 在时段外同样返回
  `main_force_window_closed`（回滚、零提交），约束落在“候选过滤 + 主脑预留 + 原生入口 + 启动前再检查”
  所有新派工入口。
- **预留启动前再检查（跨 08:00 真实释放）**：`consume_for_entry` 消费已预留 token 前，若本 attempt
  落在受门控的任一 Qoder Max（含自定义）而当前已离开时段：`reserved` 在同事务内真实释放为
  `start_failed`（只释放本任务自己的占位、绝不泄漏、绝不抢占别人名额）；`running`/`unknown` 旧在途
  绝不假释放，只拒绝重复启动。
- **降级 `claim_due` 使用同一候选过滤**：国内回收经同一 `_eligible_main_forces` 过滤，钉住任一
  Qoder Max 的原 scope 在非时段保持 pending（如实 `main_force_window_closed`/`model_unavailable`），
  绝不回收 Max、绝不改派别的模型、绝不当作 Luna。
- 活任务不追杀：本门只影响**新** `reserve`/`consume`/`claim_due`，不改静态总容量 10、不弱化
  capacity/锁/限额，已 running/unknown 占位与真实终态释放逻辑完全不变。不加定时器/余额查询/
  数据库表/后台任务。

## 1:1 主力轮换是 best-effort，不是严格均衡

`select_and_claim` 在无显式 claim 时做**原子路由选择**，顺序如下：

0. **前置安全与资格过滤（对所有入口适用）**：先做任务/工作区安全守卫（同 `task_id` 在途去重、同
   真实 workspace 单写入）、票据 scope 与原 scope 绑定核验，再按 §执行器约束 的持久 availability
   **只读**资格过滤，把被阻断的 ZCode 通道从合格候选集合剔除；之后才进入下面的选择。受信任主脑明确
   绑定的 `executor+model` 组合是**硬约束**：只按该组合自身的真实容量与安全门 claim/`routing_required`，
   **绝不因历史 committed 比例或别的组合有空而被改道**（该路径直接走组合约束判定，不套用第 3、4 步）。
1. 同 `task_id` 已有在途 attempt → `duplicate_task_in_flight` 拒绝（一个任务绝不双启动）。
2. 请求 Luna → `luna_requires_ticket` 拒绝（Luna 只经票据竞争裁决，绝不被自动派生）。
3. **（仅默认 AUTO 且未明确绑定 `executor+model` 组合时）** 请求溢出 `Qwen3.8-Flash`：
   - 合格可用主力仍有空位 → `routing_required` 到某主力组合（默认 AUTO 主力优先，不无故用 Flash）；
   - 无合格可用主力且 Flash 有合格空槽 → 允许 claim Flash；
   - Flash 也满 → `capacity_full`。
4. **（仅默认 AUTO 且未明确绑定组合时）** 请求主力：在**合格可用**主力之间按 `committed` 计数轮换选
   目标——**committed 少者优先；平票时优先请求的组合**。
   - 目标就是当前入口且有容量 → claim；
   - 目标是**另一个**合格主力组合 → `routing_required`（当前入口不是被选中组合，**不先提交错
     模型、不浪费或重复占用名额**，改由被选中入口继续）；
   - 无合格可用主力而某区 Flash 有合格空槽 → 回退 `routing_required` 到同级 Flash；
   - 无合格可用主力时按两区 Flash **同级**选（`AUTO` 可跨国际/CN 选合格候选），任一区 Flash 仍有
     空槽即 `routing_required` 指向该区（调用入口已是该区 Flash 则直接 claim），**不因其中一区
     Flash 满就报整池 full**；
   - 各主力（`zcode` + 两内置 Max + CN 自定义 Max）与两区 Flash 均满（**总 10 全满**）才 `capacity_full`。**候选均无可用槽不等于物理满**，
     降级原因与是否授权按 §执行器约束 及后文区分处理。

轮换按**已提交计数**（`rotation.committed_zcode` / `committed_qoder`）驱动，是尽力而为的
1:1，不保证任意时刻两池在途数严格相等：历史提交、单侧释放都会造成短暂偏差，这是设计允许的。
文档不宣称严格均衡。`reserve` 与 `select_and_claim` 共用同一套统一国内策略（`_domestic_policy`）：
**默认 AUTO 不绕过 1:1 轮换**；但**受信任主脑明确给出的 executor/已授权 executor+model 组合是硬
约束**，优先于历史 committed 比例（见 §执行器约束 与 §B4 加固）——有空位的指定 Qoder/Flash 绝不因
比例被改道。

## 执行器约束与 availability 资格过滤（BW-AVAILABILITY-20261009-B2）

`select_and_claim`/`reserve`/`consume_for_entry`/`claim_due` 新增可选 `executor`
（`auto`|`qoder`|`zcode`，缺省 `auto`）与 `quota_store`/`quota_routes` 透传。**先过滤合格候选，
再在可用主力之间做 1:1**：

- **availability 资格过滤走 param-or-env 的正式默认持久源**：解析顺序为显式 `quota_store` →
  环境变量 `BRAIN_WORKER_QUOTA_STORE` → 受信任默认持久文件 `qc.default_store_path()`（即
  `~/.brain-worker/quota-state.sqlite3`），使**默认 AUTO 无需额外参数即过滤不可用 ZCode**。缺库/
  缺表按只读回核当作“无记录（可用）”，读取异常一律 **fail-closed**（当作被阻断，绝不因读失败放行）；
  解析所得路径**只读**、绝不建库/建表、绝不在 pool 事务里写 quota 库。（历史旧说明：“只在显式传入
  `quota_store` 时才过滤、未传不读真实目录”已由 BW-AVAILABILITY-20261009-B5 缺陷 5 统一为默认持久源。）
  传入时读 §3b 的 `zcode_availability`（[quota-routing](quota-routing.md)）：被阻断的 ZCode 通道
  从可用主力集合里剔除。
- **`executor='qoder'`（受信任主脑显式选择）**：有空位直接 claim `qoder:Qwen3.8-Max`，Max 满则按
  溢出策略改道 Flash；**绝不因历史 committed 计数被 1:1 改道到 ZCode**。请求组合与 executor 冲突
  （如 executor=qoder 却请求 zcode 组合）→ `executor_conflict` 拒绝。
- **`executor='zcode'`**：ZCode 被 availability 阻断 → `zcode_unavailable` 拒绝（**不改道到别的
  主力、不落 Flash**）；有空位则 claim，满则 `capacity_full`（无 Flash 回退）。
- **`executor='auto'`（缺省）**：保留历史 committed 计数与 1:1 公平轮换，只在**合格**主力间轮换；
  请求 ZCode 但其被阻断 → 改道到可用主力/同级 Flash；各主力与国际 Flash 都满而 CN Flash 有空槽 →
  改道 CN Flash（入口已是 CN Flash 则 claim），**两区 Flash 也满（总 10 全满）才 `capacity_full`**，**绝不改道回不可用的
  ZCode**，也**不改道回别的池去互换明确指定的组合**。**不清库、不重置轮换计数**、不新增评分/额度逻辑。
- **消费 token 前复检**：`consume_for_entry` 走 `--dispatch-claim` token 路径时，在同一事务里
  **重新校验 ZCode availability**；若预留后通道转为不可用，则释放**本次自己**的 reserved 占位
  （`reserved→start_failed`，`capacity_released=True`，reason `zcode_unavailable`），**不泄漏本任务
  的占位**、不误释放别人的名额。
- **默认 AUTO 下 Flash 只在无合格可用主力时**才用（用户明确指定已授权 Flash 组合时按其真实容量
  独立受控派发，不受此限）；`claim_due` 的国内 reclaim 同样用合格主力集合（被阻断的 ZCode
  不被重新选中）。

### B4 加固（BW-AVAILABILITY-20261009-B4）

- **combo 硬约束**：`executor`+`runtime`+`model` 明确组合（含 `qoder:Qwen3.8-Flash`）贯穿
  `reserve/select_and_claim/consume_for_entry/claim_due`，作为候选集合硬边界——受信任主脑明确
  已授权的组合**不因别的候选有空位而被拒绝**；默认 AUTO 主力优先/容量/1:1 与各池真实容量
  （BW-MAX-WINDOW-20261010-S2 后为六池总 10：`zcode`=2、两内置 Max 各 1、CN 自定义 Max=2、两区 Flash 各 2）
  为既定政策不变（并发 2/4 为 Skill 政策、非已核实 Qoder 进程上限）。
- **国内回收尊重原 scope**：`claim_due` 用 `_scope_allowed_candidates` 依原 scope 组合/executor
  收窄候选（绑 Qoder 只回收 QMax/Flash，绝不因 ZCode 有空位改派 ZCode）；绑 ZCode 且被阻断 →
  保持 pending，**绝不改派或降级 Luna**。
- **probe 走真实绑定票据**：容量门不接受自报 `probe=true`；`zcode_direct` 把 availability 授予的
  `probe_ticket`（provider/channel_key/attempt_id/epoch）经 `_probe_allowed`→
  `zcode_probe_ticket_valid` 只读核验后才放行唯一一次启动，读取异常 fail-closed（详见
  [quota-routing](quota-routing.md) §3c）。
- **只读诊断与统一默认**：pool 的 `_zcode_availability_blocked` 与状态查询改用只读连接（缺库/缺表
  不建、读错 fail-closed、绝不静默 healthy）；quota_store 走 param-or-env，使 AUTO 无需额外参数
  即过滤不可用 ZCode；派工写事务内不调用写额度 helper。
- **竞态与 Luna 保护**：429 事实**先于**可再派空槽发布（`_preclaim_reconcile` 要求 child+wrapper
  双死且创建身份匹配，auto 回收只放执行容量不清 availability）；`reply`/`claim_due` 保护
  claimed/settled/cancelled/launch_unknown，settled 票据不被迟到/重复回复重开；物理满（总 10）与
  “合格候选耗尽”分列报告，误报不自动 Luna。

## 入口在 Popen 前的唯一容量门

两个真实直连入口（Qoder/ZCode）在**创建输出目录、Popen 之前**必须消费或校验一个容量 claim，
**不能靠提示词或一个传入布尔跳过**（CodeBuddy/WorkBuddy 已退役为 human-relay，仅提示词人工
中继，不进池、不占名额）：

- `dp.consume_for_entry(store, task_id, runtime, model, workspace, prompt_sha256, ...)`：
  - 给了 `--dispatch-claim <token>` → 同一 `BEGIN IMMEDIATE` 事务内先精确校验（task/runtime/
    model/workspace/prompt_sha256/stage/chat_id 任一漂移即拒），通过则 CAS `reserved→running`
    并持有名额；重复/并发消费同一 token 只有一个 `allowed`（一槽绝不双 Popen）；
  - 没给 claim → `select_and_claim` 原子**自动**路由（统一国内策略：1:1 轮换 + 溢出 + 容量 +
    任务防重）；非被选中组合 → `routing_required`。
- `--dispatch-plan` 只用于 `execution_control.preflight` 的权限/工作区/依赖快照核验，**绝不**
  作为绕过 1:1 轮换或溢出改道的“确切组合”后门；携带计划者仍走同一条 `select_and_claim` 原子
  路由（旧的 `exact_combo` 旁路已废弃移除）。
- 任何非放行结果（`allowed=false`）→ 入口打印 `capacity_gate_rejected`/`sent:false`/
  `exit_code:2`（含 `routing_required` 与被选中组合），**零证据目录创建、零 Popen、不计轮次**。
- 路由选择**绝不把权限拒绝/登录缺失/额度限流伪装成容量溢出**：容量门只管并发名额；权限与
  额度分别由 `execution_control.preflight` 与 `quota_control.gate_dispatch` 独立裁决。
- **两条路径都必须消费/校验一个真实容量 claim**（`reserve`/`validate`/`select_and_claim` 都会在
  池里落一条在途 attempt），绝不靠提示词或一个传入布尔跳过；容量上限与任务防重在每条路径都强制。

`reserve` 供主脑/测试为**确切** `(runtime, model)` 预留一个确定 token，再交给对应入口用
`--dispatch-claim` 精确消费。`reserve` 与 `select_and_claim` 共用统一国内策略（1:1 轮换、溢出
改道、容量上限、任务防重、真实 workspace 单写入守卫），**不绕过轮换**：请求组合非轮转选中组合
时同样 `routing_required`。生产入口正常路径不带 `reserve` 语义——它只消费/校验或走
`select_and_claim`。

## 生命周期与释放口径

- **子进程真实结束立即释放容量名额**，不等报告绑定、不等业务验收：入口在子进程退出后调用
  `dp.finish(token, terminal='finished', success=<returncode==0>)`。
- **启动失败/取消/异常结束释放本 attempt**：准备阶段中止、Popen 抛错、子进程为 None →
  `finish(terminal='start_failed'|'cancelled')`。
- **存活未知不释放、不被抢占**：`KeyboardInterrupt`、communicate 异常、wrapper 死但子进程仍活
  （或存活未知）→ `mark_unknown`，名额继续占用，交 `reconcile` 后续对账。
- `finish` 只接受 `RELEASED_STATES = finished/start_failed/cancelled/reconciled_exit`；
  传 `unknown` 抛 `ValueError`（unknown 只能经 `mark_unknown`，语义是**保留**名额）。
- **PID 复用必须绑定创建时刻**：`bind_child` 记录真实子进程 PID 与创建身份（Windows 只读
  `GetProcessTimes`），`reconcile` 只有在 wrapper 死 + child 死 + 创建身份匹配时才
  `reconciled_exit` 释放；创建身份不匹配或未知一律保守 `unknown`，绝不按占位时间新旧抢占。
- **Qoder 派生的工具子进程不算另一个 worker**：只有经入口消费 claim 的顶层执行器占名额。

`reconcile` 只读对账，可用**可注入 prober**（测试注入假 PID 存活/创建身份，绝不真实探测进程）：

| 观测 | 处置 |
| --- | --- |
| 无 wrapper pid | `unknown` 保留（从未绑定，无法确认） |
| wrapper 仍活 | 保持；`reserved` 提升为 `running` |
| wrapper 死 + child 活 | `unknown` 保留（wrapper 死子活不抢占） |
| wrapper 死 + child 死 + 创建身份匹配 | `reconciled_exit` 释放 |
| wrapper 死 + child 死 + 创建身份不匹配/未知 | `unknown` 保留（PID 复用防护） |

## Luna 救援票据（300 秒等待 ≠ 默认无限授权）

Luna 是**救援**通道，无数量上限，但绝不无条件启动。流程：

1. 国内名额全满时（BW-QODER-CN-20261010-A2 起阈值随总 8 更新，BW-MAX-WINDOW-20261010-S2 加入 CN 自定义主力后再升为总 10，含两内置 Max/自定义 Max/CN 补充；判定走
   `_domestic_active ≥ DOMESTIC_TOTAL_CAPACITY`），**宿主先真的问用户**“外部 agent 还是 Luna”。Python 只落库票据，
   **绝不谎称已经问过或已经派生 Luna**——真实提问与原生工具调用由宿主负责。
2. `dp.ask_record(task_id, ...)` 记录一次真实提问票据，开始 **300 秒**计时
   （`LUNA_ASK_TIMEOUT_SECONDS`）。同一 task 只能有一个 `pending`/`claimed` 票据；**重启不重置
   deadline**（`asked_at_utc`/`deadline_utc` 持久化）。
3. 用户回复 → `dp.reply(task_id, choice)`（`luna`/`external_agent`/`domestic`/`cancel`）。
   **一旦回复，`claim_due` 不再自动裁决**——等待超时不当作默认无限授权。
4. 到期竞争裁决 `dp.claim_due(task_id, now)`：只有票据 `pending`、已过 deadline、未回复、未
   claim，**且国内总 10 名额（含自定义主力与 CN 补充候选）确实全满**（`_domestic_active ≥ DOMESTIC_TOTAL_CAPACITY`）时，
   才在同一 `BEGIN IMMEDIATE` 事务里创建一个 `luna:native`
   attempt 并把票据置 `claimed`，返回 `launch_state='host_must_call_native'`（**宿主仍须真实
   调用原生工具**，池只给竞争裁决与名额，不代跑）。
5. **国内名额释放优先国内、原子取消同 task 的 pending 救援票据**（不双派国内 + Luna）：
   `claim_due` 若在事务内发现国内已有空位 → 取消票据（`domestic_slot_freed`）；或宿主在等待
   期间主动 `dp.cancel_pending(task_id)`。
6. **非幂等窗口**：原生 Luna 工具已启动但在写出 agentID 前崩溃 → `dp.mark_launch_unknown(task_id)`
   记 `launch_unknown` 待人工核验，**绝不自动重复启动**。`claim_due` 对已 `claimed` 票据同样
   拒绝重复 claim。
7. Luna **沿用原任务的文件/命令/副作用范围**，不能借救援绕过权限拒绝、额度错误或部署审批。

### 回复状态机与降级票据（BW-AVAILABILITY-20261009-B2）

- **回复 `domestic`**：`claim_due` **任何时刻（0s/299s/300s/301s）都绝不启动 Luna**，只在票据
  scope 内等待国内空位或**原子 reclaim** 一个合格国内主力；**回复 `external_agent`** → 停止自动
  裁决、交人工外部交接；**回复 `cancel`** → 停止，票据终态，后续不复活。
- **重复 ask 绝不重置**已 `replied`/`cancelled`/`settled`/`claimed` 的原始回复、scope 或 deadline：
  `ask_record` 对**任何已存在票据**直接拒绝并回读 `state`/`deadline_utc`/`reply_choice`（纯 INSERT，
  无 `ON CONFLICT` 重置）；`pending` 不重置计时器。
- **票据 scope 损坏/缺失 → fail-closed**（`scope_corrupt=True`，绝不据损坏数据启动 Luna 或 reclaim）。
- **可用性不足的降级决策**：当**国内名额未满总 10（含自定义主力与 CN 补充候选）** 但因 availability 不足以正常派工时，允许一次新的
  降级 `ask_record`，须带**独立、客观的降级理由** `degradation_reason ∈ {quota, auth, capacity}` +
  `degradation_detail`；`quota` 理由必须由 §3b availability 的客观证据支撑
  （`_zcode_availability_blocked`），无证据则拒绝；`capacity` 理由必须经 `_scope_allowed_candidates`
  客观核验受信任候选（含 CN 补充候选）确已排空——仍有空槽（例如 CN 还有位）时**拒绝降级、优先用国内候选**。
  降级票据**绕过“总 10 全满”前提**，但
  **非 capacity 的超时绝不可替代显式授权**：只有用户显式回复 `luna` 才 claim Luna；等待超时
  ≠ 授权（`degraded && reply != luna` → 拒绝）。**总 10 全满 + 真实提问 + 300s + 用户授权**的既有规则不变。

## 与额度门禁的边界（quota ≠ concurrency）

- `dispatch_pool` 只管“同时几个真实执行器在跑”。
- `quota_control` 继续管 ZCode 的**人工额度策略**：不做真实余额查询、无历史 429 的 24 小时锁、
  无无限重试。ZCode 入口先过容量门拿到 `verified_pool_claim`，再把它交给
  `qc.gate_dispatch(..., verified_pool_claim=...)`；额度门拒绝时释放容量名额
  （`finish(start_failed)`）。
- **两个 ZCode workspace 走同一 provider**：经**已校验的容量 claim**支持（`_verify_pool_claim`
  只认 `runtime=='zcode'` 且 `dp.validate_claim(...)['ok']`）；同 workspace 仍单写在途、
  unknown/在途保护不变，不删通道行、不禁用门禁、不伪造 claim。旧 ZCode 通道在无 verified claim
  时保持单在途语义。
- **CodeBuddy 旧限流与旧占位完全不变**；CodeBuddy/WorkBuddy 只做完整提示词人工交接，不进容量池、
  不新增直连、生成提示词不记成已派发。

## CLI（宿主/主脑可真实操作，全部输出 JSON）

```text
python scripts/dispatch_pool.py --store <abs.sqlite3> status
python scripts/dispatch_pool.py --store <abs> reserve --task-id T --runtime zcode \
  --model GLM-5.3 --workspace <abs ws> --prompt-sha256 <64hex> [--stage S] [--token K] \
  [--executor auto|qoder|zcode] [--quota-store <db>] [--quota-routes <json>]
python scripts/dispatch_pool.py --store <abs> select-and-claim --task-id T --runtime qoder \
  --model Qwen3.8-Max --workspace <abs ws> --prompt-sha256 <64hex> \
  [--executor auto|qoder|zcode] [--quota-store <db>] [--quota-routes <json>]
python scripts/dispatch_pool.py --store <abs> validate --token K [--task-id T] [--runtime R] \
  [--model M] [--workspace W] [--prompt-sha256 H]
python scripts/dispatch_pool.py --store <abs> bind-child --token K --child-pid 1234
python scripts/dispatch_pool.py --store <abs> finish --token K --terminal finished --success true
python scripts/dispatch_pool.py --store <abs> reconcile
python scripts/dispatch_pool.py --store <abs> ask-record --task-id T [--scope S] \
  [--ask-message-id M] [--deadline-seconds 300] \
  [--degradation-reason quota|auth|capacity --degradation-detail D] \
  [--quota-store <db>] [--quota-routes <json>]
python scripts/dispatch_pool.py --store <abs> reply --task-id T --choice external_agent [--note N]
python scripts/dispatch_pool.py --store <abs> claim-due --task-id T \
  [--quota-store <db>] [--quota-routes <json>]
python scripts/dispatch_pool.py --store <abs> mark-launch-unknown --task-id T
python scripts/dispatch_pool.py --store <abs> cancel-pending --task-id T [--reason R]
```

默认 store 为 `~/.brain-worker/dispatch-pool.sqlite3`（同一用户的安装版/仓库版/不同 cwd 共用，
保证真正跨会话共享）；环境变量 `BRAIN_WORKER_DISPATCH_STORE` 覆盖；`--store` 显式覆盖。
**测试必须传显式临时 store**，绝不写真实池。所有裁决在 `BEGIN IMMEDIATE` 事务内原子完成，
跨进程/跨会话/跨 cwd 一致。

## 迁移边界

- 生产 `scripts/codebuddy_direct.py` **已删除可执行传输路径**（不再 `import subprocess`，
  `dispatch_core` 移除），只保留纯解析/终态诊断/历史取证函数；`main()` 固定拒绝
  （`manual_relay_only`/`sent:false`/退出 2）。真实传输管线整体迁到**仅测试**的
  `tests/offline_codebuddy_harness.py::replay_dispatch`，供离线回放，不进生产、无新增生产开关。
- 离线回放 harness **禁止真实 node、禁止任意 stub 目录里的真实 CLI、禁止生产默认用户配置**：
  只认 `sys.executable`（realpath 相等）、仓库可信的固定合成 stub（首行 marker
  `# OFFLINE-SYNTHETIC-CODEBUDDY-STUB v1` + 声明的内容 SHA-256），任一不符即拒绝。
- Qoder/ZCode 直连保留并接入容量池。ZCode 的 argparse 缺省模型为 `GLM-5.3`（对齐主力组合
  `zcode:GLM-5.3`）。Qoder 的 argparse 缺省模型**维持历史值 `Qwen3.8-Flash`**：现有非本轮编辑
  白名单内的控制面回归 `tests/test_execution_control.py` 绑定一份 `model=Qwen3.8-Flash` 的
  dispatch-plan 且不传 `--model`，若把缺省翻成 Max 会令该计划在 `preflight` 的 model 逐项比对处
  被拒，而该文件不在 S4 白名单、无法就地改。`Qwen3.8-Max` 仍是**合法可显式请求**的 `--model`、
  且是池指定的 qoder **主力**组合（新派工流程与池测试都显式 `--model Qwen3.8-Max`）；缺省不传
  `--model` 的自主 qoder 派工仍请求 Flash（溢出），由容量池按溢出策略 `routing_required` 改道到
  主力。若主脑希望把名义缺省也翻成 Max，需把 `tests/test_execution_control.py` 纳入白名单同步更新
  其 plan 模型。

## 已知不足

- 容量池是 `sqlite3` 单文件持久锁，不是分布式协调服务；跨机器不共享（同一用户同一机器跨
  cwd/版本共享即达标）。
- 1:1 轮换是 best-effort，短暂偏差不自动纠正（见上）。
- Luna 的真实提问、原生工具调用、agentID 写回全部依赖宿主；池只持久化票据与竞争裁决，
  无法证明宿主真的问了或真的启动了——`launch_unknown` 就是对这一非幂等窗口的保守兜底。
- Windows 之外平台拿不到进程创建时刻（`process_identity.created` 为 `None`），`reconcile`
  对死进程的 PID 复用防护退化为保守 `unknown`（宁可保留名额也不误抢）。


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

## 11. 六池真实分配与主力明确限额（2026-10-10，BW-POOL-SPLIT-20261010-S2/S3/S5 + BW-MAX-WINDOW-20261010-S2）

S2/S3 定型的国内真实容量经 BW-MAX-WINDOW-20261010-S2 加入 CN 自定义主力后为六池：`zcode:GLM-5.3=2`、国际内置 `qoder:Qwen3.8-Max=1`、CN 内置 `qodercn:Qwen3.8-Max=1`、CN 自定义 `qodercn:Qwen-3.8-Max=2`、`qoder:Qwen3.8-Flash=2`、`qodercn:Qwen3.8-Flash=2`；`qodercn:DeepSeek-Flash` 退休为 0（`LEGACY_RETIRED_POOL_KEYS`）但既有在途 attempt 仍计入 `_domestic_active` 总数、真实终态自然释放；`luna:native` 无上限、仅救援。`DOMESTIC_TOTAL_CAPACITY=10` 硬守卫不派第 11 个。CN 内置模型真实名 `Qwen3.8-Max`/`Qwen3.8-Flash`（**不是** `Qwen-3.8-*`）；自定义主力用带连字符的友好名 `Qwen-3.8-Max` 与内置区分。

S5 增加 Qoder Max 池的**明确限额**闭环（BW-MAX-WINDOW-20261010-S2 把自定义 CN 主力并入承载范围）：
- **限额承载范围**：`QUOTA_LIMITABLE_POOLS = (qoder:Qwen3.8-Max, qodercn:Qwen3.8-Max, qodercn:Qwen-3.8-Max)`。自定义 CN 主力复用同一标记机制，各自 pool_key 独立落标、对两内置 Max 无连带。ZCode 仍走 `quota_control` availability；任一 Flash、退休 DeepSeek、Luna 永不落标；绝不误挡 Flash、绝不动 ZCode 既有 availability 口径。
- **落标必须绑真实 attempt + 错误证据**：`record_main_force_limit(store, pk, task_id=…, attempt_token=…, evidence_path=…, evidence_sha256=…)` 在 `BEGIN IMMEDIATE` 事务内回核 attempt 行存在、`pool_key==pk`、`task_id==传入`；同时校验 `evidence_path` 指向的文件真实存在且 `_file_sha256` 与声明一致。任一漂移或缺字段 → `recorded=False / drift=True` 拒绝写入。同池重复落标只刷新证据/时间戳，清空旧的 `released_at/release_note`。
- **consume 前查限额**：`consume_for_entry` 在同一事务内、`_workspace_conflict` 之前，若 `row['pool_key']` 命中 `main_force_limits.limited=1` → 只把**本 token** CAS `reserved→start_failed`（`capacity_released=True`），返回 `sent=False / allowed=False / reason='main_force_limited'`，**绝不泄漏本次占位、绝不越权释放其他 task 的 reserved**。人工 `release_main_force_limit(note=…)` 后同 reserved 可正常 consume 到 running。
- **恢复只人工**：`release_main_force_limit` 只在用户明确额度恢复/重置后调用，附 `note`；绝不查余额、不加定时器/探针/评分/新服务、不靠成功旧日志自动清标记；不改 `quota_control.py`/`zcode_direct.py`。
- **AUTO/executor 路由保留**：`executor='qoder'` 请求 QMax 且未落标 → 直接 claim；已落标或已满 → 同 executor 内 Flash 兜底（`routing_required` 选 `qoder:Qwen3.8-Flash`），绝不复活限额 Max、绝不跨地区换 CN。`executor='qodercn'` 同规则镜像 CN Max→CN Flash。Luna 侧不变：domestic 任何时刻都不启用 Luna、unknown 保占位、明确模型不偷偷换。
- **qoder_direct 落标时序**：真实失败终态（`protocol_success=False`）经 `_explicit_quota_limit_hit` 命中 + `_qoder_max_pool_key` 返回非 None → **先** `record_main_force_limit`（写 `stdout.json` 原字节 sha256 为证据）→ **再** 真实终态释放；落标异常绝不阻断真实终态释放（不泄漏锁）。
- **离线回归覆盖**：`tests/test_dispatch_pool.py::MainForceLimitTests`（含自定义 CN 主力 record/read/release、consume 只释放本次 reserved、对两内置 Max 无连带）、`tests/test_qoder_cn_direct_offline.py::ExplicitQuotaLimitHitTests`、`tests/test_dispatch_pool_qodercn.py`（六池容量/退休 DeepSeek `unknown_pool`）、`tests/test_availability_routing.py`（回收/降级 combo 硬约束）、`tests/test_parallel_execution.py`（并发演练 qa Max + qb Flash）。Windows/Linux CI 矩阵都跑，零网络、零凭据、零真实额度查询。
