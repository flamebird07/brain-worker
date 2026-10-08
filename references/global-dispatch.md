# 全局并发容量池与 1:1 路由（global-dispatch）

> 本文件描述 brain-worker 跨会话共享的**并发容量池**：`scripts/dispatch_pool.py`（纯标准库
> `sqlite3`，**无常驻调度服务**）。它解决的是“同一时刻全系统允许几个真实执行器在跑”，与
> `quota_control` 的**额度冷却门禁**是两回事：容量并发 ≠ 额度。落地口径以本文件为准，实现以
> `scripts/dispatch_pool.py` 为准，二者不一致时以代码为准并回修文档。

## 分配决策（用户 2026-10-08 最终决定）

所有会话共享同一个持久池，绝不各自为政：

| 角色 | 组合（`runtime:model`） | 每组合真实在途上限 | 说明 |
| --- | --- | --- | --- |
| 主力 | `zcode:GLM-5.3` | 2 | 与 Qoder 主力 **1:1** 轮换 |
| 主力 | `qoder:Qwen3.8-Max` | 2 | 与 ZCode 主力 **1:1** 轮换 |
| 溢出 | `qoder:Qwen3.8-Flash` | 2 | **只有两主力池都满**才允许使用 |
| 救援 | `luna:native` | 无上限 | 只经 `claim-due` 竞争裁决，绝不被自动选中 |

- **国内合计上限 = 6**（`DOMESTIC_TOTAL_CAPACITY`）：主力 2×2 + 溢出 2。
- **Luna 无数量上限**，但它不计入国内名额，也永远不在正常路由里被派生。
- 未列入本表的组合（例如 `qoder:GLM-5.3`、`zcode:Qwen3.8-Max`、任意未授权模型）容量为 0，
  `reserve`/`select_and_claim` 一律以 `unknown_pool` 拒绝，绝不派发未授权组合。

## 1:1 主力轮换是 best-effort，不是严格均衡

`select_and_claim` 在无显式 claim 时做**原子路由选择**：

1. 同 `task_id` 已有在途 attempt → `duplicate_task_in_flight` 拒绝（一个任务绝不双启动）。
2. 请求 Luna → `luna_requires_ticket` 拒绝（Luna 只经票据竞争裁决）。
3. 请求溢出 `Qwen3.8-Flash`：
   - 主力仍有空位 → `routing_required` 到某主力组合（主力有空不用 Flash）；
   - 两主力都满且 Flash 有空位 → 允许 claim Flash；
   - Flash 也满 → `capacity_full`（六名额全满）。
4. 请求主力：按 `committed` 计数轮换选目标——**committed 少者优先；平票时优先请求的组合**。
   - 目标就是当前入口且有容量 → claim；
   - 目标是**另一个**主力组合 → `routing_required`（当前入口不是被选中组合，**不先提交错
     模型、不浪费或重复占用名额**，改由被选中入口继续）；
   - 两主力都满 → 有空位则 `routing_required` 到溢出 Flash，否则 `capacity_full`。

轮换按**已提交计数**（`rotation.committed_zcode` / `committed_qoder`）驱动，是尽力而为的
1:1，不保证任意时刻两池在途数严格相等：历史提交、单侧释放都会造成短暂偏差，这是设计允许的。
文档不宣称严格均衡。`reserve` 与 `select_and_claim` 共用同一套统一国内策略（`_domestic_policy`），
**都不绕过轮换**。

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

1. 六个国内名额全满时，**宿主先真的问用户**“外部 agent 还是 Luna”。Python 只落库票据，
   **绝不谎称已经问过或已经派生 Luna**——真实提问与原生工具调用由宿主负责。
2. `dp.ask_record(task_id, ...)` 记录一次真实提问票据，开始 **300 秒**计时
   （`LUNA_ASK_TIMEOUT_SECONDS`）。同一 task 只能有一个 `pending`/`claimed` 票据；**重启不重置
   deadline**（`asked_at_utc`/`deadline_utc` 持久化）。
3. 用户回复 → `dp.reply(task_id, choice)`（`luna`/`external_agent`/`domestic`/`cancel`）。
   **一旦回复，`claim_due` 不再自动裁决**——等待超时不当作默认无限授权。
4. 到期竞争裁决 `dp.claim_due(task_id, now)`：只有票据 `pending`、已过 deadline、未回复、未
   claim，**且六个国内名额确实全满**时，才在同一 `BEGIN IMMEDIATE` 事务里创建一个 `luna:native`
   attempt 并把票据置 `claimed`，返回 `launch_state='host_must_call_native'`（**宿主仍须真实
   调用原生工具**，池只给竞争裁决与名额，不代跑）。
5. **国内名额释放优先国内、原子取消同 task 的 pending 救援票据**（不双派国内 + Luna）：
   `claim_due` 若在事务内发现国内已有空位 → 取消票据（`domestic_slot_freed`）；或宿主在等待
   期间主动 `dp.cancel_pending(task_id)`。
6. **非幂等窗口**：原生 Luna 工具已启动但在写出 agentID 前崩溃 → `dp.mark_launch_unknown(task_id)`
   记 `launch_unknown` 待人工核验，**绝不自动重复启动**。`claim_due` 对已 `claimed` 票据同样
   拒绝重复 claim。
7. Luna **沿用原任务的文件/命令/副作用范围**，不能借救援绕过权限拒绝、额度错误或部署审批。

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
  --model GLM-5.3 --workspace <abs ws> --prompt-sha256 <64hex> [--stage S] [--token K]
python scripts/dispatch_pool.py --store <abs> select-and-claim --task-id T --runtime qoder \
  --model Qwen3.8-Max --workspace <abs ws> --prompt-sha256 <64hex>
python scripts/dispatch_pool.py --store <abs> validate --token K [--task-id T] [--runtime R] \
  [--model M] [--workspace W] [--prompt-sha256 H]
python scripts/dispatch_pool.py --store <abs> bind-child --token K --child-pid 1234
python scripts/dispatch_pool.py --store <abs> finish --token K --terminal finished --success true
python scripts/dispatch_pool.py --store <abs> reconcile
python scripts/dispatch_pool.py --store <abs> ask-record --task-id T [--scope S] \
  [--ask-message-id M] [--deadline-seconds 300]
python scripts/dispatch_pool.py --store <abs> reply --task-id T --choice external_agent [--note N]
python scripts/dispatch_pool.py --store <abs> claim-due --task-id T
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
