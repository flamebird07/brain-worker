# Qoder CN 本机原生直连（qodercn）

> **现行口径（BW-QODERCN-TOKEN-MAX-DISABLE-20261028，权威，覆盖本文件旧文）**：用户明确
> 要求 `Qoder CN / token plan / Qwen-3.8-Max`（精确池 `qodercn:Qwen-3.8-Max`，短横线友好名）
> **从所有档位禁用新派发**：`USER_DISABLED_POOL_KEYS={qodercn:Qwen-3.8-Max}`；AUTO、显式
> reserve/select-and-claim、claim-due、无 claim 的正式 CN 入口一律在创建占位、读取私有 CN
> 配置/模型映射、建输出目录、Popen 之前以 `mode_excluded`（用户要求禁用）拒绝；claim-due
> 的该拒绝先于 Luna 总门，不被 `luna_disabled` 遮蔽。物理容量登记仍 2、历史 attempt 保留；
> 禁用前合法取得且仍 reserved 的精确 claim 继续经完整
> claim/config/model/token/workspace/权限/容量/时段校验后启动（任何漂移照旧拒绝）；其它被
> 排除池的 consume 语义不变。绝不误伤内置名 `qodercn:Qwen3.8-Max`（无短横线，仍正常可派）。
> 本文件下文（含六池 10 槽分配、`Qwen-3.8-Max` 参与新派/时段门/限额复用等描述）均为**历史
> 覆盖前的记录**，涉及该池"当前可新派"的语句一律以此引文为准。

> 运行时 `runtime='qodercn'`：本机已登录的 Qoder CN **原生 CLI**（EXE），与既有国际
> `qoder`（node + `qodercli.js` bundle）**并存、互不覆盖**。本轮落实最小可验收改动：
> 独立配置、友好名→真实模型 ID 映射、CN 六池容量（内置 Max=1 时段门同国际、自定义主力 `Qwen-3.8-Max`=2 时段门同国际（BW-CUSTOM-MAX-NIGHT-20261010-S1 起纳入仅北京 22:00 含至 08:00 不含作主力）并复用限额、与国际 Flash 同级的 Flash=2；国内合计 10）、跨区域并行/同区互斥、跨 runtime
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

- CN 池当前有效容量（BW-POOL-SPLIT-20261010-S2/S3 + BW-MAX-WINDOW-20261010-S2 六池定型；**BW-QODERCN-TOKEN-MAX-DISABLE-20261028 起 `qodercn:Qwen-3.8-Max=2` 仅保留登记与遗留 claim 生命周期，不再可新派**）：主力 **`qodercn:Qwen3.8-Max=1`**（CN 内置 Max，独立池、时段门同国际 Max）、CN 自定义主力 **`qodercn:Qwen-3.8-Max=2`**（友好名带连字符 `Qwen-3.8-Max`，与内置 `Qwen3.8-Max` 不同；属 Qoder 主力组、复用既有 committed 1:1 轮换、**与两内置 Max 同套时段门（BW-CUSTOM-MAX-NIGHT-20261010-S1 起仅北京时间 22:00 含至次日 08:00 不含作为主力参与新派工）、并复用 `main_force_limits` 限额判定、各自 pool_key 独立落标**；**2026-10-28 起被用户明确禁用新派，见文件头现行口径**），同级兜底 **`qodercn:Qwen3.8-Flash=2`**（与 `qoder:Qwen3.8-Flash` 同级）。`qodercn:DeepSeek-Flash` 新派发退休为 0（`LEGACY_RETIRED_POOL_KEYS`，`_domestic_policy` 返 `unknown_pool`），仅供已存在旧在途 attempt 真实终态释放。（历史：BW-QODER-CN-20261010-A2/A4 阶段曾把 `qodercn:DeepSeek-Flash` 当固定 2 槽的独立/AUTO 最后补充候选、国际 Max 仍 2、合计 6→8，已由 S2/S3 五池口径取代，BW-MAX-WINDOW-20261010-S2 再并入 CN 自定义主力升为六池总 10。）
**BW-QWEN-MAX-LIMIT15-20261028（当前权威）**：两个 Qoder 内置 Max（`qoder:Qwen3.8-Max`、`qodercn:Qwen3.8-Max`）的明确限额标记改走 **900 秒（15 分钟）只读短窗**：从绑定 attempt 的真实 `ended_at_utc`（终态未落盘回退 `recorded_at_utc`）起 0<=age<900 秒内，AUTO、显式 reserve/select-and-claim、consume、claim-due 与浮窗同口径拒绝/显示“上轮限额”；恰满 900 秒自动消退并按现有优先级重新可选，不删 `main_force_limits` 证据、不宣称额度恢复、无需手动清标。原三池（ZCode/Claude/CodeBuddy deepseek）仍为 1800 秒；带短横线的 CN 自定义 `qodercn:Qwen-3.8-Max` 不进短窗（维持全档禁用现状），`qoder:Sonus` 不吃任何 Max 限额标记。离线回归见 `tests/test_qwen_max_limit15.py`。
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

