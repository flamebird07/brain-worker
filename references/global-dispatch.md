# 全局并发容量池与 1:1 路由（global-dispatch）

CN 自定义 Max 恢复声明（BW-CUSTOM-MAX-RESUME-20261018-S1）：用户已于 2026-10-18 明确解除暂停，`qodercn:Qwen-3.8-Max`（Qoder CN自定义、带连字符）恢复正式授权候选；主脑新阶段授权集合恢复精确 `qodercn:Qwen-3.8-Max`。当前配置总 13 槽（BW-CODEBUDDY-MAIN-S3 计入 `codebuddy:deepseek-v4.1-flash` 1 槽），授权可新派上限恢复 13，不新增槽、自定义仍 2 槽；既有在途/unknown 继续占位直至真实终态自然释放，不终止或抢占。历史事实：2026-10-15 至恢复前曾按 BW-CUSTOM-MAX-PAUSE-20261015 暂停该组合，当时授权可新派上限为 11；该暂停只是授权候选软排除，不存在需要清理的真实 DB 暂停锁，本次不清真实池/历史限额/在途。恢复不等于认证或额度现场已验证：不查询余额、不探测模型。内置`qodercn:Qwen3.8-Max`、国际Max、两区Flash、CodeBuddy精确模型及其他既有规则不变。

每轮按优先级重试（BW-SIMPLE-PRIORITY-RETRY-S3，覆盖下方旧的「availability/限额先剔除」叙述）：每个新调度轮都从时段/容量/权限/授权与明确停用仍合格的**最高优先级**候选起真实尝试；同一 `runtime:model` 本轮最多一次，真实失败才在本轮换下一个，下一新轮重新从最高优先级起。`dispatch_pool` 的 AUTO 普通候选集合不再据历史 ZCode availability 或三 Qoder Max/Claude `main_force_limits` 剔除（`_eligible_main_forces`/`_domestic_policy` 只保留时段/容量/总守卫/工作区/unknown/权限门）；历史标记仅作诊断，真实尝试失败才落库、成功仍 epoch/attempt CAS，不假装 healthy、不清旧 hold、不伪造成功，普通入口无需 recovery probe。显式 `executor='zcode'` 权威原子门与 `purpose='probe'` 生命周期保持原样兼容。本轮失败列表由主脑经 `stage_selection` 的 authorization JSON（`round_failed_candidates`）维护，仅作本轮内存过滤、绝不落新库/定时器/自动监控，也不扩大 `allowed_candidates` 授权集合；当轮所有已授权候选都试过则诚实无候选、不再派。


> 本文件描述 brain-worker 跨会话共享的**并发容量池**：`scripts/dispatch_pool.py`（纯标准库
> `sqlite3`，**无常驻调度服务**）。它解决的是“同一时刻全系统允许几个真实执行器在跑”，与
> `quota_control` 的**额度冷却门禁**是两回事：容量并发 ≠ 额度。落地口径以本文件为准，实现以
> `scripts/dispatch_pool.py` 为准，二者不一致时以代码为准并回修文档。

## 分配决策（当前有效，BW-POOL-SPLIT-20261010-S2/S3 + BW-MAX-WINDOW-20261010-S2 六池定型 + BW-CLAUDE-DEEPSEEK-S3/S4 + BW-CLAUDE-CAPACITY2 claude 扩为 2 槽 + BW-MAIN-PRIORITY-20261011-S1 + BW-CODEBUDDY-MAIN-S3 CodeBuddy 精确模型恢复为最高优先）

所有会话共享同一个持久池，绝不各自为政：

| 角色 | 组合（`runtime:model`） | 每组合真实在途上限 | 说明 |
| --- | --- | --- | --- |
| 主力（最高优先，BW-CODEBUDDY-MAIN-S3） | `codebuddy:deepseek-v4.1-flash` | 1 | 独立 CodeBuddy CLI 的**唯一**恢复模型（界面名 Deepseek-V4.1-Flash）；优先级层=0（高于全部 Qwen3.8-Max），**不受任何时段门**（既不套 Claude 离峰窗、也不套 Max 北京夜间窗）；仅经正式入口 `codebuddy_live` 传输；无跨模型回退；其它 CodeBuddy 旧模型与 WorkBuddy 仍 human-relay |
| 主力 | `zcode:GLM-5.3` | 2 | 优先级层=3（最后，BW-MAIN-PRIORITY-20261011-S1）：CodeBuddy、三 Qoder Max、`claude:deepseek-flash` 全部不合格或无空位才轮到；旧跨层 committed 1:1 属历史、已被覆盖 |
| 主力 | `qoder:Qwen3.8-Max` | 1 | 国际 Max 主力；明确限额命中时经 `main_force_limits` 自动跳过，不查余额；**BW-MAX-WINDOW-20261010-S1：作为主力仅北京时间 22:00（含）至次日 08:00（不含）参与新派工，其余时段 AUTO 自动跳过、主脑显式 `reserve` 如实拒绝** |
| 主力 | `qodercn:Qwen3.8-Max` | 1 | CN 内置 Max 主力；独立池，不受国际 Max 标记影响，限额规则同国际 Max；**BW-MAX-WINDOW-20261010-S1：主力时段门同国际 Max（北京时间 22:00 含至 08:00 不含）** |
| 主力 | `qodercn:Qwen-3.8-Max` | 2 | CN 自定义主力（友好名带连字符 `Qwen-3.8-Max`，与内置 `Qwen3.8-Max` 不同）；属 Qoder 主力组，优先级层=1；**BW-CUSTOM-MAX-RESUME-20261018-S1：Max 层内子优先键先于两内置 Max（在途数之前），自定义满 2 槽才落内置；两内置同级按真实在途数平衡；用户已于 2026-10-18 解除暂停、恢复授权候选（历史暂停仅属当时状态）**；**BW-CUSTOM-MAX-NIGHT-20261010-S1：纳入主力时段门（作为主力仅北京时间 22:00 含至次日 08:00 不含参与新派工，其余时段 AUTO 自动跳过、主脑显式 `reserve`/`consume` 如实拒绝），并复用 `main_force_limits` 限额判定，各自 pool_key 独立落标、对两内置 Max 无连带** |
| 兜底 | `qoder:Qwen3.8-Flash` | 2 | 与 CN Flash **同级**；`AUTO` 在合格可用主力均不可用或已满时从两区 Flash 选（可跨国际/CN），明确 `executor='qoder'` 时按本地区约束 |
| 兜底 | `qodercn:Qwen3.8-Flash` | 2 | 与 `qoder:Qwen3.8-Flash` 同级；`AUTO` 可跨区选，明确 `executor='qodercn'` 时按本地区约束 |
| 退休 | `qodercn:DeepSeek-Flash` | 0 | 新派发退休（`LEGACY_RETIRED_POOL_KEYS`，`_domestic_policy` 返 `unknown_pool`），仅供已存在旧在途 attempt 按真实 runtime/model 计入总数并真实终态释放 |
| 主力（claude 2 槽，BW-CLAUDE-DEEPSEEK-S2/S3/S4 + BW-CLAUDE-CAPACITY2） | `claude:deepseek-flash` | 2 | Claude Code CLI / deepseek-flash：容量 2（BW-CLAUDE-CAPACITY2 由 1 扩为 2），**已登记入 `MAIN_FORCE_POOLS`/`MAIN_FORCE_KEYS`/`MAIN_FORCE_CAPACITY`**，`DOMESTIC_TOTAL_CAPACITY = 13`（BW-CODEBUDDY-MAIN-S3 计入 CodeBuddy 1；**原 12 槽一项未减**），claude 在途**计入** `_domestic_active`；属**独立组 claude**（committed 由 `_count_committed_claude` 从真实 attempts 统计，绝不污染 committed_zcode/committed_qoder），**AUTO 可选**；BW-CODEBUDDY-MAIN-S3 当前口径：优先级层=2，排 CodeBuddy（层=0）与三 Qoder Max（层=1）之后、`zcode:GLM-5.3`（层=3）之前——Max 全不可用时 claude 允许窗且有空位即优先于 Z（旧“平票排 Z2/Qoder 之后”属历史口径、已被覆盖）；显式 `executor='claude'`/点名该 combo 保持精确组合、无跨模型回退；受 `deepseek_offpeak` 时段门约束（工作日 09:00–12:00、14:00–18:00 高峰不得新派；其余/周末/2026 假日全天可派）；可承载 `main_force_limits` 明确限额标记 |
| 救援 | `luna:native` | 无上限 | 只经 `claim-due` 竞争裁决，绝不被自动选中 |

