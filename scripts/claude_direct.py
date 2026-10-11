"""claude_direct — brain-worker Claude Code CLI / deepseek-flash 直连入口（离线可测）。

设计目标与边界（BW-CLAUDE-DEEPSEEK-S3）：
- 运行时 `claude`，容量池 `claude:deepseek-flash`（dispatch_pool 第 11 槽，容量 1，独立第三
  公平组，AUTO 可选）；模型精确 `deepseek-flash`，经 Anthropic 兼容入口
  `https://api.deepseek.com/anthropic`；**不支持任何模型回退**：把 default opus/sonnet/haiku/
  small-fast/subagent 一并钉死 deepseek-flash。
- CLI：官方 Node CLI 2.1.76（可执行 `node.exe` + `launcher_args=[<abs cli.js>]`），
  `-p --output-format stream-json --verbose --permission-mode dontAsk`（**绝不 bypass**）；
  **不支持 `--bare`**（2.1.76 无该开关）；`--tools`/`--setting-sources` **始终传**（空值也传）；
  `--allowedTools`/`--disallowedTools` **各只出现一次**，后跟全部规则（CLI 重复 flag 会覆盖）；
  `--strict-mcp-config` + 空 `--mcp-config`；`--disable-slash-commands`；stdin 传任务原文；
  `--append-system-prompt` 附带 prompt_contract 完整九节契约；可选 `--max-turns`。
- 凭据：私有 config `scripts/claude-entry.local.json` 由**受信任宿主默认加载**（入口不按文件名
  拒绝宿主；worker 工具不得读私有配置属工具作用域约束，非加载器禁令）；支持两种 token 来源：
  (a) 明确 token 环境变量（离线 stub 用），(b) Windows DPAPI `ConvertFrom-SecureString` 密文
  文件，启动时经官方 pwsh/powershell 无日志解密。明文**只进子进程 env**（`ANTHROPIC_API_KEY`），
  绝不进 argv/request/异常/证据。**清除继承的 AUTH_TOKEN/OAuth/SIMPLE 与代理**，绝不把宿主
  已注入的旧凭据/代理带进子进程。
- 生命周期：outdir 必须不存在（否则 0 Popen）；预检（argv 构造、**prompt SHA 从文件真实回读**比对、
  可选 --dispatch-plan 精确授权、凭据解析）在 dispatch 占位之前完成（pre-check/cred 错误 →
  0 Popen、零占位泄漏）；Popen 成功后立即 started=true，`mark_running` 记真实 wrapper PID，
  `bind_child` 回核 **`bound` 必须显式为 true**；解析/分类/限额记录全在受保护流程内，
  communicate 拿到真实退出码后**无条件**做终态释放并**保留真实释放结果**（capacity_release，
  不再 except-pass 吞掉返回值）；已启动但真实退出未知保留 launch_unknown 不假释放。
  退出码：通过=0、未派发=2、失败=3。
- 结果分型（S4 真实错误提取）：`is_error=true` 时即使 `subtype=success` 也把 result 字符串取入
  错误载体；`errors` 非空或 `parse_error` ⇒ 绝不 protocol_success；`tool_use.id`↔
  `tool_result.tool_use_id` 精确配对，**成功回执 ≥1** 才算工程成功（use_count>0 不够），
  缺失回执/全部权限拒绝/孤儿 result 分别记录；正文普通 429 文本与取消退出码 `4294967295`/`-1`
  绝不判限额；普通可恢复错误只作诊断。协议成功、执行证据、报告绑定各自独立。
- 证据持久化：请求/进程/发送任务/发送契约/raw stdout/stderr 的**脱敏原件**全部落盘；最终改写后
  刷新 `result_sha256` 并重算 `evidence_sha256`，429 限额以**最终 SHA**先 record_main_force_limit
  再释放本占位。离线结果 `business_verified=false`——只证协议与绑定，不证真实业务。
- 真实世界事实（与本模块离线证明分开）：官方 Node CLI 2.1.76 真实 Read/Write **已成功**
  （前轮 `error_max_turns`、原件保留无报告，独立记录不等同验收）；native 2.1.220/296 连接失败
  单列；Bash 未通过精确权限验收故默认不开放；费用未知（CLI 估算 ≠ 账单）；真实一次新入口
  Read/Write 验收由主脑执行，本 worker 不调 API。

本文件导入即无副作用；真实子进程仅在 main()/run() 中发生。离线测试用 injectable popen_factory。
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import prompt_contract as pc            # noqa: E402
import dispatch_pool as dp              # noqa: E402
import execution_control as ec          # noqa: E402
import qoder_direct as qd               # noqa: E402  复用 analyze_report/finalize_binding

RUNTIME = 'claude'
MODEL = 'deepseek-flash'
POOL_KEY = f'{RUNTIME}:{MODEL}'
BASE_URL = 'https://api.deepseek.com/anthropic'
PERMISSION_MODE = 'dontAsk'
# 默认钉死的全部模型别名，任何一项都不回退到 Claude/pro。
_PINNED_MODEL_ENVS = ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
                      'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
                      'ANTHROPIC_SMALL_FAST_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL')
# 继承自宿主的“敏感/污染”环境：绝不带进子进程（凭据、OAuth、simple 可见性、代理）。
_CLEARED_CHILD_ENV = ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY',
                      'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_SIMPLE',
                      'HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy',
                      'ALL_PROXY', 'all_proxy')
# 非必需流量与重试/超时：钉死，避免后台轮询/自动重试污染一次性派工。
_CHILD_FIXED_ENV = {'CLAUDE_CODE_MAX_RETRIES': '0', 'API_TIMEOUT_MS': '60000',
                    'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}

DEFAULT_CONFIG = _SCRIPTS_DIR / 'claude-entry.local.json'
EXAMPLE_CONFIG = _SCRIPTS_DIR / 'claude-entry.json.example'
# 仅作说明：worker 工具不得访问真实私有配置属工具作用域约束；入口加载器本身不按文件名拒绝。
PROTECTED_LOCAL_NAME = 'claude-entry.local.json'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------------------------ 配置加载
def load_entry_config(path=None) -> dict:
    """加载受信任宿主提供的入口配置（绝对、存在、JSON 对象）。

    受信任宿主**默认加载** `scripts/claude-entry.local.json`（path 缺省即该文件）；入口**不**
    按文件名拒绝宿主（worker 工具不得读取该私有文件属工具作用域约束，不是加载器禁令）。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.is_absolute():
        raise ValueError(f'entry config path must be absolute: {cfg_path}')
    if not cfg_path.is_file():
        raise FileNotFoundError(f'entry config file not found: {cfg_path}')
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    if not isinstance(cfg, dict):
        raise ValueError(f'entry config must be a JSON object: {cfg_path}')
    return cfg