## CN 逐文件新建/写入：路径限定规则必须用 `Edit(...)`（2026-10-12，BW-CN-WRITE-20261012T135751Z）

- **原始失败证据（保留、不改写）**：上一轮 CN 派工把 `--allowed-tools Write(/references/project-closeout.md)`
  原样下发给原生 CLI，`--permission-mode dont_ask` 下该新建动作被拒绝；**同一轮里指向另一个文件的
  `Edit(/SKILL.md)` 成功**。两条规则范围不同（Write 规则针对 `references/project-closeout.md`、Edit 规则
  针对 `SKILL.md`），**不是"同一文件范围"的对照**。当时 `build_cn_argv` 不做任何规则形状检查，规则一路
  原样到达 argv，拒绝发生在 CLI 侧，因此**入口未提前指出规则形状不兼容，CLI 原始拒绝已保留**在
  `summary` 的 `permission_denials` 与错误输出中。
- **官方口径**（https://docs.qoder.cn/cli/permissions ，由主脑核对，本阶段不联网）：path-scoped 的
  写入规则**只有** `Edit(...)` 这一种写法，它同时覆盖 `Edit`/`Write`/`NotebookEdit` 三个工具；不存在
  `Write(<路径>)` 形式的逐文件写入规则。**工具可见性与权限规则是两层**：`--tools` 里出现 `Write`
  只表示把 Write 工具暴露给模型，不代表任何授权范围；授权范围写在 `--allowed-tools`/`--disallowed-tools`。
- **本阶段最小修复（只动 CN 分支）**：`build_cn_argv` 在**构造 argv 之前**调用
  `validate_cn_permission_rules(perms['allowed_tools'], perms['disallowed_tools'])`，拒绝任何
  `Write(<范围>)` 形状（范围不是 `*`）的规则，**allow 与 deny 同检**——路径限定 deny 若被静默接受，
  调用方会误以为已经禁写。`main()` 只把这段 CN argv 构造包在 `except ValueError` 里，如实输出
  `cn_permission_rule_rejected=true / sent=false / exit_code=2 / runtime=qodercn` 并带上原规则数组，
  位置在**容量 claim、建证据目录、Popen 之前**：受控未派发、零 Popen、零证据目录、不消费任何 claim，
  也不再抛 traceback。国际 `build_argv` 一字未改。
- **原样保留、绝不动的部分**：工具级裸 `Write` 与 `Write(*)`（官方语义即工具级规则）、逐文件
  `Edit(...)`、`Read`、`Bash(<原样命令>)`、显式空 `--allowed-tools` 列表、`--tools` 可见性里的
  `Write`、`--add-dir`、`dont_ask` 权限模式、模型/配额/并发/认证与其它 runtime 入口。
- **绝不自动翻译或扩权**：入口只拒绝并提示"同一文件范围请改用 `Edit(...)`"，**不会**替调用方把
  `Write(/a/b.md)` 改写成 `Edit(/a/b.md)`，也不会降级成裸 `Write`/`Write(*)` 这类更宽的授权——范围
  变化必须由主脑重新显式点单。不新增通用规则框架、不做隐式字符串映射、不改 `dont_ask`。
