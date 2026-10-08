"""continuation_contract — 中断接续包（BW-QUOTA-20261008-S2 扩展）。

S1 首版只做 SHA 绑定；S2 按已证实阻断项补齐（详细口径见随包 references/quota-routing.md 第 6 节）：
- freeze/evaluate 真正保存声明文件集的 baseline/终态字节副本并产出可核对统一 diff，
  而不是只存 SHA、只报行数（缺陷 J）；副本有界（单文件/总量上限），敏感配置名一律
  拒绝复制；
- evaluate 校验同一 workspace_realpath 与每文件原物理 target：换 workspace、
  junction/symlink 改指向即漂移，漂移文件不透过新链接读取（缺陷 L）；
- bind_registered_test 使用 execution_control.run_registered_test 的**原始 evidence
  结构**（name/argv/cwd/exit_code/inputs_unchanged/inputs/fingerprint/env_sha256/
  runtime_version/stdout/stderr SHA），复用 zcode_execution_evidence 的同一验证口径：
  指纹内部一致、当前输入匹配、日志 SHA 回读；改过即 stale；缺证据/空 inputs/退出码
  伪布尔/日志被动过 → invalid，不能继承 pass；绝不重读“当前”源造新指纹冒充历史成功
  （缺陷 K）；
- 原始报告/错误引用必须磁盘存在 + SHA 回读 verified；没有最终 report 就保留缺失，
  final_worker_report_present 只认 verified 引用，单独的接续摘要不能冒充原报告
  （缺陷 M）；
- build_handoff 关联成功 Edit/Write 的原始工具回执（tool_use_id + 事件来源行），
  不凭报告关键词；extract_write_receipts 从 ZCode events.jsonl 结构提取；
  load_continuation_contract/check_prev_handoff_drift 供两入口把接续做成真实中断
  交接（缺陷 I）：派发前 baseline 留证、终态后自动生成 continuation.json、
  上一手 handoff 漂移显式拒绝。

本模块只做绑定与核对，不伪造报告、不执行测试、不做全项目无界扫描。
"""
import difflib
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

try:
    import execution_control as ec
    import zcode_execution_evidence as zee
except ImportError:  # 直接以文件运行测试时从同目录导入
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import execution_control as ec
    import zcode_execution_evidence as zee

# 副本边界（缺陷 J）：只复制声明的有界文件集；敏感配置名拒绝复制（哈希照记）。
MAX_COPY_BYTES_PER_FILE = 2 * 1024 * 1024
MAX_TOTAL_COPY_BYTES = 8 * 1024 * 1024
SENSITIVE_BASENAME_MARKERS = ('.env', 'token', 'secret', 'credential', 'cookie',
                              'apikey', 'api_key', 'password')
WRITE_TOOLS = ('Write', 'Edit')
CONTRACT_SPEC_KEYS = {'files', 'todos', 'original_report_ref', 'original_error_ref',
                      'prev_handoff', 'test_evidence'}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _disk_read(p):
    return Path(p).read_bytes() if Path(p).is_file() else None


def _norm_within(workspace, rel) -> str | None:
    root = os.path.normcase(os.path.realpath(str(workspace)))
    target = os.path.normcase(os.path.realpath(os.path.join(root, str(rel))))
    if target == root or target.startswith(root + os.sep):
        return target
    return None


def _is_sensitive_name(rel) -> bool:
    name = Path(str(rel)).name.lower()
    return any(marker in name for marker in SENSITIVE_BASENAME_MARKERS)


def _rel_from_target(workspace, target) -> str:
    root = os.path.normcase(os.path.realpath(str(workspace)))
    return os.path.relpath(target, root)


def _copy_rel_path(rel: str) -> Path:
    # 副本路径只用规范化相对段（去盘符/分隔符统一），拒绝任何逃逸段。
    parts = [seg for seg in str(rel).replace('\\', '/').split('/') if seg not in ('', '.', '..')]
    if not parts:
        raise ValueError(f'cannot build a copy path for declared file {rel!r}')
    return Path(*parts)