# ------------------------------------------------------------------ 凭据解析（零日志）
def _dpapi_reveal(cipher_path: str, pwsh_path: str = 'powershell') -> str:
    """Windows DPAPI：把 ConvertFrom-SecureString 密文文件解回明文 token。

    只在受信任宿主真实运行时调用（离线测试走 token_env 分支，绝不触达此处）。解密结果仅
    返回给调用方注入子进程 env，绝不写日志/argv/证据。pwsh_path 由 root 填（官方宿主
    pwsh.exe；本机 powershell.exe 因模块加载冲突不可用时改指 pwsh）。"""
    if not Path(cipher_path).is_file():
        raise FileNotFoundError(f'DPAPI credential file not found: {cipher_path}')
    script = ('$s = Get-Content -Raw -Encoding UTF8 '
              f"'{cipher_path}' | ConvertTo-SecureString; "
              '[Net.NetworkCredential]::new("", $s).Password')
    proc = subprocess.run([pwsh_path, '-NoProfile', '-NonInteractive', '-Command', script],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        # 绝不回显脚本或密文；只给退出码。
        raise RuntimeError(f'DPAPI decrypt failed (exit {proc.returncode})')
    return proc.stdout.strip()


def resolve_token(cfg: dict, environ=None) -> str:
    """按 cfg['credential_source'] 解析出凭据明文（不落任何证据）。

    - mode='token_env'：从 environ[cfg...env] 读取（离线 stub 明确注入的环境变量）；
    - mode='credential_file'：DPAPI 密文文件解密（真实运行时，需 pwsh_path）。
    缺失/空一律拒绝启动（零 Popen），绝不猜、绝不用占位冒充真实凭据。"""
    env = os.environ if environ is None else environ
    cs = cfg.get('credential_source') or {}
    mode = cs.get('mode')
    if mode == 'token_env':
        var = cs.get('env') or 'ANTHROPIC_API_KEY'
        tok = env.get(var)
        if not tok:
            raise KeyError(f'credential_source.mode=token_env but env {var!r} is unset/empty')
        return str(tok)
    if mode == 'credential_file':
        path = cs.get('path')
        if not path:
            raise KeyError('credential_source.mode=credential_file requires path')
        return _dpapi_reveal(path, cs.get('pwsh_path', 'powershell'))
    raise ValueError(f'unsupported credential_source.mode: {mode!r}')


# ------------------------------------------------------------------ env / argv 构造
def build_subprocess_env(cfg: dict, token: str, environ=None) -> dict:
    """子进程环境：清除继承的敏感/代理变量 → 钉死 base_url + 全部模型别名 deepseek-flash +
    固定重试/超时/禁用非必需流量 + 凭据（**只进子进程 env**，绝不在 argv）。

    凭据放入 `ANTHROPIC_API_KEY`（Anthropic 兼容入口只认它）；绝不设 `CLAUDE_CODE_SIMPLE`
    （它会隐藏 Write/Edit）。cfg['env'] 可追加其它**只读、无秘密**的环境，但绝不许重新引入
    已清除的敏感/代理变量或 SIMPLE；base_url 钉死为指定 DeepSeek Anthropic 兼容入口。"""
    base_url = str(cfg.get('base_url', BASE_URL))
    if base_url != BASE_URL:
        raise ValueError(f'base_url must be {BASE_URL!r} for the pinned deepseek-flash '
                         'entry; refusing to send it to another endpoint')
    base = os.environ if environ is None else environ
    child = dict(base)
    for var in _CLEARED_CHILD_ENV:
        child.pop(var, None)
    extra = cfg.get('env') or {}
    for k, v in extra.items():
        if str(k) in _CLEARED_CHILD_ENV or str(k) == 'CLAUDE_CODE_SIMPLE':
            raise ValueError(f'cfg env must not reintroduce cleared/forbidden var {k!r}')
        child[str(k)] = str(v)
    for var in _CLEARED_CHILD_ENV:
        child.pop(var, None)  # 双保险：extras 之后再清一次，SIMPLE/旧凭据绝不进子进程
    child['ANTHROPIC_BASE_URL'] = BASE_URL
    for var in _PINNED_MODEL_ENVS:
        child[var] = cfg.get('model', MODEL)
    for k, v in _CHILD_FIXED_ENV.items():
        child[k] = v
    child['ANTHROPIC_API_KEY'] = token
    return child


def _validate_rule_list(value, field):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f'{field} must be a list of strings, got {type(value).__name__}')
    for item in value:
        if not isinstance(item, str) or item == '' or item.isspace():
            raise ValueError(f'{field} entries must be non-empty, non-whitespace: {item!r}')
        if item != item.strip():
            raise ValueError(f'{field} entry has surrounding whitespace: {item!r}')
    return list(value)


