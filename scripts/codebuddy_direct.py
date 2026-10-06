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
  终态 result 分两类分别处理：
  * 成功终态：subtype=success、is_error=false、result 非空字符串、
    与最后 assistant 的最后一个 text 分片精确一致（不 trim）；
  * 失败信封：仅 subtype=error_during_execution AND is_error=true 且带非空
    list[str] errors（官方形状）才算“解析有效但交付失败”的合法信封，可结构解析
    （parse_success/failure_envelope_valid 为真）而 protocol_success=false；缺/空
    errors、矛盾 subtype/is_error 记为结构错误、不声明合法已解析失败。提取
    primary_failure、原始 errors/errors_info、仅在实际提取到 reset 提示时提窗口、
    failure_stage、recoverability；429 只认明确 status/code=429，不以正文数字或
    单纯 quota 推断。失败信封若携带 result 文本原样落盘待验、不绑定；无 result 不
    生成报告。CLI exit 0 也不能伪报成功（解析正确 ≠ 交付成功）。
- 重复 init：官方 CLI 2.161.1 在同会话历史回放/重初始化时会再次发出 init（本机真实
  流的第 1、146 行两次 init 除 __timestamp 外全字段逐值相同，见
  references/codebuddy-direct.md 与 inputs/independent-runtime-provenance.json，
  重复的具体原因未知、不断言压缩）。仅白名单可变传输字段 __timestamp 外，比较两个
  init 的全字段键集与逐值：cwd/apiKeySource/agent/permissions 等既有字段、以及任何
  新增/删除/变化的未知字段漂移一律拒绝；每次 init 仍校验必备字段/类型（tools 为
  list[str]、mcp_servers 为 []、permissionMode 为 dontAsk）且既有安全字段不能缺，
  并记录差异/缺失（reinit_events），不忽略所有元数据、不放宽所有未知字段。
- 普通工具失败：user.tool_result 里的 `File does not exist`、`<tool_use_error>`、
  is_error=true 一律记录到 tool_failures（行号/tool_use_id/flags/摘录 + 统计），
  is_error=true 但 content 缺失/空也仍记为失败（excerpt 可为 None，行/id/flags 正确），
  但只是诊断信息，不自动把后来合法的修复成功判为假失败，也不单独令协议失败；
  权限拒绝单独 fail-closed（is_error=false 里藏拒绝同样拒绝，空
  result.permission_denials 不作为无拒绝证据）。
- 严格终态：
  * stdout 必须严格 UTF-8；任何非法字节保留原件并拒绝，禁止 errors='replace'
  * init.session_id 非空；协议事件凡带 session_id 字段就必须是非空字符串，否则拒绝
    （不得因 sid 空而跳过核对）；所有带 session_id 的事件必须与 init 完全一致，不得漂移
  * init.model 及所有 assistant.message.model 与请求模型精确一致
  * init.mcp_servers 严格为空 list（非空即拒绝，不得宣称零 MCP）
  * init.tools 视为完整注册表，不参与有效工具面判定；实际调用工具（tool_use.name）
    不得超出请求 --tools 白名单，否则视为越权
  * terminal_reason 为 cancelled/aborted/max_turns 一律判协议失败
- 用量口径未知：usage/modelUsage 仅原样保存 raw 值；business_verified /
  free_quota_verified / model_backend_identity_verified 恒为 false；cost_usd=0
  不视为免费证据；缓存分项不得与 input_tokens 重复累加。
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
# 失败信封 reset 窗口提示（仅从原始 errors/errors_info 文本提取，不改写）。
RESET_HINT_RE = re.compile(r'将在[^。，,；;]*?重置')
# 原始错误文本开头的独立 429 错误码（如 "429 ..."、"429："、"HTTP 429"）。仅认作为
# 独立错误码出现在开头、随后为分隔/结尾，不认 id 或正文中间的 429 数字。
RATE_LIMIT_CODE_RE = re.compile(
    r'^\s*(?:HTTP[\s/]*)?429(?=$|[\s:：，,、；;.。！!])', re.IGNORECASE)