- **国内合计上限 = 13**（`DOMESTIC_TOTAL_CAPACITY = 主力 9（codebuddy 1 + zcode 2 + 两内置
  Max 各 1 + CN 自定义 2 + claude 2）+ 两地区 Flash 各 2（4）= 13`，`_domestic_active ≥ 13`
  时任何新国内 claim 一律 `full`、**绝不派第 14 个**，仍在途的退休 DeepSeek 与超额旧 Max
  等迁移债务同样计入总数）：
  `codebuddy:deepseek-v4.1-flash`=1 + `zcode:GLM-5.3`=2 + 国际内置 Max=1 + CN 内置 Max=1 +
  CN 自定义主力=2 + `claude:deepseek-flash`=2 + 国际 Flash=2 + CN Flash=2。退休 DeepSeek
  新派发不占额度但既有在途仍计入总数。
  **配置总数 13，授权可新派上限 13**（BW-CUSTOM-MAX-RESUME-20261018-S1 已解除
  CN 自定义 Max 暂停；历史暂停期曾为授权上限 11，属当时状态）；配置容量与既有在途不变。
  **原 12 槽一项未减**。（历史：本表旧版曾记为“国际 Max 各 2 + CN DeepSeek 补充 2、合计 6→8”，
  S2/S3 收为五池总 8，BW-MAX-WINDOW-20261010-S2 加入 CN 自定义主力升为六池总 10，
  BW-CLAUDE-DEEPSEEK-S3/S4 再加 claude 主力槽升为总 11，BW-CLAUDE-CAPACITY2 将 claude 扩为
  2 槽升为总 12；BW-CODEBUDDY-MAIN-S3 计入 CodeBuddy 1 槽升为总 13，详见 §11。以上历史
  数字均被当前 13/13 口径覆盖；2026-10-15 至 2026-10-18 的暂停期曾为 13/11。）
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
  Max 从合格主力里剔除，`_select_main_force` 只在剩余合格主力里按优先级层排序（BW-MAIN-PRIORITY-
  20261011-S1：`claude:deepseek-flash`（层=1，offpeak 允许窗且有空位时）先于 `zcode:GLM-5.3`
  （层=2）），claude 与 ZCode 也都不
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
- 活任务不追杀：本门只影响**新** `reserve`/`consume`/`claim_due`，不改静态总容量 12、不弱化
  capacity/锁/限额，已 running/unknown 占位与真实终态释放逻辑完全不变。不加定时器/余额查询/
  数据库表/后台任务。

## Claude Code / deepseek-flash 2 槽（历史第 11–12 槽编号；BW-CLAUDE-DEEPSEEK-S2/S3/S4 + BW-CLAUDE-CAPACITY2，现行口径）

客户 2026-10-11 追加：新增 claude 主力槽 `claude:deepseek-flash`（Claude Code CLI，经 DeepSeek
Anthropic 兼容入口 `https://api.deepseek.com/anthropic`，精确模型 `deepseek-flash`；S3/S4 初为 1 槽，
BW-CLAUDE-CAPACITY2 扩为 2 槽）。可派工槽位由 10 增至 **11**、再由 BW-CLAUDE-CAPACITY2 增至 **12**，
**原 10 槽一项未减**；S3/S4 现行口径是 claude **登记入主力体系**、
作为**独立组**参与 AUTO（BW-MAIN-PRIORITY-20261011-S1 起优先级层=1），而不是把旧池缩小：

- **容量与公平组**：`CAPACITY['claude:deepseek-flash']=2`（BW-CLAUDE-CAPACITY2 由 1 增至 2），且进入 `MAIN_FORCE_POOLS`/
  `MAIN_FORCE_KEYS`/`MAIN_FORCE_CAPACITY`，故 `DOMESTIC_TOTAL_CAPACITY=12`；`_domestic_active`
  统计**所有非 Luna 在途 attempt**（含 claude 与仍在途的退休 DeepSeek/超额旧 Max 迁移债务），
  达 13 时任何新国内 claim 一律 `full`、**绝不派第 14 个**。committed 计数用**独立**统计
  `_count_committed_claude`（真实 attempts，`child_pid NOT NULL`），**绝不污染**
  `committed_zcode`/`committed_qoder`；BW-MAIN-PRIORITY-20261011-S1 当前口径：claude 优先级层=1
  （三 Qoder Max=0 之后、ZCode=2 之前），旧 `_grp_rank` 平票排 Z2/Qoder 之后的跨层 1:1 属历史、
  已被覆盖；历史 committed 计数照旧保留记账，**原 10 槽的容量/时段/限额/Luna 阈值语义不变**。
- **AUTO 可选但不绕统一选择**：AUTO 的 claude 入口请求**经同一权威 `_select_main_force` 优先级排序**
  （`_eligible_main_forces` 先按真实空位 + 时段门 + 限额过滤），**不绕过统一选择**；
  显式 `executor='claude'` 或点名 `claude:deepseek-flash` 组合走**精确组合**硬约束，
  绝不跨到其它池、**无跨模型回退**；`executor` 与请求组合冲突 → `executor_conflict`。
- **`_domestic_policy` claude 分支顺序（S4 收口 + BW-CLAUDE-CAPACITY2）**：`executor_conflict` →
  **总 13 守卫先判**（`_domestic_active ≥ 13` → `full`，迁移债务满 13 时即便 claude 自身有空位
  也绝不派第 14）→ 时段门（`main_force_window_closed`）→ 限额（`full`）→ 本池空位 `claim` →
  本池满 `full`。
- **时段门（`deepseek_offpeak.py`，北京时间固定 UTC+8）**：周一至周五除去中国法定节假日，
  09:00（含）–12:00（不含）、14:00（含）–18:00（不含）为**高峰，不得新派**；其余时段、周末、
  2026 已核对假日全天可派。周末只看星期几（调休周六仍属空闲、绝不当高峰）。**12:00 是上午峰的
  右开边界**，12:00–14:00 午休与 18:00 后属可派时段——测试与验收不得把 12:00 当峰值。
  **未知年度**无法确认工作日高峰是否为假日 → 高峰保守关闭，offhours/weekend 仍放行。门由
  `_max_window_blocks` 统一实现（`offpeak` 缺失时 fail-closed），AUTO/reserve/consume/claim_due
  口径一致。
- **跨高峰真实释放**：`consume_for_entry` 消费已预留 token 前若已进入高峰，只把**本任务自己的**
  `reserved` 在同一事务内真实释放为 `start_failed`，绝不启动、绝不抢占别人名额，
  `running`/`unknown` 绝不假释放——与三 Qoder Max 夜间门同一机制、同一段拒绝/释放代码路径。
- **限额标记复用**：claude 纳入 `QUOTA_LIMITABLE_POOLS`，仅在**同一条真实错误条目内绑定的**
  结构化 429/硬限额证据下 `record_main_force_limit` 落标，`main_force_limits` 自动跳过新派、
  绝不查余额；无证据不自动清标，恢复只由人工 `release_main_force_limit`。取消退出码
  （`4294967295`/`-1`）与 SIG **绝不判为 429**。
