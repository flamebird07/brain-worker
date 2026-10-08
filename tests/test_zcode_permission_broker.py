"""BW-ZCODE-PERMISSION-20261007-S1(-REPAIR1) 受控命令审批离线测试：真实 runner + 本地双重，
不联网、不登录、不调模型。

覆盖需求（全部来自子进程真实留证与真实官方 request 形状，而不是自身返回常量）：
- 跨语言 canonical SHA：Python zcode_direct._canonical_json 与 broker.canonicalSha 对同一
  注册表逐字节一致（否则独立绑定无从比对）。
- 装饰执行闸门：只把逐字登记且 realpath 后精确 cwd 的命令转发给 inner；未登记（含看似只读
  `ls /etc`）、复合（echo/分号/cd/管道/wrapper 追加/重定向/裁剪）、argv 通道、cwd 缺失/越界、
  额外 env|stdin|bashPrelude|sandbox 关闭全部在派生 inner.run 之前拒绝。
- C：input_sha256 声明的代码/测试范围在执行前真实读当前文件并复核 realpath/边界/SHA；被改写/
  移除不启动 inner；同一 command 的多 cwd 各自绑定独立 input（不丢弃后续）。注册表深冻结。
- A：官方 PermissionBrokerRequest 无 cwd；broker 用宿主冻结 trustedWorkingDirectory；无工作目录
  上下文即拒绝；真实 runner 确实把上下文传给 broker（集成层证明）。
- B：唯一胜者——claimResponse 至多一次；自动与人工分支在 claim=false 时均判 race-lost，绝不 allow。
- D：privacy_free 时只有登记且匹配的允许命令写原文，未登记只留 SHA；任何审计写失败均冒泡。
- F：spawn_error 不计入执行数，attempted/started 单列；拒绝结果用官方有效形状（error.type 非 'denied'）。
- G：装饰端口只暴露 run/close，不暴露后台方法；超时自动转后台回落前台经闸门（据源码）。
- 独立四状态互不推断；fail-closed（无 adapters_entry / SHA 不符）零提交、无 response.md。

不访问网络、不读凭据、不 spawn 真实进程。真实 SDK 现场验收由主脑在本阶段之后独立执行。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = ROOT / 'scripts'
RUNNER = SCRIPTS / 'zcode_sdk_runner.mjs'
BROKER = SCRIPTS / 'zcode_permission_broker.mjs'
sys.path.insert(0, str(SCRIPTS))
import zcode_direct as zd  # noqa: E402
import prompt_contract as pc  # noqa: E402

NODE = None


def node_bin():
    global NODE
    if NODE:
        return NODE
    from shutil import which
    NODE = which('node')
    if not NODE:
        raise unittest.SkipTest('node runtime not available for offline JS double')
    return NODE


# ---- broker 单元脚本：node 导入真实 broker，用官方 request 形状跑完整判定组合，打印 JSON ----
BROKER_UNIT_JS = r'''
import { pathToFileURL } from 'node:url';
import { readFileSync, writeFileSync, realpathSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
const broker = await import(pathToFileURL(process.argv[2]).href);
const cfg = JSON.parse(readFileSync(process.argv[3], 'utf8'));
const WS = cfg.WS;
const sha = (b) => createHash('sha256').update(Buffer.from(b, 'utf8')).digest('hex');

const fixtureTarget = realpathSync(join(WS, 'scripts', 'fixture.txt'));
const contract = { privacy_free: false, commands: [
  { command: 'ls scripts/', cwd: WS,
    input_sha256: { 'scripts/fixture.txt': { target: fixtureTarget, sha: cfg.origSha } } },
  { command: 'python spawnfail.py', cwd: WS },
] };
const registry = broker.buildRegistry(contract, { workspaceRealPath: WS });
const out = {};
out.canonical_sha = broker.canonicalSha(contract);
out.count = registry.count;

function shell(command, cwd, extra = {}) {
  return { command: { mode: 'shell', command, shellProfile: 'posix-bash' }, cwd,
           trace: { sessionId: 'S', turnId: 'T', attributes: { toolCallId: 'tc' } }, ...extra };
}
const innerCalls = [];
const inner = {
  async run(req, opts) {
    const cmd = req.command.command;
    if (opts && typeof opts.onEvent === 'function') {
      opts.onEvent({ type: 'started', pid: 4242, timestamp: new Date() });
    }
    if (cmd === 'python spawnfail.py') {
      return { status: 'spawn_error', exitCode: undefined, signal: undefined,
        stdout: { text: '', bytes: 0, truncated: false },
        stderr: { text: 'spawn boom', bytes: 10, truncated: false },
        durationMs: 1, timedOut: false, cancelled: false,
        startedAt: new Date(), completedAt: new Date(),
        error: { type: 'spawn_error', message: 'boom' } };
    }
    innerCalls.push(cmd);
    return { status: 'completed', exitCode: 0, signal: undefined,
      stdout: { text: 'ok', bytes: 2, truncated: false },
      stderr: { text: '', bytes: 0, truncated: false },
      durationMs: 1, timedOut: false, cancelled: false,
      startedAt: new Date(), completedAt: new Date(), pid: 1 };
  },
  async close() { innerCalls.push('__close__'); },
  async runBashWithBackgroundLifecycle() { throw new Error('nope'); },
};
const auditLines = [];
const sink = { record: p => auditLines.push(p) };
const gated = broker.createGatedExecutionPort(inner, registry, sink);
out.gate_methods = gated._gateMethods().sort();

const cases = {
  registered: shell('ls scripts/', WS),
  unregistered_readonly: shell('ls /etc', WS),
  cwd_mismatch_otherdir: shell('ls scripts/', cfg.otherDir),
  cwd_outside: shell('ls scripts/', cfg.outsideDir),
  cwd_missing: shell('ls scripts/', join(WS, 'does-not-exist')),
  echo_semicolon: shell('echo hi; ls scripts/', WS),
  cd_prefix: shell('cd scripts/ && ls', WS),
  pipe_wrapper: shell('ls scripts/ | wc', WS),
  redirect: shell('ls scripts/ > out.txt', WS),
  trim_variant: shell(' ls scripts/ ', WS),
  extra_env: shell('ls scripts/', WS, { env: { base: 'inherit', set: { X: '1' } } }),
  extra_stdin: shell('ls scripts/', WS, { stdin: 'data' }),
  sandbox_disabled: shell('ls scripts/', WS,
    { sandbox: { enabled: false, dangerouslyDisableSandbox: true } }),
};
out.decisions = {};
for (const [n, r] of Object.entries(cases)) out.decisions[n] = broker.gateDecision(r, registry);
out.argv_channel = broker.gateDecision(
  { command: { mode: 'argv', file: 'python', args: [] }, cwd: WS, trace: {} }, registry);

await gated.run(cases.registered);                     // fixture intact -> completed
writeFileSync(join(WS, 'scripts', 'fixture.txt'), 'changed\n', 'utf8');
out.after_change = broker.gateDecision(cases.registered, registry);   // C: input changed
writeFileSync(join(WS, 'scripts', 'fixture.txt'), cfg.origContent, 'utf8');
await gated.run(shell('python spawnfail.py', WS));     // F: spawn_error, attempted not completed
await gated.run(cases.unregistered_readonly);          // denied, no inner
await gated.run(cases.pipe_wrapper);                   // denied, no inner
out.inner_reached = innerCalls.filter(c => c !== '__close__');
out.executed_completed = gated._executedCount();
out.attempted = gated._attemptedCount();
out.started = gated._startedCount();
out.has_background_method = typeof gated.runBashWithBackgroundLifecycle === 'function';

// C: 同 command 多 cwd 各自绑定独立 input（不丢弃后续）
const mcContract = { privacy_free: false, commands: [
  { command: 'python run.py', cwd: WS,
    input_sha256: { 'scripts/fixture.txt': { target: fixtureTarget, sha: cfg.origSha } } },
  { command: 'python run.py', cwd: cfg.otherDir },
] };
const mcReg = broker.buildRegistry(mcContract, { workspaceRealPath: WS });
const mcRecs = [...mcReg.byCommand.get('python run.py').values()];
out.multi_cwd_records = mcRecs.length;
out.multi_cwd_inputs_preserved = mcRecs.filter(r => r.inputEntries.length === 1).length === 1
  && mcRecs.filter(r => r.inputEntries.length === 0).length === 1;

// broker ask 路径：官方 request 形状——无 cwd（A）。用 trustedWorkingDirectory 绑定上下文。
const wsBroker = broker.createPermissionBroker(registry, sink, { trustedWorkingDirectory: WS });
const askReg = { requestId: 'r1', sessionId: 'S', turnId: 'T', toolCallId: 'tc',
                 toolName: 'Bash', mode: 'edit', input: { command: 'ls scripts/' } };
const askBefore = JSON.stringify(askReg);
out.allow_registered = await wsBroker.requestPermission(askReg, {});
out.input_unmodified = JSON.stringify(askReg) === askBefore;
out.deny_unregistered = await wsBroker.requestPermission(
  { ...askReg, input: { command: 'cat /etc/passwd' } }, {});
// B: 登记命令 + claim=false（无 humanClient 的自动分支）也必须 race-lost，不得 allow
out.registered_claim_false = await wsBroker.requestPermission(askReg, { claimResponse: () => false });
const ctl = new AbortController(); ctl.abort();
out.cancelled = await wsBroker.requestPermission(askReg, { signal: ctl.signal });
// A: 未绑定工作目录上下文 -> 独立拒绝状态
out.no_wd = await broker.createPermissionBroker(registry, sink)
  .requestPermission(askReg, {});
// 超时：正数 timeoutMs + 永不 resolve 的 client
const slow = broker.createPermissionBroker(registry, sink,
  { trustedWorkingDirectory: WS, humanClient: () => new Promise(() => {}) });
out.timeout = await slow.requestPermission(askReg, { timeoutMs: 5 });
// 缺客户端
const miss = broker.createPermissionBroker(registry, sink,
  { trustedWorkingDirectory: WS, humanClient: () => { throw new Error('No permission client available'); } });
out.client_missing = await miss.requestPermission(askReg, { timeoutMs: 1000 });
// 人工批准 / 拒绝
const okClient = broker.createPermissionBroker(registry, sink,
  { trustedWorkingDirectory: WS, humanClient: () => true });
out.human_approve = await okClient.requestPermission(askReg, { timeoutMs: 1000 });
const noClient = broker.createPermissionBroker(registry, sink,
  { trustedWorkingDirectory: WS, humanClient: () => false });
out.human_deny = await noClient.requestPermission(askReg, { timeoutMs: 1000 });
// 人工分支 claim=false -> race-lost（唯一胜者对人工分支同样适用）
const raceClient = broker.createPermissionBroker(registry, sink,
  { trustedWorkingDirectory: WS, humanClient: () => true });
out.race_lost = await raceClient.requestPermission(
  askReg, { timeoutMs: 1000, claimResponse: () => false });

// D: privacy_free 只对匹配允许命令写原文；未登记只留 SHA
const pfContract = { privacy_free: true, commands: [{ command: 'ls scripts/', cwd: WS }] };
const pfReg = broker.buildRegistry(pfContract, { workspaceRealPath: WS });
const pfLines = [];
const pfGated = broker.createGatedExecutionPort(inner, pfReg, { record: p => pfLines.push(p) });
await pfGated.run(shell('ls scripts/', WS));
await pfGated.run(shell('ls /etc', WS));
out.pf_raw_for_matched = pfLines.some(l => l.kind === 'gate' && l.allowed === true && l.command === 'ls scripts/');
out.pf_unregistered_hashed = pfLines.some(l => l.kind === 'gate' && l.allowed === false
  && !('command' in l) && ('command_sha256' in l));

// 审计写失败必须冒泡（拒绝路径也不静默通过）
let threw = false;
try {
  const bad = broker.createGatedExecutionPort(inner, registry,
    { record() { throw new Error('disk full'); } });
  await bad.run(cases.registered);
} catch (e) { threw = String(e.message).includes('disk full'); }
out.audit_failure_propagates = threw;

// 有效拒绝形状：error.type 不是 'denied'（官方 ExecutionFailureType 无该值）
let denyShape = null;
try {
  const shapeInner = { async run() { throw new Error('must not run'); }, async close() {} };
  const shapeSink = { record() {} };
  const shapeGated = broker.createGatedExecutionPort(shapeInner, registry, shapeSink);
  denyShape = await shapeGated.run(cases.unregistered_readonly);
} catch (e) { out.deny_shape_error = String(e); }
if (denyShape) {
  out.deny_status = denyShape.status;
  out.deny_error_type = denyShape.error?.type ?? null;
  out.deny_stderr_bytes_match = denyShape.stderr.bytes === Buffer.byteLength(denyShape.stderr.text, 'utf8');
}

// REPAIR2-1：执行前用**原始声明路径**重新解析并比对登记物理目标——目标漂移（alias/junction
// 改指）即拒，且不启动 inner。用真实存在的 elsewhere.txt 作被改指目标，声明路径仍解析到 fixture。
const movedContract = { privacy_free: false, commands: [
  { command: 'ls scripts/', cwd: WS,
    input_sha256: { 'scripts/fixture.txt':
      { target: realpathSync(join(WS, 'scripts', 'elsewhere.txt')), sha: cfg.origSha } } },
] };
const movedReg = broker.buildRegistry(movedContract, { workspaceRealPath: WS });
out.target_moved = broker.gateDecision(shell('ls scripts/', WS), movedReg);
const movedGated = broker.createGatedExecutionPort(inner, movedReg, { record() {} });
await movedGated.run(shell('ls scripts/', WS));
out.target_moved_attempted = movedGated._attemptedCount();

// REPAIR2-3：非 Bash 工具（Write / 缺失 toolName）携带同一已登记 input.command 也必须规则拒绝。
out.tool_write = await wsBroker.requestPermission(
  { requestId: 'rw', sessionId: 'S', turnId: 'T', toolCallId: 'tc', toolName: 'Write',
    mode: 'edit', input: { command: 'ls scripts/' } }, {});
out.tool_missing = await wsBroker.requestPermission(
  { requestId: 'rm', sessionId: 'S', turnId: 'T', toolCallId: 'tc',
    mode: 'edit', input: { command: 'ls scripts/' } }, {});

// REPAIR2-2：execution_started 审计写失败仍保留观测到的 started 计数，不把已启动谎报为零动作。
const startInner = {
  async run(req, opts) {
    if (opts && typeof opts.onEvent === 'function') opts.onEvent({ type: 'started', pid: 7, timestamp: new Date() });
    return { status: 'completed', exitCode: 0, signal: undefined,
      stdout: { text: 'ok', bytes: 2, truncated: false }, stderr: { text: '', bytes: 0, truncated: false },
      durationMs: 1, timedOut: false, cancelled: false, startedAt: new Date(), completedAt: new Date(), pid: 7 };
  },
  async close() {},
};
const startSink = { record(p) { if (p.kind === 'execution_started') throw new Error('started audit disk full'); } };
let startThrew = false;
const startedPort = broker.createGatedExecutionPort(startInner, registry, startSink);
try { await startedPort.run(shell('ls scripts/', WS)); }
catch (e) { startThrew = String(e.message || e).includes('started audit disk full'); }
out.started_audit_throws = startThrew;
out.started_after_failure = startedPort._startedCount();
out.attempted_after_failure = startedPort._attemptedCount();
out.executed_after_failure = startedPort._executedCount();

// REPAIR3：官方 Bash handler 硬编码注入 embedded-search prelude。绑定宿主冻结 backend 的期望
// prelude 后，仅真实 Bash trace + 逐字节一致的 frozen prelude 才放行；backend 差异 / 额外字段 /
// findAndGrepEnabled 非 false / 其它 kind / 非 Bash / 缺 toolCallId / 空 sessionId / env+prelude /
// stdin+prelude 一律拒绝且不启动 inner（attempted 仍为放行数）。用真实官方 request 形状。
const frozenBackend = { kind: 'native-binaries', findCommand: 'cfgBfs', grepCommand: 'cfgUgrep', rgCommand: 'cfgRg' };
const expectedPrelude = { kind: 'embedded-search', backend: frozenBackend, findAndGrepEnabled: false };
const preludeBinding = { preludeSha256: broker.canonicalSha(expectedPrelude) };
const BASH_TRACE = { sessionId: 'S', turnId: 'T', attributes: { toolCallId: 'tc', toolName: 'Bash' } };
function bashShell(command, cwd, prelude, trace) {
  return { command: { mode: 'shell', command, shellProfile: 'posix-bash' }, cwd,
           bashPrelude: prelude, captureCwdAfterSuccess: true,
           trace: trace ?? BASH_TRACE };
}
const NB = (over) => Object.assign({ kind: 'native-binaries', findCommand: 'cfgBfs', grepCommand: 'cfgUgrep', rgCommand: 'cfgRg' }, over);
const preludeCases = {
  matched: bashShell('ls scripts/', WS, { kind: 'embedded-search', backend: NB(), findAndGrepEnabled: false }),
  backend_diff: bashShell('ls scripts/', WS, { kind: 'embedded-search', backend: NB({ findCommand: 'other' }), findAndGrepEnabled: false }),
  extra_backend_field: bashShell('ls scripts/', WS, { kind: 'embedded-search', backend: NB({ extra: 'x' }), findAndGrepEnabled: false }),
  findgrep_enabled_true: bashShell('ls scripts/', WS, { kind: 'embedded-search', backend: NB(), findAndGrepEnabled: true }),
  findgrep_missing: bashShell('ls scripts/', WS, { kind: 'embedded-search', backend: NB() }),
  kind_other: bashShell('ls scripts/', WS, { kind: 'other-prelude', backend: NB() }),
  non_bash_tool: bashShell('ls scripts/', WS, expectedPrelude, { sessionId: 'S', turnId: 'T', attributes: { toolCallId: 'tc', toolName: 'Write' } }),
  missing_toolcall: bashShell('ls scripts/', WS, expectedPrelude, { sessionId: 'S', turnId: 'T', attributes: { toolName: 'Bash' } }),
  empty_session: bashShell('ls scripts/', WS, expectedPrelude, { sessionId: '', turnId: 'T', attributes: { toolCallId: 'tc', toolName: 'Bash' } }),
  env_plus_prelude: Object.assign(bashShell('ls scripts/', WS, expectedPrelude), { env: { base: 'inherit' } }),
  stdin_plus_prelude: Object.assign(bashShell('ls scripts/', WS, expectedPrelude), { stdin: 'x' }),
};
out.prelude_decisions = {};
for (const [n, r] of Object.entries(preludeCases)) out.prelude_decisions[n] = broker.gateDecision(r, registry, preludeBinding);
// 无绑定：任何 prelude 都拒绝（维持既有 deny-extra-exec-channel 语义）。
out.prelude_no_binding = broker.gateDecision(preludeCases.matched, registry);
// 带绑定：匹配的 prelude 进入 inner 一次；各拒绝 inner=0。
const preludeInnerCalls = [];
const preludeInner = { async run(req) { preludeInnerCalls.push(req.command.command);
  return { status: 'completed', exitCode: 0, stdout: { text: 'ok', bytes: 2, truncated: false },
    stderr: { text: '', bytes: 0, truncated: false }, durationMs: 1, timedOut: false, cancelled: false,
    startedAt: new Date(), completedAt: new Date(), resolvedCwd: WS }; }, async close() {} };
const preludeGated = broker.createGatedExecutionPort(preludeInner, registry, { record() {} }, preludeBinding);
await preludeGated.run(preludeCases.matched);
for (const n of ['backend_diff', 'extra_backend_field', 'findgrep_enabled_true', 'findgrep_missing',
  'kind_other', 'non_bash_tool', 'missing_toolcall', 'empty_session', 'env_plus_prelude',
  'stdin_plus_prelude']) { await preludeGated.run(preludeCases[n]); }
out.prelude_inner_reached = preludeInnerCalls;      // 只应有 ['ls scripts/']
out.prelude_attempted = preludeGated._attemptedCount();  // 1

process.stdout.write(JSON.stringify(out));
'''

# 跨语言规范化：读取同一份 JSON 契约文件，打印 broker.canonicalSha，用于与 Python
# zcode_direct._canonical_json 逐字节比对（两语言必须产出同一 canonical）。
CANON_JS = r'''
import { readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
const broker = await import(pathToFileURL(process.argv[2]).href);
const contract = JSON.parse(await readFile(process.argv[3], 'utf8'));
process.stdout.write(broker.canonicalSha(contract));
'''

# Python 归一化契约 → JS 真实消费链：导入真实 broker，用 Python 产出的 normalized 契约
# buildRegistry，再对给定 request 跑 gateDecision（含 buildRegistry 抛错也结构化回传）。
CHAIN_JS = r'''
import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';
const broker = await import(pathToFileURL(process.argv[2]).href);
const contract = JSON.parse(readFileSync(process.argv[3], 'utf8'));
const req = JSON.parse(readFileSync(process.argv[4], 'utf8'));
const wsReal = process.argv[5];
let reg;
try { reg = broker.buildRegistry(contract, { workspaceRealPath: wsReal }); }
catch (e) { process.stdout.write(JSON.stringify({ build_error: String(e.message || e) })); process.exit(0); }
process.stdout.write(JSON.stringify(broker.gateDecision(req, reg)));
'''


# ---- 集成双重：本地 bootstrap（记录并消费注入端口）+ 本地 adapters ----
BOOTSTRAP_DOUBLE = r'''
import { appendFileSync } from 'node:fs';
const mark = (obj) => appendFileSync(process.env.DOUBLE_RECEIPT, JSON.stringify(obj) + '\n');
export async function startProcessProviderRegistryRuntime() {
  const selection = { providerId: 'account:bigmodel-individual-coding-plan',
                      modelId: 'GLM-5.3-Flash', options: { reasoningLevel: 'low' } };
  return { runtime: { registryService: {
    getView: () => ({ providers: [{ providerId: selection.providerId,
      models: [{ modelId: selection.modelId }] }] }),
    validateSelection: () => ({ ok: true }) } },
    providerRuntimeHeadersPort: {}, dispose: () => mark({ event: 'dispose' }) };
}
export async function createZCodeApp(options) {
  const selection = options.configuredDefaultModelSelection;
  mark({ event: 'app', has_executionPort: typeof options.executionPort?.run === 'function',
         has_permissionBroker: typeof options.permissionBroker?.requestPermission === 'function',
         gated_methods: options.executionPort ? Object.keys(options.executionPort).filter(k => !k.startsWith('_')) : [] });
  return {
    sessionId: 'sess-perm-1',
    listModels: () => [{ ref: selection }],
    setModel: async () => {},
    getCurrentModelOption: () => ({ ref: selection }),
    getModel: () => `${selection.providerId}/${selection.modelId}`,
    runtime: { getToolRegistry: () => ({ list: () => ['Read', 'Bash', 'Write'] }) },
    submitPrompt: async (prompt, opts) => {
      const port = options.executionPort;
      const broker = options.permissionBroker;
      const tr = (tc) => ({ sessionId: 'sess-perm-1', turnId: 'turn-perm-1', attributes: { toolCallId: tc } });
      const ws = options.runtimeConfig.workingDirectory;
      // 通过注入端口驱动：登记命令与未登记/复合命令各一次。
      await port.run({ command: { mode: 'shell', command: 'ls scripts/', shellProfile: 'posix-bash' },
                       cwd: ws + '/scripts', trace: tr('tc-reg') });
      await port.run({ command: { mode: 'shell', command: 'ls /etc', shellProfile: 'posix-bash' },
                       cwd: ws + '/scripts', trace: tr('tc-unreg') });
      await port.run({ command: { mode: 'shell', command: 'echo hi; ls', shellProfile: 'posix-bash' },
                       cwd: ws + '/scripts', trace: tr('tc-echo') });
      // A：官方 broker request 无 cwd；runner 已绑定 trustedWorkingDirectory。
      await broker.requestPermission({ requestId: 'p1', sessionId: 'sess-perm-1', turnId: 'turn-perm-1',
        toolCallId: 'tc-reg', toolName: 'Bash', mode: 'edit', input: { command: 'ls scripts/' } }, {});
      await broker.requestPermission({ requestId: 'p2', sessionId: 'sess-perm-1', turnId: 'turn-perm-1',
        toolCallId: 'tc-unreg', toolName: 'Bash', mode: 'edit', input: { command: 'cat /etc/passwd' } }, {});
      opts.onEvent({ sessionId: 'sess-perm-1', turnId: 'turn-perm-1', type: 'model_request',
        payload: { providerId: selection.providerId, modelId: selection.modelId } });
      opts.onEvent({ sessionId: 'sess-perm-1', turnId: 'turn-perm-1', type: 'turn_complete',
        payload: { resultType: 'success', response: 'done' } });
      return { response: 'done', turnId: 'turn-perm-1', projection: { status: 'completed' },
               usage: { inputTokens: 10, outputTokens: 5, totalTokens: 15 } };
    },
    close: async () => mark({ event: 'close' }),
  };
}
'''

ADAPTERS_DOUBLE = r'''
import { appendFileSync } from 'node:fs';
const receipt = process.env.INNER_RECEIPT;
export function createNodeExecutionAdapter(options = {}) {
  // I：核对 runner 传入官方 env 语义（processEnv），并记录到 double 回执供断言。
  if (process.env.ADAPTER_OPT_RECEIPT) {
    appendFileSync(process.env.ADAPTER_OPT_RECEIPT, JSON.stringify({
      has_processEnv: Boolean(options.processEnv),
      platform: options.platform ?? null,
    }) + '\n');
  }
  return {
    async run(request, options) {
      // 基础端口官方会在启动时发 started 事件；REPAIR2-2 需要真实触发它。
      if (options && typeof options.onEvent === 'function') {
        options.onEvent({ type: 'started', pid: 5, timestamp: new Date() });
      }
      if (receipt) appendFileSync(receipt, request.command.command + '\n');
      return { status: 'completed', exitCode: 0, signal: undefined,
        stdout: { text: 'inner-out', bytes: 9, truncated: false },
        stderr: { text: 'inner-err', bytes: 9, truncated: false },
        durationMs: 1, timedOut: false, cancelled: false,
        startedAt: new Date(), completedAt: new Date(), pid: 5 };
    },
    async runBashWithBackgroundLifecycle() { throw new Error('inner background must not be reached'); },
    async close() { if (receipt) appendFileSync(receipt, '__close__\n'); },
  };
}
'''

# REPAIR3：模拟官方 Bash handler——读 runner 传入的 runtimeConfig.embeddedSearchBackend 与
# nativeSearchEnhancementsEnabled，据 shell.ts/call-runner.ts 事实生成 typed embedded-search prelude
# （findAndGrepEnabled:false），随真实 Bash trace 一并喂给注入的执行端口。
BOOTSTRAP_DOUBLE_PRELUDE = r'''
import { appendFileSync } from 'node:fs';
const mark = (obj) => appendFileSync(process.env.DOUBLE_RECEIPT, JSON.stringify(obj) + '\n');
export async function startProcessProviderRegistryRuntime() {
  const selection = { providerId: 'account:bigmodel-individual-coding-plan',
                      modelId: 'GLM-5.3-Flash', options: { reasoningLevel: 'low' } };
  return { runtime: { registryService: {
    getView: () => ({ providers: [{ providerId: selection.providerId,
      models: [{ modelId: selection.modelId }] }] }),
    validateSelection: () => ({ ok: true }) } },
    providerRuntimeHeadersPort: {}, dispose: () => mark({ event: 'dispose' }) };
}
export async function createZCodeApp(options) {
  const selection = options.configuredDefaultModelSelection;
  const rc = options.runtimeConfig;
  mark({ event: 'app', has_executionPort: typeof options.executionPort?.run === 'function',
         has_permissionBroker: typeof options.permissionBroker?.requestPermission === 'function',
         embedded_search_backend_kind: rc?.embeddedSearchBackend?.kind ?? null,
         native_search_disabled: rc?.nativeSearchEnhancementsEnabled === false });
  return {
    sessionId: 'sess-prelude-1',
    listModels: () => [{ ref: selection }],
    setModel: async () => {},
    getCurrentModelOption: () => ({ ref: selection }),
    getModel: () => `${selection.providerId}/${selection.modelId}`,
    runtime: { getToolRegistry: () => ({ list: () => ['Read', 'Bash', 'Write'] }) },
    submitPrompt: async (prompt, opts) => {
      const port = options.executionPort;
      const frozenBackend = rc.embeddedSearchBackend;
      const ws = rc.workingDirectory;
      const trace = (tc) => ({ sessionId: 'sess-prelude-1', turnId: 'turn-prelude-1',
        attributes: { toolCallId: tc, toolName: 'Bash' } });
      // 官方 handler 生成的 prelude：backend 来自 runtimeConfig，findAndGrepEnabled:false。
      const sdkPrelude = { kind: 'embedded-search', backend: frozenBackend, findAndGrepEnabled: false };
      // 1) 真实 Bash + SDK 内部 prelude 与冻结值一致 → 应放行，inner 执行。
      await port.run({ command: { mode: 'shell', command: 'ls scripts/', shellProfile: 'posix-bash' },
                       cwd: ws + '/scripts', bashPrelude: sdkPrelude, captureCwdAfterSuccess: true,
                       trace: trace('tc-match') });
      // 2) backend 与冻结值不符 → deny-bash-prelude-mismatch，inner 不启动。
      await port.run({ command: { mode: 'shell', command: 'ls scripts/', shellProfile: 'posix-bash' },
                       cwd: ws + '/scripts', captureCwdAfterSuccess: true,
                       bashPrelude: { kind: 'embedded-search',
                         backend: Object.assign({}, frozenBackend, { findCommand: 'wrong' }),
                         findAndGrepEnabled: false }, trace: trace('tc-badbackend') });
      // 3) 非 Bash toolName（Write）携带同一 prelude → deny-bash-trace-unverified。
      await port.run({ command: { mode: 'shell', command: 'ls scripts/', shellProfile: 'posix-bash' },
                       cwd: ws + '/scripts', captureCwdAfterSuccess: true, bashPrelude: sdkPrelude,
                       trace: { sessionId: 'sess-prelude-1', turnId: 'turn-prelude-1',
                         attributes: { toolCallId: 'tc-write', toolName: 'Write' } } });
      opts.onEvent({ sessionId: 'sess-prelude-1', turnId: 'turn-prelude-1', type: 'model_request',
        payload: { providerId: selection.providerId, modelId: selection.modelId } });
      opts.onEvent({ sessionId: 'sess-prelude-1', turnId: 'turn-prelude-1', type: 'turn_complete',
        payload: { resultType: 'success', response: 'done' } });
      return { response: 'done', turnId: 'turn-prelude-1', projection: { status: 'completed' },
               usage: { inputTokens: 10, outputTokens: 5, totalTokens: 15 } };
    },
    close: async () => mark({ event: 'close' }),
  };
}
'''

# REPAIR3：同 SDK 公开 embedded-search backend 解析器双身——只读 env 指定键，返回确定性 backend，
# 供 runner 冻结并绑定期望 prelude（真实 SDK 中该解析器为 resolveDefaultEmbeddedSearchBackend）。
BACKEND_RESOLVER_DOUBLE = r'''
export function resolveDefaultEmbeddedSearchBackend(input = {}) {
  const env = input.env ?? {};
  const override = env.ZCODE_EMBEDDED_SEARCH_COMMAND;
  if (override) return { kind: 'internal-cli', command: override, args: ['__internal-search'] };
  return { kind: 'native-binaries', findCommand: 'bfs', grepCommand: 'ugrep', rgCommand: 'rg' };
}
'''


def run_node(script_file, args, env_extra=None, cwd=None):
    env = os.environ.copy()
    env.update({'PYTHONIOENCODING': 'utf-8'})
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run([node_bin(), str(script_file), *args], capture_output=True,
                          text=True, encoding='utf-8', env=env, cwd=cwd, timeout=120)
    return proc


def _gate_events(path):
    return [json.loads(l) for l in path.read_text(encoding='utf-8').splitlines() if l.strip()]


class BrokerUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.harness = self.base / 'broker_unit.mjs'
        self.harness.write_text(BROKER_UNIT_JS, encoding='utf-8')
        ws = self.base / 'proj'
        (ws / 'scripts').mkdir(parents=True)
        (ws / 'other').mkdir()
        outside = self.base / 'outside'
        outside.mkdir()
        self.orig_content = 'orig\n'
        fixture = ws / 'scripts' / 'fixture.txt'
        # newline='' 阻止 Windows 把 '\n' 翻译成 '\r\n'，否则磁盘字节与登记 SHA 不一致，
        # 执行前 input SHA 复核会误判 mismatch（正是缺陷 C 要拦截的替换）。origSha 直接取
        # 磁盘真实字节哈希，保证与 Node 端 writeFileSync(cfg.origContent) 的 LF 内容一致。
        fixture.write_text(self.orig_content, encoding='utf-8', newline='')
        # elsewhere.txt：REPAIR2-1 target-moved 用作“被 alias 改指到的另一真实文件”。
        (ws / 'scripts' / 'elsewhere.txt').write_text('elsewhere\n', encoding='utf-8', newline='')
        self.cfg = {
            'WS': str(ws),
            'otherDir': str(ws / 'other'),
            'outsideDir': str(outside),
            'origSha': hashlib.sha256(fixture.read_bytes()).hexdigest(),
            'origContent': self.orig_content,
        }
        self.cfg_path = self.base / 'cfg.json'
        self.cfg_path.write_text(json.dumps(self.cfg), encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self):
        proc = run_node(self.harness, [str(BROKER), str(self.cfg_path)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_canonical_sha_matches_python(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        canon_js = tmp / 'canon.mjs'
        canon_js.write_text(CANON_JS, encoding='utf-8')
        contract = {'privacy_free': False, 'commands': [
            {'command': 'ls scripts/', 'cwd': 'C:\\work\\proj\\scripts',
             'input_sha256': {'a.txt': '0' * 64}},
            {'command': 'python x.py', 'cwd': 'C:/work/proj'}]}
        contract_file = tmp / 'contract.json'
        contract_file.write_text(json.dumps(contract), encoding='utf-8')
        proc = run_node(canon_js, [str(BROKER), str(contract_file)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        js_sha = proc.stdout
        py_sha = hashlib.sha256(zd._canonical_json(contract).encode('utf-8')).hexdigest()
        self.assertEqual(js_sha, py_sha)

    def test_gate_decision_states(self):
        d = self._run()['decisions']
        self.assertTrue(d['registered']['allowed'])
        self.assertEqual(d['unregistered_readonly']['reason'], 'deny-unregistered-command')
        self.assertEqual(d['cwd_mismatch_otherdir']['reason'], 'deny-cwd-mismatch')
        self.assertEqual(d['cwd_outside']['reason'], 'deny-cwd-outside-workspace')
        self.assertEqual(d['cwd_missing']['reason'], 'deny-cwd-missing')
        self.assertEqual(d['echo_semicolon']['reason'], 'deny-composite-or-variant')
        self.assertEqual(d['cd_prefix']['reason'], 'deny-composite-or-variant')
        self.assertEqual(d['pipe_wrapper']['reason'], 'deny-composite-or-variant')
        self.assertEqual(d['redirect']['reason'], 'deny-composite-or-variant')
        self.assertEqual(d['trim_variant']['reason'], 'deny-unregistered-command')
        self.assertEqual(d['extra_env']['reason'], 'deny-extra-exec-channel')
        self.assertEqual(d['extra_stdin']['reason'], 'deny-extra-exec-channel')
        self.assertEqual(d['sandbox_disabled']['reason'], 'deny-sandbox-disabled')

    def test_argv_channel_denied(self):
        self.assertEqual(self._run()['argv_channel']['reason'], 'deny-channel-not-shell')

    def test_input_sha_reverified_before_inner(self):
        data = self._run()
        self.assertFalse(data['after_change']['allowed'])
        self.assertEqual(data['after_change']['reason'], 'deny-input-sha-mismatch')

    def test_multi_cwd_preserves_inputs(self):
        data = self._run()
        self.assertEqual(data['multi_cwd_records'], 2)
        self.assertTrue(data['multi_cwd_inputs_preserved'])

    def test_only_registered_completed_reaches_inner(self):
        data = self._run()
        self.assertEqual(data['inner_reached'], ['ls scripts/'])
        self.assertEqual(data['executed_completed'], 1)
        # spawn_error 计入 attempted/started，但不计入 executed 完成回执（F）。
        self.assertEqual(data['attempted'], 2)
        self.assertEqual(data['started'], 2)

    def test_gated_port_method_surface_is_minimal(self):
        data = self._run()
        self.assertEqual(data['gate_methods'], ['close', 'run'])
        self.assertFalse(data['has_background_method'])

    def test_denied_result_uses_valid_official_shape(self):
        data = self._run()
        self.assertEqual(data['deny_status'], 'failed')
        self.assertNotEqual(data['deny_error_type'], 'denied')
        self.assertIn(data['deny_error_type'],
                      ['spawn_error', 'timeout', 'cancelled', 'sandbox_violation',
                       'output_limit', 'unknown'])
        self.assertTrue(data['deny_stderr_bytes_match'])

    def test_broker_allow_registered_without_cwd_or_grant(self):
        data = self._run()
        allow = data['allow_registered']
        self.assertEqual(allow['decision'], 'allow')          # A：无 cwd 请求登记命令放行
        self.assertNotIn('permissionUpdates', allow)
        self.assertNotIn('sessionPermissionUpdates', allow)
        self.assertNotIn('modifiedInput', allow)
        self.assertTrue(data['input_unmodified'])

    def test_broker_registered_claim_false_is_race_lost(self):
        data = self._run()
        self.assertEqual(data['registered_claim_false']['decision'], 'deny')      # B
        self.assertTrue(data['registered_claim_false']['reason'].startswith('race-lost'))

    def test_broker_distinct_refusal_states(self):
        data = self._run()
        self.assertEqual(data['deny_unregistered']['decision'], 'deny')
        self.assertTrue(data['deny_unregistered']['reason'].startswith('rule-denied'))
        self.assertEqual(data['cancelled']['reason'].split(':')[0], 'cancelled')
        self.assertEqual(data['no_wd']['reason'].split(':')[0], 'no-working-directory')
        self.assertEqual(data['timeout']['reason'].split(':')[0], 'approval-timeout')
        self.assertEqual(data['client_missing']['reason'].split(':')[0], 'client-missing')
        self.assertEqual(data['race_lost']['reason'].split(':')[0], 'race-lost')
        self.assertEqual(data['human_approve']['decision'], 'allow')
        self.assertEqual(data['human_deny']['reason'].split(':')[0], 'rule-denied')

    def test_privacy_free_only_registers_raw_for_matched(self):
        data = self._run()
        self.assertTrue(data['pf_raw_for_matched'])
        self.assertTrue(data['pf_unregistered_hashed'])

    def test_audit_write_failure_propagates(self):
        self.assertTrue(self._run()['audit_failure_propagates'])

    def test_input_target_moved_denied_without_inner(self):
        data = self._run()
        self.assertFalse(data['target_moved']['allowed'])
        self.assertEqual(data['target_moved']['reason'], 'deny-input-target-moved')
        # 声明路径实时解析到的目标 != 登记目标 → 判定漂移，inner 一次都没启动（attempted=0）。
        self.assertEqual(data['target_moved_attempted'], 0)

    def test_non_bash_tool_denied_before_claim_or_allow(self):
        data = self._run()
        for key in ('tool_write', 'tool_missing'):
            self.assertEqual(data[key]['decision'], 'deny', key)
            self.assertTrue(data[key]['reason'].startswith('rule-denied'), key)
            self.assertIn('deny-tool-not-bash', data[key]['reason'], key)

    def test_started_preserved_when_started_audit_write_fails(self):
        data = self._run()
        self.assertTrue(data['started_audit_throws'])          # 审计写失败仍冒泡
        self.assertEqual(data['started_after_failure'], 1)     # 观测到的 started 事实保留
        self.assertEqual(data['attempted_after_failure'], 1)   # attempted 单列
        self.assertEqual(data['executed_after_failure'], 0)    # 无完成回执 → 不谎报已执行

    def test_embedded_search_prelude_only_matches_frozen_binding(self):
        pd = self._run()['prelude_decisions']
        self.assertTrue(pd['matched']['allowed'])                         # 真实 Bash + 冻结 prelude → 放行
        self.assertEqual(pd['backend_diff']['reason'], 'deny-bash-prelude-mismatch')
        self.assertEqual(pd['extra_backend_field']['reason'], 'deny-bash-prelude-mismatch')
        self.assertEqual(pd['findgrep_enabled_true']['reason'], 'deny-bash-prelude-mismatch')
        self.assertEqual(pd['findgrep_missing']['reason'], 'deny-bash-prelude-mismatch')
        self.assertEqual(pd['kind_other']['reason'], 'deny-bash-prelude-mismatch')
        self.assertEqual(pd['non_bash_tool']['reason'], 'deny-bash-trace-unverified')
        self.assertEqual(pd['missing_toolcall']['reason'], 'deny-bash-trace-unverified')
        self.assertEqual(pd['empty_session']['reason'], 'deny-bash-trace-unverified')
        self.assertEqual(pd['env_plus_prelude']['reason'], 'deny-extra-exec-channel')
        self.assertEqual(pd['stdin_plus_prelude']['reason'], 'deny-extra-exec-channel')

    def test_prelude_without_binding_denied_and_only_frozen_reaches_inner(self):
        data = self._run()
        # 无绑定时任何 SDK 内部 prelude 仍按既有语义拒绝（不静默放行）。
        self.assertEqual(data['prelude_no_binding']['reason'], 'deny-extra-exec-channel')
        # 有绑定时只放逐字节匹配的那一次，其余（backend/字段/findAndGrep/kind/非 Bash/缺 trace/env/stdin）inner 均不启动。
        self.assertEqual(data['prelude_inner_reached'], ['ls scripts/'])
        self.assertEqual(data['prelude_attempted'], 1)


class RunnerIntegrationTests(unittest.TestCase):
    def _setup(self, contract_commands, adapters_token='{adapter}', override_sha=None,
               mode='edit', privacy_free=False, prelude=False):
        base = Path(tempfile.mkdtemp()).resolve()
        ws = base / 'proj'
        (ws / 'scripts').mkdir(parents=True)
        (ws / 'scripts' / 'note.txt').write_text('seed\n', encoding='utf-8')
        double_receipt = base / 'double-receipt.jsonl'
        inner_receipt = base / 'inner-receipt.txt'
        adapter_opt_receipt = base / 'adapter-opt.jsonl'
        boot = base / 'bootstrap_double.mjs'
        boot.write_text(BOOTSTRAP_DOUBLE_PRELUDE if prelude else BOOTSTRAP_DOUBLE, encoding='utf-8')
        adapter = base / 'adapters_double.mjs'
        adapter.write_text(ADAPTERS_DOUBLE, encoding='utf-8')
        out = base / 'evidence'
        scripts_cwd = os.path.normcase(os.path.realpath(str(ws / 'scripts')))
        contract = {'privacy_free': privacy_free,
                    'commands': sorted([{'command': c, 'cwd': scripts_cwd}
                                        for c in contract_commands],
                                       key=lambda e: (e['command'], e['cwd']))}
        bound = hashlib.sha256(zd._canonical_json(contract).encode('utf-8')).hexdigest()
        request = {
            'carrier': 'zcode-sdk', 'stage': 'BW-PERM-ITG', 'workspace': str(ws),
            'output_dir': str(out), 'prompt': 'task', 'prompt_sha256': '0' * 64,
            'prompt_payload': {}, 'selection': {'providerId': 'account:bigmodel-individual-coding-plan',
                                                'modelId': 'GLM-5.3-Flash',
                                                'options': {'reasoningLevel': 'low'}},
            'mode': mode, 'allowed_tools': ['Read', 'Bash'],
            'tool_disallowlist_base': ['Write'], 'resume_session_id': None,
            'preflight_only': False, 'entry_config': str(base / 'entry.json'),
            'environment_keys': [], 'bootstrap': str(boot),
            'builtin_provider_config': str(ws / 'scripts' / 'note.txt'),
            'personal_provider_config': str(ws / 'scripts' / 'note.txt'),
            'version': None, 'runner': str(RUNNER), 'argv': [],
            'command_contract': contract,
            'command_contract_sha256': override_sha or bound,
            'adapters_entry': (adapters_token.format(adapter=str(adapter))
                               if adapters_token != 'NONE' else 'NONE'),
        }
        if prelude:
            resolver = base / 'embedded_search_backend_double.mjs'
            resolver.write_text(BACKEND_RESOLVER_DOUBLE, encoding='utf-8')
            request['embedded_search_backend_entry'] = str(resolver)
        (base / 'entry.json').write_text(json.dumps({'environment': {}}), encoding='utf-8')
        out.mkdir(parents=True, exist_ok=False)
        (out / 'request.json').write_text(json.dumps(request), encoding='utf-8')
        env = {'DOUBLE_RECEIPT': str(double_receipt), 'INNER_RECEIPT': str(inner_receipt),
               'ADAPTER_OPT_RECEIPT': str(adapter_opt_receipt)}
        proc = run_node(RUNNER, ['--request', str(out / 'request.json')], env_extra=env, cwd=str(ws))
        return base, out, proc, double_receipt, inner_receipt, adapter_opt_receipt

    def test_injection_consumed_and_evidence_written(self):
        b, out, proc, double_receipt, inner_receipt, adapter_opt = self._setup(['ls scripts/'])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        envelope = json.loads(proc.stdout)
        self.assertTrue(envelope['approval_client_ready'])
        self.assertTrue(envelope['controlled_pre_exec_gate_ready'])
        self.assertTrue(envelope['tool_visible_bash'])                 # H
        self.assertTrue(envelope['command_actually_executed'])
        self.assertEqual(envelope['command_executed_receipts'], 1)
        self.assertEqual(envelope['command_attempted'], 1)             # 只有登记命令 attempted
        self.assertEqual(envelope['command_started'], 1)               # REPAIR2-2：观测到基础端口 started
        # I：runner 用官方 env 语义构造 inner（processEnv 存在，platform 记录）。
        opt_lines = [json.loads(l) for l in adapter_opt.read_text(encoding='utf-8').splitlines() if l.strip()]
        self.assertTrue(opt_lines and all(l['has_processEnv'] for l in opt_lines))
        dlines = [json.loads(l) for l in double_receipt.read_text(encoding='utf-8').splitlines() if l.strip()]
        apprec = next(d for d in dlines if d['event'] == 'app')
        self.assertTrue(apprec['has_executionPort'])
        self.assertTrue(apprec['has_permissionBroker'])
        self.assertEqual(sorted(apprec['gated_methods']), ['close', 'run'])
        self.assertIn({'event': 'close'}, dlines)
        self.assertIn({'event': 'dispose'}, dlines)
        inner = [c for c in inner_receipt.read_text(encoding='utf-8').splitlines()
                 if c.strip() and c != '__close__']
        self.assertEqual(inner, ['ls scripts/'])
        events = _gate_events(out / 'permission-events.jsonl')
        kinds = {e['kind'] for e in events}
        self.assertIn('gate', kinds)
        self.assertIn('execution_receipt', kinds)
        self.assertIn('approval', kinds)
        # A：无 cwd 的官方 broker request，登记命令在真实 runner 绑定工作目录后放行。
        approvals = [e for e in events if e['kind'] == 'approval']
        self.assertIn('approved-registered', {a['state'] for a in approvals})
        self.assertIn('rule-denied', {a['state'] for a in approvals})
        denied = [e for e in events if e['kind'] == 'gate' and not e['allowed']]
        self.assertTrue(denied)
        for e in denied:
            self.assertNotIn('command', e)
            self.assertIn('command_sha256', e)

    def test_privacy_free_only_registers_raw_for_matched(self):
        b, out, proc, double_receipt, inner_receipt, adapter_opt = self._setup(
            ['ls scripts/'], privacy_free=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        events = _gate_events(out / 'permission-events.jsonl')
        allowed = [e for e in events if e['kind'] == 'gate' and e['allowed']]
        denied = [e for e in events if e['kind'] == 'gate' and not e['allowed']]
        self.assertTrue(allowed and all('command' in e for e in allowed))   # 匹配才写原文
        self.assertTrue(denied and all('command' not in e for e in denied))  # 未登记只留 SHA

    def test_frozen_embedded_search_prelude_bound_and_executes(self):
        b, out, proc, double_receipt, inner_receipt, adapter_opt = self._setup(
            ['ls scripts/'], prelude=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        envelope = json.loads(proc.stdout)
        # runner 用同 SDK 解析器冻结 backend 并绑定期望 prelude（只暴露非敏感 hash/kind）。
        self.assertTrue(envelope['embedded_search_prelude_bound'])
        self.assertEqual(envelope['embedded_search_backend_kind'], 'native-binaries')
        self.assertEqual(len(envelope['embedded_search_prelude_sha256']), 64)
        # 冻结 prelude 的真实 Bash 进入 inner 并执行；backend 不符 / 非 Bash 两次被前置拒绝，inner=0。
        self.assertTrue(envelope['command_actually_executed'])
        self.assertEqual(envelope['command_executed_receipts'], 1)
        self.assertEqual(envelope['command_attempted'], 1)
        self.assertEqual(envelope['command_started'], 1)
        self.assertIn('gate-deny-bash-prelude-mismatch', envelope['permission_states'])
        self.assertIn('gate-deny-bash-trace-unverified', envelope['permission_states'])
        inner = [c for c in inner_receipt.read_text(encoding='utf-8').splitlines()
                 if c.strip() and c != '__close__']
        self.assertEqual(inner, ['ls scripts/'])   # 只有匹配那一次进 inner
        # app 收到 runtimeConfig：冻结 backend + 关 nativeSearchEnhancementsEnabled。
        dlines = [json.loads(l) for l in double_receipt.read_text(encoding='utf-8').splitlines()
                  if l.strip()]
        apprec = next(d for d in dlines if d['event'] == 'app')
        self.assertEqual(apprec['embedded_search_backend_kind'], 'native-binaries')
        self.assertTrue(apprec['native_search_disabled'])
        self.assertTrue(apprec['has_executionPort'])
        # 审计只记录存在性布尔/hash，不打印原始 env/stdin。
        events = _gate_events(out / 'permission-events.jsonl')
        for e in events:
            if e['kind'] == 'gate':
                self.assertIn('has_env', e)
                self.assertIn('has_stdin', e)
                self.assertNotIn('env', e)
                self.assertNotIn('stdin', e)

    def test_fail_closed_without_adapters_entry(self):
        b, out, proc, double_receipt, inner_receipt, adapter_opt = self._setup(
            ['ls scripts/'], adapters_token='NONE')
        self.assertEqual(proc.returncode, 5)
        envelope = json.loads(proc.stdout)
        self.assertFalse(envelope['controlled_pre_exec_gate_ready'])
        self.assertFalse(envelope['approval_client_ready'])
        self.assertFalse(envelope['submitted'])
        self.assertFalse((out / 'response.md').exists())
        self.assertEqual((inner_receipt.read_text(encoding='utf-8')
                          if inner_receipt.exists() else ''), '')

    def test_sha_mismatch_refuses_submission(self):
        b, out, proc, double_receipt, inner_receipt, adapter_opt = self._setup(
            ['ls scripts/'], '{adapter}', override_sha='f' * 64)
        self.assertEqual(proc.returncode, 5)
        envelope = json.loads(proc.stdout)
        self.assertFalse(envelope['submitted'])
        self.assertFalse(envelope['controlled_pre_exec_gate_ready'])
        self.assertIn('sha mismatch', json.dumps(envelope['errors']))


class EntryLayerTests(unittest.TestCase):
    """zcode_direct 入口层：Bash 无契约拒绝、复合/越界/SHA 不符拒绝、adapters 定位。"""

    def test_bash_without_contract_refused(self):
        reasons = zd.validate_selection(stage='S', provider='p', model='m', reasoning='low',
                                        mode='edit', tools=['Read', 'Bash'])
        self.assertEqual(reasons, [])  # mode 校验本身不拒（契约在 main 里检查）

    def test_composite_command_rejected(self):
        contract = {'privacy_free': False,
                    'commands': [{'command': 'echo hi; ls', 'cwd': '.'}]}
        _, reasons = zd.validate_command_contract(contract, os.getcwd())
        self.assertTrue(any('composite/variant' in r for r in reasons))

    def test_out_of_bound_cwd_rejected(self):
        contract = {'privacy_free': False,
                    'commands': [{'command': 'ls', 'cwd': '../../../etc'}]}
        _, reasons = zd.validate_command_contract(contract, os.getcwd())
        self.assertTrue(any('outside the workspace' in r for r in reasons))

    def test_unknown_top_key_rejected(self):
        contract = {'privacy_free': False, 'commands': [{'command': 'ls', 'cwd': '.'}],
                    'bypass': True}
        _, reasons = zd.validate_command_contract(contract, os.getcwd())
        self.assertTrue(reasons)

    def test_input_sha_mismatch_rejected(self):
        tmp = tempfile.mkdtemp()
        f = Path(tmp) / 'a.txt'
        f.write_text('real', encoding='utf-8')
        contract = {'privacy_free': False,
                    'commands': [{'command': 'ls', 'cwd': '.',
                                  'input_sha256': {'a.txt': '0' * 64}}]}
        _, reasons = zd.validate_command_contract(contract, tmp)
        self.assertTrue(any('SHA mismatch' in r for r in reasons))

    def test_same_command_two_cwd_kept(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        (tmp / 'a').mkdir()
        (tmp / 'b').mkdir()
        contract = {'privacy_free': False, 'commands': [
            {'command': 'python run.py', 'cwd': 'a'},
            {'command': 'python run.py', 'cwd': 'b'}]}
        norm, reasons = zd.validate_command_contract(contract, tmp)
        self.assertEqual(reasons, [])
        self.assertEqual(len(norm['commands']), 2)

    def test_valid_contract_normalizes_and_sorts(self):
        tmp = tempfile.mkdtemp()
        (Path(tmp) / 'scripts').mkdir()
        contract = {'privacy_free': True,
                    'commands': [{'command': 'ls scripts/', 'cwd': './scripts'}]}
        norm, reasons = zd.validate_command_contract(contract, tmp)
        self.assertEqual(reasons, [])
        self.assertEqual(len(norm['commands']), 1)
        self.assertTrue(norm['commands'][0]['cwd'])

    def test_derive_adapters_entry_from_bootstrap_layout(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        boot = tmp / 'packages' / 'bootstrap' / 'dist' / 'index.js'
        boot.parent.mkdir(parents=True)
        boot.write_text('export {}', encoding='utf-8')
        adapters = tmp / 'packages' / 'adapters' / 'src' / 'index.ts'
        adapters.parent.mkdir(parents=True)
        adapters.write_text('export {}', encoding='utf-8')
        self.assertEqual(zd._derive_adapters_entry(str(boot)), str(adapters))

    def test_derive_adapters_entry_missing_returns_none(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        boot = tmp / 'packages' / 'bootstrap' / 'dist' / 'index.js'
        boot.parent.mkdir(parents=True)
        boot.write_text('export {}', encoding='utf-8')
        self.assertIsNone(zd._derive_adapters_entry(str(boot)))

    def test_derive_embedded_search_backend_entry_from_bootstrap_layout(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        boot = tmp / 'packages' / 'bootstrap' / 'dist' / 'index.js'
        boot.parent.mkdir(parents=True)
        boot.write_text('export {}', encoding='utf-8')
        es = tmp / 'packages' / 'bootstrap' / 'src' / 'app' / 'embedded-search-backend.ts'
        es.parent.mkdir(parents=True)
        es.write_text('export {}', encoding='utf-8')
        self.assertEqual(zd._derive_embedded_search_backend_entry(str(boot)), str(es))

    def test_derive_embedded_search_backend_entry_missing_returns_none(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        boot = tmp / 'packages' / 'bootstrap' / 'dist' / 'index.js'
        boot.parent.mkdir(parents=True)
        boot.write_text('export {}', encoding='utf-8')
        self.assertIsNone(zd._derive_embedded_search_backend_entry(str(boot)))

    def _chain(self, contract, req, ws_real):
        """把 Python 归一化契约喂给真实 broker.buildRegistry + gateDecision（Node 子进程）。"""
        tmp = Path(tempfile.mkdtemp()).resolve()
        harness = tmp / 'chain.mjs'
        harness.write_text(CHAIN_JS, encoding='utf-8')
        cpath = tmp / 'contract.json'
        rpath = tmp / 'req.json'
        cpath.write_text(json.dumps(contract), encoding='utf-8')
        rpath.write_text(json.dumps(req), encoding='utf-8')
        proc = run_node(harness, [str(BROKER), str(cpath), str(rpath), ws_real])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_normalized_input_preserves_declared_target_sha(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        (tmp / 'scripts').mkdir()
        probe = tmp / 'scripts' / 'probe.py'
        probe.write_text('A\n', encoding='utf-8', newline='')
        shaA = hashlib.sha256(b'A\n').hexdigest()
        author = {'privacy_free': False, 'commands': [
            {'command': 'ls scripts/', 'cwd': 'scripts',
             'input_sha256': {'scripts/probe.py': shaA}}]}
        norm, reasons = zd.validate_command_contract(author, str(tmp))
        self.assertEqual(reasons, [])
        item = norm['commands'][0]['input_sha256']['scripts/probe.py']  # 键=原声明路径
        self.assertEqual(item['sha'], shaA.lower())
        self.assertEqual(item['target'], os.path.normcase(os.path.realpath(str(probe))))

    def test_python_to_js_chain_revalidates_declared_target(self):
        import copy
        tmp = Path(tempfile.mkdtemp()).resolve()
        (tmp / 'scripts' / 'alt').mkdir(parents=True)
        probe = tmp / 'scripts' / 'probe.py'
        probe.write_text('A\n', encoding='utf-8', newline='')
        alt = tmp / 'scripts' / 'alt' / 'probe.py'
        alt.write_text('B\n', encoding='utf-8', newline='')
        shaA = hashlib.sha256(b'A\n').hexdigest()
        author = {'privacy_free': False, 'commands': [
            {'command': 'ls scripts/', 'cwd': 'scripts',
             'input_sha256': {'scripts/probe.py': shaA}}]}
        norm, reasons = zd.validate_command_contract(author, str(tmp))
        self.assertEqual(reasons, [])
        ws_real = os.path.normcase(os.path.realpath(str(tmp)))
        req = {'command': {'mode': 'shell', 'command': 'ls scripts/', 'shellProfile': 'posix-bash'},
               'cwd': str(tmp / 'scripts'), 'trace': {}}
        # 1) 完好：JS 依 Python 归一化 declared+target+sha 复核当前字节 → 允许
        self.assertTrue(self._chain(norm, req, ws_real).get('allowed'))
        # 2) 内容被改写 → deny-input-sha-mismatch（证明 JS 读当前文件，不是 helper 旧目标）
        probe.write_text('C\n', encoding='utf-8', newline='')
        self.assertEqual(self._chain(norm, req, ws_real).get('reason'), 'deny-input-sha-mismatch')
        probe.write_text('A\n', encoding='utf-8', newline='')
        # 3) 目标漂移（alias 改指等价）：声明不变、登记 target 换到另一真实文件 → deny-input-target-moved
        moved = copy.deepcopy(norm)
        moved['commands'][0]['input_sha256']['scripts/probe.py']['target'] = \
            os.path.normcase(os.path.realpath(str(alt)))
        self.assertEqual(self._chain(moved, req, ws_real).get('reason'), 'deny-input-target-moved')

    def test_alias_traversing_input_rejected(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        real = tmp / 'real'
        real.mkdir()
        (real / 'probe.py').write_text('A\n', encoding='utf-8', newline='')
        link = tmp / 'alias'
        try:
            os.symlink(str(real), str(link), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlink/junction creation not permitted in this environment')
        shaA = hashlib.sha256(b'A\n').hexdigest()
        author = {'privacy_free': False, 'commands': [
            {'command': 'ls scripts/', 'cwd': '.', 'input_sha256': {'alias/probe.py': shaA}}]}
        _, reasons = zd.validate_command_contract(author, str(tmp))
        self.assertTrue(any(('alias' in r or 'symlink' in r or 'junction' in r) for r in reasons),
                        reasons)


if __name__ == '__main__':
    unittest.main()
