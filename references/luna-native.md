# GPT-6 Luna 原生执行端参考

## 接入范围与选择

这是 brain-worker 的可选原生执行端指导，不是新增本机 CLI、自动调度器或统一执行器适配代码。Luna 使用所在云环境提供的原生子 Agent 工具；模型标识为 `gpt-6-luna`。默认兜底仍为 Qoder，沿用现有唯一模型及入口配置；ZCode 用于加速。广告任务原有执行选择继续有效，在途任务不因登记 Luna 被替换或重复派发。

## 在并发容量池中的角色：只救援、无上限、绝不自动启动

在跨会话共享的并发容量池（`scripts/dispatch_pool.py`，见 [global-dispatch](global-dispatch.md)）里，
Luna 是**救援**通道（组合 `luna:native`），**无数量上限**，但**绝不在正常路由里被自动选中**：
`select_and_claim` 对 Luna 请求一律 `luna_requires_ticket` 拒绝。只有满足全部条件才可由**宿主原生
调用**：

1. 六个国内名额（主力 `zcode:GLM-5.3`/`qoder:Qwen3.8-Max` 各 2 + 溢出 `qoder:Qwen3.8-Flash` 2）
   **确实全满**；
2. 宿主**真的问过用户**“外部 agent 还是 Luna”，并用 `ask_record` 落库票据、开始 **300 秒**计时
   （Python 只落库，绝不谎称已问或已派生 Luna）；
3. 300 秒内**无回复**（一旦用户 `reply` 即不再自动裁决——等待超时≠默认无限授权）；
4. `claim_due` 在同一 `BEGIN IMMEDIATE` 事务内竞争裁决通过（票据仍 pending、已过 deadline、未回复、
   未 claim、六名额仍全满），返回 `launch_state='host_must_call_native'`——**宿主仍须真实调用原生
   工具**，池只给裁决与名额，不代跑。

等待期间任何国内名额释放**优先国内并原子取消同 task 的 pending 票据**（不双派国内+Luna）；同一 task
只允许一个 pending/claimed 票据、**重启不重置 deadline**；原生工具已启动但在写出 agentID 前崩溃 →
`mark_launch_unknown` 记 `launch_unknown` 待人工核验、**绝不自动重复启动**；对已 claimed 票据的重复
`claim_due` 同样拒绝（非幂等）。Luna **沿用原任务的文件/命令/副作用范围**，不借救援绕过权限拒绝、
额度错误或部署审批。

## 调用与审批

- 每次调用前读取当前环境工具说明、用户授权、当次生效的 custom rule 和审批要求；审批未满足时不启动。历史批准或启动证据不能批准下一次调用。
- 仅在实际工具支持该模型且任务适合所在云环境时，按当前工具 schema 构造调用，绑定任务范围、允许动作、停止条件和验收标准。不把示例参数当作通用命令，不固定推理档位或上下文继承策略。
- 原生子 Agent 只在所属云环境中执行，不能直接控制用户电脑。本云端主聊的本地工作仍经已授权 `cloud_threads` 进入对应本机环境；不同环境的工具、文件和审批分别核对，不把本机能力归给 Luna。
- 不新增或替换 Qoder/ZCode/CodeBuddy 的模型、凭证和运行配置。不编造 Luna CLI、路径、API key、退出码、session ID、四接口协议或本地工具能力。
- 保留当前任务的实际调用及原始返回、模型参数、任务标识与可取得的事件证据；没有证据的字段写未知。主脑独立验收，调用成功不能替代业务通过；未知状态先查，授权不足不自动重试或切换执行端。

## 现有启动证据及限制

来源是根线程 `01a1118d-e8ae-77df-a6ad-c0352e629004` 于 2026-10-07 提供的验证结果：使用 `collaboration.spawn_agent(model="gpt-6-luna", reasoning_effort="xhigh", fork_turns="none")` 完成一次合成排序启动验证，子 Agent 正常返回 `[2,5,7]`。

本机本轮没有独立读取原始云端调用日志，也没有新启动 Luna。该历史证据仅支持根线程所报告的单次启动与合成小任务返回，不证明本地桌面工具、文件读写、完整业务闭环、模型后端独立身份核验、实际用量或套餐/额度节省。`xhigh` 与 `fork_turns="none"` 仅描述历史调用，不是永久默认值。

## 记录与交付

分别记录 `brain_worker`、`external_dispatch`、`native_dispatch`，真实调用次数与目标环境。只有实际启动才计入原生执行轮次；静态指导更新、历史证据登记和离线校验计新增调用 0 次。缺少原始证据时不补写 worker 报告，不将启动验证当作正式业务评分样本。

来源：上述根线程委派说明；仓库/安装版 `SKILL.md` 的 Luna 可选执行端章节。


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
