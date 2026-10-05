"""Direct CodeBuddy Code CLI invocation: minimal transport for brain-worker (direct mode).

与 qoder_direct 对齐的九节严格报告绑定：复用其 analyze_report / finalize_binding，
不修改共享文件。差异（按本机 CodeBuddy CLI 2.161.1 已核对形状）：
- argv 形状为 -p --verbose --output-format stream-json --model <ID>
  --permission-mode dontAsk --tools <单一逗号字符串> --strict-mcp-config
  --mcp-config '{"mcpServers":{}}' --setting-sources '' --settings
  '{"disableAllHooks":true}' --agent cli --no-session-persistence；
- `--tools` 永远恰好出现一次，值为一个 comma-joined 字符串（空即 ''）；
  不得逐项 arg、不得省略；
- stdin 传送完整提示词（契约 + 用户原文，不裁剪）；无 shell，cwd 指定 workspace，
  env 仅 os.environ 副本加 DISABLE_AUTOUPDATER=1；
- `--allowed-tools` / `--disallowed-tools` 为 variadic：一次 flag 后跟全部规则
  （list，不拼字符串）；无显式 allow 时按 `--tools` 逐项回退，显式列表（含空）完全替代回退；
- stdout 是 stream-json JSONL：system/subtype=init 带 session_id/model/tools/
  mcp_servers、system/subtype=status 正常进度、assistant.message 带 model/content/usage
  （可为 tool_use 或 text）、user.message.content[].type=tool_result 真实工具结果，
  唯一最后 type=result。终态严格要求：
  * stdout 必须严格 UTF-8；任何非法字节保留原件并拒绝，禁止 errors='replace'
  * 唯一 result、subtype=success、is_error=false、result 非空字符串
  * init.session_id 非空；所有事件带 session_id 字段时必须完全一致，不得漂移
  * init.model 及所有 assistant.message.model 与请求模型精确一致
  * 至少一个 assistant.message 的 content 中存在非空 text 分片
  * result.result 与最后 assistant 的最后一个 text 分片精确一致（不 trim）
  * init.mcp_servers 严格为空 list（非空即拒绝，不得宣称零 MCP）
  * init.tools 视为完整注册表，不参与有效工具面判定；实际调用工具（tool_use.name）
    不得超出请求 --tools 白名单，否则视为越权
  * 权限拒绝必须从 user.tool_result.content.text 中提取（真实形状下 is_error=false，
    result.permission_denials=[] 不能作为无拒绝证据）；命中即 fail-closed
  * terminal_reason 为 cancelled/aborted/max_turns、重复/缺失 result、尾随非 JSON、
    未知事件类型一律判协议失败
- 用量口径未知：usage/modelUsage 仅原样保存 raw 值；business_verified /
  free_quota_verified / model_backend_identity_verified 恒为 false；cost_usd=0
  不视为免费证据。
退出码：0 仅当 protocol_success 且 report_bound 均为真；preflight 校验错误为 2
且证据目录零创建；其余失败为 3。权限范围不是文件 sandbox，见 references/codebuddy-direct.md。
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from qoder_direct import analyze_report, finalize_binding  # 共享正文核对/绑定，不改其文件

DEFAULT_CONFIG = _SCRIPTS_DIR / 'codebuddy-entry.json'
BAD_TERMINAL_REASONS = ('cancelled', 'aborted', 'max_turns')
ALLOWED_TOOLS = ('Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep')
# 只匹配明确的 CLI 权限拒绝文案；不匹配模型在报告正文里的普通引用。
PERMISSION_DENIAL_RE = re.compile(
    r'Permission\s+to\s+use\s+\S+\s+has\s+been\s+denied', re.IGNORECASE)


def load_entry_config(path=None) -> dict:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.is_absolute():
        raise ValueError(f'entry config path must be absolute: {cfg_path}')
    if not cfg_path.is_file():
        raise FileNotFoundError(f'entry config file not found: {cfg_path}')
    cfg_path = cfg_path.resolve()
    cfg_bytes = cfg_path.read_bytes()
    try:
        cfg = json.loads(cfg_bytes.decode('utf-8'))
    except UnicodeDecodeError as exc:
        raise ValueError(f'entry config is not valid UTF-8: {cfg_path}: {exc}')
    if not isinstance(cfg, dict):
        raise ValueError(f'entry config must be a JSON object: {cfg_path}')
    for key in ('node', 'cli'):
        if key not in cfg:
            raise KeyError(f'entry config missing key {key!r}: {cfg_path}')
        p = Path(cfg[key])
        if not p.is_absolute() or not p.is_file():
            raise FileNotFoundError(f'entry config {key!r} must be an existing absolute file: {p}')
    return cfg


def parse_tools_arg(value) -> list[str]:
    """--tools 白名单校验：空为零授权；条目必须属于 ALLOWED_TOOLS，不重复、无空白。"""
    if value is None:
        return []
    if not isinstance(value, str):
        raise ValueError(f'tools must be a string, got {type(value).__name__}')
    if value == '':
        return []
    items = value.split(',')
    out = []
    for item in items:
        if item == '' or item != item.strip():
            raise ValueError(f'tools entries must be non-empty with no surrounding '
                             f'whitespace: {value!r}')
        if item not in ALLOWED_TOOLS:
            raise ValueError(f'tool {item!r} not in allowed whitelist {list(ALLOWED_TOOLS)}')
        if item in out:
            raise ValueError(f'tool {item!r} duplicated in --tools: {value!r}')
        out.append(item)
    return out


def _validate_rule_list(value, *, field: str):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f'{field} entries must be strings, got '
                                 f'{type(item).__name__}: {item!r}')
            if item == '' or item.isspace():
                raise ValueError(f'{field} entries must be non-empty and not '
                                 f'whitespace-only: {item!r}')
            out.append(item)
        return out
    raise ValueError(f'{field} must be a list of strings or None, got {type(value).__name__}')


def effective_permission_rules(tools_items, allowed_tools, disallowed_tools) -> dict:
    """无显式 allow 时按 --tools 逐项回退；显式列表（含空）完全替代回退。"""
    allowed = (_validate_rule_list(allowed_tools, field='allowed_tools')
               if allowed_tools is not None else list(tools_items))
    disallowed = _validate_rule_list(disallowed_tools, field='disallowed_tools')
    return {'allowed_tools': allowed, 'disallowed_tools': disallowed}


def build_argv(cfg: dict, workspace: str, model: str, tools_items: list[str],
               session_id: str | None, *, allowed_tools=None,
               disallowed_tools=None) -> list[str]:
    """构造 argv。`--tools` 永远恰好出现一次，值是一个 comma-joined 字符串（空即 ''）；
    不得逐项 arg，也不得省略。`--allowed-tools`/`--disallowed-tools` 为 variadic：
    flag 后跟全部规则（list），显式 allow 完全替代 tools 回退。"""
    perms = effective_permission_rules(tools_items, allowed_tools, disallowed_tools)
    argv = [cfg['node'], cfg['cli'],
            '-p', '--verbose', '--output-format', 'stream-json',
            '--model', model, '--permission-mode', 'dontAsk',
            '--tools', ','.join(tools_items),
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--setting-sources', '', '--settings', '{"disableAllHooks":true}',
            '--agent', 'cli', '--no-session-persistence']
    if perms['allowed_tools']:
        argv += ['--allowedTools'] + perms['allowed_tools']
    if perms['disallowed_tools']:
        argv += ['--disallowedTools'] + perms['disallowed_tools']
    if session_id:
        argv += ['--resume', session_id]
    return argv


def _extract_tool_result_text(block) -> str | None:
    inner = block.get('content')
    if isinstance(inner, str):
        return inner
    if isinstance(inner, list):
        parts = []
        for piece in inner:
            if isinstance(piece, dict) and piece.get('type') == 'text' \
                    and isinstance(piece.get('text'), str):
                parts.append(piece['text'])
        if parts:
            return '\n'.join(parts)
    return None


def parse_stream(stdout_bytes: bytes, model_requested: str,
                 tools_whitelist: list[str] | None = None) -> dict:
    """严格解析 stream-json JSONL。任何缺项/未知事件/重复或缺失终态/尾随非 JSON
    均记入 parse_errors 并使协议失败；stdout 必须严格 UTF-8，不做 replace。
    事件 session_id 若存在必须与 init 一致；assistant 至少要有一个非空 text 分片；
    最后 assistant 的最后一个 text 分片必须与 result.result 精确一致；
    result.permission_denials=[] 不作为无拒绝证据——从 user.tool_result.content.text
    扫描明确的权限拒绝文案，命中即 fail-closed；实际 tool_use.name 不得超出请求白名单。"""
    empty = {'protocol_success': False, 'parse_errors': [], 'session_id': None,
             'observed_models': [], 'observed_tool_calls': [], 'response_text': None,
             'usage': None, 'model_usage': None, 'permission_denials': [],
             'terminal_reason': None}
    try:
        text = stdout_bytes.decode('utf-8')
    except UnicodeDecodeError as exc:
        result = dict(empty)
        result['parse_errors'] = [f'stream is not valid UTF-8 (no replace): {exc}']
        return result

    errors: list[str] = []
    init = None
    assistants: list[dict] = []
    results: list[dict] = []
    observed_tool_calls: list[str] = []
    permission_denials: list[dict] = []
    session_ids_seen: set[str] = set()

    lines = text.splitlines()
    for lineno, raw in enumerate(lines, start=1):
        if raw.strip() == '':
            if lineno == len(lines):
                continue  # 末尾换行不算尾随垃圾
            errors.append(f'line {lineno}: blank line inside stream')
            continue
        try:
            event = json.loads(raw)
        except ValueError as exc:
            errors.append(f'line {lineno}: non-JSON line: {exc}')
            continue
        if not isinstance(event, dict):
            errors.append(f'line {lineno}: event is not a JSON object')
            continue
        sid = event.get('session_id')
        if isinstance(sid, str) and sid.strip():
            session_ids_seen.add(sid)
        etype = event.get('type')
        if etype == 'system':
            subtype = event.get('subtype')
            if subtype == 'init':
                if init is not None:
                    errors.append(f'line {lineno}: duplicate init event')
                    continue
                missing = [k for k in ('session_id', 'model', 'tools', 'mcp_servers')
                           if k not in event]
                if missing:
                    errors.append(f'line {lineno}: init event missing fields {missing}')
                    continue
                if not (isinstance(event['session_id'], str) and event['session_id'].strip()):
                    errors.append(f'line {lineno}: init session_id is empty or not a string')
                    continue
                if not isinstance(event['model'], str) or not event['model'].strip():
                    errors.append(f'line {lineno}: init model is empty or not a string')
                    continue
                if event['mcp_servers'] != []:
                    errors.append(f'line {lineno}: init.mcp_servers must be strictly '
                                  f'[] (got {event["mcp_servers"]!r}); non-empty means '
                                  f'MCP is not zero')
                init = event
            elif subtype == 'status':
                pass  # 正常进度事件，保留但不参与终态判定
            else:
                errors.append(f'line {lineno}: system event subtype '
                              f'{subtype!r} is not "init" or "status"')
        elif etype == 'assistant':
            message = event.get('message')
            if not isinstance(message, dict):
                errors.append(f'line {lineno}: assistant event has no message object')
                continue
            missing = [k for k in ('model', 'content', 'usage') if k not in message]
            if missing:
                errors.append(f'line {lineno}: assistant.message missing fields {missing}')
                continue
            if not isinstance(message['model'], str) or not message['model']:
                errors.append(f'line {lineno}: assistant.message.model not a non-empty string')
            content = message['content']
            if not isinstance(content, list):
                errors.append(f'line {lineno}: assistant.message.content is not a list')
                continue
            texts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get('type')
                if ptype == 'text' and isinstance(part.get('text'), str):
                    texts.append(part['text'])
                elif ptype == 'tool_use':
                    name = part.get('name')
                    if isinstance(name, str) and name:
                        observed_tool_calls.append(name)
            assistants.append({'line': lineno, 'model': message['model'],
                               'texts': texts, 'usage': message['usage']})
        elif etype == 'user':
            message = event.get('message')
            if not isinstance(message, dict):
                errors.append(f'line {lineno}: user event has no message object')
                continue
            content = message.get('content')
            if not isinstance(content, list):
                errors.append(f'line {lineno}: user.message.content is not a list')
                continue
            for block in content:
                if not isinstance(block, dict) or block.get('type') != 'tool_result':
                    continue
                joined = _extract_tool_result_text(block)
                if joined is None:
                    continue
                if PERMISSION_DENIAL_RE.search(joined):
                    permission_denials.append({
                        'source': 'user.tool_result.content.text',
                        'tool_use_id': block.get('tool_use_id'),
                        'is_error': block.get('is_error'),
                        'excerpt': joined,
                    })
                    # 空 result.permission_denials 不作为无拒绝证据；命中即 fail-closed
                    errors.append(f'line {lineno}: permission denial present in '
                                  f'user.tool_result.content.text (fail-closed)')
        elif etype == 'result':
            if results:
                errors.append(f'line {lineno}: duplicate result event')
            missing = [k for k in ('subtype', 'is_error', 'result', 'session_id',
                                   'usage', 'modelUsage') if k not in event]
            if missing:
                errors.append(f'line {lineno}: result event missing fields {missing}')
            results.append(event)
        else:
            errors.append(f'line {lineno}: unknown event type {etype!r}')

    if init is None:
        errors.append('stream has no system/init event')
    if not results:
        errors.append('stream has no result event')
    # 终态必须是唯一 result 且位于最后一个非空行
    nonblank = [(i, l) for i, l in enumerate(lines) if l.strip() != '']
    if results and nonblank:
        last_line = nonblank[-1][1]
        try:
            last_event = json.loads(last_line)
        except ValueError:
            last_event = None
        if not isinstance(last_event, dict) or last_event.get('type') != 'result':
            errors.append('last stream line is not the result event (trailing content)')
    if len(results) > 1:
        errors.append(f'expected exactly one result event, got {len(results)}')

    result = results[0] if len(results) == 1 else None
    observed_models: list[str] = []
    session_id = None
    terminal_reason = None

    if init is not None:
        observed_models.append(init['model'])
        session_id = init['session_id']
        if init['model'] != model_requested:
            errors.append(f'init model {init["model"]!r} does not exactly match '
                          f'requested {model_requested!r}')
    for i, assistant in enumerate(assistants):
        observed_models.append(assistant['model'])
        if assistant['model'] != model_requested:
            errors.append(f'assistant[{i}].message.model {assistant["model"]!r} does '
                          f'not exactly match requested {model_requested!r}')
    if init is not None:
        for sid in sorted(session_ids_seen):
            if sid != init['session_id']:
                errors.append(f'session_id drift: event carries {sid!r} but init '
                              f'session_id is {init["session_id"]!r}')

    if not any(a['texts'] and any(t.strip() for t in a['texts']) for a in assistants):
        errors.append('no assistant event carries a non-empty text part')

    if tools_whitelist is not None:
        wl = set(tools_whitelist)
        for name in observed_tool_calls:
            if name not in wl:
                errors.append(f'observed tool_use {name!r} exceeds requested '
                              f'--tools whitelist {sorted(wl)}')

    response_text = None
    if result is not None:
        terminal_reason = result.get('terminal_reason')
        if terminal_reason in BAD_TERMINAL_REASONS:
            errors.append(f'result terminal_reason {terminal_reason!r} is a '
                          f'rejected terminal state')
        if result.get('subtype') != 'success':
            errors.append(f'result subtype {result.get("subtype")!r} is not "success"')
        if result.get('is_error') is not False:
            errors.append('result is_error is not false')
        result_perm = result.get('permission_denials')
        if isinstance(result_perm, list) and result_perm:
            errors.append('result.permission_denials is non-empty; fail-closed')
            for entry in result_perm:
                if isinstance(entry, dict):
                    permission_denials.append({'source': 'result.permission_denials',
                                               **entry})
                else:
                    permission_denials.append({'source': 'result.permission_denials',
                                               'value': entry})
        response_text = result.get('result')
        if not (isinstance(response_text, str) and response_text.strip()):
            errors.append('result text is empty or not a string')
            response_text = None
        if not (isinstance(result.get('session_id'), str)
                and result['session_id'].strip()):
            errors.append('result session_id is empty or not a string')
        elif init is not None and result['session_id'] != init['session_id']:
            errors.append(f'result session_id {result["session_id"]!r} does not match '
                          f'init session_id {init["session_id"]!r}')
        else:
            session_id = result['session_id']

    if response_text is not None:
        last_text = None
        for assistant in reversed(assistants):
            if assistant['texts']:
                last_text = assistant['texts'][-1]
                break
        if last_text is None:
            errors.append('cannot compare result text: no assistant text part exists')
        elif last_text != response_text:
            errors.append('result.result is not exactly equal (no trim) to the last '
                          'assistant text part')

    return {'protocol_success': not errors,
            'parse_errors': errors,
            'session_id': session_id,
            'observed_models': observed_models,
            'observed_tool_calls': observed_tool_calls,
            'response_text': response_text,
            'usage': result.get('usage') if result is not None else None,
            'model_usage': result.get('modelUsage') if result is not None else None,
            'permission_denials': permission_denials,
            'terminal_reason': terminal_reason}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workspace', required=True)
    ap.add_argument('--prompt-file', required=True)
    ap.add_argument('--output-dir', required=True)
    ap.add_argument('--stage', required=True)
    ap.add_argument('--model', required=True,
                    help='CodeBuddy model id; required, no auto/fallback default.')
    ap.add_argument('--tools', default='',
                    help='Comma list limited to Read,Write,Edit,Bash,Glob,Grep; '
                         'no duplicates or whitespace. Empty means zero tool visibility.')
    ap.add_argument('--allowed-tools', dest='allowed_tools', action='append', default=None)
    ap.add_argument('--disallowed-tools', dest='disallowed_tools', action='append', default=None)
    ap.add_argument('--resume-session-id', dest='session_id', default=None)
    ap.add_argument('--config', default=str(DEFAULT_CONFIG))
    args = ap.parse_args()

    # ---- preflight：全部校验通过前不创建任何输出目录（失败退出码 2，零创建）----
    try:
        if not args.stage or not args.stage.strip():
            raise ValueError('--stage is required and must be non-empty')
        if not args.model or not args.model.strip():
            raise ValueError('--model is required and must be non-empty (no fallback)')
        cfg = load_entry_config(args.config)
        work = Path(args.workspace).resolve(strict=True)
        if not work.is_dir():
            raise ValueError(f'workspace is not an existing directory: {args.workspace}')
        prompt_path = Path(args.prompt_file).resolve(strict=True)
        prompt_bytes = prompt_path.read_bytes()
        try:
            prompt = prompt_bytes.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError(f'prompt file is not valid UTF-8: {prompt_path}: {exc}')
        if not prompt.strip():
            raise ValueError(f'prompt file is empty: {prompt_path}')
        tools_items = parse_tools_arg(args.tools)
        perms = effective_permission_rules(tools_items, args.allowed_tools,
                                            args.disallowed_tools)
        out = Path(args.output_dir).resolve()
        if out.exists():
            raise ValueError(f'output directory already exists (refusing to '
                             f'overwrite/replay): {out}')
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({'preflight_error': str(exc), 'exit_code': 2},
                         ensure_ascii=False))
        return 2
    out.mkdir(parents=True, exist_ok=False)

    argv = build_argv(cfg, str(work), args.model, tools_items, args.session_id,
                      allowed_tools=args.allowed_tools,
                      disallowed_tools=args.disallowed_tools)
    contract = ('Final response must contain only the complete nine-section report. '
                'First line: WORKER_REPORT_START. Last line: WORKER_REPORT_END. '
                'Those markers must appear exactly once each; never quote them in the body. '
                'No preface, epilogue, or code fences. Keep field names and values on the same line: '
                f'阶段编号与执行方式：{args.stage}；direct。 '
                f'实际项目绝对路径：{work}。 Use the exact path without punctuation in its field.')
    stdin_payload = (contract + '\n\n' + prompt).encode('utf-8')
    request = {'started_at': datetime.now(timezone.utc).isoformat(),
               'workspace': str(work), 'prompt_file': str(prompt_path),
               'prompt_sha256': hashlib.sha256(prompt_bytes).hexdigest(),
               'model_requested': args.model, 'tools': args.tools,
               'tools_items': tools_items,
               'allowed_tools': list(perms['allowed_tools']),
               'disallowed_tools': list(perms['disallowed_tools']),
               'stage': args.stage, 'resume_session_id': args.session_id,
               'entry_config': str(Path(args.config).resolve()),
               'runtime': {'node': cfg['node'], 'cli': cfg['cli']},
               'argv': argv}
    (out / 'request.json').write_bytes(json.dumps(request, ensure_ascii=False,
                                                  indent=2).encode('utf-8'))
    env = os.environ.copy()
    env['DISABLE_AUTOUPDATER'] = '1'
    with (out / 'stdout.jsonl').open('wb') as stdout, \
            (out / 'stderr.log').open('wb') as stderr:
        child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout,
                                 stderr=stderr, cwd=str(work), env=env)
        (out / 'process.json').write_bytes(json.dumps(
            {'pid': child.pid, 'state': 'running'}, ensure_ascii=False).encode('utf-8'))
        child.communicate(input=stdin_payload)
    exit_code = child.returncode
    parsed = parse_stream((out / 'stdout.jsonl').read_bytes(),
                          args.model, tools_items)
    protocol_success = bool(parsed['protocol_success'] and exit_code == 0)
    if exit_code != 0:
        parsed['parse_errors'].append(f'process exited non-zero: {exit_code}')
    summary = {'protocol_success': protocol_success,
               'report_bound': False,
               'session_id': parsed['session_id'],
               'model_requested': args.model,
               'observed_models': parsed['observed_models'],
               'observed_tool_calls': parsed['observed_tool_calls'],
               'usage': parsed['usage'],
               'model_usage': parsed['model_usage'],
               'permission_denials': parsed['permission_denials'],
               'parse_errors': parsed['parse_errors'],
               'exit_code': exit_code,
               'terminal_reason': parsed['terminal_reason'],
               'business_verified': False,
               'free_quota_verified': False,
               'model_backend_identity_verified': False,
               'usage_billing_basis': 'unknown; raw usage saved without interpretation',
               'finished_at': datetime.now(timezone.utc).isoformat(),
               'output_dir': str(out)}
    if parsed['response_text'] is not None:
        (out / 'response.md').write_bytes(parsed['response_text'].encode('utf-8'))
        disk = (out / 'response.md').read_bytes()
        digest = hashlib.sha256(disk).hexdigest()
        summary['response_sha256'] = digest
        readback_match = digest == hashlib.sha256(
            parsed['response_text'].encode('utf-8')).hexdigest()
        body = analyze_report(parsed['response_text'], args.stage, str(work))
        binding = finalize_binding(
            body['body_ok'], body['reasons'],
            protocol_success=protocol_success,
            session_id=parsed['session_id'],
            requested_session_id=args.session_id,
            readback_match=readback_match)
        report_state = {**body, 'response_sha256': digest,
                        'readback_match': readback_match,
                        'protocol_success': protocol_success,
                        'session_id': parsed['session_id'],
                        'requested_session_id': args.session_id,
                        'binding': binding,
                        'bound': binding['bound'],
                        'reasons': binding['reasons']}
    else:
        report_state = {'bound': False, 'body_ok': False,
                        'protocol_success': protocol_success,
                        'reasons': ['no parseable text result in stream',
                                    *parsed['parse_errors']],
                        'carrier_missing': True}
    (out / 'report-state.json').write_bytes(json.dumps(report_state, ensure_ascii=False,
                                                       indent=2).encode('utf-8'))
    summary['report_bound'] = bool(report_state.get('bound', False))
    summary['report_state_file'] = str(out / 'report-state.json')
    (out / 'summary.json').write_bytes(json.dumps(summary, ensure_ascii=False,
                                                  indent=2).encode('utf-8'))
    (out / 'process.json').write_bytes(json.dumps(
        {'pid': child.pid, 'state': 'exited', 'exit_code': exit_code},
        ensure_ascii=False).encode('utf-8'))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if (protocol_success and summary['report_bound']) else 3


if __name__ == '__main__':
    raise SystemExit(main())