# 重复 init 必须齐全且与首个 init 逐值一致的安全身份键（官方 CLI 会在同会话历史
# 回放/重初始化时再次发出 init）。除下列明确白名单的可变传输字段外，还要比较两个
# init 的全字段键集与逐值，任何新增/删除/变化的未知字段同样拒绝。
INIT_IDENTITY_KEYS = ('session_id', 'model', 'mcp_servers', 'tools', 'permissionMode')
# 已观察到的、每次重初始化都会合理变化的传输字段（本机真实流的两次 init 仅此项不同）。
INIT_VARIABLE_TRANSPORT_KEYS = ('__timestamp',)


def _describe_primary_failure(raw_errors, raw_errors_info) -> str | None:
    """从原始 errors / errors_info 提炼主失败一行摘要；不臆造，缺失返回 None。"""
    if isinstance(raw_errors_info, list):
        for item in raw_errors_info:
            if isinstance(item, dict):
                status = item.get('status')
                code = item.get('code')
                category = item.get('category')
                details = item.get('details') or item.get('message')
                tag = ' '.join(str(x) for x in (status, code, category) if x is not None)
                if isinstance(details, str) and details.strip():
                    return f'{tag}: {details.strip()}'.strip(': ').strip()
                if tag:
                    return tag
    if isinstance(raw_errors, list):
        for entry in raw_errors:
            if isinstance(entry, str) and entry.strip():
                return entry.strip()
            if isinstance(entry, dict):
                detail = entry.get('details') or entry.get('message')
                if isinstance(detail, str) and detail.strip():
                    return detail.strip()
    return None


def _failure_text_pool(raw_errors, raw_errors_info) -> list[str]:
    """收集全部 errors 与 errors_info 的原始文本（含 details/message），供 reset
    扫描；不只第一个 primary_failure，避免后续错误里的重置提示丢失。原件不被改写。"""
    pool: list[str] = []
    if isinstance(raw_errors, list):
        for entry in raw_errors:
            if isinstance(entry, str):
                pool.append(entry)
            elif isinstance(entry, dict):
                for key in ('details', 'message'):
                    value = entry.get(key)
                    if isinstance(value, str):
                        pool.append(value)
    if isinstance(raw_errors_info, list):
        for item in raw_errors_info:
            if isinstance(item, dict):
                for key in ('details', 'message'):
                    value = item.get(key)
                    if isinstance(value, str):
                        pool.append(value)
    return pool


def _extract_reset_hint(raw_errors, raw_errors_info) -> str | None:
    """扫描全部 errors / errors_info 文本提取“将在 … 重置”窗口提示；无则返回 None，
    不臆造 reset 时间或频率窗口。"""
    for text in _failure_text_pool(raw_errors, raw_errors_info):
        match = RESET_HINT_RE.search(text)
        if match:
            return match.group(0)
    return None


def _is_explicit_rate_limit(raw_errors, raw_errors_info) -> bool:
    """仅认明确的 status==429 或独立错误码==429，或原始错误文本**开头**的独立 429
    错误码（如 "429 ..."、"429："、"HTTP 429"）；不以正文里/id 里的 429 数字、或
    单纯 category=quota 推断限流。"""
    def hit(container):
        if isinstance(container, list):
            for item in container:
                if isinstance(item, dict) and (item.get('status') == 429
                                               or item.get('code') == 429):
                    return True
        return False
    if hit(raw_errors_info) or hit(raw_errors):
        return True
    for text in _failure_text_pool(raw_errors, raw_errors_info):
        if RATE_LIMIT_CODE_RE.match(text):
            return True
    return False


def _classify_recoverability(raw_errors, raw_errors_info, reset_hint) -> str:
    """限流类失败给出保守的可恢复描述；只有实际提取到 reset 提示才提窗口，
    无 reset 明确标注未知，来源未证实时不推断可重试成功。"""
    if not _is_explicit_rate_limit(raw_errors, raw_errors_info):
        return 'unknown: recoverability not established from the available result fields'
    if reset_hint:
        return ('rate_limited: CLI supplied an explicit reset window; the upstream '
                'source (platform / GLM channel / account window) is unverified; do '
                'not blind-rerun the full task or silently switch models')
    return ('rate_limited: explicit 429 but no reset window was supplied, so the '
            'reset time is unknown; do not blind-rerun the full task or silently '
            'switch models')