- **逐文件新建的正确写法**：新建某一份已登记文件（含"文件当前不存在"的新建动作）在 CN 侧也传
  `Edit(<该文件路径>)`，需要模型真的用 Write 工具时再把 `Write` 放进 `--tools` 可见性；一份文件一条
  可重复规则、原样单值、不按逗号拆分。
- **离线反例（本地假 Popen、临时 SQLite，零网络）**：`tests/test_qoder_cn_direct_offline.py` 的
  `CnPathScopedWriteRuleTests` / `CnArgvRuleGateTests` / `CnMainWriteRuleRefusalTests` 证明：CN 非法
  `Write(...)` 规则 0 Popen、零证据目录、0 attempt；有效 `Edit(...)` + `Write` 可见性原样到达 argv；
  裸 `Write`/`Write(*)` 与国际 argv 不变。**这些只证明入口形状，不证明真实 CN CLI 已放行**；真实逐文件
  新建能力由主脑独立核验（回执/字节/目录哈希），离线桩不得当成真实权限成功。
- **本次独立验收事实（主脑已回读；本阶段不重跑 CN）**：全量回归 **859 项、0 失败、0 错误、1 项既有跳过**。
  本次**国际 Qoder**（请求 `Qwen3.8-Flash`）实际开发回执已返回、`report_bound=true`。主脑另行用候选仓库正式
  `scripts/qoder_direct.py --runtime qodercn` 做最小现场验收，请求 `Qwen3.8-Flash`：argv 只有 `Read` 与
  `Edit(/cn-created.txt)` 两条 allow 规则，`Write` 仅作为 `--tools` 工具可见性，`dont_ask` 权限模式未改；
  CN 侧真实 Write 一次、Read 一次、`permission_denials` 为零；产出文件 25 字节、内容精确为
  `BW_CN_WRITE_EXACT_FILE_OK`，SHA256 `a810c96edb2dc6b0ac9fccbced0c2ba63b75f9ab9d83e7f067ae01afc6aa9a92`；
  session `d0ef9006-805a-4fe1-957a-337d246db4d2`，正式入口退出码 0、`protocol_success=true`、
  `capacity_released=true`、`report_bound=false`（报告格式问题的原件保留，不为格式重跑）。CN 后端只回复
  `qfmodel` 身份、未独立确认，费用未知。**本条只证明这一次这一组逐文件 `Edit(...)` + `Write` 可见性的现场
  组合被放行，不断言所有官方版本的内部匹配器都存在同一故障或都会同样放行。**

## CN 裸 mutation 规则静态门与会话权限拒绝监视（2026-10-15，BW-CN-PERMISSION-STOP-20261015T155930Z-S1）

- **原始故障（保留、不改写）**：一次 CN 派工只带裸工具名 allow 规则（Read/Glob/Grep/Edit/Write/Bash，
  plan.grants 的 edits/bash 为空），`dont_ask` 下 Write 新建嵌套目标被拒；模型随后改用 Bash
  touch/ls/rm（实际临时写）与重定向（再次被拒）反复试写，主脑 1 分 55 秒后人工终止。根因**不能**
  简化成"所有 CN 版本 bare 规则必然无效"——官方当前文档说路径写入用 `Edit(...)`（同时覆盖
  Edit/Write/NotebookEdit）；既有 `build_cn_argv` 只拒绝 `Write(/path)` 形状，未防本次裸规则派工。
