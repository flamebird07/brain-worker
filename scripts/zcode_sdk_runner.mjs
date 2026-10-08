// ZCode 官方运行时直连执行器：由 scripts/zcode_direct.py 用 JSON 请求文件驱动。
// 只使用 bootstrap 公开 API（startProcessProviderRegistryRuntime / createZCodeApp）；
// 不编译官方源码、不读取凭据内容、不自动登录、不修改桌面模型。
//
// argv（由 Python 组装，不经 shell）：
//   node --import <tsx loader fileURL> zcode_sdk_runner.mjs --request <request.json>
//
// 证据写入 request.output_dir：preflight.json / events.jsonl / result.json / response.md。
// stdout 只打印精简 JSON 信封；事件原文只落盘，不打印。
//
// 清理契约：执行主体在 async main() 内，所有分支用 return 返回退出码，
// finally 真正等待 app.close() / registry.dispose() 完成后才退出；
// 信封只在 main 结束后打印一次，不再用 process.exit 抢在清理前终止。
import { appendFileSync, realpathSync } from 'node:fs';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const argv = process.argv.slice(2);
const requestFlag = argv.indexOf('--request');
const requestPath = requestFlag === -1 ? null : argv[requestFlag + 1];
if (!requestPath) bail('missing --request');

const request = JSON.parse(await readFile(requestPath, 'utf8'));
const outDir = request.output_dir;
const selection = request.selection;
const allowed = new Set(request.allowed_tools || []);
// 受控命令审批注入（可选）：inner 执行端口与清理句柄，finally 里统一关闭。
let gatedExecutionPort;
let closeInnerPort = null;
// 审计行计数与状态分布（供 envelope 的独立状态使用）。
let auditLineCount = 0;
let auditStateCounts = {};

// 官方 runtimeConfig.mode 允许 yolo；本入口一律拒绝构造 yolo。
if (request.mode !== 'plan' && request.mode !== 'edit') bail(`unsupported mode: ${request.mode}`);

// env 独立副本：只注入本机入口配置的显式 environment 与两个官方配置路径。
// 值从 entry config 现读，不写进证据目录，避免代理/账号信息落盘。
const entry = JSON.parse(await readFile(request.entry_config, 'utf8'));
const env = { ...(entry.environment || {}) };
env.ZCODE_BUILTIN_PROVIDER_CONFIG_FILE = request.builtin_provider_config;
env.ZCODE_PERSONAL_PROVIDER_CONFIG_FILE = request.personal_provider_config;

const bootstrap = await import(pathToFileURL(request.bootstrap).href);
if (typeof bootstrap.startProcessProviderRegistryRuntime !== 'function'
  || typeof bootstrap.createZCodeApp !== 'function') {
  bail('bootstrap module lacks startProcessProviderRegistryRuntime/createZCodeApp');
}

const registry = await bootstrap.startProcessProviderRegistryRuntime(env, { standalone: {} });
const service = registry.runtime.registryService;
let app;
let preflight = { stage: request.stage, selection, phase: 'init' };
const envelope = {
  carrier: 'zcode-sdk',
  stage: request.stage,
  provider_requested: selection.providerId,
  model_requested: selection.modelId,
  reasoning_requested: selection.options?.reasoningLevel ?? null,
  mode: request.mode,
  allowed_tools: [...allowed].sort(),
  preflight_only: Boolean(request.preflight_only),
  preflight_ok: false,
  submitted: false,
  ok: false,
  session_id: null,
  turn_id: null,
  status: null,
  model_label: null,
  selection_before_submit: null,
  selection_after_submit: null,
  usage: null,
  free_quota_verified: false,
  event_count: 0,
  response_sha256: null,
  response_bytes: 0,
  observed_tool_catalog: null,
  tool_disallowlist_effective: null,
  serialization_failed: false,
  // 受控命令审批四个独立状态（互不等价，任一都不能被其它推断为真）：
  command_contract_present: Boolean(request.command_contract),
  // tool_visible_bash 依官方事实判定（活 catalog 命中 Bash 且不在 effective 屏蔽内），
  // 不能只看意图 allowed；在取得 catalog/effective 后再置真（见 preflight 计算处）。
  tool_visible_bash: false,
  approval_client_ready: false,
  controlled_pre_exec_gate_ready: false,
  command_actually_executed: false,
  command_executed_receipts: 0,
  command_attempted: 0,
  command_started: 0,
  // REPAIR3：SDK 内部 embedded-search prelude 绑定（只保存非敏感 hash/kind，不含 backend 具体命令串）。
  embedded_search_prelude_bound: false,
  embedded_search_backend_kind: null,
  embedded_search_prelude_sha256: null,
  permission_audit_events: 0,
  permission_states: {},
  errors: [],
  limitations: [],
};

