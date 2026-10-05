# CodeBuddy Code CLI 本机直连（最小传输入口）

使用已登录的官方 CodeBuddy Code CLI（本阶段核对版本 2.161.1）。复制
`scripts/codebuddy-entry.json.example` 为同目录的 `codebuddy-entry.json`，填写本机
node 与 CodeBuddy CLI 入口文件（`bin/codebuddy`）的绝对路径；也可用 `--config`
指定。真实配置含本机路径与安装位置，不得提交 Git。

```text
python scripts/codebuddy_direct.py --workspace <项目绝对路径> --prompt-file <UTF-8提示词文件> --output-dir <不存在的新证据目录> --stage <非空阶段编号> --model <模型ID>
```

`--model` 必填，无默认、无 auto、无 fallback。`--resume-session-id` 可选，内部传
官方 `--resume`。不自动重装、登录、重试、后台取消或通知；主脑不开第二个 CLI 主脑。

## 调用形状（本机 CLI 2.161.1 已核对）

```text
node <cli/bin/codebuddy> -p --verbose --output-format stream-json --model <ID> \
  --permission-mode dontAsk --tools <清单> --strict-mcp-config \
  --mcp-config '{"mcpServers":{}}' --setting-sources '' \
  --settings '{"disableAllHooks":true}' --agent cli --no-session-persistence
```

- 提示词（契约 + 用户原文，不裁剪）经 stdin 完整传送；无 shell 拼接；cwd 指定
  workspace；env 仅 os.environ 副本加 `DISABLE_AUTOUPDATER=1`；不修改机器全局设置。
  提示词原文不落盘进 `request.json`（避免重复暴露），仅在 `request.json` 记录其
  UTF-8 字节 SHA-256；CRLF 等原文字节经 stdin 原样传输、不做换行翻译。
- `--tools` 永远恰好出现一次，值是一个 comma-joined 字符串（空即 `''`）；不得逐项
  追加参数，也不得省略。空 `--tools ''` 表示零授权，同时不追加任何 `--allowed-tools`
  回退规则，避免“看似限权实则全放”。
- `--allowed-tools` / `--disallowed-tools` 为 variadic：一次 flag 后跟全部规则
  （list，不拼字符串）；每条规则原样传入，不按逗号拆分，非字符串/空/仅空白拒绝。
- 无显式 allow 时按 `--tools` 逐项回退为允许规则；显式列表（含空列表）完全替代回退。
- `--permission-mode` 固定 `dontAsk`；永不启用 bypass 或 auto-review 代理；不开
  子 Agent 或联网工具，tools 白名单保证这些不可见。

## 权限范围限制（重要）

权限规则不是文件 sandbox：dontAsk 模式下规则影响提示与自动放行，不构成对进程文件
访问的强制隔离。敏感目录保护必须依靠主脑侧环境隔离，不能只依赖本入口。

## 协议终态（不只看 exit 0）

stdout 为 stream-json JSONL，逐字以字节保存 `stdout.jsonl`（stderr 同样逐字保存
`stderr.log`）。真实终态要求：

- stdout 必须严格 UTF-8；出现非法字节保留原件并拒绝，禁止 `errors='replace'` 伪造原文。
- 唯一最后 `type=result`、`subtype=success`、`is_error=false`、非空 text。
- init.session_id 非空，所有带 session_id 的事件必须完全一致，不得漂移。
- init 模型及所有 `assistant.message.model` 与请求模型精确一致。
- 至少一个 assistant.message 存在非空 text 分片；`result.result` 与最后 assistant
  的最后一个 text 分片精确一致（不 trim）。
- `init.mcp_servers` 必须严格为 `[]`；非空一律拒绝，不得宣称零 MCP。
- `init.tools` 是整个工具注册表，不代表本次有效工具面；实际 `tool_use.name` 记录为
  已调用证据，若超出请求 `--tools` 白名单即判越权失败。
- 权限拒绝实际出现在 `user.message.content[].type=tool_result` 的文本里（真实形状下
  `is_error=false`、`result.permission_denials=[]`）。空 `permission_denials` 不作为
  无拒绝证据：入口从 tool_result 文本扫描明确的 “Permission to use … has been denied”
  文案，命中即 fail-closed，并原样保存拒绝字段与 tool_use_id。只检测 tool_result，
  不匹配模型报告正文里的普通引用。
- 未知事件/缺项/重复或缺失终态/尾随非 JSON/`terminal_reason` 为 cancelled、aborted、
  max_turns / `result.permission_denials` 非空一律判协议失败并保留证据。事件字段版本
  不同不猜测，缺项记 `parse_errors`，由主脑拿真实事件回读后再修。

计费口径未知：`usage` / `modelUsage` 仅原样保存 raw 值；`total_cost_usd=0` 不视为
免费证据；`business_verified`、`free_quota_verified`、`model_backend_identity_verified`
恒为 false。CLI 路由标识不证明底层模型身份，零 token 不证明免费。

## 证据与退出码

输出目录含 `request.json`（argv、提示词 UTF-8 字节哈希、stage、cwd、模型、工具规则；
不含提示词正文）、`process.json`（pid、状态、真实退出码）、`stdout.jsonl`、`stderr.log`、
`response.md`（result 原文字节，不 trim 不删前言）、`report-state.json`（九节正文
核对 + 最终绑定）、`summary.json`。preflight 校验错误退出码 2 且证据目录零创建；
协议成功且报告绑定均为真才退出 0；其余失败退出 3。失败证据同样完整保存，原件不删改。

官方文档：https://www.codebuddy.cn/docs/cli/cli-reference 、
https://www.codebuddy.cn/docs/cli/iam 。字段若与真实 CLI 回读不同，以主脑真实
回读为准，本入口标未知待修。

运行时参数名称为 `--allowedTools` / `--disallowedTools`（camelCase）；入口对外的重复 `--allowed-tools RULE` / `--disallowed-tools RULE` 由脚本转换，不能原样传给官方 CLI。已实测 kebab-case 会在模型调用前退出。
