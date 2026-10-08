# Qoder CLI 本机直连

使用已有官方 `@qoder-ai/qodercli` 运行时与官方保存的登录态。复制 `scripts/local-entry.json.example` 为同目录的 `local-entry.json`，填写本机 node 和 `qodercli.js` 的绝对路径；也可通过 `--config` 指定配置。真实配置不会进入 Git。

```text
python <技能目录>/scripts/qoder_direct.py --workspace <项目绝对路径> --prompt-file <UTF-8提示词文件> --output-dir <不存在的新证据目录> --stage <非空阶段编号> --model Qwen3.8-Max --tools Read
```

默认模型请求为 Qwen3.8-Max（本轮用户明确请求 Max，官方模型清单已确认 `Qwen3.8-Max` 为合法 `--model` 值；`Qwen3.8-Flash` 仍可显式选择，作为并发容量池的**溢出**组合）；默认不给工具权限。续接使用 `--resume-session-id`，内部传官方 `--resume`。不自动重装、登录或无限调度。

入口在**建输出目录、Popen 之前**必须消费或校验一个跨会话共享的**并发容量 claim**（`scripts/dispatch_pool.py`，见 [global-dispatch](global-dispatch.md)）：给了 `--dispatch-claim <token>` 就精确校验（task/runtime/model/workspace/prompt_sha256 任一漂移即拒），没给就走 `select_and_claim` 原子路由；非被选中组合 → `routing_required`/`sent=false`/退出 2、零证据目录、不计轮次。可选 `--task-id`、`--dispatch-store`（默认 `BRAIN_WORKER_DISPATCH_STORE` 或 `~/.brain-worker/dispatch-pool.sqlite3`）、`--dispatch-claim`。子进程真实结束立即释放名额；存活未知不释放、不被抢占。**容量并发 ≠ 额度冷却**。

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
