# Qoder CN 本机原生直连（qodercn）

> 运行时 `runtime='qodercn'`：本机已登录的 Qoder CN **原生 CLI**（EXE），与既有国际
> `qoder`（node + `qodercli.js` bundle）**并存、互不覆盖**。本轮落实最小可验收改动：
> 独立配置、友好名→真实模型 ID 映射、CN 六池容量（内置 Max=1 时段门同国际、自定义主力 `Qwen-3.8-Max`=2 不套时段仍复用限额、与国际 Flash 同级的 Flash=2；国内合计 10）、跨区域并行/同区互斥、跨 runtime
> claim 隔离、真实终态释放、Z 限额自动跳过（复用既有 availability）。**真实 CN 隔离小读/写
> 已由主脑独立验收**（见下节）；CN 后端权重/费用/真实模型 ID 身份仍未知，**本轮未做完整工程
> 验收，也未安装/上线**。

## 用户明确需求（锁定本轮范围，不得脱离）

- ①"我需要 Qoder 和 Qoder cn 同时能被使用。现在 Zcode 额度没有恢复前应该是自动跳过"。
- ②"Qoder CN 额外增加 2 个名额"。
- ③"不用读取 agent 的额度，只需要知道 agent 是不是限额了跳过就行"。
- ④"反思流程必须强化，不能脱离用户的明确需求"。

## 真实 CN 隔离小读/写验收（主脑独立）

主脑在隔离环境对 CN 原生入口完成了一次小规模真实 Read 与 Write 验收：两次调用均成功；证据
`request.json` 的 proof 字段与调用方 nonce 回读一致（未被改写）；验收报告绑定真实
runtime/model/阶段；调用结束真实终态释放 CN 名额。该验收只证明 **CN 原生入口在本机可真实完成
最小读写并正确记账/释放名额**，**不构成完整工程验收，也不代表已安装/上线**；CN 后端权重、计费
与真实模型 ID 身份仍未知。本机自定义 UUID/真实模型 ID 绝不写入公共源码，只保存在被 `.gitignore`
忽略的本地配置。

## 本机 CN 入口配置

复制 `scripts/local-entry-cn.json.example` 为同目录 `local-entry-cn.json`（已被 `.gitignore`
忽略，真实配置不入 Git），或经 `--cn-config <绝对路径>` 指定。字段：

```text
{
  "cli": "<qodercn 原生可执行文件绝对路径>",
  "config_dir": "<本机 CN 登录态所在配置目录绝对路径>",
  "model_ids": { "DeepSeek-Flash": "<服务端真实模型 ID>" }
}
```

- `cli` 必须为绝对且存在的文件；`config_dir` 必须为绝对且存在的目录；`model_ids` 必须为非空
  映射。示例仅占位，**绝不把本机 UUID/真实 ID 写死到公共源码**。
- 原生 `--model` 必须使用**真实模型 ID**：入口把友好名（池里的 `DeepSeek-Flash`）经 `model_ids`
  映射成真实 ID 再下发；**缺映射一律拒绝、零 Popen、零证据目录**，绝不把友好名当 ID、绝不自动
  选中同名内建/国际 Qwen 模型。

## 用法

```text
python <技能目录>/scripts/qoder_direct.py --runtime qodercn --cn-config <绝对 cn 配置> \
  --workspace <项目绝对路径> --prompt-file <UTF-8提示词文件> --output-dir <不存在的新证据目录> \
  --stage <非空阶段编号> --model DeepSeek-Flash [--config-dir <绝对目录>] [--tools ...] \
  [--allowed-tools RULE] [--disallowed-tools RULE] [--add-dir DIR] \
  [--task-id T] [--chat-id C] [--dispatch-store <db>] [--dispatch-claim <token>] \
  [--resume-session-id SID --resume-source <既往 CN request.json>]
```

不传 `--runtime` 时默认 `qoder`（国际路径**逐字不变**）。权限口径同国际：`--permission-mode`
固定 `dont_ask`，`--allowed-tools`/`--disallowed-tools`/`--add-dir` 可重复、原样单值传入、不按
逗号拆分；`request.json` 记录 `runtime_name='qodercn'`、`model_requested`（友好名）、`model_id`
（真实 ID）、`runtime={cli,config_dir}`、`argv` 真实数组与工具/规则。旧国际调用记录沿用原含义。

## 容量与并行（与 global-dispatch 一致）