def build_argv(cfg: dict, workspace: str, model: str, contract: str) -> list[str]:
    """安全 argv（无 shell、无 token、无 bypass、无 --bare）。

    - **钉死口径（BW-CLAUDE-DEEPSEEK-S4）**：permission_mode 只接受 dontAsk（其它任何模式
      一律拒绝，不只 bypass）；model 必须是 deepseek-flash；base_url 必须是指定 DeepSeek
      Anthropic 兼容入口——不同模型/权限模式/端点直接拒绝（0 argv 产出，绝不带病启动）；
    - `--bare` 官方 Node CLI 2.1.76 不支持，cfg 出现即拒绝；
    - `executable` 可为 node.exe，配 `launcher_args=[<abs cli.js>]`（官方 Node CLI 2.1.76）；
    - `--tools` 与 `--setting-sources` **始终传**（即使空也传空值）；**默认无 Bash**
      （tools 为空显式即不开放任何工具，绝不隐式注入 Bash）；
    - `--allowedTools` / `--disallowedTools` **各只出现一次**，后跟全部规则；
    - `--strict-mcp-config` + 空 `--mcp-config`（无 MCP）；`--disable-slash-commands`；
    - 空值也照传，保证授权面可被 execution_control 精确比对。token 只在 env，不进这里。"""
    exe = cfg.get('executable')
    if not exe:
        raise ValueError('entry config missing executable path')
    if cfg.get('bare'):
        raise ValueError('--bare is not supported on the official Node CLI 2.1.76; refusing')
    launcher = cfg.get('launcher_args') or []
    if not isinstance(launcher, (list, tuple)) or any(not isinstance(x, str) or not x
                                                      for x in launcher):
        raise ValueError('launcher_args must be a list of non-empty strings')
    mode = str(cfg.get('permission_mode', PERMISSION_MODE))
    if mode != PERMISSION_MODE:
        raise ValueError(f'permission_mode must be {PERMISSION_MODE!r}, got {mode!r}; '
                         'bypassPermissions and every other mode are refused')
    if str(model) != MODEL:
        raise ValueError(f'model must be {MODEL!r} on this entry, got {model!r}; '
                         'no model substitution')
    if str(cfg.get('base_url', BASE_URL)) != BASE_URL:
        raise ValueError(f'base_url must be {BASE_URL!r}; refusing to point the pinned '
                         'deepseek-flash model at a different endpoint')
    tools = _validate_rule_list(cfg.get('tools'), 'tools')
    allowed = _validate_rule_list(cfg.get('allowed_tools'), 'allowed_tools')
    disallowed = _validate_rule_list(cfg.get('disallowed_tools'), 'disallowed_tools')
    add_dirs = _validate_rule_list(cfg.get('add_dirs'), 'add_dirs')
    setting_sources = _validate_rule_list(cfg.get('setting_sources'), 'setting_sources')
    argv = [str(exe)] + [str(a) for a in launcher] + [
        '-p', '--output-format', 'stream-json', '--verbose',
        '--model', model,
        '--permission-mode', mode]
    # 始终传（空值也传）：工具可见性与设置来源，供精确授权比对。
    argv += ['--tools', ','.join(tools)]
    argv += ['--setting-sources', ','.join(setting_sources)]
    # 每个 flag 只出现一次，后面跟全部规则（CLI 重复 flag 会覆盖前面的）。
    argv += ['--allowedTools', *allowed]
    argv += ['--disallowedTools', *disallowed]
    argv += ['--strict-mcp-config', '--mcp-config', json.dumps({'mcpServers': {}})]
    argv.append('--disable-slash-commands')
    for d in add_dirs:
        argv += ['--add-dir', d]
    settings = cfg.get('settings')
    if isinstance(settings, dict):
        argv += ['--settings', json.dumps(settings, ensure_ascii=False)]
    if cfg.get('max_turns') is not None:
        argv += ['--max-turns', str(int(cfg['max_turns']))]
    if contract and cfg.get('append_system_prompt', True):
        argv += ['--append-system-prompt', contract]
    return argv


# ------------------------------------------------------------------ stream-json 解析/分型
def _block_text(block) -> str:
    """取出 tool_result block 的文本（字符串或 content 块列表），仅供审批拒绝识别使用。"""
    c = block.get('content')
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for x in c:
            if isinstance(x, dict) and isinstance(x.get('text'), str):
                parts.append(x['text'])
            elif isinstance(x, str):
                parts.append(x)
        return '\n'.join(parts)
    return ''


# 审批拒绝标记：只认**开头独立**的文本，绝不把成功 Read 源码里的引用文字误当拒绝。
_APPROVAL_DENIAL_PREFIXES = (
    'permission to use', 'permission denied', 'tool use denied', 'tool denied',
    'requested permissions', 'requested permission', 'has not been granted',
    "hasn't been granted", 'was not granted', 'not granted', 'user denied',
    'the user denied', 'denied by the user', "you haven't granted",
    "the user doesn't want", 'claude requested permissions',
)


def _looks_like_approval_denial(text) -> bool:
    """文本是否**以**审批拒绝语开头（真实错误包/开头审批拒绝）。"""
    if not isinstance(text, str):
        return False
    s = text.strip().lower()
    return any(s.startswith(p) for p in _APPROVAL_DENIAL_PREFIXES)