- **静态规则门（`validate_cn_allow_rule_shapes`，只动 CN）**：CN 正式 main 在容量 claim、占位、
  建输出目录、Popen **之前**校验有效 allow 规则：裸工具名或整工具通配 `Tool(*)` 的
  Edit/Write/NotebookEdit/Bash 一律拒绝（含 `--tools` 兼容回退生成的同名规则），错误信息明确要求
  `Edit(/<工作区相对具体文件>)` 与 `Bash(<登记原样命令>)`。绝不自动改写/扩大规则；Read/Glob/Grep
  裸规则、deny 规则、显式空 allow、`--tools` 可见性里的 Write（无写入权限的只读调用）一律不受影响；
  国际 `build_argv` 与 dont_ask 口径一字不变。**静态门只证明规则形状，不声称真实权限成功**——
  规则范围明确不等于 CN 审批匹配器必然放行（旧节既有口径保持）。
- **会话权限拒绝监视（`scripts/qoder_permission_watch.py`，纯标准库，只给 CN 增加）**：
  - 新调用生成并记录一个独立 UUID，经官方 `--session-id` 下发（主脑已核对 CN 当前官方 help 支持
    该参数；本阶段未联网复验）；`request.json` 的 `cn_session_watch` 记录 session_id、来源
    （generated/resume）、精确文件与基线。resume 沿用已验证原 session（`--resume`）。
  - 只在现场核实的 `config_dir/projects/<workspace-key>/<精确sessionId>.jsonl` 这**一个**文件上按
    精确 UUID 文件名定位（只读目录项，不扫描其它会话正文）并增量读取（基线偏移冻结；resume 旧事件
    绝不重扫触发），文件可能在 Popen 之后才出现时按 0 基线读；容忍分段 JSON 行，坏行只记事实不断
    监视，文件缩短（轮转/重写）记录事实并重新基线。原始会话文件保留不改。（旧版本文档写的
    `projects/<sessionId>.jsonl` 路径是错的，已按现场回放纠正。）
  - 仅接受 `sessionId` 精确一致、`cwd` 真实路径归一（含 Windows 8.3 短名）与本工作区一致、
    `type=user` 的真实 `message.content` tool_result；结构化 `toolDenialKind=permission-rule` 按
    **事件顶层**（现场真实事件即顶层标记，兼容 tool_result 块内/块 content 内同名字段）读取；或
    命中 tool_result **起始处**直接构成的固定拒绝文案——允许可选 `Error:` 前缀，含现场真实的
    `This action needs approval, and the "Don't ask" permission mode does not prompt.` 与
    codebuddy-direct 档案记录的同系 CLI 文案 `Permission to use … has been denied`——即停止本 child。
    固定文案只在结果起始处命中，**绝不扫描输出中间对同一句的引用、用户/助手叙述或 tool_result 之外
    的结构**；`is_error=false` 不掩盖真实拒绝；普通工具错误、402/429、取消文案都不是审批拒绝。
  - 停止只经**本次 Popen 对象** terminate → 等待真实退出（超时升级 kill），绝不按进程名杀进程、
    绝不碰别的 attempt/槽；communicate 仍由子线程**恰好一次**携 input 调用（正确送 stdin），
    超时/停止后绝不重发 input；主线程约 0.2 秒短轮询。终态无法确认仍 `mark_unknown` 保留占位。
  - 监视读错/降级只记录事实（`permission-stop.json` 的 `monitor_facts`），绝不悄悄当保护成功；
    **不宣称零延迟或零后续工具竞态**——拒绝发生后、终止生效前 child 仍可能执行有限动作。
  - 留证不只在发生拒绝时：**CN 所有已知终态都随 `summary.cn_watch_facts` 存本轮监视事实**（实际
    是否找到/观察到会话、精确位置、degraded、错误与读取进度）；无拒绝的正常结束也必须能回读监控
    状态，unknown 分支同样返回事实。**未找到文件时如实 `session_observed=false`，绝不写成已观察
    成功**；仅补留证，不新增定时器/DB/前置探针/全局健康门/新授权开关。
  - 若模型在监视器读取前已自然结束但会话流里有拒绝：如实分类为 `observed_after_exit`
    （不伪称已终止、不强行改写自然成功终态），事实仍入 `summary.permission_stop`。
