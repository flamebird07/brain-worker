// BW-ZCODE-FILES-S1：受控逐文件 FileSystemPort 执行边界（JS 侧）。
//
// 装饰官方 createNodeFileSystemAdapter，产出只放行登记文件的 FileSystemPort，注入公开的
// createZCodeApp({ fileSystemPort })。读只放行 readable ∪ writable 的精确文件；写只放行 writable
// 且必须落在工作区真实目录内；removeFile / listDirectory / searchFiles / searchText 一律 fail-closed。
// 每个操作在真正副作用之前做别名/junction/reparse/硬链接/越界/漂移复检，并把结果透传给 inner，
// 保留 expectedRevision、encoding、signal 与官方 FileSystemPortError 语义。绝不声称 OS 进程沙箱：
// 外部非协作进程仍能改盘，本边界只约束经 FileSystemPort 的工具 I/O 路径（TOCTOU 限制见文档）。
//
// canonicalize/canonicalSha 与 scripts/zcode_permission_broker.mjs 完全一致（同一 JSON 规范化口径），
// 保证 Python 冻结 SHA 与 JS 重算 SHA 同源。FileSystemPortError 由调用方（runner）从同一棵公开 SDK
// 树的 contracts 入口注入，避免不同类身份导致 instanceof 判定失败。
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, realpathSync, statSync } from 'node:fs';
import { dirname } from 'node:path';

const IS_WIN = process.platform === 'win32';

function sha256Hex(bufferOrText) {
  return createHash('sha256')
    .update(typeof bufferOrText === 'string' ? bufferOrText : bufferOrText)
    .digest('hex');
}

export function canonicalize(value) {
  if (Array.isArray(value)) return '[' + value.map(canonicalize).join(',') + ']';
  if (value && typeof value === 'object') {
    const keys = Object.keys(value).sort();
    return '{' + keys.map(k => JSON.stringify(k) + ':' + canonicalize(value[k])).join(',') + '}';
  }
  return JSON.stringify(value);
}

export function canonicalSha(contract) {
  return sha256Hex(canonicalize(contract));
}