async function main() {
  try {
    const provider = service.getView().providers.find(p => p.providerId === selection.providerId);
    const registryModel = provider?.models.find(m => m.modelId === selection.modelId);
    const validation = service.validateSelection(selection);
    preflight = {
      ...preflight,
      phase: 'registry',
      registry_provider_found: Boolean(provider),
      registry_model_found: Boolean(registryModel),
      registry_validation: validation,
      checked_at_utc: new Date().toISOString(),
    };

    if (request.preflight_only) {
      // 只读 Registry 活视图：不建 App、不建会话、不发 prompt。
      preflight.submit_eligible = Boolean(provider && registryModel && validation?.ok === true);
      preflight.disabled_reason_verified = false;
      preflight.disabled_reason_note =
        'disabledReason 只在 App.listModels() 暴露；preflight-only 不创建 App，因此本检查不能证明可发送，只能证明 Registry 目录里是否存在该选择。';
      envelope.preflight_ok = preflight.submit_eligible;
      envelope.ok = preflight.submit_eligible;
      envelope.limitations.push('preflight-only: App not created, no session, no prompt submitted');
      envelope.limitations.push('disabledReason requires App.listModels()');
      await writeJson('preflight.json', preflight);
      return preflight.submit_eligible ? 0 : 5;
    }

    if (!provider || !registryModel || validation?.ok !== true) {
      refuse('provider/model registry selection is unavailable or invalid');
    }

    // ---- 受控命令审批：注入装饰后的官方 executionPort + permissionBroker ----
    // 只在提供 command_contract 时启用；无受控前置闸门可靠来源即 fail-closed，
    // 不把 approval 能力标为 ready，也不提交需要执行命令的 prompt。
    let injectExecutionPort;
    let injectPermissionBroker;
    let approvalRuntimeConfig = {};
    if (request.command_contract) {
      const approval = await setupCommandApproval(request);
      injectExecutionPort = approval.executionPort;
      injectPermissionBroker = approval.permissionBroker;
      gatedExecutionPort = approval.gatedPort;
      closeInnerPort = approval.closeInnerPort;
      // REPAIR3：官方 Bash handler 硬编码注入 embedded-search prelude，必须把宿主用同 SDK 公开
      // 解析器冻结的 backend 显式传给 runtimeConfig（并关 nativeSearchEnhancementsEnabled → find/grep
      // 关），令 handler 生成的 prelude 与闸门绑定值同源；否则带 prelude 的登记命令仍会被拒。
      approvalRuntimeConfig = approval.runtimeConfig || {};
    }

    app = await bootstrap.createZCodeApp({
      env,
      ...(request.resume_session_id ? { sessionId: request.resume_session_id, resume: true } : {}),
      ...(request.version ? { version: request.version } : {}),
      providerRegistry: service,
      configuredDefaultModelSelection: selection,
      providerRuntimeHeadersPort: registry.providerRuntimeHeadersPort,
      ...(injectExecutionPort ? { executionPort: injectExecutionPort } : {}),
      ...(injectPermissionBroker ? { permissionBroker: injectPermissionBroker } : {}),
      runtimeConfig: {
        workingDirectory: request.workspace,
        mode: request.mode,
        modelSelection: selection,
        modelStreaming: 'on',
        presentationSurface: 'terminal',
        dynamicWorkflowEnabled: false,
        mcp: { enabled: false },
        subagents: { enabled: false },
        memory: { extractionEnabled: false },
        toolDisallowlist: request.tool_disallowlist_base,
        ...approvalRuntimeConfig,
      },
    });
    preflight.session_id = app.sessionId;

    const option = app.listModels().find(o => o.ref?.providerId === selection.providerId
      && o.ref?.modelId === selection.modelId);
    preflight.listmodels_found = Boolean(option);
    preflight.disabled_reason = option?.disabledReason ?? null;
    preflight.reasoning = option?.reasoning ?? null;
    if (request.resume_session_id && app.sessionId !== request.resume_session_id) {
      preflight.resume_mismatch = { requested: request.resume_session_id, actual: app.sessionId };
    }
    if (!option) refuse('target provider/model is absent from App.listModels()');
    if (option.disabledReason) refuse(`target model is not selectable: ${option.disabledReason}`);
    if (preflight.resume_mismatch) {
      refuse(`resume session mismatch: requested ${request.resume_session_id}, actual ${app.sessionId}`);
    }

    await app.setModel(selection);
    const observed = app.getCurrentModelOption()?.ref ?? null;
    preflight.selection_after_set_model = observed;
    if (observed?.providerId !== selection.providerId || observed?.modelId !== selection.modelId) {
      refuse(`selection mismatch after setModel: ${JSON.stringify(observed)}`);
    }
    // node_repl 的注册名是 js；推理档位不符同样禁止提交，不做静默回落。
    if (selection.options?.reasoningLevel && observed?.options?.reasoningLevel
      && observed.options.reasoningLevel !== selection.options.reasoningLevel) {
      refuse(`reasoning level mismatch after setModel: ${observed.options.reasoningLevel}`);
    }

    // 活工具目录：注册表里真实存在的工具全部排除，只留显式授权项，避免遗漏 node_repl 等。
    let catalog = null;
    try {
      catalog = app.runtime?.getToolRegistry?.().list?.() ?? null;
    } catch {
      catalog = null;
    }
    preflight.observed_tool_catalog = catalog;
    preflight.catalog_query_failed = catalog === null;
    const disallow = new Set(request.tool_disallowlist_base);
    for (const name of catalog || []) if (!allowed.has(name)) disallow.add(name);
    const effective = [...disallow].sort();
    preflight.tool_disallowlist_effective = effective;
    envelope.observed_tool_catalog = catalog;
    envelope.tool_disallowlist_effective = effective;
    // H：Bash 是否真的可见 = 活 catalog 命中 Bash 且未被 effective 屏蔽；不据意图 allowed 推断。
    envelope.tool_visible_bash = catalog.includes('Bash') && !effective.includes('Bash');
  if (catalog === null) {
    refuse('live tool registry unavailable; cannot enforce tool allowlist');
    }

    envelope.selection_before_submit = observed;
    envelope.preflight_ok = true;
    await writeJson('preflight.json', preflight);

    const events = [];
    envelope.submitted = true;
    const result = await app.submitPrompt(request.prompt, {
      toolDisallowlist: effective,
      onEvent: event => events.push(event),
    });
    envelope.submitted = true;
    const after = app.getCurrentModelOption()?.ref ?? null;
    envelope.selection_after_submit = after;
    envelope.session_id = app.sessionId;
    envelope.turn_id = result?.turnId ?? null;
    envelope.status = result?.projection?.status ?? null;
    envelope.model_label = app.getModel();
    envelope.usage = usageRecord(result?.usage);
    if (typeof result?.response !== 'string') {
      envelope.errors.push('result lacks a text response');
    } else {
      const bytes = new TextEncoder().encode(result.response);
      await writeFile(join(outDir, 'response.md'), bytes);
      envelope.response_bytes = bytes.length;
      envelope.response_sha256 = await sha256Hex(bytes);
    }
    const selectionKept = after?.providerId === selection.providerId && after?.modelId === selection.modelId;
    const turnEvents = events.filter(e => e.sessionId === app.sessionId && e.turnId === result?.turnId);
    const terminal = turnEvents.findLast(e => e.type === 'turn_complete');
    const requests = turnEvents.filter(e => e.type === 'model_request');
    envelope.model_requests_observed = requests.map(e => ({
      provider: e.payload?.providerId, model: e.payload?.modelId,
    }));
    envelope.terminal_success = terminal?.payload?.resultType === 'success'
      && terminal.payload.response === result?.response
      && !turnEvents.some(e => ['turn_error', 'turn_failed', 'turn_cancelled'].includes(e.type));
    const requestsMatch = requests.length > 0 && requests.every(e =>
      e.payload?.providerId === selection.providerId && e.payload?.modelId === selection.modelId);
    envelope.ok = envelope.terminal_success && requestsMatch
      && ['idle', 'completed'].includes(envelope.status)
      && typeof result?.response === 'string' && selectionKept;
    if (!selectionKept && envelope.submitted) {
      envelope.errors.push(`selection drifted after turn: ${JSON.stringify(after)}`);
    }
    if (!envelope.terminal_success) envelope.errors.push('missing matching successful turn_complete');
    if (!requestsMatch) envelope.errors.push('model_request evidence missing or mismatched');
    if (!['idle', 'completed'].includes(envelope.status)) envelope.errors.push(`turn status ${envelope.status}`);

    await writeJson('result.json', {
      sessionId: app.sessionId,
      turnId: result?.turnId ?? null,
      usage: result?.usage ?? null,
      projection: result?.projection ?? null,
      events_count: events.length,
    });
    await writeFile(join(outDir, 'events.jsonl'),
      events.map(event => safeStringify(event)).join('\n') + '\n');
    envelope.event_count = events.length;
    // 命令实际执行/尝试/started 只来自闸门真实计数；绝不凭协议成功推断（见 finally 里同步）。
    syncApprovalStates();
    await writeJson('preflight.json', preflight);
    return envelope.ok ? 0 : 3;
  } catch (error) {
    envelope.errors.push(error?.message ? error.message : String(error));
    envelope.preflight_ok = envelope.preflight_ok && !envelope.errors.length;
    envelope.ok = false;
    preflight.failed = true;
    preflight.errors = [...envelope.errors];
    try {
      await writeFile(join(outDir, 'runner-error.txt'), `${error?.stack || error}\n`);
    } catch { /* stderr still carries the failure */ }
    await writeJson('preflight.json', preflight);
    return 5;
  } finally {
    // 清理必须真正等待完成：先关 App，再关被装饰的执行端口（连带 inner），最后释放 Registry。
    try { await app?.close?.(); } catch (error) {
      envelope.errors.push(`app.close failed: ${error?.message || error}`);
    }
    try { await gatedExecutionPort?.close?.(); } catch (error) {
      envelope.errors.push(`gated execution port close failed: ${error?.message || error}`);
    }
    try { await closeInnerPort?.(); } catch (error) {
      envelope.errors.push(`inner execution port close failed: ${error?.message || error}`);
    }
    try { registry.dispose(); } catch (error) {
      envelope.errors.push(`registry.dispose failed: ${error?.message || error}`);
    }
    // F：无论成功/异常/审计写失败，都按闸门真实计数同步独立状态；不把已执行谎报为零动作。
    syncApprovalStates();
  }
}