def freeze(files, workspace, copies_dir=None, read_bytes=None) -> dict:
    """派发前冻结受控文件清单 baseline。路径必须解析在工作区内（realpath），越界拒绝。
    copies_dir 提供时真实保存 baseline 字节副本（有界、敏感名拒绝复制），否则只记
    SHA（哈希模式，兼容最小用法）。文件可不存在（记 missing），不猜内容。"""
    read_bytes = read_bytes or _disk_read
    root_real = os.path.realpath(str(workspace))
    entries = {}
    total = 0
    for rel in files or []:
        target = _norm_within(workspace, rel)
        if target is None:
            raise ValueError(f'declared file escapes workspace (realpath): {rel!r}')
        data = read_bytes(target)
        entry = {'target': target,
                 'baseline_sha256': _sha(data) if data is not None else None,
                 'baseline_exists': data is not None}
        if copies_dir is not None and data is not None:
            if _is_sensitive_name(rel):
                raise ValueError(
                    f'declared file {rel!r} matches a sensitive-config name pattern; '
                    f'refusing to copy its bytes (hash is still recorded); do not '
                    f'declare credential/config files in the bounded copy set')
            if len(data) > MAX_COPY_BYTES_PER_FILE:
                raise ValueError(
                    f'declared file {rel!r} is {len(data)} bytes, over the '
                    f'{MAX_COPY_BYTES_PER_FILE}-byte per-file copy bound')
            total += len(data)
            if total > MAX_TOTAL_COPY_BYTES:
                raise ValueError('declared copy set exceeds the total bounded-copy '
                                 f'budget of {MAX_TOTAL_COPY_BYTES} bytes; narrow the '
                                 'declared file list')
            dest = Path(copies_dir) / 'baseline' / _copy_rel_path(
                _rel_from_target(workspace, target))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            entry['baseline_copy'] = str(dest)
            entry['baseline_copy_sha256'] = _sha(Path(dest).read_bytes())
        entries[str(rel)] = entry
    return {'files_declared': bool(entries), 'files': entries,
            'workspace_realpath': root_real,
            'copies_kept': copies_dir is not None,
            'frozen_at_utc': _now()}


def evaluate(frozen: dict, workspace, copies_dir=None, read_bytes=None) -> dict:
    """终态后回读同一清单：逐文件 baseline/当前 SHA、changed/missing、target 漂移与
    可选终态字节副本。同一 workspace_realpath 与每文件原物理 target 是硬校验：换
    workspace / junction 改指向即漂移（ValueError / target_drift），绝不透过新链接
    读入其他文件（缺陷 L）。"""
    read_bytes = read_bytes or _disk_read
    current_root = os.path.realpath(str(workspace))
    if frozen.get('workspace_realpath') != current_root:
        raise ValueError(
            f'workspace drift: frozen baseline was taken under '
            f'{frozen.get("workspace_realpath")!r} but the current workspace resolves '
            f'to {current_root!r}; refusing to evaluate against a different workspace')
    files = {}
    terminal_total = 0
    for rel, entry in (frozen.get('files') or {}).items():
        target = _norm_within(workspace, rel)
        current = None
        drift = False
        copy_note = None
        if target is None:
            drift = True
        elif os.path.normcase(target) != os.path.normcase(entry['target']):
            drift = True  # 声明路径现解析到另一物理文件：不读、明标漂移
        else:
            data = read_bytes(target)
            current = _sha(data) if data is not None else None
            if copies_dir is not None and data is not None:
                # 终态副本复用 baseline 的同一敏感名/2MB 单文件/8MB 总量边界（S4 缺陷 8）：
                # baseline 不存在后新建的敏感文件、或文件增长越界，一律拒绝复制，只记 SHA。
                if _is_sensitive_name(rel):
                    copy_note = ('terminal file matches a sensitive-config name; '
                                 'refusing to copy bytes (SHA still recorded)')
                elif len(data) > MAX_COPY_BYTES_PER_FILE:
                    copy_note = (f'terminal file is {len(data)} bytes, over the '
                                 f'{MAX_COPY_BYTES_PER_FILE}-byte per-file copy bound; '
                                 f'not copied')
                elif terminal_total + len(data) > MAX_TOTAL_COPY_BYTES:
                    copy_note = (f'terminal copy set would exceed the '
                                 f'{MAX_TOTAL_COPY_BYTES}-byte total budget; not copied')
                else:
                    terminal_total += len(data)
                    dest = Path(copies_dir) / 'terminal' / _copy_rel_path(
                        _rel_from_target(workspace, target))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    entry = dict(entry)
                    entry['terminal_copy'] = str(dest)
                    entry['terminal_copy_sha256'] = _sha(Path(dest).read_bytes())
        changed = drift or (current != entry['baseline_sha256'])
        record = dict(entry)
        record['current_sha256'] = current
        record['changed'] = changed
        record['missing_now'] = current is None and not drift
        record['target_drift'] = drift
        if copy_note is not None:
            record['terminal_copy_refused'] = copy_note
        files[rel] = record
    return {'files': files, 'any_changed': any(f['changed'] for f in files.values()),
            'evaluated_at_utc': _now(),
            'files_declared': bool(frozen.get('files_declared')),
            'workspace_realpath': current_root}


