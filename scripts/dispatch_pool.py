"""dispatch_pool — brain-worker 跨会话并发容量池与路由（标准库 sqlite3）。

用户 2026-10-08 最终分配决策的可执行落地：所有会话共享同一持久池，绝不各自为政。
2026-10-10 用户最新分配（BW-POOL-SPLIT-20261010-S2）在 BW-QODER-CN-20261010-A 系列之上
再次刷新：总数仍 8，五池布局（容量口径见 references/global-dispatch.md）：
- 主力（3 池共 4 槽）：`zcode:GLM-5.3`=2、`qoder:Qwen3.8-Max`=1、`qodercn:Qwen3.8-Max`=1；
  Z2 与两地区 Qwen Max（合计 2 槽）之间 best-effort 1:1（沿用 rotation 两列
  committed_zcode / committed_qoder，两地区 Max 共享 committed_qoder 一列），两地区 Max
  之间用当前真实在途数平衡；仅对合格可用候选平衡，绝不因历史计数拦有空位的候选；
- 兜底（同级 2 池共 4 槽）：`qoder:Qwen3.8-Flash`=2、`qodercn:Qwen3.8-Flash`=2；三主力
  都不可用或已满时才按同级候选（真实在途少者优先、其次同区偏好）落到 Flash，不再有
  "CN 独立补充池最后"的分层；
- 已 retired：`qodercn:DeepSeek-Flash` 从正常候选池退出，`LEGACY_RETIRED_POOL_KEYS`
  保留池名与 0 容量条目——不再有任何新 claim 落到该池，但已存在的真实在途 attempt 依
  真实终态释放（finish/reconcile），绝不伪造结束或抢占；客户端自定义 model_ids 映射
  保留（DeepSeek 不再被派工不等于删除本机自定义配置）；
- Luna（`luna:native`）只做救援、无数量上限，只有八个国内名额全满、真的问过用户、
  且 300 秒无回复后才可由宿主原生调用；本模块只持久化票据与竞争裁决，绝不谎称已经
  问过或已经派生 Luna（真实提问与原生工具由宿主负责）。

（保留以下 Z1–Z6 历史修订段落作为审计口径，其文字所描述的具体池/槽位在 S2 后按上文
新池布局理解，不再逐一改写历史。）

2026-10-08 修订二（BW-GLOBAL-POOL-20261008-Z2）在 Z1 之上的关键修正：
- 主力分配一律先按持久 committed 计数选择主力（跨聊天 1:1），平票才优先请求组合；
  本池有空位也不绕过轮转直接 claim（Z 任务结束后再请求 Z 会 routing 到 Max）。
- 两主力满时请求 Z/Max 一律 routing_required 到 Flash 入口，绝不替模型偷换组合落库；
  attempts.pool_key 恒等于 runtime+':'+model，任何入口不再虚记名额。
- select/reserve 成功国内 claim 的同一事务内取消同 task 旧 pending 待援票；用户已回复
  external_agent/cancel 的票据阻断该 task 的国内启动。
- claim_due：先核票据真实性与用户回复，再看国内空位（30 秒内释放也立即 domestic_
  reclaim，不必等到期）；只有六仍满才判 300 秒 deadline/明确 reply luna。国内回收与
  Luna 两个分支都写回票据原 scope 的真实绑定字段（stage/chat/prompt_sha256/workspace）
  并返回完整原 scope；传入 scope 时两个分支都必须与原 scope 一致。
- bind_child 校验当前真实调用者（wrapper）身份；finish 对无 child 的 Popen-bind 窗口/
  unknown attempt 只有 owner 身份一致才接受 start_failed/cancelled，unknown 无 child
  绝不能凭终态参数释放；reserved 未消费的取消仍合法。

2026-10-08 修订三（BW-GLOBAL-POOL-20261008-Z3）工程补齐：
- _insert_claim wrapper 分支 SQL 15 列对齐 15 个 VALUES（修复生产 auto-consume 直接
  OperationalError）；select/reserve 的防重顺序改为先判 native-on-Luna 再判普通在途
  （task_already_on_luna 理由准确，语义不变仍拒绝）；reconcile 对无 wrapper 无 child 的
  在途行（含 running）一律明确 held_unknown。
- process_identity：Linux 在 /proc 可读时返回稳定创建身份（boot_id + /proc/PID/stat
  starttime），Windows 沿用 GetProcessTimes；失败一律 unknown/无身份。
- adopt-legacy（CLI 同名）：受信任宿主专用的最小上线交接——只接纳宿主提供的既有原
  request.json 与邻近 process.json，核验真实活 PID 与创建身份后把老 worker 如实计入；
  仅 GLM-5.3 / Qwen3.8-Max / Flash，超上限如实计入并阻止新派；无 fake-alive 开关。

2026-10-08 修订四（BW-GLOBAL-POOL-20261008-Z4）实质修正：
- _local_pid_state Windows fallback：OpenProcess 句柄成功不等于存活（父进程仍持
  句柄时已退出子进程也能打开）；正确声明 argtypes/restype，用 GetExitCodeProcess
  且 exit code == STILL_ACTIVE 才算 alive，非 STILL_ACTIVE 才 dead，探测失败
  （含 ERROR_ACCESS_DENIED）一律 unknown，绝不臆断 dead。qc 存在/缺席同语义。
- 同真实 workspace 单写入守卫：select_and_claim / reserve / consume（显式 token）
  / claim_due（国内回收与 Luna 两分支）/ adopt-legacy 全部在同一 BEGIN IMMEDIATE
  内核对 normcase(realpath(workspace)) 是否已有别的 task 的在途写入（reserved/
  running/unknown），冲突拒绝且 sent=false；同 task 原消费/续claim 除外。symlink/
  大小写/斜杠别名经 realpath+normcase 归一后同样拒绝。
- adopt-legacy 兼容真实旧原件：Qoder 旧 request（model_requested + runtime∈
  {node,qodercli}，无顶层 task_id/model）与 Zcode 旧 request（carrier='zcode-sdk'
  + selection.providerId/modelId）；task_id 由顶层 → dispatch_plan.task_id → 原
  stage 可靠推导。旧 process.json 原件只有 pid/state（Z 或有 started_at_utc）、
  没有 created：允许受信任宿主传入当前核验的 child_created 独立身份留证（created_
  source 记入 adopt_evidence），生产仍用真实 process_identity 验证 alive+birth
  完全一致；无 fake-alive 开关，绝不改写旧原件。
- adopt-legacy 把确认的实际 worker PID+birth 绑定到 child_pid/child_created（不再
  放 wrapper_pid 冒充未启动 wrapper）：旧 worker 真实退出后 reconcile 立即
  reconciled_exit 释放容量（结束即空名额）。同 PID+birth 即使不同 task 入参也绝不
  双计（pid_already_adopted，源 task 与原 request 精确绑定）；超额如实计入并阻新
  派，不杀/不丢旧 worker。

2026-10-08 修订五（BW-GLOBAL-POOL-20261008-Z6）native 终态收口实质补修：
- _native_receipt_evidence：sha256 必须是严格 64 位 ASCII 十六进制（统一小写落库）；
  content 与 sha256 同时给出必须核对一致；content 为 string 时按 UTF-8 原文字节
  哈希（绝不加 JSON 引号），对象按明确 canonical JSON 口径。回执元信息仍只是受
  信任宿主边界内的可追溯证据，绝不宣称独立服务器验证。
- settle_native：全部身份/回执校验（票据 token 绑定、国内 attempt 禁入、
  launch_unknown、agent_id、scope、receipt）先于幂等分支——错误 agent/scope/
  缺回执/不同终态的重复请求不再被伪称幂等成功；合法重复（同终态+同原回执哈希）
  幂等返回且不覆盖旧证据、不碰新 attempt。
- settle_native：terminal 与 success 不得矛盾（finished=成功、native_failed=失败）；
  显式 success 只在与 terminal 一致时接受。

2026-10-08 修订（BW-GLOBAL-POOL-20261008-Z1）统一的关键口径：
- 所有公开生产路径（reserve / select-and-claim / entry-auto consume）执行同一套
  主力优先/1:1/溢出/Luna 条件；不存在任何“测试播种”后门参数或环境变量。
- consume 对显式 claim token 是同一事务内的 CAS（reserved→running）：重复/并发消费
  只有一个 allowed，绝不允许一槽双 Popen；token 必须与原 task/stage/chat/promptSHA/
  真实 workspace/runtime/model 完全一致，任一漂移即拒。
- auto（entry-auto）路径与显式 token 路径同样在 Popen 前记录 wrapper PID 与真实
  创建身份；bind_child 单次绑定当前 owner，绝不覆盖已绑定的活 child。
- finish 增加真实终态核验：绑定过的 child 仍活（可注入 prober 回读）必须拒绝；只有
  未绑定 child（确认未启动/取消）或 child 已真实退出才释放本 attempt；重复释放幂等。
- reconcile 先判已绑定 child 的终态，再看 wrapper：wrapper 活但 child 已死 → 释放；
  真实 Windows 上死 PID 的 created=None 属正常，不要求死人可回读创建时刻才释放；
  child 存活未知/出生漂移（PID 复用）保守 unknown 不抢占。每次新 claim 前做一次
  安全的 pre-claim 对账，只对有真实 PID 记录且已验证死亡的 attempt 释放。
- ask_record 必须带非空真实询问回执标识（ask_message_id）与完整原 scope（task/stage/
  chat/workspace/promptSHA），且六个国内名额确实全满才记录；等待固定 300 秒，任何
  更短的 deadline_seconds 直接拒绝（更长的也按固定 300 落库）。
- claim_due 在同一事务内重查三池并原子裁决：国内已恢复空位 → 原子预留国内 claim 并
  取消同 task 待援票据（不再出现“只 cancel 不预留”的丢槽竞态）；六仍满、到期、无人
  回复（或用户明确 reply luna）→ claim Luna（不同 task 无全局数量 cap）。票据/重启/
  重复 callback/回复全部幂等，不重置 deadline，不覆盖已 claimed/launch_unknown；
  同 task native 启动后不能再回到国内启动。原生工具由宿主调用，Python 绝不冒称已 spawn。

其它既有口径保持：
- 容量并发 ≠ 额度冷却：本模块只管“同时几个真实执行器在跑”，额度门禁仍在 quota_control。
- 原子性：路由选择/claim/任务防重全部在同一 `BEGIN IMMEDIATE` 事务里完成，跨进程/跨
  会话/跨 cwd 一致；默认 store 为 `~/.brain-worker/dispatch-pool.sqlite3`，环境变量
  `BRAIN_WORKER_DISPATCH_STORE` 覆盖，测试必须传显式临时 store。
- 子进程真实结束立即释放容量名额（不等报告绑定/业务验收）；后续报告解析失败仍释放；
  已启动但存活未知不得假定 start_failed 释放。PID 复用必须绑定创建时刻。
- 容量只看本池在途 attempt 的真实终态与 PID 存活，不依赖 execution_control 快照。

CLI（宿主/主脑可真实操作，全部输出 JSON；不提供任何伪造终态的 flag）：
  status            查看各池在途/容量/空闲、国内合计、Luna 票据
  reserve           为确切 (runtime, model) 预留（统一策略：Flash 需主力全满；Luna 拒绝）
  select-and-claim  无 claim 时的原子路由选择；当前入口非被选中组合 → routing_required
  validate          校验一个 claim token 是否与 task/stage/chat/runtime/model/workspace/prompt 精确一致
  bind-child        记录真实子进程 PID 与创建时刻身份（单次，不覆盖）
  finish            终态释放容量（finished/start_failed/cancelled；带真实终态核验）
  reconcile         按 PID 存活/创建身份对账，安全回收已死 attempt，不误抢活/未知
  ask-record        记录一次真实的“问用户（外部 agent 或 Luna）”票据，固定 300 秒
  reply             记录用户回复选择（不再把等待超时当默认无限授权；reply luna = 已授权接续）
  claim-due         到期裁决：国内空位原子回收 / 六满且无人回应才 claim Luna
  mark-launch-unknown  原生调用非幂等窗口标记（只在 claimed 票据上，绝不自动重启）
  record-agent-id   记录宿主真实原生调用后的 agentID
  settle-native     受信任宿主原生终态结算（绑定 claimed 票据/agent_id/scope +
                     原始原生回执 source_tool/receipt_ref/sha256；单事务释放）
  cancel-pending    国内名额释放时原子取消同 task 待援票据
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
# 只读复用 quota_control 的只读 PID 存活探针（Windows 走 ctypes，绝不 os.kill）。
# 隔离修订目录没有 quota_control 时退化为本地只读探针，行为口径不变。
try:
    import quota_control as qc  # noqa: E402
except ImportError:  # isolated revision tree / standalone tests
    qc = None

# claude:deepseek-flash 时段门复用同仓 deepseek_offpeak（北京时间固定 UTC+8 + 轻量假日表）。
# 缺失时保守拒绝 claude 高峰新派（fail-closed），不影响其它池。
try:
    import deepseek_offpeak as offpeak  # noqa: E402
except ImportError:  # isolated revision tree / standalone tests
    offpeak = None

# ------------------------------------------------------------------ 池定义
# BW-POOL-SPLIT-20261010-S2 + BW-MAX-WINDOW-20261010-S2 用户 2026-10-10 最新分配：
#   六池布局（BW-CLAUDE-CAPACITY2 起含 claude 2 槽；BW-CODEBUDDY-MAIN-S3 再加 CodeBuddy
#   精确模型 1 槽；BW-WORKER-WIDGET-20261018-S3 起 CodeBuddy GLM 独立 1 槽；
#   BW-OPENCODE-20261019 再加两个 OpenCode 免费模型各 1 槽）、当前调度总数 16：
#   主力（四池共 6 槽，同属 best-effort 1:1 的两个组）：
#     zcode:GLM-5.3=2（Z 组）、qoder:Qwen3.8-Max=1、qodercn:Qwen3.8-Max=1（国际/CN 内置
#     Max，属 Qoder 组，仅北京时间 22:00 含至次日 08:00 不含作主力）、
#     qodercn:Qwen-3.8-Max=2（CN 自定义友好名 Qwen-3.8-Max（有短横线），属 Qoder 组主力，
#     **不受内置 Max 时段门约束**，复用既有私有 model_ids 映射下发真实 ID）；
#   兜底（同级两池共 4 槽）：qoder:Qwen3.8-Flash=2、qodercn:Qwen3.8-Flash=2；
#   CN 独立补充池（qodercn:DeepSeek-Flash）仍在正常候选池移除（capacity=0，仅旧在途释放）。
# 主力优先级（BW-MAIN-PRIORITY-20261011-S1，覆盖旧跨层 best-effort 1:1；BW-CUSTOM-MAX-
# RESUME-20261018-S1 调整 Max 层内次序）：AUTO 只在**当前真实有空位**且合格的候选里，按主力
# 优先级层排序——三 Qoder Max（两内置 Max + 自定义 Max）最高，其中自定义
# qodercn:Qwen-3.8-Max 先于两个内置 Max，两内置之间同在途平衡；其次
# claude:deepseek-flash，最后 zcode:GLM-5.3；同层内以当前真实在途数少者优先，
# 平票才优先请求组合，再按 pool_key stable tie（`_select_main_force` 先按 capacity/active
# 过滤再排序）。历史 committed 计数（rotation 两列 committed_zcode / committed_qoder 与
# claude 提交历史）仍照旧记账并保留，但**不再参与跨层排序**，低层绝不能凭历史计数抢高层名额。
# 不新增记分系统，复用既有 rotation 列与 `_count_active` 即足够。
# BW-CLAUDE-DEEPSEEK-S3：Claude Code / deepseek-flash 纳入主力候选与 AUTO 可派集合，成为
# **独立第三公平组 claude**（与 Z2 组、Qoder 组并列）；BW-CLAUDE-CAPACITY2：容量由 1 增至 2。
# BW-CODEBUDDY-MAIN-S3（用户 2026-10-17 授权）：独立 CodeBuddy CLI 的精确模型
# **codebuddy:deepseek-v4.1-flash**（界面名 Deepseek-V4.1-Flash）为**最高优先主力**（独立
# 主力名额 1，优先级高于所有 Qwen3.8-Max）。该模型**不受任何时段门约束**（既不套 Claude
# 官方 API 离峰窗，也不套 Max 北京夜间窗）。
# BW-WORKER-WIDGET-20261018-S3（用户 2026-10-18 最新明确）：codebuddy:deepseek-v4.1-flash
# 与 codebuddy:glm-5.3-flash **不共享名额，各自独立 1 槽**（旧「同一物理 1 槽」口径作废）。
# 当前 AUTO 层序：CodeBuddy deepseek(0) > 三 Qoder Max(1) > claude(2) > ZCode(3) >
# 两 OpenCode 免费模型(4，同级、各自独立 1 槽、无时段门/无限额条件) >
# CodeBuddy GLM(5，额度独立的低层补位；BW-LIMIT30-20261019-S3 起不再要求当轮 DeepSeek
# 限额证据，缺省候选即含) > 两地区 Flash 兜底。
# claude 仍 2 槽。物理调度总数 DOMESTIC_TOTAL_CAPACITY = 12(主力，含两个 CodeBuddy 各 1
# 与两 OpenCode 各 1) + 4(两地区 Flash) = 16（由 MAIN_FORCE+OVERFLOW 求和自动得出）。
# WorkBuddy 仍 human-relay、CodeBuddy 其它旧模型（新旧 GLM 之外的组合）仍不恢复。
MAIN_FORCE_POOLS = (('codebuddy', 'deepseek-v4.1-flash'),
                    ('zcode', 'GLM-5.3'),
                    ('qoder', 'Qwen3.8-Max'),
                    ('qodercn', 'Qwen3.8-Max'),
                    ('qodercn', 'Qwen-3.8-Max'),
                    ('claude', 'deepseek-flash'),
                    ('opencode', 'deepseek-v4.1-flash-free'),
                    ('opencode', 'glm-5.3-flash-free'),
                    ('codebuddy', 'glm-5.3-flash'))
MAIN_FORCE_CAPACITY = {'codebuddy:deepseek-v4.1-flash': 1,
                       'codebuddy:glm-5.3-flash': 1,
                       'zcode:GLM-5.3': 2,
                       'qoder:Qwen3.8-Max': 1,
                       'qodercn:Qwen3.8-Max': 1,
                       'qodercn:Qwen-3.8-Max': 2,
                       'claude:deepseek-flash': 2,
                       'opencode:deepseek-v4.1-flash-free': 1,
                       'opencode:glm-5.3-flash-free': 1}
OVERFLOW_POOLS = (('qoder', 'Qwen3.8-Flash'), ('qodercn', 'Qwen3.8-Flash'))
OVERFLOW_CAPACITY = {'qoder:Qwen3.8-Flash': 2, 'qodercn:Qwen3.8-Flash': 2}
# BW-POOL-SPLIT-20261010-S2：CN DeepSeek-Flash 从正常池退出，但保留池名与 0 容量条目，
# 让已有真实在途 attempt 仍能通过 `_count_active` 与 `finish/reconcile` 到达自然终态
# 释放；不再有任何新 claim 落到该池（AUTO/executor=qodercn/显式 combo 均拒绝）。
LEGACY_RETIRED_POOL_KEYS = ('qodercn:DeepSeek-Flash',)
LUNA_RUNTIME = 'luna'
LUNA_MODEL = 'native'
LUNA_ASK_TIMEOUT_SECONDS = 300
DOMESTIC_TOTAL_CAPACITY = (sum(MAIN_FORCE_CAPACITY.values())
                           + sum(OVERFLOW_CAPACITY.values()))  # 12 (两个 CodeBuddy+两 OpenCode) + 4 = 16

# pool_key -> capacity（None 表示无上限，仅 Luna；0 表示已退出正常池但保留历史真实在途）。
CAPACITY = {}
CAPACITY.update(MAIN_FORCE_CAPACITY)
CAPACITY.update(OVERFLOW_CAPACITY)
for _rk in LEGACY_RETIRED_POOL_KEYS:
    CAPACITY[_rk] = 0
CAPACITY['luna:native'] = None
# BW-CLAUDE-DEEPSEEK-S3：claude:deepseek-flash 已并入 MAIN_FORCE_POOLS/CAPACITY（独立第三公平组
# claude），故其容量已在上面 CAPACITY.update 中登记；BW-CLAUDE-CAPACITY2 起容量为 2。
# 关键口径（BW-OPENCODE-20261019 现行权威）：
#  - DOMESTIC_TOTAL_CAPACITY = 16：两个 CodeBuddy 模型（deepseek-v4.1-flash / glm-5.3-flash）
#    与两个 OpenCode 免费模型（deepseek-v4.1-flash-free / glm-5.3-flash-free）
#    **各独立 1 槽、互不占位互斥**，claude 与它们在途都计入国内总在途（_domestic_active
#    只排除 Luna）；
#  - _select_main_force 按主力优先级层排序：codebuddy:deepseek-v4.1-flash 最高 > 三 Qoder
#    Max > claude:deepseek-flash > zcode:GLM-5.3 > 两 OpenCode 同级 >
#    codebuddy:glm-5.3-flash（低层补位，额度独立，BW-LIMIT30-20261019-S3）。
#    committed 仍只作历史记账、不参与跨层排序，低层绝不凭历史计数抢高层名额；
#  - claude 受 deepseek_offpeak 的北京时间工作日高峰门约束（高峰不得新派）；
#  - codebuddy:deepseek-v4.1-flash **不受任何时段门约束**：既不套 Claude 官方 API 离峰窗，
#    也不套 Max 北京夜间窗；仅经自身 capacity=1 与总在途 16 守卫，无任何跨模型回退。
#    两个 OpenCode 免费模型同样无时段门、无额度前提，仅经各自 capacity=1 与总守卫；
#  - codebuddy:glm-5.3-flash 同样无时段门，额度独立（BW-LIMIT30-20261019-S3 起不再要求
#    当轮 DeepSeek 限额证据，缺省候选即含），仅经自身 capacity=1 守卫。
#    CodeBuddy 其它旧模型仍不恢复、不进候选池。
CLAUDE_RUNTIME = 'claude'
CLAUDE_MODEL = 'deepseek-flash'
CLAUDE_POOL_KEY = f'{CLAUDE_RUNTIME}:{CLAUDE_MODEL}'
# 容量单一可信来源：MAIN_FORCE_CAPACITY（上面 CAPACITY.update 也取自它），此处只做同步引用，
# 避免两处常量漂移。
CLAUDE_CAPACITY = MAIN_FORCE_CAPACITY[CLAUDE_POOL_KEY]
# BW-CODEBUDDY-MAIN-S3：独立 CodeBuddy CLI 的精确恢复模型（界面名 Deepseek-V4.1-Flash）——
# 唯一被解禁为正式主力的 CodeBuddy 组合；最高优先主力层，capacity=1，无时段门、无跨模型回退。
CODEBUDDY_RUNTIME = 'codebuddy'
CODEBUDDY_MODEL = 'deepseek-v4.1-flash'
CODEBUDDY_POOL_KEY = f'{CODEBUDDY_RUNTIME}:{CODEBUDDY_MODEL}'
CODEBUDDY_CAPACITY = MAIN_FORCE_CAPACITY[CODEBUDDY_POOL_KEY]
# BW-CODEBUDDY-GLM-FALLBACK-20261018 用户授权：CodeBuddy / GLM-5.3-Flash（精确 CLI ID
# `codebuddy:glm-5.3-flash`）是**只在当轮 DeepSeek 真实明确限额后**启用的低层补位，优先级
# 低于 ZCode、高于两地区 Qwen3.8-Flash。BW-WORKER-WIDGET-20261018-S3 用户最新明确：它与
# deepseek-v4.1-flash **不共享名额，独立 1 槽**（已入 MAIN_FORCE_CAPACITY），不同工作区
# 两模型可各有 1 个在途并发；各自第 2 个仍按自身容量拒绝；一个结束只释放自己的槽。
CODEBUDDY_GLM_MODEL = 'glm-5.3-flash'
CODEBUDDY_GLM_POOL_KEY = f'{CODEBUDDY_RUNTIME}:{CODEBUDDY_GLM_MODEL}'
CODEBUDDY_GLM_CAPACITY = MAIN_FORCE_CAPACITY[CODEBUDDY_GLM_POOL_KEY]
# BW-OPENCODE-20261019 用户明确：OpenCode 独立 CLI 只开放两个**精确免费模型 ID**
# （deepseek-v4.1-flash-free / glm-5.3-flash-free），各自独立 1 槽、同级，优先级低于
# ZCode 且高于 CodeBuddy GLM（额度独立）；无时段门、无 CodeBuddy 额度前提、绝不自动付费回退。
OPENCODE_RUNTIME = 'opencode'
OPENCODE_DEEPSEEK_MODEL = 'deepseek-v4.1-flash-free'
OPENCODE_GLM_MODEL = 'glm-5.3-flash-free'
OPENCODE_MODELS = (OPENCODE_DEEPSEEK_MODEL, OPENCODE_GLM_MODEL)
OPENCODE_POOL_KEYS = tuple(f'{OPENCODE_RUNTIME}:{m}' for m in OPENCODE_MODELS)
OPENCODE_DEEPSEEK_POOL_KEY = OPENCODE_POOL_KEYS[0]
OPENCODE_GLM_POOL_KEY = OPENCODE_POOL_KEYS[1]
# 已弃用（BW-WORKER-WIDGET-20261018-S3）：两个 CodeBuddy 模型改为各自独立 1 槽，不再存在
# 跨模型共享物理槽。旧同名常量仅保留空元组供既有 import 兼容，任何代码/文案都不得再据此
# 推导共享容量或跨模型占位互斥。
CODEBUDDY_SHARED_POOL_KEYS: tuple = ()
MAIN_FORCE_KEYS = tuple(f'{r}:{m}' for r, m in MAIN_FORCE_POOLS)
OVERFLOW_KEYS = tuple(f'{r}:{m}' for r, m in OVERFLOW_POOLS)
# 兼容既有引用名（CN_KEYS/CN_POOL_KEY 保留为空元组/空串，代码不再据此派工）；
# 保留符号避免下游 import 报错，任何候选枚举只走 MAIN_FORCE_KEYS/OVERFLOW_KEYS。
CN_KEYS: tuple = ()
CN_RUNTIME = 'qodercn'
CN_MODEL = 'Qwen3.8-Max'
CN_POOL_KEY = f'{CN_RUNTIME}:{CN_MODEL}'  # 现指向 CN 主力 Qwen3.8-Max（正常池新组合）
CN_MAX_POOL_KEY = f'{CN_RUNTIME}:Qwen3.8-Max'
# BW-MAX-WINDOW-20261010-S2：CN 自定义 Qoder 主力友好名（短横线 Qwen-3.8-Max），真实 --model
# 经既有私有 model_ids 映射下发，池名以友好名为准，属 Qoder 主力组；BW-CUSTOM-MAX-NIGHT-
# 20261010-S1：与两内置 Max 同受北京时间主力时段门约束（仅 22:00 含至 08:00 不含新派工）。
CN_CUSTOM_MAX_POOL_KEY = f'{CN_RUNTIME}:Qwen-3.8-Max'
CN_CUSTOM_MAX_MODEL = 'Qwen-3.8-Max'
CN_FLASH_POOL_KEY = f'{CN_RUNTIME}:Qwen3.8-Flash'
QMAX_POOL_KEY = 'qoder:Qwen3.8-Max'
QFLASH_POOL_KEY = 'qoder:Qwen3.8-Flash'
DEEPSEEK_RETIRED_KEY = 'qodercn:DeepSeek-Flash'
LUNA_KEY = f'{LUNA_RUNTIME}:{LUNA_MODEL}'
# BW-POOL-SPLIT-20261010-S3 + BW-MAX-WINDOW-20261010-S2：可承载“明确限额”最小标记的主力池——
# 两个 Qoder 内置 Max（qoder:Qwen3.8-Max / qodercn:Qwen3.8-Max）与 CN 自定义 Qoder Max
# （qodercn:Qwen-3.8-Max）。ZCode 走既有 quota_control availability/hard_hold（本轮不改），
# 两地区 Flash 兜底永不被限额标记误挡；自定义 Max 与内置 Max 各自独立落标、互不连坐。标记只是
# 布尔不可用 + pool_key + 真实 attempt + 错误证据路径/哈希，绝不查余额、不带定时器/期限/
# 自动恢复探针/评分/新服务/外部 API；恢复只由用户明确额度恢复/重置后人工解除。
# BW-LIMIT30-20261019-S3 用户最新明确：ZCode/GLM-5.3 与 Claude/deepseek-flash 的真实结构化
# 429（Claude 另含真实 402 Insufficient Balance）落 main_force_limits 后，只在新派发的
# **1800 秒只读窗口**内跳过；窗口到期按现有优先级重新可选。原错误原件不清除、不宣称额度
# 恢复、不需要手动重置。Qoder Max 的既有标记语义不变（本窗口只作用于下列两个精确池）。
QUOTA_LIMIT_WINDOW_SECONDS = 1800
QUOTA_LIMIT_WINDOW_POOLS = ('zcode:GLM-5.3', CLAUDE_POOL_KEY)
QUOTA_LIMITABLE_POOLS = (QMAX_POOL_KEY, CN_MAX_POOL_KEY, CN_CUSTOM_MAX_POOL_KEY,
                         CLAUDE_POOL_KEY, 'zcode:GLM-5.3')
QUOTA_DISPLAY_ONLY_POOLS = (CODEBUDDY_POOL_KEY, CODEBUDDY_GLM_POOL_KEY)

# attempt 状态机：在途（占容量）与已释放（不占容量）。
ACTIVE_STATES = ('reserved', 'running', 'unknown')
RELEASED_STATES = ('finished', 'start_failed', 'cancelled', 'reconciled_exit')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts(
  token TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  stage TEXT,
  chat_id TEXT,
  prompt_sha256 TEXT,
  workspace TEXT,
  runtime TEXT NOT NULL,
  model TEXT NOT NULL,
  pool_key TEXT NOT NULL,
  state TEXT NOT NULL,
  wrapper_pid INTEGER,
  wrapper_created TEXT,
  child_pid INTEGER,
  child_created TEXT,
  reserved_at_utc TEXT,
  started_at_utc TEXT,
  ended_at_utc TEXT,
  terminal TEXT,
  success INTEGER,
  origin TEXT,
  adopt_evidence TEXT);
CREATE TABLE IF NOT EXISTS rotation(
  pool_group TEXT PRIMARY KEY,
  committed_zcode INTEGER NOT NULL DEFAULT 0,
  committed_qoder INTEGER NOT NULL DEFAULT 0,
  updated_at_utc TEXT);
CREATE TABLE IF NOT EXISTS luna_tickets(
  task_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  scope TEXT,
  ask_message_id TEXT,
  asked_at_utc TEXT,
  deadline_utc TEXT,
  reply_choice TEXT,
  reply_note TEXT,
  replied_at_utc TEXT,
  claimed_token TEXT,
  launch_state TEXT,
  agent_id TEXT,
  degradation_reason TEXT,
  degradation_detail TEXT,
  updated_at_utc TEXT);
CREATE TABLE IF NOT EXISTS main_force_limits(
  pool_key TEXT PRIMARY KEY,
  limited INTEGER NOT NULL DEFAULT 0,
  task_id TEXT,
  attempt_token TEXT,
  evidence_path TEXT,
  evidence_sha256 TEXT,
  recorded_at_utc TEXT,
  released_at_utc TEXT,
  release_note TEXT,
  updated_at_utc TEXT);
"""