def summarize_stream(lines) -> dict:
    """把 stream-json 逐行事件归并成事实：最终 result 事件、tool_use/tool_result 计数、
    is_error/subtype/session_id、assistant 文本。协议/工具证据独立保留，绝不合并冒充。
    非空但无法 JSON 解析的行不静默丢弃：计入 `malformed_lines`（parse_error 证据）。"""
    events = []
    malformed = 0
    for raw in lines:
        raw = raw.strip() if isinstance(raw, str) else raw
        if not raw:
            continue
        if not isinstance(raw, str):
            events.append(raw)
            continue
        try:
            events.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            malformed += 1
    tool_use, tool_result = [], []
    result_event = None
    session_id = None
    assistant_texts = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        et = ev.get('type')
        if et == 'result':
            result_event = ev
            session_id = ev.get('session_id') or session_id
        if ev.get('session_id'):
            session_id = ev.get('session_id')
        msg = ev.get('message') if isinstance(ev.get('message'), dict) else None
        content = (msg or {}).get('content')
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'tool_use':
                    tool_use.append(block)
                elif block.get('type') == 'tool_result':
                    tool_result.append(block)
                elif block.get('type') == 'text' and isinstance(block.get('text'), str):
                    assistant_texts.append(block['text'])
    subtype = (result_event or {}).get('subtype')
    is_error = bool((result_event or {}).get('is_error', False))
    # 回执配对（BW-CLAUDE-DEEPSEEK-S4）：tool_use.id ↔ tool_result.tool_use_id 精确匹配；
    # 错误回执、缺回执、permission_denials 分别记（普通可恢复 Read 错误只作诊断，
    # 绝不泛化成权限故障）。成功工程任务必须至少有 1 条**成功**回执，use_count>0 不算。
    use_ids = [b.get('id') for b in tool_use if isinstance(b.get('id'), str) and b.get('id')]
    result_by_id = {b.get('tool_use_id'): b for b in tool_result
                    if isinstance(b.get('tool_use_id'), str) and b.get('tool_use_id')}
    matched_ids = [i for i in use_ids if i in result_by_id]
    ok_receipt_ids = [i for i in matched_ids
                      if not (result_by_id[i].get('is_error')
                              or result_by_id[i].get('error'))]
    error_receipt_ids = [i for i in matched_ids if i not in ok_receipt_ids]
    missing_receipt_ids = [i for i in use_ids if i not in result_by_id]
    orphan_result_ids = sorted({k for k in result_by_id if k not in set(use_ids)})
    denials = (result_event or {}).get('permission_denials')
    denials = denials if isinstance(denials, list) else []
    # 重复/冲突终态：同一 tool_use.id 多次出现，或同一 tool_use_id 有多条 result（终态冲突）。
    from collections import Counter
    duplicate_use_ids = sorted(i for i, c in Counter(use_ids).items() if c > 1)
    result_use_ids = [b.get('tool_use_id') for b in tool_result
                      if isinstance(b.get('tool_use_id'), str) and b.get('tool_use_id')]
    duplicate_result_ids = sorted(i for i, c in Counter(result_use_ids).items() if c > 1)
    # 明确审批拒绝：结构化 permission_denials（可能 is_error=false），加上**错误回执**与 result
    # 错误载体里**开头独立**的审批拒绝文本。成功 Read 源码里的引用文字绝不看（is_error=false
    # 且无 error 的回执不参与识别）。
    denial_ids = set()
    for d in denials:
        tid = d.get('tool_use_id') if isinstance(d, dict) else None
        if isinstance(tid, str) and tid:
            denial_ids.add(tid)
    for b in tool_result:
        if isinstance(b, dict) and (b.get('is_error') or b.get('error')):
            if _looks_like_approval_denial(_block_text(b)):
                tid = b.get('tool_use_id')
                if isinstance(tid, str) and tid:
                    denial_ids.add(tid)
    approval_denial_in_result = False
    for key in ('result_errors', 'errors'):
        v = (result_event or {}).get(key)
        if isinstance(v, list):
            for item in v:
                text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                if _looks_like_approval_denial(text):
                    approval_denial_in_result = True
    if (result_event or {}).get('is_error'):
        approval_denial_in_result = (approval_denial_in_result
                                     or _looks_like_approval_denial(
                                         (result_event or {}).get('result')))
    return {'events': events, 'result_event': result_event, 'subtype': subtype,
            'is_error': is_error, 'session_id': session_id,
            'tool_use': tool_use, 'tool_result': tool_result,
            'tool_use_count': len(tool_use), 'tool_result_count': len(tool_result),
            'matched_receipt_ids': matched_ids, 'ok_receipt_ids': ok_receipt_ids,
            'error_receipt_ids': error_receipt_ids,
            'missing_receipt_ids': missing_receipt_ids,
            'orphan_result_ids': orphan_result_ids,
            'duplicate_use_ids': duplicate_use_ids,
            'duplicate_result_ids': duplicate_result_ids,
            'approval_denial_ids': sorted(denial_ids),
            'approval_denial_in_result': bool(approval_denial_in_result),
            'permission_denials': denials,
            'malformed_lines': malformed, 'parse_error': bool(malformed),
            'assistant_text': ''.join(assistant_texts)}


def _result_error_carrier(result_event: dict):
    """真实错误载体：errors 数组（result_errors / errors），或 is_error=true 时的 result
    字符串（即便 subtype==success——真实 API 错误就在 result 文本里）。返回
    (errors_list, errors_info, is_error)。**is_error=false 的成功正文绝不进错误载体**，
    因此普通报告正文里的 429 字样不会被扫描成限额。"""
    errors = result_event.get('result_errors')
    if not isinstance(errors, list):
        errors = result_event.get('errors')
    if not isinstance(errors, list):
        errors = []
    info = result_event.get('errors_info')
    is_error = bool(result_event.get('is_error', False))
    if is_error and not errors:
        result_text = result_event.get('result')
        if isinstance(result_text, str) and result_text.strip():
            errors = [result_text]
    return errors, info, is_error