- **终止语义**：监视器终止时 `protocol_success=false`、`report_bound=false`（即使部分 stdout 恰好
  可解析也不绑定）；`permission_stop`（kind/工具ID/来源/行号/观察时间/真实 exit）保留在 summary 与
  report-state，后续 stdout 解析失败只能追加 `parse_error`，**绝不把权限停止覆盖成 API/模型故障**；
  最小拒绝补入 `permission_denials` 供失败分型；**绝不落 quota 标记**；实际已退出才释放本 attempt；
  部分 stdout/stderr 原样保存，最小原始拒绝事件（匹配的 tool_result 块+身份头）单独落
  `permission-stop.json`，不整份拷贝私人会话流。
- **国际路径不受影响**：无 `--session-id`、无 `cn_session_watch`、无监视器，`communicate` 默认行为
  与旧 argv 一字不变（`tests/test_qoder_permission_stop.py::InternationalUnchangedTests` 回归）。
- **离线回归**：`tests/test_qoder_permission_stop.py`（tools-only/显式裸 mutation 0 Popen/0 claim/
  零证据目录；`Edit(/nested/created.txt)`+Write 可见性放行；国际不变；session/cwd 绑定、resume 旧
  事件不触发、分段行、is_error=false、普通错误/叙述不误停；固定文案起始锚定——真实 dont_ask 原文
  识别、输出中间引用同一句不误停；假 child 终止/自然退出/unknown 三态；无拒绝的正常结束可回读
  `cn_watch_facts`；全部会话文件按 `projects/<workspace-key>/<sid>.jsonl` 真实布局统一；
  本地无网络真实合成子进程写指定 native session + 延迟 sentinel，证明拒绝后终止、sentinel 未执行、
  普通错误继续）。`tests/test_qoder_cn_direct_offline.py` 同步更新（假 Popen 补 poll/wait/terminate/
  kill 表面；裸 Write 放行用例改为拒绝；正向用例 Write 仅留可见性）。**离线桩不证明真实 CN CLI
  行为**；`--session-id` 官方支持、真实拒绝文案全集与监视时延由主脑独立真实验收。本阶段（S3）
  修正的路径/字段层级/固定文案锚定已通过离线回归并由主脑原件回放，但**尚未最终现场验收**，
  本文件不宣称真实通过。

## ZCode 限额自动跳过（复用，不改额度系统）

本轮**禁止修改** `quota_control.py`/`zcode_direct.py`、查剩余额度/账单、加历史导入/冷却/恢复探针/
额度系统、发任何 ZCode 请求。跳过能力**直接复用**既有 `zcode_availability` 的 `hard_hold`
（主脑已写入并回读确认的真实 429/1310）：`dispatch_pool` 经只读 `_zcode_availability_blocked`
回核，Z 被阻断时 `AUTO` 自动跳过 Z（改道可用主力/溢出），显式 `executor='zcode'` 得
`zcode_unavailable`；**只判断"是否限额→跳过"，不读取真实额度**。测试用既有 `record_zcode_unavailability`
在临时库构造 hard_hold 验证跳过，真实剩余额度不在本轮范围。

## 离线验证

```text
python tests/test_qoder_cn_direct_offline.py    # 国际 argv 不变 / CN 映射 / 缺映射 0 Popen / CN 接续拒绝 / CN 路径限定 Write(...) 规则拒绝与 Edit(...) 原样放行 / 裸 mutation 规则拒绝
python tests/test_qoder_permission_stop.py      # CN 裸 mutation 门 0 Popen / --session-id 会话监视绑定 / 拒绝即停与 unknown 占位 / 真实合成子进程 sentinel 证明 / 国际不变
python tests/test_dispatch_pool_qodercn.py      # CN 池/总 10、自定义主力时段门同内置（用例经夜间 T0 claim）、跨区域并行、同区互斥、跨 runtime 拒、终态释放、Z hold 跳过
```

两者用本地假 Popen、临时 SQLite，绝不读写真实/共享池或额度库，不证明真实后端能力。CN 隔离小
读/写真实验收已由主脑完成（见上节）；完整工程验收、安装版/仓库版与 GitHub/Obsidian 同步仍由主脑
独立执行，本文件不声称已完成。