# ------------------------------------------------------------------ 基础工具
def pool_key(runtime, model) -> str:
    return f'{runtime}:{model}'


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> str:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.astimezone(timezone.utc).isoformat()


def _parse(ts):
    if not ts:
        return None
    return datetime.fromisoformat(ts)


# ------------------------------------------------------------------ 主力时段（北京时间）
# BW-CUSTOM-MAX-NIGHT-20261010-S1：三个 Qoder Max 主力（国际内置 qoder:Qwen3.8-Max、CN
# 内置 qodercn:Qwen3.8-Max、CN 自定义 qodercn:Qwen-3.8-Max）只在北京时间 22:00（含）至
# 次日 08:00（不含）作为主力新派工；其它时间新 reserve/consume 自动跳过该三池。北京时间固
# 定按 UTC+8 计算，绝不依赖宿主系统时区：把注入的 now（缺省 utcnow）折算到 +08:00 再取小
# 时，与本地钟点无关。22:00 可选、08:00 不可选（左闭右开跨午夜窗口）。ZCode 主力、两地区
# Flash 兜底、Luna、已 retired DeepSeek 的时段一律不受本窗口限制。
BEIJING_UTC_OFFSET = timedelta(hours=8)
MAIN_FORCE_WINDOW_START_HOUR = 22  # 北京时间 22:00 含
MAIN_FORCE_WINDOW_END_HOUR = 8      # 北京时间 08:00 不含
# 这三个 Qoder Max 主力受同一时段门约束（BW-CUSTOM-MAX-NIGHT-20261010-S1：把 CN 自定义
# Qwen-3.8-Max 纳入相同时段门，AUTO/reserve/select/consume/claim_due 口径一致）。
WINDOW_GATED_MAIN_FORCES = (QMAX_POOL_KEY, CN_MAX_POOL_KEY, CN_CUSTOM_MAX_POOL_KEY)


def _beijing_hour(now) -> int:
    """把注入的 now 先归一到 UTC，再按固定 +08:00 偏移折算北京时间小时；now 为 naive 时按
    UTC 理解，绝不读取宿主本地时区。返回 0..23。"""
    if now is None:
        now = utcnow()
    if now.tzinfo is None:
        base = now.replace(tzinfo=timezone.utc)
    else:
        base = now.astimezone(timezone.utc)
    return (base + BEIJING_UTC_OFFSET).hour


def _main_force_window_open(now) -> bool:
    """北京时间主力可选窗口：h>=22（22:00 含）或 h<8（08:00 不含）→ 可选。"""
    h = _beijing_hour(now)
    return h >= MAIN_FORCE_WINDOW_START_HOUR or h < MAIN_FORCE_WINDOW_END_HOUR


def _max_window_blocks(pk, now) -> bool:
    """该池是否被时段门挡下（高峰不得新派、已预留跨到高峰时 consume 释放本 token）：
    - claude:deepseek-flash：由 deepseek_offpeak 判定（北京时间工作日 09:00–12:00、
      14:00–18:00 关闭；其余/周末/2026 假日放行；未知年度工作日高峰保守关闭）。offpeak
      不可用时 fail-closed（视为挡下，绝不在不确定时误派）。
    - 三个 Qoder Max（两内置 + CN 自定义）：非北京 22:00–08:00 窗口挡下。
    - 其它池永不挡。"""
    if pk == CLAUDE_POOL_KEY:
        if offpeak is None:
            return True
        return not offpeak.can_dispatch(now)
    return pk in WINDOW_GATED_MAIN_FORCES and not _main_force_window_open(now)


def _window_closed_reason(pk) -> str:
    """时段门拒绝的人类可读原因（按池区分 claude 高峰门 vs 三 Qoder Max 夜间门）。"""
    if pk == CLAUDE_POOL_KEY:
        return (f'the requested pool {pk!r} (Claude Code / deepseek-flash) may not start a '
                'new dispatch during Beijing-time weekday peak windows 09:00(inclusive)-'
                '12:00(exclusive) or 14:00(inclusive)-18:00(exclusive) (nor in an unknown '
                'year during a weekday peak, conservatively closed); off-hours, weekends and '
                'listed 2026 statutory holidays remain open, and it is never swapped to '
                'another model')
    return (f'the explicitly requested Qoder Max pool {pk!r} is only a main force during '
            'the Beijing-time window 22:00 (inclusive) to 08:00 (exclusive); it is now '
            'outside that window, so a new dispatch is honestly refused and never silently '
            'swapped to another model')


def default_store_path() -> Path:
    override = os.environ.get('BRAIN_WORKER_DISPATCH_STORE')
    if override:
        return Path(override)
    return Path.home() / '.brain-worker' / 'dispatch-pool.sqlite3'


# 首次并发初始化：多个 OS 进程同时 spawn 打开同一个（可能全新的）SQLite 库时，
# 切换到 WAL / 建表需要短暂排他锁，`PRAGMA journal_mode=WAL` 在并发下可能直接抛
# “database is locked”。生产初始化必须正确支持这种并发：对 busy/locked 做有界等待 +
# 重试，并在每次失败时可靠关闭那条尚未初始化完成的连接，绝不泄漏句柄。只吞
# busy/locked，其它 OperationalError 原样抛出（不掩盖真实缺陷，也不预建测试库）。
_CONNECT_BUSY_TIMEOUT_MS = 30000
_CONNECT_INIT_MAX_RETRY = 50
_CONNECT_INIT_BACKOFF_SECONDS = 0.02


def _is_busy_locked(exc) -> bool:
    msg = str(exc).lower()
    return 'database is locked' in msg or 'database is busy' in msg or 'database table is locked' in msg


def _init_connection(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=_CONNECT_BUSY_TIMEOUT_MS / 1000.0,
                           isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f'PRAGMA busy_timeout={_CONNECT_BUSY_TIMEOUT_MS}')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript(_SCHEMA)
        _migrate(conn)
    except BaseException:
        # 初始化中途失败：可靠关闭这条连接（含 busy/locked 与其它异常），不泄漏句柄。
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - close 失败也不能掩盖原异常
            pass
        raise
    return conn


def connect(store_path=None) -> sqlite3.Connection:
    path = Path(store_path) if store_path else default_store_path()
    if str(path) != ':memory:':
        path.parent.mkdir(parents=True, exist_ok=True)
    attempt = 0
    while True:
        try:
            return _init_connection(path)
        except sqlite3.OperationalError as exc:
            # 只对首次并发初始化的 busy/locked 做有界等待 + 重试；其它错误立即抛出。
            if not _is_busy_locked(exc) or attempt >= _CONNECT_INIT_MAX_RETRY:
                raise
            attempt += 1
            time.sleep(_CONNECT_INIT_BACKOFF_SECONDS * attempt)


def _migrate(conn):
    """老库升级：luna_tickets 补 agent_id 列、attempts 补 adopt_evidence 列（无破坏性）。"""
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(luna_tickets)')}
    if cols and 'agent_id' not in cols:
        try:
            conn.execute('ALTER TABLE luna_tickets ADD COLUMN agent_id TEXT')
        except sqlite3.OperationalError:
            pass
    acols = {r['name'] for r in conn.execute('PRAGMA table_info(attempts)')}
    if acols and 'adopt_evidence' not in acols:
        try:
            conn.execute('ALTER TABLE attempts ADD COLUMN adopt_evidence TEXT')
        except sqlite3.OperationalError:
            pass
    # BW-AVAILABILITY-20261009-B2：Luna 降级决策独立原因/明细列（无破坏性追加）。
    for col in ('degradation_reason', 'degradation_detail'):
        if cols and col not in cols:
            try:
                conn.execute(f'ALTER TABLE luna_tickets ADD COLUMN {col} TEXT')
            except sqlite3.OperationalError:
                pass


def capacity_for(pk) -> int | None:
    """已知池容量；Luna 无上限（None）；未列入分配决策的组合容量为 0（拒绝）。"""
    return CAPACITY.get(pk, 0)


def is_known_pool(pk) -> bool:
    return pk in CAPACITY


# ------------------------------------------------------------------ PID 身份
def _windows_process_created(pid):
    """Windows 只读获取进程创建时刻（FILETIME，64 位整数字符串），用于 PID 复用绑定。
    查询失败一律返回 None（调用方保守处理），绝不 os.kill、绝不终止任何进程。
    真实 Windows 上已退出的 PID 查不到创建时刻（created=None）属正常现象。"""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        class FILETIME(ctypes.Structure):
            _fields_ = [('lo', wintypes.DWORD), ('hi', wintypes.DWORD)]

        open_process = kernel32.OpenProcess
        open_process.restype = ctypes.c_void_p
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            get_times = kernel32.GetProcessTimes
            get_times.argtypes = [ctypes.c_void_p, ctypes.POINTER(FILETIME),
                                  ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                  ctypes.POINTER(FILETIME)]
            creation, exit_t, kernel_t, user_t = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not get_times(ctypes.c_void_p(handle), ctypes.byref(creation),
                             ctypes.byref(exit_t), ctypes.byref(kernel_t),
                             ctypes.byref(user_t)):
                return None
            return str((creation.hi << 32) | creation.lo)
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except (OSError, AttributeError, ImportError, ValueError):
        return None


def _linux_process_created(pid):
    """Linux 只读创建身份：/proc/PID/stat 的 starttime（第 22 字段）+ boot_id，
    组成 'boot_id:starttime' 稳定区分 PID 复用。/proc 不可读/字段异常一律 None
    （调用方按 unknown 处理，绝不臆造身份）。"""
    try:
        stat = Path(f'/proc/{int(pid)}/stat').read_text(encoding='utf-8')
        # comm 可能含空格/括号：以最后一个 ')' 切分，其后第 20 个字段即第 22 列 starttime。
        fields = stat[stat.rindex(')') + 2:].split()
        starttime = fields[19]
        boot = ''
        try:
            boot = Path('/proc/sys/kernel/random/boot_id').read_text(
                encoding='utf-8').strip()
        except OSError:
            pass
        return f'{boot}:{starttime}' if boot else str(starttime)
    except (OSError, ValueError, IndexError):
        return None


def _local_pid_state(pid, platform=None):
    """quota_control 缺席时的本地只读存活探测（Windows GetExitCodeProcess / POSIX 信号 0）。
    Z4：Windows 上父进程仍持 process 句柄时，OpenProcess 对已退出的子进程同样能成功，
    单凭句柄会把死进程判成 alive；必须声明 argtypes/restype 后用 GetExitCodeProcess，
    exit code == STILL_ACTIVE(259) 才算 alive，非 STILL_ACTIVE 才是 dead；探测失败
    （含 ERROR_ACCESS_DENIED）一律 unknown，绝不臆断 dead。句柄在 finally 中关闭。"""
    plat = str(platform or sys.platform).lower()
    if plat.startswith('win') or plat == 'nt':
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 0x103
            ERROR_INVALID_PARAMETER = 87
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.restype = wintypes.HANDLE
            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.restype = wintypes.BOOL
            get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            close_handle = kernel32.CloseHandle
            close_handle.restype = wintypes.BOOL
            close_handle.argtypes = [wintypes.HANDLE]
            handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                # 87 = 该 PID 不存在（真 dead）；拒绝访问等其它错误只能 unknown。
                if ctypes.get_last_error() == ERROR_INVALID_PARAMETER:
                    return 'dead'
                return 'unknown'
            try:
                code = wintypes.DWORD()
                if not get_exit_code(handle, ctypes.byref(code)):
                    return 'unknown'
                return 'alive' if code.value == STILL_ACTIVE else 'dead'
            finally:
                close_handle(handle)
        except (OSError, AttributeError, ImportError, ValueError):
            return 'unknown'
    try:
        os.kill(int(pid), 0)
        return 'alive'
    except ProcessLookupError:
        return 'dead'
    except (PermissionError, OSError):
        return 'unknown'


