'use strict';

/**
 * 被动传输阶段观测（默认关闭）。
 *
 * 只订阅 Undici 官方 diagnostics_channel，不包装/改写 fetch、https、dispatcher、
 * 重试或 socket；不读取 request/response 的 headers、body、trailers 内容。
 * 仅在显式 env `BRAIN_WORKER_CLAUDE_TRACE_FILE` 指向绝对路径时启用，独占创建新文件；
 * 未设 env 时零订阅、零写、零副作用。任何诊断异常都被吞掉，绝不影响 CLI。
 *
 * 观测能力边界：连接事件本身不是请求关联；只有 `undici:client:sendHeaders`
 * 同时给出 request 与 socket 时才把两者关联，socket 的实际使用次数是复用证据，
 * 不是按“第几次请求”推断出来的。
 */

const diagnostics_channel = require('diagnostics_channel');
const fs = require('fs');
const path = require('path');

const TRACE_ENV = 'BRAIN_WORKER_CLAUDE_TRACE_FILE';

const ALLOWED_HOST = 'api.deepseek.com';
const ALLOWED_PROTOCOLS = new Set(['https:', '']);
const ALLOWED_PORTS = new Set(['443', '']);
const ALLOWED_PATHS = new Set([
  '/anthropic/v1/messages',
  '/anthropic/v1/messages/count_tokens',
]);
const ALLOWED_METHODS = new Set(['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']);

const ERROR_CODES = new Set([
  'ECONNRESET',
  'ECONNREFUSED',
  'ETIMEDOUT',
  'EPIPE',
  'ENOTFOUND',
  'EAI_AGAIN',
  'UND_ERR_CONNECT_TIMEOUT',
  'UND_ERR_SOCKET',
  'UND_ERR_HEADERS_TIMEOUT',
  'UND_ERR_BODY_TIMEOUT',
  'ABORT_ERR',
]);
const SYSCALLS = new Set(['connect', 'read', 'write', 'getaddrinfo']);
const MAX_CAUSE_DEPTH = 3;

const DEFAULT_MAX_RECORDS = 1000;
const DEFAULT_MAX_BYTES = 256 * 1024;

const CHANNELS = Object.freeze({
  requestCreate: 'undici:request:create',
  requestBodySent: 'undici:request:bodySent',
  requestHeaders: 'undici:request:headers',
  requestTrailers: 'undici:request:trailers',
  requestError: 'undici:request:error',
  clientBeforeConnect: 'undici:client:beforeConnect',
  clientConnected: 'undici:client:connected',
  clientConnectError: 'undici:client:connectError',
  clientSendHeaders: 'undici:client:sendHeaders',
});

const EVENT_NAMES = Object.freeze({
  [CHANNELS.requestCreate]: 'request:create',
  [CHANNELS.requestBodySent]: 'request:bodySent',
  [CHANNELS.requestHeaders]: 'request:headers',
  [CHANNELS.requestTrailers]: 'request:trailers',
  [CHANNELS.requestError]: 'request:error',
  [CHANNELS.clientBeforeConnect]: 'client:beforeConnect',
  [CHANNELS.clientConnected]: 'client:connected',
  [CHANNELS.clientConnectError]: 'client:connectError',
  [CHANNELS.clientSendHeaders]: 'client:sendHeaders',
});

function isObject(value) {
  return value !== null && typeof value === 'object';
}

function hasOwn(obj, key) {
  try {
    return Object.prototype.hasOwnProperty.call(obj, key);
  } catch (err) {
    return false;
  }
}

/** 只取白名单 code / syscall；未知 code 记为 'unknown'，其余一律省略。 */
function pickErrorFields(err) {
  const out = {};
  if (!isObject(err)) return out;
  try {
    if (hasOwn(err, 'code')) {
      const code = err.code;
      out.code = typeof code === 'string' && ERROR_CODES.has(code) ? code : 'unknown';
    }
    const syscall = err.syscall;
    if (typeof syscall === 'string' && SYSCALLS.has(syscall)) out.syscall = syscall;
  } catch (err2) {
    return {};
  }
  return out;
}

/** 错误只保留白名单 code/syscall 与有限层 cause；绝不复制 message/stack/任意 dict。 */
function safeError(err) {
  try {
    const out = pickErrorFields(err);
    const cause = [];
    let node = isObject(err) ? err.cause : undefined;
    let depth = 0;
    while (isObject(node) && depth < MAX_CAUSE_DEPTH) {
      cause.push(pickErrorFields(node));
      node = node.cause;
      depth += 1;
    }
    if (cause.length > 0) out.cause = cause;
    return out;
  } catch (err2) {
    return {};
  }
}

function normalizeTarget(origin) {
  if (origin === null || origin === undefined) return null;
  if (isObject(origin)) {
    return {
      protocol: typeof origin.protocol === 'string' ? origin.protocol : '',
      hostname: typeof origin.hostname === 'string' ? origin.hostname : '',
      port: origin.port === null || origin.port === undefined ? '' : String(origin.port),
    };
  }
  if (typeof origin !== 'string') return null;
  try {
    const parsed = new URL(origin);
    return {
      protocol: parsed.protocol,
      hostname: parsed.hostname,
      port: parsed.port,
    };
  } catch (err) {
    return null;
  }
}

