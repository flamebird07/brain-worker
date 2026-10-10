# Qoder CLI 本机直连

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

遇到 Qoder 拒绝某动作时，执行 Agent 报告具体未执行的动作、被拒规则和证据，停止同类无效尝试；不重复询问已有授权，也不宣称修复成功——是否放行由主脑按隔离验证判断，不由执行 Agent 自报。

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

- **失败识别只扫结构化载体**：`_explicit_quota_limit_hit(summary)` 只读 `summary['result_errors']` / `summary['result_errors_info']`（真实 CLI 原始 envelope 的失败字段），绝不扫报告正文、`response.md` 或请求历史消息；`ec.explicit_429(errors, errors_info)` 命中或 `_hard_quota_limit_text` 命中任一 marker 才算真限额。识别大小写不敏感。
- **硬限额 markers**（`_QUOTA_HARD_LIMIT_MARKERS`）：`credits exhausted / out of credits / credit exhausted / credits depleted / insufficient credits / insufficient balance / no credits left / quota exhausted / credit usage limit / 积分用尽 / 积分不足 / 余额不足 / 额度用尽 / 额度不足`。用户真实失败文案 **"You've reached your credit usage limit."** 必识别。
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