// Windows 折叠大小写并把分隔符统一为反斜杠；POSIX 保留大小写、只折叠重复分隔符与末分隔符。
export function pathKey(p) {
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

function realpathOrNull(p) {
  try { return realpathSync(p); } catch { return null; }
}

// 构建期对单个登记项做真实复检：漂移/别名/类型/新文件父目录。任一不符即抛（runner fail-closed）。
// mustBeWithin：writable 必须落在工作区内；readable 允许明确的外部只读参考（与 Python 侧同口径），
// 故 readable 传 false 时跳过 in-workspace 强制，但仍复检别名/类型/硬链接/内容漂移。
function reverifyEntry(entry, workspaceReal, FileSystemPortError, mustBeWithin) {
  const regKey = pathKey(entry.real);
  const real = realpathOrNull(entry.real);
  if (entry.sha256 !== null && entry.sha256 !== undefined) {
    if (real === null) throw new FileSystemPortError({ code: 'not_found', path: entry.real,
      message: `registered file vanished since registration: ${entry.real}` });
    if (pathKey(real) !== regKey) {
      throw new FileSystemPortError({ code: 'invalid_path', path: entry.real,
        message: `registered file resolves through an alias/junction: ${entry.real}` });
    }
    if (mustBeWithin && !isWithin(workspaceReal, regKey)) {
      throw new FileSystemPortError({ code: 'permission_denied', path: entry.real,
        message: `registered file resolves outside the workspace: ${entry.real}` });
    }
    const st = statSync(real);
    if (!st.isFile()) {
      throw new FileSystemPortError({ code: 'invalid_path', path: entry.real,
        message: `registered file is not a regular file: ${entry.real}` });
    }
    if (st.nlink > 1) {
      throw new FileSystemPortError({ code: 'permission_denied', path: entry.real,
        message: `registered file has multiple hard links: ${entry.real}` });
    }
    const actual = sha256Hex(readFileSync(real));
    if (actual !== entry.sha256.toLowerCase()) {
      throw new FileSystemPortError({ code: 'stale_write', path: entry.real,
        message: `registered file content drifted since registration: ${entry.real}` });
    }
  } else {
    // 新写文件：目标此刻不应存在；若已存在则做与现有文件同样的别名/硬链接/类型复检，
    // 若不存在则其父目录必须真实存在且仍在工作区内（拒绝隐式建目录 / 父目录漂移）。
    if (real !== null) {
      if (pathKey(real) !== regKey) {
        throw new FileSystemPortError({ code: 'invalid_path', path: entry.real,
          message: `new-file target resolves through an alias/junction: ${entry.real}` });
      }
      const st = statSync(real);
      if (!st.isFile()) {
        throw new FileSystemPortError({ code: 'invalid_path', path: entry.real,
          message: `new-file target is not a regular file: ${entry.real}` });
      }
      if (st.nlink > 1) {
        throw new FileSystemPortError({ code: 'permission_denied', path: entry.real,
          message: `new-file target has multiple hard links: ${entry.real}` });
      }
    } else {
      const parent = dirname(entry.real);
      const parentReal = realpathOrNull(parent);
      if (parentReal === null || !statSync(parentReal).isDirectory()) {
        throw new FileSystemPortError({ code: 'invalid_path', path: entry.real,
          message: `new-file parent directory missing: ${parent}` });
      }
      // 精确父目录绑定：真实解析必须逐字回到登记父目录键。仅"在工作区内"不够——登记后把父目录
      // 换成指向同工作区另一目录（或外部）的 junction/symlink，仍在工作区内却会把未登记目标
      // 写进别名指向的另一目录，这是两次操作之间即可稳定复现的静态父目录别名漂移。
      if (pathKey(parentReal) !== pathKey(parent)) {
        throw new FileSystemPortError({ code: 'invalid_path', path: entry.real,
          message: `new-file parent directory resolves through an alias/junction: ${parent}` });
      }
      if (!isWithin(workspaceReal, pathKey(parentReal))) {
        throw new FileSystemPortError({ code: 'permission_denied', path: entry.real,
          message: `new-file parent directory resolves outside the workspace: ${parent}` });
      }
    }
    if (!isWithin(workspaceReal, regKey)) {
      throw new FileSystemPortError({ code: 'permission_denied', path: entry.real,
        message: `registered writable file resolves outside the workspace: ${entry.real}` });
    }
  }
}

// 冻结守卫：解析并复检 workspace + 两个集合，返回可被 createGuardedFileSystemPort 使用的描述。
export function buildFileScopeGuard(contract, { workspaceRealPath, FileSystemPortError }) {
  if (!FileSystemPortError) throw new Error('buildFileScopeGuard requires FileSystemPortError');
  if (!workspaceRealPath) throw new Error('buildFileScopeGuard requires workspaceRealPath');
  const wsReal = realpathOrNull(workspaceRealPath);
  if (wsReal === null) {
    throw new FileSystemPortError({ code: 'invalid_path', path: workspaceRealPath,
      message: `workspace realpath unavailable: ${workspaceRealPath}` });
  }
  const workspaceKey = pathKey(wsReal);
  if (pathKey(contract.workspace) !== workspaceKey) {
    throw new FileSystemPortError({ code: 'invalid_path', path: contract.workspace,
      message: `contract workspace ${contract.workspace} does not match realpath ${wsReal}` });
  }
  const writeMap = new Map();
  const readMap = new Map();
  const parentSet = new Set();
  for (const entry of (contract.writable_files || [])) {
    reverifyEntry(entry, workspaceKey, FileSystemPortError, true);
    writeMap.set(pathKey(entry.real), entry);
    readMap.set(pathKey(entry.real), entry);
    parentSet.add(pathKey(dirname(entry.real)));
  }
  for (const entry of (contract.readable_files || [])) {
    reverifyEntry(entry, workspaceKey, FileSystemPortError, false);
    readMap.set(pathKey(entry.real), entry);
  }
  return { workspaceKey, writeMap, readMap, parentSet, FileSystemPortError };
}

function traceAssoc(request) {
  const trace = request?.trace || {};
  const attrs = trace.attributes || {};
  return {
    session_id: trace.sessionId ?? null,
    turn_id: trace.turnId ?? null,
    tool_call_id: attrs.toolCallId ?? null,
    tool_name: attrs.toolName ?? null,
  };
}

function isoNow() { return new Date().toISOString(); }

// 运行期对单个入站路径做登记匹配与副作用前复检；返回 {ok, rec, reason}。
function decide(guard, map, request, { requireWrite }) {
  const path = request?.path;
  if (typeof path !== 'string' || path === '') return { ok: false, reason: 'invalid-path' };
  const lexKey = pathKey(path);
  const rec = map.get(lexKey);
  if (!rec) return { ok: false, reason: requireWrite ? 'unregistered-write' : 'unregistered-read' };
  // 副作用前复检：realpath 解析后仍指向同一登记真实路径（拒绝别名/漂移/`..` 绕开）。
  const real = realpathOrNull(path);
  if (rec.sha256 !== null && rec.sha256 !== undefined) {
    if (real === null) return { ok: false, reason: 'missing-since-registration' };
    if (pathKey(real) !== pathKey(rec.real)) return { ok: false, reason: 'alias-or-drift' };
    let st;
    try { st = statSync(real); } catch { return { ok: false, reason: 'stat-failed' }; }
    if (!st.isFile()) return { ok: false, reason: 'not-a-file' };
    if (st.nlink > 1) return { ok: false, reason: 'hardlink-multiple' };
    // 写必须落工作区内；读允许外部只读参考（build 期已按 mustBeWithin 区分），故仅写检越界。
    if (requireWrite && !isWithin(guard.workspaceKey, pathKey(real))) {
      return { ok: false, reason: 'outside-workspace' };
    }
  } else {
    if (real === null) {
      // 新写文件：父目录必须真实存在且仍精确解析回登记的父目录（拒绝隐式建目录 / build 之后
      // 把父目录换成同工作区或外部别名的漂移写，别名会把未登记目标写进别名指向的另一目录）。
      const parent = dirname(path);
      const regParent = dirname(rec.real);
      const parentReal = realpathOrNull(parent);
      if (parentReal === null) return { ok: false, reason: 'parent-missing' };
      let pst;
      try { pst = statSync(parentReal); } catch { return { ok: false, reason: 'parent-stat-failed' }; }
      if (!pst.isDirectory()) return { ok: false, reason: 'parent-not-directory' };
      if (pathKey(parentReal) !== pathKey(regParent)) {
        return { ok: false, reason: 'parent-alias-or-drift' };
      }
      if (!isWithin(guard.workspaceKey, pathKey(parentReal))) {
        return { ok: false, reason: 'parent-outside-workspace' };
      }
    } else {
      if (pathKey(real) !== pathKey(rec.real)) return { ok: false, reason: 'alias-or-drift' };
      let st;
      try { st = statSync(real); } catch { return { ok: false, reason: 'stat-failed' }; }
      if (!st.isFile()) return { ok: false, reason: 'not-a-file' };
      if (st.nlink > 1) return { ok: false, reason: 'hardlink-multiple' };
      if (!isWithin(guard.workspaceKey, pathKey(real))) return { ok: false, reason: 'outside-workspace' };
    }
  }
  return { ok: true, rec };
}

function denied(guard, reason, path) {
  return new guard.FileSystemPortError({ code: 'permission_denied', path,
    message: `file-scope guard denied (${reason}): ${path}` });
}

export function createGuardedFileSystemPort({ innerPort, guard, sink }) {
  if (!innerPort) throw new Error('guarded file system port requires an inner port');
  if (!guard) throw new Error('guarded file system port requires a guard');
  if (!sink || typeof sink.record !== 'function') {
    throw new Error('guarded file system port requires an audit sink');
  }
  let ops = 0;
  let deniedCount = 0;
  let auditEvents = 0;
  const E = guard.FileSystemPortError;

  function auditOrThrow(payload) {
    try {
      sink.record(payload);
      auditEvents += 1;
    } catch (error) {
      throw new E({ code: 'io_error', message: `file-scope audit write failed; refusing next action: `
        + `${error?.message || error}` });
    }
  }

  // 登记目标放行才记录真实 path；未登记只记录 path 的 SHA，绝不写原始路径/内容/凭据。
  function gateRow(op, request, allowed, reason, rec) {
    const assoc = traceAssoc(request);
    const row = { kind: 'fs_gate', op, at_utc: isoNow(), allowed, reason: reason ?? null, ...assoc };
    if (allowed && rec) row.target_ref = rec.real;
    else row.target_sha256 = sha256Hex(String(request?.path ?? ''));
    auditOrThrow(row);
  }

  function makeReadMethod(name, innerName) {
    return async function (request, options) {
      const decision = decide(guard, guard.readMap, request, { requireWrite: false });
      gateRow(name, request, decision.ok, decision.ok ? null : decision.reason, decision.rec);
      if (!decision.ok) { deniedCount += 1; throw denied(guard, decision.reason, request?.path); }
      const result = await innerPort[innerName](request, options);
      ops += 1;
      auditOrThrow({ kind: 'fs_result', op: name, at_utc: isoNow(), status: 'ok',
        target_ref: decision.rec.real, ...traceAssoc(request) });
      return result;
    };
  }

  const port = {
    stat: makeReadMethod('stat', 'stat'),
    readTextFile: makeReadMethod('readTextFile', 'readTextFile'),
    readBinaryFile: makeReadMethod('readBinaryFile', 'readBinaryFile'),
    readTextFileRange: makeReadMethod('readTextFileRange', 'readTextFileRange'),

    async writeTextFile(request, options) {
      // 只放行 writable；readable-only 参考命中 readMap 但不在 writeMap → unregistered-write 拒。
      const decision = decide(guard, guard.writeMap, request, { requireWrite: true });
      gateRow('writeTextFile', request, decision.ok, decision.ok ? null : decision.reason, decision.rec);
      if (!decision.ok) { deniedCount += 1; throw denied(guard, decision.reason, request?.path); }
      // 强制不建父目录：新文件的父目录已在守卫中确认存在；拒绝任何隐式扩大创建范围。
      const forwarded = { ...request, createParents: false };
      let result;
      try {
        result = await innerPort.writeTextFile(forwarded, options);
      } catch (error) {
        // 实际副作用可能已发生：如实留错误事实（不谎报为零动作），错误语义向上冒泡。
        auditOrThrow({ kind: 'fs_result', op: 'writeTextFile', at_utc: isoNow(),
          status: 'inner_error', error: String(error?.message || error),
          ...traceAssoc(request) });
        throw error;
      }
      ops += 1;
      auditOrThrow({ kind: 'fs_result', op: 'writeTextFile', at_utc: isoNow(), status: 'ok',
        bytes_written: result?.bytesWritten ?? null, target_ref: decision.rec.real,
        ...traceAssoc(request) });
      return result;
    },

    async createDirectory(request, options) {
      const key = pathKey(request?.path ?? '');
      // 仅允许对应登记写文件“已存在的必要父目录”，且该父目录必须未被别名替换：realpath 逐字
      // 回到登记键，否则拒绝（拒绝把登记父目录换成 junction 后仍"建目录"，实为操作别名指向的目录）。
      const exists = (() => {
        try {
          const rp = realpathOrNull(request?.path);
          return rp !== null && pathKey(rp) === key && statSync(rp).isDirectory();
        } catch { return false; }
      })();
      const ok = key && guard.parentSet.has(key) && exists;
      gateRow('createDirectory', request, ok, ok ? null : 'directory-not-granted',
        ok ? { real: request.path } : null);
      if (!ok) { deniedCount += 1; throw denied(guard, 'directory-not-granted', request?.path); }
      return innerPort.createDirectory(request, options);
    },

    async removeFile(request, options) {
      // 不授予删除：一律拒绝，inner 零调用（零副作用）。
      gateRow('removeFile', request, false, 'remove-not-granted', null);
      deniedCount += 1;
      throw denied(guard, 'remove-not-granted', request?.path);
    },

    // 本模式关闭 Glob/Grep/Bash/其它搜索工具：list/search 端口入口 fail-closed，不透传。
    async listDirectory(request) {
      gateRow('listDirectory', request, false, 'unsupported-in-file-scope', null);
      deniedCount += 1;
      throw new E({ code: 'unsupported', path: request?.path,
        message: 'directory listing is disabled in controlled file-scope mode' });
    },
    async searchFiles(request) {
      gateRow('searchFiles', request, false, 'unsupported-in-file-scope', null);
      deniedCount += 1;
      throw new E({ code: 'unsupported', path: request?.path,
        message: 'file search is disabled in controlled file-scope mode' });
    },
    async searchText(request) {
      gateRow('searchText', request, false, 'unsupported-in-file-scope', null);
      deniedCount += 1;
      throw new E({ code: 'unsupported', path: request?.path,
        message: 'text search is disabled in controlled file-scope mode' });
    },

    // 供 runner 汇总的只读计数（下划线前缀，不作为端口对外方法被工具探测）。
    _fsOps: () => ops,
    _fsDenied: () => deniedCount,
    _fsAuditEvents: () => auditEvents,
    // 对外暴露的端口方法面（runner 断言恰为官方 10 个 FileSystemPort 方法，无额外/后台面）。
    _fsMethods: () => Object.keys(port).filter(k => !k.startsWith('_')),
  };
  return port;
}