- CN 池当前有效容量（BW-POOL-SPLIT-20261010-S2/S3 + BW-MAX-WINDOW-20261010-S2 六池定型）：主力 **`qodercn:Qwen3.8-Max=1`**（CN 内置 Max，独立池、时段门同国际 Max）、CN 自定义主力 **`qodercn:Qwen-3.8-Max=2`**（友好名带连字符 `Qwen-3.8-Max`，与内置 `Qwen3.8-Max` 不同；属 Qoder 主力组、复用既有 committed 1:1 轮换、**不套时段门、但复用 `main_force_limits` 限额判定、各自 pool_key 独立落标**），同级兜底 **`qodercn:Qwen3.8-Flash=2`**（与 `qoder:Qwen3.8-Flash` 同级）。`qodercn:DeepSeek-Flash` 新派发退休为 0（`LEGACY_RETIRED_POOL_KEYS`，`_domestic_policy` 返 `unknown_pool`），仅供已存在旧在途 attempt 真实终态释放。（历史：BW-QODER-CN-20261010-A2/A4 阶段曾把 `qodercn:DeepSeek-Flash` 当固定 2 槽的独立/AUTO 最后补充候选、国际 Max 仍 2、合计 6→8，已由 S2/S3 五池口径取代，BW-MAX-WINDOW-20261010-S2 再并入 CN 自定义主力升为六池总 10。）
- `AUTO` 在合格可用主力均不可用或已满时从两区 Flash **同级**选、可跨国际/CN 选合格候选；只有明确指定 `Qoder`/`ZCode`/`CN` 的 `executor`/`model` 约束才保持本地区、不互换、不扩大。默认入口仍通过准确
  runtime/model 选中的真正执行器提交，绝不伪装 Qwen 或改未授权模型。
- **国内合计 10**：`zcode:GLM-5.3=2` + 国际内置 Max=1 + CN 内置 Max=1 + CN 自定义主力=2 + 国际 Flash=2 + CN Flash=2。Luna 阈值随总 10 更新，
  仍沿用既有 300 秒真实提问/用户回复/`claim-due` 竞争裁决状态机，**不扩 Luna 授权**；回复
  `domestic` 任何时刻都绝不启动 Luna。
- CN 与国际可**同时经各自真实入口、在不同 workspace 并行**；同一真实 workspace 仍单写入（守卫与
  runtime 无关，CN 抢国际正占用的目录会被 `workspace_in_flight` 拒绝，反之亦然）。
- **跨 runtime 绝不消费旧 claim**：`consume_for_entry` 对 runtime 漂移即 `claim_invalid`——CN token
  不能被国际入口消费，国际 token 不能被 CN 入口消费；消费/claim 都落真实在途 attempt。
- **真实终态立即释放**：CN 子进程真实结束后 `finish(terminal='finished', success=rc==0)` 释放该 CN
  名额；启动失败/取消释放本 attempt；存活未知不释放、不被抢占（PID 复用绑创建时刻）。

## 接续边界（不新建会话系统）

CN 接续**至少拒绝把国际原件/session 当作 CN 续用**：无 `--resume-source`，或其指向的历史
`request.json` 不是 `runtime_name='qodercn'` 且 `resume_verifiable` 为真 → 一律拒绝续用（零 Popen、
零证据目录）。本轮**不建新的会话系统**，只保证不跨 runtime 冒用来源。

## CN 审批拒绝处理（2026-10-10，BW-MAX-WINDOW-20261010-S3）

- Edit/Write 按当次受控文件范围逐文件明确规则；Bash 只按登记的原样命令执行。
- 遇到审批拒绝立即停下并如实报告：不重复 Edit，不附加 `cd`/`ls`/`&&`/`echo` 或任何包装变体，
  不改 `dont_ask`，不放宽权限或换入口绕过拒绝。
- 规则范围明确**不等于**能保证 CN 审批匹配器放行；放行失败的确切原因未知时如实记为未知，不猜成
  环境故障或额度问题。
- 本次所处理的 CN 拒绝报告：3 次 Bash + 2 次 Edit 审批拒绝、人工终止码 `4294967295`、零修改、无
  有效回归；该 `4294967295` 是取消/人工终止码、**不是 HTTP 429 额度**，额度失败分型不成立。不声称
  本次已修 CLI 或已验证 CN 编辑能力；无需复制私人会话正文。本节为文档口径补充，不改代码/测试/池
  分配，也不改变上文的离线验收结论。

## ZCode 限额自动跳过（复用，不改额度系统）