def _tool_failure_stats(tool_failures) -> dict:
    total = len(tool_failures)
    by_flag: dict[str, int] = {}
    for failure in tool_failures or []:
        for flag in failure.get('flags', []):
            by_flag[flag] = by_flag.get(flag, 0) + 1
    return {'total': total, 'by_flag': by_flag}


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
    """严格解析 stream-json JSONL。成功终态与失败信封分别处理：
    success 要求 result 非空字符串且与最后 assistant 的最后一个 text 分片逐字一致，
    且 errors 为空（非空不得绿灯）。仅 subtype=error_during_execution AND is_error=true
    且带非空 list[str] errors 才算合法失败信封：可结构解析（parse_success/
    failure_envelope_valid 为真）而 protocol_success=false；缺/空 errors、矛盾
    subtype/is_error 记为结构错误、不声明合法已解析失败。失败信封若携带 result 文本
    原样落盘待验、不绑定，无 result 不生成报告。429 只认明确 status/code=429，
    reset 仅在实际从全部 errors/errors_info 提取到窗口时提及，否则标未知。
    重复 init：除白名单可变字段 __timestamp 外比较两个 init 的全字段键集与逐值，
    任何新增/删除/变化字段（含 cwd/apiKeySource/agent/未知字段）或缺失一律拒绝，
    每次 init 仍校验 tools list[str]/mcp []/permissionMode dontAsk；未知事件、
    空/重复 result、尾随非 JSON、缺失终态、越权 tool_use、非空 MCP、session/model 漂移、
    空 session_id 字段一律拒绝。stdout 严格 UTF-8（不 replace）；permission_denials=[]
    不作为无拒绝证据——命中权限拒绝文案即 fail-closed；普通 File does not exist /
    <tool_use_error> / is_error=true 记入 tool_failures 仅诊断，is_error=true 但
    content 缺失/空也记为失败（excerpt 可为 None），不单独令协议失败、也不把后续合法
    修复成功判为假失败。"""
    empty = {'protocol_success': False, 'parse_errors': [], 'session_id': None,
             'observed_models': [], 'observed_tool_calls': [], 'response_text': None,
             'usage': None, 'model_usage': None, 'permission_denials': [],
             'terminal_reason': None, 'terminal_state': 'none',
             'parse_success': False, 'failure_envelope': False,
             'failure_envelope_valid': False, 'primary_failure': None, 'errors': None,
             'errors_info': None, 'reset_hint': None, 'failure_stage': None,
             'recoverability': None, 'carried_result': None,
             'tool_failures': [], 'reinit_events': []}
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
    tool_failures: list[dict] = []
    reinit_events: list[dict] = []
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
        if 'session_id' in event:
            sid = event.get('session_id')
            if isinstance(sid, str) and sid.strip():
                session_ids_seen.add(sid)
            else:
                # 带 session_id 字段但为空/非字符串：不得因 sid 空而跳过身份核对，
                # 一律拒绝（身份规则直接路径）。
                errors.append(f'line {lineno}: event carries session_id that is '
                              f'empty or not a string ({sid!r}); rejected')
        etype = event.get('type')
        if etype == 'system':
            subtype = event.get('subtype')
            if subtype == 'init':
                if init is None:
                    missing = [k for k in ('session_id', 'model', 'tools', 'mcp_servers',
                                           'permissionMode') if k not in event]
                    if missing:
                        errors.append(f'line {lineno}: init event missing fields {missing}')
                        continue
                    if not (isinstance(event['session_id'], str)
                            and event['session_id'].strip()):
                        errors.append(f'line {lineno}: init session_id is empty or not a string')
                        continue
                    if not isinstance(event['model'], str) or not event['model'].strip():
                        errors.append(f'line {lineno}: init model is empty or not a string')
                        continue
                    if not (isinstance(event['tools'], list)
                            and all(isinstance(t, str) for t in event['tools'])):
                        errors.append(f'line {lineno}: init tools must be a list of strings')
                        continue
                    if event['mcp_servers'] != []:
                        errors.append(f'line {lineno}: init.mcp_servers must be strictly '
                                      f'[] (got {event["mcp_servers"]!r}); non-empty means '
                                      f'MCP is not zero')
                        continue
                    if event['permissionMode'] != 'dontAsk':
                        errors.append(f'line {lineno}: init permissionMode must be '
                                      f'"dontAsk" (got {event["permissionMode"]!r})')
                        continue
                    init = event
                else:
                    # 官方 CLI 2.161.1 在同会话历史回放/重初始化时会再次发出 init。
                    # 除白名单可变传输字段（__timestamp）外，比较两个 init 的全字段键集
                    # 与逐值：任何身份字段漂移、新增/删除的未知字段一律拒绝，既有安全字段
                    # 不能缺；仍记录差异/缺失，不把所有元数据忽略。
                    absent = [k for k in INIT_IDENTITY_KEYS if k not in event]
                    first_keys = set(init) - set(INIT_VARIABLE_TRANSPORT_KEYS)
                    dup_keys = set(event) - set(INIT_VARIABLE_TRANSPORT_KEYS)
                    added_keys = sorted(dup_keys - first_keys)
                    removed_keys = sorted(first_keys - dup_keys - set(absent))
                    changed = sorted(k for k in (first_keys & dup_keys)
                                     if event[k] != init[k])
                    identical = not (absent or added_keys or removed_keys or changed)
                    reinit_events.append({'line': lineno, 'identical': identical,
                                          'changed_fields': changed,
                                          'missing_identity_fields': absent,
                                          'added_fields': added_keys,
                                          'removed_fields': removed_keys})
                    if absent:
                        errors.append(f'line {lineno}: duplicate init missing identity '
                                      f'fields {absent}; rejected')
                    if changed:
                        errors.append(f'line {lineno}: duplicate init identity drift on '
                                      f'{changed}; rejected')
                    if added_keys:
                        errors.append(f'line {lineno}: duplicate init added unknown '
                                      f'fields {added_keys}; rejected')
                    if removed_keys:
                        errors.append(f'line {lineno}: duplicate init removed fields '
                                      f'{removed_keys}; rejected')
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
                is_err = block.get('is_error')
                joined = _extract_tool_result_text(block)
                if joined is None:
                    # 内容缺失/空但 is_error=true：仍记为失败（id/行/flags 正确），
                    # excerpt 可为 None，不再静默丢弃。
                    if is_err is True:
                        tool_failures.append({
                            'line': lineno,
                            'tool_use_id': block.get('tool_use_id'),
                            'is_error': is_err,
                            'flags': ['is_error_true'],
                            'excerpt': None,
                        })
                    continue
                if PERMISSION_DENIAL_RE.search(joined):
                    permission_denials.append({
                        'source': 'user.tool_result.content.text',
                        'tool_use_id': block.get('tool_use_id'),
                        'is_error': block.get('is_error'),
                        'excerpt': joined,
                    })
                    # 空 result.permission_denials 不作为无拒绝证据；命中即 fail-closed
                    # is_error=false 里藏拒绝同样 fail-closed。权限拒绝优先于普通工具失败。
                    errors.append(f'line {lineno}: permission denial present in '
                                  f'user.tool_result.content.text (fail-closed)')
                    continue
                # 普通工具失败仅作诊断记录（File does not exist / <tool_use_error> /
                # is_error=true），不自动把后来合法的修复成功判为假失败，也不单独令协议失败。
                flags: list[str] = []
                is_err = block.get('is_error')
                if is_err is True:
                    flags.append('is_error_true')
                if '<tool_use_error>' in joined:
                    flags.append('tool_use_error')
                if 'File does not exist' in joined:
                    flags.append('file_not_found')
                if flags:
                    tool_failures.append({
                        'line': lineno,
                        'tool_use_id': block.get('tool_use_id'),
                        'is_error': is_err,
                        'flags': flags,
                        'excerpt': joined,
                    })
        elif etype == 'result':
            if results:
                errors.append(f'line {lineno}: duplicate result event')
            # 'result' 文本只对成功终态强制；error_during_execution 失败信封合法地
            # 只带 errors 而无 result 字段，不能因此判成结构损坏。
            required = ['subtype', 'is_error', 'session_id', 'usage', 'modelUsage']
            if event.get('subtype') == 'success' and event.get('is_error') is False:
                required.append('result')
            missing = [k for k in required if k not in event]
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

    if tools_whitelist is not None:
        wl = set(tools_whitelist)
        for name in observed_tool_calls:
            if name not in wl:
                errors.append(f'observed tool_use {name!r} exceeds requested '
                              f'--tools whitelist {sorted(wl)}')

    terminal_state = 'none'
    failure_envelope = False
    failure_envelope_valid = False
    primary_failure = None
    raw_errors = None
    raw_errors_info = None
    reset_hint = None
    failure_stage = None
    recoverability = None
    response_text = None
    carried_result = None

    if result is not None:
        terminal_reason = result.get('terminal_reason')
        subtype = result.get('subtype')
        is_error = result.get('is_error')
        # session_id 与 permission_denials 在任何终态形状下都必须核对。
        if not (isinstance(result.get('session_id'), str)
                and result['session_id'].strip()):
            errors.append('result session_id is empty or not a string')
        elif init is not None and result['session_id'] != init['session_id']:
            errors.append(f'result session_id {result["session_id"]!r} does not match '
                          f'init session_id {init["session_id"]!r}')
        else:
            session_id = result['session_id']
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
        if terminal_reason in BAD_TERMINAL_REASONS:
            errors.append(f'result terminal_reason {terminal_reason!r} is a '
                          f'rejected terminal state')

        if subtype == 'success' and is_error is False \
                and terminal_reason not in BAD_TERMINAL_REASONS:
            # 成功交付：额外校验非空 assistant text 与 result 逐字一致（不 trim）。
            terminal_state = 'success'
            # 成功信封 errors 边界：缺字段兼容；空 list 兼容；非空 list 拒绝；
            # 存在但不是 list（string/dict/None）一律拒绝。原始值（不 trim）保留到
            # result_errors，即使非 list 异常也原样保留。
            succ_errors = result.get('errors')
            raw_errors = succ_errors
            if 'errors' in result and not isinstance(succ_errors, list):
                errors.append(f'success result carries errors that is not a list '
                              f'(got {type(succ_errors).__name__}); cannot be '
                              f'greenlit')
            elif isinstance(succ_errors, list) and succ_errors:
                errors.append('success result carries non-empty errors; cannot be '
                              'greenlit')
            if not any(a['texts'] and any(t.strip() for t in a['texts'])
                       for a in assistants):
                errors.append('no assistant event carries a non-empty text part')
            response_text = result.get('result')
            if not (isinstance(response_text, str) and response_text.strip()):
                errors.append('success result text is empty or not a string')
                response_text = None
            if response_text is not None:
                last_text = None
                for assistant in reversed(assistants):
                    if assistant['texts']:
                        last_text = assistant['texts'][-1]
                        break
                if last_text is None:
                    errors.append('cannot compare result text: no assistant text '
                                  'part exists')
                elif last_text != response_text:
                    errors.append('result.result is not exactly equal (no trim) to '
                                  'the last assistant text part')
        elif subtype == 'error_during_execution' or is_error is True:
            # 错误终态：区分“解析有效”与“交付失败”。只有 subtype=error_during_execution
            # AND is_error=true 且有非空 list[str] errors（官方形状）才算合法失败信封：
            # 可结构解析（parse_success 不因该信封而变 false）但 protocol_success 仍 false。
            # 缺/空 errors、矛盾 subtype/is_error 不得声明为合法已解析失败——记为结构错误。
            terminal_state = 'error'
            raw_errors = result.get('errors')
            raw_errors_info = result.get('errors_info')
            primary_failure = _describe_primary_failure(raw_errors, raw_errors_info)
            carried = result.get('result')
            if isinstance(carried, str) and carried:
                # 失败信封若携带 result 文本，原样落盘待验、不绑定。
                carried_result = carried
            envelope_valid = (subtype == 'error_during_execution'
                              and is_error is True
                              and isinstance(raw_errors, list) and len(raw_errors) > 0
                              and all(isinstance(e, str) and e.strip() for e in raw_errors))
            if envelope_valid:
                failure_envelope = True
                failure_envelope_valid = True
                failure_stage = 'result_terminal'
                reset_hint = _extract_reset_hint(raw_errors, raw_errors_info)
                recoverability = _classify_recoverability(raw_errors, raw_errors_info,
                                                           reset_hint)
                # 交付失败根因（subtype/is_error/errors 与退出码）单独归类，不混入
                # parse_errors 结构错误；protocol_success 由 terminal_state 判 false。
            else:
                problems = []
                if subtype != 'error_during_execution':
                    problems.append(f'subtype {subtype!r} is not error_during_execution')
                if is_error is not True:
                    problems.append(f'is_error {is_error!r} is not true')
                if not isinstance(raw_errors, list) or not raw_errors:
                    problems.append('errors missing or empty')
                elif not all(isinstance(e, str) and e.strip() for e in raw_errors):
                    problems.append('errors is not a non-empty list of non-empty strings')
                errors.append('error terminal is not a valid failure envelope: '
                              + '; '.join(problems))
        else:
            terminal_state = 'error'
            errors.append(f'result subtype {subtype!r} / is_error {is_error!r} is '
                          f'neither a valid success terminal nor a recognized '
                          f'error_during_execution failure envelope')

    parse_success = not errors
    return {'protocol_success': parse_success and terminal_state == 'success',
            'parse_success': parse_success,
            'parse_errors': errors,
            'session_id': session_id,
            'observed_models': observed_models,
            'observed_tool_calls': observed_tool_calls,
            'response_text': response_text,
            'usage': result.get('usage') if result is not None else None,
            'model_usage': result.get('modelUsage') if result is not None else None,
            'permission_denials': permission_denials,
            'terminal_reason': terminal_reason,
            'terminal_state': terminal_state,
            'failure_envelope': failure_envelope,
            'failure_envelope_valid': failure_envelope_valid,
            'primary_failure': primary_failure,
            'errors': raw_errors,
            'errors_info': raw_errors_info,
            'reset_hint': reset_hint,
            'failure_stage': failure_stage,
            'recoverability': recoverability,
            'carried_result': carried_result,
            'tool_failures': tool_failures,
            'reinit_events': reinit_events}


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
               'terminal_state': parsed.get('terminal_state'),
               'parse_success': parsed.get('parse_success'),
               'failure_envelope': parsed.get('failure_envelope'),
               'failure_envelope_valid': parsed.get('failure_envelope_valid'),
               'primary_failure': parsed.get('primary_failure'),
               'result_errors': parsed.get('errors'),
               'errors_info': parsed.get('errors_info'),
               'reset_hint': parsed.get('reset_hint'),
               'failure_stage': parsed.get('failure_stage'),
               'recoverability': parsed.get('recoverability'),
               'tool_failures': parsed.get('tool_failures'),
               'tool_failure_stats': _tool_failure_stats(parsed.get('tool_failures')),
               'reinit_events': parsed.get('reinit_events'),
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
                        'parse_success': parsed.get('parse_success'),
                        'failure_envelope_valid': parsed.get('failure_envelope_valid'),
                        'reasons': ['no parseable text result in stream',
                                    *parsed['parse_errors']],
                        'carrier_missing': True}
        if parsed.get('failure_envelope'):
            report_state['terminal_state'] = parsed.get('terminal_state')
            report_state['primary_failure'] = parsed.get('primary_failure')
            report_state['reset_hint'] = parsed.get('reset_hint')
            report_state['failure_stage'] = parsed.get('failure_stage')
            report_state['recoverability'] = parsed.get('recoverability')
            report_state['reasons'].insert(
                0, f'failure envelope: no final report carrier; delivery failed '
                   f'(protocol_success stays false)')
        if parsed.get('carried_result') is not None:
            # 失败信封携带的 result 文本原样落盘待验，不参与绑定（不生成 response.md）。
            (out / 'failure-result.pending.txt').write_bytes(
                parsed['carried_result'].encode('utf-8'))
            report_state['carried_result_pending'] = str(out / 'failure-result.pending.txt')
            report_state['carried_result_bound'] = False
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