## 六池真实分配与 CN 内置/自定义 Max 明确限额（2026-10-10，BW-POOL-SPLIT-20261010-S2/S3/S5 + BW-MAX-WINDOW-20261010-S2）

S2/S3 依用户明确指令重构国内池，BW-MAX-WINDOW-20261010-S2 加 CN 自定义主力后定型为**六池真实分配、总 10**：
- 主力：`zcode:GLM-5.3=2`、国际内置 `qoder:Qwen3.8-Max=1`、**CN 内置 `qodercn:Qwen3.8-Max=1`**、**CN 自定义 `qodercn:Qwen-3.8-Max=2`**（各池独立、可跨区域并行、跨 runtime 绝不互抢；自定义主力属 Qoder 主力组、复用既有 1:1 轮换、与两内置 Max 同套主力时段门（BW-CUSTOM-MAX-NIGHT-20261010-S1：仅北京 22:00 含至 08:00 不含作主力）。
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
- **牛马主力时段门（2026-10-10 BW-MAX-WINDOW-20261010-S1）**：用户锁定——Qoder CN 内置 `Qwen3.8-Max` **只在北京时间 22:00（含）至次日 08:00（不含）作为主力**，与国际 Max 同门。这段时间以外：`AUTO` 新派工自动跳过 CN Max（走 ZCode 主力或同级 CN Flash）；主脑显式把新任务 `reserve` 到 CN Max → `dispatch_pool` 如实拒绝 `main_force_window_closed`，绝不偷偷换成 CN Flash。若 CN Max 曾在时段内预留、启动时已跨过 08:00，`consume_for_entry` 把该 reserved 真实释放为 `start_failed`（只释放本任务占位、不泄漏不抢占），已 `running`/`unknown` 旧在途绝不误释放。北京时间固定按 UTC+8 折算、与本地时区无关；**BW-CUSTOM-MAX-NIGHT-20261010-S1 起时段门扩为三个 Qoder Max**（`WINDOW_GATED_MAIN_FORCES = (qoder:Qwen3.8-Max, qodercn:Qwen3.8-Max, qodercn:Qwen-3.8-Max)`），CN 自定义主力 `qodercn:Qwen-3.8-Max`（2 槽）与两内置走同一 `_max_window_blocks` 门、同享夜间窗口，白天显式 `reserve`/`consume`/原生入口 `select_and_claim` 同样 `main_force_window_closed`，跨 08:00 的 `reserved` 真实释放而 `running`/`unknown` 不被抢占；国内合计仍由 BW-MAX-WINDOW-20261010-S2 升为 10（容量口径不变）；CN Flash、ZCode、退休 DeepSeek 与其他自定义模型不受该时段门影响。详见 [global-dispatch](global-dispatch.md) §“牛马主力时段门”。

**离线回归**（每用例独立临时 SQLite，零网络）：`tests/test_dispatch_pool_qodercn.py`（六池容量含 CN 自定义主力时段门同内置、用例经夜间 T0 claim/退休 DeepSeek `unknown_pool`/跨区域并行/跨 runtime 拒/终态释放/Z hold 跳过）、`tests/test_dispatch_pool.py::BeijingMainForceWindowTests`（三 Qoder Max 时段门：自定义 22:00 含/08:00 不含边界、白天显式与 AUTO 零启动、夜间两槽、跨 08 点本 reserved 真实释放、running 活任务不假释放）、`tests/test_dispatch_pool.py::MainForceLimitTests`（record/read/release/read 往返 + drift 全拒 + `consume_for_entry` 只释放本次 reserved + 自定义 CN 主力限额无连带）、`tests/test_qoder_cn_direct_offline.py::ExplicitQuotaLimitHitTests`（真实文案命中 + 不误伤 + `_qoder_max_pool_key` 只对两内置 Max 与 CN 自定义主力返回非 None、其他一律 None）。Windows/Linux CI 矩阵都跑。