const exitCode = await main();
finish(exitCode);

function usageRecord(usage) {
  if (!usage || typeof usage !== 'object') return null;
  return {
    source: usage.source ?? null,
    model_request_count: usage.modelRequestCount ?? null,
    input_tokens: usage.inputTokens ?? null,
    output_tokens: usage.outputTokens ?? null,
    total_tokens: usage.totalTokens ?? null,
    cache_read_tokens: usage.cacheReadTokens ?? null,
    cache_write_tokens: usage.cacheWriteTokens ?? null,
    reasoning_tokens: usage.reasoningTokens ?? null,
    cache_read_included_in_input: true,
    note: '数值取自本次 result.usage；官方 ModelUsageSummary 的缓存读取已计入输入，不再相加。',
  };
}

function refuse(message) {
  envelope.limitations.push('no prompt submitted: preflight refused, no fallback to another model');
  throw new Error(message);
}

// fail-closed：受控前置闸门不可用时，绝不把 approval 能力标为 ready，也不提交 prompt。
function failClosedApproval(message) {
  envelope.approval_client_ready = false;
  envelope.controlled_pre_exec_gate_ready = false;
  envelope.command_actually_executed = false;
  refuse(`controlled pre-execution gate unavailable; refusing to submit: ${message}`);
}

function makeAuditSink() {
  const path = join(outDir, 'permission-events.jsonl');
  return {
    record(payload) {
      const line = safeStringify(payload);
      try {
        appendFileSync(path, line + '\n');
      } catch (error) {
        // 审计写入失败必须冒泡：门禁不静默通过任何一次放行/拒绝。
        throw new Error(`permission audit write failed: ${error?.message || error}`);
      }
      auditLineCount += 1;
      if (payload && payload.kind === 'approval' && typeof payload.state === 'string') {
        auditStateCounts[payload.state] = (auditStateCounts[payload.state] || 0) + 1;
      } else if (payload && payload.kind === 'gate') {
        const key = payload.allowed ? 'gate-allowed' : `gate-${payload.reason}`;
        auditStateCounts[key] = (auditStateCounts[key] || 0) + 1;
      } else if (payload && payload.kind === 'execution_receipt') {
        // 完成回执按真实 status 分类：只有 completed 计 executed，spawn_error/failed 单列（F）。
        const key = payload.status === 'completed' ? 'executed'
          : `receipt-${payload.status ?? 'unknown'}`;
        auditStateCounts[key] = (auditStateCounts[key] || 0) + 1;
      } else if (payload && payload.kind === 'execution_started') {
        auditStateCounts.started = (auditStateCounts.started || 0) + 1;
      } else if (payload && payload.kind === 'execution_attempt_failed') {
        auditStateCounts.attempt_failed = (auditStateCounts.attempt_failed || 0) + 1;
      }
    },
  };
}