/** 只允许 api.deepseek.com 的 HTTPS 443；其余 host/协议/端口一律不记录。 */
function matchOrigin(target) {
  if (!target) return false;
  if (String(target.hostname).toLowerCase() !== ALLOWED_HOST) return false;
  if (!ALLOWED_PROTOCOLS.has(target.protocol)) return false;
  if (!ALLOWED_PORTS.has(target.port)) return false;
  return true;
}

function normalizePath(rawPath) {
  if (typeof rawPath !== 'string' || rawPath.length === 0) return null;
  let p = rawPath;
  const cut = p.search(/[?#]/);
  if (cut >= 0) p = p.slice(0, cut);
  if (p.length === 0 || p[0] !== '/') return null;
  return ALLOWED_PATHS.has(p) ? p : null;
}

function normalizeMethod(rawMethod) {
  if (typeof rawMethod !== 'string') return null;
  const method = rawMethod.toUpperCase();
  return ALLOWED_METHODS.has(method) ? method : null;
}

/** 请求目标：host/协议/端口/路径/方法全部命中白名单才返回可记录字段。 */
function matchRequest(request) {
  if (!isObject(request)) return null;
  const origin = matchOrigin(normalizeTarget(request.origin));
  if (!origin) return null;
  const endpoint = normalizePath(request.path);
  if (!endpoint) return null;
  const method = normalizeMethod(request.method);
  if (!method) return null;
  return { method, endpoint };
}

function matchConnectParams(params) {
  if (!isObject(params)) return false;
  const hostname = typeof params.hostname === 'string' ? params.hostname.toLowerCase() : '';
  const port = params.port === null || params.port === undefined ? '' : String(params.port);
  const protocol = typeof params.protocol === 'string' ? params.protocol : '';
  return hostname === ALLOWED_HOST
    && ALLOWED_PORTS.has(port)
    && ALLOWED_PROTOCOLS.has(protocol);
}

function normalizeStatus(response) {
  if (!isObject(response)) return null;
  const status = response.statusCode;
  if (typeof status !== 'number' || !Number.isInteger(status)) return null;
  if (status < 100 || status > 599) return null;
  return status;
}

function inertRecorder(filePath) {
  return {
    enabled: false,
    filePath: filePath,
    close() {},
    stats() {
      return { enabled: false, records: 0, bytes: 0, stopped: false };
    },
  };
}

/**
 * 创建被动记录器。永不抛出：路径非绝对、父目录不存在、文件已存在、写失败
 * 都只表现为 enabled=false 或停止采集，不新建目录、不覆盖既有证据、不轮转。
 */
function createRecorder(options) {
  const opts = isObject(options) ? options : {};
  const filePath = typeof opts.filePath === 'string' ? opts.filePath : '';
  const maxRecords = Number.isInteger(opts.maxRecords) && opts.maxRecords > 0
    ? opts.maxRecords
    : DEFAULT_MAX_RECORDS;
  const maxBytes = Number.isInteger(opts.maxBytes) && opts.maxBytes > 0
    ? opts.maxBytes
    : DEFAULT_MAX_BYTES;

  if (filePath.length === 0 || !path.isAbsolute(filePath)) return inertRecorder(filePath);

  const state = {
    enabled: false,
    stopped: false,
    fd: -1,
    records: 0,
    bytes: 0,
    started: 0,
    requestSeq: 0,
    socketSeq: 0,
  };
  const requestIds = new Map();
  const socketIds = new Map();
  const subscriptions = [];

  function stop() {
    if (state.stopped) return;
    state.stopped = true;
    state.enabled = false;
    for (const [channel, handler] of subscriptions.splice(0)) {
      try {
        channel.unsubscribe(handler);
      } catch (err) {
        /* 诊断通道异常绝不外抛 */
      }
    }
    const fd = state.fd;
    state.fd = -1;
    if (fd >= 0) {
      try {
        fs.closeSync(fd);
      } catch (err) {
        /* 关闭失败同样静默 */
      }
    }
    requestIds.clear();
    socketIds.clear();
  }

  try {
    state.fd = fs.openSync(filePath, 'wx');
  } catch (err) {
    return inertRecorder(filePath);
  }
  state.enabled = true;
  state.started = Date.now();

  function emit(event, fields) {
    if (!state.enabled || state.stopped) return;
    let line;
    try {
      const record = { t: new Date().toISOString(), ms: Date.now() - state.started, event };
      if (isObject(fields)) {
        for (const key of Object.keys(fields)) record[key] = fields[key];
      }
      line = JSON.stringify(record);
    } catch (err) {
      return;
    }
    if (typeof line !== 'string') return;
    const size = Buffer.byteLength(line, 'utf8') + 1;
    if (state.records + 1 > maxRecords || state.bytes + size > maxBytes) {
      stop();
      return;
    }
    try {
      fs.writeSync(state.fd, line + '\n');
    } catch (err) {
      stop();
      return;
    }
    state.records += 1;
    state.bytes += size;
  }

  function requestIdOf(request) {
    if (!isObject(request)) return 0;
    const id = requestIds.get(request);
    return typeof id === 'number' ? id : 0;
  }

  function socketIdOf(socket) {
    if (!isObject(socket)) return null;
    let entry = socketIds.get(socket);
    if (!entry) {
      state.socketSeq += 1;
      entry = { id: state.socketSeq, uses: 0 };
      socketIds.set(socket, entry);
    }
    return entry;
  }

  function subscribe(channelName, handler) {
    try {
      const channel = diagnostics_channel.channel(channelName);
      const wrapped = (payload) => {
        try {
          handler(payload);
        } catch (err) {
          /* 单个 callback 异常不中断 CLI，也不影响其它订阅者 */
        }
      };
      channel.subscribe(wrapped);
      subscriptions.push([channel, wrapped]);
    } catch (err) {
      /* 订阅失败只意味着少一类证据 */
    }
  }

  subscribe(CHANNELS.requestCreate, (payload) => {
    const request = isObject(payload) ? payload.request : null;
    const target = matchRequest(request);
    if (!target) return;
    if (requestIds.has(request)) return;
    state.requestSeq += 1;
    requestIds.set(request, state.requestSeq);
    emit('request:create', {
      request_id: state.requestSeq,
      method: target.method,
      endpoint: target.endpoint,
    });
  });

  subscribe(CHANNELS.requestBodySent, (payload) => {
    const request = isObject(payload) ? payload.request : null;
    const id = requestIdOf(request);
    if (!id) return;
    emit('request:bodySent', { request_id: id });
  });

  subscribe(CHANNELS.requestHeaders, (payload) => {
    const request = isObject(payload) ? payload.request : null;
    const id = requestIdOf(request);
    if (!id) return;
    const fields = { request_id: id };
    const status = normalizeStatus(isObject(payload) ? payload.response : null);
    if (status !== null) fields.status = status;
    emit('request:headers', fields);
  });

  subscribe(CHANNELS.requestTrailers, (payload) => {
    const request = isObject(payload) ? payload.request : null;
    const id = requestIdOf(request);
    if (!id) return;
    // trailers 内容绝不读取，只记录阶段到达。
    emit('request:trailers', { request_id: id });
  });

  subscribe(CHANNELS.requestError, (payload) => {
    const request = isObject(payload) ? payload.request : null;
    const id = requestIdOf(request);
    if (!id) return;
    emit('request:error', {
      request_id: id,
      error: safeError(isObject(payload) ? payload.error : undefined),
    });
  });

  subscribe(CHANNELS.clientBeforeConnect, (payload) => {
    if (!matchConnectParams(isObject(payload) ? payload.connectParams : null)) return;
    // 此时没有 socket，也没有 request；不凭空绑定。
    emit('client:beforeConnect', {});
  });

  subscribe(CHANNELS.clientConnected, (payload) => {
    if (!matchConnectParams(isObject(payload) ? payload.connectParams : null)) return;
    const entry = socketIdOf(isObject(payload) ? payload.socket : null);
    if (!entry) return;
    emit('client:connected', { socket_id: entry.id });
  });

  subscribe(CHANNELS.clientConnectError, (payload) => {
    if (!matchConnectParams(isObject(payload) ? payload.connectParams : null)) return;
    // 连接错误没有 request 关联；socket 可能尚未建立。
    emit('client:connectError', {
      error: safeError(isObject(payload) ? payload.error : undefined),
    });
  });

  subscribe(CHANNELS.clientSendHeaders, (payload) => {
    const request = isObject(payload) ? payload.request : null;
    const socket = isObject(payload) ? payload.socket : null;
    const id = requestIdOf(request);
    if (!id || !isObject(socket)) return;
    const entry = socketIdOf(socket);
    if (!entry) return;
    entry.uses += 1;
    emit('client:sendHeaders', {
      request_id: id,
      socket_id: entry.id,
      socket_uses: entry.uses,
      socket_reused: entry.uses > 1,
    });
  });

  return {
    enabled: true,
    filePath: filePath,
    close: stop,
    stats() {
      return {
        enabled: state.enabled,
        records: state.records,
        bytes: state.bytes,
        stopped: state.stopped,
      };
    },
  };
}

let defaultRecorder = null;
try {
  const target = process.env[TRACE_ENV];
  if (typeof target === 'string' && target.length > 0 && path.isAbsolute(target)) {
    defaultRecorder = createRecorder({ filePath: target });
  }
} catch (err) {
  defaultRecorder = null;
}

module.exports = {
  CHANNELS,
  ERROR_CODES,
  SYSCALLS,
  ALLOWED_PATHS,
  createRecorder,
  safeError,
  getDefaultRecorder() {
    return defaultRecorder;
  },
};