def diff_declared(frozen: dict, evaluation: dict, max_lines=200) -> dict:
    """声明文件的真实统一 diff：baseline 副本 vs 终态副本（freeze/evaluate 带
    copies_dir 时）。缺任一副本（含敏感名/超界/漂移/缺失）就如实标注原因，不猜、
    不再退回“只有行数”（缺陷 J）。"""
    out = {}
    for rel, entry in (frozen.get('files') or {}).items():
        ev = (evaluation.get('files') or {}).get(rel, {})
        base_copy = entry.get('baseline_copy')
        term_copy = ev.get('terminal_copy')
        record = {'baseline_sha256': entry.get('baseline_sha256'),
                  'current_sha256': ev.get('current_sha256'),
                  'changed': ev.get('changed'),
                  'target_drift': ev.get('target_drift')}
        if entry.get('baseline_exists') is False or ev.get('missing_now'):
            record['diff_status'] = 'missing_side'
            record['note'] = ('baseline or terminal side is missing; no textual diff '
                              'is fabricated')
        elif ev.get('target_drift'):
            record['diff_status'] = 'target_drift'
            record['note'] = ('declared path now resolves to a different physical '
                              'file; refusing to diff across the swap')
        elif not base_copy or not term_copy:
            record['diff_status'] = 'copies_not_kept'
            record['note'] = ('byte copies were not kept for this file (sensitive '
                              'name, size bound, or hash-only freeze); verify via the '
                              'recorded SHA pair')
        else:
            # 先回读比对 baseline/terminal 副本自身的记录 SHA：副本被篡改就不出 diff，
            # 绝不 diff_status=ok（S4 缺陷 8）。
            base_disk = Path(base_copy).read_bytes()
            term_disk = Path(term_copy).read_bytes()
            base_rec = entry.get('baseline_copy_sha256')
            term_rec = ev.get('terminal_copy_sha256')
            if (base_rec and _sha(base_disk) != base_rec) or \
                    (term_rec and _sha(term_disk) != term_rec):
                record['diff_status'] = 'copy_tampered'
                record['note'] = ('a kept byte copy no longer matches its recorded '
                                  'SHA; refusing to diff a possibly tampered copy')
                out[rel] = record
                continue
            base_text = base_disk.decode('utf-8', 'replace').splitlines()
            term_text = term_disk.decode('utf-8', 'replace').splitlines()
            diff = list(difflib.unified_diff(
                base_text, term_text, fromfile=f'baseline/{rel}', tofile=f'terminal/{rel}',
                lineterm=''))
            record['diff_status'] = 'ok'
            record['diff_truncated'] = len(diff) > max_lines
            record['diff_lines'] = diff[:max_lines]
            record['diff_total_lines'] = len(diff)
        out[rel] = record
    return out


# ---------------------------------------------------------------- 契约装载与漂移
def load_continuation_contract(path, workspace) -> dict:
    """装载接续契约 JSON：{files: [rel...], todos?: [...], original_report_ref?: path,
    original_error_ref?: path, prev_handoff?: path, test_evidence?: {evidence_path,
    registered_inputs: [...]}}。文件清单与所有引用路径（报告/错误/handoff/测试证据）
    必须规范化解析在授权工作区内（realpath），不接受证据自报的任意路径（缺陷 L）；
    test_evidence.registered_inputs 必须是登记时的原始输入路径字符串（非空）。"""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f'continuation contract file not found: {p}')
    try:
        spec = json.loads(p.read_text(encoding='utf-8'))
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        raise ValueError(f'continuation contract unreadable: {exc}')
    if not isinstance(spec, dict):
        raise ValueError('continuation contract must be a JSON object')
    unknown = sorted(set(spec) - CONTRACT_SPEC_KEYS)
    if unknown:
        raise ValueError(f'unknown continuation contract field(s): {unknown}')
    files = spec.get('files')
    if not isinstance(files, list) or any(not isinstance(f, str) or not f.strip()
                                          for f in files):
        raise ValueError('continuation contract files must be a list of non-blank '
                         'string paths (the declared bounded file set)')
    for rel in files:
        if _norm_within(workspace, rel) is None:
            raise ValueError(f'continuation contract file escapes workspace '
                             f'(realpath): {rel!r}')
    for key in ('original_report_ref', 'original_error_ref', 'prev_handoff'):
        ref = spec.get(key)
        if ref is None:
            continue
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError(f'{key} must be a non-blank string path when present')
        if _norm_within(workspace, ref) is None:
            raise ValueError(f'{key} escapes the authorized workspace (realpath): '
                             f'{ref!r}')
    todos = spec.get('todos')
    if todos is not None and (not isinstance(todos, list)
                              or any(not isinstance(t, str) for t in todos)):
        raise ValueError('todos must be a list of strings when present')
    test_spec = spec.get('test_evidence')
    if test_spec is not None:
        if not isinstance(test_spec, dict) \
                or set(test_spec) != {'evidence_path', 'registered_inputs'}:
            raise ValueError('test_evidence must be an object with exactly '
                             'evidence_path and registered_inputs')
        if not isinstance(test_spec['evidence_path'], str) \
                or _norm_within(workspace, test_spec['evidence_path']) is None:
            raise ValueError('test_evidence evidence_path escapes the authorized '
                             'workspace (realpath)')
        inputs = test_spec['registered_inputs']
        if not isinstance(inputs, list) or not inputs \
                or any(not isinstance(i, str) or not i.strip() for i in inputs):
            raise ValueError('test_evidence registered_inputs must be a non-empty '
                             'list of the original registration input path strings')
    return spec