def process_identity(pid, platform=None):
    """只读进程身份：{pid, state: alive/dead/unknown, created}. Z3：created 在 Windows
    为 GetProcessTimes 的 FILETIME；在 /proc 可读的 Linux 为 boot_id+starttime。死进程或
    读取失败一律 None（没有身份，绝不臆造）。绝不带副作用（不终止、不信号投递，除 POSIX
    信号 0）。生命周期对账据此区分明确 dead 与 PID 复用（同 PID 活但 birth 不符 ≠ 原
    child 已死）。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {'pid': pid, 'state': 'dead', 'created': None}
    plat = str(platform or sys.platform).lower()
    if qc is not None:
        state = qc._windows_pid_state(pid) if (
            plat.startswith('win') or plat == 'nt') else (
            'alive' if qc._pid_alive(pid, platform) else 'dead')
    else:
        state = _local_pid_state(pid, platform)
    if plat.startswith('win') or plat == 'nt':
        created = _windows_process_created(pid)
    else:
        created = _linux_process_created(pid)
    return {'pid': pid, 'state': state, 'created': created}


def _created_matches(recorded, observed) -> bool:
    """PID 复用防护：任一侧创建时刻未知则不能确认同一进程（保守视为不匹配→不抢占）。"""
    if not recorded or not observed:
        return False
    return str(recorded) == str(observed)


# ------------------------------------------------------------------ 计数/查询
def _count_active(conn, pk) -> int:
    row = conn.execute(
        'SELECT COUNT(*) AS n FROM attempts WHERE pool_key=? AND state IN (%s)'
        % ','.join('?' * len(ACTIVE_STATES)), (pk, *ACTIVE_STATES)).fetchone()
    return int(row['n']) if row else 0


def _has_own_capacity(conn, pk) -> bool:
    """该池按**自身独立容量**是否仍有真实空位；容量为 None（Luna）视为无上限。
    BW-WORKER-WIDGET-20261018-S3：两个 CodeBuddy 模型各自独立 1 槽，此判断只数自身
    pool_key 的 reserved/running/unknown，绝不做跨模型占位互斥（旧 `_shared_active` /
    `_shared_has_capacity` / `shared_group_for` 共享组语义已随用户最新授权废弃移除）。"""
    cap = capacity_for(pk)
    if cap is None:
        return True
    return _count_active(conn, pk) < cap


def _count_committed_claude(conn) -> int:
    """claude 独立第三组的 committed 计数（BW-CLAUDE-DEEPSEEK-S3）：从**已有 attempts 表**的真实
    提交历史计算（`pool_key=claude:deepseek-flash` 且 `child_pid IS NOT NULL`，即真实启动过子进程
    的 attempt），绝不新增 rotation 列/表/框架。BW-MAIN-PRIORITY-20261011-S1 起 `_select_main_force`
    改按主力优先级层排序（三 Qoder Max > claude > zcode），本计数**仅保留作历史记账**（供
    status/rotation 观测），不再参与跨层排序或组间 best-effort 公平；claude 不计入
    committed_zcode/committed_qoder。"""
    row = conn.execute(
        'SELECT COUNT(*) AS n FROM attempts WHERE pool_key=? AND child_pid IS NOT NULL',
        (CLAUDE_POOL_KEY,)).fetchone()
    return int(row['n']) if row else 0


def _active_task(conn, task_id):
    return conn.execute(
        'SELECT * FROM attempts WHERE task_id=? AND state IN (%s) LIMIT 1'
        % ','.join('?' * len(ACTIVE_STATES)), (task_id, *ACTIVE_STATES)).fetchone()


def _norm_workspace(workspace):
    """真实 workspace 归一：normcase(realpath())，symlink/大小写/斜杠别名同目录归一。"""
    if not workspace:
        return workspace
    return os.path.normcase(os.path.realpath(str(workspace)))


def _workspace_conflict(conn, workspace, *, own_task_id=None, own_token=None):
    """Z4 同真实 workspace 单写入守卫：同一事务内核对 normcase(realpath(workspace))
    是否已有别的 task 的在途 attempt（reserved/running/unknown；包含 Luna 行——原生
    Luna 也在写同一真实目录）。同 task（own_task_id）或同 token（own_token）的原
    consume/续claim 除外。返回冲突行或 None；不做容量回收、不动业务占位。"""
    if not workspace:
        return None
    wn = _norm_workspace(workspace)
    for row in conn.execute(
            'SELECT * FROM attempts WHERE workspace=? AND state IN (%s)'
            % ','.join('?' * len(ACTIVE_STATES)), (wn, *ACTIVE_STATES)):
        if own_token is not None and row['token'] == own_token:
            continue
        if own_task_id is not None and row['task_id'] == own_task_id:
            continue
        return row
    return None


def _workspace_busy_reject(task_id, pk, row):
    return _reject('workspace_in_flight', task_id, pk,
                   [f'workspace {row["workspace"]!r} already has an in-flight attempt '
                    f'from a different task (token={row["token"]}, '
                    f'task={row["task_id"]!r}, state={row["state"]}); one real '
                    'workspace never takes two concurrent writers — no capacity is '
                    'reclaimed and no business placeholder is deleted'])


def _domestic_active(conn) -> int:
    """国内总在途保护计数（BW-POOL-SPLIT-20261010-S3 + BW-CLAUDE-DEEPSEEK-S3）：统计**所有
    非 Luna 的在途 attempt**，含 claude:deepseek-flash 以及仍在途的 retired DeepSeek 与超额
    旧 Max（迁移债务）。这样即便某个新池按各自容量看似有空位，只要真实总在途已达物理总容量
    （现为 16：主力含两个 CodeBuddy 各 1、两个 OpenCode 各 1 + claude 2 + 两地区 Flash 各 2），就不会派出超额的国内槽；减少容量不杀
    老任务，老任务依真实终态自然释放后计数才下降。Luna（救援池、无上限）不计入国内总额。"""
    row = conn.execute(
        'SELECT COUNT(*) AS n FROM attempts WHERE pool_key != ? AND state IN (%s)'
        % ','.join('?' * len(ACTIVE_STATES)), (LUNA_KEY, *ACTIVE_STATES)).fetchone()
    return int(row['n']) if row else 0


def _main_force_limit_active(conn, pk) -> bool:
    """事务内只读回核：该主力池是否被**明确限额**标记为不可用（limited=1）。这是原始标记
    诊断口径（不清除、不按时间篡改）；是否阻断新派发由 `_quota_limit_window_active` 按
    1800 秒真实失败窗口单独判定。ZCode/Claude 可承载标记；两地区 Flash 兜底、retired
    DeepSeek、Luna 一律返回 False（绝不误挡 Flash / 绝不误挡其它池）。"""
    if pk not in QUOTA_LIMITABLE_POOLS:
        return False
    row = conn.execute('SELECT limited FROM main_force_limits WHERE pool_key=?',
                       (pk,)).fetchone()
    return bool(row is not None and int(row['limited']) == 1)


def _limit_reference_utc(conn, row):
    """限额窗口的真实参照时刻：优先绑定 attempt 的 ended_at_utc（真实失败终态时刻），
    终态尚未落盘才回退标记 recorded_at_utc。绝不用其它操作的 updated_at 顶替，也不在
    ON CONFLICT 之外改写 recorded_at。"""
    ref = row['recorded_at_utc']
    token = row['attempt_token']
    if token:
        att = conn.execute('SELECT ended_at_utc FROM attempts WHERE token=?',
                           (token,)).fetchone()
        if att is not None and att['ended_at_utc']:
            ref = att['ended_at_utc']
    return ref


def _quota_limit_window_active(conn, pk, now=None) -> bool:
    """BW-LIMIT30-20261019-S3：仅 ZCode/GLM-5.3 与 Claude/deepseek-flash 两个精确池，
    从绑定真实失败的 ended_at_utc（回退 recorded_at_utc）起 0<=age<1800 秒内阻止新派发。
    恰好 1800 秒及更久返回 False（按现有优先级重新可选），但绝不删除原标记/证据、绝不
    宣称额度恢复。缺失/无时区/无法解析/未来时间不伪造有效窗口（不永久挡新轮）。每个
    pool_key 独立判定，互不连带。"""
    if pk not in QUOTA_LIMIT_WINDOW_POOLS:
        return False
    now = now or utcnow()
    row = conn.execute('SELECT limited, attempt_token, recorded_at_utc '
                       'FROM main_force_limits WHERE pool_key=?', (pk,)).fetchone()
    if row is None or int(row['limited']) != 1:
        return False
    # BW-LIMIT30-20261019-S4 缺口 2：绑定 attempt 的 ended_at_utc/recorded_at_utc 可能是
    # 历史上非法/损坏时间串，`fromisoformat` 会抛 ValueError——绝不能让只读窗口判定把整条
    # 派发路径带崩。非法时间等同"无法解析"：本窗口判定返回 False（不伪造有效限额窗口、
    # 不永久挡新轮），但**绝不删除或改写** main_force_limits 原标记与错误证据（不伪造恢复，
    # 原始 limited=1 事实仍在 status/诊断里如实回显）。
    try:
        ref = _parse(_limit_reference_utc(conn, row))
    except (ValueError, TypeError):
        return False
    if ref is None or ref.tzinfo is None:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age = (now - ref).total_seconds()
    return 0 <= age < QUOTA_LIMIT_WINDOW_SECONDS


def _rotation_row(conn):
    row = conn.execute('SELECT * FROM rotation WHERE pool_group=?', ('main',)).fetchone()
    if row is None:
        conn.execute('INSERT INTO rotation(pool_group, committed_zcode, committed_qoder) '
                     "VALUES('main',0,0)")
        row = conn.execute('SELECT * FROM rotation WHERE pool_group=?', ('main',)).fetchone()
    return row


def _bump_rotation(conn, pk, now):
    """committed 计数语义（BW-POOL-SPLIT-20261010-S2 + BW-MAX-WINDOW-20261010-S2）：
    - `zcode:GLM-5.3` → committed_zcode +1（Z2 组）；
    - `qoder:Qwen3.8-Max`、`qodercn:Qwen3.8-Max`（两内置 Max）与 `qodercn:Qwen-3.8-Max`
      （CN 自定义 Qoder Max）**合起来**记入 committed_qoder（Qoder 主力组，共享一列历史计数，
      代表 1:1 对侧 Z2），组内以真实在途数平衡；不新造轮换算法，自定义 Max 只是 Qoder 组新成员；
    - Flash/兜底、Luna、已退出正常池的 DeepSeek 均不再新增 rotation 记录。"""
    row = _rotation_row(conn)
    if pk == 'zcode:GLM-5.3':
        conn.execute('UPDATE rotation SET committed_zcode=committed_zcode+1, '
                     'updated_at_utc=? WHERE pool_group=?', (_iso(now), 'main'))
    elif pk in ('qoder:Qwen3.8-Max', 'qodercn:Qwen3.8-Max', CN_CUSTOM_MAX_POOL_KEY):
        conn.execute('UPDATE rotation SET committed_qoder=committed_qoder+1, '
                     'updated_at_utc=? WHERE pool_group=?', (_iso(now), 'main'))


def _luna_native_started(conn, task_id) -> bool:
    """同 task 的 Luna native 已启动（票据 claimed 或 launch_unknown）→ 不得再国内启动。"""
    row = conn.execute('SELECT state, launch_state FROM luna_tickets WHERE task_id=?',
                       (task_id,)).fetchone()
    if row is None:
        return False
    return row['state'] == 'claimed' or row['launch_state'] == 'launch_unknown'


def _ticket_answered_external(conn, task_id):
    """同 task 待援票据已被用户明确回复 external_agent/cancel → 不得再偷偷国内启动。"""
    row = conn.execute('SELECT state, reply_choice FROM luna_tickets WHERE task_id=?',
                       (task_id,)).fetchone()
    if row is None:
        return None
    if row['state'] == 'replied' and row['reply_choice'] in ('external_agent', 'cancel'):
        return row['reply_choice']
    return None


def _cancel_pending_ticket(conn, task_id, now, reason='domestic_slot_freed'):
    """同一事务内取消同 task 的 pending 待援票据（国内已拿到名额，绝不双派）。"""
    cur = conn.execute(
        "UPDATE luna_tickets SET state='cancelled', reply_note=?, updated_at_utc=? "
        "WHERE task_id=? AND state='pending'", (reason, _iso(now), task_id))
    return cur.rowcount


# ------------------------------------------------------------------ status
def status(store_path=None, now=None) -> dict:
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        pools = {}
        for pk, cap in CAPACITY.items():
            # active 始终是该 pool_key 自身的真实 attempt 数（同一 attempt 绝不复制两次）；
            # BW-WORKER-WIDGET-20261018-S3：两个 CodeBuddy 模型各自独立 1 槽，free 按自身
            # 容量独立计算，不再输出任何 shared_group/shared_capacity/shared_active 字段。
            active = _count_active(conn, pk)
            pools[pk] = {'capacity': cap, 'active': active,
                         'free': (None if cap is None else max(0, cap - active))}
        for lpk in QUOTA_LIMITABLE_POOLS:
            if lpk in pools:
                pools[lpk]['limited'] = _main_force_limit_active(conn, lpk)
        rot = _rotation_row(conn)
        domestic_active = _domestic_active(conn)
        tickets = [dict(r) for r in conn.execute('SELECT * FROM luna_tickets').fetchall()]
        limits = {r['pool_key']: dict(r) for r in
                  conn.execute('SELECT * FROM main_force_limits').fetchall()}
    return {'store': str(store_path or default_store_path()), 'now_utc': _iso(now),
            'pools': pools,
            'rotation': {'committed_zcode': rot['committed_zcode'],
                         'committed_qoder': rot['committed_qoder']},
            'domestic': {'capacity': DOMESTIC_TOTAL_CAPACITY,
                         'active': domestic_active,
                         'free': DOMESTIC_TOTAL_CAPACITY - domestic_active,
                         'full': domestic_active >= DOMESTIC_TOTAL_CAPACITY},
            'main_force_limits': limits,
            'luna_tickets': tickets}


# ------------------------------------------------------- 主力明确限额最小标记 API
# BW-POOL-SPLIT-20261010-S3：三主力明确限额都自动跳过、不查询余额。ZCode 走既有
# quota_control availability（本轮不改），两个 Qoder Max 用这里的**最小布尔标记**：只记
# “某主力已明确限额不可用”+ pool_key + 真实 attempt + 错误证据路径/哈希。绝不做额度系统、
# 绝不查余额、绝不引入定时器/24h 期限/自动恢复探针/评分/新服务/外部 API；恢复只由用户
# 明确额度恢复/重置后经 `release_main_force_limit` 人工解除，绝不靠成功旧日志自动清标记。
def record_main_force_limit(store_path=None, pk=None, *, task_id=None, attempt_token=None,
                            evidence_path=None, evidence_sha256=None, now=None) -> dict:
    """把一个主力池标记为明确限额不可用（limited=1）。只接受 QUOTA_LIMITABLE_POOLS（两个
    Qoder Max）；其它池（ZCode/Flash/retired/Luna）一律拒绝记录，绝不误挡 Flash、绝不改
    ZCode 既有 availability 口径。标记必须绑定**已存在且匹配 task/pool 的真实 attempt**与
    **真实错误证据路径/哈希**：attempt_token 对应的在途 attempt 必须落在同一 pk、task_id
    一致；evidence_path 必须是真实存在文件且其内容 sha256 与 evidence_sha256 一致，任一
    漂移一律拒绝（拒漂移），绝不凭一个 flag 就把主力钉死。幂等：对同一池重复记录只刷新
    证据与时间戳，仍 limited=1，并清空之前的解除痕迹（released_at/release_note）。"""
    now = now or utcnow()
    if pk not in QUOTA_LIMITABLE_POOLS and pk not in QUOTA_DISPLAY_ONLY_POOLS:
        return {'recorded': False, 'pool_key': pk, 'limited': False,
                'reasons': [f'{pk!r} is not a quota-limitable or display-only pool; only '
                            f'{QUOTA_LIMITABLE_POOLS + QUOTA_DISPLAY_ONLY_POOLS} carry '
                            'explicit-limit markers (ZCode uses quota_control availability; '
                            'Flash is never limited)']}
    drift = []
    if not evidence_path or not evidence_sha256:
        drift.append('an explicit-limit marker must bind a real error-evidence path and '
                     'its sha256; recording a bare flag without evidence is refused')
    if attempt_token is None or task_id is None:
        drift.append('an explicit-limit marker must bind a real attempt (task_id + '
                     'attempt_token); a marker without a matching attempt is refused')
    if not drift and evidence_path is not None:
        p = Path(str(evidence_path))
        if not p.is_file():
            drift.append(f'evidence_path {str(evidence_path)!r} does not exist; the '
                         'marker must reference a real error-evidence file')
        else:
            actual = _file_sha256(str(p))
            if actual is None or actual != evidence_sha256:
                drift.append('evidence_sha256 does not match the actual error-evidence '
                             'file content (evidence drift)')
    if drift:
        return {'recorded': False, 'pool_key': pk, 'limited': False, 'drift': True,
                'reasons': drift}
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT task_id, pool_key, state FROM attempts '
                               'WHERE token=?', (attempt_token,)).fetchone()
            if row is None:
                conn.execute('ROLLBACK')
                return {'recorded': False, 'pool_key': pk, 'limited': False, 'drift': True,
                        'reasons': [f'no attempt found for token {attempt_token!r}; an '
                                    'explicit-limit marker must bind a real attempt']}
            if row['pool_key'] != pk:
                conn.execute('ROLLBACK')
                return {'recorded': False, 'pool_key': pk, 'limited': False, 'drift': True,
                        'reasons': [f'attempt pool_key drift: attempt={row["pool_key"]!r} '
                                    f'marker={pk!r}']}
            if row['task_id'] != task_id:
                conn.execute('ROLLBACK')
                return {'recorded': False, 'pool_key': pk, 'limited': False, 'drift': True,
                        'reasons': [f'attempt task_id drift: attempt={row["task_id"]!r} '
                                    f'marker={task_id!r}']}
            conn.execute(
                'INSERT INTO main_force_limits(pool_key, limited, task_id, attempt_token, '
                'evidence_path, evidence_sha256, recorded_at_utc, updated_at_utc) '
                'VALUES(?, 1, ?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(pool_key) DO UPDATE SET limited=1, '
                'task_id=excluded.task_id, attempt_token=excluded.attempt_token, '
                'evidence_path=excluded.evidence_path, '
                'evidence_sha256=excluded.evidence_sha256, '
                'recorded_at_utc=excluded.recorded_at_utc, released_at_utc=NULL, '
                'release_note=NULL, updated_at_utc=excluded.updated_at_utc',
                (pk, task_id, attempt_token, evidence_path, evidence_sha256,
                 _iso(now), _iso(now)))
            conn.execute('COMMIT')
            return {'recorded': True, 'pool_key': pk, 'limited': True,
                    'task_id': task_id, 'attempt_token': attempt_token,
                    'evidence_path': evidence_path, 'evidence_sha256': evidence_sha256,
                    'recorded_at_utc': _iso(now)}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def release_main_force_limit(store_path=None, pk=None, *, note=None, now=None) -> dict:
    """人工解除接口：仅在用户明确额度恢复/重置后把 limited 置 0（保留历史行与解除时间/备注）。
    绝不宣称余额/免费、绝不靠成功旧日志自动清标记、绝不自动恢复。无标记行则无可解除。"""
    now = now or utcnow()
    if pk not in QUOTA_LIMITABLE_POOLS and pk not in QUOTA_DISPLAY_ONLY_POOLS:
        return {'released': False, 'pool_key': pk,
                'reasons': [f'{pk!r} is not a quota-limitable or display-only pool']}
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT limited FROM main_force_limits WHERE pool_key=?',
                               (pk,)).fetchone()
            if row is None:
                conn.execute('ROLLBACK')
                return {'released': False, 'pool_key': pk, 'limited': False,
                        'reasons': ['no explicit-limit marker recorded for this pool; '
                                    'nothing to release']}
            conn.execute(
                'UPDATE main_force_limits SET limited=0, released_at_utc=?, release_note=?, '
                'updated_at_utc=? WHERE pool_key=?', (_iso(now), note, _iso(now), pk))
            conn.execute('COMMIT')
            return {'released': True, 'pool_key': pk, 'limited': False,
                    'released_at_utc': _iso(now), 'release_note': note}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def main_force_limited(store_path=None, pk=None, *, now=None) -> bool:
    """只读查询：某主力池当前是否被明确限额标记为不可用。非可限额池恒 False。"""
    with closing(connect(store_path)) as conn:
        return _main_force_limit_active(conn, pk)


def main_force_limits(store_path=None, *, now=None) -> dict:
    """只读查询：返回全部主力限额标记（含已解除行），供人工核验；键为 pool_key。"""
    with closing(connect(store_path)) as conn:
        return {r['pool_key']: dict(r) for r in
                conn.execute('SELECT * FROM main_force_limits').fetchall()}


# ------------------------------------------------------------------ 统一国内策略
ZCODE_POOL_KEY = 'zcode:GLM-5.3'


def _resolve_quota_store(quota_store=None):
    """BW-AVAILABILITY-20261009-B5 缺陷 5：把"显式参数 → 环境变量 BRAIN_WORKER_QUOTA_STORE
    → 受信任默认持久文件"统一成一处，供**正式 API 与 CLI 默认**共用，使默认 AUTO 也会读取
    共享 availability 过滤不可用 Z，而不是只有显式传参才过滤。qc 缺席则不查询（保持旧 dp
    行为，绝不假装可用/不可用）。解析得到的路径只用于**只读**回核，绝不建库/建表/迁移、绝不在
    pool 事务里写 quota 库。"""
    if quota_store:
        return quota_store
    if qc is None:
        return None
    try:
        return qc.default_store_path()
    except Exception:  # noqa: BLE001 - 无法确定默认路径：保守不查询（不建库、不误判）
        return None


def _zcode_availability_blocked(quota_store=None, now=None, routes_path=None):
    """BW-SIMPLE-PRIORITY-RETRY-S4 用户政策：普通新一轮**不再据历史 ZCode availability
    阻断**——每轮从最高优先级真实候选起真实尝试，真实失败才落 availability 诊断、成功仍
    epoch/attempt CAS 结算。这里对新派工直接返回 None（视为未阻断），且**不读取真实 quota
    库**（避免每个旧容量测试误读/依赖本机共享状态，也不谎称 provider 侧 healthy/免费）。
    历史 availability 诊断仍由 qc.zcode_availability_status / qc.zcode_blocking_rows 保留
    可查；显式 recovery probe（purpose='probe'）生命周期与 epoch CAS 结算完全不变，仅在
    普通工程路径不再作为派工前置闸。绝不因本函数而永久或到期后挡新轮真实尝试。"""
    return None


def _probe_allowed(quota_store, probe_ticket, now=None):
    """缺陷 4：容量门只放行**经真实 CAS 状态核验过**的 bound recovery probe 票据，绝不
    信任调用方自报的 probe 布尔。probe_ticket 必须携带精确授予行 key 与 attempt_id/epoch，
    由 qc.zcode_probe_ticket_valid 只读回核。任一不符 → False（正常派工照旧被拒）。"""
    if not probe_ticket or qc is None:
        return False
    store = _resolve_quota_store(quota_store)
    if store is None:
        return False
    try:
        return bool(qc.zcode_probe_ticket_valid(
            store, provider=probe_ticket.get('provider'),
            channel_key=probe_ticket.get('channel_key'),
            attempt_id=probe_ticket.get('attempt_id'),
            epoch=probe_ticket.get('epoch'), now=now))
    except Exception:  # noqa: BLE001 - fail-closed: never trust an unverifiable ticket
        return False


def _eligible_main_forces(conn, zcode_blocked, now=None, codebuddy_glm_allowed=False):
    """可用主力集合（BW-POOL-SPLIT-20261010-S3 + BW-MAX-WINDOW-20261010-S1 +
    BW-LIMIT30-20261019-S3）：先按时段门与 1800 秒真实限额窗口过滤，再在可用主力间按
    现有优先级层选择。
    - 三个 Qoder Max 在北京时间非 22:00（含）至 08:00（不含）时段的**新派工候选**里剔除；
    - ZCode/GLM-5.3 与 Claude/deepseek-flash 若绑定了**真实结构化 429/402** 且距该真实
      失败结束不足 1800 秒，剔除；恰好 1800 秒及更久按现有优先级重新可选（不删除原标记，
      不宣称恢复）。其它池（Qoder Max 的既有标记、CodeBuddy、Flash）不受该时间窗口影响；
    - `codebuddy_glm_allowed` 仅为旧调用兼容保留：BW-LIMIT30-20261019-S3 起 GLM 与
      DeepSeek 额度独立，不再需要任何当轮 DeepSeek 限额证据，始终按自身 1 槽参与候选。
    BW-SIMPLE-PRIORITY-RETRY-S3 的历史标记不挡新轮政策，只对上述两个精确池的 1800 秒
    真实窗口例外；其它历史 availability/限额仍仅作诊断。"""
    eligible = set()
    for k in MAIN_FORCE_KEYS:
        if _max_window_blocks(k, now):
            continue
        if _quota_limit_window_active(conn, k, now):
            continue
        eligible.add(k)
    return eligible


def _scope_combo_from_scope(scope):
    """BW-AVAILABILITY-20261009-B5 缺陷 6：受信任 combo 用 **executor + model** 表达；
    runtime 可被接受但绝不成为必需的冗余字段——scope={executor:qoder, model:Qwen3.8-Flash}
    _without_ 额外 runtime 也必须解析成唯一的 qoder:Qwen3.8-Flash。返回 (combo_or_None,
    conflict)：
    - 同时给了 runtime 与 executor 且前缀不一致 → (None, True)（冲突，调用方 fail-closed，
      绝不猜一个池放行）；
    - 给了 model 且能定出唯一池前缀（executor 优先，其次 runtime）→ 该 pool_key；若该 combo
      不在已知池内、或落在已退出正常池的 LEGACY_RETIRED_POOL_KEYS（含 DeepSeek-Flash），
      一律视为矛盾 fail-closed；
    - 只有 executor 无 model（或纯 auto）→ (None, False)，交由候选集合按执行器展开。"""
    executor = (scope or {}).get('executor')
    runtime = (scope or {}).get('runtime')
    model = (scope or {}).get('model')
    exec_runtime = {'qoder': 'qoder', 'zcode': 'zcode',
                    CN_RUNTIME: CN_RUNTIME, 'claude': 'claude',
                    CODEBUDDY_RUNTIME: CODEBUDDY_RUNTIME,
                    OPENCODE_RUNTIME: OPENCODE_RUNTIME}.get(executor)
    if runtime and exec_runtime and str(runtime) != exec_runtime:
        return None, True
    combo_runtime = exec_runtime or (str(runtime) if runtime else None)
    if model:
        if combo_runtime is None:
            return None, False
        combo = pool_key(combo_runtime, str(model))
        if combo not in CAPACITY or combo in LEGACY_RETIRED_POOL_KEYS:
            return None, True
        return combo, False
    return None, False


def _scope_allowed_candidates(scope, zcode_blocked, codebuddy_glm_allowed=False):
    """按票据 scope 的执行器/combo 硬约束给出该任务真正受信任的候选池（BW-POOL-SPLIT-
    20261010-S2）：优先顺序是**主力在前、两地区 Flash 同级兜底在后**，CN DeepSeek-Flash
    已退出正常候选池不再作为独立补充候选。scope 明确只要 ZCode 而 ZCode 当前不可用 →
    返回空；scope 明确只要 Qoder 或只要 Qoder CN 时只列自身 Max/Flash，绝不跨执行器
    改派。combo 与执行器冲突、未知 model 或已 retired 的 DeepSeek-Flash combo 一律
    fail-closed 返回空候选，绝不臆造池。"""
    executor = (scope or {}).get('executor')
    combo, conflict = _scope_combo_from_scope(scope)
    if conflict:
        return []
    if combo is not None:
        if combo == ZCODE_POOL_KEY and zcode_blocked:
            return []
        return [combo]
    if executor == 'zcode':
        return [] if zcode_blocked else [ZCODE_POOL_KEY]
    if executor == 'qoder':
        return [QMAX_POOL_KEY, QFLASH_POOL_KEY]
    if executor == CN_RUNTIME:
        return [CN_MAX_POOL_KEY, CN_CUSTOM_MAX_POOL_KEY, CN_FLASH_POOL_KEY]
    if executor == 'claude':
        # 显式指定 claude 入口：只有独立 claude 池自身，绝不跨到其它国内槽或 Flash/回退模型。
        return [CLAUDE_POOL_KEY]
    if executor == CODEBUDDY_RUNTIME:
        # 显式指定 CodeBuddy 入口：两个精确模型各自独立 1 槽，均直接可用（BW-LIMIT30-
        # 20261019-S3 撤销”GLM 需当轮 DeepSeek 限额证据”的前置门；额度互不连带）。
        return [CODEBUDDY_POOL_KEY, CODEBUDDY_GLM_POOL_KEY]
    if executor == OPENCODE_RUNTIME:
        # 显式 OpenCode 入口：只列两个精确免费模型，同级、无时段门/额度前提，绝不跨到
        # CodeBuddy/其它池，也绝不自动付费回退。
        return list(OPENCODE_POOL_KEYS)
    mains = [k for k in MAIN_FORCE_KEYS
             if not (k == ZCODE_POOL_KEY and zcode_blocked)]
    # 默认/auto 受信任候选：三主力在前、两地区 Flash 同级兜底在后；已 retired 的 CN
    # DeepSeek-Flash 不在候选集合中，任何 AUTO 派工绝不新落该池。
    return mains + list(OVERFLOW_KEYS)


def _select_main_force(conn, requested_pk, now, eligible=None):
    """主力选择（BW-MAIN-PRIORITY-20261011-S1，覆盖旧跨层 best-effort 1:1）：
    1. 候选 = `MAIN_FORCE_KEYS` 经 `eligible` 过滤（ZCode availability 阻断、三 Qoder Max
       明确限额/北京时段门外都先剔除，绝不把名额分给不合格主力）；
    2. 只保留**当前真实有空位**的池（capacity_for > `_count_active`）——绝不因历史
       committed 计数拦有空位的候选；
    3. 排序第一键 = 主力优先级层：codebuddy:deepseek-v4.1-flash=0（最高，无时段门），三
       Qoder Max（qoder:Qwen3.8-Max / qodercn:Qwen3.8-Max / qodercn:Qwen-3.8-Max）=1，
       claude:deepseek-flash=2（容量 2），zcode:GLM-5.3=3。历史 committed、请求池偏好与在途
       数**绝不能**让低层抢高层名额；最高层全部不合格或无空位才落到下一层；
    3b. BW-CUSTOM-MAX-RESUME-20261018-S1：Max 层内增加自定义子优先键——CN 自定义
       qodercn:Qwen-3.8-Max（0）先于两个内置 Max（1）；该子键位于真实在途数之前，故自定义
       即使已有 1 在途、两内置 0 在途，仍先选自定义直到其 2 槽全满；两内置之间继续按在途
       数平衡。CodeBuddy 仍高于所有 Max，claude/ZCode 层序不变；
    4. 同层（同一子优先键）内保持现有在途平衡：当前真实在途数（`_count_active`）少者优先，
       平票才优先请求的组合，再按 pool_key stable tie；rotation 历史 committed 计数照旧
       保留记账，但不再参与跨层竞争；
    5. 全部主力都无可用槽才返回 main_force_full（由调用方走两地区 Flash 兜底）；
    6. 不新增记分/服务/额度，只用已有 attempts 表真实计数。
    返回 (target_pk, reason)。"""
    order = list(MAIN_FORCE_KEYS)
    if eligible is not None:
        order = [k for k in order if k in eligible]
    # BW-LIMIT30-20261019-S3：GLM 与 DeepSeek 额度独立，默认候选同样包含 GLM（层=4，
    # ZCode 之后、两区 Flash 之前）；不再需要当轮 DeepSeek 限额证据。
    avail = []
    for pk in order:
        cap = capacity_for(pk)
        if cap is None:
            continue
        # 两个精确池的真实 1800 秒限额窗口：只影响新派发，已运行 attempt 原样保留。
        if _quota_limit_window_active(conn, pk, now):
            continue
        # BW-WORKER-WIDGET-20261018-S3：所有池（含两个 CodeBuddy 模型）都按自身独立
        # 容量与自身在途数判断空位，绝不做跨模型占位互斥。
        active = _count_active(conn, pk)
        if active < cap:
            avail.append((pk, active))
    if not avail:
        return None, 'main_force_full'
    # BW-CODEBUDDY-MAIN-S3：最高优先层为 codebuddy:deepseek-v4.1-flash（无时段门），其后依序
    # 三 Qoder Max、claude:deepseek-flash、zcode:GLM-5.3（全天可用者垫底）。低层绝不凭历史
    # committed 抢高层名额；最高层不合格或无空位才落到下一层。
    # 完整层序：CodeBuddy deepseek(0) > CN 自定义 Max 与两内置 Max(1) > Claude(2) >
    # ZCode(3) > 两 OpenCode 同级(4) > CodeBuddy GLM(5，额度独立，缺省候选即含) > 两地区 Flash（主力之外）。
    # BW-LIMIT30-20261019-S3：GLM 默认参与排序（不再要求当轮 DeepSeek 限额证据），仅当
    # 授权候选未含它、或它自身处于真实 1800 秒限额窗口时才被剔除。
    tier = {CODEBUDDY_POOL_KEY: 0,
            QMAX_POOL_KEY: 1, CN_MAX_POOL_KEY: 1, CN_CUSTOM_MAX_POOL_KEY: 1,
            CLAUDE_POOL_KEY: 2, ZCODE_POOL_KEY: 3,
            OPENCODE_DEEPSEEK_POOL_KEY: 4, OPENCODE_GLM_POOL_KEY: 4,
            CODEBUDDY_GLM_POOL_KEY: 5}
    # BW-CUSTOM-MAX-RESUME-20261018-S1：Max 层内自定义子优先（在途数/tie 之前）——
    # qodercn:Qwen-3.8-Max 先于两个内置 Max，两内置之间仍按在途数平衡。保留原 tier 数值，
    # 仅加一个子键，不新建优先级框架。
    max_sub = {CN_CUSTOM_MAX_POOL_KEY: 0, QMAX_POOL_KEY: 1, CN_MAX_POOL_KEY: 1}

    def _key(item):
        pk, active = item
        return (tier.get(pk, len(tier)), max_sub.get(pk, 0), active,
                0 if pk == requested_pk else 1, pk)

    avail.sort(key=_key)
    return avail[0][0], 'main_force_selected'


def _flash_fallback_or_full(conn, requested_pk):
    """主力均不可用或已满 → 两地区 Flash **同级**兜底（BW-POOL-SPLIT-20261010-S2）：
    先按真实在途数少者优先，平票才按同区偏好（requested 若属 qodercn 则优先 CN Flash，
    属 qoder 则优先国际 Flash；ZCode 入口两区平权），再按 pool_key stable tie；命中
    空位则：入口已点名该 Flash → claim，否则 routing 到该 Flash。两 Flash 都满才返回
    物理 full。CN DeepSeek-Flash 已从正常池 retired，绝不作补充候选、绝不再派。"""
    def _region_pref(pk):
        if requested_pk.startswith('qodercn:'):
            return 0 if pk.startswith('qodercn:') else 1
        if requested_pk.startswith('qoder:') and not requested_pk.startswith('qodercn:'):
            return 0 if pk.startswith('qoder:') and not pk.startswith('qodercn:') else 1
        return 0
    flash = list(OVERFLOW_KEYS)
    flash.sort(key=lambda pk: (_count_active(conn, pk), _region_pref(pk), pk))
    for pk in flash:
        cap = capacity_for(pk)
        if cap is not None and _count_active(conn, pk) < cap:
            if requested_pk == pk:
                return 'claim', pk
            return 'routing', pk
    return 'full', None


def _domestic_policy(conn, requested_pk, now, executor='auto', eligible=None,
                     zcode_blocked=None, probe_allowed=False,
                     codebuddy_glm_allowed=False):
    """reserve 与 select-and-claim 共用的统一国内裁决（BW-POOL-SPLIT-20261010-S2）：
    返回 ('claim', pk) / ('routing', target_pk) / ('full', None) /
    ('zcode_unavailable', None) / ('executor_conflict', None) / ('reject_luna', None) /
    ('unknown_pool', None)。
    - Luna 永不在此自动放行（只能经 claim-due 票据裁决）；
    - 已 retired 的 CN DeepSeek-Flash 一律 `unknown_pool`：不新派、绝不作为 AUTO/executor
      任何分支的候选；已存在的真实在途 attempt 由 finish/reconcile 独立释放，与本策略无关；
    - executor='qoder'（可信主脑显式指定 Qoder 入口）：有空位且**未被明确限额**的
      qoder:Qwen3.8-Max 一律直接 claim，绝不因历史 committed 计数被改派到 ZCode 或 CN；
      国际内置 Max **仅北京时间 22:00（含）~次日 08:00（不含）做主力**，时段外一律
      `main_force_window_closed` 如实拒绝（不新启动、绝不静默换成同区 Flash）；
      Max 满**或 Max 被明确限额标记命中** → routing 到自身 qoder:Qwen3.8-Flash（同级
      Max→Flash 兜底，sent=false 明示改道、绝不偷偷提交限额 Max）；Flash 也满 → full；
      绝不跨执行器；提交非 qoder 组合 → executor_conflict；
    - executor='qodercn'（受信任主脑显式指定 CN 原生入口）：有空位且未被明确限额的
      qodercn:Qwen3.8-Max 直接 claim，同样**仅主力时段**、时段外 `main_force_window_closed`
      拒绝；自定义 qodercn:Qwen-3.8-Max（BW-CUSTOM-MAX-NIGHT-20261010-S1：与两内置 Max 同
      套主力时段门、时段外如实拒绝；时段内仍复用限额判定）有空且未限额 → 直接
      claim；Max 满或被明确限额 → routing 到 qodercn:Qwen3.8-Flash；Flash 也满 → full；
      绝不跨执行器；提交非 CN 组合 → executor_conflict；
    - executor='zcode'（ZCode 入口，最后的权威原子门）：ZCode 不可用 → zcode_unavailable；
      有空位 → claim；满 → full（绝不回落 Flash/其它池）；提交非 ZCode 组合 → executor_conflict；
    - executor='auto'：先过滤合格候选（时段外的三 Qoder Max（两内置 + 自定义）；历史
      availability/限额标记只作诊断、不再剔除），再在可用主力之间按既有优先级层 + 真实在途
      数选择；本池有空也不绕过层序；选中≠请求 → routing；主力都不可用/已满 → 两地区 Flash
      同级兜底；两 Flash 也满才 full。
    - 迁移债务总额守卫（BW-CLAUDE-DEEPSEEK-S4 起对全部国内池生效，含 claude/codebuddy）：
      真实国内在途（含在途 retired DeepSeek 与超额旧 Max）已达总 16 时，即便某池按各自容量
      看似有空也一律 full，绝不派第 17 个。"""
    if requested_pk == LUNA_KEY:
        return 'reject_luna', None
    if requested_pk in LEGACY_RETIRED_POOL_KEYS:
        return 'unknown_pool', None
    # BW-CLAUDE-DEEPSEEK-S4 + BW-CODEBUDDY-MAIN-S3：claude/codebuddy 已计入
    # DOMESTIC_TOTAL_CAPACITY=16，总守卫必须同样约束：真实国内在途（含迁移债务/旧超额在途）
    # 已满 16 时，即便该池按自身容量看似有空也绝不派第 17 个。显式点名 claude
    # （executor='claude' 或 None）保持
    # 精确组合：时段/限额/总守卫都过且有空位才 claim，否则如实拒绝，绝不回退国内模型；
    # AUTO 请求 claude（executor='auto'）绝不绕过三组公平选择——落入下方 auto 分支，与
    # 其它主力一样经 `_eligible_main_forces`（高峰/限额剔除）+ `_select_main_force` 轮转。
    if requested_pk == CLAUDE_POOL_KEY:
        if executor not in ('auto', 'claude', None):
            return 'executor_conflict', None
        if executor == 'auto':
            if _max_window_blocks(CLAUDE_POOL_KEY, now):
                # AUTO 的 claude 入口高峰请求：如实拒绝新派，绝不绕过公平选择偷认领自己。
                return 'main_force_window_closed', None
        else:
            # BW-LIMIT30-20261019-S3：真实 402 Insufficient Balance / 明确额度 429 落标后
            # 1800 秒内不新派；到期自动按现有优先级重新可选，不删原标记、不宣称恢复。
            if _quota_limit_window_active(conn, CLAUDE_POOL_KEY, now):
                return 'main_force_limited', None
            if _domestic_active(conn) >= DOMESTIC_TOTAL_CAPACITY:
                return 'full', None
            if _max_window_blocks(CLAUDE_POOL_KEY, now):
                return 'main_force_window_closed', None
            # BW-SIMPLE-PRIORITY-RETRY-S3：历史明确限额标记不再挡普通新一轮（真实尝试失败
            # 才落诊断、CAS 结算不变）；claude 仍受总守卫与时段门约束，绝不误放第 17 个。
            if _count_active(conn, CLAUDE_POOL_KEY) < capacity_for(CLAUDE_POOL_KEY):
                return 'claim', CLAUDE_POOL_KEY
            return 'full', None
    # BW-CODEBUDDY-MAIN-S3：codebuddy:deepseek-v4.1-flash 是最高优先独立主力（capacity=1），
    # **不受任何时段门约束**（不套 Claude 离峰窗、不套 Max 夜间窗）。显式点名 CodeBuddy
    # （executor='codebuddy' 或 None）保持精确组合：只在总守卫（16）与本池自身 capacity=1
    # 有空时 claim，否则如实拒绝，绝不跨模型回退到其它国内池/Flash；历史旧 GLM/WorkBuddy
    # 冷却一律不挡此新模型（本分支只认新 pool_key，不查任何冷却）。BW-WORKER-WIDGET-
    # 20261018-S3：GLM 在途**不再**挡本池（各自独立 1 槽）。AUTO 请求 codebuddy
    # （executor='auto'）不直接认领，落到下方统一 auto 分支经 `_select_main_force` 公平选择
    # （codebuddy 为最高层，有空位即优先，绝不绕过层序）。
    if requested_pk == CODEBUDDY_POOL_KEY:
        if executor not in ('auto', 'codebuddy', None):
            return 'executor_conflict', None
        if executor in ('codebuddy', None):
            if _domestic_active(conn) >= DOMESTIC_TOTAL_CAPACITY:
                return 'full', None
            if _has_own_capacity(conn, CODEBUDDY_POOL_KEY):
                return 'claim', CODEBUDDY_POOL_KEY
            return 'full', None
    # BW-CODEBUDDY-GLM-FALLBACK-20261018 + BW-WORKER-WIDGET-20261018-S3 +
    # BW-LIMIT30-20261019-S3：GLM 与 deepseek-v4.1-flash 额度独立、各自独立 1 槽；不再需要
    # 当轮 DeepSeek 明确限额证据。GLM 仍是层=4（ZCode 之后、两区 Flash 之前），仅在自身
    # capacity=1 真实有空位时 claim，绝不自动切换/重派、绝不消耗通用 reserve 来“证明”额度、
    # 绝不给 Max 夜间窗或 Claude 离峰窗待遇（无时段门）。DeepSeek 在途不挡 GLM 自身名额。
    if requested_pk == CODEBUDDY_GLM_POOL_KEY:
        if executor not in ('auto', CODEBUDDY_RUNTIME, None):
            return 'executor_conflict', None
        if executor != 'auto':
            if _domestic_active(conn) >= DOMESTIC_TOTAL_CAPACITY:
                return 'full', None
            if _has_own_capacity(conn, CODEBUDDY_GLM_POOL_KEY):
                return 'claim', CODEBUDDY_GLM_POOL_KEY
            return 'full', None
        # executor='auto'：不直接认领，落入下方统一 auto 分支；更高优先主力全部
        # 受限/满/不合格后才轮到 tier=5 的 GLM。
    # BW-OPENCODE-20261019：两个 OpenCode 免费模型各自独立 1 槽、同级、无时段门、
    # 无额度前提。显式点名（executor='opencode' 或 None）时只在本池自身有空位时 claim，
    # 满即如实 full，绝不自动付费回退/跨模型；AUTO 请求不直接认领，落入统一 auto 分支按
    # tier=4 公平选择（两模型之间按真实在途数 + stable tie 平衡）。
    if requested_pk in OPENCODE_POOL_KEYS:
        if executor not in ('auto', OPENCODE_RUNTIME, None):
            return 'executor_conflict', None
        if executor in (OPENCODE_RUNTIME, None):
            if _domestic_active(conn) >= DOMESTIC_TOTAL_CAPACITY:
                return 'full', None
            if _has_own_capacity(conn, requested_pk):
                return 'claim', requested_pk
            return 'full', None
    # 迁移债务总额守卫（BW-POOL-SPLIT-20261010-S3）：总在途已满 DOMESTIC_TOTAL_CAPACITY
    # （现为 16）→ 任何新国内 claim 都不得放行（减少容量不杀老任务，老任务真实终态自然
    # 释放后计数才下降）。
    if _domestic_active(conn) >= DOMESTIC_TOTAL_CAPACITY:
        return 'full', None
    elig = set(eligible) if eligible is not None else set(MAIN_FORCE_KEYS)

    if executor == 'qoder':
        if requested_pk == QMAX_POOL_KEY:
            # BW-MAX-WINDOW-20261010-S2：国际内置 Max 仅北京 22:00（含）~次日 08:00（不含）
            # 做主力。时段外无论 reserve/select_and_claim/consume 一律如实拒绝新启动（0 提交），
            # 绝不静默换成同区 Flash，也不为旧测试放宽生产规则。AUTO 改派另由 eligible 过滤。
            if _max_window_blocks(QMAX_POOL_KEY, now):
                return 'main_force_window_closed', None
            # BW-SIMPLE-PRIORITY-RETRY-S3：历史明确限额不挡新一轮真实尝试（失败才落诊断、
            # CAS 结算不变）；Max 有空位即 claim，满了才同执行器 Flash 兜底（Max→Flash），
            # 绝不跨执行器。
            cap = capacity_for(requested_pk)
            if cap is not None and _count_active(conn, requested_pk) < cap:
                return 'claim', requested_pk
            if capacity_for(QFLASH_POOL_KEY) is not None \
                    and _count_active(conn, QFLASH_POOL_KEY) < capacity_for(QFLASH_POOL_KEY):
                return 'routing', QFLASH_POOL_KEY
            return 'full', None
        if requested_pk == QFLASH_POOL_KEY:
            cap = capacity_for(requested_pk)
            if cap is not None and _count_active(conn, requested_pk) < cap:
                return 'claim', requested_pk
            return 'full', None
        return 'executor_conflict', None

    if executor == CN_RUNTIME:
        if requested_pk == CN_MAX_POOL_KEY:
            # BW-MAX-WINDOW-20261010-S2：CN 内置 Max 同样仅主力时段可用；时段外无论
            # reserve/select_and_claim/consume 一律如实拒绝新启动，绝不静默换成同区 CN Flash。
            if _max_window_blocks(CN_MAX_POOL_KEY, now):
                return 'main_force_window_closed', None
            # BW-SIMPLE-PRIORITY-RETRY-S3：历史明确限额不挡新一轮真实尝试；CN 内置 Max
            # 有空即 claim，满了才 CN Flash 兜底（Max→Flash），绝不误挡 Flash、绝不跨执行器。
            cap = capacity_for(requested_pk)
            if cap is not None and _count_active(conn, requested_pk) < cap:
                return 'claim', requested_pk
            if capacity_for(CN_FLASH_POOL_KEY) is not None \
                    and _count_active(conn, CN_FLASH_POOL_KEY) < capacity_for(CN_FLASH_POOL_KEY):
                return 'routing', CN_FLASH_POOL_KEY
            return 'full', None
        if requested_pk == CN_CUSTOM_MAX_POOL_KEY:
            # BW-CUSTOM-MAX-NIGHT-20261010-S1：自定义 CN Max（Qwen-3.8-Max）纳入与两内置 Max
            # 相同的北京时间主力时段门（仅 22:00 含至 08:00 不含做主力）；时段外无论
            # reserve/select_and_claim/consume 一律如实拒绝新启动，绝不静默换成同区 CN Flash。
            # 时段内复用既有明确限额判定；有空位且未限额 → 直接 claim；已限额或已满 → CN
            # Flash 兜底，绝不误挡 Flash、绝不跨执行器。
            if _max_window_blocks(CN_CUSTOM_MAX_POOL_KEY, now):
                return 'main_force_window_closed', None
            # BW-SIMPLE-PRIORITY-RETRY-S3：历史明确限额不挡新一轮真实尝试；自定义 CN Max
            # 有空即 claim，满了才 CN Flash 兜底，绝不误挡 Flash、绝不跨执行器。用户明确
            # 暂停该自定义组合仍由候选授权范围（不在 allowed/scope 内）体现，不由此限额门表达。
            cap = capacity_for(requested_pk)
            if cap is not None and _count_active(conn, requested_pk) < cap:
                return 'claim', requested_pk
            if capacity_for(CN_FLASH_POOL_KEY) is not None \
                    and _count_active(conn, CN_FLASH_POOL_KEY) < capacity_for(CN_FLASH_POOL_KEY):
                return 'routing', CN_FLASH_POOL_KEY
            return 'full', None
        if requested_pk == CN_FLASH_POOL_KEY:
            cap = capacity_for(requested_pk)
            if cap is not None and _count_active(conn, requested_pk) < cap:
                return 'claim', requested_pk
            return 'full', None
        return 'executor_conflict', None

    if executor == 'zcode':
        if requested_pk != ZCODE_POOL_KEY:
            return 'executor_conflict', None
        # BW-SIMPLE-PRIORITY-RETRY-S4 用户政策：普通新一轮即便调用方/mock 传入历史
        # availability blocked，也**不因此拒绝**——直接尝试真实 ZCode 池（受容量/工作区/
        # 去重/unknown 在途照常约束，绝不抢占别人的在途名额）。真实失败才落 availability
        # 诊断、成功仍 epoch/attempt CAS 结算，不假装 healthy。显式 recovery probe 的
        # 生命周期/陈旧成功保护由 quota_control 入口与结算独立保证，不在此前置拦截。
        # BW-LIMIT30-20261019-S3：若最近一次真实结构化 429 距其 ended_at_utc 不足 1800 秒，
        # 直接 ZCode 入口必须零 Popen 拒绝；恰好 1800 秒及更久自动恢复现有优先级可选。
        if _quota_limit_window_active(conn, requested_pk, now):
            return 'main_force_limited', None
        cap = capacity_for(requested_pk)
        if cap is not None and _count_active(conn, requested_pk) < cap:
            return 'claim', requested_pk
        return 'full', None

    # executor == 'codebuddy' 但请求的不是精确恢复组合：绝不落回 AUTO 选择/别的池。
    if executor == CODEBUDDY_RUNTIME:
        return 'executor_conflict', None
    # executor == 'opencode' 但请求的不是两个精确免费组合：绝不落回 AUTO/别的池。
    if executor == OPENCODE_RUNTIME:
        return 'executor_conflict', None

    # executor == 'auto'
    if requested_pk in MAIN_FORCE_KEYS:
        # BW-SIMPLE-PRIORITY-RETRY-S3：普通 AUTO 不再因历史 availability 把 ZCode 改派走；
        # ZCode 与其它主力一样进 elig（已不含 availability/限额剔除），按优先级层选择：
        # 有空位且为最高优先层的候选直接 claim（真实尝试），选中≠请求才 routing，绝不因旧
        # hard_hold/backoff 标记永久挡新轮。时段/容量/总守卫/unknown/工作区仍照常。
        target, _ = _select_main_force(conn, requested_pk, now, eligible=elig)
        if target is not None:
            if target == requested_pk:
                return 'claim', requested_pk
            return 'routing', target
        return _flash_fallback_or_full(conn, requested_pk)
    if requested_pk in OVERFLOW_KEYS:
        target, _ = _select_main_force(conn, requested_pk, now, eligible=elig)
        if target is not None:
            return 'routing', target
        cap = capacity_for(requested_pk)
        if cap is not None and _count_active(conn, requested_pk) < cap:
            return 'claim', requested_pk
        # 本 Flash 已满：另一地区 Flash 是同级兜底候选，有空位则 routing 过去。
        for k in OVERFLOW_KEYS:
            if k == requested_pk:
                continue
            if capacity_for(k) is not None and _count_active(conn, k) < capacity_for(k):
                return 'routing', k
        return 'full', None
    return 'unknown_pool', None


def _preclaim_reconcile(conn, now):
    """每次新 claim 前的安全对账：BW-AVAILABILITY-20261009-B4 缺陷 6 收紧——
    只有当已绑定 child **且**原 wrapper 都经只读探针确认已死亡、且 wrapper 创建身份
    与记录一致时才自动回收执行容量（reconciled_exit）。任一未知一律不动：
    - child 存活/未知 → 保留占位；
    - wrapper 仍在世（还在做原生 finish/终态处理）→ 保留，交给显式 finish，绝不抢；
    - wrapper PID 被复用（当前 created 与记录 wrapper_created 不符）→ 视为未知，保留；
    - 无 PID 记录 / 探针失败 → 保守保留（交给显式 reconcile）。
    自动回收只释放执行容量，绝不清除 availability（额度可用性由 quota 侧独立状态管理）。"""
    rows = conn.execute(
        'SELECT * FROM attempts WHERE state IN (%s) AND pool_key != ? '
        'AND child_pid IS NOT NULL' % ','.join('?' * len(ACTIVE_STATES)),
        (*ACTIVE_STATES, LUNA_KEY)).fetchall()
    released = []
    for row in rows:
        try:
            ident = process_identity(int(row['child_pid']))
        except Exception:
            continue
        if ident.get('state') != 'dead':
            continue  # child 存活/未知：保守保留
        # wrapper（真实调用者）身份必须齐备才能自动回收。BW-AVAILABILITY-20261009-B5：
        # 缺 wrapper_pid 或缺 wrapper_created → 无法确认原调用已结束，保守保留（交显式
        # finish/reconcile），绝不在空窗放行同 workspace 新 writer。
        wpid = row['wrapper_pid']
        recorded_created = row['wrapper_created'] if 'wrapper_created' in row.keys() else None
        if wpid is None or recorded_created is None:
            continue  # 无原记录身份/创建时刻：保守保留
        try:
            wident = process_identity(int(wpid))
        except Exception:
            continue
        if wident.get('state') != 'dead':
            continue  # 原 wrapper 仍活着，可能正写终态
        if wident.get('created') and \
                str(recorded_created) != str(wident.get('created')):
            continue  # PID 复用：创建身份不符，视为未知，保守保留
        conn.execute("UPDATE attempts SET state='reconciled_exit', "
                     "terminal='reconciled_exit', ended_at_utc=? WHERE token=? "
                     "AND state IN (%s)" % ','.join('?' * len(ACTIVE_STATES)),
                     (_iso(now), row['token'], *ACTIVE_STATES))
        released.append(row['token'])
    return released


def _insert_claim(conn, *, task_id, stage, chat_id, prompt_sha256, workspace,
                  runtime, model, pk, token, now, origin,
                  wrapper_pid=None, wrapper_created=None) -> str:
    """插入一个 claim。给了 wrapper_pid（entry 在 Popen 前的真实调用者身份）则直接
    running 并记录 wrapper 创建身份；否则 reserved（等待 consume CAS）。"""
    tok = token or uuid.uuid4().hex
    if wrapper_pid is not None and wrapper_created is None:
        wrapper_created = process_identity(int(wrapper_pid)).get('created')
    if wrapper_pid is not None:
        conn.execute(
            'INSERT INTO attempts(token, task_id, stage, chat_id, prompt_sha256, workspace, '
            'runtime, model, pool_key, state, wrapper_pid, wrapper_created, '
            'started_at_utc, reserved_at_utc, origin) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (tok, task_id, stage, chat_id, prompt_sha256,
             _norm_workspace(workspace),
             runtime, model, pk, 'running', int(wrapper_pid), wrapper_created,
             _iso(now), _iso(now), origin))
    else:
        conn.execute(
            'INSERT INTO attempts(token, task_id, stage, chat_id, prompt_sha256, workspace, '
            'runtime, model, pool_key, state, reserved_at_utc, origin) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (tok, task_id, stage, chat_id, prompt_sha256,
             _norm_workspace(workspace),
             runtime, model, pk, 'reserved', _iso(now), origin))
    return tok


# ------------------------------------------------------------------ 路由选择
def select_and_claim(store_path=None, *, task_id, runtime, model, workspace,
                     prompt_sha256, stage=None, chat_id=None, token=None,
                     now=None, origin='select-and-claim',
                     wrapper_pid=None, wrapper_created=None,
                     executor='auto', quota_store=None, quota_routes=None,
                     probe_ticket=None, codebuddy_glm_allowed=False,
                     _preclaim=True) -> dict:
    """无显式 claim 时的原子路由选择（与 reserve 共用 _domestic_policy，语义集中）：
    - 请求 Luna → 拒绝（Luna 只经 claim-due 竞争裁决，绝不自动派生）；
    - 请求溢出 Flash：主力仍有空位 → routing_required 到该主力；主力与 Flash 均满但 CN
      仍空 → routing_required 到 CN（Flash 入口不因自身满就报整池 full）；
    - 请求主力：按 committed 轮换；当前入口非目标 → routing_required（不提交错模型）；
      两主力都满 → 溢出 Flash（有空位则直接 claim）；Flash 也满时 CN 仍是有空位的补充
      候选 → routing_required 到 CN（入口已是 CN 则 claim），CN 也满（原 8 全满）才拒绝。
    防重：同 task 已有在途 attempt，或同 task Luna native 已启动 → 一律拒绝。
    auto consume 场景传 wrapper_pid（默认由 consume_for_entry 填 os.getpid()），
    Popen 前即记录 wrapper 真实创建身份。"""
    now = now or utcnow()
    pk = pool_key(runtime, model)
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if _preclaim:
                _preclaim_reconcile(conn, now)
            # Z3：先判 native 已启动再判普通在途——同 task 的 Luna attempt 本身就是在途
            # 行，先给准确的 task_already_on_luna（仍一律拒绝，绝不因换理由放松）。
            if _luna_native_started(conn, task_id):
                conn.execute('ROLLBACK')
                return _reject('task_already_on_luna', task_id, pk,
                               ['a native Luna launch for this task was already claimed '
                                '(or is launch_unknown); the task can never fall back to '
                                'a domestic start afterwards'])
            dup = _active_task(conn, task_id)
            if dup is not None:
                conn.execute('ROLLBACK')
                return _reject('duplicate_task_in_flight', task_id, pk,
                               [f'task {task_id!r} already has an in-flight attempt '
                                f'(token={dup["token"]}, pool={dup["pool_key"]}, '
                                f'state={dup["state"]}); a task can never double-start'])
            answered = _ticket_answered_external(conn, task_id)
            if answered is not None:
                conn.execute('ROLLBACK')
                return _reject('task_answered_external', task_id, pk,
                               [f'the user explicitly replied {answered!r} to the ask '
                                'ticket; a domestic start for this task is not allowed'])
            # Z4：真实 workspace 单写入守卫先于路由/容量裁决——别的 task 已在同目录
            # 在途写入则直接拒绝（sent=false），不得改到 Luna/Flash 绕过。
            busy = _workspace_conflict(conn, workspace, own_task_id=task_id)
            if busy is not None:
                conn.execute('ROLLBACK')
                return _workspace_busy_reject(task_id, pk, busy)
            zcode_blocked = _zcode_availability_blocked(quota_store, now=now,
                                                        routes_path=quota_routes)
            eligible = _eligible_main_forces(conn, zcode_blocked, now,
                                             codebuddy_glm_allowed=codebuddy_glm_allowed)
            probe_allowed = _probe_allowed(quota_store, probe_ticket, now=now)
            verdict, target = _domestic_policy(conn, pk, now, executor=executor,
                                               eligible=eligible,
                                               zcode_blocked=zcode_blocked,
                                               probe_allowed=probe_allowed,
                                               codebuddy_glm_allowed=codebuddy_glm_allowed)
            domestic_full = _domestic_active(conn) >= DOMESTIC_TOTAL_CAPACITY
            if verdict == 'unknown_pool':
                conn.execute('ROLLBACK')
                return _reject('unknown_pool', task_id, pk,
                               [f'runtime/model {runtime!r}/{model!r} is not part of the '
                                f'sanctioned allocation (main force zcode:GLM-5.3 / '
                                f'qoder:Qwen3.8-Max / qodercn:Qwen3.8-Max, same-tier '
                                f'overflow qoder:Qwen3.8-Flash + qodercn:Qwen3.8-Flash, '
                                f'rescue luna:native; the retired '
                                f'qodercn:DeepSeek-Flash pool is no longer dispatched); '
                                f'refusing to dispatch an unsanctioned combo'])
            if verdict == 'zcode_unavailable':
                conn.execute('ROLLBACK')
                return _reject('zcode_unavailable', task_id, pk,
                               ['ZCode:GLM-5.3 is unavailable per the persistent ZCode '
                                'availability state (hard-quota hold / bounded backoff / '
                                'recovery-unverified); the ZCode entry is the authoritative '
                                'last atomic gate and is never routed back to an '
                                'unavailable ZCode'] + (zcode_blocked.get('reasons') or []))
            if verdict == 'executor_conflict':
                conn.execute('ROLLBACK')
                return _reject('executor_conflict', task_id, pk,
                               [f'executor {executor!r} may not submit combo {pk}; the '
                                'explicit executor constraint forbids this runtime/model'])
            if verdict == 'main_force_window_closed':
                conn.execute('ROLLBACK')
                return _reject('main_force_window_closed', task_id, pk,
                               [_window_closed_reason(pk)])
            if verdict == 'main_force_limited':
                conn.execute('ROLLBACK')
                return _reject('main_force_limited', task_id, pk,
                               [f'{pk} has a real structured quota failure less than '
                                '1800 seconds old; new dispatch is skipped inside that '
                                'read-only window (the original failure evidence is kept, '
                                'no quota recovery is claimed, and no manual reset is '
                                'required). After 1800 seconds the pool becomes selectable '
                                'again at its existing priority'])
            if verdict == 'reject_luna':
                conn.execute('ROLLBACK')
                return _reject('luna_requires_ticket', task_id, pk,
                               ['Luna is rescue-only and is never auto-selected here; it '
                                'may only be claimed via claim-due after a real ask ticket '
                                'expired with all eight domestic slots full and no reply'])
            if verdict == 'routing':
                tr, tm = target.split(':', 1)
                if pk in OVERFLOW_KEYS and target in MAIN_FORCE_KEYS:
                    why = ('main force still has a free slot; overflow Qwen3.8-Flash must '
                           f'not be used while {target} has capacity')
                elif target in OVERFLOW_KEYS:
                    why = (f'main force pools are full; continue via the same-tier '
                           f'overflow entry {target} instead of submitting {pk} under a '
                           'Flash pool_key')
                else:
                    why = (f'best-effort 1:1 rotation selected {target} for this task; the '
                           f'current entry ({pk}) is not the selected combo — continue via '
                           'the selected entry instead of submitting the wrong model')
                conn.execute('ROLLBACK')
                return _routing(task_id, pk, target, [why], domestic_full=domestic_full)
            if verdict == 'full':
                conn.execute('ROLLBACK')
                if pk == CLAUDE_POOL_KEY:
                    full_reasons = [f'{pk} (Claude Code / deepseek-flash) is at its capacity '
                                    f'of {CLAUDE_CAPACITY} — an additional concurrent '
                                    'dispatch to this independent pool is refused and never '
                                    'falls back to another model; wait for the real terminal '
                                    'release']
                elif pk == CODEBUDDY_POOL_KEY:
                    full_reasons = [f'{pk} (standalone CodeBuddy / deepseek-v4.1-flash) is at '
                                    f'its capacity of {CODEBUDDY_CAPACITY} — a second '
                                    'concurrent dispatch to this independent pool is refused '
                                    'and never falls back to another model or an old CodeBuddy '
                                    'channel; wait for the real terminal release']
                elif pk == CODEBUDDY_GLM_POOL_KEY:
                    full_reasons = [f'{pk} (low-tier fallback CodeBuddy / glm-5.3-flash) is '
                                    f'at its own independent capacity of '
                                    f'{CODEBUDDY_GLM_CAPACITY} (it does not share a slot '
                                    f'with {CODEBUDDY_POOL_KEY}); a second concurrent '
                                    'dispatch to this model is refused and never falls back '
                                    'to another model — wait for the real terminal release']
                else:
                    full_reasons = ['the sanctioned domestic candidate set for this combo is '
                                    'fully occupied; ask the user (external agent or Luna) and '
                                    'record an ask ticket']
                return _reject('capacity_full', task_id, pk,
                               full_reasons,
                               domestic_full=(_domestic_active(conn) >=
                                              DOMESTIC_TOTAL_CAPACITY))
            claim_pk = target
            tok = _insert_claim(conn, task_id=task_id, stage=stage, chat_id=chat_id,
                                prompt_sha256=prompt_sha256, workspace=workspace,
                                runtime=runtime, model=model, pk=claim_pk,
                                token=token, now=now, origin=origin,
                                wrapper_pid=wrapper_pid,
                                wrapper_created=wrapper_created)
            _bump_rotation(conn, claim_pk, now)
            # Z2：同 task 国内救回时同一事务取消旧 pending 待援票，防止之后被 claim_due
            # 再次 Luna（双派）。
            _cancel_pending_ticket(conn, task_id, now)
            conn.execute('COMMIT')
            return {'allowed': True, 'claimed': True, 'token': tok, 'pool_key': claim_pk,
                    'routing_required': False, 'reasons': [], 'task_id': task_id,
                    'selected': {'runtime': runtime, 'model': model},
                    'domestic_active': _domestic_active(conn)}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def reserve(store_path=None, *, task_id, runtime, model, workspace, prompt_sha256,
            stage=None, chat_id=None, token=None, now=None, origin='reserve',
            executor='auto', quota_store=None, quota_routes=None,
            probe_ticket=None, codebuddy_glm_allowed=False,
            _preclaim=True) -> dict:
    """为确切 (runtime, model) 预留一个 claim。修订后与 select-and-claim 共用统一国内
    策略（_domestic_policy）：空库/主力有空时 reserve Flash 一律 routing_required 到
    主力（不保留任何绕过主力顺序的口子）；reserve Luna 一律拒绝（只能经 ask 票据 +
    claim-due）。主脑/测试据此拿到确定 token，交给对应入口 --dispatch-claim 精确消费。"""
    now = now or utcnow()
    pk = pool_key(runtime, model)
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if _preclaim:
                _preclaim_reconcile(conn, now)
            # Z3：与 select_and_claim 相同顺序——native 已启动的准确理由优先于普通在途。
            if _luna_native_started(conn, task_id):
                conn.execute('ROLLBACK')
                return _reject('task_already_on_luna', task_id, pk,
                               ['a native Luna launch for this task was already claimed '
                                '(or is launch_unknown); no domestic start afterwards'])
            dup = _active_task(conn, task_id)
            if dup is not None:
                conn.execute('ROLLBACK')
                return _reject('duplicate_task_in_flight', task_id, pk,
                               [f'task {task_id!r} already has an in-flight attempt '
                                f'(token={dup["token"]}, state={dup["state"]})'])
            answered = _ticket_answered_external(conn, task_id)
            if answered is not None:
                conn.execute('ROLLBACK')
                return _reject('task_answered_external', task_id, pk,
                               [f'the user explicitly replied {answered!r} to the ask '
                                'ticket; a domestic start for this task is not allowed'])
            # Z4：真实 workspace 单写入守卫先于路由/容量裁决（与 select_and_claim 同）。
            busy = _workspace_conflict(conn, workspace, own_task_id=task_id)
            if busy is not None:
                conn.execute('ROLLBACK')
                return _workspace_busy_reject(task_id, pk, busy)
            zcode_blocked = _zcode_availability_blocked(quota_store, now=now,
                                                        routes_path=quota_routes)
            eligible = _eligible_main_forces(conn, zcode_blocked, now,
                                             codebuddy_glm_allowed=codebuddy_glm_allowed)
            probe_allowed = _probe_allowed(quota_store, probe_ticket, now=now)
            verdict, target = _domestic_policy(conn, pk, now, executor=executor,
                                               eligible=eligible,
                                               zcode_blocked=zcode_blocked,
                                               probe_allowed=probe_allowed,
                                               codebuddy_glm_allowed=codebuddy_glm_allowed)
            if verdict == 'unknown_pool':
                conn.execute('ROLLBACK')
                return _reject('unknown_pool', task_id, pk,
                               [f'{runtime!r}/{model!r} is not a sanctioned pool'])
            if verdict == 'zcode_unavailable':
                conn.execute('ROLLBACK')
                return _reject('zcode_unavailable', task_id, pk,
                               ['ZCode:GLM-5.3 is unavailable per the persistent ZCode '
                                'availability state; reserve never hands out a slot on an '
                                'unavailable ZCode'] + (zcode_blocked.get('reasons') or []))
            if verdict == 'executor_conflict':
                conn.execute('ROLLBACK')
                return _reject('executor_conflict', task_id, pk,
                               [f'executor {executor!r} may not reserve combo {pk}'])
            if verdict == 'main_force_window_closed':
                conn.execute('ROLLBACK')
                return _reject('main_force_window_closed', task_id, pk,
                               [_window_closed_reason(pk)])
            if verdict == 'main_force_limited':
                conn.execute('ROLLBACK')
                return _reject('main_force_limited', task_id, pk,
                               [f'{pk} has a real structured quota failure less than '
                                '1800 seconds old; reserve is skipped inside that read-only '
                                'window without clearing the original failure evidence or '
                                'claiming quota recovery. After 1800 seconds the pool '
                                'becomes selectable again at its existing priority'])
            if verdict == 'reject_luna':
                conn.execute('ROLLBACK')
                return _reject('luna_requires_ticket', task_id, pk,
                               ['Luna is rescue-only; reserve never hands out a Luna '
                                'slot — only claim-due after a real, expired, unreplied '
                                'ask ticket with all eight domestic slots full'])
            if verdict == 'routing':
                conn.execute('ROLLBACK')
                return _routing(task_id, pk, target,
                                [f'unified domestic policy: {pk} may not be reserved '
                                 f'while {target} still has capacity; reserve {target} '
                                 'instead'],
                                domestic_full=_domestic_active(conn) >= DOMESTIC_TOTAL_CAPACITY)
            if verdict == 'full':
                conn.execute('ROLLBACK')
                if pk == CLAUDE_POOL_KEY:
                    full_reason = (f'{pk} (Claude Code / deepseek-flash) is at its capacity '
                                   f'of {CLAUDE_CAPACITY} — an additional concurrent '
                                   'dispatch to this independent pool is refused and never '
                                   'falls back to any other model or the domestic pools')
                elif pk == CODEBUDDY_POOL_KEY:
                    full_reason = (f'{pk} (standalone CodeBuddy / deepseek-v4.1-flash) is at '
                                   f'its capacity of {CODEBUDDY_CAPACITY} — a second '
                                   'concurrent dispatch is refused and never falls back to '
                                   'another model or an old CodeBuddy channel')
                elif pk == CODEBUDDY_GLM_POOL_KEY:
                    full_reason = (f'{pk} (low-tier fallback CodeBuddy / glm-5.3-flash) is '
                                   f'at its own independent capacity of '
                                   f'{CODEBUDDY_GLM_CAPACITY} (it does not share a slot with '
                                   f'{CODEBUDDY_POOL_KEY}); no second concurrent dispatch is '
                                   'reserved and no automatic switch is performed')
                else:
                    full_reason = f'{pk} and every other domestic pool is at capacity'
                return _reject('capacity_full', task_id, pk,
                               [full_reason],
                               domestic_full=(_domestic_active(conn) >=
                                              DOMESTIC_TOTAL_CAPACITY))
            tok = _insert_claim(conn, task_id=task_id, stage=stage, chat_id=chat_id,
                                prompt_sha256=prompt_sha256, workspace=workspace,
                                runtime=runtime, model=model, pk=pk, token=token,
                                now=now, origin=origin)
            if pk in MAIN_FORCE_KEYS:
                _bump_rotation(conn, pk, now)
            _cancel_pending_ticket(conn, task_id, now)
            conn.execute('COMMIT')
            return {'allowed': True, 'reserved': True, 'token': tok, 'pool_key': pk,
                    'routing_required': False, 'reasons': [], 'task_id': task_id,
                    'selected': {'runtime': runtime, 'model': model}}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def _reject(reason, task_id, pk, reasons, domestic_full=False):
    return {'allowed': False, 'sent': False, 'claimed': False, 'token': None,
            'routing_required': reason == 'routing_required',
            'reason': reason, 'pool_key': pk, 'task_id': task_id,
            'reasons': reasons, 'domestic_full': domestic_full}


def _routing(task_id, requested_pk, target_pk, reasons, domestic_full=False):
    tr, tm = target_pk.split(':', 1)
    return {'allowed': False, 'sent': False, 'claimed': False, 'token': None,
            'routing_required': True, 'reason': 'routing_required',
            'pool_key': requested_pk, 'task_id': task_id, 'reasons': reasons,
            'selected': {'pool_key': target_pk, 'runtime': tr, 'model': tm},
            'domestic_full': domestic_full}


# ------------------------------------------------------------------ claim 消费
def _validate_attempt_row(row, *, task_id=None, runtime=None, model=None, workspace=None,
                          prompt_sha256=None, stage=None, chat_id=None):
    """对已取出的 attempt 行做精确一致性校验（供只读 validate 与事务内 CAS 复用）。
    显式 claim token 必须与原 task/stage/chat/promptSHA/真实 workspace/runtime/model
    全部一致；任一漂移即拒。"""
    reasons = []
    if task_id is not None and row['task_id'] != task_id:
        reasons.append(f'task_id drift: claim={row["task_id"]!r} entry={task_id!r}')
    if runtime is not None and row['runtime'] != runtime:
        reasons.append(f'runtime drift: claim={row["runtime"]!r} entry={runtime!r}')
    if model is not None and row['model'] != model:
        reasons.append(f'model drift: claim={row["model"]!r} entry={model!r}')
    if stage is not None and row['stage'] != stage:
        reasons.append(f'stage drift: claim={row["stage"]!r} entry={stage!r}')
    if chat_id is not None and row['chat_id'] != chat_id:
        reasons.append(f'chat_id drift: claim={row["chat_id"]!r} entry={chat_id!r}')
    if workspace is not None:
        wn = os.path.normcase(os.path.realpath(str(workspace)))
        if row['workspace'] != wn:
            reasons.append(f'workspace drift: claim={row["workspace"]!r} entry={wn!r}')
    if prompt_sha256 is not None and row['prompt_sha256'] != prompt_sha256:
        reasons.append('prompt_sha256 drift: claim does not match the entry prompt bytes')
    if row['state'] not in ACTIVE_STATES:
        reasons.append(f'claim already terminal (state={row["state"]}); cannot consume')
    return {'ok': not reasons, 'reasons': reasons, 'drift': bool(reasons)}


def validate_claim(store_path, token, *, task_id=None, runtime=None, model=None,
                   workspace=None, prompt_sha256=None, stage=None, chat_id=None) -> dict:
    """校验 claim token 与 task/stage/chat/runtime/model/workspace/prompt 精确一致；
    任一漂移即拒。只读，不改状态。"""
    with closing(connect(store_path)) as conn:
        row = conn.execute('SELECT * FROM attempts WHERE token=?', (token,)).fetchone()
    if row is None:
        return {'ok': False, 'reasons': [f'no such claim token: {token}'], 'drift': True,
                'attempt': None}
    attempt = dict(row)
    check = _validate_attempt_row(row, task_id=task_id, runtime=runtime, model=model,
                                  workspace=workspace, prompt_sha256=prompt_sha256,
                                  stage=stage, chat_id=chat_id)
    return {'ok': check['ok'], 'reasons': check['reasons'], 'drift': check['drift'],
            'attempt': attempt}


def consume_for_entry(store_path=None, *, task_id, runtime, model, workspace,
                      prompt_sha256, stage=None, chat_id=None, claim_token=None,
                      wrapper_pid=None, wrapper_created=None, now=None,
                      executor='auto', quota_store=None, quota_routes=None,
                      probe_ticket=None, codebuddy_glm_allowed=False) -> dict:
    """入口在 Popen 前的唯一容量门：
    - 给了 --dispatch-claim → 同一 `BEGIN IMMEDIATE` 事务内先精确校验（含 stage/chat）
      再 CAS reserved→running；重复/并发消费只有一个 allowed（一槽绝不双 Popen）；
    - 没给 → select_and_claim 原子路由；非被选中组合 → routing_required（sent=false）。
    auto 路径同样在 Popen 前记录 wrapper PID 与真实创建身份（缺省为当前进程）。
    放行即已持有该池一个在途名额，返回 token 供 finish/bind_child 绑定生命周期。"""
    now = now or utcnow()
    if claim_token:
        with closing(connect(store_path)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            try:
                row = conn.execute('SELECT * FROM attempts WHERE token=?',
                                   (claim_token,)).fetchone()
                if row is None:
                    conn.execute('ROLLBACK')
                    return {'allowed': False, 'sent': False, 'claimed': False,
                            'token': None, 'routing_required': False,
                            'reason': 'claim_invalid', 'pool_key': pool_key(runtime, model),
                            'task_id': task_id,
                            'reasons': [f'no such claim token: {claim_token}'],
                            'drift': True}
                check = _validate_attempt_row(row, task_id=task_id, runtime=runtime,
                                               model=model, workspace=workspace,
                                               prompt_sha256=prompt_sha256,
                                               stage=stage, chat_id=chat_id)
                if not check['ok']:
                    conn.execute('ROLLBACK')
                    return {'allowed': False, 'sent': False, 'claimed': False,
                            'token': None, 'routing_required': False,
                            'reason': 'claim_invalid', 'pool_key': pool_key(runtime, model),
                            'task_id': task_id, 'reasons': check['reasons'],
                            'drift': check['drift']}
                # BW-LIMIT30-20261019-S3：GLM 与 DeepSeek 额度独立，claim token 消费不再
                # 要求当轮 DeepSeek 限额证据；旧 `codebuddy_glm_allowed` 参数仅兼容保留。
                # BW-LIMIT30-20261019-S3：ZCode/Claude 的最近真实结构化 429/402 距绑定
                # attempt 的 ended_at_utc 不足 1800 秒时，消费已预留 token 也一致拒绝：
                # 只把本 token 的 reserved 释放为 start_failed（绝不泄漏、绝不抢占别人的
                # 名额）；running/unknown 旧在途原样保留，只拒绝重复启动。
                if row['pool_key'] in QUOTA_LIMIT_WINDOW_POOLS \
                        and _quota_limit_window_active(conn, row['pool_key'], now):
                    cur = conn.execute(
                        "UPDATE attempts SET state='start_failed', "
                        "terminal='start_failed', ended_at_utc=? WHERE token=? "
                        "AND state='reserved'", (_iso(now), claim_token))
                    released = cur.rowcount == 1
                    if released:
                        conn.execute('COMMIT')
                    else:
                        conn.execute('ROLLBACK')
                    return {'allowed': False, 'sent': False, 'claimed': False,
                            'token': None, 'routing_required': False,
                            'reason': 'main_force_limited',
                            'pool_key': row['pool_key'], 'task_id': task_id,
                            'capacity_released': released,
                            'reasons': [f'{row["pool_key"]} has a real structured quota '
                                        'failure less than 1800 seconds old (measured from '
                                        'the bound attempt ended_at_utc, falling back to '
                                        'the marker recorded_at_utc); new dispatch is '
                                        'skipped inside that read-only window and the '
                                        'original failure evidence is not cleared. After '
                                        '1800 seconds the pool becomes selectable again '
                                        'at its existing priority'
                                        + (' and this attempt\'s own reserved placeholder '
                                           'was released to start_failed (real release, no '
                                           'capacity leak)' if released else
                                           '; the attempt is no longer reserved, so its '
                                           'original state and in-flight placeholder are '
                                           'preserved (no false release)')]}
                # BW-SIMPLE-PRIORITY-RETRY-S4 用户政策：消费已预留 token 前**不再**据历史
                # ZCode availability 或主力明确限额（main_force_limits）把本 reserved 释放为
                # start_failed 并拒绝启动——每轮从最高优先级真实尝试，真实失败才落诊断、成功
                # 仍 epoch/attempt CAS 结算，不假装 healthy、不清旧 hold。历史 availability /
                # 限额标记仅作诊断保留（qc.*、main_force_limits 表、status 快照不改）。时段门
                # （_max_window_blocks）与 unknown 在途/单写入/去重照常，见下方与既有守卫。
                # BW-MAX-WINDOW-20261010-S1：消费已预留 token 前，若本 attempt 落在两个
                # Qoder 内置 Max 而当前已不在北京时间主力时段（22:00 含至 08:00 不含）——
                # 即预留发生在窗口内、真正启动时已跨过 08:00——在同一事务内把本次 reserved
                # 释放为 start_failed（真实释放本任务自己那份占位，绝不泄漏、绝不抢占别人的
                # 名额），拒绝启动。running/unknown 旧在途绝不假释放，只拒绝重复启动。
                if _max_window_blocks(row['pool_key'], now):
                    cur = conn.execute(
                        "UPDATE attempts SET state='start_failed', "
                        "terminal='start_failed', ended_at_utc=? WHERE token=? "
                        "AND state='reserved'", (_iso(now), claim_token))
                    if cur.rowcount != 1:
                        # 本 token 已不在 reserved（running/unknown 旧在途）：CAS 命中 0 行，
                        # 绝不谎称释放；保留原状态与占位，只拒绝重复启动。
                        conn.execute('ROLLBACK')
                        return {'allowed': False, 'sent': False, 'claimed': False,
                                'token': None, 'routing_required': False,
                                'reason': 'main_force_window_closed',
                                'pool_key': row['pool_key'], 'task_id': task_id,
                                'capacity_released': False,
                                'reasons': ['this attempt reserved into a gated Qoder Max '
                                            f'pool {row["pool_key"]!r} during the main-force '
                                            'window, but '
                                            'the Beijing-time window (22:00 inclusive to '
                                            '08:00 exclusive) has closed before the start '
                                            'and this attempt is no longer reserved (state='
                                            + str(row['state']) + '); its original state and '
                                            'in-flight placeholder are preserved and the '
                                            'duplicate start is refused (no false release)']}
                    conn.execute('COMMIT')
                    return {'allowed': False, 'sent': False, 'claimed': False,
                            'token': None, 'routing_required': False,
                            'reason': 'main_force_window_closed',
                            'pool_key': row['pool_key'], 'task_id': task_id,
                            'capacity_released': True,
                            'reasons': [_window_closed_reason(row['pool_key']) +
                                        '; released this attempt\'s own reserved '
                                        'placeholder to start_failed (real release, never '
                                        'leaked, never preempting another task) and refused '
                                        'the start before Popen']}
                # Z4：同 task 原消费除外；别的 task 已在同真实 workspace 在途写入则拒。
                busy = _workspace_conflict(conn, workspace, own_token=claim_token)
                if busy is not None:
                    conn.execute('ROLLBACK')
                    return _workspace_busy_reject(task_id, pool_key(runtime, model),
                                                  busy)
                if wrapper_pid is None:
                    wrapper_pid = os.getpid()
                if wrapper_created is None:
                    wrapper_created = process_identity(int(wrapper_pid)).get('created')
                cur = conn.execute(
                    "UPDATE attempts SET state='running', started_at_utc=?, "
                    'wrapper_pid=COALESCE(?, wrapper_pid), '
                    'wrapper_created=COALESCE(?, wrapper_created) '
                    "WHERE token=? AND state='reserved'",
                    (_iso(now), int(wrapper_pid), wrapper_created, claim_token))
                if cur.rowcount != 1:
                    conn.execute('ROLLBACK')
                    return {'allowed': False, 'sent': False, 'claimed': False,
                            'token': None, 'routing_required': False,
                            'reason': 'consume_conflict',
                            'pool_key': row['pool_key'], 'task_id': task_id,
                            'reasons': ['this claim was already consumed (state != '
                                        'reserved) by a competing entry; only one '
                                        'consume may win — never double-Popen one slot']}
                conn.execute('COMMIT')
                return {'allowed': True, 'claimed': True, 'token': claim_token,
                        'pool_key': row['pool_key'], 'routing_required': False,
                        'reasons': [], 'task_id': task_id,
                        'selected': {'runtime': runtime, 'model': model}}
            except Exception:
                conn.execute('ROLLBACK')
                raise
    if wrapper_pid is None:
        wrapper_pid = os.getpid()
    return select_and_claim(store_path, task_id=task_id, runtime=runtime, model=model,
                            workspace=workspace, prompt_sha256=prompt_sha256,
                            stage=stage, chat_id=chat_id, now=now, origin='entry-auto',
                            wrapper_pid=wrapper_pid, wrapper_created=wrapper_created,
                            executor=executor, quota_store=quota_store,
                            quota_routes=quota_routes, probe_ticket=probe_ticket,
                            codebuddy_glm_allowed=codebuddy_glm_allowed)


def mark_running(store_path, token, *, wrapper_pid=None, wrapper_created=None, now=None):
    """CAS reserved→running（同 consume 语义的单入口版本）；已消费/已终态时 settled=False，
    绝不二次放行。"""
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if wrapper_pid is not None and wrapper_created is None:
                wrapper_created = process_identity(int(wrapper_pid)).get('created')
            cur = conn.execute(
                "UPDATE attempts SET state='running', started_at_utc=?, "
                'wrapper_pid=COALESCE(?, wrapper_pid), '
                'wrapper_created=COALESCE(?, wrapper_created) '
                "WHERE token=? AND state='reserved'",
                (_iso(now), wrapper_pid, wrapper_created, token))
            changed = cur.rowcount
            conn.execute('COMMIT')
            return {'settled': bool(changed), 'token': token,
                    'reason': None if changed else 'not_reserved'}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def bind_child(store_path, token, child_pid, *, child_created=None, wrapper_pid=None,
               wrapper_created=None, now=None) -> dict:
    """单次绑定当前 owner 的真实 child PID+创建身份：只在尚无 child 且 attempt 在途时
    绑定；已绑定（尤其还活着）绝不覆盖。Z2 修订：attempt 已记录 wrapper 身份时，只有
    当前真实调用者身份（wrapper_pid/created）与记录一致才允许绑定——别人不能把子进程
    挂到不属于自己的 attempt 上。"""
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if child_created is None:
                child_created = process_identity(int(child_pid)).get('created')
            row = conn.execute('SELECT child_pid, state, wrapper_pid, wrapper_created '
                               'FROM attempts WHERE token=?', (token,)).fetchone()
            if row is not None and row['wrapper_pid'] is not None:
                if wrapper_pid is None or int(row['wrapper_pid']) != int(wrapper_pid):
                    conn.execute('ROLLBACK')
                    return {'bound': False, 'token': token,
                            'reason': 'owner_mismatch',
                            'reasons': ['attempt has a recorded wrapper (owner) pid '
                                        f'{row["wrapper_pid"]}; only that real owner may '
                                        'bind a child to this attempt']}
                if (row['wrapper_created'] and wrapper_created is not None
                        and str(wrapper_created) != str(row['wrapper_created'])):
                    conn.execute('ROLLBACK')
                    return {'bound': False, 'token': token,
                            'reason': 'owner_mismatch',
                            'reasons': ['caller wrapper creation identity does not match '
                                        'the recorded owner (possible pid reuse)']}
            cur = conn.execute(
                'UPDATE attempts SET child_pid=?, child_created=? WHERE token=? '
                "AND child_pid IS NULL AND state IN (%s)"
                % ','.join('?' * len(ACTIVE_STATES)),
                (child_pid, child_created, token, *ACTIVE_STATES))
            changed = cur.rowcount
            conn.execute('COMMIT')
            if not changed:
                row = conn.execute('SELECT child_pid, child_created FROM attempts '
                                   'WHERE token=?', (token,)).fetchone()
                existing = (dict(row) if row is not None else None)
                return {'bound': False, 'token': token,
                        'existing_child': existing,
                        'reasons': ['child already bound (single-shot; never overwrite '
                                    'an existing live child) or attempt not in flight']}
            return {'bound': True, 'token': token, 'child_pid': child_pid,
                    'child_created': child_created}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def finish(store_path, token, *, terminal='finished', success=None, now=None,
           prober=None, wrapper_pid=None, wrapper_created=None) -> dict:
    """终态释放容量名额（带真实终态核验，绝不能凭一个 CLI 参数假造）：
    - 已绑定 child：只接受探针回读 child 已真实退出（dead）才释放；child 仍活 → 拒绝；
      child 存活未知 → 拒绝（先 reconcile/mark_unknown）；已启动未知的绝不假定
      start_failed 释放。死 PID 的 created=None 属正常，不额外要求创建时刻。
    - 从未绑定 child（Z2 修订）：reserved 且无 wrapper（未消费的合法取消/预留）可
      start_failed/cancelled 释放；已进入 Popen-bind 窗口（有 wrapper 或 state=unknown）
      的无 child attempt，只有当前真实 owner 身份（wrapper_pid/created 与记录一致，
      即 owner 亲证 Popen 真失败/主动取消）才可 start_failed/cancelled，unknown 无
      child 绝不能凭一个终态参数释放；finished 无 child 证据 → 拒绝。
    - 子进程真实结束即释放（后续报告解析失败也仍释放）；重复释放幂等，不影响新 attempt。
    prober/wrapper_* 参数仅供测试与真实 owner 注入；CLI 不提供任何伪造终态的 flag。"""
    now = now or utcnow()
    if terminal not in RELEASED_STATES:
        raise ValueError(f'terminal must be one of {RELEASED_STATES}')
    probe = prober or (lambda pid: process_identity(int(pid)))
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT * FROM attempts WHERE token=?', (token,)).fetchone()
            if row is None:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'token': token,
                        'terminal': terminal,
                        'reasons': [f'no such attempt token: {token}']}
            if row['state'] not in ACTIVE_STATES:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'token': token,
                        'terminal': row['terminal'], 'idempotent': True,
                        'reasons': [f'attempt already terminal '
                                    f'(state={row["state"]}, terminal={row["terminal"]}); '
                                    'repeat release changes nothing and never affects a '
                                    'new attempt']}
            if row['pool_key'] == LUNA_KEY:
                # 原生 Luna attempt 没有 OS child，国内 finish 的 child/owner 证据链
                # 对它无意义且可能被用来绕过回执要求；唯一终态出口是 settle-native。
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'token': token,
                        'terminal': terminal,
                        'reasons': ['native Luna attempts have no OS child; the only '
                                    'terminal exit is settle-native with a trusted-'
                                    'host native receipt (raw worker text is never a '
                                    'terminal)']}
            if row['child_pid'] is not None:
                ident = probe(int(row['child_pid'])) or {'state': 'unknown', 'created': None}
                if ident.get('state') == 'alive':
                    conn.execute('ROLLBACK')
                    return {'settled': False, 'released': False, 'token': token,
                            'terminal': terminal, 'reason': 'child_alive',
                            'reasons': [f'bound child pid {row["child_pid"]} is still '
                                        'alive; direct finish of a live child is refused '
                                        '— wait for real exit or reconcile']}
                if ident.get('state') != 'dead':
                    conn.execute("UPDATE attempts SET state='unknown', "
                                 "terminal='unknown' WHERE token=?", (token,))
                    conn.execute('COMMIT')
                    return {'settled': False, 'released': False, 'token': token,
                            'terminal': 'unknown', 'reason': 'child_state_unknown',
                            'reasons': ['child liveness could not be verified; held as '
                                        'unknown (never assumed start_failed)']}
            elif terminal == 'finished':
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'token': token,
                        'terminal': terminal, 'reason': 'no_child_terminal_unverified',
                        'reasons': ['terminal=finished requires a bound child verified '
                                    'exited; use start_failed/cancelled only when the '
                                    'process truly never started / was cancelled']}
            elif row['wrapper_pid'] is not None or row['state'] == 'unknown':
                # Z2：Popen-bind 窗口/unknown 的无 child attempt，只有当前真实 owner
                # （wrapper 身份一致，亲证 Popen 真失败或主动取消）才可释放。
                if (row['wrapper_pid'] is None or wrapper_pid is None
                        or int(wrapper_pid) != int(row['wrapper_pid'])):
                    conn.execute('ROLLBACK')
                    return {'settled': False, 'released': False, 'token': token,
                            'terminal': terminal, 'reason': 'owner_unverified',
                            'reasons': ['attempt is in a Popen-bind window (wrapper '
                                        f'pid {row["wrapper_pid"]}, state={row["state"]}) '
                                        'with no child; only the recorded real owner '
                                        'confirming its own Popen failure/cancel may '
                                        'release it — an unknown attempt is never '
                                        'released on a terminal argument alone']}
                if (row['wrapper_created'] and wrapper_created is not None
                        and str(wrapper_created) != str(row['wrapper_created'])):
                    conn.execute('ROLLBACK')
                    return {'settled': False, 'released': False, 'token': token,
                            'terminal': terminal, 'reason': 'owner_unverified',
                            'reasons': ['caller wrapper creation identity does not '
                                        'match the recorded owner (possible pid reuse)']}
            cur = conn.execute(
                'UPDATE attempts SET state=?, ended_at_utc=?, terminal=?, '
                'success=? WHERE token=? AND state IN (%s)'
                % ','.join('?' * len(ACTIVE_STATES)),
                (terminal, _iso(now), terminal,
                 (None if success is None else int(bool(success))), token, *ACTIVE_STATES))
            changed = cur.rowcount
            conn.execute('COMMIT')
            return {'settled': bool(changed), 'released': bool(changed),
                    'token': token, 'terminal': terminal}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def mark_unknown(store_path, token, *, now=None) -> dict:
    """子进程可能仍活/存活未知：只标 unknown，继续占容量，绝不释放、绝不被抢占。"""
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            cur = conn.execute(
                "UPDATE attempts SET state='unknown', terminal='unknown', ended_at_utc=? "
                "WHERE token=? AND state IN ('reserved','running')",
                (_iso(now), token))
            changed = cur.rowcount
            conn.execute('COMMIT')
            return {'settled': bool(changed), 'released': False, 'token': token,
                    'terminal': 'unknown', 'capacity_held': True}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def reconcile(store_path=None, *, now=None, prober=None, platform=None) -> dict:
    """按 PID 存活/创建身份对在途 attempt 对账（修订：先判已绑定 child 的终态，再看
    wrapper；Luna native attempt 无 PID，跳过）：
    - 有 child：child 死（真实 Windows 死 PID created=None 属正常，不要求创建时刻）
      → reconciled_exit 释放（无论 wrapper 死活）；child 活且出生匹配 → 保持（reserved
      提升为 running）；child 活但出生未知/漂移（PID 复用嫌疑）或存活未知 → unknown
      保守不抢占；
    - 无 child：wrapper 活 → 保持（可能处于 Popen/bind 窗口，reserved 提升 running）；
      wrapper 死/存活未知 → 缺真实“未启动”证据，置 unknown 保留，绝不凭租期放行。
    缺证据一律保守 unknown，绝不按占位时间新旧抢占。prober 仅供测试注入合成探针。"""
    now = now or utcnow()
    prober = prober or (lambda pid: process_identity(pid, platform))
    released, held, promoted = [], [], []
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            rows = conn.execute(
                'SELECT * FROM attempts WHERE state IN (%s)'
                % ','.join('?' * len(ACTIVE_STATES)), ACTIVE_STATES).fetchall()
            for row in rows:
                tok = row['token']
                if row['pool_key'] == LUNA_KEY:
                    continue
                if row['child_pid'] is not None:
                    cident = prober(int(row['child_pid'])) or {'state': 'unknown',
                                                               'created': None}
                    if cident.get('state') == 'dead':
                        # BW-AVAILABILITY-20261009-B5：child 死亡不足以自动释放。缺原记录
                        # wrapper 身份、wrapper 仍活/未知、或 PID 被复用（创建身份不符）都必须
                        # 保留占位——否则原 owner 尚在写终态/接续时被空窗放行同 workspace 新
                        # writer。只有原身份齐备且 child+wrapper 双方确认死亡、创建身份匹配才
                        # 自动 reconciled_exit；owner 显式 finish 仍走各自路径正常释放。
                        wpid = row['wrapper_pid']
                        recorded_created = row['wrapper_created'] if \
                            'wrapper_created' in row.keys() else None
                        if wpid is None or recorded_created is None:
                            conn.execute("UPDATE attempts SET state='unknown', "
                                         "terminal='unknown' WHERE token=?", (tok,))
                            held.append({'token': tok, 'why': 'child dead but no recorded '
                                       'wrapper identity/creation to confirm the owning '
                                       'call has finished; held conservatively'})
                            continue
                        wident = prober(int(wpid)) or {'state': 'unknown',
                                                        'created': None}
                        if wident.get('state') != 'dead':
                            conn.execute("UPDATE attempts SET state='unknown', "
                                         "terminal='unknown' WHERE token=?", (tok,))
                            held.append({'token': tok, 'why': 'child dead but wrapper '
                                       'alive/unknown (owner may still be finalizing); '
                                       'held conservatively'})
                            continue
                        if wident.get('created') and \
                                str(recorded_created) != str(wident.get('created')):
                            conn.execute("UPDATE attempts SET state='unknown', "
                                         "terminal='unknown' WHERE token=?", (tok,))
                            held.append({'token': tok, 'why': 'child dead but wrapper PID '
                                       'reused (creation identity mismatch); held '
                                       'conservatively'})
                            continue
                        conn.execute("UPDATE attempts SET state='reconciled_exit', "
                                     "terminal='reconciled_exit', ended_at_utc=? "
                                     "WHERE token=?", (_iso(now), tok))
                        released.append(tok)
                        continue
                    if cident.get('state') == 'alive' and _created_matches(
                            row['child_created'], cident.get('created')):
                        if row['state'] == 'reserved':
                            conn.execute("UPDATE attempts SET state='running' "
                                         "WHERE token=?", (tok,))
                            promoted.append(tok)
                        continue
                    conn.execute("UPDATE attempts SET state='unknown', terminal='unknown' "
                                 "WHERE token=?", (tok,))
                    held.append({'token': tok, 'why': 'child alive with unknown/drifting '
                                                      'creation identity (possible pid '
                                                      'reuse) or liveness unknown'})
                    continue
                wp = row['wrapper_pid']
                if wp is None:
                    # 从未消费的合法预留（state=reserved、无 wrapper、无 child）：这是等待
                    # consume 的 reserved→running CAS、或显式 cancel 的干净名额。reconcile
                    # 绝不把它翻成 unknown——否则会破坏 consume 的同事务 CAS 竞争，并令合法
                    # cancel 误报 owner_unverified 无法释放（不做 lease 抢占，保持 reserved，
                    # 仍占容量，由持有 token 的入口 consume 或 cancel 单赢家结算）。
                    if row['state'] == 'reserved':
                        continue
                    # Z3：无 wrapper 也无 child 的 running/unknown 行没有任何可核验身份，
                    # 一律明确 held_unknown，绝不保持 running 也不释放。
                    conn.execute("UPDATE attempts SET state='unknown', terminal='unknown' "
                                 "WHERE token=?", (tok,))
                    held.append({'token': tok, 'why': 'no wrapper pid recorded'})
                    continue
                wident = prober(int(wp)) or {'state': 'unknown', 'created': None}
                if wident.get('state') == 'alive':
                    if row['state'] == 'reserved':
                        conn.execute("UPDATE attempts SET state='running' WHERE token=?",
                                     (tok,))
                        promoted.append(tok)
                    continue
                conn.execute("UPDATE attempts SET state='unknown', terminal='unknown' "
                             "WHERE token=?", (tok,))
                held.append({'token': tok, 'why': 'wrapper dead or unknown with no bound '
                                                  'child; no real never-started evidence, '
                                                  'held conservatively'})
            conn.execute('COMMIT')
            return {'released': released, 'held': held, 'promoted': promoted}
        except Exception:
            conn.execute('ROLLBACK')
            raise


# ------------------------------------------------------------------ Luna 票据
_SCOPE_KEYS = ('task_id', 'stage', 'chat_id', 'workspace', 'prompt_sha256')
_LUNA_PREAUTH_KEY = 'luna_preauthorization'


def _normalize_luna_preauthorization(value):
    """校验 scope 里可选的**任务级 300 秒 Luna 预授权**（旧任务没有该键，行为不变）。
    返回 (ok, normalized_or_None, reasons)。预授权必须是对本任务原话的明确授权：
    authorized 必须为 true、seconds 必须恰为固定 300 秒、original_quote 与 scope_ref
    非空；可选 round_failed_candidates 只能列出本轮真实失败过的候选池（不伪造全满、
    不自动拆分/扩写用户原话）。"""
    if value is None:
        return True, None, []
    if not isinstance(value, dict):
        return False, None, [f'scope.{_LUNA_PREAUTH_KEY} must be a JSON object carrying '
                             'the task-level 300-second Luna authorization']
    reasons = []
    if value.get('authorized') is not True:
        reasons.append(f'scope.{_LUNA_PREAUTH_KEY}.authorized must be exactly true; a '
                       'preauthorization is only the user\'s real explicit authorization')
    seconds = value.get('seconds')
    if isinstance(seconds, bool) or seconds != LUNA_ASK_TIMEOUT_SECONDS:
        reasons.append(f'scope.{_LUNA_PREAUTH_KEY}.seconds must be exactly '
                       f'{LUNA_ASK_TIMEOUT_SECONDS}; a task may neither shorten nor '
                       'customize the fixed wait')
    quote = value.get('original_quote')
    if not isinstance(quote, str) or not quote.strip():
        reasons.append(f'scope.{_LUNA_PREAUTH_KEY}.original_quote must be the user\'s '
                       'non-empty original authorization wording; it is never inferred')
    ref = value.get('scope_ref')
    if not isinstance(ref, str) or not ref.strip():
        reasons.append(f'scope.{_LUNA_PREAUTH_KEY}.scope_ref must be a non-empty '
                       'reference to the original authorized scope/message')
    failed = value.get('round_failed_candidates')
    if failed is not None:
        if not isinstance(failed, list) or not all(
                isinstance(x, str) and x.strip() for x in failed):
            reasons.append(f'scope.{_LUNA_PREAUTH_KEY}.round_failed_candidates must be a '
                           'list of non-empty pool_key strings when present')
        else:
            failed = [x.strip() for x in failed]
    if reasons:
        return False, None, reasons
    normalized = {'authorized': True, 'seconds': LUNA_ASK_TIMEOUT_SECONDS,
                  'original_quote': quote.strip(), 'scope_ref': ref.strip()}
    if failed is not None:
        normalized['round_failed_candidates'] = failed
    return True, normalized, []


def _scope_luna_preauthorization(scope):
    """返回已校验的预授权 dict；缺失/非法返回 None（调用方必须 fail-closed）。"""
    ok, normalized, _ = _normalize_luna_preauthorization(
        (scope or {}).get(_LUNA_PREAUTH_KEY))
    return normalized if ok else None


def _normalize_scope(scope):
    """原 scope 绑定核验：接受 dict 或 JSON 字符串，必须含 task/stage/chat/workspace/
    promptSHA 五个键，task_id/workspace/prompt_sha256 非空；可选
    luna_preauthorization 必须是合法的任务级 300 秒明确授权。返回 (ok, dict, reasons)。
    旧 scope 不含预授权键时完全维持原行为。"""
    if scope is None:
        return False, None, ['scope is required: the ticket must bind the original '
                             'task/stage/chat/workspace/prompt_sha256 being escalated']
    if isinstance(scope, str):
        try:
            parsed = json.loads(scope)
        except ValueError:
            return False, None, ['scope must be a JSON object or dict, not a bare string']
    elif isinstance(scope, dict):
        parsed = dict(scope)
    else:
        return False, None, [f'unsupported scope type: {type(scope).__name__}']
    if not isinstance(parsed, dict):
        return False, None, ['scope must be a JSON object binding the original ask facts']
    missing = [k for k in _SCOPE_KEYS if k not in parsed]
    if missing:
        return False, None, [f'scope missing keys: {missing}']
    for k in ('task_id', 'workspace', 'prompt_sha256'):
        if not parsed.get(k):
            return False, None, [f'scope.{k} must be non-empty']
    pre_ok, _pre, pre_reasons = _normalize_luna_preauthorization(
        parsed.get(_LUNA_PREAUTH_KEY))
    if not pre_ok:
        return False, None, pre_reasons
    return True, parsed, []


def _scope_candidate_allowlist(scope):
    """scope 可选 allowed_candidates（BW-CONTINUE-20261019-S7）：**仅**收窄本任务的候选
    筛选（ask_record 降级核验、国内回收与 exhausted 判定），绝不改动全局路由。返回
    (ok, set_or_None)：缺字段 (True, None) 保持旧兼容；合法非空、全部是已知未 retired
    的国内 pool_key 返回 (True, set)；空列表、非字符串项、未知/retired/Luna 键一律
    (False, None)——诚实拒绝，绝不把空/未知授权当作耗尽。"""
    value = (scope or {}).get('allowed_candidates')
    if value is None:
        return True, None
    if not isinstance(value, list) or not value or not all(
            isinstance(x, str) and x.strip() for x in value):
        return False, None
    cand = {x.strip() for x in value}
    for k in cand:
        if k == LUNA_KEY or k in LEGACY_RETIRED_POOL_KEYS or k not in CAPACITY:
            return False, None
    return True, cand


def _task_authorized_candidates(scope, zcode_blocked):
    """统一的本任务候选筛选（ask_record 降级核验、claim_due 国内回收与最终 exhausted
    共用）：先按票据原 scope 的 executor/combo 硬约束取受信任候选，再与合法非空的
    scope.allowed_candidates 取交集（空/未知/冲突集合诚实拒绝为无候选，由调用方经
    _preauth_candidates_exhausted 的 fail-closed 核验拦截，绝不当作耗尽），最后剔除
    **本轮**真实失败过的候选——剔除只在携带合法任务级 luna_preauthorization 且给出
    round_failed_candidates 时生效；缺授权完全维持旧候选行为。该筛选只会收窄、绝不
    扩大用户授权，也绝不把未授权候选算进本任务。"""
    allowed = _scope_allowed_candidates(scope, zcode_blocked)
    if not allowed:
        return allowed
    ok_ac, allowlist = _scope_candidate_allowlist(scope)
    if not ok_ac:
        return []
    if allowlist is not None:
        allowed = [k for k in allowed if k in allowlist]
    preauth = _scope_luna_preauthorization(scope)
    if preauth is None:
        return allowed
    failed = set(preauth.get('round_failed_candidates') or [])
    if not failed:
        return allowed
    return [k for k in allowed if k not in failed]


def _preauth_candidates_exhausted(conn, scope, now, quota_store=None,
                                  quota_routes=None):
    """按**本任务原 scope** 判断本轮合格候选是否真的耗尽。只统计 scope 授权的候选；
    round_failed_candidates 里本轮真实失败的候选不再重试；时段门、真实限额窗口、自身
    容量都作为过滤原因。返回 (exhausted, free_candidate, details)；绝不伪造全满、
    绝不强制探测、绝不清除任何冷却。没有有效预授权时返回 (False, None, reasons)。"""
    preauth = _scope_luna_preauthorization(scope)
    if preauth is None:
        return False, None, ['no valid task-level luna_preauthorization in scope']
    ok_ac, allowlist = _scope_candidate_allowlist(scope)
    if not ok_ac:
        return False, None, ['scope.allowed_candidates is present but empty, unknown or '
                             'malformed; the task-level authorized candidate set cannot be '
                             'trusted — fail-closed, an invalid or empty authorization is '
                             'never counted as exhaustion']
    failed = set(preauth.get('round_failed_candidates') or [])
    _combo, combo_conflict = _scope_combo_from_scope(scope)
    if combo_conflict:
        return False, None, ['the original scope pins an ambiguous/unknown executor/model '
                             'combo; fail-closed — no Luna takeover is inferred']
    zcode_blocked = _zcode_availability_blocked(quota_store, now=now,
                                                routes_path=quota_routes)
    allowed = _scope_allowed_candidates(scope, zcode_blocked)
    if allowlist is not None:
        allowed = [k for k in allowed if k in allowlist]
    if not allowed:
        return False, None, ['the original scope authorizes no eligible domestic candidate '
                             '(executor/combo hard constraint intersected with '
                             'allowed_candidates); fail-closed rather than inventing '
                             'exhaustion']
    details = []
    for k in allowed:
        if k in failed:
            details.append(f'{k}: already failed this round (round_failed_candidates)')
            continue
        cap = capacity_for(k)
        if cap is None:
            details.append(f'{k}: not a bounded domestic candidate')
            continue
        if _max_window_blocks(k, now):
            details.append(f'{k}: time-window gate closed')
            continue
        if _quota_limit_window_active(conn, k, now):
            details.append(f'{k}: inside its real 1800s quota-failure window')
            continue
        if _has_own_capacity(conn, k):
            return False, k, [f'{k}: an authorized candidate still has a free slot']
        details.append(f'{k}: full')
    return True, None, details


def ask_record(store_path=None, *, task_id, scope=None, ask_message_id=None,
               now=None, deadline_seconds=LUNA_ASK_TIMEOUT_SECONDS,
               degradation_reason=None, degradation_detail=None,
               quota_store=None, quota_routes=None) -> dict:
    """记录一次真实的“问用户（外部 agent 或 Luna）”票据并开始 300 秒计时。
    修订后的硬校验：
    - ask_message_id 必须非空（真实询问回执标识；本函数绝不假装已经问过）；
    - scope 必须完整绑定原 task/stage/chat/workspace/promptSHA，且 scope.task_id 与
      task_id 一致；
    - 六个国内名额必须确实全满（未满时优先国内，不该升级询问）；**除非**给出可信的
      降级决策（degradation_reason ∈ quota/auth/capacity + degradation_detail）：ZCode
      持久不可用（quota）等导致“可用主力不足”时，允许在六未满时记录一次降级询问；
      缺预授权时 quota 降级必须带客观证据（持久 ZCode availability 阻断行），绝不凭空
      降级；携带合法任务级预授权时 quota/auth/capacity 统一改经**本任务候选耗尽客观
      核验**（不强制某个 ZCode 历史标记，其它模型真实 quota 同样可核验记录）；
      scope.allowed_candidates（可选、仅本任务）为合法非空集合时与 executor/combo 授权
      取交集，空/未知/冲突集合诚实拒绝，绝不把空授权当耗尽；
    - 等待固定 LUNA_ASK_TIMEOUT_SECONDS=300 秒：更短的 deadline_seconds 直接拒绝，
      更长也按固定 300 落库（任务不能任意缩短/自定义）。
    同一 task 只能有一个票据：任何已存在的票据（pending/claimed/replied/cancelled/
    settled）都拒绝重复记录——重复询问绝不重置原回复/计时/scope；重启不重置 deadline。
    降级票据（degradation_reason 非空）默认不由超时授权 Luna：claim-due 要求用户明确
    reply luna，或原单 scope 携带合法任务级 luna_preauthorization 且候选客观耗尽后
    固定 300 秒无回复才放行（超时本身不得替代明确授权）。
    now 参数仅供确定性函数测试注入；宿主生产路径（CLI）始终使用真实 UTC。"""
    now = now or utcnow()
    ok, parsed_scope, scope_reasons = _normalize_scope(scope)
    if degradation_reason is not None and degradation_reason not in ('quota', 'auth', 'capacity'):
        return {'recorded': False, 'task_id': task_id,
                'reasons': [f'degradation_reason must be one of quota/auth/capacity; '
                            f'got {degradation_reason!r}']}
    degraded = degradation_reason is not None
    if degraded and (not degradation_detail or not str(degradation_detail).strip()):
        return {'recorded': False, 'task_id': task_id,
                'reasons': ['degradation_detail is required for a degradation ask: the '
                            'independent objective reason (quota/auth/capacity) must be '
                            'recorded, never fabricated']}
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT * FROM luna_tickets WHERE task_id=?',
                               (task_id,)).fetchone()
            if row is not None:
                # 任何已存在的票据（含 replied/cancelled/settled/claimed）一律拒绝重复
                # 记录：重复询问绝不重置原回复、计时或 scope。
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id, 'state': row['state'],
                        'deadline_utc': row['deadline_utc'],
                        'reply_choice': row['reply_choice'],
                        'reasons': ['an ask ticket already exists for this task '
                                    f'(state={row["state"]!r}); a repeated ask never '
                                    'resets the original reply, timer, or scope']}
            reasons = []
            if not ask_message_id or not str(ask_message_id).strip():
                reasons.append('ask_message_id is required: a real ask receipt '
                               'identifier (message id) from the actual question')
            if not ok:
                reasons.extend(scope_reasons)
            elif parsed_scope['task_id'] != task_id:
                reasons.append(f"scope.task_id {parsed_scope['task_id']!r} does not "
                               f'match ticket task_id {task_id!r}')
            if deadline_seconds is not None and deadline_seconds < LUNA_ASK_TIMEOUT_SECONDS:
                reasons.append(f'deadline_seconds={deadline_seconds} is below the fixed '
                               f'{LUNA_ASK_TIMEOUT_SECONDS}s wait; a task may never '
                               'shorten the user-response window')
            if degraded:
                # 降级询问允许在八槽未满时记录，但 quota 降级必须带客观持久 ZCode 不可用证据。
                if ok:
                    ok_ac, _allowlist = _scope_candidate_allowlist(parsed_scope)
                    if not ok_ac:
                        # BW-CONTINUE-20261019-S7：空/未知/畸形 allowed_candidates 诚实拒绝，
                        # 绝不猜候选或冒充耗尽。
                        reasons.append('scope.allowed_candidates is present but empty, '
                                       'unknown or malformed: the authorized candidate set '
                                       'for this task cannot be verified, so the '
                                       'degradation ask is refused fail-closed instead of '
                                       'guessing candidates or exhaustion')
                if degradation_reason == 'quota':
                    if _scope_luna_preauthorization(parsed_scope) is not None:
                        # BW-CONTINUE-20261019-S7：携带合法任务级预授权时，quota 降级统一
                        # 走下方**客观候选耗尽核验**（round_failed_candidates + 真实容量/
                        # 限额窗口），不再强制某个 ZCode 历史阻断标记——其它模型的真实
                        # quota 本轮全失败同样可核验记录。不造余额/恢复，不清任何真实冷却。
                        pass
                    else:
                        # 缺预授权维持旧机制：必须带客观持久 ZCode 不可用证据。
                        zcode_blocked = _zcode_availability_blocked(
                            quota_store, now=now, routes_path=quota_routes)
                        if not zcode_blocked:
                            reasons.append('quota degradation requires objective evidence: '
                                           'the persistent ZCode availability state must '
                                           'actually block ZCode; a non-capacity timeout or '
                                           'an unverified hunch is never a quota degradation')
                elif degradation_reason == 'capacity':
                    # 缺陷 7：capacity 降级不能只凭一句字符串+detail 就宣称"所有受信任候选
                    # 都不可用"——必须客观核验受信任候选池是否真的排空。仍有受信任候选有空位
                    # 时优先用国内候选，绝不升级到问用户/待援。候选集合按票据 scope 的执行器/
                    # combo 硬约束与本轮失败集合统一收窄（_task_authorized_candidates）：
                    # scope 明确只要 Qoder 时，ZCode 空位不是该任务的受信任候选；本轮真实
                    # 失败过的候选空位也不反向阻塞有预授权的降级询问。缺预授权保持旧行为。
                    zcode_blocked = _zcode_availability_blocked(
                        quota_store, now=now, routes_path=quota_routes)
                    allowed_c = _task_authorized_candidates(parsed_scope, zcode_blocked)
                    free_candidate = None
                    for k in allowed_c:
                        # 每个池（含两个 CodeBuddy 独立槽）都按自身容量判断真实空位
                        # （GLM 默认不在候选内）。
                        if capacity_for(k) is not None and _has_own_capacity(conn, k):
                            free_candidate = k
                            break
                    if free_candidate is not None:
                        reasons.append(
                            f'capacity degradation refused: an eligible domestic candidate '
                            f'{free_candidate} still has a free slot; prioritize the real '
                            'capacity instead of escalating to an ask (a capacity claim '
                            'must not fabricate exhaustion of authorized candidates)')
                if ok and _scope_luna_preauthorization(parsed_scope) is not None \
                        and degradation_reason in ('quota', 'auth'):
                    # 有任务级预授权的降级询问必须核验**客观候选**，而不是 auth/quota
                    # 字符串即可进：合法预授权 + 本任务受信任候选（剔除本轮已失败者）里
                    # 仍有真实空位时先走国内，绝不落预授权询问票据；客观核验本身 fail-closed
                    # 失败（歧义 combo、未授权任何候选等）同样拒绝记录。缺预授权保持旧行为。
                    pa_exhausted, pa_free, pa_details = _preauth_candidates_exhausted(
                        conn, parsed_scope, now, quota_store=quota_store,
                        quota_routes=quota_routes)
                    if pa_free is not None:
                        reasons.append(
                            'preauthorized degradation ask refused: the authorized '
                            f'candidate {pa_free} still has a real free slot; prioritize '
                            'domestic capacity instead of recording a preauthorized ask '
                            '(an auth/quota detail string alone never admits the ticket)')
                    elif not pa_exhausted:
                        reasons.append(
                            'preauthorized degradation ask refused fail-closed: the '
                            'task-level objective candidate check must succeed for the '
                            'preauthorization to apply')
                        reasons.extend(pa_details)
            elif _domestic_active(conn) < DOMESTIC_TOTAL_CAPACITY:
                reasons.append('domestic slots are not full; prioritize domestic '
                               'capacity instead of escalating to an ask')
            if reasons:
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id, 'reasons': reasons}
            asked = _iso(now)
            deadline = _iso(now + timedelta(seconds=LUNA_ASK_TIMEOUT_SECONDS))
            scope_json = json.dumps(parsed_scope, ensure_ascii=False, sort_keys=True)
            # 纯 INSERT（已确认无同 task 票据）：绝不用 ON CONFLICT DO UPDATE 覆盖任何
            # 既有回复/计时/scope。
            conn.execute(
                'INSERT INTO luna_tickets(task_id, state, scope, ask_message_id, '
                'asked_at_utc, deadline_utc, agent_id, degradation_reason, '
                'degradation_detail, updated_at_utc) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (task_id, 'pending', scope_json, str(ask_message_id), asked, deadline,
                 None, degradation_reason, degradation_detail, asked))
            conn.execute('COMMIT')
            return {'recorded': True, 'task_id': task_id, 'state': 'pending',
                    'asked_at_utc': asked, 'deadline_utc': deadline,
                    'deadline_seconds': LUNA_ASK_TIMEOUT_SECONDS,
                    'degradation_reason': degradation_reason,
                    'degradation_detail': degradation_detail}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def reply(store_path=None, *, task_id, choice, note=None, now=None) -> dict:
    """记录用户回复选择（幂等：首条回复生效，之后重复 callback 不覆盖、不重置任何
    计时）。一旦回复，claim-due 的超时自动裁决停止；reply luna 是明确的已授权接续
    路径（claim-due 可立即裁决 Luna）；external_agent/cancel 停止自动 Luna。"""
    now = now or utcnow()
    if choice not in ('luna', 'external_agent', 'domestic', 'cancel'):
        raise ValueError("choice must be one of luna/external_agent/domestic/cancel")
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT * FROM luna_tickets WHERE task_id=?',
                               (task_id,)).fetchone()
            if row is None:
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id,
                        'reasons': ['no ask ticket exists for this task']}
            if row['state'] == 'claimed':
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id, 'state': 'claimed',
                        'launch_state': row['launch_state'],
                        'reasons': ['ticket already claimed; reply recorded too late' +
                                    (f' (launch_state={row["launch_state"]}); a claimed '
                                     'or launch_unknown ticket is never reopened by a late '
                                     'reply' if row['launch_state'] else '')]}
            if row['state'] == 'settled':
                # 缺陷 7：已原生结算(settled)的票据绝不能被迟到/重复 reply 重新打开。
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id, 'state': 'settled',
                        'reply_choice': row['reply_choice'],
                        'reasons': ['ticket already settled natively; a late or repeated '
                                    'reply never reopens a settled ticket']}
            if row['state'] == 'replied':
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id, 'state': 'replied',
                        'reply_choice': row['reply_choice'], 'idempotent': True,
                        'reasons': ['reply already recorded; repeat callbacks never '
                                    'overwrite the first reply or reset anything']}
            if row['state'] == 'cancelled':
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id, 'state': 'cancelled',
                        'reasons': ['ticket was already cancelled']}
            conn.execute('UPDATE luna_tickets SET state=?, reply_choice=?, reply_note=?, '
                         'replied_at_utc=?, updated_at_utc=? WHERE task_id=?',
                         ('replied', choice, note, _iso(now), _iso(now), task_id))
            conn.execute('COMMIT')
            return {'recorded': True, 'task_id': task_id, 'state': 'replied',
                    'choice': choice}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def claim_due(store_path=None, *, task_id, now=None, scope=None,
              quota_store=None, quota_routes=None,
              _preclaim=True) -> dict:
    """到期裁决（同一 `BEGIN IMMEDIATE` 事务内的竞争，修订后无丢槽竞态）：
    - 票据 pending 且已过 deadline 且未回复（或用户已明确 reply luna）时：
      * 国内已恢复空位 → 在同一事务内按真实主力策略原子预留一个国内 claim、
        取消同 task 待援票据，返回 mode=domestic_reclaim + 真实选中组合 + token
        （绝不“只 cancel 不预留”，槽不再丢失）；scope 必须与票据原 scope 一致；
      * 六仍满 → 创建一个 luna attempt（不同 task 无全局数量 cap）并把票据置
        claimed（launch_state=host_must_call_native）。实际原生工具由宿主调用，
        Python 绝不冒称已 spawn；启动后崩溃未写出 agentID → mark_launch_unknown，
        绝不自动重复启动。
    - 已 claimed / launch_unknown → 幂等拒绝（带 agent_id/launch_state），不重启；
    - 已回复（非 luna）→ 超时不是默认无限授权，拒绝；未到期 → not_due。
    - 降级票据唯一预授权例外：quota/auth/capacity 降级 + 原单 scope 内任务级
      luna_preauthorization 合法且本轮合格候选真实耗尽（含容量耗尽）时，固定 300 秒无回复
      按原单接续 Luna（复用原票据、单写入者守卫）；候选未耗尽、无/非法预授权仍一律拒绝，
      capacity 降级且无预授权时维持旧的"必须明确 reply luna"拒绝。
    - 统一的本轮候选筛选：票据原 scope 携带合法任务级预授权且给出 round_failed_candidates
      时，本轮真实失败过的候选**不会**被 domestic_reclaim 重新认领（否则 299 秒一腾出空位
      就把刚失败的组合再派一遍）；只有本任务受信任候选全部失败耗尽（失败型耗尽，不是
      "只是满"）才落入 Luna 裁决——299 秒仍是 not_due，300 秒到期才 Luna 单赢家；纯 exact
      combo 本轮失败后同样不再被旧 pinned 分支永久挡死。缺预授权时回收与 pinned 语义完全
      照旧；用户 reply domestic/external/cancel、单写入者/活 writer 互斥、限额/时段门均不变。"""
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if _preclaim:
                _preclaim_reconcile(conn, now)
            row = conn.execute('SELECT * FROM luna_tickets WHERE task_id=?',
                               (task_id,)).fetchone()
            if row is None:
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id,
                        'reasons': ['no ask ticket; Luna was never really requested']}
            if row['state'] == 'claimed' or row['launch_state'] == 'launch_unknown':
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                        'already_claimed': True,
                        'claimed_token': row['claimed_token'],
                        'launch_state': row['launch_state'],
                        'agent_id': row['agent_id'],
                        'reasons': ['ticket already claimed or launch_unknown; the '
                                    'native call is non-idempotent — no auto-repeat, '
                                    'no restart']}
            if row['state'] == 'cancelled':
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id, 'state': 'cancelled',
                        'reasons': ['ticket was cancelled (a domestic slot freed or the '
                                    'user chose otherwise)']}
            if row['state'] == 'settled':
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id, 'state': 'settled',
                        'reasons': ['ticket was already settled natively (terminal '
                                    'receipt recorded); it is never re-escalated or '
                                    're-dispatched']}
            if row['state'] == 'replied':
                if row['reply_choice'] not in ('luna', 'domestic'):
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': 'replied',
                            'reply_choice': row['reply_choice'],
                            'reasons': ['user replied '
                                        f'{row["reply_choice"]!r}; wait-timeout is not a '
                                        'default infinite authorization and the explicit '
                                        'reply stops auto-Luna']}
            # Z2：先做票据原 scope 解析与（若传入）一致性核验——两个分支（国内回收与
            # Luna）都必须与原 scope 完全一致，任一字段漂移即拒。
            # BW-AVAILABILITY-20261009-B2：损坏/缺失的票据 scope 一律 fail-closed 拒绝，
            # 绝不在无法绑定原始事实时悄悄继续（既不国内回收也不 Luna）。
            ticket_scope = row['scope']
            ok_ts, parsed_ticket_scope, ts_reasons = _normalize_scope(ticket_scope)
            if not ok_ts:
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                        'scope_corrupt': True,
                        'reasons': ['the ticket scope is corrupt or missing; refusing to '
                                    'adjudicate without the original binding facts '
                                    f'(fail-closed): {ts_reasons}']}
            if scope is not None:
                ok, parsed, scope_reasons = _normalize_scope(scope)
                if not ok:
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                            'reasons': scope_reasons}
                if json.dumps(parsed, ensure_ascii=False, sort_keys=True) != ticket_scope:
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                            'reasons': ['scope does not match the original ticket scope '
                                        '(checked for both domestic reclaim and Luna)']}
            # Z2 修订顺序：真实票且未被 claimed/launch_unknown、用户未选外部/cancel 之后，
            # 先看国内是否已恢复空位（不必等到期：30 秒内释放也立即国内回收）；只有六
            # 仍满才判断 300 秒 deadline 或用户明确 reply luna。
            reply_luna = row['state'] == 'replied' and row['reply_choice'] == 'luna'
            reply_domestic = row['state'] == 'replied' and row['reply_choice'] == 'domestic'
            degraded = bool(row['degradation_reason']) if 'degradation_reason' in row.keys() else False
            # 统一本任务候选筛选的失败来源：票据原 scope 携带合法任务级预授权时，
            # round_failed_candidates 里的候选是**本轮真实失败过**的组合——国内回收、
            # ask_record 核验与最终 exhausted 共用同一剔除口径（只收窄、绝不扩大授权；
            # 缺预授权时为空集合，保持旧行为）。
            ticket_preauth = _scope_luna_preauthorization(parsed_ticket_scope)
            ticket_round_failed = set(
                (ticket_preauth or {}).get('round_failed_candidates') or [])
            zcode_blocked = _zcode_availability_blocked(quota_store, now=now,
                                                        routes_path=quota_routes)
            eligible = _eligible_main_forces(conn, zcode_blocked, now)
            # BW-AVAILABILITY-20261009-B4 缺陷 7 + B4 combo：国内回收必须真正遵守票据原
            # scope 携带的执行器 + 模型 combo 硬约束（受信任主脑的明确授权，含本次 Flash），
            # 绝不因为别的候选有空位就把 scope=qoder 的任务改派成 ZCode。scope 若声明
            # executor/model，则候选池被收窄到该 combo；combo 无空位就保持 pending 等待，
            # 绝不降级 Luna（domestic 任何时刻都不是 Luna）。
            scope_executor = parsed_ticket_scope.get('executor')
            scope_model = parsed_ticket_scope.get('model')
            scope_combo, scope_combo_conflict = _scope_combo_from_scope(parsed_ticket_scope)
            if not reply_luna and _domestic_active(conn) < DOMESTIC_TOTAL_CAPACITY:
                # BW-CONTINUE-20261019-S7：票据 scope 若携带 allowed_candidates，只收窄
                # **本任务**的回收候选（与 executor/combo 硬约束取交集），绝不改全局路由；
                # 缺字段保持旧行为；空/未知/畸形集合诚实拒绝（不猜池回收，也绝不当作耗尽）。
                ok_ac, scope_allowlist = _scope_candidate_allowlist(parsed_ticket_scope)
                if not ok_ac:
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                            'reasons': ['the ticket scope carries an empty/unknown/'
                                        'malformed allowed_candidates set; the authorized '
                                        'candidate set for this task cannot be trusted, so '
                                        'no domestic reclaim pool is guessed and an empty '
                                        'authorization is never counted as exhaustion — '
                                        'the ticket stays pending fail-closed']}
                allowed = None
                if scope_combo_conflict:
                    # executor/runtime 冲突或未知 model：fail-closed，无受信任候选，保持
                    # pending，绝不猜池回收、绝不 Luna。
                    allowed = set()
                elif scope_combo is not None:
                    allowed = {scope_combo}
                elif scope_executor == 'qoder':
                    allowed = {QMAX_POOL_KEY, QFLASH_POOL_KEY}
                elif scope_executor == 'zcode':
                    allowed = {ZCODE_POOL_KEY}
                elif scope_executor == CN_RUNTIME:
                    # CN-bound ticket reclaims only the CN Max (built-in + custom)/Flash pools,
                    # never international/ZCode, never Luna; retired DeepSeek-Flash is not a
                    # reclaim candidate either.
                    allowed = {CN_MAX_POOL_KEY, CN_CUSTOM_MAX_POOL_KEY, CN_FLASH_POOL_KEY}
                if scope_allowlist is not None:
                    # 与 executor/combo 收窄后的候选取交集；无执行器约束时也只允许
                    # 名单内候选参与本任务回收。
                    allowed = (set(scope_allowlist) if allowed is None
                               else allowed & scope_allowlist)
                if allowed is not None and ZCODE_POOL_KEY in allowed and zcode_blocked:
                    # 原 scope 只要 ZCode，但 ZCode 当前不可用：不回收、不 Luna，保持 pending。
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                            'reasons': ['the original scope pins this task to ZCode but '
                                        'ZCode is currently unavailable per persistent '
                                        'availability; a domestic reclaim never reroutes a '
                                        'ZCode-bound scope to another executor and never '
                                        'escalates to Luna — it stays pending waiting for a '
                                        'restored ZCode slot']
                                    + (zcode_blocked.get('reasons') or [])}
                # 本轮已真实失败过的候选绝不作为 domestic_reclaim 重新认领（否则 299 秒
                # 一旦腾出空位就会把刚失败的组合再派一遍）；与 ask_record/exhausted 同一
                # 收窄口径。
                if ticket_round_failed:
                    if allowed is not None:
                        allowed = allowed - ticket_round_failed
                eligible2 = eligible if allowed is None else (eligible & allowed)
                if ticket_round_failed:
                    eligible2 = eligible2 - ticket_round_failed
                target, _ = _select_main_force(conn, scope_combo, now, eligible=eligible2)
                if target is None and allowed is not None:
                    for k in allowed:
                        if k not in MAIN_FORCE_KEYS and capacity_for(k) is not None \
                                and not _main_force_limit_active(conn, k) \
                                and not _max_window_blocks(k, now) \
                                and _count_active(conn, k) < capacity_for(k):
                            target = k
                            break
                if target is None and scope_combo is not None \
                        and scope_combo not in ticket_round_failed \
                        and (scope_allowlist is None or scope_combo in scope_allowlist) \
                        and scope_combo in MAIN_FORCE_KEYS \
                        and scope_combo != CODEBUDDY_GLM_POOL_KEY \
                        and not _main_force_limit_active(conn, scope_combo) \
                        and not _max_window_blocks(scope_combo, now) \
                        and capacity_for(scope_combo) is not None \
                        and _has_own_capacity(conn, scope_combo):
                    # 票据回收没有"当轮 DeepSeek 明确限额证据"的上文：GLM 永不通过这条
                    # 路径被自动回收/启用，保持 pending；CodeBuddy deepseek 按其自身
                    # 独立 1 槽判断空位。
                    target = scope_combo
                if target is None and allowed is None:
                    # 无显式 scope 的国内回收：主力已满后按同级两地区 Flash 兜底
                    # （qoder:Qwen3.8-Flash 与 qodercn:Qwen3.8-Flash），先命中真实在途少的
                    # 一个；两 Flash 都满才保持 pending，绝不改派 Luna 或 retired DeepSeek。
                    for k in sorted(OVERFLOW_KEYS,
                                    key=lambda pk: (_count_active(conn, pk), pk)):
                        if k in ticket_round_failed:
                            continue
                        cap = capacity_for(k)
                        if cap is not None and _count_active(conn, k) < cap:
                            target = k
                            break
                if target is None:
                    # combo/执行器约束收窄后没有可用国内空位（别的候选有空位也不算）：
                    # 保持 pending 等待满足原约束的槽，绝不为图省事改派或降级 Luna。
                    # 被明确限额钉住的具体 combo 也在此保持 pending（sent=false /
                    # model_unavailable 语义）：绝不偷偷换成别的模型，等用户明确额度
                    # 恢复/重置解除标记后才有该 combo 的空位。
                    # 唯一例外：合法任务级 Luna 预授权 + 本任务受信任候选**全部**在
                    # round_failed_candidates 里本轮真实失败过（失败型耗尽，不是"只是
                    # 满"）且客观核验通过时，不在此处返回，落入下方 Luna 裁决——299 秒
                    # 仍是 not_due，300 秒到期才允许按原单接续 Luna（单赢家）。
                    failure_exhausted = False
                    if (ticket_preauth is not None and ticket_round_failed
                            and not scope_combo_conflict
                            and not _task_authorized_candidates(parsed_ticket_scope,
                                                                 zcode_blocked)):
                        fx_exhausted, fx_free, _fx_details = (
                            _preauth_candidates_exhausted(
                                conn, parsed_ticket_scope, now,
                                quota_store=quota_store, quota_routes=quota_routes))
                        failure_exhausted = bool(fx_exhausted and fx_free is None)
                    if failure_exhausted:
                        pass
                    else:
                        scope_combo_limited = (scope_combo is not None
                                               and _main_force_limit_active(conn, scope_combo))
                        scope_combo_window_closed = (scope_combo is not None
                                                     and _max_window_blocks(scope_combo, now))
                        conn.execute('ROLLBACK')
                        return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                                'sent': False,
                                'model_unavailable': bool(scope_combo_limited
                                                           or scope_combo_window_closed),
                                'main_force_window_closed': bool(scope_combo_window_closed),
                                'reasons': ['no domestic slot satisfies the original scope '
                                            f'constraint (executor={scope_executor!r}, '
                                            f'combo={scope_combo!r}'
                                            + ('; the pinned combo is explicitly quota-limited '
                                               'so it is never silently swapped to another '
                                               'model (model_unavailable)'
                                               if scope_combo_limited else '')
                                            + ('; the pinned Qoder Max pool is only '
                                               'a main force during Beijing time 22:00 '
                                               '(inclusive) to 08:00 (exclusive) and is outside '
                                               'that window now, so it is never silently '
                                               'swapped to another model'
                                               if scope_combo_window_closed else '')
                                            + '); the ticket stays pending '
                                            '— a slot for another candidate never satisfies it '
                                            'and domestic is never Luna']}
                if target is not None:
                    dup = _active_task(conn, task_id)
                    if dup is not None:
                        conn.execute("UPDATE luna_tickets SET state='cancelled', "
                                     'updated_at_utc=? WHERE task_id=?',
                                     (_iso(now), task_id))
                        conn.execute('COMMIT')
                        return {'claimed': False, 'task_id': task_id, 'state': 'cancelled',
                                'reasons': [f'task {task_id!r} already has an in-flight '
                                            'domestic attempt; aid ticket cancelled']}
                    tr, tm = target.split(':', 1)
                    # Z4：国内回收同样过真实 workspace 单写入守卫（别的 task 在途则拒，
                    # 票据保持 pending 等下次裁决，绝不改派 Luna/Flash 绕过）。
                    busy_ws = _workspace_conflict(
                        conn, parsed_ticket_scope.get('workspace')
                        if parsed_ticket_scope else None, own_task_id=task_id)
                    if busy_ws is not None:
                        conn.execute('ROLLBACK')
                        return {'claimed': False, 'task_id': task_id,
                                'state': row['state'],
                                'reasons': [f'workspace {busy_ws["workspace"]!r} already '
                                            f'has an in-flight attempt from a different '
                                            f'task (token={busy_ws["token"]}, '
                                            f'task={busy_ws["task_id"]!r}); the domestic '
                                            'reclaim was refused and the ticket stays '
                                            'pending — never rerouted around the guard']}
                    # Z2：国内回收必须写回票据原 scope 的真实绑定字段（stage/chat/
                    # prompt_sha256/workspace），返回的 token 才能被原入口按原 scope 消费。
                    tok = _insert_claim(conn, task_id=task_id,
                                        stage=parsed_ticket_scope.get('stage')
                                        if parsed_ticket_scope else None,
                                        chat_id=parsed_ticket_scope.get('chat_id')
                                        if parsed_ticket_scope else None,
                                        prompt_sha256=parsed_ticket_scope.get('prompt_sha256')
                                        if parsed_ticket_scope else None,
                                        workspace=_norm_workspace(
                                            parsed_ticket_scope.get('workspace'))
                                        if parsed_ticket_scope else None,
                                        runtime=tr, model=tm, pk=target, token=None,
                                        now=now, origin='claim-due-domestic-reclaim')
                    if target in MAIN_FORCE_KEYS:
                        _bump_rotation(conn, target, now)
                    conn.execute("UPDATE luna_tickets SET state='cancelled', "
                                 "reply_note='domestic_slot_freed', updated_at_utc=? "
                                 "WHERE task_id=?", (_iso(now), task_id))
                    conn.execute('COMMIT')
                    return {'claimed': True, 'mode': 'domestic_reclaim', 'task_id': task_id,
                            'token': tok, 'pool_key': target,
                            'selected': {'pool_key': target, 'runtime': tr, 'model': tm},
                            'scope': parsed_ticket_scope, 'launch_state': None,
                            'reasons': ['a domestic slot is free again; atomically reserved '
                                        'for this task (bound to the original ticket scope) '
                                        'and cancelled the pending aid (no double-dispatch, '
                                        'no lost slot)']}
            # 六仍满：未到期（不重置 deadline）一律拒绝；到期未回复或明确 reply luna
            # 才允许 Luna。
            # BW-AVAILABILITY-20261009-B2：用户明确 reply domestic → 永不 Luna（只能等国内
            # 或原子回收；六仍满也绝不降级 Luna）。
            if reply_domestic:
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id, 'state': 'replied',
                        'reply_choice': 'domestic',
                        'reasons': ['the user explicitly replied domestic; a domestic '
                                    'reply never escalates to Luna — wait for a domestic '
                                    'slot or an atomic domestic reclaim, never Luna']}
            # BW-AVAILABILITY-20261009-B5 缺陷 6：票据原 scope 若把任务钉在某个国内 combo
            # （executor+model，如 qoder:Qwen3.8-Flash），六仍满时**只等该 combo 的国内空位**，
            # 绝不因超时自动升级到 Luna（Luna 是不同执行器，从不满足国内 combo 约束）。只有用户
            # 明确 reply luna（已授权接续）才放行；executor/runtime 冲突或未知 model 同样
            # fail-closed 保持 pending，绝不猜池、绝不 Luna。
            # 例外（失败型耗尽，不是"只是满"）：合法任务级 Luna 预授权 + 该钉住 combo 本身
            # 已在 round_failed_candidates 里本轮真实失败 + 客观候选耗尽核验通过时，纯 exact
            # combo 不再被旧 pinned 分支永久挡死——固定 300 秒无回复按原单接续 Luna（未到期
            # 仍是 not_due）。该例外不放宽任何其它门：reply domestic/external/cancel、冲突
            # combo、未知 model、单写入者/活 writer 互斥照旧；候选集合仍由原 scope 收窄，
            # 绝不因预授权换 executor 绕过。
            pinned_domestic = (scope_combo is not None and scope_combo != LUNA_KEY
                               and not reply_luna)
            if scope_combo_conflict or pinned_domestic:
                pinned_lifted = False
                if pinned_domestic and not scope_combo_conflict \
                        and scope_combo in ticket_round_failed:
                    p_exhausted, p_free, _p_details = _preauth_candidates_exhausted(
                        conn, parsed_ticket_scope, now, quota_store=quota_store,
                        quota_routes=quota_routes)
                    pinned_lifted = bool(p_exhausted and p_free is None)
                if not pinned_lifted:
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                            'reasons': ['the original ticket scope pins this task to a '
                                        'domestic '
                                        f'combo (executor={scope_executor!r}, '
                                        f'model={scope_model!r}'
                                        + (f', combo={scope_combo!r}'
                                           if scope_combo else '')
                                        + ('; the combo is ambiguous/unknown so nothing is '
                                           'guessed (fail-closed)'
                                           if scope_combo_conflict else '')
                                        + '); when all eight domestic slots are full it '
                                        'waits for that combo to free — a wait timeout '
                                        'never escalates a domestic-combo scope to Luna; '
                                        'only an explicit user reply luna, or a valid '
                                        'task-level preauthorization over a pinned combo '
                                        'that genuinely failed this round with the '
                                        'authorized candidates objectively exhausted, '
                                        'may proceed']}
            # 降级票据（<8 满时凭独立 quota/auth/capacity 证据记录）默认绝不由超时授权
            # Luna：超时不得替代明确授权。唯一例外（任务级预授权，BW-CONTINUE-20261019-S7
            # 起覆盖 quota/auth/capacity 三类）：降级 + 原单 scope 内合法
            # luna_preauthorization（authorized=true、固定 300 秒、含用户原话的任务级一次性
            # 授权）+ **本轮**合格候选真实耗尽时，固定 300 秒无回复可按原单接续 Luna——
            # 复用原票据与单写入者守卫，绝不新建票据/重置计时。用户授权覆盖全部合格候选
            # 耗尽（含容量），不再有 capacity 的无条件阻断；候选未耗尽（优先国内）、缺少/
            # 非法预授权一律 fail-closed 拒绝；capacity 降级无预授权时维持旧拒绝语义。
            if degraded and not reply_luna:
                if row['degradation_reason'] == 'capacity' and ticket_preauth is None:
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                            'degradation_reason': 'capacity',
                            'reasons': ['this is a capacity degradation ask recorded without '
                                        'eight-full domestic slots; a wait timeout is never '
                                        'an implicit authorization — the user must explicitly '
                                        'reply luna before Luna may be claimed']}
                exhausted, free_candidate, details = _preauth_candidates_exhausted(
                    conn, parsed_ticket_scope, now, quota_store=quota_store,
                    quota_routes=quota_routes)
                if free_candidate is not None:
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                            'degradation_reason': row['degradation_reason'],
                            'reasons': ['preauthorized Luna continuation refused: the '
                                        f'authorized domestic candidate {free_candidate} '
                                        'still has a free slot; prioritize the real domestic '
                                        'capacity instead of escalating to Luna (the ticket '
                                        'stays pending)']}
                if not exhausted:
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                            'degradation_reason': row['degradation_reason'],
                            'reasons': ['preauthorized Luna continuation refused '
                                        'fail-closed: the task-level exhaustion check must '
                                        'succeed for the preauthorization to apply']
                                        + list(details)}
            if row['state'] == 'pending':
                deadline = _parse(row['deadline_utc'])
                if deadline is not None and now < deadline:
                    conn.execute('ROLLBACK')
                    return {'claimed': False, 'task_id': task_id, 'state': 'pending',
                            'not_due': True, 'deadline_utc': row['deadline_utc'],
                            'reasons': ['all eight domestic slots are still full and the '
                                        'ask deadline has not elapsed yet']}
            dup = _active_task(conn, task_id)
            if dup is not None:
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id,
                        'reasons': [f'task {task_id!r} already has an in-flight attempt']}
            tok = uuid.uuid4().hex
            # Z4：Luna 分支同样过真实 workspace 单写入守卫（原生 Luna 写同一真实目录，
            # 与国内执行器互斥）。
            busy_ws = _workspace_conflict(
                conn, parsed_ticket_scope.get('workspace')
                if parsed_ticket_scope else None, own_task_id=task_id)
            if busy_ws is not None:
                conn.execute('ROLLBACK')
                return {'claimed': False, 'task_id': task_id, 'state': row['state'],
                        'reasons': [f'workspace {busy_ws["workspace"]!r} already has '
                                    f'an in-flight attempt from a different task '
                                    f'(token={busy_ws["token"]}, '
                                    f'task={busy_ws["task_id"]!r}); Luna was refused '
                                    'for the same one-writer-per-workspace rule']}
            # Z2：Luna attempt 同样写回票据原 scope，保持记录完整可追溯。
            conn.execute(
                'INSERT INTO attempts(token, task_id, stage, chat_id, prompt_sha256, '
                'workspace, runtime, model, pool_key, state, reserved_at_utc, origin) '
                "VALUES(?,?,?,?,?,?,?,?,?, 'running', ?, 'luna-claim-due')",
                (tok, task_id,
                 parsed_ticket_scope.get('stage') if parsed_ticket_scope else None,
                 parsed_ticket_scope.get('chat_id') if parsed_ticket_scope else None,
                 parsed_ticket_scope.get('prompt_sha256') if parsed_ticket_scope else None,
                 _norm_workspace(parsed_ticket_scope.get('workspace'))
                 if parsed_ticket_scope else None,
                 LUNA_RUNTIME, LUNA_MODEL, LUNA_KEY, _iso(now)))
            conn.execute("UPDATE luna_tickets SET state='claimed', claimed_token=?, "
                         "launch_state='host_must_call_native', updated_at_utc=? "
                         "WHERE task_id=?", (tok, _iso(now), task_id))
            conn.execute('COMMIT')
            if (degraded or ticket_preauth is not None) and not reply_luna:
                luna_reasons = [
                    'luna_preauthorization (task-scoped) is valid for this '
                    f'{row["degradation_reason"] if degraded else "non-degraded"} ticket '
                    'and this round\'s authorized '
                    'candidates are genuinely exhausted; the fixed 300s ask deadline '
                    'elapsed with no reply, so Luna continues on the same ticket (single '
                    'writer per workspace, no new ask, no timer reset). If the native '
                    'tool started but crashed before writing an agentID, record '
                    'launch_unknown and do NOT auto-repeat the start.']
            else:
                luna_reasons = [
                    'all eight domestic slots full and the ask deadline elapsed '
                    'with no reply (or the user explicitly replied luna); '
                    'the host may now call native Luna. If the native tool '
                    'started but crashed before writing an agentID, record '
                    'launch_unknown and do NOT auto-repeat the start.']
            return {'claimed': True, 'mode': 'luna', 'task_id': task_id, 'token': tok,
                    'pool_key': LUNA_KEY, 'launch_state': 'host_must_call_native',
                    'scope': parsed_ticket_scope, 'reasons': luna_reasons}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def mark_launch_unknown(store_path=None, *, task_id, now=None) -> dict:
    """原生 Luna 调用非幂等窗口：工具已启动但在写出 agentID 前崩溃 → launch_unknown，
    待人工核验，绝不自动重复启动。只对已 claimed 票据生效，不改变 claimed 终态。"""
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT state, launch_state FROM luna_tickets '
                               'WHERE task_id=?', (task_id,)).fetchone()
            if row is None or row['state'] != 'claimed':
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id,
                        'reasons': ['launch_unknown only applies to a claimed ticket '
                                    '(native already started once)']}
            conn.execute("UPDATE luna_tickets SET launch_state='launch_unknown', "
                         'updated_at_utc=? WHERE task_id=?', (_iso(now), task_id))
            conn.execute('COMMIT')
            return {'recorded': True, 'task_id': task_id,
                    'launch_state': 'launch_unknown'}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def record_agent_id(store_path=None, *, task_id, agent_id, now=None) -> dict:
    """记录宿主真实原生调用后的 agentID（真实回执；本模块绝不自己生成或冒称）。"""
    now = now or utcnow()
    if not agent_id or not str(agent_id).strip():
        return {'recorded': False, 'task_id': task_id,
                'reasons': ['agent_id must be a non-empty real native-agent identifier']}
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT state FROM luna_tickets WHERE task_id=?',
                               (task_id,)).fetchone()
            if row is None or row['state'] != 'claimed':
                conn.execute('ROLLBACK')
                return {'recorded': False, 'task_id': task_id,
                        'reasons': ['agent_id can only be recorded on a claimed ticket']}
            conn.execute('UPDATE luna_tickets SET agent_id=?, updated_at_utc=? '
                         'WHERE task_id=?', (str(agent_id), _iso(now), task_id))
            conn.execute('COMMIT')
            return {'recorded': True, 'task_id': task_id, 'agent_id': str(agent_id)}
        except Exception:
            conn.execute('ROLLBACK')
            raise


NATIVE_TERMINAL_STATES = ('finished', 'native_failed')


_RECEIPT_SHA256 = re.compile(r'\A[0-9a-fA-F]{64}\Z')


def _receipt_content_bytes(content):
    """Z6-B 回执原文哈希口径：content 为 string 时按 UTF-8 原文字节哈希（绝不加
    JSON 引号）；对象按明确 canonical JSON（sort_keys + ensure_ascii=False +
    紧凑分隔符）哈希。"""
    if isinstance(content, str):
        return content.encode('utf-8')
    return json.dumps(content, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def _native_receipt_evidence(receipt):
    """校验宿主提供的原生终态回执，返回可追溯元信息 (evidence, reasons)。
    回执必须是 dict/JSON 对象，含：
    - source_tool：受信任宿主用哪个原生工具独立回读确认终态；
    - receipt_ref：原始原生回执的可检索引用（消息/运行标识等）；
    - sha256：原始回执哈希（Z6：严格 64 位 ASCII 十六进制，统一小写落库）；
      或 content：原始回执原文（本函数只计算并保存哈希，绝不解析/采信其内容
      本身）。两者同时给出必须核对一致；content 为 string 时按 UTF-8 原文字节
      哈希（不加 JSON 引号），对象按 canonical JSON。仅 terminal 字符串、缺
      source_tool/receipt_ref/哈希、非 64 位十六进制 sha 都视为没有真实宿主回执，
      拒绝。元信息只是受信任宿主边界内的可追溯证据，不是独立服务器验证。"""
    if receipt is None:
        return None, ['receipt is required: a bare terminal string is not a native '
                      'terminal proof; the trusted host must independently re-read the '
                      'native tool and pass the original receipt reference + hash']
    if isinstance(receipt, str):
        try:
            parsed = json.loads(receipt)
        except ValueError:
            return None, ['receipt must be a JSON object with source_tool/receipt_ref '
                          '(+ sha256 or content); a bare non-JSON string is not a '
                          'native receipt']
    elif isinstance(receipt, dict):
        parsed = dict(receipt)
    else:
        return None, [f'unsupported receipt type: {type(receipt).__name__}']
    if not isinstance(parsed, dict):
        return None, ['receipt must be a JSON object binding the native tool receipt']
    source_tool = parsed.get('source_tool')
    receipt_ref = parsed.get('receipt_ref')
    digest = parsed.get('sha256')
    content = parsed.get('content')
    if not source_tool or not str(source_tool).strip():
        return None, ['receipt.source_tool is required: which native tool the trusted '
                      'host re-read to independently confirm the terminal state']
    if not receipt_ref or not str(receipt_ref).strip():
        return None, ['receipt.receipt_ref is required: a retrievable reference to the '
                      'original native terminal receipt']
    # Z6-B：sha256 必须是严格 64 位 ASCII 十六进制（大小写规范：统一按小写落库）。
    if digest is not None:
        digest = str(digest)
        if not _RECEIPT_SHA256.match(digest):
            return None, ['receipt.sha256 must be exactly 64 ASCII hexadecimal '
                          f'characters (canonical lowercase on storage); got {digest!r}']
        digest = digest.lower()
    if content is None and digest is None:
        return None, ['receipt must carry sha256 of the original native receipt, '
                      'or the receipt content itself (its hash is computed and '
                      'stored; the raw text alone without any reference is never '
                      'accepted)']
    if content is not None:
        # Z6-B：content 与 sha256 同时给出必须核对一致；string 原文按 UTF-8 原字节
        # （不加 JSON 引号），对象按 canonical JSON 口径。
        computed = hashlib.sha256(_receipt_content_bytes(content)).hexdigest()
        if digest is None:
            digest = computed
        elif digest != computed:
            return None, ['receipt.sha256 does not match the sha256 of receipt.content '
                          '(string content is hashed as its raw UTF-8 bytes without '
                          'JSON quotes; object content uses canonical JSON); when both '
                          'are given they must agree']
    return {'source_tool': str(source_tool), 'receipt_ref': str(receipt_ref),
            'receipt_sha256': digest}, []


def settle_native(store_path=None, *, task_id, token, agent_id, terminal,
                  success=None, scope=None, receipt=None, now=None) -> dict:
    """原生 Luna 终态结算（最小受信任宿主原生回执入口；单事务）。

    信任边界：本机没有 Luna 的 OS 子进程，原生平台终态无法由本脚本独立探测。
    只有受信任宿主先用原生工具独立回读、确认 agent 真实终态之后，才可携带原始
    回执元信息调用本入口结算；原始 worker 文本/仅 terminal 字符串绝不构成终态。

    硬校验（任一不满足 → 拒绝且不释放，attempt/workspace 继续占用）：
    - 票据必须处于 claimed 且 claimed_token 与传入 token 一致（token 漂移拒绝）；
    - launch_state=launch_unknown → 拒绝（待人工核验，也绝不自动重派）；
    - 票据必须已登记真实 agent_id，且与传入 agent_id 一致（缺/错 agentID 拒绝）；
    - 传入 scope 若给出，必须与票据原 scope 完全一致（scope 漂移拒绝）；
    - receipt 必须是含 source_tool/receipt_ref + sha256(或 content) 的真实回执
      元信息（哈希与引用入库可追溯；仅 terminal 字符串拒绝）；
    - terminal 只接受明确终态 finished（成功）/native_failed（失败），运行中/
      unknown 等非终态拒绝；正常完成绝不伪标 cancelled/start_failed。

    单事务内：attempt 置终态（成功/失败分开，回执元信息存 adopt_evidence）、
    票据置 settled/launch_state=native_terminal_recorded，原 task/workspace 随
    attempt 退出 ACTIVE 而释放（Luna 本不占国内 6 个名额，国内活进程不受影响）。
    重复同终态结算幂等且不影响新 attempt；不加 force、没有任何全放行布尔。"""
    now = now or utcnow()
    if terminal not in NATIVE_TERMINAL_STATES:
        raise ValueError(f'terminal must be one of {NATIVE_TERMINAL_STATES}; running/'
                         'unknown are explicitly not native terminal states')
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT * FROM luna_tickets WHERE task_id=?',
                               (task_id,)).fetchone()
            if row is None:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'reasons': ['no such luna ticket; settle_native only applies '
                                    'to a real claimed Luna escalation']}
            attempt = None
            if token:
                attempt = conn.execute('SELECT * FROM attempts WHERE token=?',
                                       (token,)).fetchone()
            if attempt is None:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'reasons': [f'no attempt matches token {token!r}; settle_native '
                                    'must bind the exact claimed ticket token']}
            # Z6-C：token 必须绑定本票据（claimed 或已 settled 的幂等重放同此口径）。
            if row['claimed_token'] != token:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token, 'state': row['state'],
                        'reasons': ['ticket/token drift: the ticket is not bound '
                                    f'to this exact token (state={row["state"]}, '
                                    f'claimed_token={row["claimed_token"]!r}); refused '
                                    'and nothing released']}
            # Z6-C：国内 attempt 绝不能经 native 入口结算。
            if attempt['pool_key'] != LUNA_KEY:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token,
                        'reasons': ['settle_native only settles native Luna attempts; '
                                    f'a domestic attempt (pool={attempt["pool_key"]}) '
                                    'must leave through the domestic finish/reconcile '
                                    'path — the native entry never settles it']}
            # Z6-C：全部身份/回执校验先于幂等返回——错误 agent/scope/缺回执/不同终态
            # 的重复请求绝不能伪称幂等成功。
            if row['launch_state'] == 'launch_unknown':
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token, 'launch_state': 'launch_unknown',
                        'reasons': ['launch_unknown must stay protected: the native '
                                    'launch was never confirmed; it is never settled '
                                    'here and never auto-re-dispatched (manual '
                                    'verification required)']}
            if not row['agent_id']:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token,
                        'reasons': ['ticket has no recorded real agent_id; a native '
                                    'terminal can only be settled for a registered '
                                    'real agent']}
            if not agent_id or str(agent_id) != str(row['agent_id']):
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token,
                        'reasons': [f'agent_id mismatch: ticket holds '
                                    f'{row["agent_id"]!r}; the settle receipt must '
                                    'bind the same real native agent']}
            if scope is not None:
                ok, parsed, scope_reasons = _normalize_scope(scope)
                if not ok:
                    conn.execute('ROLLBACK')
                    return {'settled': False, 'released': False, 'task_id': task_id,
                            'token': token, 'reasons': scope_reasons}
                if json.dumps(parsed, ensure_ascii=False, sort_keys=True) != row['scope']:
                    conn.execute('ROLLBACK')
                    return {'settled': False, 'released': False, 'task_id': task_id,
                            'token': token,
                            'reasons': ['scope does not match the original ticket '
                                        'scope; drift is refused and nothing is '
                                        'released']}
            evidence, ev_reasons = _native_receipt_evidence(receipt)
            if evidence is None:
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token, 'reasons': ev_reasons}
            if attempt['state'] not in ACTIVE_STATES:
                # 票据已结算后的重复请求：只有同终态 + 同原回执哈希的合法幂等重放
                # 返回 settled=true（不覆盖旧证据、不碰任何新 attempt）；其余全拒。
                stored_digest = None
                if attempt['adopt_evidence']:
                    try:
                        stored_digest = json.loads(attempt['adopt_evidence']).get(
                            'native_terminal_receipt', {}).get('receipt_sha256')
                    except ValueError:
                        stored_digest = None
                same_terminal = attempt['terminal'] == terminal
                same_receipt = (stored_digest is not None
                                and stored_digest == evidence['receipt_sha256'])
                conn.execute('ROLLBACK')
                if same_terminal and same_receipt:
                    return {'settled': True, 'released': False, 'task_id': task_id,
                            'token': token, 'terminal': attempt['terminal'],
                            'idempotent': True, 'receipt_sha256': stored_digest,
                            'reasons': ['attempt already terminal '
                                        f'(state={attempt["state"]}, '
                                        f'terminal={attempt["terminal"]}) with the '
                                        'same original receipt; the legal repeat '
                                        'changes nothing, never overwrites the first '
                                        'evidence and never affects a new attempt']}
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token, 'terminal': attempt['terminal'],
                        'reasons': ['attempt already terminal '
                                    f'(state={attempt["state"]}, '
                                    f'terminal={attempt["terminal"]}); a repeat '
                                    'settle with a different terminal or a different '
                                    'receipt is refused and never overwrites the '
                                    'recorded evidence']}
            if row['state'] != 'claimed':
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token, 'state': row['state'],
                        'reasons': [f'ticket state is {row["state"]!r}, not claimed; '
                                    'refused and nothing released']}
            if success is None:
                success = (terminal == 'finished')
            elif bool(success) != (terminal == 'finished'):
                # Z6-D：terminal 与 success 不得矛盾——--success true 不能把
                # native_failed 记成成功，--success false 也不能把 finished 记失败。
                conn.execute('ROLLBACK')
                return {'settled': False, 'released': False, 'task_id': task_id,
                        'token': token, 'terminal': terminal,
                        'reasons': [f'success={bool(success)!r} contradicts '
                                    f'terminal={terminal!r}; finished means success '
                                    'and native_failed means failure — the flag is '
                                    'only accepted when it agrees with the terminal']}
            conn.execute(
                'UPDATE attempts SET state=?, ended_at_utc=?, terminal=?, success=?, '
                'adopt_evidence=? WHERE token=? AND state IN (%s)'
                % ','.join('?' * len(ACTIVE_STATES)),
                (terminal, _iso(now), terminal, int(bool(success)),
                 json.dumps({'native_terminal_receipt': evidence},
                            ensure_ascii=False, sort_keys=True),
                 token, *ACTIVE_STATES))
            conn.execute("UPDATE luna_tickets SET state='settled', "
                         "launch_state='native_terminal_recorded', updated_at_utc=? "
                         'WHERE task_id=?', (_iso(now), task_id))
            conn.execute('COMMIT')
            return {'settled': True, 'released': True, 'task_id': task_id,
                    'token': token, 'terminal': terminal,
                    'success': bool(success),
                    'receipt_sha256': evidence['receipt_sha256'],
                    'reasons': ['trusted host re-read the native tool and provided the '
                                'original terminal receipt (source_tool/receipt_ref/'
                                'sha256 recorded); attempt and ticket settled in one '
                                'transaction, original task/workspace released '
                                '(domestic slots were never held by Luna)']}
        except Exception:
            conn.execute('ROLLBACK')
            raise


def cancel_pending(store_path=None, *, task_id, now=None, reason='domestic_slot_freed') -> dict:
    """国内名额释放时，原子取消同 task 的 pending 救援票据（优先国内，不双派）。"""
    now = now or utcnow()
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            cur = conn.execute(
                "UPDATE luna_tickets SET state='cancelled', reply_note=?, updated_at_utc=? "
                "WHERE task_id=? AND state='pending'", (reason, _iso(now), task_id))
            changed = cur.rowcount
            conn.execute('COMMIT')
            return {'cancelled': bool(changed), 'task_id': task_id, 'reason': reason}
        except Exception:
            conn.execute('ROLLBACK')
            raise


# ------------------------------------------------------------------ adopt-legacy
# Z3：新旧版本上线交接。只接纳“受信任宿主”提供的既有 Qoder/Zcode 原请求与邻近
# process.json，绝不自动扫描用户目录；宿主负责确认该 PID 与该 receipt 的真实命令/
# 工作区对应（可信操作前提，见 handoff/revision3-report.md）。仅允许 GLM-5.3 与
# Qwen3.8-Max/Flash；CB/WB 等其它组合一律拒绝且不触摸其 quota/并发锁。
ADOPTABLE_POOL_KEYS = ('zcode:GLM-5.3', 'qoder:Qwen3.8-Max', 'qoder:Qwen3.8-Flash')


def _normalize_legacy_request(request):
    """Z4/Z5：把宿主已核对的原 request.json 归一为本池绑定字段，兼容真实新旧 schema：
    - 当前 schema：顶层 task_id/runtime/model；
    - Qoder 真实旧原件：无顶层 task_id/model；runtime 是对象 {node, qodercli}（不是
      字符串 'node'/'qodercli'），模型在 model_requested（池 runtime 统一为 qoder，
      model 取 model_requested）；
    - Zcode 真实旧原件：carrier='zcode-sdk' + selection.{providerId, modelId}。runtime
      必须归为 'zcode'（来自 carrier），providerId 是账号/供应商标识
      （account:bigmodel-individual-coding-plan），绝不能当作 runtime；model 取 modelId。
    task_id 可靠推导顺序：顶层 task_id → dispatch_plan.task_id → 原阶段 stage。
    只读解析，绝不改写任何旧原件；识别不了返回 (None, reasons)。"""
    if not isinstance(request, dict):
        return None, ['request must be a JSON object']
    if request.get('task_id') and request.get('runtime') and request.get('model'):
        return ({'task_id': str(request['task_id']),
                 'runtime': str(request['runtime']), 'model': str(request['model']),
                 'stage': request.get('stage'), 'chat_id': request.get('chat_id')}, [])
    selection = request.get('selection')
    if isinstance(selection, str):
        try:
            selection = json.loads(selection)
        except ValueError:
            selection = None
    if not isinstance(selection, dict):
        selection = {}
    plan = request.get('dispatch_plan')
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except ValueError:
            plan = None
    if not isinstance(plan, dict):
        plan = {}
    rt = request.get('runtime')
    # Qoder 真实旧原件 runtime 是 {node, qodercli} 对象；也兼容历史上的字符串形式。
    qoder_runtime = (
        (isinstance(rt, dict) and ('node' in rt or 'qodercli' in rt))
        or (isinstance(rt, str) and rt in ('node', 'qodercli'))
    )
    if request.get('carrier') == 'zcode-sdk' and selection.get('modelId'):
        # runtime 恒为 zcode（来自 carrier），providerId 只是账号/供应商标识，不是 runtime。
        runtime, model = 'zcode', str(selection['modelId'])
    elif request.get('model_requested') and qoder_runtime:
        runtime, model = 'qoder', str(request['model_requested'])
    else:
        return None, ['unrecognized request schema: expected current {task_id, runtime, '
                      'model}, legacy Qoder {model_requested, runtime {node,qodercli}} or '
                      "legacy Zcode {carrier='zcode-sdk', selection.modelId}"]
    task_id = request.get('task_id') or plan.get('task_id') or request.get('stage')
    if not task_id:
        return None, ['cannot derive task_id: need request task_id, dispatch_plan.'
                      'task_id or the original stage']
    return ({'task_id': str(task_id), 'runtime': runtime, 'model': model,
             'stage': request.get('stage'), 'chat_id': request.get('chat_id')}, [])


def _file_sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _adopt_reject(reason, task_id, pk, reasons):
    return {'adopted': False, 'token': None, 'task_id': task_id, 'pool_key': pk,
            'reason': reason, 'reasons': reasons}


def adopt_legacy(store_path=None, *, task_id, runtime, model, request_path,
                 process_path, prober=None, now=None, child_created=None) -> dict:
    """把一个仍活着的旧版 worker 如实计入本池（Z3 上线交接 + Z4 实质修正）：
    - 输入只认宿主提供的既有原 request.json 与邻近 process.json 文件路径，只读解析、
      绝不改写旧原件。request 兼容真实新旧 schema（见 _normalize_legacy_request）：
      当前顶层 task_id/runtime/model；Qoder 旧件 model_requested + runtime∈{node,
      qodercli}；Zcode 旧件 carrier='zcode-sdk' + selection.providerId/modelId。
      task 由顶层 → dispatch_plan.task_id → 原 stage 可靠推导；调用参数 task/
      runtime/model 必须与推导结果完全一致，原 prompt SHA/workspace/stage 照绑；
    - process.json 原件只有 {pid, state}（Z 或有 started_at_utc）、没有 created 属
      正常：此时允许受信任宿主传入其当前核验的 child_created 独立身份留证
      （created_source='host_vouched_child_created' 记入 adopt_evidence；宿主必须
      核验真实 argv/工作区与该 PID 对应）。生产仍用真实只读探针验证：PID 现在
      必须 alive 且观测创建身份与留证身份完全一致；已退出/unknown/漂移一律拒绝；
      无任何 fake-alive 开关（prober 仅供离线测试注入，CLI 不暴露）；
    - 仅 ADOPTABLE_POOL_KEYS（GLM-5.3 / Qwen3.8-Max / Flash）；其它组合（CB/WB 等）
      拒绝且不触摸其 quota/并发锁；
    - Z4-D：确认的实际 worker PID+birth 绑定到 child_pid/child_created（绝不冒充
      未启动 wrapper），旧 worker 真实退出后 reconcile 即 reconciled_exit 释放容量
      （结束即空名额）；
    - 源文件内容哈希留证（attempts.adopt_evidence，仅哈希，绝不落 argv/凭据）；
    - 幂等/防双计：同 PID+创建身份的 adopted attempt 已在途 → 同 task 幂等返回、
      不同 task 入参拒绝（pid_already_adopted，源 task 与原 request 精确绑定）；
      同 task 有其它在途 attempt → duplicate_task_in_flight 拒绝；
    - Z4-B：同真实 workspace 已有别的 task 在途写入 → workspace_in_flight 拒绝；
    - 超上限也如实计入（老 worker 真实占着名额），随后新派发会因容量检查被阻止；
      绝不为符合上限丢弃/杀掉老 worker，也不清任何旧 quota/工作区锁。"""
    now = now or utcnow()
    probe = prober or (lambda pid: process_identity(int(pid)))
    rp, pp = Path(request_path), Path(process_path)
    try:
        request = json.loads(rp.read_text(encoding='utf-8'))
        request_sha = _file_sha256(rp)
    except (OSError, ValueError) as exc:
        return _adopt_reject('request_unreadable', task_id, None,
                             [f'cannot read/parse the original request file: '
                              f'{type(exc).__name__}'])
    try:
        proc = json.loads(pp.read_text(encoding='utf-8'))
        process_sha = _file_sha256(pp)
    except (OSError, ValueError) as exc:
        return _adopt_reject('process_receipt_unreadable', task_id, None,
                             [f'cannot read/parse the adjacent process receipt: '
                              f'{type(exc).__name__}'])
    if not isinstance(request, dict) or not isinstance(proc, dict):
        return _adopt_reject('receipt_not_object', task_id, None,
                             ['request.json / process.json must be JSON objects'])
    pid = proc.get('pid')
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return _adopt_reject('process_identity_missing', task_id, None,
                             ['process.json must carry a real positive integer pid'])
    receipt_created = proc.get('created')
    if receipt_created and str(receipt_created).strip():
        recorded_created, created_source = str(receipt_created), 'process_receipt'
    elif child_created and str(child_created).strip():
        # Z4-C：真实旧 process.json 原件没有 created——受信任宿主传入当前核验的
        # child_created 独立身份留证；下面仍用真实探针做 alive+birth 完全一致验证。
        recorded_created = str(child_created)
        created_source = 'host_vouched_child_created'
    else:
        return _adopt_reject('process_identity_missing', task_id, None,
                             ['process.json has no creation identity (real legacy '
                              'receipts only carry pid/state); the trusted host must '
                              'pass its currently-verified child_created — a worker is '
                              'never adopted on a bare pid'])
    ident = probe(pid) or {'state': 'unknown', 'created': None}
    if ident.get('state') == 'dead':
        return _adopt_reject('legacy_process_exited', task_id, None,
                             [f'pid {pid} has already exited; an exited legacy worker '
                              'is not adopted (nothing was touched)'])
    if ident.get('state') != 'alive':
        return _adopt_reject('legacy_alive_unknown', task_id, None,
                             [f'pid {pid} liveness could not be verified '
                              f'(state={ident.get("state")!r}); pending verification, '
                              'refused — never adopted on an unverifiable pid'])
    observed = ident.get('created')
    if not observed or str(observed) != str(recorded_created):
        return _adopt_reject('legacy_identity_drift', task_id, None,
                             [f'observed creation identity for pid {pid} does not match '
                              f'the vouched identity ({created_source}); possible pid '
                              'reuse or forged receipt; refused'])
    normalized, norm_reasons = _normalize_legacy_request(request)
    if normalized is None:
        return _adopt_reject('request_binding_incomplete', task_id, None, norm_reasons)
    if not request.get('workspace') or not request.get('prompt_sha256'):
        return _adopt_reject('request_binding_incomplete', task_id, None,
                             ['original request is missing workspace/prompt_sha256; '
                              'the attempt must be bound to the verified original '
                              'request facts'])
    if normalized['task_id'] != str(task_id):
        return _adopt_reject('request_task_mismatch', task_id, None,
                             [f"derived request task_id {normalized['task_id']!r} != "
                              f'caller task_id {task_id!r}'])
    if normalized['runtime'] != str(runtime) or normalized['model'] != str(model):
        return _adopt_reject('request_combo_mismatch', task_id,
                             pool_key(runtime, model),
                             ['caller runtime/model must match the verified original '
                              'request exactly; the attempt is bound to the original '
                              'combination, not to what the caller claims'])
    pk = pool_key(normalized['runtime'], normalized['model'])
    if pk not in ADOPTABLE_POOL_KEYS:
        # CB/WB 等未授权组合：拒绝且不触摸其 quota/并发锁。
        return _adopt_reject('adopt_pool_forbidden', task_id, pk,
                             [f'{pk} is not adoptable (only '
                              f'{"/".join(ADOPTABLE_POOL_KEYS)}); nothing was touched '
                              '— no quota or concurrency locks of that combo were '
                              'modified'])
    workspace = _norm_workspace(request['workspace'])
    evidence = json.dumps({'request_sha256': request_sha,
                           'process_sha256': process_sha,
                           'vouched_pid': int(pid),
                           'created_source': created_source}, sort_keys=True)
    with closing(connect(store_path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            # Z4-D：同 PID+创建身份绝不双计——即使换 task 入参也拒绝，源 task 与原
            # request 的精确绑定优先。
            same_pid = conn.execute(
                "SELECT * FROM attempts WHERE origin='adopt-legacy' AND child_pid=? "
                'AND child_created=? AND state IN (%s) LIMIT 1'
                % ','.join('?' * len(ACTIVE_STATES)),
                (int(pid), str(recorded_created), *ACTIVE_STATES)).fetchone()
            if same_pid is not None:
                if same_pid['task_id'] == task_id:
                    conn.execute('ROLLBACK')
                    return {'adopted': True, 'idempotent': True,
                            'token': same_pid['token'], 'task_id': task_id,
                            'pool_key': same_pid['pool_key'],
                            'reason': 'already_adopted',
                            'reasons': ['the exact same pid+creation identity was '
                                        'already adopted for this task; not double '
                                        'counted']}
                conn.execute('ROLLBACK')
                return _adopt_reject(
                    'pid_already_adopted', task_id, pk,
                    [f'pid {pid} with this creation identity is already adopted by '
                     f'task {same_pid["task_id"]!r} (token={same_pid["token"]}); the '
                     'same live worker is never double counted even under different '
                     'task inputs — the source task and its original request stay '
                     'exactly bound'])
            dup = _active_task(conn, task_id)
            if dup is not None:
                conn.execute('ROLLBACK')
                return _adopt_reject('duplicate_task_in_flight', task_id, pk,
                                     [f'task {task_id!r} already has an in-flight '
                                      f'attempt (token={dup["token"]}, '
                                      f'state={dup["state"]})'])
            # Z4-B：同真实 workspace 已有别的 task 在途写入 → 拒绝（不做容量回收、
            # 不动业务占位）。
            busy = _workspace_conflict(conn, workspace, own_task_id=task_id)
            if busy is not None:
                conn.execute('ROLLBACK')
                return _adopt_reject('workspace_in_flight', task_id, pk,
                                     [f'workspace {busy["workspace"]!r} already has '
                                      f'an in-flight attempt from a different task '
                                      f'(token={busy["token"]}, '
                                      f'task={busy["task_id"]!r}); one real workspace '
                                      'never takes two concurrent writers'])
            tok = uuid.uuid4().hex
            # Z4-D：worker PID+birth 绑定 child_pid/child_created（真实执行器，结束即
            # reconciled_exit 释放），绝不放 wrapper_pid 冒充未启动 wrapper。
            conn.execute(
                'INSERT INTO attempts(token, task_id, stage, chat_id, prompt_sha256, '
                'workspace, runtime, model, pool_key, state, child_pid, child_created, '
                'reserved_at_utc, started_at_utc, origin, adopt_evidence) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (tok, task_id, normalized.get('stage'), normalized.get('chat_id'),
                 request.get('prompt_sha256'), workspace, normalized['runtime'],
                 normalized['model'], pk, 'running', int(pid), str(recorded_created),
                 _iso(now), _iso(now), 'adopt-legacy', evidence))
            if pk in MAIN_FORCE_KEYS:
                # 主力池如实计入持久轮转计数（1:1），老活 worker 不绕过也不被丢弃。
                _bump_rotation(conn, pk, now)
            conn.execute('COMMIT')
            return {'adopted': True, 'idempotent': False, 'token': tok,
                    'task_id': task_id, 'pool_key': pk, 'reason': None, 'reasons': [],
                    'evidence': {'request_sha256': request_sha,
                                 'process_sha256': process_sha,
                                 'created_source': created_source}}
        except Exception:
            conn.execute('ROLLBACK')
            raise


# ------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description='brain-worker cross-chat dispatch capacity pool')
    ap.add_argument('--store', default=None,
                    help='dispatch pool sqlite path (default: env '
                         'BRAIN_WORKER_DISPATCH_STORE or ~/.brain-worker/dispatch-pool.sqlite3)')
    sub = ap.add_subparsers(dest='command', required=True)

    sub.add_parser('status', help='show pool capacity/active/free and Luna tickets')

    p = sub.add_parser('reserve', help='reserve an exact (runtime, model) claim')
    _add_claim_args(p)

    p = sub.add_parser('select-and-claim', help='atomic 1:1 route selection + claim')
    _add_claim_args(p)

    p = sub.add_parser('validate', help='validate a claim token against entry facts')
    p.add_argument('--token', required=True)
    p.add_argument('--task-id', dest='task_id', default=None)
    p.add_argument('--runtime', default=None)
    p.add_argument('--model', default=None)
    p.add_argument('--workspace', default=None)
    p.add_argument('--prompt-sha256', dest='prompt_sha256', default=None)
    p.add_argument('--stage', default=None)
    p.add_argument('--chat-id', dest='chat_id', default=None)

    p = sub.add_parser('bind-child', help='record the real child pid + creation identity')
    p.add_argument('--token', required=True)
    p.add_argument('--child-pid', dest='child_pid', type=int, required=True)
    p.add_argument('--wrapper-pid', dest='wrapper_pid', type=int, default=None,
                   help='current real caller (owner) pid; required when the attempt '
                        'already records a wrapper identity')

    p = sub.add_parser('finish', help='terminal-release a claim (real terminal verified)')
    p.add_argument('--token', required=True)
    p.add_argument('--terminal', default='finished',
                   choices=list(RELEASED_STATES))
    p.add_argument('--success', default=None, choices=['true', 'false'])
    p.add_argument('--wrapper-pid', dest='wrapper_pid', type=int, default=None,
                   help='current real caller (owner) pid; required to release a '
                        'no-child Popen-bind-window/unknown attempt via '
                        'start_failed/cancelled')
    p.add_argument('--wrapper-created', dest='wrapper_created', default=None,
                   help='creation identity of the caller pid (pid-reuse binding)')

    sub.add_parser('reconcile', help='reconcile in-flight attempts by pid liveness/identity')

    p = sub.add_parser('ask-record', help='record a real ask-the-user ticket (fixed 300s)')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--scope', default=None,
                   help='JSON object binding task_id/stage/chat_id/workspace/prompt_sha256')
    p.add_argument('--ask-message-id', dest='ask_message_id', default=None)
    p.add_argument('--degradation-reason', dest='degradation_reason', default=None,
                   choices=['quota', 'auth', 'capacity'],
                   help='Independent degradation reason allowing an ask with fewer than '
                        'six-full domestic slots; quota requires objective persistent '
                        'ZCode unavailability evidence')
    p.add_argument('--degradation-detail', dest='degradation_detail', default=None,
                   help='Required objective detail for a degradation ask')
    p.add_argument('--quota-store', dest='quota_store', default=None,
                   help='Persistent availability store consulted for quota degradation evidence')
    p.add_argument('--quota-routes', dest='quota_routes', default=None)

    p = sub.add_parser('reply', help='record the user reply choice')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--choice', required=True,
                   choices=['luna', 'external_agent', 'domestic', 'cancel'])
    p.add_argument('--note', default=None)

    p = sub.add_parser('claim-due', help='atomic due adjudication: domestic reclaim or Luna')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--scope', default=None,
                   help='JSON object matching the original ticket scope (for reclaim)')
    p.add_argument('--quota-store', dest='quota_store', default=None,
                   help='Persistent availability store used to filter the domestic '
                        'reclaim target away from an unavailable ZCode')
    p.add_argument('--quota-routes', dest='quota_routes', default=None)

    p = sub.add_parser('mark-launch-unknown', help='record a non-idempotent launch_unknown')
    p.add_argument('--task-id', dest='task_id', required=True)

    p = sub.add_parser('record-agent-id', help='record the real native agent id')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--agent-id', dest='agent_id', required=True)

    p = sub.add_parser('settle-native',
                       help='settle a native Luna terminal (trusted-host native '
                            'receipt only; binds claimed ticket + agent_id + scope)')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--token', required=True,
                   help='the exact claimed_token returned by claim-due')
    p.add_argument('--agent-id', dest='agent_id', required=True,
                   help='the real agent_id recorded via record-agent-id')
    p.add_argument('--terminal', required=True,
                   choices=list(NATIVE_TERMINAL_STATES),
                   help='finished (success) / native_failed (failure); running/unknown '
                        'are refused — there is deliberately no force/allow-all flag')
    p.add_argument('--success', default=None, choices=['true', 'false'])
    p.add_argument('--scope', default=None,
                   help='JSON object matching the original ticket scope (drift refused)')
    p.add_argument('--receipt', default=None,
                   help='JSON object with source_tool + receipt_ref + sha256 (or '
                        'content) of the original native terminal receipt the trusted '
                        'host re-read; a bare terminal string is refused')

    p = sub.add_parser('cancel-pending', help='atomically cancel a pending aid ticket')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--reason', default='domestic_slot_freed')

    p = sub.add_parser(
        'adopt-legacy',
        help='adopt a live legacy worker vouched by the trusted host (verified '
             'original request.json + adjacent process.json; real liveness and '
             'creation identity are checked — there is deliberately NO fake-alive '
             'flag)')
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--runtime', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--request-json', dest='request_json', required=True,
                   help='path to the original request.json provided by the host')
    p.add_argument('--process-json', dest='process_json', required=True,
                   help='path to the adjacent process.json {pid, created} receipt')
    p.add_argument('--child-created', dest='child_created', default=None,
                   help='trusted-host vouched creation identity for the pid, only '
                        'used when the real legacy process.json original carries no '
                        'created field; liveness + identity are still verified with '
                        'the real probe (no fake-alive)')
    return ap


def _add_claim_args(p):
    p.add_argument('--task-id', dest='task_id', required=True)
    p.add_argument('--runtime', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--workspace', required=True)
    p.add_argument('--prompt-sha256', dest='prompt_sha256', required=True)
    p.add_argument('--stage', default=None)
    p.add_argument('--chat-id', dest='chat_id', default=None)
    p.add_argument('--token', default=None)
    p.add_argument('--executor', default='auto',
                   choices=['auto', 'qoder', 'zcode', CN_RUNTIME, CODEBUDDY_RUNTIME],
                   help='Explicit executor constraint from a trusted brain caller: '
                        'qoder honors a free Qwen3.8-Max slot without 1:1 rerouting to '
                        'ZCode; zcode is the authoritative last gate (unavailable → '
                        'refused); auto keeps historical committed-count fairness')
    p.add_argument('--quota-store', dest='quota_store', default=None,
                   help='Persistent ZCode availability store consulted for availability '
                        'filtering. When omitted it resolves through the formal default '
                        'source in order: explicit --quota-store -> env '
                        'BRAIN_WORKER_QUOTA_STORE -> the trusted default persistent file '
                        '(~/.brain-worker/quota-state.sqlite3), so the default path DOES '
                        'filter an unavailable ZCode. Resolution is READ-ONLY only: it '
                        'never creates a db/table and never writes the quota store inside '
                        'a pool transaction; a read error fails closed (treated as '
                        'unavailable), never claiming recovery/free/balance.')
    p.add_argument('--quota-routes', dest='quota_routes', default=None,
                   help='Optional quota routes file for ZCode availability resolution')


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    store = args.store
    now = utcnow()
    if args.command == 'status':
        out = status(store, now=now)
    elif args.command == 'reserve':
        out = reserve(store, task_id=args.task_id, runtime=args.runtime, model=args.model,
                      workspace=args.workspace, prompt_sha256=args.prompt_sha256,
                      stage=args.stage, chat_id=args.chat_id, token=args.token, now=now,
                      executor=args.executor, quota_store=args.quota_store,
                      quota_routes=args.quota_routes)
    elif args.command == 'select-and-claim':
        out = select_and_claim(store, task_id=args.task_id, runtime=args.runtime,
                               model=args.model, workspace=args.workspace,
                               prompt_sha256=args.prompt_sha256, stage=args.stage,
                               chat_id=args.chat_id, token=args.token, now=now,
                               executor=args.executor, quota_store=args.quota_store,
                               quota_routes=args.quota_routes)
    elif args.command == 'validate':
        out = validate_claim(store, args.token, task_id=args.task_id, runtime=args.runtime,
                             model=args.model, workspace=args.workspace,
                             prompt_sha256=args.prompt_sha256, stage=args.stage,
                             chat_id=args.chat_id)
    elif args.command == 'bind-child':
        out = bind_child(store, args.token, args.child_pid, wrapper_pid=args.wrapper_pid,
                         now=now)
    elif args.command == 'finish':
        success = None if args.success is None else (args.success == 'true')
        out = finish(store, args.token, terminal=args.terminal, success=success,
                     now=now, wrapper_pid=args.wrapper_pid,
                     wrapper_created=args.wrapper_created)
    elif args.command == 'reconcile':
        out = reconcile(store, now=now)
    elif args.command == 'ask-record':
        out = ask_record(store, task_id=args.task_id, scope=args.scope,
                         ask_message_id=args.ask_message_id, now=now,
                         degradation_reason=args.degradation_reason,
                         degradation_detail=args.degradation_detail,
                         quota_store=args.quota_store, quota_routes=args.quota_routes)
    elif args.command == 'reply':
        out = reply(store, task_id=args.task_id, choice=args.choice, note=args.note, now=now)
    elif args.command == 'claim-due':
        out = claim_due(store, task_id=args.task_id, now=now, scope=args.scope,
                        quota_store=args.quota_store, quota_routes=args.quota_routes)
    elif args.command == 'mark-launch-unknown':
        out = mark_launch_unknown(store, task_id=args.task_id, now=now)
    elif args.command == 'record-agent-id':
        out = record_agent_id(store, task_id=args.task_id, agent_id=args.agent_id, now=now)
    elif args.command == 'settle-native':
        success = None if args.success is None else (args.success == 'true')
        out = settle_native(store, task_id=args.task_id, token=args.token,
                            agent_id=args.agent_id, terminal=args.terminal,
                            success=success, scope=args.scope,
                            receipt=args.receipt, now=now)
    elif args.command == 'cancel-pending':
        out = cancel_pending(store, task_id=args.task_id, reason=args.reason, now=now)
    elif args.command == 'adopt-legacy':
        out = adopt_legacy(store, task_id=args.task_id, runtime=args.runtime,
                           model=args.model, request_path=args.request_json,
                           process_path=args.process_json, now=now,
                           child_created=args.child_created)
    else:  # pragma: no cover - argparse guards
        out = {'error': f'unknown command {args.command}'}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    if isinstance(out, dict) and (out.get('allowed') is False
                                  or out.get('ok') is False
                                  or out.get('claimed') is False
                                  or out.get('recorded') is False):
        # 拒绝/未 claim 也返回 0，让主脑读取 JSON 决策；仅内部错误抛异常。
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