- **入口与生命周期**：`scripts/claude_direct.py` 运行时 `claude`，`-p --output-format stream-json
  --verbose`、`--permission-mode dontAsk`（任何其它权限模式被 `build_argv` 直接拒绝，
  **绝不 bypass/yolo**）、逐项 `--tools`/`--allowedTools`（**默认不开 Bash**——未通过精确只读
  自动批准验收故默认不开放）、stdin 传任务、`--append-system-prompt` 附完整九节契约、
  `--max-turns`；**`--bare` 被拒**（官方 Node CLI 2.1.76 不支持）；模型/权限模式/base_url
  与钉死值不同一律 `ValueError`。子进程 env 把 `ANTHROPIC_MODEL` 与 default
  opus/sonnet/haiku/subagent 全部钉死 `deepseek-flash`（禁止回退 Claude/pro），
  `CLAUDE_CODE_SIMPLE` **绝不设置**。真实错误提取：`is_error=true` 时即使 `subtype=success`
  也从 result 字符串取真实 API 错误；`errors` 非空或 `parse_error` ⇒ 绝不
  `protocol_success`；`tool_use.id`↔`tool_result` 真实配对，**成功回执 ≥1** 才算工程成功。
  生命周期：预检失败 ⇒ **零 Popen**；`consume_for_entry`→`mark_running`（真实 PID）→
  `bind_child`（**`bound` 必须显式 `true`**）→**无条件**真实终态释放并保留真实释放结果，
  已启动但退出未知保留 `launch_unknown`。凭据只进子进程 env（token 环境变量或 Windows DPAPI
  密文经官方宿主 `powershell`/`pwsh` 解密），绝不进 argv/request/异常/证据；私有配置
  `claude-entry.local.json` 由 root 本机持有，worker 工具拒绝读取。
- **真实世界事实（与离线证明分开）**：**官方 Node CLI 2.1.76 真实 Read/Write 已成功**
  （前轮 `error_max_turns`、原件保留无报告，独立记录、不等同验收通过）；
  **native 2.1.220/296 连接失败单列**，不与 Node 事实混写；**费用未知**——CLI 估算 ≠ 账单。
  **真实一次新入口 Read/Write 验收由主脑（root）执行，worker 不调 API。**

> 状态：本槽与入口已纳入 `python -m unittest discover -s tests -p "test_*.py"` 离线回归
> （全套通过、含 1 项平台跳过；确切用例数以最近一次原样运行与 SKILL.md 为准），
> 零网络、零凭据、零真实额度查询、零真实模型启动。

## 主力优先级排序 CodeBuddy > Max > Claude > GLM（BW-CODEBUDDY-MAIN-S3 覆盖 BW-MAIN-PRIORITY-20261011-S1 的旧层序）

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
4. **（仅默认 AUTO 且未明确绑定组合时）** 请求主力：在**合格可用**主力之间按**优先级层**排序选
   目标（BW-CODEBUDDY-MAIN-S3）——**codebuddy:deepseek-v4.1-flash（层=0，最高、无时段门）
   最优先，三 Qwen-3.8-Max（国际内置/CN 内置/CN 自定义，层=1）次之，`claude:deepseek-flash`
   （层=2）再次，`zcode:GLM-5.3`（层=3）最后**；Max 层内 CN 自定义 `qodercn:Qwen-3.8-Max`
   子优先于两内置 Max（该子键在在途数之前），自定义满 2 槽才落内置，两内置同级按真实在途数
   平衡；历史 committed 计数、请求池偏好与在途数**绝不能**让低层抢高层，最高层全部不合格或
   无空位才落下一层；**同层内真实在途数少者优先，平票才优先请求的组合，再按 pool_key stable tie**。
   - 目标就是当前入口且有容量 → claim；
   - 目标是**另一个**合格主力组合 → `routing_required`（当前入口不是被选中组合，**不先提交错
     模型、不浪费或重复占用名额**，改由被选中入口继续）；
   - 无合格可用主力而某区 Flash 有合格空槽 → 回退 `routing_required` 到同级 Flash；
   - 无合格可用主力时按两区 Flash **同级**选（`AUTO` 可跨国际/CN 选合格候选），任一区 Flash 仍有
     空槽即 `routing_required` 指向该区（调用入口已是该区 Flash 则直接 claim），**不因其中一区
     Flash 满就报整池 full**；
   - 各主力（CodeBuddy 1 + `zcode` 2 + 两内置 Max + CN 自定义 Max 2 + claude 2）与两区 Flash
     均满（**总 13 全满**；授权可新派上限 13）才
     `capacity_full`。**候选均无可用槽不等于物理满**，
     降级原因与是否授权按 §执行器约束 及后文区分处理。

排序第一键是**优先级层**（时段受限的三 Qoder Max 在允许时段优先排活），同层内按真实在途数
best-effort 平衡，不保证任意时刻同层各池在途数严格相等：单侧释放会造成短暂偏差，这是设计允许的。
历史 committed 计数（`rotation.committed_zcode` / `committed_qoder` 与 `_count_committed_claude`）
**照旧保留记账，但不再驱动跨层轮换**——旧跨层 best-effort 1:1（committed 少者组优先、claude 平票排
Z2/Qoder 之后）属历史行为，已由 BW-MAIN-PRIORITY-20261011-S1 明确覆盖。
文档不宣称严格均衡。`reserve` 与 `select_and_claim` 共用同一套统一国内策略（`_domestic_policy`）：
**默认 AUTO 不绕过优先级排序**（AUTO 的 claude 入口请求同样经 `_select_main_force` 统一排序，
不绕过选择）；但**受信任主脑明确给出的 executor/已授权 executor+model 组合是硬
约束**，优先于优先级层（见 §执行器约束 与 §B4 加固）——有空位的指定 Qoder/Flash/Claude/ZCode 绝不因
排序被改道，也不擅自跨区。

## CodeBuddy 精确模型主力（BW-CODEBUDDY-MAIN-S3，2026-10-17）

用户明确授权新增独立 CodeBuddy CLI 名额 1、最高优先级：`codebuddy:deepseek-v4.1-flash`
（界面名 Deepseek-V4.1-Flash）恢复为正式主力；**只恢复这一个精确模型**，WorkBuddy 与 CodeBuddy
其它旧模型（含旧 GLM）仍 human-relay。

- 容量：`CAPACITY['codebuddy:deepseek-v4.1-flash']=1`，进入 `MAIN_FORCE_POOLS`/
  `MAIN_FORCE_KEYS`/`MAIN_FORCE_CAPACITY`；`DOMESTIC_TOTAL_CAPACITY=13`。配置 13、授权可新派
  上限 13（BW-CUSTOM-MAX-RESUME-20261018-S1 已解除 CN 自定义 Max 暂停）。
- 优先级：`_select_main_force` 第一键层序 `codebuddy=0 > 三 Max=1 > claude=2 > zcode=3`；
  Max 层内 CN 自定义 `qodercn:Qwen-3.8-Max` 先于两内置 Max，两内置之间按真实在途数平衡；
  其余层内仍按真实在途数、请求组合、pool_key stable tie。低层绝不凭历史 committed 抢高层名额。
- 无时段门：`_max_window_blocks` 对 CodeBuddy 恒 false——不套 Claude 离峰窗，也不套 Max 北京
  22:00–08:00 窗；仅受自身 capacity=1、总在途 13、同 task/workspace/unknown/权限守卫。
- 路由：显式 `executor='codebuddy'` 只认该精确组合，满/总满如实 `capacity_full`，绝不跨模型
  回退；AUTO 请求 codebuddy 不绕过统一层序；其它 executor 绝不改派到 codebuddy。
- 每轮只试一次：本轮真实失败由主脑在 `round_failed_candidates` 记下，下一候选继续；下一新轮
  重新从最高层起。不因旧额度/旧冷却挡新模型，不查余额、不新增冷却/表/服务、不删真实锁。
- 入口：`codebuddy_live`（唯一 Popen 处，精确模型硬门、`consume_for_entry`/`bind_child`/
  `finish` 生命周期、MCP 空/hooks 禁用/无 fallback、真实起止时间、显式超时可选）。旧直连解析/
  诊断函数与离线 harness 保留，生产 `codebuddy_direct.main()` 仅对该精确模型移交 live 入口。
- 工具证据：`execution_evidence_ok` 只表示原始工具调用有成功匹配回执，不代表业务验收；零工具
  纯文本传输可合法通过，但零工具工程报告没有工程执行证据。

## 执行器约束与 availability 资格过滤（BW-AVAILABILITY-20261009-B2）