def check_prev_handoff_drift(spec: dict, workspace, read_bytes=None) -> list:
    """下一执行器携带上一手 continuation.json 时的接续校验（缺陷 I）：工作区
    realpath 必须一致；上一手记录的每文件 current_sha256 必须仍与磁盘一致。任何
    漂移都显式列出（调用方拒绝派发或转待复核），绝不默默用新 baseline 覆盖。"""
    read_bytes = read_bytes or _disk_read
    prev_path = (spec or {}).get('prev_handoff')
    if not prev_path:
        return []
    # prev_handoff 在 load 时是按 workspace 校验的相对/绝对引用；这里必须按**同一真实
    # workspace** 解析（绝不按启动 cwd 读），否则起始 cwd!=workspace 时相对 prev_path 会
    # 读到错误文件。_norm_within 归一到 workspace，越界/junction 逃逸直接拒绝。
    prev_norm = _norm_within(workspace, prev_path)
    if prev_norm is None:
        return [f'prev_handoff {prev_path!r} resolves outside the authorized workspace '
                f'(realpath); refusing to read it from the startup cwd']
    try:
        prev = json.loads(Path(prev_norm).read_text(encoding='utf-8'))
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        return [f'prev_handoff unreadable: {exc}']
    if not isinstance(prev, dict):
        return ['prev_handoff is not a JSON object']
    prev_root = prev.get('workspace_realpath')
    current_root = os.path.realpath(str(workspace))
    reasons = []
    if prev_root != current_root:
        reasons.append(f'prev_handoff was built under {prev_root!r} but this '
                       f'workspace resolves to {current_root!r}; continuation across '
                       'workspaces is refused')
    for rel, entry in (prev.get('files') or {}).items():
        target = _norm_within(workspace, rel)
        if target is None:
            reasons.append(f'prev_handoff file {rel!r} no longer resolves inside the '
                           'workspace')
            continue
        # 物理目标核验（S5 缺陷 7）：声明路径当前解析到的物理 realpath 必须与上一手记录的
        # physical_target 一致。A→B 同内容软链/junction 换指向时 SHA 可能仍相等，但物理
        # 目标已变——拒绝继续，绝不透过新链接把写入落到 B。
        prev_physical = entry.get('physical_target')
        if isinstance(prev_physical, str) and prev_physical:
            if os.path.normcase(target) != os.path.normcase(prev_physical):
                reasons.append(
                    f'physical target drift: declared file {rel!r} now resolves to '
                    f'{target!r} but the previous handoff froze it at '
                    f'{prev_physical!r}; a same-content symlink/junction swap would let '
                    'this run write a different physical file — refusing to continue')
                continue
        data = read_bytes(target)
        current = _sha(data) if data is not None else None
        if current != entry.get('current_sha256'):
            reasons.append(
                f'drift: declared file {rel!r} changed since the previous handoff '
                f'(recorded current_sha256={entry.get("current_sha256")}, '
                f'now={current}); refusing to continue — reconcile the change first')
    return reasons