本轮**禁止修改** `quota_control.py`/`zcode_direct.py`、查剩余额度/账单、加历史导入/冷却/恢复探针/
额度系统、发任何 ZCode 请求。跳过能力**直接复用**既有 `zcode_availability` 的 `hard_hold`
（主脑已写入并回读确认的真实 429/1310）：`dispatch_pool` 经只读 `_zcode_availability_blocked`
回核，Z 被阻断时 `AUTO` 自动跳过 Z（改道可用主力/溢出），显式 `executor='zcode'` 得
`zcode_unavailable`；**只判断"是否限额→跳过"，不读取真实额度**。测试用既有 `record_zcode_unavailability`
在临时库构造 hard_hold 验证跳过，真实剩余额度不在本轮范围。

## 离线验证

```text
python tests/test_qoder_cn_direct_offline.py    # 国际 argv 不变 / CN 映射 / 缺映射 0 Popen / CN 接续拒绝
python tests/test_dispatch_pool_qodercn.py      # CN 池/总 10、自定义主力不套时段、跨区域并行、同区互斥、跨 runtime 拒、终态释放、Z hold 跳过
```

两者用本地假 Popen、临时 SQLite，绝不读写真实/共享池或额度库，不证明真实后端能力。CN 隔离小
读/写真实验收已由主脑完成（见上节）；完整工程验收、安装版/仓库版与 GitHub/Obsidian 同步仍由主脑
独立执行，本文件不声称已完成。

## 六池真实分配与 CN 内置/自定义 Max 明确限额（2026-10-10，BW-POOL-SPLIT-20261010-S2/S3/S5 + BW-MAX-WINDOW-20261010-S2）

S2/S3 依用户明确指令重构国内池，BW-MAX-WINDOW-20261010-S2 加 CN 自定义主力后定型为**六池真实分配、总 10**：
- 主力：`zcode:GLM-5.3=2`、国际内置 `qoder:Qwen3.8-Max=1`、**CN 内置 `qodercn:Qwen3.8-Max=1`**、**CN 自定义 `qodercn:Qwen-3.8-Max=2`**（各池独立、可跨区域并行、跨 runtime 绝不互抢；自定义主力属 Qoder 主力组、复用既有 1:1 轮换、不套时段门）。
- 同级兜底 Flash：`qoder:Qwen3.8-Flash=2`、**`qodercn:Qwen3.8-Flash=2`**。两 Flash **同级**：`AUTO` 在合格可用主力均不可用或已满时从两区 Flash 选、可跨国际/CN 选合格候选；只有明确指定 `executor`/`model` 时才锁定本地区兜底（国际 Max→国际 Flash、CN Max→CN Flash），不跨区互换明确组合。
- **CN 内置模型真实名**：`Qwen3.8-Max` / `Qwen3.8-Flash`（内置池口径，**不是** `Qwen-3.8-*`）；CN 自定义主力用带连字符的友好名 `Qwen-3.8-Max` 以示与内置区分，仍经私有 `model_ids` 映射到真实模型 ID。私有 `local-entry-cn.json` 的 `cli / config_dir / model_ids` 映射不改写、不覆盖、不新增评分或调度器；示例仅占位、绝不写死本机 UUID/真实 ID 到公共源码。
- **DeepSeek-Flash 新派发退休为 0**：`LEGACY_RETIRED_POOL_KEYS = ('qodercn:DeepSeek-Flash',)`、`_domestic_policy` 返 `unknown_pool`；但既有在途 attempt 仍按真实 runtime/model 计入 `_domestic_active` 总数并做真实终态释放——不杀旧 Max/DeepSeek 进程、不因新池缩表误清活锁、不越总 10。