`select_and_claim`/`reserve`/`consume_for_entry`/`claim_due` 新增可选 `executor`
（`auto`|`qoder`|`zcode`|`claude`，缺省 `auto`）与 `quota_store`/`quota_routes` 透传。**先过滤合格候选，
再在可用主力之间按优先级层排序（CodeBuddy > Max > Claude > GLM，同层在途平衡）**：

- **availability 资格过滤走 param-or-env 的正式默认持久源**：解析顺序为显式 `quota_store` →
  环境变量 `BRAIN_WORKER_QUOTA_STORE` → 受信任默认持久文件 `qc.default_store_path()`（即
  `~/.brain-worker/quota-state.sqlite3`），使**默认 AUTO 无需额外参数即过滤不可用 ZCode**。缺库/
  缺表按只读回核当作“无记录（可用）”，读取异常一律 **fail-closed**（当作被阻断，绝不因读失败放行）；
  解析所得路径**只读**、绝不建库/建表、绝不在 pool 事务里写 quota 库。（历史旧说明：“只在显式传入
  `quota_store` 时才过滤、未传不读真实目录”已由 BW-AVAILABILITY-20261009-B5 缺陷 5 统一为默认持久源。）
  传入时读 §3b 的 `zcode_availability`（[quota-routing](quota-routing.md)）：被阻断的 ZCode 通道
  从可用主力集合里剔除。
- **`executor='qoder'`（受信任主脑显式选择）**：有空位直接 claim `qoder:Qwen3.8-Max`，Max 满则按
  溢出策略改道 Flash；**绝不因历史 committed 计数或主力排序被改道到 ZCode**。请求组合与 executor 冲突
  （如 executor=qoder 却请求 zcode 组合）→ `executor_conflict` 拒绝。
- **`executor='zcode'`**：ZCode 被 availability 阻断 → `zcode_unavailable` 拒绝（**不改道到别的
  主力、不落 Flash**）；有空位则 claim，满则 `capacity_full`（无 Flash 回退）。
- **`executor='auto'`（缺省）**：保留历史 committed 计数记账，只在**合格**主力间按优先级层排序
  （CodeBuddy > CN 自定义 Max > 两内置 Max 同级平衡 > Claude > GLM，BW-CODEBUDDY-MAIN-S3，
  Max 层内次序见 BW-CUSTOM-MAX-RESUME-20261018-S1）；
  请求 ZCode 但其被阻断 → 改道到可用主力/同级 Flash；各主力与国际 Flash 都满而 CN Flash 有空槽 →
  改道 CN Flash（入口已是 CN Flash 则 claim），**主力（含 CodeBuddy 与 claude）与两区 Flash 也满
  （总 13 全满；授权可新派上限 13）才 `capacity_full`**，**绝不改道回不可用的
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
  已授权的组合**不因别的候选有空位而被拒绝**；默认 AUTO 主力优先/容量/主力排序（现为优先级层
  Max > Claude > GLM，见上文；BW-CODEBUDDY-MAIN-S3 后为 CodeBuddy > Max > Claude > GLM）与各池真实容量
  （BW-MAX-WINDOW-20261010-S2 后为六池总 10：`zcode`=2、两内置 Max 各 1、CN 自定义 Max=2、两区 Flash 各 2；
  BW-CLAUDE-DEEPSEEK-S3/S4 起把 claude 登记为第 11 主力槽 → **总 11**，BW-CLAUDE-CAPACITY2 扩为
  2 槽 → **总 12**，BW-CODEBUDDY-MAIN-S3 计入 CodeBuddy 1 槽 → **总 13**；原 10 槽一项未减）
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
  claimed/settled/cancelled/launch_unknown，settled 票据不被迟到/重复回复重开；物理满（总 13，含 CodeBuddy 与 claude 2 槽）与
  “合格候选耗尽”分列报告，误报不自动 Luna。

## 入口在 Popen 前的唯一容量门

各真实直连入口（国际 Qoder、Qoder CN、ZCode、Claude 与 `codebuddy:deepseek-v4.1-flash`
精确模型）在**创建输出目录、Popen 之前**必须消费或校验一个容量 claim，**不能靠提示词或一个
传入布尔跳过**（WorkBuddy 与 CodeBuddy 其它旧模型仍 human-relay，仅提示词人工中继，不进池、
不占名额）：

- `dp.consume_for_entry(store, task_id, runtime, model, workspace, prompt_sha256, ...)`：
  - 给了 `--dispatch-claim <token>` → 同一 `BEGIN IMMEDIATE` 事务内先精确校验（task/runtime/
    model/workspace/prompt_sha256/stage/chat_id 任一漂移即拒），通过则 CAS `reserved→running`
    并持有名额；重复/并发消费同一 token 只有一个 `allowed`（一槽绝不双 Popen）；
  - 没给 claim → `select_and_claim` 原子**自动**路由（统一国内策略：主力优先级排序 + 溢出 + 容量 +
    任务防重）；非被选中组合 → `routing_required`。
- `--dispatch-plan` 只用于 `execution_control.preflight` 的权限/工作区/依赖快照核验，**绝不**
  作为绕过主力优先级排序或溢出改道的“确切组合”后门；携带计划者仍走同一条 `select_and_claim` 原子
  路由（旧的 `exact_combo` 旁路已废弃移除）。
- 任何非放行结果（`allowed=false`）→ 入口打印 `capacity_gate_rejected`/`sent:false`/
  `exit_code:2`（含 `routing_required` 与被选中组合），**零证据目录创建、零 Popen、不计轮次**。
- 路由选择**绝不把权限拒绝/登录缺失/额度限流伪装成容量溢出**：容量门只管并发名额；权限与
  额度分别由 `execution_control.preflight` 与 `quota_control.gate_dispatch` 独立裁决。
- **两条路径都必须消费/校验一个真实容量 claim**（`reserve`/`validate`/`select_and_claim` 都会在
  池里落一条在途 attempt），绝不靠提示词或一个传入布尔跳过；容量上限与任务防重在每条路径都强制。

`reserve` 供主脑/测试为**确切** `(runtime, model)` 预留一个确定 token，再交给对应入口用
`--dispatch-claim` 精确消费。`reserve` 与 `select_and_claim` 共用统一国内策略（主力优先级排序、溢出
改道、容量上限、任务防重、真实 workspace 单写入守卫），**不绕过统一排序**：请求组合非选中组合
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

1. 国内名额全满时（BW-QODER-CN-20261010-A2 起阈值随总 8 更新，BW-MAX-WINDOW-20261010-S2 加入 CN 自定义主力后再升为总 10，BW-CLAUDE-DEEPSEEK-S3/S4 加入 claude 主力槽后升为总 11，BW-CLAUDE-CAPACITY2 扩 claude 为 2 槽后升为总 12，BW-CODEBUDDY-MAIN-S3 计入 CodeBuddy 1 槽后为**总 13（原 12 槽一项未减；BW-CUSTOM-MAX-RESUME-20261018-S1 解除暂停后授权可新派上限 13）**，含两内置 Max/自定义 Max/CN 补充/claude/CodeBuddy；判定走
   `_domestic_active ≥ DOMESTIC_TOTAL_CAPACITY`），**宿主先真的问用户**“外部 agent 还是 Luna”。Python 只落库票据，
   **绝不谎称已经问过或已经派生 Luna**——真实提问与原生工具调用由宿主负责。
2. `dp.ask_record(task_id, ...)` 记录一次真实提问票据，开始 **300 秒**计时
   （`LUNA_ASK_TIMEOUT_SECONDS`）。同一 task 只能有一个 `pending`/`claimed` 票据；**重启不重置
   deadline**（`asked_at_utc`/`deadline_utc` 持久化）。
3. 用户回复 → `dp.reply(task_id, choice)`（`luna`/`external_agent`/`domestic`/`cancel`）。
   **一旦回复，`claim_due` 不再自动裁决**——等待超时不当作默认无限授权。
