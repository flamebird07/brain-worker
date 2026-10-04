# Qoder CLI 本机直连

使用已有官方 `@qoder-ai/qodercli` 运行时与官方保存的登录态。复制 `scripts/local-entry.json.example` 为同目录的 `local-entry.json`，填写本机 node 和 `qodercli.js` 的绝对路径；也可通过 `--config` 指定配置。真实配置不会进入 Git。

```text
python <技能目录>/scripts/qoder_direct.py --workspace <项目绝对路径> --prompt-file <UTF-8提示词文件> --output-dir <不存在的新证据目录> --stage <非空阶段编号> --model Qwen3.8-Flash --tools Read
```

默认模型请求为 Qwen3.8-Flash；默认不给工具权限。续接使用 `--resume-session-id`，内部传官方 `--resume`。不自动重装、登录或无限调度。

输出包含 request.json、process.json、stdout.json、stderr.log、response.md、summary.json、report-state.json。保留原始信封与最终回复，核对协议终态、实际会话、阶段、路径、九节正文、续接与 SHA-256。先检查 protocol_success 与最终 bound，再独立验收业务。失败原件保留，不替执行者改写。

本机真实验收已验证指定文件 Read、随机校验码与求和、报告绑定；41 项离线回归覆盖配置、参数、协议、边界、歧义字段、哈希与续接绑定。CLI 回传 qfmodel/gfmodel 路由，无法独立确认底层模型版本或真实计费。

## 九节兼容报告模板

本驱动机械校验此模板。阶段字段与路径字段必须填写提示词中的精确值，首尾标记各独占一行，不加前后说明或围栏。主脑从本文件正确提取以下完整代码块，原样嵌入任务说明，不仅引用模板名。执行者最终回复完整报告，由调用器原样落盘。

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
```

测试使用本地假执行器，不需要 Qoder 登录或任何网络调用，不证明真实后端能力。真实派工与用量由独立主脑验收记录确认。