// 解析真实执行端口来源：显式可选 adapters_entry（仅用于定位公共适配器入口，
// 不能覆盖真实 entry config），核对文件可导入且导出 createNodeExecutionAdapter，
// 再构造 inner 端口；任一步失败一律 fail-closed。
async function resolveInnerExecutionPort() {
  if (!request.adapters_entry) {
    failClosedApproval('no adapters entry declared to locate the public execution adapter');
  }
  const url = pathToFileURL(request.adapters_entry).href;
  let adapterModule;
  try {
    adapterModule = await import(url);
  } catch (error) {
    failClosedApproval(`adapters entry import failed: ${error?.message || error}`);
  }
  if (typeof adapterModule?.createNodeExecutionAdapter !== 'function') {
    failClosedApproval('adapters entry does not export createNodeExecutionAdapter');
  }
  // I：inner 构造必须沿用官方 create-app 的 env 语义（processEnv），不无意扩成别的继承环境；
  // 官方默认 `processEnv: options.env ?? process.env`——这里 options.env 即本 runner 的 env。
  const inner = adapterModule.createNodeExecutionAdapter({ processEnv: env, platform: process.platform });
  if (!inner || typeof inner.run !== 'function') {
    failClosedApproval('created inner execution port lacks run()');
  }
  return inner;
}

