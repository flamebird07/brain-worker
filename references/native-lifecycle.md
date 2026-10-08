# 原生 Luna 任务收口参考（settle-native）

## 适用场景

原生 Luna 任务没有本地 OS 子进程，不能走普通 `finish`。真实任务（国内六名额满后
发起的 Luna 救援，真实 ask 后固定 300 秒等待，可无限 dispatch）结束后，必须由
受信任宿主完成终态回读与收口结算：

- 国内恢复优先：六名额（2 Z + 2 Max + 2 Flash）有空位时优先国内；
- Luna 不占国内六名额，收口也不影响国内在跑进程；
- Luna 不另设数量上限。

## 宿主可信边界（必须如实执行）

本脚本不做、也不声称做任何服务端验证。终态证明完全依赖受信任宿主：

1. 宿主必须先用原生工具（如查询 agent/run 状态的官方工具）**独立回读**该
   agent_id 的真实终态（finished / failed），并保留原始回执原文；
2. 原始 worker 的输出文本（哪怕是 terminal 字符串）**不能**当终态；
3. 回读显示 running / unknown → 不要调用 settle-native（会拒绝且不释放，属预期）；
   显示启动即崩溃无 agentID → 走 mark-launch-unknown，人工核验，绝不自动重派。

## CLI（通用示例，请替换为真实值）

```
python scripts/dispatch_pool.py settle-native \
  --task-id <task_id> \
  --token <claim-due 返回的 claimed_token> \
  --agent-id <record-agent-id 登记的真实 agentID> \
  --terminal finished|native_failed \
  --scope '<原票据 scope JSON 原样传入>' \
  --receipt '{"source_tool":"<回读所用原生工具名>","receipt_ref":"<原始回执可检索引用>","sha256":"<原始回执哈希>"}'
```

- `terminal` 仅接受 `finished`（成功）/ `native_failed`（失败），成功失败分开落库；
  running/unknown 一律拒绝；正常完成绝不伪标 cancelled/start_failed，不假造 PID；
- 硬校验：票据 claimed 且 token 一致、launch_state 非 launch_unknown、agent_id
  已登记且一致、scope 与票据原 JSON 完全一致、回执三字段齐全；任一不满足拒绝
  且不释放，task/workspace 继续占用；
- 幂等：只有"同终态 + 同原回执哈希"的重复结算返回 idempotent，错误 agent、
  缺回执、不同终态、scope 漂移、不同哈希一律拒绝且不覆盖旧证据；
- `--success` 只能与 terminal 一致（finished=成功、native_failed=失败）。

## SHA 口径

`sha256` 必须是严格 64 位 ASCII 十六进制（统一小写落库）：

- string 回执原文按 UTF-8 原字节哈希，**不加 JSON 引号**；
- 对象回执按 canonical JSON 口径哈希；
- `content` 与 `sha256` 同时给出必须核对一致，不一致拒绝。

## 回执保存

原始回执原件由宿主留存；本地只保存 `source_tool` / `receipt_ref` / `sha256`
等可追溯元信息（存 attempt 的 adopt_evidence 列），作为宿主可信边界内的
可追溯证据，不是独立服务端验证。

## 验证边界

`tests/test_native_lifecycle.py` 的 17 项只是隔离合成验证（临时 SQLite、注入
时钟、离线），未运行真实 Luna 平台验收。