4. 到期竞争裁决 `dp.claim_due(task_id, now)`：只有票据 `pending`、已过 deadline、未回复、未
   claim，**且国内总 13 名额（含自定义主力、CN 补充候选、claude 2 槽与 CodeBuddy 1 槽）确实全满**（`_domestic_active ≥ DOMESTIC_TOTAL_CAPACITY`）时，
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
- **可用性不足的降级决策**：当**国内名额未满总 13（含自定义主力、CN 补充候选、claude 2 槽与 CodeBuddy 1 槽）** 但因 availability 不足以正常派工时，允许一次新的
  降级 `ask_record`，须带**独立、客观的降级理由** `degradation_reason ∈ {quota, auth, capacity}` +
  `degradation_detail`；`quota` 理由必须由 §3b availability 的客观证据支撑
  （`_zcode_availability_blocked`），无证据则拒绝；`capacity` 理由必须经 `_scope_allowed_candidates`
  客观核验受信任候选（含 CN 补充候选）确已排空——仍有空槽（例如 CN 还有位）时**拒绝降级、优先用国内候选**。
  降级票据**绕过“总 13 全满”前提**，但
  **非 capacity 的超时绝不可替代显式授权**：只有用户显式回复 `luna` 才 claim Luna；等待超时
  ≠ 授权（`degraded && reply != luna` → 拒绝）。**总 13 全满 + 真实提问 + 300s + 用户授权**的既有规则不变。

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
- **CodeBuddy 旧限流与旧占位完全不变**；BW-CODEBUDDY-MAIN-S3 仅 `codebuddy:deepseek-v4.1-flash`
  进容量池（1 槽、最高优先、无时段门），其它 CodeBuddy 旧模型与 WorkBuddy 仍只做完整提示词
  人工交接，不进容量池、不新增直连、生成提示词不记成已派发。

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

## 阶段重裁决只读规划入口（`scripts/stage_selection.py`，BW-STAGE-SELECTION-S1/S2）

`stage_selection` 是**只读规划入口**：它**不是**新调度器/常驻服务/数据库表，也**绝不**
`connect`/`reserve`/`select_and_claim`/`reconcile`/`mark`/`finish`。它只以 sqlite
`mode=ro`（`PRAGMA query_only=ON` + `BEGIN` 只读事务，**绝不用 `immutable=1`**；缺库/缺
schema/不可读快照一律如实拒绝且**绝不**创建/迁移/写 WAL/sidecar）打开**已有** dispatch
store，复用既有 `dispatch_pool` 权威方法
（`CAPACITY`/`MAIN_FORCE_KEYS`/`OVERFLOW_KEYS`/`capacity_for`/`_count_active`/
`_active_task`/`_workspace_conflict`/`_domestic_active`/`_zcode_availability_blocked`/
`_main_force_limit_active`/`_max_window_blocks`/`_select_main_force`/`_scope_allowed_candidates`），
**不复制**主力排序/日历/额度算法（主力选择始终复用单一权威 `_select_main_force`，
BW-MAIN-PRIORITY-20261011-S1 的 Max > Claude > GLM 优先级对本入口同样生效，绝不另造排序）。
用途是：主脑**先规划、再生成精确入口参数**，最终正式
入口仍原子复核——规划快照**绝不是**占位、派发或实时成功证明。

### 真实 `fixed` 与临时 `temporary_fallback` 的区别

- **`fixed`**：授权明确要求某个 `runtime+model` 组合时，**即使是 Flash 也按原组合**，
  绝不因为是 Flash 就被主力替换；该组合被时段门/明确限额/容量挡下时**如实拒绝**
  （`fixed_candidate_unavailable`），绝不静默改道。
- **`temporary_fallback`**：授权携带明确的 `fallback_scope`（**必须显式含 `task_id` 与
  `stage`**）与当阶段指定的 `runtime+model`（**必须是两区 Flash 之一**，否则
  `temporary_not_flash`）。`fallback_scope.task_id` **缺失或 != 本 `task_id` 一律
  `fallback_scope_mismatch` 拒绝**——缺字段不得猜授权、不凭 previous 或候选集合蒙混。
  **同阶段**（`fallback_scope.stage == 本轮 stage`）时**只按该指定 Flash**临时降级
  （`temporary_fallback_selected`，仍须在授权集合内并通过时段/限额/容量门，否则
  `temporary_candidate_unavailable`）——**不是 AUTO 关系选择，也不是永久 `fixed`**；
  `allowed_candidates` 保留**基础授权候选集合**（不是只放选中 Flash）。
  **旧 `fallback_scope` 的原值不改写**：先按 `fallback_scope.stage` 识别到期，而非先用
  本轮 stage 去校验旧 scope；旧 temp 的 `authorization_scope` 只对**其原 scope** 核验，
  **不跨 task**、**不凭随意 previous 误授权**。到期（`fallback_scope.stage != 本轮 stage`）
  时，**只有** `previous.task_id == task_id`、`previous.stage == fallback_scope.stage`、
  且 previous 为真实终态 + 已独立验收，才允许切换**显式 `parent_authorization`**
  （parent 当前 task/stage 必须合法、`quote` 真实、类型限 `auto`/`fixed`/`executor`）重新
  裁决，receipt 记 `reselection_due=true`；无 previous 或 previous 不匹配一律
  `fallback_scope_mismatch` 拒绝。**到期可用集合取 `parent.allowed_candidates` 与本轮原
  base `allowed_candidates` 的交集（不扩权），空交集拒绝**，禁止 unknown/retired/Luna/非
  列表注入；**绝不把 `previous selected` 变成约束、绝不猜测扩大授权**。

  短例（旧临时 s1 → 显式父授权、本 task 范围 AUTO → 当前 s2 重新裁决；**不改写成新 scope**）：

  ```json
  {"selection_mode": "temporary_fallback",
   "fallback_scope": {"task_id": "T", "stage": "s1"},
   "runtime": "qodercn", "model": "Qwen3.8-Flash",
   "parent_authorization": {"selection_mode": "auto",
     "quote": "本轮真实原话", "authorization_scope": {"task_id": "T", "stage": "s2"}}}
  ```
  ```json
  {"previous": {"task_id": "T", "stage": "s1", "state": "finished",
                "acceptance_reviewed": true}}
  ```
  此时 receipt 的 `selection_mode`/`authorization_quote`/`authorization_scope`/
  `allowed_candidates` 体现**本轮有效父授权**，旧值仅在 `source_*` 与 `fallback_scope` 追溯。

### 会话复用规则

`session_reuse_allowed` **仅当** `previous.task_id` **显式等于**本 `task_id`、本次
`selected(runtime/model)` 与 `previous` 上轮组合**相同**、`previous` 为真实终态且
`previous.acceptance_reviewed` 为真（已独立验收）时才记；**缺 `task_id` 或来自 OTHER 任务
一律不复用**，**绝不跨 task 继承**旧 CLI session / session ID。普通 AUTO/fixed 合法新选择
照常返回（`selected` 非空），但 `session_reuse_allowed=false`，也不把 CLI session 延续当作
授权或本轮选择。

### 上下文、执行器与只读快照

- **必需上下文**：`task_id`/`stage`/`workspace` 缺一即 `missing_context` 拒绝（无法核对防重与
  工作区）；CLI 对应参数 **required**。
- **有限字段直查**：`executor` 模式缺 `executor` 字段或非法类型/未知值 → `invalid_executor`
  直接 JSON 拒绝（不做通用 schema 框架）。
- **executor 本地区约束**：`executor` 模式的授权候选与本轮 `dp._scope_allowed_candidates`
  给出的本地区集合取交集，**绝不跨区**；地区外候选在 `candidate_filters` 如实显示
  `not_authorized`，**绝不把未选范围显示成 `eligible`**。