async function setupCommandApproval(req) {
  const broker = await import(new URL('./zcode_permission_broker.mjs', import.meta.url).href);
  // 独立绑定：JS 侧重算注册表 canonical SHA，与派工前冻结值精确比对，不符即零提交。
  const recomputed = broker.canonicalSha(req.command_contract);
  if (recomputed !== req.command_contract_sha256) {
    failClosedApproval(`command contract sha mismatch: bound ${req.command_contract_sha256} `
      + `vs recomputed ${recomputed}`);
  }
  // A/E：官方 broker request 无 cwd；工作目录上下文用宿主冻结的 workspace realpath 注入，
  // 且注册表 realpath/边界解析也以它为准；realpath 失败即无法确认工作目录 → fail-closed。
  let workspaceReal;
  try {
    workspaceReal = realpathSync(req.workspace);
  } catch (error) {
    failClosedApproval(`workspace realpath unavailable; cannot confirm working directory: `
      + `${error?.message || error}`);
  }
  let registry;
  try {
    registry = broker.buildRegistry(req.command_contract, { workspaceRealPath: workspaceReal });
  } catch (error) {
    failClosedApproval(`command registry build failed: ${error?.message || error}`);
  }
  const inner = await resolveInnerExecutionPort();
  const sink = makeAuditSink();
  // REPAIR3：冻结宿主可信 embedded-search backend 并绑定期望 prelude。解析器不可得时降级为
  // binding=null（带 prelude 的 Bash 命令仍按 deny-extra-exec-channel 拒），但绝不 fail-closed 整个
  // 审批，保留无 prelude 命令的既有行为；可解析时才把冻结 backend 显式回灌 runtimeConfig。
  const es = await freezeEmbeddedSearchPrelude(req, broker);
  const gatedPort = broker.createGatedExecutionPort(inner, registry, sink, es.binding);
  // 真实 runner 把宿主工作目录实际传给 broker（A：不依赖不存在的 request.cwd）。
  const permissionBroker = broker.createPermissionBroker(registry, sink,
    { humanClient: null, trustedWorkingDirectory: workspaceReal });
  // 只有装饰端口确实存在 run 且不含后台通道时才算受控前置闸门就绪。
  const methods = gatedPort._gateMethods();
  if (!methods.includes('run') || methods.some(m =>
    ['start', 'runBashWithBackgroundLifecycle', 'getBackgroundTask',
      'readBackgroundBashOutput', 'cancelBackgroundTask'].includes(m))) {
    failClosedApproval(`gated port method surface is not minimal: ${JSON.stringify(methods)}`);
  }
  envelope.approval_client_ready = true;
  envelope.controlled_pre_exec_gate_ready = true;
  return {
    executionPort: gatedPort,
    permissionBroker,
    gatedPort,
    runtimeConfig: es.runtimeConfig,
    closeInnerPort: async () => {
      // gatedPort.close 已连带关闭 inner；此处仅兜底，不重复抛。
      if (typeof inner.close === 'function') { /* handled by gatedPort.close */ }
    },
  };
}

