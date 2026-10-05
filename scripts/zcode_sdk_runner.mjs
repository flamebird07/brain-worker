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
    app = await bootstrap.createZCodeApp({
      env,
      ...(request.resume_session_id ? { sessionId: request.resume_session_id, resume: true } : {}),
      ...(request.version ? { version: request.version } : {}),
      providerRegistry: service,
      configuredDefaultModelSelection: selection,
      providerRuntimeHeadersPort: registry.providerRuntimeHeadersPort,
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
    // 清理必须真正等待完成：先关 App 再释放 Registry，异常只记录不吞退出码。
    try { await app?.close?.(); } catch (error) {
      envelope.errors.push(`app.close failed: ${error?.message || error}`);
    }
    try { registry.dispose(); } catch (error) {
      envelope.errors.push(`registry.dispose failed: ${error?.message || error}`);
    }
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