- **AUTO 候选全集与 `not_authorized` 语义（BW-CANDIDATE-SCOPE-S1）**：AUTO 的
  `allowed_candidates` 从真实用户原话/原 scope **保留完整已授权候选**（当前 8 候选＝CodeBuddy
  精确模型 + 三 Qoder Max + claude + zcode + 两区 Flash）；时段/明确限额/容量/ZCode availability 等**动态不可用只写
  `candidate_filters`**（`window_closed`/`explicitly_limited`/`capacity_full`/`zcode_unavailable`），
  **绝不改写 `allowed_candidates` 去绕过**。某 `runtime:model` 失败（含内置两 Max 落 `explicitly_limited`）
  **不泛化成整个 executor**，CN 自定义 `qodercn:Qwen-3.8-Max` 仍**独立评估**、优先级仍属 Qoder Max 层（层=1，仅次于 CodeBuddy 精确模型）。`not_authorized`
  **只表示该池不在本轮输入里**，未核对真人原话**不得**说成“用户未授权/撤权”；运行错误不算撤权，授权缩窄
  必须有真实用户新指令或 executor 约束。诚实边界：入口**不能**凭主脑自写 `quote` 自动验证真人授权，也**不**
  新增机器级防伪门、`not_authorized` 字段改动或强制 AUTO 全集代码门（合法用户范围可更窄），靠 7 候选规范 +
  独立回读审查把关。默认 Flash 临时选择只对本阶段生效、下一轮按优先级重选；既有明确 `fixed` Flash 保持。
- **只读快照**：只以 `mode=ro`（`PRAGMA query_only=ON` + `BEGIN` 只读事务）打开**既有**库，
  **绝不用 `immutable=1`**（会忽略 WAL 中最新在途记录，不是安全快照）。WAL 存在但 `-shm`
  缺失、模式/库不可读 → 如实 `store_snapshot_unavailable`，**绝不创建 sidecar/建库/建表/
  迁移**；缺 `attempts`/`rotation` schema → `store_refused`。connect 成功但
  `PRAGMA`/`BEGIN` 失败时**先 close 已建连接再抛 `Refused`**，不留连接泄漏、不改库。回归用
  临时 WAL 库 + 打开的 writer 保留最新 active，规划必须真实读到它；不可读/缺 schema 时字节
  与文件集不得改变，**绝不把过期基页当作成功**。逐候选过滤按**权限能力（仅当本轮声明
  permission_grants 精确需求）→ 时段 → 明确限额 → 容量**
  顺序只读求值；入口后续仍由正式入口**原子加锁**复核，规划快照永不代替锁。

### 任务权限需求与运行时能力匹配（BW-TASK-PERMISSION-S1）

派工计划生成**之前**，规划入口接受一个**可选** `permission_grants` 输入，表达当阶段
实际所需的最小权限；它与 `authorization` 对象**完全分开**——**能力兼容不构成授权，
授权也不隐含能力**。结构直接复用 `execution_control` 的 grants 字典（`edits`/`bash`/
`read_dirs` 非空未带空白字符串列表，未知键/非列表/畸形一律 `invalid_permission_grants`
严格拒绝，不做通用 schema 框架）；**工具可见性（如整工具 `Read`）不是按文件权限**，
不接受把整工具隔离方案误当精确 grants。

- **推导口径**：`fine_grained` 需求 = `edits` 或 `bash` 非空；`read_dirs` 需求 =
  `read_dirs` 非空——与 `execution_control.preflight` 的能力判定同一口径。
- **能力表复用**：**只读复用 `execution_control.CAPABILITY`**（按 pool_key 的 runtime
  前缀查 `fine_grained`/`read_dirs`），**绝不修改 CAPABILITY** 把 ZCode 伪装成
  `fine_grained=True`。ZCode 只有整工具开关：`CAPABILITY['zcode']` 两能力皆 False 是
  事实，不是本入口的缺陷。
- **过滤与可辨原因**：仅对**授权集合内**候选应用；不满足者标 `permission_incompatible`，
  与 `zcode_unavailable`（availability/额度）、`not_authorized`（不在本轮输入集合）、
  `window_closed`/`explicitly_limited`/`capacity_full` **分别可辨、绝不互相冒充**；
  `allowed_candidates` 保留完整授权集合不改写。选择（auto/executor 关系选择、fixed、
  temporary 同阶段与到期重选）只在能力兼容且通过既有门的候选内进行。
- **fixed 诚实拒绝**：`fixed` 明确 `zcode:*` 而任务要求精确 grants →
  `fixed_candidate_unavailable`（detail=`permission_incompatible`），**不偷偷换执行器**；
  要求 ZCode 的等价保护须采用**单独验证过实际边界**的整工具隔离方案，不删除精确
  grants 或放宽校验冒充等价保护。
- **各模式应用当前需求**：`auto`/`executor`/`fixed`/`temporary_fallback`（同阶段与旧
  scope 到期经父授权重选）都按**当阶段**需求过滤，**绝不从旧 session 继承需求**。
- **旧兼容行为**：未传 `permission_grants` 或无精确需求（三类皆空）→ 能力过滤整体
  不启用，ZCode 等候选照旧 eligible；现有时段/容量/额度/工作区/在途/正式入口检查全部
  保留、一项不弱化。
- **普通已授权项目的默认整工具计划（BW-ZCODE-TASK-TOOLS-S1/S4）**：当用户本次已明确授权修改
  某项目、且任务只是常规读写+自行测试（无精确 `grants.edits`/`grants.bash` 细粒度规则需求）时，
  ZCode 直接派工的默认口径是**整工具计划** `--mode edit --tools Read,Glob,Grep,Edit,Write,Bash`，
  不强制附 `--command-contract`/`--file-scope-contract`，也不因缺这两份契约把 edit+Bash/写在派发前
  当违规拒绝（前述严格逐字命令契约与受控逐文件契约仍是**可选加固**，用户要求精确命令/文件范围或
  本身就是精确严格计划时继续套用、原样不放宽）。这条只降“本 Skill 自加的默认硬门”，**不改动**
  `CAPABILITY['zcode'].fine_grained=False`、国内 13 槽、CodeBuddy > Max > Claude > GLM 优先级、额度、同真实工作区
  单写入、在途单占位，也不扩任何生产/GitHub/凭据/外部写入授权。普通路径只注入任务工具授权 broker、
  不注入普通 `executionPort`，Bash 交给**官方原生 executionPort** 执行；真实测试执行由 `execution_evidence`
  门禁回读官方 raw Bash 结果 `result.perf.detail.command`（completed+严格整数 exit0）据实核验
  （`success=true`≠exit0；不再有自建观测端口的 started/exit 计数可宣称），能力可用≠业务完成。
- **最小需求回执**：receipt 增加 `permission_requirements`
  （`{"fine_grained": bool, "read_dirs": bool}`，未传为全 false），其余不变。
- **受控文件 profile 例外（BW-ZCODE-FILES-S1，现场已核验）**：`CAPABILITY['zcode'].fine_grained` 仍为
  `False`、绝不改写。仅当本轮需求**只需精确文件 Edit**（`edits` 非空、`bash` 与 `read_dirs` 皆空）时，
  `stage_selection` 才把 ZCode 判为 `eligible`，并在 receipt 增加独立布尔 `controlled_file_profile_required`
  标明该路径依赖已验证绑定的 file_scope 契约；混合 Edit+Bash 仍 `permission_incompatible`。正式 preflight
  （`execution_control`）据 `--file-scope-contract` 冻结的 profile 复核契约精确覆盖 `edits`+workspace+实际工具，
  才接受 ZCode 精确 Edit。**硬 Edit+Bash 不支持 ZCode 直接承接，但主脑可以选择能承接同一硬合同的
  已授权兼容执行器一轮完成整段混合授权；仅当任务确有依赖且经任务授权时，才采用纯 Edit profile
  （ZCode 改文件 → 真实终态/磁盘回读 → 兼容执行器跑登记测试）与必要的独立测试，
  不强制串行拆分、不机械拆分**。2026-10-13 4 个隔离 GLM-5.3 真 Agent 用例已在 FileSystemPort 层现场核验
  （登记 Read/Edit/Write 成功、只读参考 Edit 拒、未登记 Write 拒、未登记 junction 拒，均 exit0+capacity_released；
  正向 `report_bound=false` 仅因路径末尾多句号、余三例 true、原件保留不为格式重跑）；TraceContext 不给 toolCallId 故不伪造、
  以 session/turn/原始工具事件关联。范围仅限登记/未登记路径与只读参考、未登记 junction，**不宣称全部重解析竞态/外部进程
  TOCTOU 已防护、不升级 Bash、非 OS 进程沙箱**。