# ---------------------------------------------------------------- 引用核验
def verify_ref(ref, read_bytes=None) -> dict:
    """原始报告/错误引用核验（缺陷 M）：引用必须磁盘存在并 SHA 回读。支持 str 路径
    或 {'path','sha256'}；声明了 sha256 时必须精确匹配。verified=True 只代表
    “该文件此刻在磁盘上且哈希一致”，不代表其内容是合格报告。"""
    read_bytes = read_bytes or _disk_read
    if ref is None:
        return {'path': None, 'exists': False, 'verified': False,
                'note': 'no reference supplied'}
    if isinstance(ref, dict):
        path = ref.get('path')
        declared = ref.get('sha256')
    else:
        path, declared = ref, None
    if not isinstance(path, str) or not path.strip():
        return {'path': path, 'exists': False, 'verified': False,
                'note': 'malformed reference (path missing)'}
    data = read_bytes(path)
    if data is None:
        return {'path': path, 'exists': False, 'verified': False,
                'note': 'referenced file does not exist on disk; absence is preserved, '
                        'nothing is fabricated in its place'}
    actual = _sha(data)
    verified = declared is None or declared == actual
    return {'path': path, 'exists': True, 'sha256': actual,
            'declared_sha256': declared, 'verified': verified,
            'note': ('sha matches the declared digest' if verified else
                     'sha does NOT match the declared digest; treat as tampered')}


# ---------------------------------------------------------------- 已登记测试证据
def _recorded_fingerprint(prior, argv, cwd_norm, registered_inputs, read_bytes,
                          workspace):
    """用**登记时记录在案的** input 哈希复算指纹（不读当前源文件）：与
    execution_control.compute_fingerprint 的 canonical 口径逐字段一致（argv/cwd/
    files/env），只是把 files 的取值换成 prior['inputs'] 里保存的历史哈希。指纹自洽
    → 历史证据未被伪造；当前输入是否漂移由 test_inputs_stale 独立判定（S3-safety：
    历史绿测文件 SHA 变化保留历史已验证，不以当前输入抹掉原验证）。

    集合边界（S4 缺陷 5）：prior['inputs'] 必须**精确等于** registered_inputs 集合，
    且每个 recorded/registered input 都解析在真实 workspace 内；额外/缺失/越界/
    junction 逃逸的 input 一律拒绝（否则可用“合法 SHA + 额外外部路径”伪造旧指纹，
    或读取 workspace 外的 bytes）。绝不读外部路径，只用登记在案哈希复算。"""
    recorded_by_real = {}
    for key, value in (prior.get('inputs') or {}).items():
        if not isinstance(key, str) or not _is_hex(value):
            return None, 'recorded inputs contain a malformed path/hash entry'
        if _norm_within(workspace, key) is None:
            return None, (f'recorded input {key!r} resolves outside the authorized '
                          f'workspace; an external path can never prove the historical '
                          f'run and is not read')
        recorded_by_real[os.path.normcase(os.path.realpath(key))] = value
    registered_real = {}
    for rel in registered_inputs:
        norm = _norm_within(workspace, rel)
        if norm is None:
            return None, (f'registered input {rel!r} resolves outside the authorized '
                          f'workspace (realpath); refusing')
        registered_real[os.path.normcase(os.path.realpath(str(rel)))] = norm
    if set(recorded_by_real) != set(registered_real):
        extra = sorted(set(recorded_by_real) - set(registered_real))
        missing = sorted(set(registered_real) - set(recorded_by_real))
        return None, ('recorded test inputs do not EXACTLY match the registered input '
                      f'set (extra={extra}, missing={missing}); a subset, an extra '
                      f'external input, or a junction-drifted path cannot prove the '
                      f'historical run')
    files = {}
    for rel in registered_inputs:
        rk = os.path.normcase(os.path.realpath(str(rel)))
        files[os.path.normpath(str(rel))] = recorded_by_real[rk]
    env = prior.get('env_sha256')
    runtime_version = prior.get('runtime_version')
    if not _is_hex(env) or not isinstance(runtime_version, str) or not runtime_version:
        return None, 'test evidence env_sha256/runtime_version malformed'
    payload = {'argv': [str(a) for a in argv], 'cwd': _norm_dir(cwd_norm),
               'files': files,
               'env': dict(sorted({'env_sha256': env,
                                   'runtime_version': runtime_version}.items()))}
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                       separators=(',', ':'))
    return _sha(canon.encode('utf-8')), None


def _is_hex(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in '0123456789abcdefABCDEF' for c in value))