def classify_result(summary: dict, exit_code: int, stderr_text: str,
                    *, is_engineering_task: bool) -> dict:
    """CLI 结果分型（协议成功 / 执行证据 / 报告绑定 各自独立，0 工具工程任务不冒成功）。

    真实错误载体：errors 数组或 is_error=true 的 result 字符串（即便 subtype==success）；
    只认**结构化/开头独立**的明确 429（`execution_control.explicit_429`），普通成功正文里的
    429 字样与取消退出码（4294967295/-1）绝不被误判为限额。"""
    result_event = summary.get('result_event') or {}
    errors, errors_info, carrier_error = _result_error_carrier(result_event)
    subtype_ok = summary.get('subtype') == 'success'
    is_error = bool(summary.get('is_error') or carrier_error)
    parse_error = bool(summary.get('parse_error'))
    # 协议成功必须同时满足：subtype=success、无 is_error、退出码 0、错误载体为空、
    # 无畸形行（errors 非空/parse_error 绝不 protocol_success）。取消退出码
    # （4294967295/-1）不是 429；普通正文的 429 字样进不了错误载体（is_error=false 不
    # 提取 result 文本），explicit_429 只认结构化/开头独立 429。
    protocol_success = bool(subtype_ok and not is_error and exit_code == 0
                            and not errors and not parse_error)
    quota_429 = ec.explicit_429(errors, errors_info) if (errors or errors_info) else False
    ok_receipts = summary.get('ok_receipt_ids') or []
    missing_receipts = summary.get('missing_receipt_ids') or []
    orphan_results = summary.get('orphan_result_ids') or []
    duplicate_uses = summary.get('duplicate_use_ids') or []
    duplicate_results = summary.get('duplicate_result_ids') or []
    denials_list = summary.get('permission_denials') or []
    approval_denial = bool(summary.get('approval_denial_ids') or denials_list
                           or summary.get('approval_denial_in_result'))
    zero_receipt_engineering = (is_engineering_task and not ok_receipts)
    # 执行证据门禁：协议成功之外，必须无缺失/孤儿/重复回执、无冲突终态、无明确审批拒绝，
    # 且工程任务至少 1 条**成功**回执。缺任一条即 execution_evidence_ok=False（不伪造成功）。
    conflict = bool(duplicate_uses or duplicate_results)
    execution_evidence_ok = bool(
        protocol_success and not missing_receipts and not orphan_results
        and not conflict and not approval_denial
        and (not is_engineering_task or ok_receipts))
    facts = {
        'exit_code': exit_code,
        'subtype': summary.get('subtype'),
        'is_error': is_error,
        'protocol_success': protocol_success,
        'quota_429': bool(quota_429),
        'parse_error': parse_error,
        'malformed_lines': summary.get('malformed_lines', 0),
        'tool_use_count': summary.get('tool_use_count', 0),
        'tool_result_count': summary.get('tool_result_count', 0),
        'ok_tool_receipts': len(ok_receipts),
        'error_tool_receipts': len(summary.get('error_receipt_ids') or []),
        'missing_tool_receipts': len(missing_receipts),
        'orphan_tool_results': len(orphan_results),
        'duplicate_tool_uses': len(duplicate_uses),
        'duplicate_tool_results': len(duplicate_results),
        'permission_denials': len(denials_list),
        'approval_denials': int(approval_denial),
        # 兼容旧字段名：工程任务必须有至少一条**成功**回执（不是仅 use_count>0）。
        'zero_tool_engineering': bool(zero_receipt_engineering),
        'stderr_present': bool(stderr_text),
        # 同义字段：execution_evidence_ok 与原 execution_success 完全一致。
        'execution_success': execution_evidence_ok,
        'execution_evidence_ok': execution_evidence_ok,
    }
    return facts


# ------------------------------------------------------------------ 证据落盘（脱敏）
def _redact(obj, secret: str):
    """把证据里的 token 明文彻底抹掉（防御性：env/secret/header 绝不入证据）。"""
    if not secret:
        return obj
    if isinstance(obj, str):
        return obj.replace(secret, '[REDACTED_TOKEN]')
    if isinstance(obj, list):
        return [_redact(x, secret) for x in obj]
    if isinstance(obj, dict):
        return {k: _redact(v, secret) for k, v in obj.items()}
    return obj


