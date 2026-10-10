# Qoder CN 本机原生直连（qodercn）

> 运行时 `runtime='qodercn'`：本机已登录的 Qoder CN **原生 CLI**（EXE），与既有国际
> `qoder`（node + `qodercli.js` bundle）**并存、互不覆盖**。本轮落实最小可验收改动：
> 独立配置、友好名→真实模型 ID 映射、独立 2 槽补充候选、跨区域并行/同区互斥、跨 runtime
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

- `qodercn:DeepSeek-Flash` 固定 2 槽，是**独立补充候选**：既可经显式 `executor='qodercn'` 直接
  claim 自身真实容量（**绝不因历史 committed 比例改道**、也绝不与国际/ZCode 互串），也作为
  **`AUTO` 的最后补充候选**（BW-QODER-CN-20261010-A4）——`AUTO` 仍在原合格主力间做 best-effort
  1:1、国际 Flash 溢出居中，只有**原合格主力与国际 Flash 都不可用或已满**时才把 CN 当补充候选：
  此时 `AUTO` 返回 `routing_required` 选 CN（调用入口已是 CN 则直接允许 claim），**原 6 满 + CN
  空绝不报整池 full，只有原 8 全满才物理 full**。`AUTO` 用 CN 不改道、不评分、不新增轮换状态或
  额度逻辑；明确指定 `Qoder`/`ZCode`/`CN` 的任务约束不互换、不扩大。默认入口仍通过准确
  runtime/model 选中的真正执行器提交，绝不伪装 Qwen 或改未授权模型。
- **国内合计 8**：原 Z2 / 国际 Max2 / 国际 Flash2 **完全原样**，CN 补 2。Luna 阈值随总 8 更新，
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
python tests/test_dispatch_pool_qodercn.py      # CN2/总8、跨区域并行、同区互斥、跨 runtime 拒、终态释放、Z hold 跳过
```

两者用本地假 Popen、临时 SQLite，绝不读写真实/共享池或额度库，不证明真实后端能力。CN 隔离小
读/写真实验收已由主脑完成（见上节）；完整工程验收、安装版/仓库版与 GitHub/Obsidian 同步仍由主脑
独立执行，本文件不声称已完成。