S5 给 **CN 内置 Qwen3.8-Max** 加上与国际 QMax 同规格的**明确限额**闭环（承载池 `qodercn:Qwen3.8-Max` ∈ `QUOTA_LIMITABLE_POOLS`）；BW-MAX-WINDOW-20261010-S2 把 CN 自定义主力 `qodercn:Qwen-3.8-Max` 并入同一 `QUOTA_LIMITABLE_POOLS` 承载范围，复用同一限额标记机制、各自 pool_key 独立落标、对两内置 Max 无连带：
- **识别只扫结构化载体**：`_explicit_quota_limit_hit(summary)` 只读 `summary['result_errors']` / `summary['result_errors_info']`；用户真实文案 **"You've reached your credit usage limit."** 必识别（`_QUOTA_HARD_LIMIT_MARKERS` 含 `credit usage limit`，大小写不敏感）。绝不扫报告正文/response.md；裸 `quota`、正文 429、permission 拒绝、认证/401、成功、取消 exit `4294967295` 都不当限额。
- **`_qoder_max_pool_key('qodercn', 'Qwen3.8-Max')` → `'qodercn:Qwen3.8-Max'`；`_qoder_max_pool_key('qodercn', 'Qwen-3.8-Max')` → `'qodercn:Qwen-3.8-Max'`**（BW-MAX-WINDOW-20261010-S2 自定义主力）；国际 `qoder` 请求自定义名 `Qwen-3.8-Max`、其他 CN 模型（Flash / DeepSeek / 其他自定义）一律 `None`，绝不误挡 Flash、绝不偷偷落标。
- **落标必绑真实 attempt + 证据**：`record_main_force_limit(store, 'qodercn:Qwen3.8-Max', task_id=…, attempt_token=…, evidence_path=str(out/'stdout.json'), evidence_sha256=sha256(stdout_bytes))` 事务内回核 attempt 行存在、pool/task 一致、`_file_sha256(evidence_path)` 与声明 sha256 匹配；任一漂移或缺字段 → `recorded=False / drift=True` 拒写。同池重复落标只刷新证据/时间戳、清空 `released_at/release_note`。
- **consume 前查限额**：`consume_for_entry` 在同一事务内若本 attempt pool 已 limited → 只把**本 token** CAS `reserved→start_failed`（`capacity_released=True`），`sent=False / reason='main_force_limited'`；不动其他 task 的 reserved，不泄漏本次占位。
- **落标 → 释放时序**：`main()` 先 `record_main_force_limit`、后 `_release_claim('finished', …)`；落标异常被 `except Exception` 兜住不阻断真实终态释放（不泄漏锁）。
- **恢复只人工**：`release_main_force_limit(store, pk, note=…)` 只在用户明确额度恢复/重置后调用；不查余额、不加定时器/探针/评分/新服务/外部 API、不改 `quota_control.py`/`zcode_direct.py`、绝不靠成功旧日志自动清。
- **AUTO/executor 路由保留**：`executor='qodercn'` 请求 CN Max 且未落标 → 直接 claim；已落标或已满 → 同 executor 内 CN Flash 兜底（`routing_required` 选 `qodercn:Qwen3.8-Flash`），绝不复活限额 Max、绝不跨地区换国际池。ZCode availability 跳过路径不变。Luna 侧不变：domestic 任何时刻都不启用 Luna、unknown 保占位、明确模型不偷偷换。
- **牛马主力时段门（2026-10-10 BW-MAX-WINDOW-20261010-S1）**：用户锁定——Qoder CN 内置 `Qwen3.8-Max` **只在北京时间 22:00（含）至次日 08:00（不含）作为主力**，与国际 Max 同门。这段时间以外：`AUTO` 新派工自动跳过 CN Max（走 ZCode 主力或同级 CN Flash）；主脑显式把新任务 `reserve` 到 CN Max → `dispatch_pool` 如实拒绝 `main_force_window_closed`，绝不偷偷换成 CN Flash。若 CN Max 曾在时段内预留、启动时已跨过 08:00，`consume_for_entry` 把该 reserved 真实释放为 `start_failed`（只释放本任务占位、不泄漏不抢占），已 `running`/`unknown` 旧在途绝不误释放。北京时间固定按 UTC+8 折算、与本地时区无关；时段门只套两内置 `Qwen3.8-Max`，国内合计已由 BW-MAX-WINDOW-20261010-S2 升为 10，CN 自定义主力 `qodercn:Qwen-3.8-Max`（2 槽、不套时段、复用限额判定）与 CN Flash/ZCode/自定义模型一样不受该时段门影响。详见 [global-dispatch](global-dispatch.md) §“牛马主力时段门”。

**离线回归**（每用例独立临时 SQLite，零网络）：`tests/test_dispatch_pool_qodercn.py`（六池容量含 CN 自定义主力不套时段/退休 DeepSeek `unknown_pool`/跨区域并行/跨 runtime 拒/终态释放/Z hold 跳过）、`tests/test_dispatch_pool.py::MainForceLimitTests`（record/read/release/read 往返 + drift 全拒 + `consume_for_entry` 只释放本次 reserved + 自定义 CN 主力限额无连带）、`tests/test_qoder_cn_direct_offline.py::ExplicitQuotaLimitHitTests`（真实文案命中 + 不误伤 + `_qoder_max_pool_key` 只对两内置 Max 与 CN 自定义主力返回非 None、其他一律 None）。Windows/Linux CI 矩阵都跑。
