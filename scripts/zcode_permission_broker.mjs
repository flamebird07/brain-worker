// ZCode 受控命令审批与执行前置门禁（供 zcode_sdk_runner.mjs 注入官方
// createZCodeApp({ executionPort, permissionBroker })）。
//
// 只用官方公开的端口形状（contracts/interfaces/execution.port.ts 与
// PermissionBrokerRequest），不 monkey-patch、不修改官方运行时。
//
// 官方事实（core/src/tool/handlers/bash.ts 与 permission-flow.ts）：
// - 只读 Bash（resolveBashPermissionCapability 令 needsApproval=false）跳过 permissionBroker，
//   但仍一律经 `executionPort.run(request, options)`；request.command =
//   {mode:'shell', command:<逐字>, shellProfile:'posix-bash', shellOverride?}，request.cwd =
//   官方 resolveToolWorkingDirectory 解析出的绝对工作目录。因此唯一可靠的“禁止未登记命令”闸门
//   是被装饰的 executionPort，在派生子进程之前逐字比较 command 与真实 cwd（并 realpath 复核）。
// - PermissionBrokerRequest（permission-flow.ts 构造）不含 cwd：只有 input/mode/reason/requestId/
//   riskLevel/ruleId/sessionId/sideEffectScope/toolCallId/toolName/traceId/turnId 等。故 broker 的
//   工作目录上下文必须由宿主（runner）冻结并注入 trustedWorkingDirectory，绝不能依赖不存在的
//   request.cwd/input.workdir；无法确认工作目录即拒绝（deny no-working-directory）。
// - 后台：显式 run_in_background 在端口无 runBashWithBackgroundLifecycle 时由 core 抛
//   ConfigurationError（硬错误）；而“超时自动转后台”在无后台方法时**不报错**，回落到前台
//   executionPort.run（仍过本闸门）。装饰端口不暴露任何后台方法，故 supportsBashBackgroundLifecycle
//   为假，后台通道不可用；这不是把“全部后台”都变成错误，需要据源码描述。
//
// 设计边界：
// - 精确注册命令 + realpath 后精确 cwd 才放行；trim/改写/拼接/复合（; && || | 重定向 换行）/cd 前缀/
//   wrapper 追加/argv 通道/错误 cwd/额外 env|stdin|sandbox 关闭一律拒绝。官方 Bash 内部 embedded-search
//   bashPrelude 仅当逐字节等于宿主用同 SDK 公开解析器冻结的期望值且为真实 Bash trace 时才放行，
//   否则（无绑定/形状或 backend 不符/非 Bash/缺 trace）一律拒绝，inner 不启动。
// - 声明式 input+SHA 是“代码/测试输入范围”的证明，**不是操作系统文件沙箱**：闸门在委派 inner 之前
//   真实读取当前输入文件、再复核 realpath/工作区边界/SHA，缺失/被替换/不一致不启动 inner。
// - 审批绝不改写 input、绝不下发 permissionUpdates/sessionPermissionUpdates；已登记精确命令默认无需
//   等待人工 UI 即放行，但**唯一胜者语义**（claimResponse 至多 claim 一次，输掉/abort/超时/迟到回复
//   都不产生 allow 或持久/会话授权）对自动与人工分支同样适用。
// - 缺审批客户端 / 规则拒绝 / 审批超时 / 取消 / 竞速败 / 无工作目录上下文 各返回独立状态，绝不归类为
//   GLM API/quota。
// - 审计写入失败必须抛出（不静默通过）；无“完成回执”不得声称 executed=true；spawn_error 不计入执行数；
//   执行后审计失败无法撤销已发生执行——计数在写审计前先落地，异常/汇总路径仍如实反映 attempted/started。
// - 原始命令文本只在**登记且匹配**（matched）且 privacy_free 时留证；未登记/异常输入只留 SHA-256。
import { createHash } from 'node:crypto';
import { readFileSync, realpathSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';

// 复合/变体特征：登记阶段即拒绝声明这些命令；执行阶段逐字相等同样不会匹配。
const COMPOSITE_MARKERS = [';', '&&', '||', '|', '\n', '\r', '>>', '>', '<'];
const IS_WIN = process.platform === 'win32';

function sha256Hex(text) {
  return createHash('sha256').update(typeof text === 'string' ? text
    : Buffer.from(text ?? '', 'utf8')).digest('hex');
}

// 与 Python json.dumps(obj, sort_keys=True, separators=(',',':'), ensure_ascii=False)
// 对齐的确定性规范化：递归排序键、无空格的 JSON（字符串用标准转义）。
export function canonicalize(value) {
  if (Array.isArray(value)) {
    return '[' + value.map(canonicalize).join(',') + ']';
  }
  if (value && typeof value === 'object') {
    const keys = Object.keys(value).sort();
    return '{' + keys.map(k => JSON.stringify(k) + ':' + canonicalize(value[k])).join(',') + '}';
  }
  return JSON.stringify(value);
}

export function canonicalSha(contract) {
  return sha256Hex(canonicalize(contract));
}

// 比较键：Windows 折叠大小写/分隔符（normcase 语义仅用于 Windows），POSIX 保留大小写；
// 先 realpath（在调用处完成）消除 symlink/junction 别名与 `..`，这里只做末分隔符归一。
function pathKey(p) {
  let s = String(p ?? '');
  if (IS_WIN) s = s.replace(/[\\/]+/g, '\\').toLowerCase();
  else s = s.replace(/\/{2,}/g, '/');
  if (s.length > 1) s = s.replace(/[\\/]+$/, '');
  return s;
}

function isWithin(rootKey, key) {
  if (key === rootKey) return true;
  return key.startsWith(rootKey + (IS_WIN ? '\\' : '/'));
}

// realpath 解析 target（绝对或相对 workspace）并断言仍在工作区内；否则 ok:false + 原因。
// realpathSync 会解析 symlink/junction，故别名目标改指工作区外也会被拒（E）。
function resolveRealWithin(target, workspaceReal) {
  if (typeof target !== 'string' || target === '') return { ok: false, reason: 'path-missing' };
  const abs = isAbsolute(target) ? target : resolve(workspaceReal, target);
  let real;
  try {
    real = realpathSync(abs);
  } catch {
    return { ok: false, reason: 'path-missing' };
  }
  const rootKey = pathKey(workspaceReal);
  const key = pathKey(real);
  if (!isWithin(rootKey, key)) return { ok: false, reason: 'outside-workspace' };
  return { ok: true, real, key };
}

// 声明路径是否穿越 symlink/junction/reparse 别名：path.resolve 只折叠 `.`/`..` 不解析链接，
// realpathSync 会解析——两者规范化后不相等即说明某个组件是别名（登记期保守拒绝用）。
// 若最终文件缺失（realpathSync 抛错）则返回 null，交由存在性/SHA 校验处理。
function aliasInPath(declared, workspaceReal) {
  const abs = isAbsolute(declared) ? declared : resolve(workspaceReal, declared);
  let lex;
  try {
    lex = resolve(abs);
  } catch {
    return null;
  }
  let real;
  try {
    real = realpathSync(abs);
  } catch {
    return null;
  }
  const lk = pathKey(lex);
  const rk = pathKey(real);
  return lk === rk ? null : `${lk}->${rk}`;
}

// 真实 Bash trace：官方 Bash handler 在 request.trace 携带非空 sessionId/turnId、
// attributes.toolCallId，且 attributes.toolName==='Bash'。仅当这些都在且为 Bash 时，才允许
// 携带 SDK 内部 embedded-search prelude 的执行通过（embedded-search prelude 只准 Bash 内部来源）。
function genuineBashTrace(trace) {
  const attrs = trace?.attributes ?? {};
  const nonEmpty = (v) => typeof v === 'string' && v.length > 0;
  return nonEmpty(trace?.sessionId) && nonEmpty(trace?.turnId)
    && nonEmpty(attrs.toolCallId) && attrs.toolName === 'Bash';
}

// 期望 prelude 的规范 SHA 绑定：由 runner 用同 SDK 公开解析器冻结的 backend 计算，逐字
// 比较整个 prelude（kind + backend 全部字段 + findAndGrepEnabled），任何差异即拒绝——不是只
// 比较 kind，更不从模型传来的 prelude 反向登记。
export function preludeSha(prelude) {
  return sha256Hex(canonicalize(prelude));
}

// 构建冻结内存注册表：byCommand = Map(command -> Map(cwdKey -> record))，
// 每个 (command,cwd) 记录独立携带自己的 input_sha256（不因同命令多 cwd 而丢弃后续输入）。
// contract.commands 已由派工前（zcode_direct.validate_command_contract）校验：单条逐字命令、
// cwd 绝对且在工作区内、input_sha256 为 64 位 hex 且与登记时字节一致。
export function buildRegistry(contract, { workspaceRealPath } = {}) {
  if (!workspaceRealPath) throw new Error('buildRegistry requires workspaceRealPath context');
  let wsReal;
  try {
    wsReal = realpathSync(workspaceRealPath);
  } catch (error) {
    throw new Error(`workspace realpath unavailable: ${error?.message || error}`);
  }
  const byCommand = new Map();
  for (const entry of contract.commands) {
    const cmd = entry.command;
    const cwdResolved = resolveRealWithin(entry.cwd, wsReal);
    if (!cwdResolved.ok) {
      throw new Error(`registered cwd ${entry.cwd} unresolvable/outside workspace`);
    }
    const inputEntries = Object.freeze(Object.entries(entry.input_sha256 || {}).map(([declared, rec]) => {
      // 归一化输入形状：{ "<原声明路径>": { target: "<登记物理目标>", sha: "<64hex>" } }——
      // 保留原始声明路径（执行期重新解析）与登记物理目标（比对重绑定），不是只存旧目标。
      if (!rec || typeof rec !== 'object' || typeof rec.sha !== 'string'
        || !/^[0-9a-fA-F]{64}$/.test(rec.sha) || typeof rec.target !== 'string'
        || rec.target === '') {
        throw new Error(`registered input ${declared} must be {target, sha64}`);
      }
      // 登记期保守拒绝：声明路径不得穿越别名（symlink/junction），否则路径绑定不可信。
      const alias = aliasInPath(declared, wsReal);
      if (alias) throw new Error(`registered input ${declared} traverses a symlink/junction (${alias})`);
      // 登记物理目标必须仍在工作区内（不重绑）；记下 targetKey 供执行期与实时解析比对。
      const tr = resolveRealWithin(rec.target, wsReal);
      if (!tr.ok) throw new Error(`registered input ${declared} target unresolvable/outside workspace`);
      return Object.freeze({ declared, targetKey: pathKey(rec.target), sha: rec.sha.toLowerCase() });
    }));
    const record = Object.freeze({
      command: cmd,
      command_sha256: sha256Hex(cmd),
      cwdKey: cwdResolved.key,
      inputEntries,
      variant: COMPOSITE_MARKERS.some(m => String(cmd).includes(m)),
    });
    if (!byCommand.has(cmd)) byCommand.set(cmd, new Map());
    const perCwd = byCommand.get(cmd);
    if (perCwd.has(record.cwdKey)) throw new Error(`duplicate command+cwd for ${cmd}`);
    perCwd.set(record.cwdKey, record);
  }
  return Object.freeze({
    privacyFree: contract.privacy_free === true,
    byCommand,
    count: byCommand.size,
    canonical_sha256: canonicalSha(contract),
    workspaceReal: wsReal,
  });
}

// 前置执行闸门：逐字比较 command 与真实 cwd（realpath+边界），并核验声明式 input+SHA。
// binding（可选）承载宿主冻结的期望 embedded-search prelude 规范 SHA；无 binding 时任何
// bashPrelude 一律拒绝（维持既有语义）。
// 返回 {allowed, reason, matched, cwdKey} —— reason 为稳定分类标签，绝不猜成成功。
export function gateDecision(request, registry, binding = null) {
  const deny = (reason) => ({ allowed: false, reason, matched: false });
  const command = request?.command;
  if (!command || command.mode !== 'shell') return deny('deny-channel-not-shell');
  if (command.shellProfile !== 'posix-bash') return deny('deny-shell-profile-unexpected');
  const raw = command.command;
  if (typeof raw !== 'string') return deny('deny-command-not-string');
  if (COMPOSITE_MARKERS.some(m => raw.includes(m))) return deny('deny-composite-or-variant');
  // 可信官方派生 shell 字段需限定：仅接受 posix/git-bash 方言（拒绝 cmd/legacy-shell 语义漂移）。
  if (command.shellOverride) {
    const dialect = command.shellOverride.dialect;
    if (dialect !== 'posix' && dialect !== 'git-bash') return deny('deny-shell-profile-unexpected');
  }
  // 额外 env overlay / stdin 一律拒绝（注册命令不得因此改变执行语义，G）。
  if (request.env || request.stdin !== undefined) {
    return deny('deny-extra-exec-channel');
  }
  // REPAIR3：官方 Bash handler 会硬编码注入 typed embedded-search bashPrelude
  // （shouldInjectEmbeddedSearchBashPrelude()===true，见 core/src/embedded-search/shell.ts），
  // 因此合法登记的 Bash 命令也会带该 prelude——旧闸门把 bashPrelude 与 env/stdin 同列一律拒绝，
  // 导致已登记命令也无法执行。此处只在**该 prelude 逐字节等于宿主用同 SDK 公开解析器冻结的
  // 期望值**（backend 全部字段与 findAndGrepEnabled 精确一致，无额外/缺失字段）、且请求确为**真实
  // Bash trace** 时放行；未绑定/形状或 backend 不符/非 Bash/缺 trace 一律拒绝，inner 不启动。
  // captureCwdAfterSuccess 是官方内部字段（adapter 内部追加保存并 pwd -P 至临时文件、保留退出码，
  // 通常不增加 stdout；命令入口仍是原命令），闸门不因其存在而拒绝，也不据此改判复合命令。
  if (request.bashPrelude !== undefined) {
    if (!binding || typeof binding.preludeSha256 !== 'string') {
      return deny('deny-extra-exec-channel');
    }
    if (!genuineBashTrace(request.trace)) {
      return deny('deny-bash-trace-unverified');
    }
    if (preludeSha(request.bashPrelude) !== binding.preludeSha256) {
      return deny('deny-bash-prelude-mismatch');
    }
  }
  if (request.sandbox && (request.sandbox.enabled === false
    || request.sandbox.dangerouslyDisableSandbox === true)) {
    return deny('deny-sandbox-disabled');
  }
  const perCwd = registry.byCommand.get(raw);
  if (!perCwd) return deny('deny-unregistered-command');
  const cwdResolved = resolveRealWithin(request?.cwd, registry.workspaceReal);
  if (!cwdResolved.ok) {
    return deny(cwdResolved.reason === 'outside-workspace'
      ? 'deny-cwd-outside-workspace' : 'deny-cwd-missing');
  }
  const record = perCwd.get(cwdResolved.key);
  if (!record) return deny('deny-cwd-mismatch');
  // C/E：委派 inner 之前，用**原始声明路径**实时重新解析、比对登记物理目标（重绑定即拒），
  // 再读当前字节复核 SHA；缺失/被替换/目标漂移一律不启动 inner。
  for (const rec of record.inputEntries) {
    const live = resolveRealWithin(rec.declared, registry.workspaceReal);
    if (!live.ok) {
      return deny(live.reason === 'outside-workspace'
        ? 'deny-input-path-unresolved' : 'deny-input-missing');
    }
    if (live.key !== rec.targetKey) return deny('deny-input-target-moved');
    let buf;
    try {
      buf = readFileSync(live.real);
    } catch {
      return deny('deny-input-missing');
    }
    if (sha256Hex(buf) !== rec.sha) return deny('deny-input-sha-mismatch');
  }
  return { allowed: true, reason: 'allow-registered-exact', matched: true, cwdKey: cwdResolved.key };
}

function isoNow() {
  return new Date().toISOString();
}

// 拒绝结果必须用官方有效形状：ExecutionFailureType 不含 'denied'，此处用 'unknown' 承载策略拒绝，
// 绝不谎报为 spawn/timeout/sandbox；stderr.bytes 为真实字节数。
function deniedResult(reason, started) {
  const now = new Date();
  const text = `command refused by controlled pre-execution gate: ${reason}\n`;
  return {
    status: 'failed',
    exitCode: undefined,
    signal: undefined,
    stdout: { text: '', bytes: 0, truncated: false },
    stderr: { text, bytes: Buffer.byteLength(text, 'utf8'), truncated: false },
    durationMs: Math.max(0, now.getTime() - started.getTime()),
    timedOut: false,
    cancelled: false,
    startedAt: started,
    completedAt: now,
    error: { type: 'unknown', message: `controlled pre-execution gate denied (${reason})` },
  };
}

// 审计 sink：任何写入失败必须抛出（不静默通过）。
function auditOrThrow(sink, payload) {
  if (!sink || typeof sink.record !== 'function') {
    throw new Error('permission audit sink is required; refusing to run without an auditable gate');
  }
  sink.record(payload); // sink.record 抛错则向上冒泡，门禁不吞。
}

// D：原始命令文本只在“登记且匹配 + privacy_free”时留证；未登记/异常输入只留 SHA。
function redactCommand(command, privacyFree, matched) {
  const digest = sha256Hex(command);
  if (privacyFree && matched) return { command_sha256: digest, command };
  return { command_sha256: digest };
}

// 被装饰的执行端口：先过闸门，再委托真实 innerPort.run，并按官方形状留真实回执。
// binding（可选）= { preludeSha256 }：宿主冻结的期望 embedded-search prelude 规范 SHA。
export function createGatedExecutionPort(innerPort, registry, sink, binding = null) {
  if (!innerPort || typeof innerPort.run !== 'function') {
    throw new Error('gated execution port requires a real inner ExecutionPort.run');
  }
  // 独立计数：attempted（放行且进入 inner）、started（观测到基础端口 started 事件）、
  // completed（status==='completed' 完成回执）。executed 只等于 completed，绝不把 spawn_error
  // 或未回执算作已执行（F）。
  let attempted = 0;
  let startedEvents = 0;
  let completedReceipts = 0;
  const gated = {
    // 只提供 run/close：不提供 runBashWithBackgroundLifecycle / start / getBackgroundTask /
    // readBackgroundBashOutput / cancelBackgroundTask；官方 supportsBashBackgroundLifecycle 因此为假。
    async run(request, options) {
      const started = new Date();
      const decision = gateDecision(request, registry, binding);
      const trace = request?.trace || {};
      const assoc = {
        sessionId: trace.sessionId ?? null,
        turnId: trace.turnId ?? null,
        toolCallId: trace.attributes?.toolCallId ?? null,
      };
      const audit = {
        kind: 'gate',
        at_utc: started.toISOString(),
        allowed: decision.allowed,
        reason: decision.reason,
        ...assoc,
        cwd: request?.cwd ?? null,
        cwd_key: decision.matched ? decision.cwdKey : null,
        // 只记录“哪些执行字段存在”的布尔/结构 hash，绝不打印原始 env/stdin 内容。
        tool_name: trace.attributes?.toolName ?? null,
        has_env: Boolean(request?.env),
        has_stdin: request?.stdin !== undefined,
        has_bash_prelude: request?.bashPrelude !== undefined,
        capture_cwd_after_success: request?.captureCwdAfterSuccess === true,
      };
      // 带 prelude 但未放行时，记录其规范 SHA（结构性 hash，不含 backend 具体命令串之外敏感信息）
      // 供审计定位；放行则无需（已绑定期望值）。
      if (request?.bashPrelude !== undefined && !decision.allowed) {
        audit.bash_prelude_sha256 = preludeSha(request.bashPrelude);
      }
      if (typeof request?.command?.command === 'string') {
        Object.assign(audit, redactCommand(request.command.command,
          registry.privacyFree, decision.matched));
      }
      if (decision.allowed && request?.command?.shellOverride) {
        audit.shell_dialect = request.command.shellOverride.dialect ?? null;
        audit.shell_id = request.command.shellOverride.id ?? null;
      }
      auditOrThrow(sink, audit);
      if (!decision.allowed) {
        return deniedResult(decision.reason, started);
      }
      attempted += 1;
      const userOnEvent = typeof options?.onEvent === 'function' ? options.onEvent : null;
      const runOptions = {
        ...options,
        onEvent: (event) => {
          if (event?.type === 'started') {
            startedEvents += 1;
            auditOrThrow(sink, { kind: 'execution_started', at_utc: isoNow(),
              pid: event.pid ?? null, ...assoc });
          }
          return userOnEvent ? userOnEvent(event) : undefined;
        },
      };
      // 转发原始 request（不改写、不追加 shell/env/sandbox），inner 语义与官方一致。
      let result;
      try {
        result = await innerPort.run(request, runOptions);
      } catch (error) {
        // 执行已尝试但抛错：记录 attempt 事实（不能撤销已发生），计数不落 completed。
        auditOrThrow(sink, { kind: 'execution_attempt_failed', at_utc: isoNow(),
          error: String(error?.message || error), ...assoc });
        throw error;
      }
      const status = result?.status ?? null;
      if (status === 'completed') completedReceipts += 1;
      const stdout = result?.stdout ?? { text: '', bytes: 0 };
      const stderr = result?.stderr ?? { text: '', bytes: 0 };
      // 完成回执先落地计数再写审计：审计失败不能抹掉真实执行事实（F）。
      auditOrThrow(sink, {
        kind: 'execution_receipt',
        at_utc: isoNow(),
        status,
        exitCode: result?.exitCode ?? null,
        signal: result?.signal ?? null,
        started: startedEvents > 0,
        timedOut: Boolean(result?.timedOut),
        cancelled: Boolean(result?.cancelled),
        stdout_sha256: sha256Hex(stdout.text ?? ''),
        stderr_sha256: sha256Hex(stderr.text ?? ''),
        ...assoc,
      });
      return result;
    },
    async close() {
      if (typeof innerPort.close === 'function') await innerPort.close();
    },
    // 供 runner 汇总的只读计数（不暴露为端口方法，避免后台能力探测）。
    _executedCount: () => completedReceipts,
    _attemptedCount: () => attempted,
    _startedCount: () => startedEvents,
    _gateMethods: () => Object.keys(gated).filter(k => !k.startsWith('_')),
  };
  return gated;
}

// 权限 broker（ask 路径）：官方 request 无 cwd，故用宿主冻结的 trustedWorkingDirectory；
// 唯一胜者（claim 至多一次）；独立分类缺客户端/规则拒绝/超时/取消/竞速败/无工作目录。
// 绝不修改 input、绝不下发 permissionUpdates/sessionPermissionUpdates。
export function createPermissionBroker(registry, sink,
  { humanClient = null, trustedWorkingDirectory = null } = {}) {
  async function requestPermission(request, options = {}) {
    const started = new Date();
    const command = request?.input?.command;
    const base = {
      kind: 'approval',
      at_utc: started.toISOString(),
      requestId: request?.requestId ?? null,
      sessionId: request?.sessionId ?? null,
      turnId: request?.turnId ?? null,
      toolCallId: request?.toolCallId ?? null,
      toolName: request?.toolName ?? null,
      claim_supported: typeof options.claimResponse === 'function',
    };
    const finish = (state, decision, reason, extra = {}) => {
      const audit = { ...base, state, decision, reason };
      if (typeof command === 'string') {
        Object.assign(audit, redactCommand(command, registry.privacyFree, decision === 'allow'));
      }
      auditOrThrow(sink, audit);
      return { decision, reason: `${state}: ${reason}`, ...extra };
    };

    // 取消优先：调用方 abort 已置位 → cancelled（独立状态，非 quota/API）。
    if (options.signal && options.signal.aborted) {
      return finish('cancelled', 'deny', 'approval-cancelled-by-caller');
    }
    // A：官方 PermissionBrokerRequest 无 cwd；工作目录上下文必须由宿主注入并确认。
    if (!trustedWorkingDirectory) {
      return finish('no-working-directory', 'deny', 'working-directory-context-unconfirmed');
    }
    // 3：审批仅约束 Bash 工具——toolName 必须严格等于 'Bash'；缺失或其它工具（如 Write）即便
    // 携带同一个已登记的 input.command，也一律规则拒绝，绝不把命令执行权限授予别的工具，
    // 且该判定在 claim 与 allow 之前完成。
    if (request?.toolName !== 'Bash') {
      return finish('rule-denied', 'deny', 'deny-tool-not-bash');
    }
    // 匹配：broker 只判定“是否为登记的确切命令”（精确 cwd 由执行闸门权威强制）。
    if (typeof command !== 'string' || command === '') {
      return finish('rule-denied', 'deny', 'deny-command-not-string');
    }
    if (COMPOSITE_MARKERS.some(m => command.includes(m))) {
      return finish('rule-denied', 'deny', 'deny-composite-or-variant');
    }
    if (!registry.byCommand.has(command)) {
      return finish('rule-denied', 'deny', 'deny-unregistered-command');
    }
    // B：唯一胜者——claim 至多一次；输掉即 race-lost（自动与人工分支同样适用），绝不 allow。
    if (typeof options.claimResponse === 'function' && options.claimResponse() !== true) {
      return finish('race-lost', 'deny', 'another-responder-claimed-first');
    }
    // 已登记精确命令且无客户端：默认无需等待人工 UI 放行（不授予持久/会话 always-allow）。
    if (!humanClient) {
      return finish('approved-registered', 'allow', 'exact-registered-command');
    }
    // 有客户端：遵循 signal / 正数 timeoutMs（缺省不制造 0ms 假超时）/ claim；清理 listener+timer。
    let timer = null;
    let onAbort = null;
    try {
      const outcome = await new Promise((resolvePromise, rejectPromise) => {
        const cleanup = () => {
          if (timer) { clearTimeout(timer); timer = null; }
          if (onAbort && options.signal) options.signal.removeEventListener?.('abort', onAbort);
        };
        const settle = (fn) => { cleanup(); fn(); };
        const ms = Number.isFinite(options.timeoutMs) && options.timeoutMs > 0
          ? options.timeoutMs : null;
        if (ms !== null) {
          timer = setTimeout(() => settle(() => rejectPromise(
            Object.assign(new Error('approval request timed out'),
              { permissionState: 'approval-timeout' }))), ms);
        }
        onAbort = () => settle(() => rejectPromise(
          Object.assign(new Error('approval cancelled'), { permissionState: 'cancelled' })));
        if (options.signal) {
          if (options.signal.aborted) {
            return settle(() => rejectPromise(
              Object.assign(new Error('approval cancelled'), { permissionState: 'cancelled' })));
          }
          options.signal.addEventListener?.('abort', onAbort, { once: true });
        }
        Promise.resolve()
          .then(() => humanClient(request, options))
          .then((allowed) => settle(() => resolvePromise(allowed)))
          .catch((error) => settle(() => rejectPromise(error)));
      });
      if (outcome === true) return finish('approved', 'allow', 'human-approved');
      return finish('rule-denied', 'deny', 'human-denied');
    } catch (error) {
      const message = error?.message || String(error);
      const state = error?.permissionState
        || (/no permission client available/i.test(message) ? 'client-missing' : 'client-error');
      return finish(state, 'deny', message);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
  return { requestPermission };
}

export const BROKER_STATE_FIELDS = ['approved', 'approved-registered', 'rule-denied',
  'cancelled', 'approval-timeout', 'client-missing', 'race-lost', 'client-error',
  'no-working-directory'];
