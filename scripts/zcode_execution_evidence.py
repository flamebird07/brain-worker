"""ZCode 执行证据门禁（成果 B）：最小任务执行契约 × 同 session/turn 工具事件 × 磁盘回读。

边界（与 references/zcode-direct.md 一致）：
- 不改变 protocol_success / report_bound / business_verified 的含义；本模块只产出
  execution_evidence_ok 与具体理由，business_verified 仍由主脑独立验收；
- 没有任务执行契约时显式返回 None（未验），绝不默认 true；
- 只接受同 sessionId/turnId 的事件，结果事件无 toolName，必须按 toolCallId 关联；
  调度 + 成功回执即可构成证据，不强制 started 事件，也不把流式事件数当动作数；
- result.success 必须是严格布尔：缺失、字符串、数值、null 一律 malformed，失败回执
  绝不能因 truthy 而变成功；payload/toolCallId malformed 也干净拒绝；
- 路径归属用 realpath+normcase 完整规范化精确比较，拒绝邻居前缀、`..`、绝对越界
  与符号链接逃逸；观察到的越界读写、越界日志路径直接拒绝，无任何越界 fallback 读取；
- “必需修改的已有文件”以派工前 baseline 哈希快照核对（成功写回执 + 与快照不同的
  结果哈希）；expected_artifacts 声明纯新产物（派工前必须不存在，旧文件不能充当）；
- 测试主张复用 execution_control.run_registered_test / compute_fingerprint 的真实
  结构：精确 name/argv/cwd、exit_code 严格整数 0、inputs_unchanged is True、已登记
  inputs 的当前哈希、env_sha256/runtime_version 与 fingerprint 内部一致、日志 SHA 与
  路径归属。两套路径口径分开：哈希归属比较用 normcase(realpath) 规范化键；指纹复算
  用契约 test_evidence.inputs 提供的原登记路径字符串（producer 的 files 键是
  os.path.normpath(原登记路径)，保留大小写）。required_test_command 与 argv 的单一
  精确口径是 subprocess.list2cmdline(argv)，矛盾在派工前拒绝；
- 所有证据/字段坏类型或缺失返回具体未验/失败理由，不抛未处理异常；
- 摘要不含源码、令牌或完整工具内容；turn_complete.toolCallCount 只是计数线索。
"""
import hashlib
import json
import os
import subprocess

import execution_control as ec

CONTRACT_TASK_TYPES = ('engineering', 'review_no_change', 'reasoning')
ALLOWED_CONTRACT_KEYS = {'task_type', 'required_reads', 'expected_artifacts',
                         'required_modified_files', 'allow_no_changes',
                         'required_test_command', 'test_evidence'}
PATH_LIST_KEYS = ('required_reads', 'expected_artifacts', 'required_modified_files')
TEST_EVIDENCE_KEYS = {'name', 'argv', 'cwd', 'evidence_path', 'inputs'}
SCHEDULED = 'tool_call_scheduled'
RESULT = 'tool_call_result'
ERROR = 'tool_call_error'
ERROR_TEXT_FIELDS = ('code', 'type', 'message', 'detail', 'stack')
WRITE_TOOLS = ('Write', 'Edit')
READ_TOOL = 'Read'
PERMISSION_MARKERS = ('permission', 'not allowed', 'denied', 'unauthorized')


def load_execution_contract(path) -> dict:
    """读取契约 JSON；任何读取/解码/结构错误统一抛 ValueError（含文件缺失）。"""
    try:
        with open(path, 'rb') as handle:
            raw = handle.read()
    except OSError as exc:
        raise ValueError(f'execution contract unreadable: {exc}')
    try:
        contract = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f'execution contract unreadable: {exc}')
    if not isinstance(contract, dict):
        raise ValueError('execution contract must be a JSON object')
    return contract


def norm_within(workspace, path):
    """完整规范化（realpath+normcase）后判定是否落在工作区内；越界返回 None。

    兼容 Windows 盘符/反斜杠/正斜杠；拒绝邻居前缀（`wsx` vs `ws`）、`..` 解析越界
    与符号链接/目录联接逃逸。"""
    root = os.path.normcase(os.path.realpath(str(workspace)))
    joined = os.path.join(str(workspace), str(path))
    candidate = os.path.normcase(os.path.realpath(joined))
    if candidate == root or candidate.startswith(root + os.sep):
        return candidate
    return None