def _norm_dir(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def bind_registered_test(evidence_path, workspace, registered_inputs,
                         read_bytes=None) -> dict:
    """装载并核验**既有** run_registered_test evidence.json（缺陷 K + S3-safety）。

    这里判定的是历史证据的**内部完整性/未被伪造**：name/argv/cwd 一致、exit_code 严格
    整数 0、inputs_unchanged 为真、inputs 非空、指纹可用登记哈希自洽复算、stdout.log/
    stderr.log 兄弟文件回读 SHA 与记录一致。缺证据/空 inputs/退出码伪布尔/日志被动过/
    指纹不符 → invalid。当前输入文件是否漂移**不**在这里否定历史验证，而由
    test_inputs_stale 独立判定（保留“历史已验证 + 当前 stale”）。绝不重读当前源造新
    指纹冒充历史成功。evidence_path 必须解析在授权工作区内。"""
    read_bytes = read_bytes or _disk_read
    norm = _norm_within(workspace, evidence_path)
    if norm is None:
        return {'valid': False, 'status': 'invalid_evidence_location',
                'reason': 'test evidence path escapes the authorized workspace',
                'evidence_path': str(evidence_path)}
    raw = read_bytes(norm)
    if raw is None:
        return {'valid': False, 'status': 'invalid_missing_evidence',
                'reason': 'registered test evidence file missing on disk',
                'evidence_path': str(evidence_path)}
    try:
        prior = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        return {'valid': False, 'status': 'invalid_evidence_parse',
                'reason': f'registered test evidence unreadable: {exc}',
                'evidence_path': str(evidence_path)}
    if not isinstance(prior, dict):
        return {'valid': False, 'status': 'invalid_evidence_parse',
                'reason': 'registered test evidence must be a JSON object',
                'evidence_path': str(evidence_path)}
    if not isinstance(registered_inputs, list) or not registered_inputs \
            or any(not isinstance(i, str) or not i.strip() for i in registered_inputs):
        return {'valid': False, 'status': 'invalid_registered_inputs',
                'reason': 'registered_inputs (original registration path strings) are '
                          'required and must be non-empty; an empty input set can '
                          'never prove a historical pass',
                'evidence_path': str(evidence_path)}
    exit_code = prior.get('exit_code')
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code != 0:
        return {'valid': False, 'status': 'invalid_evidence_exit_code',
                'reason': f'exit_code {exit_code!r} is not a strict integer 0',
                'evidence_path': str(evidence_path)}
    if prior.get('inputs_unchanged') is not True:
        return {'valid': False, 'status': 'invalid_evidence_inputs_changed',
                'reason': 'test evidence does not confirm inputs unchanged across the run',
                'evidence_path': str(evidence_path)}
    cwd_raw = prior.get('cwd')
    cwd_norm = _norm_within(workspace, cwd_raw) if isinstance(cwd_raw, str) else None
    if cwd_norm is None:
        return {'valid': False, 'status': 'invalid_evidence_cwd',
                'reason': 'test evidence cwd does not resolve inside the workspace',
                'evidence_path': str(evidence_path)}
    recomputed, err = _recorded_fingerprint(prior, prior.get('argv') or [], cwd_norm,
                                           registered_inputs, read_bytes, workspace)
    if err is not None:
        return {'valid': False, 'status': 'invalid_evidence_inputs', 'reason': err,
                'evidence_path': str(evidence_path)}
    fingerprint = prior.get('fingerprint')
    if not _is_hex(fingerprint) or fingerprint != recomputed:
        return {'valid': False, 'status': 'invalid_evidence_fingerprint',
                'reason': 'test evidence fingerprint does not match the recorded '
                          'argv/cwd/inputs/env (recorded inputs are required, current '
                          'source drift is judged separately as stale)',
                'evidence_path': str(evidence_path)}
    folder = os.path.dirname(norm)
    for key in ('stdout_sha256', 'stderr_sha256'):
        digest = prior.get(key)
        if not _is_hex(digest):
            return {'valid': False, 'status': 'invalid_evidence_log',
                    'reason': f'test evidence {key} is malformed',
                    'evidence_path': str(evidence_path)}
        log_norm = _norm_within(workspace, os.path.join(folder, key.split('_')[0] + '.log'))
        if log_norm is None:
            return {'valid': False, 'status': 'invalid_evidence_log',
                    'reason': f'test log escapes workspace: {key}',
                    'evidence_path': str(evidence_path)}
        data = read_bytes(log_norm)
        if data is None or _sha(data) != digest:
            return {'valid': False, 'status': 'invalid_evidence_log',
                    'reason': f'test log missing or tampered: {log_norm}',
                    'evidence_path': str(evidence_path)}
    return {'valid': True, 'status': 'verified',
            'reason': 'historical registered-test evidence is internally consistent '
                      '(name/argv/cwd/exit 0/inputs_unchanged/fingerprint from recorded '
                      'inputs/log SHA); current input drift is reported separately as '
                      'stale and never erases this verification',
            'evidence_path': str(evidence_path),
            'workspace_realpath': os.path.normcase(os.path.realpath(str(workspace))),
            'evidence': {'name': prior.get('name'), 'argv': prior.get('argv'),
                         'cwd': prior.get('cwd'), 'exit_code': prior.get('exit_code'),
                         'inputs_unchanged': prior.get('inputs_unchanged'),
                         'inputs': prior.get('inputs'),
                         'fingerprint': prior.get('fingerprint'),
                         'env_sha256': prior.get('env_sha256'),
                         'runtime_version': prior.get('runtime_version'),
                         'stdout_sha256': prior.get('stdout_sha256'),
                         'stderr_sha256': prior.get('stderr_sha256')},
            'bound_at_utc': _now()}


def test_inputs_stale(test_binding: dict, current_read_bytes=None) -> dict:
    """当前输入与已核验测试证据的匹配状态（缺陷 K）：无效/空 inputs 一律不可继承
    （stale=True 且 valid=False），绝不把“没输入”当成“没改过”。"""
    read_bytes = current_read_bytes or _disk_read
    if not isinstance(test_binding, dict) or not test_binding.get('valid'):
        return {'valid': False, 'stale': True,
                'status': (test_binding or {}).get('status', 'invalid'),
                'reason': (test_binding or {}).get(
                    'reason', 'no valid registered-test binding'),
                'note': 'invalid or missing test evidence cannot be inherited as pass'}
    inputs = (test_binding.get('evidence') or {}).get('inputs')
    if not isinstance(inputs, dict) or not inputs:
        return {'valid': False, 'stale': True, 'status': 'invalid_empty_inputs',
                'note': 'empty inputs can never prove freshness; old green tests are '
                        'not inheritable'}
    # 只在授权 workspace 内回读：越界/junction 逃逸的 input 一律视为漂移（stale），
    # 绝不读取 workspace 外的 bytes（S4 缺陷 5）。
    ws_root = test_binding.get('workspace_realpath')
    mismatches = []
    missing = []
    external = []
    for path, digest in inputs.items():
        if isinstance(ws_root, str) and _norm_within(ws_root, path) is None:
            external.append(path)
            continue
        data = read_bytes(path)
        if data is None:
            missing.append(path)
        elif _sha(data) != digest:
            mismatches.append(path)
    stale = bool(mismatches or missing or external)
    return {'valid': True, 'stale': stale, 'mismatched_inputs': mismatches,
            'missing_inputs': missing, 'external_inputs': external,
            'note': 'old green tests do not carry over once inputs changed or an '
                    'input escapes the workspace' if stale
                    else 'inputs still match the verified test evidence'}


# ---------------------------------------------------------------- 工具回执
def extract_write_receipts(events, session_id=None, turn_id=None) -> list:
    """从 ZCode events.jsonl 结构提取成功 Write/Edit 回执（缺陷 J + S4 缺陷 9）：
    tool_call_scheduled 携带非空 toolCallId/toolName/input.file_path，同 id 的
    tool_call_result result.success 严格为 true 才算成功回执。

    当前 session 关联：调用方给出 session_id/turn_id 时事件必须严格等于该值才算当前
    session 成功——缺 sessionId/turnId 的事件不再当当前 session（旧口径误把 None 也放行）；
    若调用方未给任何 session/turn，则 identity_verified=False（unverified），绝不凭缺身份
    字段冒充当前 session/turn。附带事件来源（文件 + 行号），不凭报告关键词。"""
    scheduled = {}
    results = {}
    session_bound = session_id is not None
    turn_bound = turn_id is not None
    # S4/S5 缺陷 5：身份核验必须 session 与 turn **两者齐全**且分别严格匹配 scheduled/
    # result，任一缺失（缺 turn、或只有旧 session）一律 unverified，绝不以其中一项冒充。
    both_bound = session_bound and turn_bound
    for line_no, event in enumerate(events or [], start=1):
        if not isinstance(event, dict):
            continue
        if session_id is not None and event.get('sessionId') != session_id:
            continue
        if turn_id is not None and event.get('turnId') != turn_id:
            continue
        # 记录每个 scheduled 的 session/turn 身份，供与 result 严格配对。
        etype = event.get('type')
        payload = event.get('payload')
        if not isinstance(payload, dict):
            continue
        call_id = payload.get('toolCallId')
        if not isinstance(call_id, str) or not call_id:
            continue
        if etype == 'tool_call_scheduled':
            if payload.get('toolName') in WRITE_TOOLS:
                tool_input = payload.get('input')
                file_path = tool_input.get('file_path') \
                    if isinstance(tool_input, dict) else None
                scheduled[call_id] = (payload.get('toolName'), file_path, line_no)
        elif etype == 'tool_call_result':
            result = payload.get('result')
            results[call_id] = isinstance(result, dict) \
                and result.get('success') is True
    receipts = []
    for call_id, (tool, file_path, line_no) in scheduled.items():
        receipts.append({'tool': tool, 'tool_use_id': call_id,
                         'file_path': file_path,
                         'successful': results.get(call_id) is True,
                         'identity_verified': bool(both_bound),
                         'source': 'events.jsonl',
                         'source_line': line_no})
    receipts.sort(key=lambda r: str(r['tool_use_id']))
    return receipts


# ---------------------------------------------------------------- 交接包
def _receipts_for_target(receipts, target):
    """落在同一物理 target 上的全部写回执（含失败），仅示尝试；成功与否由
    successful 字段区分，不在这里判定。"""
    if target is None:
        return []
    return [r for r in receipts
            if isinstance(r.get('file_path'), str)
            and os.path.normcase(os.path.realpath(r['file_path'])) == target]


def build_handoff(frozen: dict, evaluation: dict, test_binding: dict | None,
                  test_stale: dict | None, todos, *, original_report_ref=None,
                  original_error_ref=None, write_receipts=None,
                  terminal_state=None, read_bytes=None) -> dict:
    """紧凑交接包（缺陷 I/J/M）：文件清单 + 前后 SHA +（有副本时的）真实 diff 由
    调用方随包写入；测试证据核验状态 + 成功 Edit/Write 原始回执关联（按规范化
    target 匹配到声明文件）+ 待办 + **核验过的**原始证据引用。没有最终 worker
    report 就保留缺失，绝不补写；接续摘要本身不冒充原报告。"""
    read_bytes = read_bytes or _disk_read
    report_verified = verify_ref(original_report_ref, read_bytes)
    error_verified = verify_ref(original_error_ref, read_bytes)
    receipts = []
    for receipt in write_receipts or []:
        receipts.append(dict(receipt))
    files = {}
    for rel, entry in (evaluation.get('files') or {}).items():
        target = entry.get('target')
        # 只有成功 + 身份已核验（当前 session/turn 严格匹配）的回执才算写关联；失败
        # Write/Edit（successful 不为 True）或缺 session/turn 身份（identity_verified 不为
        # True）绝不冒充当前 session 成功关联（S3-safety + S4 缺陷 9）。全部回执仍随包保留
        # 以示尝试，但 linked 只取成功且身份核验的。
        linked = [r for r in receipts
                  if r.get('successful') is True
                  and r.get('identity_verified') is True
                  and isinstance(r.get('file_path'), str) and target is not None
                  and os.path.normcase(os.path.realpath(r['file_path'])) == target]
        files[rel] = {'baseline_sha256': entry.get('baseline_sha256'),
                      'current_sha256': entry.get('current_sha256'),
                      'changed': entry.get('changed'),
                      'missing_now': entry.get('missing_now'),
                      'target_drift': entry.get('target_drift'),
                      'physical_target': target,
                      'write_receipts': _receipts_for_target(receipts, target),
                      'successful_write_receipts': linked,
                      'write_receipt_linked': bool(linked)}
    return {'handoff_kind': 'continuation-contract',
            'workspace_realpath': frozen.get('workspace_realpath'),
            'frozen_at_utc': frozen.get('frozen_at_utc'),
            'evaluated_at_utc': evaluation.get('evaluated_at_utc'),
            'terminal_state': terminal_state,
            'files': files,
            'any_changed': evaluation.get('any_changed'),
            'files_declared': evaluation.get('files_declared'),
            'test_evidence': test_binding,
            'test_inputs_stale': test_stale,
            'original_report_ref': report_verified,
            'original_error_ref': error_verified,
            'final_worker_report_present': bool(report_verified.get('verified')),
            'todos': list(todos or []),
            'note': 'next executor continues from frozen/existing outputs; do not '
                    'roll back or rewrite the batch; undeclared files are NOT claimed '
                    'frozen; a missing final report stays missing (never fabricated); '
                    'stale old-green tests must be re-run; this handoff is a '
                    'continuation summary and never impersonates the original report',
            'built_at_utc': _now()}