// 用同一棵公共 SDK 树的公开解析器 resolveDefaultEmbeddedSearchBackend({env}) 取宿主可信 backend，
// 冻结后计算期望 embedded-search prelude 的规范 SHA 并绑定装饰执行端口；同时把冻结 backend 与
// nativeSearchEnhancementsEnabled:false 交回 main 显式写入 runtimeConfig，令官方 Bash handler 生成的
// prelude 与绑定值同源。解析器缺失/导入失败/返回不可用一律降级为无绑定（不 fail-closed 整个审批）。
// 只保存规范 SHA/backend kind/解析器定位等非敏感信息；绝不把整个 env 或账号文件写入证据。
async function freezeEmbeddedSearchPrelude(req, broker) {
  const none = { binding: null, runtimeConfig: {} };
  if (!req.embedded_search_backend_entry) {
    envelope.limitations.push('embedded-search backend resolver not declared; SDK-internal '
      + 'prelude cannot be bound (prelude-bearing Bash will be denied pre-execution)');
    return none;
  }
  const url = pathToFileURL(req.embedded_search_backend_entry).href;
  let mod;
  try {
    mod = await import(url);
  } catch (error) {
    envelope.limitations.push(`embedded-search resolver import failed; no prelude binding: `
      + `${error?.message || error}`);
    return none;
  }
  if (typeof mod?.resolveDefaultEmbeddedSearchBackend !== 'function') {
    envelope.limitations.push('embedded-search resolver lacks resolveDefaultEmbeddedSearchBackend; '
      + 'no prelude binding');
    return none;
  }
  // 解析器只读 env 中指定键（ZCODE_EMBEDDED_SEARCH_COMMAND 及 binary 变量名），不读全量；
  // 这里传入本 runner 的 env 副本，但不落盘其内容。
  let backend;
  try {
    backend = mod.resolveDefaultEmbeddedSearchBackend({ env });
  } catch (error) {
    envelope.limitations.push(`embedded-search resolver threw; no prelude binding: `
      + `${error?.message || error}`);
    return none;
  }
  if (!backend || typeof backend !== 'object' || typeof backend.kind !== 'string') {
    envelope.limitations.push('embedded-search resolver returned no usable backend; no prelude binding');
    return none;
  }
  // 规范成纯 JSON 值（深拷贝），避免后续引用漂移；期望 prelude 与 backend 同源绑定。
  const frozenBackend = JSON.parse(JSON.stringify(backend));
  const expectedPrelude = { kind: 'embedded-search', backend: frozenBackend, findAndGrepEnabled: false };
  const preludeSha256 = broker.canonicalSha(expectedPrelude);
  envelope.embedded_search_prelude_bound = true;
  envelope.embedded_search_backend_kind = frozenBackend.kind;
  envelope.embedded_search_prelude_sha256 = preludeSha256;
  preflight.embedded_search_binding = {
    prelude_sha256: preludeSha256,
    backend_kind: frozenBackend.kind,
    expected_find_and_grep_enabled: false,
    resolver_ref: 'bootstrap/src/app/embedded-search-backend.ts#resolveDefaultEmbeddedSearchBackend',
  };
  return {
    binding: { preludeSha256 },
    runtimeConfig: {
      embeddedSearchBackend: frozenBackend,
      nativeSearchEnhancementsEnabled: false,
    },
  };
}