def command_caliber(argv) -> str:
    """required_test_command 与 argv 绑定的单一精确口径：list2cmdline。"""
    return subprocess.list2cmdline([str(a) for a in argv])


def contract_sha256(contract: dict) -> str:
    canon = json.dumps(contract, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'))
    return hashlib.sha256(canon.encode('utf-8')).hexdigest()


def build_baseline(contract: dict, workspace, read_bytes) -> dict:
    """派工前快照：契约涉及的全部路径 exists/sha256，随 request 留证。"""
    baseline = {}
    for key in PATH_LIST_KEYS:
        for rel in contract.get(key) or []:
            norm = norm_within(workspace, rel)
            data = read_bytes(norm) if norm else None
            baseline[norm or str(rel)] = {
                'exists': data is not None,
                'sha256': hashlib.sha256(data).hexdigest() if data is not None else None,
            }
    return baseline


def _path_list(contract, key) -> list:
    value = contract.get(key, [])
    if not isinstance(value, list) or any(
            not isinstance(p, str) or isinstance(p, bool) or not p.strip() for p in value):
        raise ValueError(f'execution contract {key!r} must be a list of non-blank '
                         'string paths (booleans are not paths)')
    return value


def _validate_test_evidence_spec(evidence, workspace, reasons):
    """test_evidence 声明的严格类型/路径校验；问题并入 reasons。"""
    if not isinstance(evidence, dict) or set(evidence) != TEST_EVIDENCE_KEYS:
        reasons.append('test_evidence must be an object with exactly '
                       'name/argv/cwd/evidence_path/inputs')
        return
    if not isinstance(evidence['name'], str) or not evidence['name'].strip():
        reasons.append('test_evidence name must be a non-empty string')
    if (not isinstance(evidence['argv'], list) or not evidence['argv']
            or any(not isinstance(a, str) or not a for a in evidence['argv'])):
        reasons.append('test_evidence argv must be a non-empty list of strings')
    for key in ('cwd', 'evidence_path'):
        if not isinstance(evidence[key], str) or not evidence[key].strip():
            reasons.append(f'test_evidence {key} must be a non-empty string')
        elif norm_within(workspace, evidence[key]) is None:
            reasons.append(f'test_evidence {key} escapes workspace')
    inputs = evidence['inputs']
    if (not isinstance(inputs, list) or not inputs
            or any(not isinstance(p, str) or isinstance(p, bool) or not p.strip()
                   for p in inputs)):
        reasons.append('test_evidence inputs must be a non-empty list of '
                       'non-blank string paths (the declared input set)')
    else:
        for rel in inputs:
            norm = norm_within(workspace, rel)
            if norm is None:
                reasons.append(f'test_evidence input escapes workspace: {rel!r}')
            elif not os.path.isfile(norm):
                reasons.append(f'test_evidence input missing on disk: {rel!r}')


def validate_execution_contract(contract: dict, workspace) -> list:
    """派工前机械校验：未知字段、伪值、越界/缺失/矛盾一律拒绝。"""
    reasons = []
    unknown = sorted(set(contract) - ALLOWED_CONTRACT_KEYS)
    if unknown:
        reasons.append(f'unknown contract field(s): {unknown}')
    task_type = contract.get('task_type')
    if task_type not in CONTRACT_TASK_TYPES:
        reasons.append(f'task_type must be one of {", ".join(CONTRACT_TASK_TYPES)}')
        task_type = None
    try:
        lists = {key: _path_list(contract, key) for key in PATH_LIST_KEYS}
    except ValueError as exc:
        return reasons + [str(exc)]
    for key, paths in lists.items():
        for rel in paths:
            norm = norm_within(workspace, rel)
            if norm is None:
                reasons.append(f'{key} path escapes workspace: {rel!r}')
            elif key in ('required_reads', 'required_modified_files') and not os.path.isfile(norm):
                reasons.append(f'{key} path missing on disk: {rel!r}')
            elif key == 'expected_artifacts' and os.path.exists(norm):
                reasons.append(f'expected_artifacts path already exists (declare new '
                               f'artifacts only; existing files belong in '
                               f'required_modified_files): {rel!r}')
    if 'allow_no_changes' in contract and not isinstance(contract['allow_no_changes'], bool):
        reasons.append('allow_no_changes must be a boolean')
    command = contract.get('required_test_command')
    if command is not None and (not isinstance(command, str) or not command.strip()):
        reasons.append('required_test_command must be a non-empty string when present')
    evidence = contract.get('test_evidence')
    if evidence is not None:
        _validate_test_evidence_spec(evidence, workspace, reasons)
    # required_test_command 与 argv 的单一精确口径绑定，矛盾派工前拒绝。
    if isinstance(command, str) and isinstance(evidence, dict) \
            and isinstance(evidence.get('argv'), list) and evidence['argv'] \
            and all(isinstance(a, str) for a in evidence['argv']):
        if command != command_caliber(evidence['argv']):
            reasons.append('required_test_command does not equal '
                           'subprocess.list2cmdline(test_evidence.argv); fix the '
                           'contract, the command is not an optional annotation')
    if task_type == 'reasoning' and any(lists[k] for k in PATH_LIST_KEYS):
        reasons.append('reasoning task cannot demand reads, artifacts or modified files')
    if task_type == 'reasoning' and (command is not None or evidence is not None):
        reasons.append('reasoning task cannot demand test evidence')
    if task_type == 'review_no_change' and (lists['expected_artifacts']
                                            or lists['required_modified_files']):
        reasons.append('review_no_change cannot expect artifact changes')
    if contract.get('allow_no_changes') and (lists['expected_artifacts']
                                             or lists['required_modified_files']):
        reasons.append('allow_no_changes contradicts expected artifacts or modified files')
    if task_type == 'engineering' and not (lists['expected_artifacts']
                                           or lists['required_modified_files']) \
            and contract.get('allow_no_changes') is not True:
        reasons.append('engineering contract declares no write outcomes '
                       '(expected_artifacts/required_modified_files); a read-only '
                       'engineering claim must explicitly set allow_no_changes=true, '
                       'regardless of required_reads')
    return reasons


def _is_permission_denial(result_payload) -> bool:
    if not isinstance(result_payload, dict):
        return False
    content = result_payload.get('content')
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return any(marker in text.lower() for marker in PERMISSION_MARKERS)


def _result_identity(payload):
    """执行身份：(严格布尔 success, result 内容稳定序列化)；None 表示 malformed。

    success 必须是严格 bool：'false' 字符串、1、null 等一律 malformed，不能靠
    truthy 把失败回执变成成功。"""
    result = payload.get('result')
    if not isinstance(result, dict) or not isinstance(result.get('success'), bool):
        return None
    canon = json.dumps(result, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'))
    return (result['success'], canon)


def _error_identity(payload):
    """明确失败终态：按已观察 schema 严格归一化 tool_call_error → (False, canon)。

    error 必须是 dict；已知文本字段 code/type/detail/stack **若存在**须为字符串，
    否则视为畸形；必须带**非空 message 字符串**才算有效错误内容。缺 toolCallId、
    error 非 dict、无有效错误内容或已知字段类型错误一律返回 None（malformed）——
    绝不接受 {}、只有陌生键的 dict 或非字符串已知字段来伪造终态。合法 message-only
    错误仍归失败（不是成功）。未知附加元数据被忽略，不进入规范正文、也不充当错误内容。"""
    err = payload.get('error')
    if not isinstance(err, dict):
        return None
    for key in ERROR_TEXT_FIELDS:
        if key in err and not isinstance(err[key], str):
            return None
    message = err.get('message')
    if not isinstance(message, str) or not message.strip():
        return None
    canon = json.dumps({k: err[k] for k in ERROR_TEXT_FIELDS if k in err},
                       ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return (False, canon)


def _error_is_permission_denial(payload) -> bool:
    """只查明确的 code/type 与约定 message/detail 文本字段判权限拒绝。

    排除 stack、未知键名与无关元数据（如 {'permission': False}），避免键名或
    stack 里的 'PermissionBroker' 之类噪音误判。真正明确的权限拒绝仍归 denied，
    即便外层 is_error=false 也不掩盖。"""
    err = payload.get('error')
    if not isinstance(err, dict):
        return False
    fields = (err.get('code'), err.get('type'), err.get('message'), err.get('detail'))
    text = ' '.join(v.lower() for v in fields if isinstance(v, str))
    return any(marker in text for marker in PERMISSION_MARKERS)


def _is_hex64(value) -> bool:
    return isinstance(value, str) and len(value) == 64 \
        and all(c in '0123456789abcdef' for c in value)


def _check_test_evidence(evidence_spec, workspace, read_bytes) -> tuple:
    """按 execution_control.run_registered_test 的 evidence.json 真实结构校验。

    返回 (ok, reason)。任何坏类型/缺字段返回具体原因而不是抛异常；绝不发生越界
    fallback 读取，日志 realpath 必须在工作区内。"""
    if not isinstance(evidence_spec, dict) or set(evidence_spec) != TEST_EVIDENCE_KEYS:
        return False, 'malformed test_evidence spec (needs exactly name/argv/cwd/' \
                      'evidence_path/inputs)'
    try:
        norm = norm_within(workspace, evidence_spec['evidence_path'])
        if norm is None:
            return False, 'test evidence path escapes workspace'
        raw = read_bytes(norm)
        if raw is None:
            return False, 'test evidence file missing on disk'
        prior = json.loads(raw.decode('utf-8'))
        if not isinstance(prior, dict):
            return False, 'test evidence must be a JSON object'
    except (ValueError, OSError, UnicodeDecodeError, TypeError, KeyError) as exc:
        return False, f'test evidence unreadable: {exc}'
    if prior.get('name') != evidence_spec['name']:
        return False, (f"test evidence name {prior.get('name')!r} does not match the "
                       f"declared {evidence_spec['name']!r}")
    if prior.get('argv') != evidence_spec['argv']:
        return False, 'test evidence argv does not exactly match the declared argv'
    cwd_norm = norm_within(workspace, evidence_spec['cwd'])
    if cwd_norm is None or not isinstance(prior.get('cwd'), str) \
            or os.path.normcase(os.path.realpath(prior['cwd'])) != cwd_norm:
        return False, 'test evidence cwd does not match the declared cwd'
    exit_code = prior.get('exit_code')
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code != 0:
        return False, f'test evidence exit_code is {exit_code!r}, not strict integer 0'
    if prior.get('inputs_unchanged') is not True:
        return False, 'test evidence does not confirm inputs unchanged across the run'
    # 已登记 inputs 的当前哈希：哈希归属比较用 normcase(realpath) 规范化键；指纹复算
    # 则必须用契约提供的原登记路径字符串（producer compute_fingerprint 的 files 键是
    # os.path.normpath(原登记路径)，保留大小写），两套口径不得混用。
    declared_raw = []
    declared_inputs = {}
    for rel in evidence_spec['inputs']:
        norm_in = norm_within(workspace, rel)
        data = read_bytes(norm_in) if norm_in else None
        if data is None:
            return False, f'declared test input missing on disk: {rel!r}'
        declared_inputs[norm_in] = hashlib.sha256(data).hexdigest()
        declared_raw.append(str(rel))
    recorded = prior.get('inputs')
    if not isinstance(recorded, dict) or not recorded:
        return False, 'test evidence inputs must be a non-empty object'
    recorded_norm = {}
    for key, value in recorded.items():
        if not isinstance(key, str) or not _is_hex64(value):
            return False, 'test evidence inputs contain a malformed path/hash entry'
        recorded_norm[os.path.normcase(os.path.realpath(key))] = value
    if recorded_norm != declared_inputs:
        return False, 'test evidence inputs do not match the declared input set hashes'
    env_sha = prior.get('env_sha256')
    runtime_version = prior.get('runtime_version')
    fingerprint = prior.get('fingerprint')
    if not _is_hex64(env_sha) or not isinstance(runtime_version, str) \
            or not runtime_version:
        return False, 'test evidence env_sha256/runtime_version malformed'
    if not _is_hex64(fingerprint):
        return False, f'test evidence fingerprint {fingerprint!r} is not a sha256 hex string'
    try:
        expected_fp = ec.compute_fingerprint(
            evidence_spec['argv'], cwd_norm, declared_raw,
            {'env_sha256': env_sha, 'runtime_version': runtime_version})
    except (OSError, ValueError, TypeError) as exc:
        return False, f'fingerprint recomputation failed: {exc}'
    if fingerprint != expected_fp:
        return False, 'test evidence fingerprint does not match argv/cwd/inputs/env'
    folder = os.path.dirname(norm)
    if norm_within(workspace, folder) is None:
        return False, 'test evidence folder escapes workspace'
    for key in ('stdout_sha256', 'stderr_sha256'):
        digest = prior.get(key)
        if not _is_hex64(digest):
            return False, f'test evidence {key} is malformed'
        log_norm = norm_within(workspace, os.path.join(folder, key.split('_')[0] + '.log'))
        if log_norm is None:
            return False, f'test log escapes workspace: {key}'
        data = read_bytes(log_norm)
        if data is None:
            return False, f'test log missing: {log_norm}'
        if hashlib.sha256(data).hexdigest() != digest:
            return False, f'test log sha mismatch or tampered: {log_norm}'
    return True, 'registered test evidence verified (name/argv/cwd/exit 0/inputs/' \
                 'fingerprint/log SHA)'


def evaluate_execution_evidence(events, contract, *, session_id, turn_id,
                                workspace, read_bytes, baseline=None,
                                preflight_only=False) -> dict:
    """核对契约 × 同 session/turn 工具事件 × 磁盘回读，产出执行证据结论。

    baseline 是派工前快照（build_baseline），由入口随 request 留证，不依赖执行后
    才读取的契约文件。返回 execution_evidence_ok（True/False/None）与 status。
    """
    out = {'execution_evidence_ok': None, 'execution_evidence_status': None,
           'reasons': [], 'note': 'mechanical evidence gate only; natural-language '
           'claims remain for brain-side review; business_verified stays with the brain'}
    if contract is None:
        out['execution_evidence_status'] = 'unverified_no_contract'
        out['reasons'] = ['no execution contract supplied; explicitly unverified, '
                          'never defaults to true']
        return out
    if preflight_only:
        out['execution_evidence_status'] = 'unverified_preflight_only'
        out['reasons'] = ['contract supplied but preflight-only run submits no prompt; '
                          'real execution is not claimed']
        return out
    if not session_id or not turn_id:
        out['execution_evidence_status'] = 'unverified_no_session_turn'
        out['reasons'] = ['runner envelope lacks session_id/turn_id; tool events '
                          'cannot be attributed']
        return out
    if baseline is None:
        out['execution_evidence_status'] = 'unverified_no_baseline'
        out['reasons'] = ['dispatch-time baseline snapshot missing; change proof is '
                          'not possible']
        return out

    turn_events = [e for e in events
                   if isinstance(e, dict) and e.get('sessionId') == session_id
                   and e.get('turnId') == turn_id]
    scheduled = {}
    results = {}
    malformed_events = 0
    for event in turn_events:
        etype = event.get('type')
        if etype not in (SCHEDULED, RESULT, ERROR):
            continue
        payload = event.get('payload')
        if not isinstance(payload, dict):
            malformed_events += 1
            continue
        call_id = payload.get('toolCallId')
        if not isinstance(call_id, str) or not call_id:
            malformed_events += 1
            continue
        if etype == SCHEDULED:
            if not isinstance(payload.get('toolName'), str):
                malformed_events += 1
                continue
            identity = (payload['toolName'], json.dumps(payload.get('input'),
                                                        ensure_ascii=False,
                                                        sort_keys=True))
            if call_id in scheduled and scheduled[call_id] != identity:
                out.update(execution_evidence_ok=False,
                           execution_evidence_status='tool_result_conflict',
                           reasons=[f'conflicting duplicate {SCHEDULED} for toolCallId '
                                    f'{call_id}'])
                return out
            scheduled[call_id] = identity
            continue
        if etype == RESULT:
            # 严格布尔成功检查保持不变：非严格 bool 的 result.success 视为 malformed。
            identity = _result_identity(payload)
            if identity is None:
                out.update(execution_evidence_ok=False,
                           execution_evidence_status='tool_result_malformed',
                           reasons=['a tool_call_result lacks a strictly boolean '
                                    'result.success; it cannot serve as evidence'])
                return out
            entry = ('result', identity, payload)
        else:
            # tool_call_error 是明确失败终态（无 result.success）：归一化为失败，
            # 与 scheduled 配对，绝不因缺 result 而当 missing，也绝不当成功。
            identity = _error_identity(payload)
            if identity is None:
                malformed_events += 1
                continue
            entry = ('error', identity, payload)
        if call_id in results:
            # 只按事件 kind + 规范终态 identity 比较：同 call 的 result/error 终态正文
            # 一致即幂等，外层计时/duration 或其它元数据不同不制造冲突；正文（identity）
            # 漂移或 kind 不同（success result 与 error 并存）一律拒绝。
            if results[call_id][:2] == entry[:2]:
                continue  # 同 call 一致重放 → 幂等
            out.update(execution_evidence_ok=False,
                       execution_evidence_status='tool_result_conflict',
                       reasons=[f'conflicting terminal events ({RESULT}/{ERROR}) for '
                                f'toolCallId {call_id}: identity drift or a success '
                                'receipt paired with an error receipt'])
            return out
        results[call_id] = entry
    if malformed_events:
        out.update(execution_evidence_ok=False,
                   execution_evidence_status='tool_result_malformed',
                   reasons=[f'{malformed_events} scheduled/result event(s) have a '
                            'malformed payload or toolCallId'])
        return out

    orphans = sorted(cid for cid in results if cid not in scheduled)
    if orphans:
        out.update(execution_evidence_ok=False,
                   execution_evidence_status='tool_result_orphan',
                   reasons=[f'{len(orphans)} result event(s) without a matching '
                            f'{SCHEDULED} cannot serve as execution evidence'])
        return out
    unresolved = sorted(cid for cid in scheduled if cid not in results)
    if unresolved:
        out.update(execution_evidence_ok=False,
                   execution_evidence_status='tool_result_missing',
                   reasons=[f'{len(unresolved)} scheduled tool call(s) without a result '
                            f'event in the same session/turn'])
        return out

    reads_ok, denied, failed, out_of_bounds = [], [], [], []
    write_evidence, undeclared_writes = [], []
    for call_id, (kind, identity, payload) in sorted(results.items()):
        success = identity[0] is True
        tool, input_json = scheduled[call_id]
        file_path = None
        try:
            parsed_input = json.loads(input_json) if input_json else {}
        except ValueError:
            parsed_input = {}
        if isinstance(parsed_input, dict):
            candidate = parsed_input.get('file_path')
            if isinstance(candidate, str):
                file_path = candidate
        if not success:
            # 明确失败：error 终态或伪布尔/失败 result。权限拒绝按内容判定
            # （error 即便不带 is_error 标志也不掩盖拒绝）。绝不升级为成功。
            perm = (_error_is_permission_denial(payload) if kind == 'error'
                    else _is_permission_denial(payload.get('result')))
            if perm:
                denied.append({'tool': tool, 'file_path': file_path})
            else:
                failed.append({'tool': tool, 'file_path': file_path})
            continue
        if tool in (READ_TOOL,) + WRITE_TOOLS and isinstance(file_path, str):
            norm = norm_within(workspace, file_path)
            if norm is None:
                out_of_bounds.append({'tool': tool, 'file_path': file_path})
                continue
            if tool == READ_TOOL:
                reads_ok.append(norm)
            else:
                disk = read_bytes(norm)
                if disk is None:
                    out.update(execution_evidence_ok=False,
                               execution_evidence_status='execution_claim_mismatch',
                               reasons=[f'successful {tool} receipt for {file_path!r} '
                                        'but the artifact is missing on disk'])
                    return out
                write_evidence.append({'file_path': norm,
                                       'sha256': hashlib.sha256(disk).hexdigest()})

    if out_of_bounds:
        out.update(execution_evidence_ok=False,
                   execution_evidence_status='execution_claim_mismatch',
                   reasons=[f'{len(out_of_bounds)} successful tool call(s) targeted '
                            'paths outside the workspace; refused without fallback reads'])
        return out
    if denied:
        out.update(execution_evidence_ok=False,
                   execution_evidence_status='permission_denied',
                   reasons=[f'{len(denied)} tool result(s) report an authorization '
                            'denial; the original refusal is reported, never converted '
                            'into GPT substitution or another executor'])
        return out
    if failed:
        out.update(execution_evidence_ok=False,
                   execution_evidence_status='tool_result_failed',
                   reasons=[f'{len(failed)} tool result(s) failed without success receipts'])
        return out

    task_type = contract.get('task_type')
    required_reads = contract.get('required_reads') or []
    artifacts = contract.get('expected_artifacts') or []
    modified = contract.get('required_modified_files') or []

    if task_type == 'reasoning':
        out.update(execution_evidence_ok=True, execution_evidence_status='verified',
                   reasons=['pure reasoning task: tools are not required and none were '
                            'needed'])
        return out

    if not scheduled:
        out.update(execution_evidence_ok=False,
                   execution_evidence_status='no_required_execution',
                   reasons=[f'{task_type} task declared required source reading but zero '
                            'tool calls occurred in the matching session/turn'])
        return out

    def _norm_contract(rel):
        return norm_within(workspace, rel)

    missing_reads = [rel for rel in required_reads
                     if _norm_contract(rel) not in reads_ok]
    if missing_reads:
        out.update(execution_evidence_ok=False,
                   execution_evidence_status='no_required_execution',
                   reasons=['required_reads without a successful Read receipt at the '
                            f'exact workspace-normalized path: {missing_reads}'])
        return out

    if task_type == 'review_no_change' and write_evidence:
        out.update(execution_evidence_ok=False,
                   execution_evidence_status='execution_claim_mismatch',
                   reasons=['review_no_change contract saw successful Write/Edit '
                            'receipts; zero writes were allowed'])
        return out

    write_paths = {w['file_path'] for w in write_evidence}
    for rel in artifacts:
        norm = _norm_contract(rel)
        if norm not in write_paths:
            out.update(execution_evidence_ok=False,
                       execution_evidence_status='execution_claim_mismatch',
                       reasons=[f'expected new artifact without a matching successful '
                            f'Write/Edit receipt: {rel!r}'])
            return out
        if read_bytes(norm) is None:
            out.update(execution_evidence_ok=False,
                       execution_evidence_status='execution_claim_mismatch',
                       reasons=[f'expected artifact missing on disk: {rel!r}'])
            return out
        if (baseline.get(norm) or {}).get('exists') is True:
            out.update(execution_evidence_ok=False,
                       execution_evidence_status='execution_claim_mismatch',
                       reasons=[f'expected new artifact existed at dispatch time; an '
                            f'old file cannot pose as a new artifact: {rel!r}'])
            return out
    for rel in modified:
        norm = _norm_contract(rel)
        if norm not in write_paths:
            out.update(execution_evidence_ok=False,
                       execution_evidence_status='execution_claim_mismatch',
                       reasons=[f'required modified file without a matching successful '
                            f'Write/Edit receipt: {rel!r}'])
            return out
        disk = read_bytes(norm)
        before = baseline.get(norm) or {}
        if disk is None:
            out.update(execution_evidence_ok=False,
                       execution_evidence_status='execution_claim_mismatch',
                       reasons=[f'required modified file missing after write: {rel!r}'])
            return out
        after_sha = hashlib.sha256(disk).hexdigest()
        if before.get('exists') is not True:
            out.update(execution_evidence_ok=False,
                       execution_evidence_status='execution_claim_mismatch',
                       reasons=[f'required modified file had no dispatch-time baseline: '
                                f'{rel!r}'])
            return out
        if after_sha == before.get('sha256'):
            out.update(execution_evidence_ok=False,
                       execution_evidence_status='execution_claim_mismatch',
                       reasons=[f'successful Write/Edit receipt for {rel!r} but the '
                                'post-run hash equals the dispatch-time baseline '
                                '(no real change)'])
            return out

    declared = {_norm_contract(rel) for rel in list(artifacts) + list(modified)}
    undeclared_writes = sorted(p for p in write_paths if p not in declared)

    command = contract.get('required_test_command')
    if command is not None:
        evidence_spec = contract.get('test_evidence')
        ok, reason = (_check_test_evidence(evidence_spec, workspace, read_bytes)
                      if isinstance(evidence_spec, dict) else (False, 'no test_evidence '
                      'declared; Bash tool events alone cannot prove the target test ran'))
        if not ok:
            out.update(execution_evidence_ok=False,
                       execution_evidence_status='tests_not_run',
                       reasons=['contract requires a test command but no verified '
                            f'registered-test evidence exists: {reason}; reported as '
                            'not run, never as a permission refusal or transport failure'])
            return out

    out.update(execution_evidence_ok=True, execution_evidence_status='verified',
               reasons=[f'required reads verified ({len(required_reads)}), declared '
                        f'writes verified ({len(artifacts) + len(modified)}), artifacts '
                        f'disk-verified with post-change hashes'],
               write_evidence=write_evidence,
               undeclared_workspace_writes=undeclared_writes,
               tool_counts={'scheduled': len(scheduled), 'reads': len(reads_ok),
                            'writes': len(write_evidence)})
    return out
