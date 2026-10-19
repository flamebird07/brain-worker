"""opencode_direct — OpenCode CLI 正式直连入口（BW-OPENCODE-20261019-S1）。

只服务两个**精确免费模型**：
  - opencode/deepseek-v4.1-flash-free
  - opencode/glm-5.3-flash-free
其它 model 在任何副作用之前拒绝；绝不自动付费回退、绝不跨模型重派。

设计边界（严格对齐本轮用户任务，不新增服务/库/表/后台重试/余额查询）：
- 容量：复用 `dispatch_pool.consume_for_entry`（executor='opencode'）→ `mark_running` →
  `bind_child`（真实 PID + 创建身份）→ 已知终态 `finish`。bind 未明确成功绝不写 stdin、
  不假释放；未知进程保留 unknown；已确认退出（含异常退出）也释放本 attempt 自己的槽。
- 传输：`shell=False`，每 run 只调用一次 CLI；argv 为
  `opencode run --pure --model opencode/<ID> --format json`，完整九节契约 + 任务原文经
  **stdin** 发送（不裁剪）。stdout/stderr/events/request/process/response/summary 全部
  落盘留证。
- 环境隔离：`OPENCODE_CONFIG_CONTENT` 使用 share=disabled、默认 `*`=deny、
  read/edit=allow、external_directory=deny、mcp={}、plugin=[]；`OPENCODE_CONFIG_DIR`、
  `XDG_CONFIG_HOME`、`XDG_DATA_HOME` 为每 run 私有空目录；禁用自动更新、Claude Code
  兼容加载与 LSP 下载。`--pure` 禁外部插件。不复制任何现有 OpenCode Go 凭据。
- 工具权限：默认仅 read/edit（原生 write 由 edit 权限控制）；配置可通过 `allowed_tools`
  显式声明额外已授权工具（如 bash），但 agent/task/subagent 一律拒绝，未知/非法工具名
  （含 `*` 与禁用名大小写变体）一律拒绝，只放行已知工具清单。绝不把纯文件 profile
  默认开启所有工具；这是整工具开关，不是逐文件沙箱；execution_control 预检的 actual
  grants 如实反映该 profile，绝不拿空 grants 冒充"实际无权限"。
- JSON 流：逐行 type=step_start/tool_use/step_finish/text。`step_finish.reason=tool-calls`
  不是完成；只有最后一个 step_finish 为 `stop` 且无顶层结构化 error 事件才协议成功。
  工具 error / 权限拒绝与顶层 error 分开记录；正常 read 正文里的 429/ECONNRESET 不误报。
  最终 `text.part.text` 才是原始报告；中间 assistant 文字原样保留，绝不删前言造绑定。
- 工程成功需要真实成功工具回执（execution_evidence_ok）；`business_verified` 恒 false，
  `report_bound` 独立；没有后端身份回执时只写 requested_model，不冒充已验证身份/免费账单。
- 失败绝不生成假九节报告。CLI exit 0 不证明任务成功。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import dispatch_pool as dp  # noqa: E402
import execution_control as ec  # noqa: E402
import prompt_contract as pc  # noqa: E402
import qoder_direct as qd  # noqa: E402

RUNTIME = dp.OPENCODE_RUNTIME
MODELS = dp.OPENCODE_MODELS
DEFAULT_MODEL = MODELS[0]
POOL_KEY = dp.OPENCODE_DEEPSEEK_POOL_KEY
DEFAULT_CONFIG = _SCRIPTS_DIR / 'opencode-entry.json'
EXAMPLE_CONFIG = _SCRIPTS_DIR / 'opencode-entry.json.example'

# 有界墙钟：OpenCode 独占一个真实槽，必须有界，避免未知 writer 永久占位。
DEFAULT_TIMEOUT_SECONDS = 1800
# kill 之后再给一次有界 communicate 收尸；仍卡住则如实标 unknown，绝不谎报释放。
POST_KILL_GRACE_SECONDS = 60

_FORBIDDEN_TOOLS = ('agent', 'task', 'subagent')
# 只放行已知 OpenCode 工具名；`*` 通配与未知工具一律拒绝（agent/task/subagent 的
# 大小写变体由 _FORBIDDEN_TOOLS 的 lower() 比对先行拒绝）。
_KNOWN_TOOLS = frozenset({
    'read', 'edit', 'write', 'bash', 'glob', 'grep', 'list', 'patch',
    'todowrite', 'question', 'webfetch', 'websearch', 'external_directory',
})
_BASE_PERMISSION = {'*': 'deny', 'read': 'allow', 'edit': 'allow',
                    'external_directory': 'deny'}


def _validate_allowed_tools(tools) -> list:
    """allowed_tools 校验：必须是非空、未带空白的字符串列表；agent/task/subagent
    （含大小写变体）拒绝；`*` 通配与未知工具名拒绝；只放行已知工具清单。"""
    if not isinstance(tools, list) or any(
            not isinstance(x, str) or not x or x != x.strip() for x in tools):
        raise ValueError('allowed_tools must be a list of non-empty, untrimmed strings')
    bad = [t for t in tools if t.lower() in _FORBIDDEN_TOOLS]
    if bad:
        raise ValueError(f'allowed_tools may not enable agent/task/subagent: {bad}')
    unknown = [t for t in tools if t not in _KNOWN_TOOLS]
    if unknown:
        raise ValueError(f'allowed_tools contains unknown or wildcard tool names: '
                         f'{unknown}; only known tools are permitted')
    return tools

# 只扫描**结构化错误字段**（顶层 error 事件、工具 state.error），绝不扫描正常
# prompt/report/工具 output 正文；因此正常 Read 内容里的 429/ECONNRESET 不会误报。
_PERMISSION_RE = re.compile(r'permission|denied|not\s+allowed|forbidden', re.I)
_AUTH_RE = re.compile(r'\b(?:401|403)\b|unauthori[sz]ed|authentication|api[\s_-]*key|'
                      r'credential|login\s+required|invalid\s+token', re.I)
_QUOTA_RE = re.compile(r'\b429\b|quota|rate[\s_-]*limit|usage[\s_-]*limit|'
                       r'credit|insufficient[\s_-]*balance', re.I)
_TRANSPORT_RE = re.compile(r'econnreset|econnrefused|etimedout|eai_again|socket|'
                           r'network|unreachable|dns|tls|ssl', re.I)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_entry_config(path=None) -> dict:
    """加载入口配置（可选）。缺省路径不存在时返回空配置（exe 走 PATH 发现）。
    显式传入的路径不存在/非 JSON 对象一律抛错，绝不猜。"""
    explicit = path is not None
    candidate = Path(path) if path else DEFAULT_CONFIG
    if not candidate.is_file():
        if explicit:
            raise ValueError(f'entry config not found: {candidate}')
        return {}
    try:
        cfg = json.loads(candidate.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError(f'invalid entry config {candidate}: {exc}') from exc
    if not isinstance(cfg, dict):
        raise ValueError('entry config must be a JSON object')
    tools = cfg.get('allowed_tools')
    if tools is not None:
        _validate_allowed_tools(tools)
    return cfg


def resolve_executable(cfg=None, environ=None) -> str:
    """解析 OpenCode 可执行文件：配置 executable/exe（绝对路径或 PATH 名）→
    环境变量 OPENCODE_EXE → PATH 中的 opencode。缺失即抛错，0 副作用。"""
    cfg = cfg or {}
    env = os.environ if environ is None else environ
    raw = cfg.get('executable') or cfg.get('exe') or env.get('OPENCODE_EXE') or 'opencode'
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('config executable must be a non-empty string')
    raw = raw.strip()
    p = Path(raw)
    if p.is_absolute():
        if not p.is_file():
            raise ValueError(f'OpenCode executable not found: {p}')
        return str(p)
    found = shutil.which(raw, path=env.get('PATH'))
    if not found:
        raise ValueError(f'OpenCode executable {raw!r} not found on PATH; configure '
                         '"executable" with an absolute path')
    return found


def build_argv(executable: str, model: str) -> list[str]:
    """正式 CLI 参数。任务正文不放进 argv，优先经 stdin 发送。"""
    if model not in MODELS:
        raise ValueError(f'unsupported OpenCode model {model!r}')
    return [str(executable), 'run', '--pure', '--model', f'opencode/{model}',
            '--format', 'json']


def build_opencode_config(cfg=None) -> dict:
    """构造 OPENCODE_CONFIG_CONTENT。默认只允许 read/edit；配置可显式追加已授权工具，
    但 agent/task/subagent 永远拒绝。"""
    cfg = cfg or {}
    permission = dict(_BASE_PERMISSION)
    tools = cfg.get('allowed_tools') or []
    _validate_allowed_tools(tools)
    for tool in tools:
        name = str(tool).strip()
        permission[name] = 'allow'
    # external_directory 始终保持 deny；配置不能把它打开。
    permission['external_directory'] = 'deny'
    return {'share': 'disabled', 'permission': permission, 'mcp': {}, 'plugin': []}


def build_private_dirs(out_dir) -> dict:
    """每 run 私有空目录（在新建 out_dir 之下，绝不指向用户全局配置/数据目录）。"""
    base = Path(out_dir)
    dirs = {
        'OPENCODE_CONFIG_DIR': base / '.opencode-config-dir',
        'XDG_CONFIG_HOME': base / '.opencode-xdg-config',
        'XDG_DATA_HOME': base / '.opencode-xdg-data',
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=False)
        if any(p.iterdir()):
            raise ValueError(f'private OpenCode dir is not empty: {p}')
    return {k: str(v) for k, v in dirs.items()}


def build_subprocess_env(cfg=None, private_dirs=None, environ=None) -> dict:
    """宿主 environ 副本 + 强制隔离项。绝不注入/复制凭据，绝不改用户全局。"""
    env = dict(os.environ if environ is None else environ)
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(build_opencode_config(cfg),
                                                ensure_ascii=False,
                                                separators=(',', ':'))
    private_dirs = private_dirs or {}
    for key in ('OPENCODE_CONFIG_DIR', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
        if private_dirs.get(key):
            env[key] = str(private_dirs[key])
        else:
            # 私有目录未提供时绝不继承宿主全局目录——真实隔离，而非只是覆盖。
            env.pop(key, None)
    env['OPENCODE_DISABLE_AUTOUPDATE'] = '1'
    env['OPENCODE_DISABLE_CLAUDE_CODE'] = '1'
    env['OPENCODE_DISABLE_LSP_DOWNLOAD'] = '1'
    return env


# 官方顶层 error 事件形如 {type:'error', name:'APIError', data:{message, statusCode}}。
# 白名单只跟随这些结构化错误字段；headers/header 等键绝不进入扫描（避免头里的
# 401/429 字样误报），statusCode 这类数值也转成文本以命中 \b401\b/\b429\b 分类。
_ERROR_WALK_KEYS = ('message', 'msg', 'detail', 'name', 'code', 'error', 'errors',
                    'data', 'statusCode', 'status_code', 'status')
_ERROR_SKIP_KEYS = frozenset({'headers', 'header', 'responseheaders', 'response_headers'})


def _structured_error_texts(event) -> list[str]:
    """只从顶层结构化 error 事件的白名单字段提取文本（含 data 嵌套与 statusCode 数值），
    不碰正常文本/工具输出正文，绝不读取 headers。"""
    if not isinstance(event, dict):
        return []
    out = []

    def walk(value, depth=0):
        if depth > 6:
            return
        if isinstance(value, str):
            if value:
                out.append(value)
        elif isinstance(value, bool):
            return
        elif isinstance(value, (int, float)):
            out.append(str(int(value)))
        elif isinstance(value, dict):
            for key, sub in value.items():
                if str(key).lower() in _ERROR_SKIP_KEYS:
                    continue
                if key in _ERROR_WALK_KEYS:
                    walk(sub, depth + 1)
        elif isinstance(value, list):
            for item in value[:20]:
                walk(item, depth + 1)

    for key in ('error', 'message', 'detail', 'errors'):
        if key in event:
            walk(event[key])
    return out


def _error_field_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def parse_events(stdout_bytes: bytes) -> dict:
    """解析 OpenCode 逐行 JSON 流。

    协议成功：进程退出 0（由调用方并入）、最后一个 step_finish.reason == 'stop'、
    无顶层结构化 error 事件、无 JSON 解析错误。工具 error/权限拒绝单独记录，不冒充
    顶层协议错误；正常 read 正文不做任何关键词扫描。
    """
    text = (stdout_bytes or b'').decode('utf-8', errors='replace')
    events, parse_errors = [], []
    event_types = {}
    session_ids, observed_models = [], []
    step_starts = 0
    step_finishes = []
    text_parts = []
    tool_calls = []
    top_errors = []
    usage_parts = []

    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            parse_errors.append(f'line {lineno}: invalid JSON ({exc})')
            continue
        if not isinstance(event, dict):
            parse_errors.append(f'line {lineno}: event is not a JSON object')
            continue
        events.append(event)
        etype = event.get('type')
        etype_key = str(etype)
        event_types[etype_key] = event_types.get(etype_key, 0) + 1
        part = event.get('part') if isinstance(event.get('part'), dict) else {}
        sid = event.get('sessionID') or event.get('sessionId') or part.get('sessionID')
        if isinstance(sid, str) and sid.strip():
            session_ids.append(sid)
        for key in ('model', 'modelID', 'modelId'):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                observed_models.append(value)
            pvalue = part.get(key)
            if isinstance(pvalue, str) and pvalue.strip():
                observed_models.append(pvalue)

        if etype == 'error':
            top_errors.append(event)
        elif etype == 'step_start':
            step_starts += 1
        elif etype == 'step_finish':
            tokens = part.get('tokens') if isinstance(part.get('tokens'), dict) else None
            step_finishes.append({
                'reason': part.get('reason'),
                'tokens': tokens,
                'cost': part.get('cost'),
                'session_id': sid,
            })
            if tokens is not None:
                usage_parts.append(tokens)
        elif etype == 'text':
            t = part.get('text')
            if isinstance(t, str):
                text_parts.append(t)
        elif etype == 'tool_use' or part.get('type') == 'tool':
            state = part.get('state') if isinstance(part.get('state'), dict) else {}
            tool = part.get('tool') or state.get('tool')
            call_id = (part.get('callID') or part.get('callId')
                       or event.get('callID') or event.get('callId'))
            status = state.get('status')
            error = state.get('error')
            inp = state.get('input') if isinstance(state.get('input'), dict) else {}
            err_text = _error_field_text(error)
            if status == 'error' or error is not None:
                kind = ('permission' if _PERMISSION_RE.search(err_text)
                        else 'error')
                tool_calls.append({
                    'call_id': call_id, 'tool': tool, 'status': status,
                    'error': err_text, 'error_class': kind,
                    'file_path': inp.get('filePath') or inp.get('file_path'),
                    'completed': False,
                })
            elif status == 'completed':
                tool_calls.append({
                    'call_id': call_id, 'tool': tool, 'status': status,
                    'error': None, 'error_class': None,
                    'file_path': inp.get('filePath') or inp.get('file_path'),
                    'completed': True,
                })
            else:
                tool_calls.append({
                    'call_id': call_id, 'tool': tool, 'status': status,
                    'error': None, 'error_class': None,
                    'file_path': inp.get('filePath') or inp.get('file_path'),
                    'completed': False, 'missing_receipt': True,
                })

    completed = [c for c in tool_calls if c.get('completed')]
    failed = [c for c in tool_calls if c.get('error') is not None
              or c.get('status') == 'error']
    missing = [c for c in tool_calls if c.get('missing_receipt')]
    permission_denials = [c for c in tool_calls
                          if c.get('error_class') == 'permission']
    write_receipts = [c for c in completed
                      if str(c.get('tool') or '').lower() in ('write', 'edit')]
    read_receipts = [c for c in completed
                     if str(c.get('tool') or '').lower() in ('read', 'view')]
    successful_ids = [c.get('call_id') for c in completed
                      if c.get('call_id') is not None]
    last_reason = step_finishes[-1]['reason'] if step_finishes else None
    final_stop = last_reason == 'stop'
    protocol_success = (final_stop and not top_errors and not parse_errors)
    final_text = text_parts[-1] if text_parts else ''
    intermediate_texts = text_parts[:-1] if len(text_parts) > 1 else []
    session_id = session_ids[0] if session_ids else None
    execution_evidence_ok = bool(completed) and not failed and not permission_denials \
        and not missing

    # 只对结构化错误字段做分类；正常正文/工具 output 从不参与。
    structured_texts = []
    for ev in top_errors:
        structured_texts.extend(_structured_error_texts(ev))
    for c in failed:
        if c.get('error'):
            structured_texts.append(c['error'])
    joined = '\n'.join(structured_texts)
    classification = {
        'permission': bool(permission_denials)
        or bool(_PERMISSION_RE.search(joined)),
        'auth': bool(_AUTH_RE.search(joined)),
        'quota': bool(_QUOTA_RE.search(joined)),
        'transport': bool(_TRANSPORT_RE.search(joined)),
        'protocol_parse_failure': bool(parse_errors),
        'structured_error_events': len(top_errors),
    }
    classification['model_execution_failure'] = bool(
        not protocol_success and not any(classification[k] for k in
                                         ('permission', 'auth', 'quota', 'transport',
                                          'protocol_parse_failure')))

    return {
        'events': events,
        'event_types': event_types,
        'parse_errors': parse_errors,
        'step_start_count': step_starts,
        'step_finishes': step_finishes,
        'last_step_reason': last_reason,
        'final_stop': final_stop,
        'top_level_errors': top_errors,
        'tool_calls': tool_calls,
        'successful_tool_receipts': successful_ids,
        'write_receipts': write_receipts,
        'read_receipts': read_receipts,
        'failed_tool_receipts': failed,
        'missing_tool_receipts': missing,
        'permission_denials': permission_denials,
        'execution_evidence_ok': execution_evidence_ok,
        'execution_evidence_scope': ('tool receipts only; not business acceptance and '
                                     'not independent file-system truth'),
        'response_text': final_text,
        'intermediate_text_parts': intermediate_texts,
        'text_part_count': len(text_parts),
        'session_id': session_id,
        'observed_models': sorted(set(observed_models)),
        'usage_parts': usage_parts,
        'protocol_success': bool(protocol_success),
        'failure_classification': classification,
    }


def _build_actual(*, cfg, task_id, stage, model, workspace, prompt_sha256, argv):
    """execution_control 预检实际值：如实反映 build_opencode_config 的真实 read/edit +
    显式追加工具，绝不拿空 grants 冒充"实际无权限"；仍是整工具开关，不带逐文件 grants。"""
    permission = build_opencode_config(cfg)['permission']
    allowed = sorted(k for k, v in permission.items() if v == 'allow')
    denied = sorted(k for k, v in permission.items() if v == 'deny')
    grants = ec.grants_from_rules(allowed_rules=allowed, add_dirs=[],
                                  disallowed_rules=denied, tools_items=allowed)
    return {'task_id': task_id, 'stage': stage, 'runtime': RUNTIME, 'model': model,
            'workspace': str(workspace), 'cwd': str(workspace),
            'prompt_sha256': prompt_sha256, 'argv': list(argv), 'shell': False,
            'grants': grants}


def _write_json(path: Path, obj) -> None:
    path.write_bytes(json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8'))


def run(cfg, *, workspace, prompt_file, out_dir, stage, task_id, model,
        prompt_sha256=None, claim_token=None, now=None, popen_factory=subprocess.Popen,
        environ=None, dispatch_store=None, child_prober=None, bind_child_pid=True,
        plan=None, active_tasks=None, timeout_seconds=None,
        clock_injected=None) -> dict:
    """正式 OpenCode 传输。任何前置/预检失败零 Popen；每个 run 只调用一次 CLI。"""
    if clock_injected is None:
        clock_injected = now is not None
    now = now or dp.utcnow()
    timeout_seconds = (DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None
                       else int(timeout_seconds))

    # (0) 精确模型硬门 + 有界超时：任何副作用之前拒绝。
    if str(model) not in MODELS:
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                'reason': 'cli_model_rejected',
                'reasons': [f'--model must be one of {list(MODELS)!r}; no paid fallback '
                            f'or other OpenCode model is accepted, got {model!r}']}
    if timeout_seconds <= 0:
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                'reason': 'timeout_out_of_bounds',
                'reasons': ['timeout_seconds must be positive; a bounded wall clock is '
                            'mandatory for an OpenCode real-slot writer']}

    # (1) outdir 必须不存在；绝不覆写旧证据。
    out = Path(out_dir)
    if out.exists():
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                'reason': 'output_dir_exists',
                'reasons': [f'output dir {str(out)!r} already exists; refusing to '
                            'overwrite prior evidence']}

    # (2) 提示词/argv/可执行文件/配置：全部在认领与 Popen 之前。
    try:
        prompt_bytes = Path(prompt_file).read_bytes()
        actual_prompt_sha = _sha(prompt_bytes)
        if prompt_sha256 and str(prompt_sha256).lower() != actual_prompt_sha:
            raise ValueError(f'prompt sha256 recheck failed: file hashes '
                             f'{actual_prompt_sha} but caller asserted {prompt_sha256!r}')
        task_text = prompt_bytes.decode('utf-8')
        executable = resolve_executable(cfg, environ)
        argv = build_argv(executable, str(model))
        build_opencode_config(cfg)  # 提前校验配置，不等到 Popen
        # 合约/输入在认领之前构建并校验，避免构造失败泄漏已占用的槽位。
        contract_text = pc.build_contract(stage, str(workspace), runtime=RUNTIME,
                                          model=str(model))
        sent_payload = pc.compose_task_payload(contract_text, task_text).encode('utf-8')
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                'reason': 'precheck_failed', 'error_class': type(exc).__name__,
                'reasons': [f'prompt/config/argv precheck failed before any claim: '
                            f'{exc!r}']}

    # (3) dispatch plan 精确预检（认领之前，0 Popen）。
    if plan is not None:
        try:
            actual = _build_actual(cfg=cfg, task_id=task_id, stage=stage,
                                   model=str(model),
                                   workspace=workspace,
                                   prompt_sha256=actual_prompt_sha, argv=argv)
            pf = ec.preflight(plan, actual, active_tasks=active_tasks)
        except Exception as exc:  # noqa: BLE001
            return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                    'reason': 'dispatch_plan_invalid', 'error_class': type(exc).__name__,
                    'reasons': [f'dispatch plan validation failed: {exc!r}']}
        if not pf.get('ok'):
            return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                    'reason': 'dispatch_plan_rejected', 'preflight': pf,
                    'reasons': pf.get('reasons') or []}

    # (4) 原子容量认领（executor='opencode'，无时段门/无限额前提）。
    gate = dp.consume_for_entry(
        dispatch_store, task_id=task_id, runtime=RUNTIME, model=str(model),
        workspace=str(workspace), prompt_sha256=actual_prompt_sha, stage=stage,
        claim_token=claim_token, now=now, executor=RUNTIME, wrapper_pid=os.getpid())
    if not gate.get('allowed'):
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                'gate': gate}
    claim = gate['token']
    pool_key = dp.pool_key(RUNTIME, str(model))

    started = False
    child_verified = False
    child_done = False
    timed_out = False
    proc = None
    stdout_bytes = b''
    stderr_bytes = b''
    exit_code = None
    started_at_utc = now
    finished_at_utc = now
    private_dirs = {}
    child_env = {}
    try:
        out.mkdir(parents=True, exist_ok=False)
        private_dirs = build_private_dirs(out)
        child_env = build_subprocess_env(cfg, private_dirs, environ)
        started_at_utc = now if clock_injected else _now_utc()
        proc = popen_factory(argv, cwd=str(workspace), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=child_env)
        started = True
        dp.mark_running(dispatch_store, claim, wrapper_pid=os.getpid(),
                        now=started_at_utc)
        if bind_child_pid:
            bound = None
            if getattr(proc, 'pid', None) is not None:
                bound = dp.bind_child(dispatch_store, claim, int(proc.pid),
                                      wrapper_pid=os.getpid(), now=started_at_utc)
            child_verified = bool(isinstance(bound, dict) and bound.get('bound') is True)
            if not child_verified:
                # 绑定未明确成功：绝不写 stdin、不杀进程、不假释放；保留未知占位与原始证据。
                try:
                    (out / 'sent-payload-stdin.bin').write_bytes(sent_payload)
                    _write_json(out / 'process.json', {
                        'reason': 'bind_child_failed',
                        'child_pid': getattr(proc, 'pid', None),
                        'note': 'child PID binding not confirmed; stdin not written, '
                                'process not killed, capacity held as unknown'})
                except OSError:
                    pass
                try:
                    unknown_guard = dp.mark_unknown(dispatch_store, claim,
                                                    now=started_at_utc)
                except Exception as uexc:  # noqa: BLE001
                    unknown_guard = {'marked': False, 'error': type(uexc).__name__}
                return {'dispatch_failed': False, 'sent': False, 'dispatched': True,
                        'launch_unknown': True, 'started': True, 'token': claim,
                        'pool_key': pool_key, 'child_verified': False,
                        'bind_required': True, 'unknown_guard': unknown_guard,
                        'business_verified': False}
        try:
            stdout_bytes, stderr_bytes = proc.communicate(input=sent_payload,
                                                          timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            try:
                stdout_bytes, stderr_bytes = proc.communicate(
                    timeout=POST_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                # kill 后仍卡住：真实进程状态未知，绝不谎报释放——保留原始证据后标
                # unknown，占位继续，等 reconcile，不静默放行。
                post_kill_stuck = True
                try:
                    (out / 'sent-payload-stdin.bin').write_bytes(sent_payload)
                    _write_json(out / 'process.json', {
                        'reason': 'post_kill_grace_stuck',
                        'timeout_seconds': timeout_seconds,
                        'post_kill_grace_seconds': POST_KILL_GRACE_SECONDS,
                        'child_pid': getattr(proc, 'pid', None),
                        'note': 'child still not reaped after kill + bounded grace; '
                                'settled as unknown, capacity not released, originals kept'})
                except OSError:
                    pass
                try:
                    unknown_guard = dp.mark_unknown(dispatch_store, claim, now=now)
                except Exception as uexc:  # noqa: BLE001
                    unknown_guard = {'marked': False, 'error': type(uexc).__name__}
                return {'dispatch_failed': False, 'sent': False, 'dispatched': True,
                        'launch_unknown': True, 'started': True, 'token': claim,
                        'pool_key': pool_key, 'child_verified': child_verified,
                        'timed_out': True, 'post_kill_grace_stuck': True,
                        'unknown_guard': unknown_guard, 'business_verified': False}
        exit_code = getattr(proc, 'returncode', None)
        finished_at_utc = now if clock_injected else _now_utc()
        child_done = True
    except BaseException as exc:  # noqa: BLE001 - 终态处理必须兜底
        if started:
            if not child_done:
                try:
                    unknown_guard = dp.mark_unknown(dispatch_store, claim, now=now)
                except Exception as uexc:  # noqa: BLE001
                    unknown_guard = {'marked': False, 'error': type(uexc).__name__}
                return {'dispatch_failed': False, 'sent': False, 'dispatched': True,
                        'launch_unknown': True, 'started': True, 'token': claim,
                        'pool_key': pool_key, 'unknown_guard': unknown_guard,
                        'error_class': type(exc).__name__, 'business_verified': False}
            exit_code = getattr(proc, 'returncode', None) if exit_code is None else exit_code
        else:
            try:
                start_release = dp.finish(dispatch_store, claim,
                                          terminal='start_failed', now=now,
                                          wrapper_pid=os.getpid())
            except Exception as fexc:  # noqa: BLE001
                start_release = {'released': False, 'error': type(fexc).__name__}
            return {'dispatch_failed': False, 'sent': False, 'dispatched': False,
                    'started': False, 'token': claim, 'pool_key': pool_key,
                    'capacity_release': start_release,
                    'error_class': type(exc).__name__, 'business_verified': False}

    # 已确认退出：解析/留证/释放全部进受保护流程；释放无条件执行并保留真实结果。
    ec_exit = exit_code if exit_code is not None else -1
    parsed = {}
    binding = {}
    report_text = ''
    evidence = {}
    downstream_error = None
    try:
        (out / 'stdout.jsonl').write_bytes(stdout_bytes)
        (out / 'stderr.log').write_bytes(stderr_bytes)
        (out / 'events.jsonl').write_bytes(stdout_bytes)
        (out / 'sent-payload-stdin.bin').write_bytes(sent_payload)
        parsed = parse_events(stdout_bytes)
        protocol_success = bool(parsed['protocol_success'] and ec_exit == 0
                                and not timed_out)
        if ec_exit != 0:
            parsed['parse_errors'].append(f'process exited non-zero: {ec_exit}')
        if timed_out:
            parsed['parse_errors'].append(
                f'bounded child wall-clock timeout after {timeout_seconds}s '
                '(settled as failure, never healthy)')
        report_text = parsed.get('response_text') or ''
        if report_text:
            (out / 'response.md').write_bytes(report_text.encode('utf-8'))
            disk = (out / 'response.md').read_bytes()
            readback_match = _sha(disk) == _sha(report_text.encode('utf-8'))
            body = qd.analyze_report(report_text, stage, str(workspace))
            binding = qd.finalize_binding(
                body['body_ok'], body.get('reasons'),
                protocol_success=protocol_success,
                session_id=parsed.get('session_id'),
                requested_session_id=None,
                readback_match=bool(readback_match))
            report_state = {**body, 'readback_match': readback_match,
                            'protocol_success': protocol_success,
                            'session_id': parsed.get('session_id'),
                            'binding': binding, 'bound': binding['bound'],
                            'reasons': binding['reasons']}
        else:
            binding = {'bound': False,
                       'reasons': ['no final text.part.text report carrier']}
            report_state = {'bound': False, 'protocol_success': protocol_success,
                            'reasons': ['no final report carrier',
                                        *parsed.get('parse_errors', [])],
                            'carrier_missing': True}
        _write_json(out / 'report-state.json', report_state)

        execution_evidence_ok = bool(parsed.get('execution_evidence_ok'))
        passed = bool(protocol_success and binding.get('bound')
                      and execution_evidence_ok and not timed_out)
        summary = {
            'runtime': RUNTIME, 'model': str(model), 'pool_key': pool_key,
            'requested_model': str(model),
            'model_backend_identity_verified': False,
            'identity_source': ('entry runtime + requested model; not a verified '
                                'backend/account identity'),
            'exit_code': exit_code, 'timed_out': timed_out,
            'protocol_success': protocol_success,
            'report_bound': bool(binding.get('bound')),
            'engineering_success': passed,
            'business_verified': False,
            'free_quota_verified': False,
            'session_id': parsed.get('session_id'),
            'observed_models': parsed.get('observed_models'),
            'event_types': parsed.get('event_types'),
            'step_start_count': parsed.get('step_start_count'),
            'step_finishes': parsed.get('step_finishes'),
            'last_step_reason': parsed.get('last_step_reason'),
            'final_stop': parsed.get('final_stop'),
            'text_part_count': parsed.get('text_part_count'),
            'intermediate_text_parts': parsed.get('intermediate_text_parts'),
            'tool_calls': [{k: v for k, v in c.items() if k != 'content'}
                           for c in parsed.get('tool_calls', [])],
            'successful_tool_receipts': parsed.get('successful_tool_receipts'),
            'write_receipts': parsed.get('write_receipts'),
            'read_receipts': parsed.get('read_receipts'),
            'failed_tool_receipts': parsed.get('failed_tool_receipts'),
            'missing_tool_receipts': parsed.get('missing_tool_receipts'),
            'permission_denials': parsed.get('permission_denials'),
            'execution_evidence': {
                'ok': execution_evidence_ok,
                'scope': parsed.get('execution_evidence_scope'),
            },
            'parse_errors': parsed.get('parse_errors'),
            'failure_classification': parsed.get('failure_classification'),
            'usage_parts_raw': parsed.get('usage_parts'),
            'usage_billing_basis': 'unknown; raw usage saved without interpretation',
            'output_dir': str(out),
        }
        _write_json(out / 'summary.json', summary)
        _write_json(out / 'request.json', {
            'started_at_utc': started_at_utc.isoformat(),
            'finished_at_utc': finished_at_utc.isoformat(),
            'workspace': str(workspace), 'cwd': str(workspace),
            'runtime': RUNTIME, 'model': str(model), 'pool_key': pool_key,
            'task_id': task_id, 'stage': stage,
            'argv': argv, 'shell': False, 'stdin_payload': True,
            'timeout_seconds': timeout_seconds,
            'prompt_sha256': actual_prompt_sha,
            'contract_sha256': _sha(contract_text.encode('utf-8')),
            'contract_sha256_caliber': 'utf-8 bytes of the contract prepended to stdin',
            'sent_payload_sha256': _sha(sent_payload),
            'entry_config': {
                'executable': cfg.get('executable') or cfg.get('exe'),
                'allowed_tools': list(cfg.get('allowed_tools') or []),
            },
            'no_paid_fallback': True,
            'one_cli_invocation_per_run': True,
        })
        _write_json(out / 'process.json', {
            'pid': getattr(proc, 'pid', None),
            'exit_code': exit_code,
            'timed_out': timed_out,
            'started_at_utc': started_at_utc.isoformat(),
            'finished_at_utc': finished_at_utc.isoformat(),
        })
        evidence = {'stdout_jsonl': str(out / 'stdout.jsonl'),
                    'stderr_log': str(out / 'stderr.log'),
                    'events_jsonl': str(out / 'events.jsonl'),
                    'sent_payload_stdin': str(out / 'sent-payload-stdin.bin'),
                    'response_md': str(out / 'response.md') if report_text else None,
                    'report_state': str(out / 'report-state.json'),
                    'summary_json': str(out / 'summary.json'),
                    'request_json': str(out / 'request.json'),
                    'process_json': str(out / 'process.json')}
    except Exception as exc:  # noqa: BLE001 - 落盘失败不外泄；终态仍按真实退出释放
        downstream_error = type(exc).__name__
        parsed = parsed or {'protocol_success': False, 'parse_errors': [],
                            'execution_evidence_ok': False, 'usage_parts': []}
        binding = binding or {'bound': False, 'reasons': ['downstream write error']}
        report_text = report_text or ''

    protocol_success = bool(parsed.get('protocol_success') and ec_exit == 0
                            and not timed_out)
    execution_evidence_ok = bool(parsed.get('execution_evidence_ok'))
    success = bool(protocol_success and binding.get('bound') and execution_evidence_ok
                   and not timed_out)
    try:
        release = dp.finish(dispatch_store, claim, terminal='finished', success=success,
                            now=finished_at_utc, prober=child_prober)
    except Exception as fexc:  # noqa: BLE001
        release = {'released': False, 'error': type(fexc).__name__}
    bind_ok = child_verified if bind_child_pid else True
    release_ok = bool(isinstance(release, dict) and release.get('released') is True)
    passed = bool(success and not downstream_error and bind_ok and release_ok)
    return {'sent': True, 'dispatched': True, 'started': started,
            'child_verified': child_verified, 'token': claim, 'exit_code': exit_code,
            'pool_key': pool_key, 'protocol_success': protocol_success,
            'report_bound': bool(binding.get('bound')),
            'execution_evidence_ok': execution_evidence_ok,
            'timed_out': timed_out, 'evidence': evidence, 'launched': True,
            'capacity_release': release,
            'started_at_utc': started_at_utc.isoformat(),
            'finished_at_utc': finished_at_utc.isoformat(),
            'passed': passed, 'business_verified': False,
            'requested_model': str(model),
            'model_backend_identity_verified': False,
            'failure_classification': parsed.get('failure_classification'),
            'report_text_sha256': (_sha(report_text.encode('utf-8'))
                                   if report_text else None)}


def build_parser():
    ap = argparse.ArgumentParser(prog='opencode_direct')
    ap.add_argument('--workspace', required=True)
    ap.add_argument('--prompt-file', required=True, dest='prompt_file')
    ap.add_argument('--output-dir', required=True, dest='output_dir')
    ap.add_argument('--stage', required=True)
    ap.add_argument('--task-id', required=True, dest='task_id')
    ap.add_argument('--model', required=True,
                    help=f'one of {list(MODELS)}; no paid fallback or other model')
    ap.add_argument('--config', default=None,
                    help='optional entry config JSON (executable / allowed_tools); '
                         f'defaults to {DEFAULT_CONFIG.name} when present, otherwise '
                         'PATH discovery of opencode')
    ap.add_argument('--dispatch-claim', dest='dispatch_claim', default=None)
    ap.add_argument('--dispatch-store', dest='dispatch_store', default=None)
    ap.add_argument('--dispatch-plan', dest='dispatch_plan', default=None)
    ap.add_argument('--active-tasks', dest='active_tasks', default=None)
    ap.add_argument('--timeout-seconds', dest='timeout_seconds', type=int,
                    default=None,
                    help=f'bounded wall clock (default {DEFAULT_TIMEOUT_SECONDS}s); '
                         'zero/negative values are refused')
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if str(args.model) not in MODELS:
        print(json.dumps({'dispatch_failed': True, 'sent': False, 'dispatched': False,
                          'reason': 'cli_model_rejected',
                          'reasons': [f'--model must be one of {list(MODELS)!r}, got '
                                      f'{args.model!r}']}, ensure_ascii=False))
        return 2
    if args.timeout_seconds is not None and args.timeout_seconds <= 0:
        print(json.dumps({'dispatch_failed': True, 'sent': False, 'dispatched': False,
                          'reason': 'timeout_out_of_bounds',
                          'reasons': ['--timeout-seconds must be positive']},
                         ensure_ascii=False))
        return 2
    try:
        cfg = load_entry_config(args.config)
    except ValueError as exc:
        print(json.dumps({'dispatch_failed': True, 'sent': False, 'dispatched': False,
                          'reason': 'config_invalid', 'reasons': [str(exc)]},
                         ensure_ascii=False))
        return 2
    plan = None
    active_tasks = None
    if args.dispatch_plan:
        plan = json.loads(Path(args.dispatch_plan).read_text(encoding='utf-8'))
        if args.active_tasks:
            active_tasks = json.loads(Path(args.active_tasks).read_text(encoding='utf-8'))
    prompt_bytes = Path(args.prompt_file).read_bytes()
    out = run(cfg, workspace=args.workspace, prompt_file=Path(args.prompt_file),
              out_dir=Path(args.output_dir), stage=args.stage, task_id=args.task_id,
              model=args.model, prompt_sha256=_sha(prompt_bytes),
              claim_token=args.dispatch_claim, dispatch_store=args.dispatch_store,
              plan=plan, active_tasks=active_tasks,
              timeout_seconds=args.timeout_seconds)
    print(json.dumps(out, ensure_ascii=False))
    if not out.get('dispatched'):
        return 2
    return 0 if out.get('passed') else 3


if __name__ == '__main__':
    raise SystemExit(main())