// 按闸门真实计数同步独立状态（executed 只等于完成回执；attempted/started 单列）。
// 在成功、异常与 finally 路径都调用，确保审计写失败/抛错也不谎报为零动作（F）。
function syncApprovalStates() {
  if (!request.command_contract || !gatedExecutionPort) return;
  envelope.command_actually_executed = gatedExecutionPort._executedCount() > 0;
  envelope.command_executed_receipts = gatedExecutionPort._executedCount();
  envelope.command_attempted = gatedExecutionPort._attemptedCount();
  // started 单独同步：即便 execution_started 审计写失败抛出，闸门已在事件里先计数，
  // 这里仍如实透传观测到的启动数（不把 failed/timed_out/cancelled 的实际启动说成零动作）。
  envelope.command_started = gatedExecutionPort._startedCount();
  envelope.permission_audit_events = auditLineCount;
  envelope.permission_states = auditStateCounts;
}

function bail(message) {
  // 只在 envelope/registry 尚未建立的参数阶段使用；此处没有任何需要清理的资源。
  process.stderr.write(`zcode_sdk_runner: ${message}\n`);
  process.exit(2);
}

async function writeJson(name, value) {
  let text;
  try {
    text = JSON.stringify(value, null, 1) + '\n';
  } catch (error) {
    // 序列化失败必须留证据并判失败，不写成空文件掩盖。
    envelope.serialization_failed = true;
    envelope.errors.push(`serializing ${name} failed: ${error?.message || error}`);
    await writeFile(join(outDir, 'serialization-error.txt'),
      `failed to serialize ${name}: ${error?.stack || error}\n`);
    return;
  }
  await writeFile(join(outDir, name), text);
}

function safeStringify(value) {
  try {
    return JSON.stringify(value);
  } catch (error) {
    envelope.serialization_failed = true;
    envelope.errors.push(`serializing an event payload failed: ${error?.message || error}`);
    return JSON.stringify({ serialization_failed: true, reason: String(error?.message || error) });
  }
}

async function sha256Hex(bytes) {
  const { createHash } = await import('node:crypto');
  return createHash('sha256').update(bytes).digest('hex');
}

function finish(code) {
  // main() 与 finally 清理全部结束后才走到这里；信封只打印这一次。
  let text;
  try {
    text = JSON.stringify(envelope) + '\n';
  } catch (error) {
    process.stderr.write(`zcode_sdk_runner: envelope serialization failed: ${error?.message || error}\n`);
    process.exit(6);
  }
  process.stdout.write(text);
  process.exit(envelope.serialization_failed ? 6 : code);
}