def write_evidence(out_dir: Path, *, events, facts: dict, report_text: str,
                   binding: dict, secret: str, task_sha: str,
                   request=None, process=None, sent_task=None, sent_contract=None,
                   raw_stdout=None, raw_stderr=None) -> dict:
    """落盘真实证据**原件**（脱敏 token，但不改写/裁剪报告正文；stdout/stderr/发送的
    任务与契约原文全部保存，BW-CLAUDE-DEEPSEEK-S4）。最终返回的 sha 一律对
    **最后写入的文件**回读计算（绝不沿用循环里的旧哈希）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    ev_path = out_dir / 'events.jsonl'
    with ev_path.open('w', encoding='utf-8', newline='\n') as fh:
        for ev in events:
            fh.write(_redact(json.dumps(ev, ensure_ascii=False), secret) + '\n')
    raw = {}
    for name, payload in (('request.json', request), ('process.json', process),
                          ('sent-contract.md', sent_contract), ('sent-task.md', sent_task),
                          ('raw-stdout.txt', raw_stdout), ('raw-stderr.txt', raw_stderr)):
        if payload is None:
            continue
        p = out_dir / name
        data = (json.dumps(payload, ensure_ascii=False, indent=2)
                if isinstance(payload, (dict, list)) else str(payload))
        p.write_bytes(_redact(data, secret).encode('utf-8'))
        raw[f'{name.rsplit(".", 1)[0].replace("-", "_")}_path'] = str(p)
        raw[f'{name.rsplit(".", 1)[0].replace("-", "_")}_sha256'] = _sha(p.read_bytes())
    report_path = out_dir / 'report.md'
    # 只对**精确已知 secret** 脱敏，其余原文逐字保留（不裁剪/不补写）；记录脱敏状态，
    # 绝不把 token 明文写进报告。回读比较的是脱敏后的预期字节。
    report_clean = _redact(report_text or '', secret)
    report_path.write_bytes(report_clean.encode('utf-8'))  # 不转换换行，保证回读一致
    report_redacted = bool(secret and report_clean != (report_text or ''))
    result_path = out_dir / 'claude-result.json'
    result_path.write_text(_redact(json.dumps({'facts': facts, 'binding': binding,
                                               'task_sha256': task_sha}, ensure_ascii=False,
                                             indent=2), secret), encoding='utf-8')
    summary_path = out_dir / 'claude-summary.json'
    result = {'events_jsonl': str(ev_path), 'result_json': str(result_path),
              'report_md': str(report_path), 'summary_json': str(summary_path),
              'events_sha256': _sha(ev_path.read_bytes()),
              'result_sha256': _sha(result_path.read_bytes()),
              'report_sha256': _sha(report_path.read_bytes()),
              'report_redacted': report_redacted, **raw}
    # 最后写入 summary（含上述事实哈希）并**回读其真实字节**计算最终 evidence_sha256。
    summary_path.write_text(_redact(json.dumps(result, ensure_ascii=False, indent=2), secret),
                            encoding='utf-8')
    result['evidence_sha256'] = _sha(summary_path.read_bytes())  # 最后写入文件回读
    return result


# ------------------------------------------------------------------ 编排（Popen 生命周期）
def _build_actual(cfg, *, task_id, stage, model, workspace, prompt_sha256, argv, task_text):
    """构造 execution_control 预检所需的 actual（与 plan 做双向精确集合比对）。"""
    tools = _validate_rule_list(cfg.get('tools'), 'tools')
    allowed = _validate_rule_list(cfg.get('allowed_tools'), 'allowed_tools')
    disallowed = _validate_rule_list(cfg.get('disallowed_tools'), 'disallowed_tools')
    add_dirs = _validate_rule_list(cfg.get('add_dirs'), 'add_dirs')
    # 直接复用 execution_control 的权威授权映射，绝不另造映射/手工填空（S5 修复：旧实现
    # 把 edits 错填为 []、tool_visibility 错填为全部可见工具，导致正确计划被误拒）。
    grants = ec.grants_from_rules(allowed, add_dirs=add_dirs,
                                  disallowed_rules=disallowed, tools_items=tools)
    return {'task_id': task_id, 'stage': stage, 'runtime': RUNTIME, 'model': model,
            'workspace': str(workspace), 'cwd': str(workspace),
            'prompt_sha256': prompt_sha256, 'prompt': task_text, 'argv': list(argv),
            'shell': False, 'grants': grants}


def run(cfg: dict, *, workspace: str, prompt_file: Path, out_dir: Path, stage: str,
        task_id: str, prompt_sha256: str, claim_token: str | None = None,
        is_engineering_task: bool = True, now=None, popen_factory=subprocess.Popen,
        environ=None, dispatch_store=None, child_prober=None, bind_child_pid=True,
        plan=None, active_tasks=None) -> dict:
    """真实编排：outdir/预检/凭据前置 → 时段+容量门 → Popen → 解析 → 绑定 → 终态释放。

    - outdir 必须不存在（存在即 0 Popen 拒绝，绝不覆写旧证据）；
    - --dispatch-plan 精确预检（execution_control.preflight，复用真实 grants）在占位之前完成；
      预检/凭据/argv 任一失败 → 0 Popen、零占位泄漏；
    - communicate 拿到真实退出码即释放终态（即便随后解析/落盘失败也不再泄漏）；
    - 已启动但真实退出未知 → launch_unknown，绝不假释放；
    - 明确限额 → 先 record_main_force_limit（带真实证据）再释放本占位。

    离线测试注入 popen_factory（假 Popen）+ token_env 环境，不产生 OS 子进程、不联网、
    不调真实模型。"""
    now = now or dp.utcnow()
    model = cfg.get('model', MODEL)

    # (0) outdir 必须不存在。
    if Path(out_dir).exists():
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                'reason': 'output_dir_exists',
                'reasons': [f'output dir {str(out_dir)!r} already exists; refusing to '
                            'overwrite prior evidence (choose a fresh dir)']}

    # (1) 构造 argv + 契约 + 任务（任一失败 → 0 Popen、零占位）。实际 prompt SHA 必须由
    # prompt 文件重算回核，绝不只信入参；入参（非空时）与文件哈希不符即拒。
    try:
        prompt_bytes = Path(prompt_file).read_bytes()
        actual_prompt_sha = _sha(prompt_bytes)
        if prompt_sha256 and str(prompt_sha256).lower() != actual_prompt_sha:
            raise ValueError(f'prompt sha256 recheck failed: file hashes {actual_prompt_sha} '
                             f'but caller asserted {prompt_sha256!r}')
        task_text = prompt_bytes.decode('utf-8')
        contract_text = pc.build_contract(stage, workspace, mode='direct')
        argv = build_argv(cfg, str(workspace), model, contract_text)
    except Exception as exc:  # noqa: BLE001
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                'reason': 'precheck_failed', 'error_class': type(exc).__name__,
                'reasons': [f'argv/prompt precheck failed before any claim: {exc!r}']}
    sent_payload = pc.compose_task_payload(contract_text, task_text)
    prompt_sha256 = actual_prompt_sha

    # (2) --dispatch-plan 精确预检（占位之前，0 Popen）。
    if plan is not None:
        try:
            actual = _build_actual(cfg, task_id=task_id, stage=stage, model=model,
                                   workspace=workspace, prompt_sha256=prompt_sha256,
                                   argv=argv, task_text=task_text)
            pf = ec.preflight(plan, actual, active_tasks=active_tasks)
        except Exception as exc:  # noqa: BLE001
            return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                    'reason': 'dispatch_plan_invalid', 'error_class': type(exc).__name__,
                    'reasons': [f'dispatch plan validation failed: {exc!r}']}
        if not pf.get('ok'):
            return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                    'reason': 'dispatch_plan_rejected', 'preflight': pf,
                    'reasons': pf.get('reasons') or []}

    # (3) 凭据解析（失败 → 0 Popen、零占位）。
    try:
        token = resolve_token(cfg, environ)
    except Exception as exc:  # noqa: BLE001
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                'reason': 'credential_unavailable', 'error_class': type(exc).__name__,
                'reasons': [f'credential resolution failed before any claim: {exc!r}']}

    child_env = build_subprocess_env(cfg, token, environ)
    secret = child_env.get('ANTHROPIC_API_KEY', '')

    # (4) 容量门 + claude 时段门（占位发生在 consume_for_entry 内）。
    gate = dp.consume_for_entry(
        dispatch_store, task_id=task_id, runtime=RUNTIME, model=model,
        workspace=str(workspace), prompt_sha256=prompt_sha256, stage=stage,
        claim_token=claim_token, now=now, executor=RUNTIME)
    if not gate.get('allowed'):
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False, 'gate': gate}
    claim = gate['token']

    started = False
    child_verified = False
    child_done = False
    proc = None
    stdout_text = ''
    stderr_text = ''
    exit_code = None
    try:
        proc = popen_factory(argv, cwd=str(workspace), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=child_env, text=True)
        # Popen 成功返回真实句柄 → 立即 started=true；running 已带 wrapper(os.getpid())。
        started = True
        dp.mark_running(dispatch_store, claim, wrapper_pid=os.getpid(), now=now)
        if bind_child_pid:
            bound = None
            if getattr(proc, 'pid', None) is not None:
                bound = dp.bind_child(dispatch_store, claim, int(proc.pid),
                                      wrapper_pid=os.getpid(), now=now)
            # bound 必须**明确为 true** 才算已核验：缺字段/false/None 一律未核验，绝不默认。
            child_verified = bool(isinstance(bound, dict) and bound.get('bound') is True)
            if not child_verified:
                # 生产默认绑定未明确成功：绝不把任务写入子进程（不调用 communicate(input=...)）、
                # 不杀进程、不假 start_failed/未知释放；保留活/未知占位供核查。
                try:
                    unknown_guard = dp.mark_unknown(dispatch_store, claim, now=now)
                except Exception as uexc:  # noqa: BLE001
                    unknown_guard = {'marked': False, 'error': type(uexc).__name__}
                return {'dispatch_failed': False, 'sent': False, 'dispatched': True,
                        'launch_unknown': True, 'started': True, 'token': claim,
                        'child_verified': False, 'bind_required': True,
                        'unknown_guard': unknown_guard, 'business_verified': False}
        stdout_text, stderr_text = proc.communicate(input=sent_payload)
        exit_code = getattr(proc, 'returncode', None)
        child_done = True  # communicate 返回 → 子进程真实退出码已知（确知已退出）
    except BaseException as exc:  # noqa: BLE001 - 终态处理必须兜底
        if started:
            # 已启动但真实退出未知（communicate/Popen 之后异常）→ 保留占位为 unknown，
            # 绝不假释放；真实 guard 结果保留供核查（异常也必须可查，不 except pass 冒称）。
            if not child_done:
                try:
                    unknown_guard = dp.mark_unknown(dispatch_store, claim, now=now)
                except Exception as uexc:  # noqa: BLE001
                    unknown_guard = {'marked': False, 'error': type(uexc).__name__}
                return {'dispatch_failed': False, 'sent': False, 'dispatched': True,
                        'launch_unknown': True, 'started': True, 'token': claim,
                        'unknown_guard': unknown_guard,
                        'error_class': type(exc).__name__, 'business_verified': False}
            # 极小竞态：communicate 已返回但取 returncode 抛出 → 子进程确知已结束，
            # 走下面统一释放路径（不在此 return）。
        else:
            try:
                start_release = dp.finish(dispatch_store, claim, terminal='start_failed',
                                          now=now, wrapper_pid=os.getpid())
            except Exception as fexc:  # noqa: BLE001
                start_release = {'released': False, 'error': type(fexc).__name__}
            return {'dispatch_failed': False, 'sent': False, 'dispatched': False,
                    'launch_unknown': False, 'started': False, 'token': claim,
                    'capacity_release': start_release,
                    'error_class': type(exc).__name__, 'business_verified': False}
        if child_done:
            exit_code = getattr(proc, 'returncode', None) if exit_code is None else exit_code

    # communicate 返回即拿到真实退出码：解析/绑定/限额登记全部进 try，终态释放无条件执行，
    # 绝不因下游异常泄漏确知已退出的 child；活/未知保留（上面分支处理），不错误释放。
    ec_exit = exit_code if exit_code is not None else -1
    summary = {}
    facts = {}
    ev = {}
    binding = {}
    report_text = ''
    limit_recorded = False
    limit_error = None
    try:
        summary = summarize_stream((stdout_text or '').splitlines())
        facts = classify_result(summary, ec_exit, stderr_text or '',
                                is_engineering_task=is_engineering_task)
        report_text = ((summary.get('result_event') or {}).get('result')
                       or summary.get('assistant_text') or '')
        ev = write_evidence(Path(out_dir), events=summary.get('events', []), facts=facts,
                            report_text=report_text, binding={}, secret=secret,
                            task_sha=pc.sha256_hex(sent_payload.encode('utf-8')),
                            request={'argv': argv, 'cwd': str(workspace),
                                     'runtime': RUNTIME, 'model': model},
                            process={'pid': getattr(proc, 'pid', None),
                                     'exit_code': exit_code, 'wrapper_pid': os.getpid()},
                            sent_task=task_text, sent_contract=contract_text,
                            raw_stdout=stdout_text, raw_stderr=stderr_text)
        body = qd.analyze_report(Path(ev['report_md']).read_text(encoding='utf-8'),
                                 stage, str(workspace))
        readback_match = (_sha(Path(ev['report_md']).read_bytes())
                          == _sha(_redact(report_text, secret).encode('utf-8')))
        binding = qd.finalize_binding(
            body['body_ok'], body.get('reasons'), protocol_success=facts['protocol_success'],
            session_id=summary.get('session_id'), requested_session_id=None,
            readback_match=bool(readback_match))
        Path(ev['result_json']).write_text(_redact(json.dumps(
            {'facts': facts, 'binding': binding,
             'task_sha256': pc.sha256_hex(sent_payload.encode('utf-8'))},
            ensure_ascii=False, indent=2), secret), encoding='utf-8')
        # result 文件被最终重写 → 刷新真实哈希并重写 summary（最终文件回读为准）。
        ev['result_sha256'] = _sha(Path(ev['result_json']).read_bytes())
        Path(ev['summary_json']).write_text(_redact(json.dumps(ev, ensure_ascii=False,
                                                              indent=2), secret),
                                            encoding='utf-8')
        ev['evidence_sha256'] = _sha(Path(ev['summary_json']).read_bytes())
        # 明确限额：用真实载体/最终文件的正确 SHA record_main_force_limit，成功后才释放。
        if facts.get('quota_429'):
            evid_path = ev.get('result_json') or ev.get('events_jsonl')
            evid_sha = ev.get('result_sha256') or ev.get('events_sha256')
            if evid_path and evid_sha:
                try:
                    rec = dp.record_main_force_limit(dispatch_store, POOL_KEY,
                                                     task_id=task_id, attempt_token=claim,
                                                     evidence_path=evid_path,
                                                     evidence_sha256=evid_sha, now=now)
                    limit_recorded = bool(rec.get('recorded'))
                except Exception as rec_exc:  # noqa: BLE001
                    limit_error = type(rec_exc).__name__
    except Exception as exc:  # noqa: BLE001 - 解析/落盘失败不外泄；终态仍按真实退出释放
        facts = dict(facts, downstream_error=type(exc).__name__)

    success = bool(facts.get('execution_success') and binding.get('bound'))
    try:
        release = dp.finish(dispatch_store, claim, terminal='finished', success=success,
                            now=now, prober=child_prober)
    except Exception as fexc:  # noqa: BLE001
        release = {'released': False, 'error': type(fexc).__name__}
    # passed 需要：执行证据成功 + 报告绑定 + 适用证据（最终 result 文件真实哈希）；
    # 生产默认（bind_child_pid=True）还要求子进程绑定**明确成功**；终态释放必须**明确
    # released=true**；下游异常 / 真实限额登记失败一律不得 passed=0。
    bind_ok = child_verified if bind_child_pid else True
    release_ok = bool(isinstance(release, dict) and release.get('released') is True)
    limit_ok = (bool(limit_recorded) if facts.get('quota_429') else True)
    passed = bool(success and ev.get('result_sha256') and bind_ok
                  and release_ok and not facts.get('downstream_error') and limit_ok)
    return {'sent': True, 'dispatched': True, 'started': started,
            'child_verified': child_verified, 'token': claim, 'exit_code': exit_code,
            'facts': facts, 'binding': binding, 'evidence': ev, 'launched': True,
            'limit_recorded': limit_recorded, 'limit_error': limit_error,
            'capacity_release': release, 'passed': passed, 'business_verified': False,
            'report_text_sha256': _sha(report_text.encode('utf-8'))}


def build_parser():
    ap = argparse.ArgumentParser(prog='claude_direct')
    ap.add_argument('--workspace', required=True)
    ap.add_argument('--prompt-file', required=True)
    ap.add_argument('--output-dir', required=True)
    ap.add_argument('--stage', required=True)
    ap.add_argument('--task-id', required=True)
    ap.add_argument('--config', default=None,
                    help='entry config path (default: scripts/claude-entry.local.json)')
    ap.add_argument('--model', default=MODEL)
    ap.add_argument('--dispatch-claim', default=None)
    ap.add_argument('--dispatch-store', default=None)
    ap.add_argument('--dispatch-plan', default=None,
                    help='execution_control dispatch plan JSON (precise preflight before Popen)')
    ap.add_argument('--active-tasks', default=None,
                    help='JSON file with the active-tasks snapshot for the plan preflight')
    ap.add_argument('--not-engineering-task', action='store_true')
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # CLI --model 必须精确等于钉死模型：其它值**明确拒绝**（0 Popen），绝不因 cfg 已有
    # deepseek-flash 而被静默掩盖——不只检 cfg，也检 CLI 参数本身。
    if str(args.model) != MODEL:
        print(json.dumps({'dispatch_failed': True, 'sent': False, 'dispatched': False,
                          'reason': 'cli_model_rejected',
                          'reasons': [f'--model must be {MODEL!r} on this entry, '
                                      f'got {args.model!r}; no model substitution']},
                         ensure_ascii=False))
        return 2
    cfg = load_entry_config(args.config or str(DEFAULT_CONFIG))
    cfg.setdefault('model', args.model)
    plan = None
    active_tasks = None
    if args.dispatch_plan:
        plan = json.loads(Path(args.dispatch_plan).read_text(encoding='utf-8'))
        if args.active_tasks:
            active_tasks = json.loads(Path(args.active_tasks).read_text(encoding='utf-8'))
    prompt_bytes = Path(args.prompt_file).read_bytes()
    out = run(cfg, workspace=args.workspace, prompt_file=Path(args.prompt_file),
              out_dir=Path(args.output_dir), stage=args.stage, task_id=args.task_id,
              prompt_sha256=_sha(prompt_bytes), claim_token=args.dispatch_claim,
              is_engineering_task=not args.not_engineering_task,
              dispatch_store=args.dispatch_store, plan=plan, active_tasks=active_tasks)
    print(json.dumps(out, ensure_ascii=False))
    if not out.get('dispatched'):
        return 2  # not dispatched (precheck/credential/gate)
    return 0 if out.get('passed') else 3  # 3: dispatched but failed (protocol/binding/exec)


if __name__ == '__main__':
    raise SystemExit(main())