- **规划顺序与边界**：**每次先明确当前权限需求 → 再规划 → 再按选中的 runtime 生成同
  权限的 plan/actual 并过正式 preflight**；不可只把旧 Qoder 计划的 runtime 换成 ZCode
  ——正式 preflight 会按 `CAPABILITY` 拒绝（`sent=false`、exit 2），该拒绝**不是模型
  调用失败、不是额度事实**。CLI 可表达的规则 ≠ 后端实际执行能力或余额验证。

```json
{"permission_grants": {"edits": ["Edit(C:/proj/app.py)"],
                        "bash": ["Bash(pytest -q tests/test_app.py)"],
                        "read_dirs": []}}
```

### 普通任务范围 task_scope 与硬合同分清（BW-PERMISSION-SCOPE-S1）

普通已授权隔离工程的文件/测试目标用**可选** `task_scope`（`--task-scope-json`）表达，
与 `permission_grants` **分别作用**：task_scope 只是"本轮普通任务要碰哪些文件/跑哪个
测试/读哪些参考"的清单（同 `edits/bash/read_dirs` 结构，畸形严格 `invalid_task_scope`
拒绝），**只进回执/任务书**，不驱动能力过滤、不构成授权、也绝不弱化或覆盖 hard
grants；`permission_grants` 仍是必须强制的运行时硬合同，两者同传时 hard grants 完整
生效。缺来源的既有精确 grants 按硬合同对待，不自动降级。receipt 另增
`permission_incompatibility_details`：仅对 `permission_incompatible` 候选说明实际冲突
（per-file edits/verbatim Bash grants、external read_dirs grant），并标明
`ordinary_task_tools` 可选路线——先明确不要求硬合同、以普通整工具 edit+Bash 计划重新
构造后再选该 runtime；纯 Edit 受控 profile 是现有可选严格路线，混合 Edit+Bash 硬合同
仍不能直接承接。该明细不自动拆阶段/降权限/扩候选，也不是模型/认证/额度失败；正文 402
或"无调用"等叙述不进入本入口，quota 仍只认既有 availability 口径。

```json
{"task_scope": {"edits": ["source/f1.cjs", "source/f2.cjs"],
                "bash": ["python -m unittest discover -s tests -p test_app.py"],
                "read_dirs": ["references/"]}}
```

主脑后续按选中 runtime 生成 formal plan 时：**用普通 task_scope 选 ZCode 的普通任务，
formal plan 的 `grants` 为空精准列表，`tools` 声明为 Read,Glob,Grep,Edit,Write,Bash
（整工具）**——绝不能把 task_scope 的文件名单复制进 formal grants 又变回硬合同；只有
用户明确要求运行时强制边界才生成精确 `permission_grants` plan。

### 最小 receipt schema（输出 JSON，无占位）

```json
{
  "preview_only": true, "sent": false, "placeholder": false,
  "task_id": "T", "stage": "s2", "workspace": "…abs ws…",
  "observed_at_utc": "2026-10-11T00:00:00+00:00",
  "selection_mode": "auto|fixed|executor|temporary_fallback",
  "selected_reason": "main_force_selected|executor_region_main|flash_fallback_selected|fixed_authorized|temporary_fallback_selected|null",
  "authorization_quote": "…本轮有效授权原话（到期重选时为父授权原话）…",
  "authorization_scope": {"task_id": "T", "stage": "s2"},
  "allowed_candidates": ["zcode:GLM-5.3", "…"],
  "fallback_scope": {"task_id": "T", "stage": "s1"}, "reselection_due": false,
  "source_selection_mode": "temporary_fallback（仅到期重选时出现，追溯用）",
  "source_authorization_quote": "…旧 temp 原话…",
  "source_authorization_scope": {"task_id": "T", "stage": "s1"},
  "session_reuse_allowed": false,
  "permission_requirements": {"fine_grained": false, "read_dirs": false},
  "task_scope": null,
  "permission_incompatibility_details": {},
  "selected": {"runtime": "zcode", "model": "GLM-5.3", "pool_key": "zcode:GLM-5.3"},
  "candidate_filters": {"qoder:Qwen3.8-Max": "window_closed|explicitly_limited|capacity_full|zcode_unavailable|permission_incompatible|not_authorized|eligible"},
  "zcode_blocked": false, "refused": null,
  "snapshot_note": "snapshot time is not live capacity; the authoritative entry re-checks atomically"
}
```

到期重选时 `selection_mode`/`authorization_quote`/`authorization_scope`/`allowed_candidates`
体现**本轮有效父授权**，旧 temp 的 `source_selection_mode`/`source_authorization_quote`/
`source_authorization_scope` 仅作追溯；`fallback_scope` 保留旧值、`reselection_due=true`。
`refused` 非空时 `selected=null` 且整项拒绝（不换模型/不落 Luna）；拒绝原因含
`missing_authorization`/`missing_context`/`no_source_quote`/`missing_authorization_scope`/
`scope_conflict`/`unknown_selection_mode`/`invalid_executor`/`missing_fixed_combo`/
`empty_allowed_candidates`/`unauthorized_candidate`/`previous_in_flight`/
`previous_awaiting_acceptance`/`previous_not_terminal`/`task_in_flight`/`workspace_conflict`/
`domestic_total_full`/`fallback_scope_mismatch`/`parent_authorization_required`/
`candidate_not_authorized`/`fixed_candidate_unavailable`/`temporary_not_flash`/
`temporary_candidate_unavailable`/`rotation_state_unavailable`/`no_eligible_candidate`/
`invalid_permission_grants`（BW-TASK-PERMISSION-S1，畸形权限需求严格拒绝）/
`invalid_task_scope`（BW-PERMISSION-SCOPE-S1，畸形任务范围清单严格拒绝）/
`store_refused`/`store_snapshot_unavailable`。`candidate_filters` 的逐候选取值另含
`permission_incompatible`（能力不匹配，与 `zcode_unavailable`/`not_authorized` 可辨）。

### CLI 与示例调用（先规划，再精确参数）

```text
python scripts/stage_selection.py --store <abs.sqlite3> --task-id T --stage s2 \
  --workspace <abs ws> --authorization-json '<authorization object>' \
  [--permission-grants-json '<permission_grants object>'] \
  [--task-scope-json '<task_scope object>'] \
  [--previous-json '<previous record>'] [--quota-store <db>]
```

`--task-id`/`--stage`/`--workspace` 均为 **required**（缺一即 `missing_context` 拒绝）。
`--permission-grants-json`（BW-TASK-PERMISSION-S1）与 `--authorization-json` 同为 JSON
字符串最小新输入，**不与授权对象合并**；畸形 JSON 走既有 `_load_json_arg` 非零退出，
畸形 grants 结构走 `invalid_permission_grants` 拒收回执（退出 2）；`--task-scope-json`
（BW-PERMISSION-SCOPE-S1）同口径，畸形结构走 `invalid_task_scope` 拒收回执（退出 2）。

退出码 **0 = 选择成功 / 2 = 拒绝**（输出 JSON）。拿到 `selected.pool_key` 后，主脑再用
精确 `runtime/model`（`reserve`/`select_and_claim` 的 `--dispatch-claim` 路径）交正式入口
原子复核——规划入口本身**不占名额、不派工**。`previous` 记录前阶段
`task/stage/runtime/model/state` 及 `acceptance_reviewed`：前轮 `reserved/running/unknown/
executing/awaiting_acceptance` 或**未独立验收**时拒绝新选择（不抢占、不停进程、不重复
派工），只有真实终态 + 已独立验收（含明确失败验收后的补修）才可新裁决。

