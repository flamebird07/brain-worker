# 桌面状态浮窗（只读牛马看板）

BW-WORKER-WIDGET-20261018-S2 新增：一个小巧的 Windows 桌面浮窗，实时只读显示各牛马
（跨对话调度池）的工作状态。**纯标准库 + 本机已有 Python/Tk，无任何新依赖；不新增
服务/HTTP/API，不查余额，不调度、不派工、不杀进程、不改共享库一个字节。**

## 最简单启动

```text
pythonw scripts/worker_status_widget.py
```

`pythonw` 无控制台窗口；用 `python` 也可以。可选参数：

- `--store <路径>`：显式指定调度池 SQLite。缺省沿用 `dispatch_pool` 的权威默认
  （`~/.brain-worker/dispatch-pool.sqlite3`，`BRAIN_WORKER_DISPATCH_STORE` 环境覆盖）。
  路径不存在时如实显示“读取失败 · 待核”，**绝不建库/建表**。
- `--interval <秒>`：刷新间隔，默认 2 秒、最小 1 秒。
- `--once`：不启动 GUI，只读取一次并输出安全 JSON（不含 token/prompt/argv/秘密），
  供主脑/脚本做真实只读验证；快照不可用时退出码 2。

桌面双击入口（`.bat`/快捷方式指向上述 `pythonw` 命令）、真实本机启动与截图验收由
root 阶段完成；本文件只登记启动方式。

## 数据从哪来

数据只来自**跨对话共享的调度池 SQLite**（`dispatch_pool` 的 attempts 等既有表），
经 `stage_selection._open_store_ro` 的 `mode=ro` 短事务只读快照——与阶段规划入口同一
套拒绝语义：缺库、缺表、被锁定、WAL 存在但缺 `-shm` 都如实拒绝/待核，绝不创建
sidecar、绝不 migrate、绝不回落成空表冒充空闲。

浮窗**不直接探测任何模型、不查余额、不扫原始日志**；“每 2 秒更新”只是定时重读这份
本地数据库快照，不是每秒抓日志或轮询外部系统。

## 界面语义（重要：显示 ≠ 业务验收）

- 行状态：**运行中 / 已预留（排队）/ 待核实 / 空闲 / 时段外**。
  - 占用计数以 attempts 表 `reserved/running/unknown` 为权威：子进程死了、PID 复用、
    创建身份对不上都**仍占位**，浮窗绝不代替 reconcile 释放名额。
  - **主行状态绝不只信 DB `running`**（BW-WORKER-WIDGET-20261018-S3 root 审查修正）：
    只有探针确认「活 PID + 创建身份匹配」才显示运行中；死 PID、PID 复用/身份不符、
    探测失败/未知、running 却未绑定子进程，主行一律显著「待核实」。同池混合时
    **待核优先**，绝不被另一个真 running 掩盖成绿灯。`reserved` 未绑子进程仍如实
    显示「已预留」；Luna 原生行不套本机探针。DB 原状态在详情里如实回显，占位计数不变。
  - “待核实”表示**本机进程状态未知或身份不明**，是诚实的不确定，不是业务裁决。
  - “时段外”只表示该池当前不接受**新派**（三 Qoder Max 北京夜间窗 / Claude 空闲时段
    门）；已有在途任务照常显示，绝不被时段遮盖。
- 顶部“总占用 x/14”：国内物理总槽 14（权威常量 `DOMESTIC_TOTAL_CAPACITY`）。
  CodeBuddy 两个模型（DeepSeek-V4.1-Flash 与条件补位 GLM-5.3-Flash）**不共享名额、
  各自独立 1 槽**（BW-WORKER-WIDGET-20261018-S3 用户 2026-10-18 最新明确；旧「共用
  同一物理 1 槽、总 13」口径作废），两行独立计数；Luna 外援行无上限、**不计入 14**。
- 行名完整显示用户给定的 CN 名称：`Qoder CN／Qwen3.8-Max`（pool_key
  `qodercn:Qwen3.8-Max`）与 `Qoder CN／token plan／Qwen-3.8-Max`（pool_key
  `qodercn:Qwen-3.8-Max`），**不再写「CN 内置/CN 自定义」**；精确 runtime/model
  pool_key 不变。主界面宽 500，关键名称绝不截断。
- “上轮调用通过 / 未通过 / 待核”只读 attempts 的真实 `success` 字段：缺失=待核，
  `finished` 不一概当绿灯；这是**最近一次调用的记录**，不是永久健康状态，更不是
  业务验收。“上轮限额”仅在主力限额标记仍绑定该池最新 attempt 时显示，属历史标记，
  不实时查额度。
- 点击行在底部显示当前任务完整 ID、阶段与最近时间；界面不显示 token、argv、prompt、
  原始错误正文或任何秘密。

## 窗口操作

标题栏拖动移动；“置顶:开/关”切换总在最前；“—”收起为小条——收起时隐藏置顶按钮与
长标题，小条内**总占用 / 展开（▢）/ 关闭（×）三要素完整可见、绝不裁剪**；“▢”
展开还原 500 宽主界面；“×”**仅关闭浮窗本身**——不触碰任何 worker、不终止任何进程、
不写任何状态。Windows 命名 mutex 单实例（ctypes 已显式声明 64 位 `HANDLE`
（`c_void_p`）返回与 `CreateMutexW`/`CloseHandle` argtypes，避免句柄截断/泄漏）：
重复双击不会叠出多个浮窗；mutex 创建失败时诚实降级放行，绝不伪称单实例已验证。
真实 GUI 行为（含收起条实际像素宽度）由 root 阶段本机启动验证。

## 离线验证

```text
python tests/test_worker_status_widget.py
```

临时 SQLite + 合成 rows + 注入时钟/PID 探针；不启动真 Tk、不连真实池、不调真实
模型。覆盖两 CodeBuddy 独立计数（总 14）、死 PID/复用/探测未知/未绑定 running 主行
待核、混合池待核不被真 running 掩盖、真活才运行中、reserved 无子进程仍已预留、
跨时段在途不被遮盖、success 缺失不伪装通过、旧限额不压更新成功、缺库/锁定/损坏
如实待核且读取前后库字节与行数不变、Luna 不计国内总数且不套本机探针、import 不
启动 GUI 等。
