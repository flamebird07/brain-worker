---
name: brain-worker
description: 在 ZCode 桌面端使用本机 brain-worker 规划、派发和独立验收工程任务；需要调用共享牛马池、查看牛马状态或继续受阻任务时使用。
---

# Brain Worker for ZCode Desktop

这是 ZCode 桌面端的轻量入口。实际调度、容量、优先级、时段、限额和执行器入口继续使用同一套 brain-worker 引擎，不在这里复制一套容易漂移的规则。

## 启动

1. 读取 `${ZCODE_SKILL_DIR}/engine.json`，取得 `engine_root`。该文件只保存本机路径和版本，不得包含凭据。
2. 确认以下文件存在：
   - `<engine_root>/scripts/stage_selection.py`
   - `<engine_root>/scripts/dispatch_pool.py`
   - `<engine_root>/scripts/worker_status_widget.py`
3. 缺文件或 `engine.json` 无效时，明确报告 ZCode 入口未正确安装，不猜路径、不新建另一套状态库。

用户可在 ZCode 新会话输入 `/brain-worker`，也可在与本技能描述匹配的任务中让 ZCode 自动加载本技能。

## ZCode 桌面端的角色

- 默认把当前 ZCode 桌面会话当作主脑宿主：收敛目标、生成调度输入、展示本轮牛马、验收产物并完成适用收尾。
- 当前宿主不是可再次派发的 `zcode:GLM-5.3` worker。构造本轮 `allowed_candidates` 时排除这个精确组合，并在回执中记录 `host_excluded_candidates=["zcode:GLM-5.3"]`，防止 ZCode 递归启动自己。
- 如果用户明确要求当前 ZCode 桌面会话亲自完成任务，直接在当前会话工作，不把它伪装成一次外部牛马派发，也不占共享 ZCode worker 名额。
- Luna 已删除并强制禁用，任何容量或等待状态都不得启动 Luna。

## 每轮流程

1. 读取用户当前目标、工作区和授权边界。用户的最新明确要求优先，不能从旧任务复制临时兜底模型或过期候选列表。
2. 读取共享池当前状态，再调用 `<engine_root>/scripts/stage_selection.py` 生成本阶段只读选择回执。不要自己重写优先级、容量、时段或限额算法。
3. 每个新调度轮从当前合格候选的最高优先级开始。本轮某组合真实失败后才把它加入本轮失败列表；下一新轮重新裁决。权限、工作区冲突或未知在途状态不得靠换执行器绕过。
4. 正式启动前，必须向用户显示：`本轮牛马：执行器／请求模型；阶段目标；状态：准备派发。` 真正提交后显示“已提交”；预检拒绝则显示“未派发”和原始原因。
5. 只调用所选执行器对应的正式入口，保存 request、进程终态、原始 stdout/stderr、报告、工具事件和产物回读。模型自述、退出码 0、格式合格或测试数量都不能单独证明工程完成。
6. 独立核对实际改动、必要测试和业务结果。首次权限拒绝停止当前受影响调用并保存证据；在已有授权范围内修正配置或重新裁决，不把内部错误升级成整个用户任务暂停。
7. 用户任务尚未完成时继续处理已有授权且可行的部分。状态询问、故障报告和阶段复盘不能取代原目标。

## 调用现有引擎

- 共享状态与浮窗：优先使用 `<engine_root>/scripts/worker_status_widget.py --once` 的只读结果。
- 阶段选择：使用 `<engine_root>/scripts/stage_selection.py`；授权范围、工作区、权限需求和本轮失败候选必须来自当前任务事实。
- 正式派发：只使用所选执行器已有的正式 `*_direct.py` 入口。先读该入口的 `--help` 和对应 reference，再构造参数；不凭经验混用 `--tools` 与 `--allowed-tools`。
- 更详细的执行器约束按需读取 `<engine_root>/references/` 中对应文件。不要一次性读取完整的大型主 Skill，也不要把历史事故段落当成当前状态。

ZCode 桌面端只负责调用既有引擎，不修改真实共享池来制造空位，不清历史错误，不查询余额，不复制或输出任何凭据。

## 验收与收尾

- 区分：模型已提交、工具实际执行、代码验收、生产加载、GitHub 远端、Obsidian 记录和能力台账。
- 业务验收通过后仍有适用且已授权的 GitHub、Obsidian 或安装同步时，状态保持“收尾中”并继续；远端完整提交回读和文档写后回读完成后才能说整体完成。
- 最终答复必须列出实际使用的执行器／请求模型、真实轮数、每轮结果、独立验收、未完成项，以及 `brain_worker=used`、`external_dispatch`。当前 ZCode 会话自身工作不记作外部派工。