> 离线回归 `tests/test_stage_selection.py`：临时 SQLite + mock Z 可用函数，零真实模型/
> 共享库/凭据/余额；覆盖临时 CN Flash 终态后新阶段重选主力、fixed 长期不变、executor
> 不跨区、**白天三 Max 剔除、其它主力各自判窗与可用**、夜晚进入并
> 保公平、明确限额跳过、容量过滤、在途/未验收/同 workspace 冲突/父授权缺失/scope 漂移
> 整项拒绝、旧 scope 到期重选、父授权交集不扩权、`fallback_scope` 缺 `task_id` 拒绝、
> 会话复用仅限同 task 组合（OTHER/missing 一律 false）、只读打开失败先 close 不泄漏、
> 上下文缺项、WAL 活 writer 可见与不可读
> 快照拒绝、读写前后哈希不变、缺库不创建、退休/Luna 不授权、CLI 退出码。
> BW-TASK-PERMISSION-S1 另覆盖（`PermissionGrantsTests`，同临时 SQLite + mock）：白天
> 7 候选 Z 健康有空位但任务需精确 Edit/Bash → 选兼容已授权 Flash、Z 启动计划为 0；同窗
> 无精确规则 → Z 照旧可选；fixed Z + 精确要求 → 诚实拒绝不偷偷换；read_dirs 独立能力
> 过滤；缺 Qoder 授权不因能力过滤扩权；`zcode_unavailable` 与 `permission_incompatible`
> 分别可辨；工作区/同任务在途守卫与只读快照哈希在带 grants 时照旧；畸形 grants 与 CLI
> 新输入错误拒绝；临时降级同阶段与父授权到期重选应用**当前**需求。BW-PERMISSION-SCOPE-S1
> 另覆盖（`TaskScopeTests`）：白天 Max 窗关 + Claude limited + 仅 task_scope → 选 ZCode；
> 同 scope 加 hard Edit+Bash → Z 仍 `permission_incompatible` 且原输入不突变；legacy 仅
> grants 行为不变；纯 hard Edit 受控 profile 一致；fixed/缺授权/availability/在途/工作区
> 守卫不被 scope 绕过；带与不带 scope 的逐候选原因完全一致；畸形 scope 与 CLI 新输入
> 拒绝（退出 2）；规划仍只读（哈希不变）。Windows/Linux CI 矩阵都跑。

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
- 同层在途平衡是 best-effort，短暂偏差不自动纠正；跨层是硬性优先级层（Max > Claude > GLM，
  见上）。旧跨层 committed 1:1 属历史口径、已被 BW-MAIN-PRIORITY-20261011-S1 覆盖。
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

## 11. 六池 + claude 2 槽 + CodeBuddy 1 槽真实分配与主力明确限额（2026-10-10/11/17，BW-POOL-SPLIT-20261010-S2/S3/S5 + BW-MAX-WINDOW-20261010-S2 + BW-CLAUDE-DEEPSEEK-S3/S4 + BW-CLAUDE-CAPACITY2 + BW-CODEBUDDY-MAIN-S3）

> 当前有效口径（覆盖本节历史数字）：物理 `DOMESTIC_TOTAL_CAPACITY=13`，其中新增
> `codebuddy:deepseek-v4.1-flash=1`；BW-CUSTOM-MAX-RESUME-20261018-S1 解除 CN 自定义 Max
> 暂停后授权可新派上限 13（暂停期曾为 11，属历史状态）。
> 绝不派第 14 个；历史 8/10/11/12 均属被覆盖的旧记录。

S2/S3 定型的国内真实容量经 BW-MAX-WINDOW-20261010-S2 加入 CN 自定义主力后为六池：`zcode:GLM-5.3=2`、国际内置 `qoder:Qwen3.8-Max=1`、CN 内置 `qodercn:Qwen3.8-Max=1`、CN 自定义 `qodercn:Qwen-3.8-Max=2`、`qoder:Qwen3.8-Flash=2`、`qodercn:Qwen3.8-Flash=2`；**BW-CLAUDE-DEEPSEEK-S3/S4 追加主力 `claude:deepseek-flash=1`（独立第三公平组，见上文 Claude 章节）→ 物理总容量 11，BW-CLAUDE-CAPACITY2 将该池由 1 扩为 2 → 物理总容量 12**；**BW-CODEBUDDY-MAIN-S3 再追加 `codebuddy:deepseek-v4.1-flash=1`（最高优先、无时段门）→ 物理总容量 13**；`qodercn:DeepSeek-Flash` 退休为 0（`LEGACY_RETIRED_POOL_KEYS`）但既有在途 attempt 仍计入 `_domestic_active` 总数、真实终态自然释放；`luna:native` 无上限、仅救援。**现行 `DOMESTIC_TOTAL_CAPACITY=13`、绝不派第 14 个；原 12 槽一项未减**。CN 内置模型真实名 `Qwen3.8-Max`/`Qwen3.8-Flash`（**不是** `Qwen-3.8-*`）；自定义主力用带连字符的友好名 `Qwen-3.8-Max` 与内置区分（2026-10-18 已恢复新派工，历史暂停事实保留）。

S5 增加 Qoder Max 池的**明确限额**闭环（BW-MAX-WINDOW-20261010-S2 把自定义 CN 主力并入承载范围）：
- **限额承载范围**：`QUOTA_LIMITABLE_POOLS = (qoder:Qwen3.8-Max, qodercn:Qwen3.8-Max, qodercn:Qwen-3.8-Max)`。自定义 CN 主力复用同一标记机制，各自 pool_key 独立落标、对两内置 Max 无连带。ZCode 仍走 `quota_control` availability；任一 Flash、退休 DeepSeek、Luna 永不落标；绝不误挡 Flash、绝不动 ZCode 既有 availability 口径。
- **落标必须绑真实 attempt + 错误证据**：`record_main_force_limit(store, pk, task_id=…, attempt_token=…, evidence_path=…, evidence_sha256=…)` 在 `BEGIN IMMEDIATE` 事务内回核 attempt 行存在、`pool_key==pk`、`task_id==传入`；同时校验 `evidence_path` 指向的文件真实存在且 `_file_sha256` 与声明一致。任一漂移或缺字段 → `recorded=False / drift=True` 拒绝写入。同池重复落标只刷新证据/时间戳，清空旧的 `released_at/release_note`。
- **consume 前查限额**：`consume_for_entry` 在同一事务内、`_workspace_conflict` 之前，若 `row['pool_key']` 命中 `main_force_limits.limited=1` → 只把**本 token** CAS `reserved→start_failed`（`capacity_released=True`），返回 `sent=False / allowed=False / reason='main_force_limited'`，**绝不泄漏本次占位、绝不越权释放其他 task 的 reserved**。人工 `release_main_force_limit(note=…)` 后同 reserved 可正常 consume 到 running。
- **恢复只人工**：`release_main_force_limit` 只在用户明确额度恢复/重置后调用，附 `note`；绝不查余额、不加定时器/探针/评分/新服务、不靠成功旧日志自动清标记；不改 `quota_control.py`/`zcode_direct.py`。
- **AUTO/executor 路由保留**：`executor='qoder'` 请求 QMax 且未落标 → 直接 claim；已落标或已满 → 同 executor 内 Flash 兜底（`routing_required` 选 `qoder:Qwen3.8-Flash`），绝不复活限额 Max、绝不跨地区换 CN。`executor='qodercn'` 同规则镜像 CN Max→CN Flash。Luna 侧不变：domestic 任何时刻都不启用 Luna、unknown 保占位、明确模型不偷偷换。
- **qoder_direct 落标时序**：真实失败终态（`protocol_success=False`）经 `_explicit_quota_limit_hit` 命中 + `_qoder_max_pool_key` 返回非 None → **先** `record_main_force_limit`（写 `stdout.json` 原字节 sha256 为证据）→ **再** 真实终态释放；落标异常绝不阻断真实终态释放（不泄漏锁）。
- **离线回归覆盖**：`tests/test_dispatch_pool.py::MainForceLimitTests`（含自定义 CN 主力 record/read/release、consume 只释放本次 reserved、对两内置 Max 无连带）、`tests/test_qoder_cn_direct_offline.py::ExplicitQuotaLimitHitTests`、`tests/test_dispatch_pool_qodercn.py`（六池容量/退休 DeepSeek `unknown_pool`）、`tests/test_availability_routing.py`（回收/降级 combo 硬约束）、`tests/test_parallel_execution.py`（并发演练 qa Max + qb Flash）。Windows/Linux CI 矩阵都跑，零网络、零凭据、零真实额度查询。
