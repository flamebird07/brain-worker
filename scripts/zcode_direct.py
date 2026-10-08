"""ZCode 官方运行时直连派工：brain-worker 传输层薄壳（Python 标准库）。

调用同目录 `zcode_sdk_runner.mjs`，由该脚本通过 ZCode 官方 bootstrap 公开 API
（startProcessProviderRegistryRuntime / createZCodeApp / submitPrompt）在同一进程内
完成模型选择预检与提交。参数一律走 JSON 请求文件，不做 shell 拼接。

边界（与 references/zcode-direct.md 一致）：
- 配置缺失、路径不存在、阶段为空、mode/reasoning/tools 非法 → 派工前拒绝，不建证据目录；
- 目标 provider/model/reasoning 与运行时实测不一致 → 由 runner 拒绝提交，禁止回落；
- 协议终态、正文绑定、业务验收三者分离：退出码只反映协议终态，绑定看
  report-state.json，`business_verified` 恒为 false，由主脑独立判定；
- 报告原样字节落盘并做哈希回读；失败证据一律保留，不删前言、不改写。
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

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / 'zcode-entry.json'
DEFAULT_RUNNER = SCRIPT_DIR / 'zcode_sdk_runner.mjs'
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
# 复用已验收的正文格式核对与最终绑定，不改写旧脚本。
from qoder_direct import analyze_report, finalize_binding  # noqa: E402
import execution_control as ec  # noqa: E402 任务级控制面（同目录，标准库）
import prompt_contract as pc  # noqa: E402 三入口共享九节契约（调用前用于构造发送载荷）
import zcode_execution_evidence as zee  # noqa: E402 执行证据门禁（契约×同turn工具事件×磁盘回读）
import quota_control as qc  # noqa: E402 持久额度冷却与路由门禁（sqlite3，默认共享 store）
import continuation_contract as cc  # noqa: E402 中断接续：baseline 冻结/终态交接（同目录）
import dispatch_pool as dp  # noqa: E402 跨会话并发容量池（同目录，标准库 sqlite3）
DEFAULT_PROVIDER = 'account:bigmodel-individual-coding-plan'
DEFAULT_MODEL = 'GLM-5.3'
REASONING_LEVELS = ('low', 'high', 'max')
MODES = ('plan', 'edit')
CONFIG_FILE_KEYS = ('node', 'bootstrap', 'tsx_loader',
                    'builtin_provider_config', 'personal_provider_config')
# 只有已核对的必需工具可以显式开放；Task/子 agent、联网（WebFetch/WebSearch）、
# node_repl（js）、Browser/CUA、workflow、cron、off-peak、Skill、Todo 等一律不暴露。
PERMITTED_TOOLS = ('Read', 'Glob', 'Grep', 'Write', 'Edit', 'Bash')
# plan 模式只读：写文件与执行命令不得在 plan 下开放。
PLAN_FORBIDDEN_TOOLS = ('Write', 'Edit', 'Bash')
# 官方内置工具全目录（apps/zcode-cli/packages/core/src/tool/handlers/index.ts 的
# builtInTools 注册名单，node_repl 的注册名是 js；web_search 是 WebSearch 的
# provider-native 契约名，一并排除）。runner 还会与运行时活注册表取并集。
FULL_TOOL_CATALOG = (
    'Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep', 'WebFetch', 'WebSearch',
    'web_search',
    'TodoRead', 'TodoWrite', 'CronCreate', 'CronList', 'CronUpdate', 'CronDelete',
    'OffPeakCreate', 'OffPeakList', 'EnterPlanMode', 'ExitPlanMode',
    'AskUserQuestion', 'SendMessage', 'RespondToCoordinator', 'submit_result',
    'escalate', 'TaskOutput', 'TaskStop', 'ReadSessionContext', 'Agent', 'Task',
    'Skill', 'js', 'CreateWorkflow', 'AmendWorkflow', 'SaveWorkflow',
    'ListSavedWorkflows', 'ListModels', 'EvalWorkflowSnippet', 'ListWorkflowRuns',
    'GetWorkflowRun', 'ResumeWorkflowRun', 'ResolveWorkflowQuestion',
)


def _derive_adapters_entry(bootstrap_path):
    """从已核验的 bootstrap 绝对路径定位同一公开 SDK 树的 adapters/src/index.ts。

    典型：.../packages/bootstrap/dist/index.js → .../packages/adapters/src/index.ts。
    只认这一确定布局；找不到或不存在即返回 None（runner fail-closed），绝不猜其他运行时或
    修改官方 CLI/入口配置（需求 I）。"""
    parts = Path(bootstrap_path).parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lower() == 'bootstrap':
            candidate = Path(*parts[:i]) / 'adapters' / 'src' / 'index.ts'
            if candidate.is_file():
                return str(candidate)
            return None
    return None


def _derive_embedded_search_backend_entry(bootstrap_path):
    """从已核验的 bootstrap 绝对路径定位同一公开 SDK 树的 embedded-search backend 解析器。

    典型：.../packages/bootstrap/dist/index.js → .../packages/bootstrap/src/app/embedded-search-backend.ts。
    该模块导出 resolveDefaultEmbeddedSearchBackend，只读 env 中指定 binary 键、不回显全量环境。
    只认这一确定布局；找不到即返回 None（runner 降级为无 prelude 绑定，不 fail-closed 整个审批），
    绝不修改官方 CLI/入口配置。"""
    parts = Path(bootstrap_path).parts
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lower() == 'bootstrap':
            candidate = (Path(*parts[:i]) / 'bootstrap' / 'src' / 'app'
                         / 'embedded-search-backend.ts')
            if candidate.is_file():
                return str(candidate)
            return None
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_zcode_config(path=None) -> dict:
    """本机入口配置：只存官方运行时文件路径与两个官方配置路径，不存凭据值。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.is_absolute():
        raise ValueError(f'entry config path must be absolute: {cfg_path}')
    if not cfg_path.is_file():
        raise FileNotFoundError(f'entry config file not found: {cfg_path}')
    cfg_path = cfg_path.resolve()
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    if not isinstance(cfg, dict):
        raise ValueError(f'entry config must be a JSON object: {cfg_path}')
    for key in CONFIG_FILE_KEYS:
        if key not in cfg:
            raise KeyError(f'entry config missing key {key!r}: {cfg_path}')
        value = Path(cfg[key])
        if not value.is_absolute() or not value.is_file():
            raise FileNotFoundError(f'entry config {key!r} must be an existing '
                                    f'absolute file: {value}')
    environment = cfg.get('environment', {})
    if not isinstance(environment, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in environment.items()):
        raise ValueError('entry config environment must be a string->string object')
    node_args = cfg.get('node_args')
    if node_args is not None and (not isinstance(node_args, list)
                                  or any(not isinstance(a, str) or not a for a in node_args)):
        raise ValueError('entry config node_args must be a list of non-empty strings')
    runner = cfg.get('runner')
    if runner is not None:
        runner_path = Path(runner)
        if not runner_path.is_absolute() or not runner_path.is_file():
            raise FileNotFoundError(f'entry config runner must be an existing absolute file: {runner}')
    if 'version' in cfg and not isinstance(cfg['version'], str):
        raise ValueError('entry config version must be a string when present')
    cfg['_entry_config_path'] = str(cfg_path)
    return cfg


def parse_tools(raw) -> list:
    """--tools 原样逐项校验：空串=零授权；未知/空白/大小写不符一律拒绝。"""
    if not isinstance(raw, str):
        raise ValueError(f'tools must be a string, got {type(raw).__name__}')
    if raw.strip() == '':
        return []
    out = []
    for piece in raw.split(','):
        if piece == '' or piece.isspace():
            raise ValueError(f'tools contains empty/whitespace item: {raw!r}')
        if piece != piece.strip():
            raise ValueError(f'tools item has surrounding whitespace: {raw!r}')
        if piece not in PERMITTED_TOOLS:
            raise ValueError(f'unknown or non-permitted tool {piece!r}; permitted: '
                             f'{", ".join(PERMITTED_TOOLS)}')
        if piece in out:
            raise ValueError(f'duplicate tool item: {raw!r}')
        out.append(piece)
    return out


def validate_selection(*, stage: str, provider: str, model: str, reasoning: str,
                       mode: str, tools: list) -> list:
    """派工前的机械校验；返回原因列表，空列表表示通过。"""
    reasons = []
    if not stage or not stage.strip():
        reasons.append('--stage is required and must be non-empty')
    if not provider or not provider.strip():
        reasons.append('--provider must be non-empty')
    if not model or not model.strip():
        reasons.append('--model must be non-empty')
    if reasoning not in REASONING_LEVELS:
        reasons.append(f'--reasoning must be one of {", ".join(REASONING_LEVELS)}')
    if mode not in MODES:
        reasons.append(f'--mode must be one of {", ".join(MODES)} (yolo is never offered)')
    if mode == 'plan':
        clash = [t for t in tools if t in PLAN_FORBIDDEN_TOOLS]
        if clash:
            reasons.append(f'--mode plan is read-only; these tools would grant write/execute: {clash}')
    return reasons


def build_tool_disallowlist(tools: list) -> list:
    """整工具开关：默认禁用全目录，仅显式名单放行。"""
    allowed = set(tools)
    return sorted(name for name in FULL_TOOL_CATALOG if name not in allowed)


# ---- 受控命令契约（--command-contract）：派工前严格校验并冻结注册表 ----
# 结构口径与 scripts/zcode_permission_broker.mjs 完全一致，两语言共用同一 canonical SHA。
COMMAND_CONTRACT_TOP_KEYS = {'privacy_free', 'commands'}
COMMAND_CONTRACT_ITEM_KEYS = {'command', 'cwd'}
COMMAND_CONTRACT_ITEM_OPTIONAL = {'input_sha256'}
# 复合/变体特征：登记阶段即拒绝（与 broker COMPOSITE_MARKERS 对齐），逐字相等在执行阶段
# 同样不会匹配，故这两道都拦住 echo/分号/cd/管道/重定向/wrapper 追加。
COMMAND_COMPOSITE_MARKERS = (';', '&&', '||', '|', '\n', '\r', '>>', '>', '<')
COMMAND_CWD_PREFIXES = ('cd ', 'cd\t')


def _canonical_json(obj) -> str:
    """与 broker.canonicalize 对齐：递归排序键、无空格、ensure_ascii=False。"""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _norm_within(workspace, path):
    norm = os.path.normcase(os.path.realpath(os.path.join(str(workspace), str(path))))
    root = os.path.normcase(os.path.realpath(str(workspace)))
    return norm if (norm == root or norm.startswith(root + os.sep)) else None


def validate_command_contract(contract, workspace):
    """返回 (规范化契约, reasons)。结构/类型/越界/变体/SHA 不符全部拒绝。"""
    reasons = []
    if not isinstance(contract, dict):
        return None, ['command contract must be a JSON object']
    if set(contract) != COMMAND_CONTRACT_TOP_KEYS:
        return None, [f'command contract top-level keys must be exactly '
                      f'{sorted(COMMAND_CONTRACT_TOP_KEYS)}, got {sorted(contract)}']
    if not isinstance(contract.get('privacy_free'), bool):
        reasons.append('privacy_free must be a boolean')
    commands = contract.get('commands')
    if not isinstance(commands, list) or not commands:
        reasons.append('commands must be a non-empty list')
        return None, reasons
    normalized = []
    seen = set()
    for index, item in enumerate(commands):
        if not isinstance(item, dict):
            reasons.append(f'commands[{index}] must be an object')
            continue
        keys = set(item)
        if not keys <= (COMMAND_CONTRACT_ITEM_KEYS | COMMAND_CONTRACT_ITEM_OPTIONAL):
            reasons.append(f'commands[{index}] has unknown keys: {sorted(keys - (COMMAND_CONTRACT_ITEM_KEYS | COMMAND_CONTRACT_ITEM_OPTIONAL))}')
            continue
        if keys & COMMAND_CONTRACT_ITEM_KEYS != COMMAND_CONTRACT_ITEM_KEYS:
            reasons.append(f'commands[{index}] must carry both command and cwd')
            continue
        command = item['command']
        if not isinstance(command, str) or not command or command != command.strip():
            reasons.append(f'commands[{index}].command must be a non-empty untrimmed string')
            continue
        if any(marker in command for marker in COMMAND_COMPOSITE_MARKERS):
            reasons.append(f'commands[{index}].command is a composite/variant command '
                           '(contains a shell operator or newline); only single verbatim '
                           'commands may be registered')
            continue
        if command.startswith(COMMAND_CWD_PREFIXES):
            reasons.append(f'commands[{index}].command must not begin with cd (cwd is '
                           'declared separately and compared verbatim)')
            continue
        cwd_norm = _norm_within(workspace, item['cwd'])
        if cwd_norm is None:
            reasons.append(f'commands[{index}].cwd resolves outside the workspace: {item["cwd"]!r}')
            continue
        entry = {'command': command, 'cwd': cwd_norm}
        if 'input_sha256' in keys:
            declared = item['input_sha256']
            if not isinstance(declared, dict) or not declared:
                reasons.append(f'commands[{index}].input_sha256 must be a non-empty object '
                               'of {path: sha256} (declared code/test input scope)')
                continue
            verified = {}
            for rel, digest in declared.items():
                if not isinstance(digest, str) or len(digest) != 64:
                    reasons.append(f'commands[{index}].input_sha256[{rel!r}] must be a 64-char hex sha')
                    continue
                # 1：登记期保守拒绝别名——声明路径若穿越 symlink/junction（abspath 不解析链接、
                # realpath 解析），路径绑定不可信，直接拒绝；不扩成 OS 沙箱。锚定在已解析的工作区
                # 根上，避免临时根里的链接误判成声明路径自身的别名。
                ws_real_for_alias = os.path.realpath(str(workspace))
                abs_lexical = os.path.normpath(os.path.join(ws_real_for_alias, str(rel)))
                realp = os.path.realpath(abs_lexical)
                if os.path.normcase(abs_lexical) != os.path.normcase(realp):
                    reasons.append(f'commands[{index}].input_sha256[{rel!r}] path traverses a '
                                   'symlink/junction/alias; refuse to bind an alias-based input')
                    continue
                target = _norm_within(workspace, rel)
                if target is None:
                    reasons.append(f'commands[{index}].input_sha256 path escapes workspace: {rel!r}')
                    continue
                if not os.path.isfile(target):
                    reasons.append(f'commands[{index}].input_sha256 declared input missing: {rel!r}')
                    continue
                actual = hashlib.sha256(Path(target).read_bytes()).hexdigest()
                if actual != digest:
                    reasons.append(f'commands[{index}].input_sha256[{rel!r}] registration SHA '
                                   'mismatch (input changed since registration)')
                    continue
                # 归一化保留三元组：原声明路径（键）+ 登记物理目标（供 JS 执行期重解析比对）+ SHA。
                verified[str(rel)] = {'target': target, 'sha': digest.lower()}
            if any(f'commands[{index}].input_sha256' in r for r in reasons):
                continue
            entry['input_sha256'] = verified
        signature = (command, cwd_norm)
        if signature in seen:
            reasons.append(f'commands[{index}] duplicates an earlier command+cwd registration')
            continue
        seen.add(signature)
        normalized.append(entry)
    if reasons:
        return None, reasons
    normalized.sort(key=lambda e: (e['command'], e['cwd']))
    return {'privacy_free': bool(contract['privacy_free']), 'commands': normalized}, []


def build_node_argv(cfg: dict, runner: Path, request_path: Path) -> list:
    """无 shell 的参数数组。真实 node 走 tsx loader；测试可用 node_args/runner 换载体。"""
    node_args = cfg.get('node_args')
    if node_args is None:
        node_args = ['--import', Path(cfg['tsx_loader']).as_uri()]
    return [cfg['node'], *node_args, str(runner), '--request', str(request_path)]


def build_request(cfg: dict, *, workspace: str, prompt: str, out_dir: Path,
                  stage: str, provider: str, model: str, reasoning: str,
                  mode: str, tools: list, resume_session_id,
                  preflight_only: bool, runner: Path,
                  prompt_sha256: str, prompt_payload: dict, chat_id=None) -> dict:
    selection = {'providerId': provider, 'modelId': model,
                 'options': {'reasoningLevel': reasoning}}
    return {
        'carrier': 'zcode-sdk',
        'started_at_utc': _now(),
        'stage': stage,
        'chat_id': chat_id,
        'workspace': workspace,
        'output_dir': str(out_dir),
        'prompt': prompt,
        # prompt_sha256 是原始提示词文件字节哈希（dispatch-plan 统一口径）；完整发送载荷
        # 与契约的哈希在 prompt_payload 里单独留证，绝不用本 JSON 文件哈希冒充 prompt 哈希。
        'prompt_sha256': prompt_sha256,
        'prompt_payload': prompt_payload,
        'selection': selection,
        'mode': mode,
        'allowed_tools': list(tools),
        'tool_disallowlist_base': build_tool_disallowlist(tools),
        'resume_session_id': resume_session_id,
        'preflight_only': bool(preflight_only),
        'entry_config': cfg['_entry_config_path'],
        'environment_keys': sorted((cfg.get('environment') or {}).keys()),
        'bootstrap': cfg['bootstrap'],
        'builtin_provider_config': cfg['builtin_provider_config'],
        'personal_provider_config': cfg['personal_provider_config'],
        'version': cfg.get('version'),
        'runner': str(runner),
        'argv': build_node_argv(cfg, runner, out_dir / 'request.json'),
    }


def _read_envelope(path: Path):
    raw = path.read_bytes()
    if not raw.strip():
        return None, 'runner stdout is empty'
    try:
        return json.loads(raw.decode('utf-8')), None
    except (ValueError, UnicodeDecodeError) as exc:
        return None, f'runner stdout parse error: {exc}'


def _read_events(path: Path) -> list:
    """读取 events.jsonl 原始工具事件（逐行 JSON，坏行跳过不整体失败）；用于从工具
    错误结构提取“缺少 Bash 权限客户端”，即使 summary permission_denials=0。"""
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


# 包装层额度异常的白名单字段：只从已知失败结构提取这些键，其余（headers、原始
# stderr 全文、正文里偶然出现的数字）一律忽略，绝不回显、绝不落进证据。
_WRAPPER_QUOTA_FIELDS = ('provider', 'response_status', 'provider_code', 'message',
                         'reset_timezone', 'source_stderr_sha256', 'original_source')


# 实际额度耗尽事实的白名单 provider code（结构化，非正文扫描）：仅这些数值码算 quota。
_QUOTA_PROVIDER_CODES = {1308}


def _numeric_provider_code(value):
    """从白名单 code 值取纯数字 provider_code（保留数值，不只 str）。非数字→None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _wrapper_quota_error(envelope):
    """ZCode 包装层异常的额度信号提取（缺陷 B / 需求 1）。

    只在**已知失败结构**（envelope 的 quota_error/wrapper_error 字段，或 errors 列表里
    的 dict 项）中按白名单提取 provider / response_status(==429) / provider_code /
    message（以及引用性的 reset_timezone、source_stderr_sha256、original_source），
    归一化成 errors_info 结构项供冷却分类复用。绝不读取或输出 HTTP headers、绝不读
    原始 stderr 字节、绝不扫描正常报告正文里的 429/数字。无 429 结构即返回 None。
    reset 时区只透传原引用的判定（如 unverified），本函数不猜 UTC 偏移。

    S4 缺陷 3 修正：不再无条件把任何 429 归成 category=quota。只有实际额度事实
    （纯数字 provider_code ∈ {1308}、显式 category=quota，或来自 quota_error 字段）
    才标 quota；无耗尽/无窗口的普通 429 标 rate_limit，交给分类走 temporary_backoff
    （保留结构化 Retry-After 下限），绝不误落 24h 无窗口冷却。"""
    if not isinstance(envelope, dict):
        return None
    candidates = []
    for field in ('quota_error', 'wrapper_error'):
        value = envelope.get(field)
        if isinstance(value, dict):
            candidates.append((field, value))
    for item in envelope.get('errors') or []:
        if isinstance(item, dict):
            candidates.append(('errors', item))
    for field, item in candidates:
        if item.get('response_status') != 429:
            continue
        raw_code = item.get('provider_code')
        provider_num = _numeric_provider_code(raw_code)
        is_quota = (provider_num in _QUOTA_PROVIDER_CODES
                    or item.get('category') == 'quota' or field == 'quota_error')
        entry = {'status': 429, 'category': 'quota' if is_quota else 'rate_limit'}
        if isinstance(raw_code, str) and raw_code.strip():
            entry['code'] = raw_code.strip()
        if provider_num is not None:
            entry['provider_code'] = provider_num  # 保留数值（1308），非只 str
        message = item.get('message')
        if isinstance(message, str):
            entry['details'] = message
            entry['message'] = message
        for key in _WRAPPER_QUOTA_FIELDS:
            if key in ('response_status', 'provider_code', 'message'):
                continue
            value = item.get(key)
            if value is not None:
                entry[key] = value
        return entry
    return None


# 已知 SDK ProviderBusinessError 错误帧的有限识别边界（只在帧内取白名单字段）：
# 帧首关键字 + 结构化 responseStatus/code 行；不看任意正文、不看 headers。
_PB_ERROR_KEYWORD = 'ProviderBusinessError'
_PB_STATUS_RE = re.compile(r'responseStatus:\s*(\d{3})')
_PB_CODE_RE = re.compile(r"code:\s*'([^']+)'")
_PB_MAX_SCAN_BYTES = 64 * 1024


def _provider_business_error_from_stderr(stderr_bytes, provider=None):
    """从本次子进程自己写出的 stderr 里，有限、白名单地识别**已知** ProviderBusinessError
    错误帧（S4 缺陷 11 / S5 缺陷 2）。只有同时出现帧关键字与 responseStatus 字段且值为 429
    才成立。真实 SDK 帧按原始行序会先给出**外层 wrapper code**（如 PROVIDER_BUSINESS_ERROR）、
    再给出**内层真实数字 provider_code**（如 1308）；因此这里在**不调整行序**的前提下收集
    全部 `code:` 值：首个作为 wrapper_code，第一个纯数字作为 provider_code（数值保留，不只 str）。
    reset 文本无时区 → reset_timezone=unverified（不猜 UTC）。S4 缺陷 3：只有内层数字码命中实际
    额度事实（1308）才标 category=quota，普通 429 限流标 rate_limit 交分类走 temporary_backoff。
    绝不回显 headers/cookies/原始字节，扫描范围有界（前 64KB），正文里偶然出现的 429 不触发
    （需 responseStatus 结构字段）。返回归一化 errors_info 结构项或 None。"""
    if not stderr_bytes:
        return None
    text = stderr_bytes[:_PB_MAX_SCAN_BYTES].decode('utf-8', 'replace')
    idx = text.find(_PB_ERROR_KEYWORD)
    if idx < 0:
        return None
    frame = text[idx:]
    m_status = _PB_STATUS_RE.search(frame)
    if not m_status or m_status.group(1) != '429':
        return None
    codes = _PB_CODE_RE.findall(frame)  # 原始行序，全部 code 值
    wrapper_code = codes[0] if codes else None
    provider_num = None
    provider_str = None
    for c in codes:
        if c.isdigit():
            provider_num = int(c)
            provider_str = c
            break
    is_quota = provider_num in _QUOTA_PROVIDER_CODES
    entry = {'status': 429, 'provider': provider,
             'category': 'quota' if is_quota else 'rate_limit'}
    if wrapper_code is not None:
        entry['wrapper_code'] = wrapper_code
    if provider_str is not None:
        entry['code'] = provider_str           # 内层真实数字码（1308），非外层
    if provider_num is not None:
        entry['provider_code'] = provider_num   # 数值保留，非只 str
    first_line = frame.splitlines()[0] if frame.splitlines() else _PB_ERROR_KEYWORD
    msg = first_line[len(_PB_ERROR_KEYWORD):].lstrip(' :')
    if msg:
        entry['details'] = msg
        entry['message'] = msg
    # reset 时间只有带明确时区才可核验；SDK 帧 message 无时区 → 保守 unverified，不猜。
    entry['reset_timezone'] = 'unverified: stderr error frame carried no timezone'
    entry['source_stderr_sha256'] = hashlib.sha256(stderr_bytes).hexdigest()
    entry['original_source'] = 'this-run child stderr.log (ProviderBusinessError frame)'
    return entry


def _zcode_failure_facts(summary: dict, envelope, events: list,
                         stderr_bytes=None, provider=None) -> dict:
    errors = (envelope or {}).get('errors') or []
    errors_info = (envelope or {}).get('errors_info')
    wrapper_quota = _wrapper_quota_error(envelope)
    # 真实 SDK 失败链（S4 缺陷 11）：未改的 zcode_sdk_runner 只在 envelope.errors 里放
    # **字符串**错误，真正的 status/code 元数据在该次子进程**自己写出的 stderr**里，表现
    # 为已知 ProviderBusinessError 错误帧。这里对本次 out/'stderr.log' 字节做安全、有限、
    # 白名单提取（只认 responseStatus/code/首行 message），并绑定 request provider + 原始
    # stderr SHA/ref。**绝不**扫描正常报告正文里的 429、绝不读 HTTP headers/cookies。
    if wrapper_quota is None and stderr_bytes:
        wrapper_quota = _provider_business_error_from_stderr(stderr_bytes, provider)
    # 只在结构层面并入白名单额度项；原始 errors_info 不改写，正文不被扫描。
    merged_errors_info = list(errors_info) if isinstance(errors_info, list) else []
    if wrapper_quota is not None:
        merged_errors_info = merged_errors_info + [wrapper_quota]
    client_missing_hits = ec.permission_client_missing_from_events(events)
    rule_denied_hits = ec.permission_rule_denied_from_events(events)
    parse_failure = bool(summary.get('parse_error')) or bool(
        (envelope or {}).get('serialization_failed'))
    quota_429 = (ec.explicit_429(errors, merged_errors_info) or bool(wrapper_quota))
    preflight_only = bool(summary.get('preflight_only'))
    protocol_ok = bool(summary.get('protocol_success'))
    other_cause = (parse_failure or client_missing_hits or rule_denied_hits
                   or quota_429 or preflight_only or protocol_ok)
    model_execution = bool(not protocol_ok and not other_cause
                           and (summary.get('submitted') or errors
                                or summary.get('turn_status')))
    return {'quota_429': quota_429,
            'permission_client_missing': bool(client_missing_hits),
            'permission_rule_denied': bool(rule_denied_hits),
            'protocol_parse_failure': parse_failure,
            'model_execution_failure': bool(model_execution),
            'reset_hint': ec.extract_reset_hint(errors, merged_errors_info),
            'evidence': {'runner_errors': errors,
                         'wrapper_quota_error': wrapper_quota,
                         'turn_status': summary.get('turn_status'),
                         'terminal_success': summary.get('terminal_success'),
                         'permission_denials_in_summary': (envelope or {}).get(
                             'permission_denials'),
                         'client_missing_events': client_missing_hits[:3],
                         'rule_denied_events': rule_denied_hits[:3]}}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--workspace', required=True)
    ap.add_argument('--prompt-file', required=True)
    ap.add_argument('--output-dir', required=True)
    ap.add_argument('--stage', required=True,
                    help='Non-empty stage id the report must carry as an exact token.')
    ap.add_argument('--provider', default=DEFAULT_PROVIDER,
                    help=f'Standalone account provider id; defaults to {DEFAULT_PROVIDER}.')
    ap.add_argument('--model', default=DEFAULT_MODEL,
                    help=f'Requested model id; defaults to {DEFAULT_MODEL}. Never falls back.')
    ap.add_argument('--reasoning', default='low', choices=REASONING_LEVELS,
                    help='reasoningLevel carried in the model selection.')
    ap.add_argument('--mode', default='plan', choices=MODES,
                    help='plan is read-only; edit requires project-modification authorization. '
                         'yolo is never offered.')
    ap.add_argument('--tools', default='',
                    help=f'Comma separated whole-tool grants, drawn from {", ".join(PERMITTED_TOOLS)}. '
                         'Empty disables every built-in tool. This is a tool on/off switch only: '
                         'it is not a path or command permission rule.')
    ap.add_argument('--resume-session-id', dest='session_id', default=None,
                    help='Exact official ZCode session id to resume; mismatch refuses submission.')
    ap.add_argument('--preflight-only', dest='preflight_only', action='store_true',
                    help='Check registry selectability only; creates no app/session and submits no prompt.')
    ap.add_argument('--execution-contract', dest='execution_contract', default=None,
                    help='Optional task execution contract JSON (task_type, required_reads, '
                         'expected_artifacts, required_modified_files, allow_no_changes, '
                         'required_test_command, test_evidence). Paths must stay inside '
                         '--workspace (realpath-exact); missing/out-of-bound/contradictory '
                         'contracts are refused before dispatch and a dispatch-time baseline '
                         'hash snapshot is bound into request.json. Without a contract, '
                         'execution_evidence_ok stays null (explicitly unverified); with a '
                         'contract, evidence not passing yields exit 3 even if the protocol '
                         'succeeded.')
    ap.add_argument('--config', default=str(DEFAULT_CONFIG),
                    help='Local ZCode entry config JSON with absolute runtime paths.')
    ap.add_argument('--command-contract', dest='command_contract', default=None,
                    help='Optional controlled command-approval JSON {privacy_free: bool, '
                         'commands:[{command, cwd, input_sha256?}]}. Only the frozen verbatim '
                         'commands with their declared in-workspace cwd may execute; the runner '
                         'injects a decorated executionPort (verbatim command+cwd gate) plus a '
                         'permissionBroker. Bash granted without this contract is refused; no '
                         'full-allow/yolo/bypass/auto-login. Unknown/missing fields, out-of-bound '
                         'cwd, composite/variant commands (;/&&/||/|/newline/redirect/cd) and '
                         'input SHA mismatch are refused before dispatch.')
    ap.add_argument('--dispatch-plan', dest='dispatch_plan', default=None,
                    help='Optional task-level dispatch plan JSON. ZCode exposes only '
                         'whole-tool toggles, so a plan that needs per-file Edit or '
                         'verbatim Bash limits is refused here (adopt an isolated '
                         'workspace with tool-visibility grants instead). Legacy no-plan '
                         'dispatch stays compatible.')
    ap.add_argument('--active-tasks', dest='active_tasks', default=None,
                    help='Optional JSON file of the active-task snapshot (list of '
                         '{task_id,state,...}). When given it overrides the plan\'s own '
                         'active_tasks; otherwise the plan snapshot is used, so a parallel '
                         'plan that omits it is refused rather than treated as "nothing '
                         'in flight".')
    ap.add_argument('--quota-store', dest='quota_store', default=None,
                    help='Explicit quota cooldown store (sqlite3). Defaults to the '
                         'stable shared store from quota_control (env '
                         'BRAIN_WORKER_QUOTA_STORE or ~/.brain-worker).')
    ap.add_argument('--quota-routes', dest='quota_routes', default=None,
                    help='Trusted local quota-routes JSON mapping real channel '
                         'identity (provider) to quota_group; unmatched channels share '
                         'the conservative unknown-shared group.')
    ap.add_argument('--task-id', dest='task_id', default=None,
                    help='Stable task identity for cross-chat capacity dedup. Defaults '
                         'to the dispatch-plan task_id when a plan is given, else --stage.')
    ap.add_argument('--dispatch-store', dest='dispatch_store', default=None,
                    help='Explicit dispatch-pool sqlite path. Defaults to the stable '
                         'shared pool (env BRAIN_WORKER_DISPATCH_STORE or '
                         '~/.brain-worker/dispatch-pool.sqlite3). Tests must pass a '
                         'temporary store. --preflight-only submits nothing and is exempt.')
    ap.add_argument('--dispatch-claim', dest='dispatch_claim', default=None,
                    help='A capacity claim token pre-reserved via dispatch_pool. When '
                         'given it is validated against task/runtime/model/workspace/'
                         'prompt and consumed before Popen; any drift is refused. When '
                         'omitted the entry performs an atomic route selection and, if '
                         'this entry is not the selected combo, returns routing_required '
                         'with sent=false instead of submitting the wrong model.')
    ap.add_argument('--chat-id', dest='chat_id', default=None,
                    help='Chat/conversation identity for capacity dedup and claim scope. '
                         'When a pre-reserved claim was reserved with an explicit chat_id '
                         'in its scope, the consuming entry MUST pass the same --chat-id '
                         'or the claim is refused as drift (zero Popen). Never fabricated: '
                         'omit it when the host has no verified chat context, in which case '
                         'the auto path records chat_id=None rather than inventing one.')
    ap.add_argument('--quota-recovery-probe', dest='quota_probe',
                    action='store_true',
                    help='Run this dispatch as a single bounded, read-only verification '
                         'probe. For ZCode this is OPTIONAL only: the auto quota '
                         'cooldown gate is disabled by user policy, so a normal '
                         'dispatch never requires a probe first (this is not a gate to '
                         'unlock a cooldown). Use a scheduler-supplied minimal prompt, '
                         'never the full long task. Read-only tools only, no resume, no '
                         'dispatch-plan, bounded prompt bytes and an explicit timeout '
                         'are enforced programmatically before dispatch.')
    ap.add_argument('--quota-probe-timeout-seconds', dest='quota_probe_timeout',
                    type=int, default=qc.PROBE_DEFAULT_TIMEOUT_SECONDS,
                    help=f'Bounded wall-clock timeout for the recovery probe child in '
                         f'seconds [{qc.PROBE_TIMEOUT_MIN_SECONDS}, '
                         f'{qc.PROBE_TIMEOUT_MAX_SECONDS}]; on timeout the child is '
                         f'killed and settled as a non-429 failure (never healthy).')
    ap.add_argument('--continuation-contract', dest='continuation_contract',
                    default=None,
                    help='Optional interruption-continuation contract JSON {files, '
                         'todos?, original_report_ref?, original_error_ref?, '
                         'prev_handoff?, test_evidence?}. Declared files are frozen '
                         '(baseline bytes + SHA) before dispatch; after the confirmed '
                         'terminal state a continuation.json handoff with terminal '
                         'copies, real diffs, verified original refs and successful '
                         'Write/Edit receipts is generated. prev_handoff drift is '
                         'refused before dispatch. Without a contract the summary '
                         'explicitly records unverified_no_contract.')
    args = ap.parse_args()

    try:
        tools = parse_tools(args.tools)
    except ValueError as exc:
        print(json.dumps({'refused_before_dispatch': True, 'reasons': [str(exc)]},
                         ensure_ascii=False))
        return 2
    reasons = validate_selection(stage=args.stage, provider=args.provider,
                                 model=args.model, reasoning=args.reasoning,
                                 mode=args.mode, tools=tools)
    if reasons:
        print(json.dumps({'refused_before_dispatch': True, 'reasons': reasons},
                         ensure_ascii=False))
        return 2

    try:
        cfg = load_zcode_config(args.config)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(json.dumps({'refused_before_dispatch': True, 'reasons': [str(exc)]},
                         ensure_ascii=False))
        return 2

    try:
        work = Path(args.workspace).resolve(strict=True)
        prompt_path = Path(args.prompt_file).resolve(strict=True)
    except (OSError, ValueError) as exc:
        print(json.dumps({'refused_before_dispatch': True,
                          'reasons': [f'workspace/prompt-file unavailable: {exc}']},
                         ensure_ascii=False))
        return 2
    if not work.is_dir():
        print(json.dumps({'refused_before_dispatch': True,
                          'reasons': ['--workspace must be an existing directory']},
                         ensure_ascii=False))
        return 2
    try:
        prompt_bytes = prompt_path.read_bytes()
        # 发送任务文本沿用既有语义（换行归一，不回写原文件）；prompt_sha256 另取原始文件
        # 字节哈希，CRLF 输入时二者可不同，均在 prompt_payload 里留证。
        prompt_text = prompt_bytes.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
    except (OSError, UnicodeDecodeError) as exc:
        print(json.dumps({'refused_before_dispatch': True,
                          'reasons': [f'prompt file unreadable as UTF-8: {exc}']},
                         ensure_ascii=False))
        return 2
    prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
    if not prompt_text.strip():
        print(json.dumps({'refused_before_dispatch': True,
                          'reasons': ['prompt file is empty']}, ensure_ascii=False))
        return 2

    execution_contract = None
    contract_baseline = None
    if args.execution_contract:
        try:
            execution_contract = zee.load_execution_contract(args.execution_contract)
        except ValueError as exc:
            print(json.dumps({'refused_before_dispatch': True, 'reasons': [str(exc)]},
                             ensure_ascii=False))
            return 2
        contract_reasons = zee.validate_execution_contract(execution_contract, work)
        if contract_reasons:
            print(json.dumps({'refused_before_dispatch': True,
                              'reasons': contract_reasons}, ensure_ascii=False))
            return 2
        # 派工前 baseline 快照随 request 留证并绑定契约哈希；执行后契约文件被改写
        # 不影响本次判定的依据。
        contract_baseline = zee.build_baseline(execution_contract, str(work),
                                               lambda p: Path(p).read_bytes()
                                               if Path(p).is_file() else None)

    # ---- 受控命令契约：派工前冻结注册表并绑定 canonical SHA；Bash 无契约一律拒绝 ----
    command_contract = None
    command_contract_sha256 = None
    if args.command_contract:
        if args.preflight_only:
            print(json.dumps({'refused_before_dispatch': True,
                              'reasons': ['--command-contract is not applicable to '
                                          '--preflight-only (no prompt is submitted and no '
                                          'command may execute)']}, ensure_ascii=False))
            return 2
        try:
            raw_contract = json.loads(Path(args.command_contract).read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            print(json.dumps({'refused_before_dispatch': True,
                              'reasons': [f'command contract unreadable: {exc}']},
                             ensure_ascii=False))
            return 2
        normalized, cc_reasons = validate_command_contract(raw_contract, work)
        if cc_reasons:
            print(json.dumps({'refused_before_dispatch': True, 'reasons': cc_reasons},
                             ensure_ascii=False))
            return 2
        command_contract = normalized
        command_contract_sha256 = hashlib.sha256(
            _canonical_json(normalized).encode('utf-8')).hexdigest()
    if 'Bash' in tools and not args.preflight_only and command_contract is None:
        print(json.dumps({'refused_before_dispatch': True,
                          'reasons': ['Bash is granted without --command-contract; the '
                                      'controlled pre-execution gate cannot be built, so '
                                      'dispatch is refused (Bash is never enabled by default '
                                      'and is not silently dropped into a broad permission)']},
                         ensure_ascii=False))
        return 2

    runner = Path(cfg['runner']) if cfg.get('runner') else DEFAULT_RUNNER
    if not runner.is_file():
        print(json.dumps({'refused_before_dispatch': True,
                          'reasons': [f'runner script missing: {runner}']},
                         ensure_ascii=False))
        return 2

    out = Path(args.output_dir).resolve()

    # ---- 任务级预检（仅 --dispatch-plan 时启用）：argv 建好后、Popen/建目录前 ----
    plan_block = None
    plan_task_id = None
    if args.dispatch_plan:
        try:
            plan = ec.load_plan(args.dispatch_plan)
            active_tasks = None
            if args.active_tasks:
                active_tasks = json.loads(Path(args.active_tasks).read_text(encoding='utf-8'))
        except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({'refused_before_dispatch': True, 'sent': False,
                              'exit_code': 2, 'reasons': [str(exc)]}, ensure_ascii=False))
            return 2
        plan_task_id = plan.get('task_id')
        actual = {'task_id': plan.get('task_id'), 'stage': args.stage,
                  'runtime': 'zcode', 'model': args.model, 'workspace': str(work),
                  'cwd': str(work),
                  'prompt_sha256': prompt_sha256,
                  'argv': build_node_argv(cfg, runner, out / 'request.json'),
                  'shell': False,
                  # Record the actual whole-tool allow/deny sets; no fine-grained grants.
                  'grants': ec.grants_from_rules(list(tools), [],
                                                build_tool_disallowlist(tools), list(tools))}
        result = ec.preflight(plan, actual, active_tasks=active_tasks)
        plan_block = {'plan_path': str(Path(args.dispatch_plan).resolve()),
                      'plan_hash': result['plan_hash'], 'ok': result['ok'],
                      'reasons': result['reasons'], 'argv_sha256': result['argv_sha256'],
                      'active_task_count': result['active_task_count'],
                      'is_atomic_lock': result['is_atomic_lock']}
        if not result['ok']:
            print(json.dumps({'refused_before_dispatch': True, 'sent': False,
                              'exit_code': 2, 'reasons': result['reasons'],
                              'plan_hash': result['plan_hash']}, ensure_ascii=False))
            return 2

    # ---- 持久额度冷却门禁（所有路径，含不传 --dispatch-plan 的兼容路径）----
    # Popen/建目录前原子检查+占位；拒绝必带 sent=false、原因与 UTC 截止，退出 2、
    # 零输出目录。quota_group 只来自受信任本机路由配置（如 user_confirmed 独立组），
    # 未知关系进保守共享组，plan 不能自报组绕过。
    quota_store = args.quota_store or str(qc.default_store_path())
    # recovery probe 程序化边界（缺陷 D）：只读工具、禁 resume、禁完整任务 plan、
    # 有界提示词、显式时限；违反一律在建目录/Popen 前退出 2、零提交、零目录。
    probe_problems = qc.validate_probe_dispatch(
        probe=args.quota_probe, tools_items=tools,
        resume_session_id=args.session_id, prompt_bytes=prompt_bytes,
        dispatch_plan=args.dispatch_plan,
        timeout_seconds=args.quota_probe_timeout if args.quota_probe else None)
    if probe_problems:
        print(json.dumps({'refused_before_dispatch': True, 'sent': False, 'exit_code': 2,
                          'reasons': ['recovery probe bounds violated: '
                                      + ' | '.join(probe_problems)]},
                         ensure_ascii=False))
        return 2
    # 中断接续契约（缺陷 I）：派发装载并做上一手 handoff 漂移检查，漂移拒绝；
    # baseline 冻结在 out.mkdir 之后、Popen 之前随 request 留证。
    continuation_spec = None
    if args.continuation_contract:
        try:
            continuation_spec = cc.load_continuation_contract(
                args.continuation_contract, str(work))
        except ValueError as exc:
            print(json.dumps({'refused_before_dispatch': True, 'sent': False,
                              'exit_code': 2,
                              'reasons': [f'continuation contract invalid: {exc}']},
                             ensure_ascii=False))
            return 2
        drift = cc.check_prev_handoff_drift(continuation_spec, str(work))
        if drift:
            print(json.dumps({'continuation_drift_refused': True, 'sent': False,
                              'exit_code': 2, 'reasons': drift}, ensure_ascii=False))
            return 2
    # ---- 跨会话并发容量门（普通派工路径；--preflight-only 不提交、不建会话，豁免）----
    # Popen/建目录前原子消费一个容量 claim：给了 --dispatch-claim 就精确校验并 mark_running
    # （漂移即拒），否则做原子路由选择；当前入口非被选中组合 → routing_required、sent=false、
    # 退出 2、零输出目录，绝不先提交错模型、绝不浪费/重复占用分配名额。容量并发 ≠ 额度冷却：
    # 这里只管“同时几个真实执行器在跑”，下面的额度门禁语义完全不变。放在额度门之前，容量门
    # 拒绝时不产生任何额度占位副作用。
    dispatch_store = args.dispatch_store or str(dp.default_store_path())
    task_id = args.task_id or plan_task_id or args.stage
    # 真实调用者身份（Z2）：Popen 前用本进程真实 PID + 创建时刻锚定 owner，供 bind_child/
    # finish 校验——别人不能把子进程挂到不属于自己的 attempt，也不能凭一个终态参数释放本
    # owner 尚未启动/未知的名额。全程复用同一身份，绝不伪造。
    wrapper_pid = os.getpid()
    wrapper_created = dp.process_identity(wrapper_pid).get('created')
    claim_token = None
    pool_gate = None
    quota_verified_claim = None
    if not args.preflight_only:
        pool_gate = dp.consume_for_entry(
            dispatch_store, task_id=task_id, runtime='zcode', model=args.model,
            workspace=str(work), prompt_sha256=prompt_sha256, stage=args.stage,
            chat_id=args.chat_id, claim_token=args.dispatch_claim,
            wrapper_pid=wrapper_pid, wrapper_created=wrapper_created)
        if not pool_gate['allowed']:
            print(json.dumps({'refused_before_dispatch': True, 'capacity_gate_rejected': True,
                              'sent': False, 'exit_code': 2,
                              'routing_required': bool(pool_gate.get('routing_required')),
                              'reason': pool_gate.get('reason'),
                              'selected': pool_gate.get('selected'),
                              'pool_key': pool_gate.get('pool_key'),
                              'domestic_full': pool_gate.get('domestic_full'),
                              'reasons': pool_gate['reasons']}, ensure_ascii=False))
            return 2
        claim_token = pool_gate['token']
        # ZCode 主力 GLM-5.3 池容量 2：把已核验的容量 claim 传给额度门，允许两个不同工作区
        # 同 provider 并行（仍保持同工作区单写入、活/未知占位保护）；无 claim 时额度门维持
        # 旧的通道单在途语义。
        quota_verified_claim = {'token': claim_token, 'store': dispatch_store,
                                'pool_key': pool_gate.get('pool_key'),
                                'capacity': dp.capacity_for(pool_gate.get('pool_key'))}

    def _release_capacity(terminal, success=None):
        """带真实 owner 身份释放本 attempt 容量名额，返回 finish 结果供核验；无 claim 时
        视为无需释放。释放异常不掩盖真实终态（容量对账由 reconcile 兜底）。"""
        if claim_token is None:
            return {'released': False, 'terminal': terminal, 'reason': 'no_claim'}
        try:
            return dp.finish(dispatch_store, claim_token, terminal=terminal,
                             success=success, wrapper_pid=wrapper_pid,
                             wrapper_created=wrapper_created)
        except Exception as exc:  # noqa: BLE001
            return {'settled': False, 'released': False, 'token': claim_token,
                    'terminal': terminal, 'error': str(exc)}

    quota_gate = qc.gate_dispatch(
        quota_store, runtime='zcode',
        identity={'provider': args.provider},
        workspace=str(work),
        purpose='probe' if args.quota_probe else 'dispatch',
        routes_path=args.quota_routes,
        verified_pool_claim=quota_verified_claim)
    if not quota_gate['allowed']:
        # 额度门拒绝：释放刚拿到的容量名额（若有，带 owner 身份），零输出目录、零 Popen。
        _release_capacity('start_failed')
        print(json.dumps({'refused_before_dispatch': True, 'sent': False,
                          'quota_gate_rejected': True,
                          'exit_code': 2, 'reasons': quota_gate['reasons'],
                          'quota_group': quota_gate.get('quota_group'),
                          'cooldown_until_utc': quota_gate.get('cooldown_until_utc')},
                         ensure_ascii=False))
        return 2

    # gate 之后、Popen 之前的所有准备阶段失败都属"已知未启动"（S4 缺陷 2）：安全结算
    # 本 attempt 的 start_failed（只释放本次占位，绝不释放别人/活执行器），sent=false、
    # 零 Popen。用一个局部收口函数，避免各提前 return 泄漏占位或泄漏容量名额。
    def _abort_before_start(payload, code):
        payload = dict(payload)
        payload['quota_settlement'] = qc.settle_attempt(
            quota_store, quota_gate, terminal='start_failed')
        if claim_token is not None:
            rel = _release_capacity('start_failed')
            # 如实反映是否真释放（owner 未核验/child 状态未知时可能未释放），绝不谎称。
            payload['capacity_released'] = bool(rel.get('released'))
        payload.setdefault('sent', False)
        print(json.dumps(payload, ensure_ascii=False))
        return code

    try:
        out.mkdir(parents=True, exist_ok=False)  # 拒绝覆盖或重放已有证据目录
    except FileExistsError:
        return _abort_before_start(
            {'refused_before_dispatch': True, 'exit_code': 2,
             'reasons': [f'output dir already exists: {out}']}, 2)
    except OSError as exc:
        # 普通 OSError/PermissionError/NotADirectoryError（非"已存在"）也是"已知未启动"：
        # 统一 settle start_failed、零 Popen、sent=false，只释放本 attempt 占位不泄漏。
        return _abort_before_start(
            {'refused_before_dispatch': True, 'exit_code': 3,
             'reasons': [f'output dir could not be created before start: {exc!r}']}, 3)

    # 接续 baseline：Popen 前对有界声明文件保存 baseline 字节副本+SHA 随 request 留证
    # （缺陷 I）；终态确认后再 evaluate 生成 continuation.json。
    continuation_copies = out / 'continuation-copies'
    continuation_baseline = None
    if continuation_spec is not None:
        try:
            continuation_baseline = cc.freeze(continuation_spec.get('files') or [],
                                             str(work), copies_dir=continuation_copies)
        except (ValueError, OSError) as exc:
            return _abort_before_start(
                {'dispatch_failed': True, 'exit_code': 3,
                 'reasons': [f'continuation baseline freeze failed before start: {exc}']},
                3)

    # 报告格式指令拼在原任务之前，逐字保留任务；不回换行、不修剪模型输出。runner 把
    # request['prompt'] 原样交给官方 submitPrompt，离线 receipts 记录它消费的哈希。
    try:
        if args.preflight_only:
            report_contract = None
            sent_prompt = prompt_text
        else:
            report_contract = pc.build_contract(args.stage, str(work))
            sent_prompt = pc.compose_task_payload(report_contract, prompt_text)
        sent_payload_bytes = sent_prompt.encode('utf-8')
        (out / 'sent-task-payload.bin').write_bytes(sent_payload_bytes)
        sent_readback = (out / 'sent-task-payload.bin').read_bytes()
    except Exception as exc:
        # 契约构造/载荷落盘/回读失败（PermissionError/OSError/其他）仍属"已知未启动"：
        # 统一 settle start_failed、零 Popen、sent=false、只释放本 attempt 占位。
        return _abort_before_start(
            {'dispatch_failed': True, 'refused_before_dispatch': True, 'exit_code': 3,
             'reasons': [f'sent payload contract/write failed before start: {exc!r}']}, 3)
    prompt_payload = pc.payload_evidence(
        raw_prompt_bytes=prompt_bytes, sent_task_text=prompt_text, contract=report_contract,
        sent_payload_bytes=sent_payload_bytes,
        newline_caliber=('CRLF/CR-to-LF; preflight-only does not submit to SDK'
                         if args.preflight_only else
                         'CRLF/CR-to-LF; task=request.prompt to submitPrompt, '
                         'contract prepended'))
    prompt_payload['readback_match'] = sent_readback == sent_payload_bytes
    prompt_payload['channels'] = {
        'request_prompt_sha256': pc.sha256_hex(sent_readback),
        'submit_channel': 'none' if args.preflight_only else 'sdk-request-prompt',
        'payload_role': 'planned_request_prompt' if args.preflight_only else 'submission_prompt',
        'contract_prepended': not args.preflight_only,
    }
    request = build_request(cfg, workspace=str(work),
                            prompt=sent_prompt,
                            out_dir=out, stage=args.stage, provider=args.provider,
                            model=args.model, reasoning=args.reasoning, mode=args.mode,
                            tools=tools, resume_session_id=args.session_id,
                            preflight_only=args.preflight_only, runner=runner,
                            prompt_sha256=prompt_sha256, prompt_payload=prompt_payload,
                            chat_id=args.chat_id)
    argv = build_node_argv(cfg, runner, out / 'request.json')
    request['argv'] = argv
    request['quota_gate'] = quota_gate
    # BW-ZCODE-MANUAL-QUOTA-20261008-S1：ZCode 额度由用户手动管理/重置，取消自动额度冷却。
    # 该标记如实声明本次 ZCode 派工不受自动额度冷却门禁（工作区单写入/通道单在途并发占位
    # 仍生效），绝不宣称有额度/已恢复/免费；普通派工不要求 recovery probe。
    request['quota_auto_cooldown_disabled'] = True
    request['quota_auto_cooldown_policy'] = (
        'ZCode auto quota cooldown disabled by user policy (manual quota management). '
        'No cooldown gate is applied to this dispatch; workspace single-writer and '
        'quota-channel single-in-flight concurrency placeholders still apply. This '
        'makes no claim about remaining quota, recovery, or free availability, and a '
        'normal dispatch does not require a recovery probe.')
    if args.quota_probe:
        # probe 实际执行边界留证（缺陷 D）：记录程序化强制的边界值，不是 purpose 标记。
        request['quota_probe_bounds'] = {
            'purpose': 'recovery_probe',
            'forbidden_tools': list(qc.PROBE_FORBIDDEN_TOOLS),
            'granted_tools': list(tools),
            'resume_session_id': None,
            'dispatch_plan': None,
            'prompt_max_bytes': qc.PROBE_PROMPT_MAX_BYTES,
            'prompt_bytes_sent': len(sent_payload_bytes),
            'timeout_seconds': args.quota_probe_timeout,
            'single_execution': True}
    if continuation_spec is not None:
        # 派发前有界声明文件 baseline 随 request 留证（缺陷 I）。
        request['continuation'] = {
            'contract_path': str(Path(args.continuation_contract).resolve()),
            'baseline': continuation_baseline,
            'prev_handoff': continuation_spec.get('prev_handoff'),
            'test_evidence': continuation_spec.get('test_evidence'),
            'original_report_ref': continuation_spec.get('original_report_ref'),
            'original_error_ref': continuation_spec.get('original_error_ref')}
    if plan_block is not None:
        request['dispatch_plan'] = plan_block
    if execution_contract is not None:
        request['execution_contract'] = execution_contract
        request['execution_contract_sha256'] = zee.contract_sha256(execution_contract)
        request['execution_contract_baseline'] = contract_baseline
    if command_contract is not None:
        # 冻结的注册表 + canonical SHA 一起入 request；runner 独立重算并精确比对后注入
        # 装饰执行端口与 permissionBroker。adapters_entry 仅用于定位公共适配器（不能
        # 覆盖真实 entry config），缺失则 runner fail-closed，approval 能力不标 ready。
        request['command_contract'] = command_contract
        request['command_contract_sha256'] = command_contract_sha256
        adapters_entry = cfg.get('adapters_entry')
        if adapters_entry is not None:
            # 显式兼容保留：adapters_entry 仅用于定位公共适配器入口，不能覆盖真实 entry config。
            adapters_path = Path(adapters_entry)
            if not adapters_path.is_absolute() or not adapters_path.is_file():
                return _abort_before_start(
                    {'refused_before_dispatch': True, 'exit_code': 2,
                     'reasons': [f'entry config adapters_entry must be an existing '
                                 f'absolute file: {adapters_entry}']}, 2)
            request['adapters_entry'] = str(adapters_path)
        else:
            # I：真实 zcode-entry.json 尚无 adapters_entry——不修改/复制真实入口，改从已核验
            # bootstrap 绝对路径定位同一公开 SDK 树的 adapters/src/index.ts；无法确定即 None，
            # 由 runner fail-closed（approval 能力不标 ready）。
            request['adapters_entry'] = _derive_adapters_entry(cfg['bootstrap'])
        # REPAIR3：官方 Bash handler 硬编码注入 embedded-search prelude；runner 需同一公开 SDK
        # 树的 embedded-search 解析器以冻结宿主可信 backend。同样**不修改/复制真实入口**，仅从
        # 已核验 bootstrap 绝对路径定位 bootstrap/src/app/embedded-search-backend.ts；找不到即不
        # 写入（runner 降级为无 prelude 绑定，带 prelude 的 Bash 命令仍被前置拒绝，不谎报放行）。
        es_entry = cfg.get('embedded_search_backend_entry')
        if es_entry is not None:
            es_path = Path(es_entry)
            if not es_path.is_absolute() or not es_path.is_file():
                return _abort_before_start(
                    {'refused_before_dispatch': True, 'exit_code': 2,
                     'reasons': [f'entry config embedded_search_backend_entry must be an '
                                 f'existing absolute file: {es_entry}']}, 2)
            request['embedded_search_backend_entry'] = str(es_path)
        else:
            derived = _derive_embedded_search_backend_entry(cfg['bootstrap'])
            if derived is not None:
                request['embedded_search_backend_entry'] = derived
    try:
        (out / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2),
                                          encoding='utf-8')
    except (OSError, TypeError, ValueError) as exc:
        return _abort_before_start(
            {'refused_before_dispatch': False, 'dispatch_failed': True, 'exit_code': 3,
             'reasons': [f'request.json serialization failed: {exc}']}, 3)

    # attempt 生命周期（缺陷 C）：占位释放绝不再先于终态识别/冷却落库。
    # - 启动前失败（从未 Popen）→ settle start_failed，安全释放占位；
    # - 终态无法确认（wait/communicate 异常且无法回收）→ settle unknown，保留占位退出；
    # - 已确认退出 → 解析终态 → 冷却写入/清除 + 占位释放，同一事务原子完成。
    child = None
    timed_out = False
    try:
        with (out / 'stdout.json').open('wb') as stdout, (out / 'stderr.log').open('wb') as stderr:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout,
                                     stderr=stderr, cwd=str(work))
            if claim_token is not None:
                bind = dp.bind_child(dispatch_store, claim_token, child.pid,
                                     wrapper_pid=wrapper_pid, wrapper_created=wrapper_created)
                if not bind.get('bound'):
                    # 无法把真实子进程绑到本 attempt（owner 不符/已绑/不在途）：子进程确已
                    # 启动且存活未知——只标 unknown 继续占容量与占位，绝不释放、绝不误抢、
                    # 绝不杀其它任务，交 reconcile/人工核验；如实上报，不谎称成功或已释放。
                    dp.mark_unknown(dispatch_store, claim_token)
                    qc.settle_attempt(quota_store, quota_gate, terminal='unknown')
                    print(json.dumps({'dispatch_failed': True, 'sent': True,
                                      'terminal_state': 'unknown', 'capacity_held': True,
                                      'reasons': [f'could not bind child to claim: '
                                                  f'{bind.get("reasons") or bind.get("reason")}']},
                                     ensure_ascii=False))
                    return 3
            (out / 'process.json').write_text(json.dumps({'pid': child.pid, 'state': 'running',
                                                          'started_at_utc': _now()}),
                                              encoding='utf-8')
            try:
                if args.quota_probe:
                    try:
                        child.communicate(timeout=args.quota_probe_timeout)
                    except subprocess.TimeoutExpired:
                        # 有界 recovery probe 超时：kill 后回收，按非 429 失败结算，绝不写 healthy。
                        timed_out = True
                        child.kill()
                        child.communicate()
                else:
                    child.wait()
            except subprocess.TimeoutExpired:
                timed_out = True
                child.kill()
                child.communicate()
    except KeyboardInterrupt:
        # 中断时子进程存活未知：只标 unknown 继续占容量与占位，绝不释放、绝不误抢，交由 reconcile。
        if claim_token is not None:
            dp.mark_unknown(dispatch_store, claim_token)
        qc.settle_attempt(quota_store, quota_gate, terminal='unknown')
        raise
    except Exception as exc:
        if child is None:
            # Popen 本身失败（从未启动子进程）：带 owner 身份 settle start_failed，如实反映
            # 是否真释放，绝不谎称已释放。
            rel = _release_capacity('start_failed')
            settlement = qc.settle_attempt(quota_store, quota_gate,
                                          terminal='start_failed')
            print(json.dumps({'dispatch_failed': True, 'sent': False,
                              'capacity_released': bool(rel.get('released')),
                              'reasons': [f'child launch failed before start: {exc}'],
                              'quota_settlement': settlement}, ensure_ascii=False))
            return 3
        if claim_token is not None:
            dp.mark_unknown(dispatch_store, claim_token)
        settlement = qc.settle_attempt(quota_store, quota_gate, terminal='unknown')
        print(json.dumps({'dispatch_failed': True, 'sent': True,
                          'terminal_state': 'unknown',
                          'reasons': [f'subprocess terminal state could not be '
                                      f'confirmed: {exc}',
                                      'workspace/channel/probe placeholders retained '
                                      '(fail-closed); verify the executor, then use '
                                      'the manual release path'],
                          'quota_settlement': settlement}, ensure_ascii=False))
        return 3
    (out / 'process.json').write_text(json.dumps({'pid': child.pid, 'state': 'exited',
                                                  'exit_code': child.returncode,
                                                  'finished_at_utc': _now()}),
                                      encoding='utf-8')
    # 子进程真实结束 → 先释放容量名额（带 owner 身份、核验探针回读 child 已 dead），即使
    # 随后的 envelope 解析/报告绑定/接续保存失败容量也已释放；原始工作区/任务保护与额度
    # 终态结算仍独立按下面既有逻辑处理。释放结果如实记录，未释放不谎称已释放。
    capacity_release = _release_capacity('finished', success=(child.returncode == 0))
    capacity_released = bool(capacity_release.get('released'))

    envelope, parse_error = _read_envelope(out / 'stdout.json')
    summary = {'exit_code': child.returncode, 'finished_at_utc': _now(),
               'stage': args.stage, 'workspace': str(work), 'output_dir': str(out),
               'provider_requested': args.provider, 'model_requested': args.model,
               'reasoning_requested': args.reasoning, 'mode': args.mode,
               'allowed_tools': tools,
               'tool_disallowlist_base': request['tool_disallowlist_base'],
               'resume_session_id': args.session_id,
               'preflight_only': args.preflight_only,
               'capacity_released': capacity_released,
               'capacity_terminal': capacity_release.get('terminal'),
               'protocol_success': False, 'business_verified': False,
               'free_quota_verified': False,
               # BW-ZCODE-MANUAL-QUOTA-20261008-S1：明确标识 ZCode 自动额度冷却已禁用，
               # 并发占位保留；绝不谎称有额度/恢复/免费（free_quota_verified 恒为 False）。
               'quota_auto_cooldown_disabled': True,
               'quota_auto_cooldown_policy': (
                   'ZCode auto quota cooldown disabled by user policy (manual quota '
                   'management); concurrency placeholders retained; no claim of '
                   'remaining quota, recovery, or free availability')}
    report_state = None
    if parse_error or not isinstance(envelope, dict):
        summary['parse_error'] = parse_error
        report_state = {'bound': False, 'body_ok': False, 'carrier_missing': True,
                        'reasons': [parse_error or 'runner envelope is not an object']}
    else:
        summary.update({
            'preflight_ok': bool(envelope.get('preflight_ok')),
            'submitted': bool(envelope.get('submitted')),
            'session_id': envelope.get('session_id'),
            'turn_id': envelope.get('turn_id'),
            'turn_status': envelope.get('status'),
            'terminal_success': envelope.get('terminal_success'),
            'model_requests_observed': envelope.get('model_requests_observed'),
            'model_label': envelope.get('model_label'),
            'selection_before_submit': envelope.get('selection_before_submit'),
            'selection_after_submit': envelope.get('selection_after_submit'),
            'usage': envelope.get('usage'),
            'event_count': envelope.get('event_count'),
            'observed_tool_catalog': envelope.get('observed_tool_catalog'),
            'tool_disallowlist_effective': envelope.get('tool_disallowlist_effective'),
            'runner_errors': envelope.get('errors') or [],
            'runner_limitations': envelope.get('limitations') or [],
            'serialization_failed': bool(envelope.get('serialization_failed')),
            # 受控命令审批四个独立状态，原样透传 runner 判定，绝不相互推断。
            'command_contract_present': bool(envelope.get('command_contract_present')),
            'tool_visible_bash': bool(envelope.get('tool_visible_bash')),
            'approval_client_ready': bool(envelope.get('approval_client_ready')),
            'controlled_pre_exec_gate_ready': bool(
                envelope.get('controlled_pre_exec_gate_ready')),
            'command_actually_executed': bool(envelope.get('command_actually_executed')),
            'command_executed_receipts': envelope.get('command_executed_receipts'),
            'command_attempted': envelope.get('command_attempted'),
            'command_started': envelope.get('command_started'),
            # REPAIR3：SDK 内部 embedded-search prelude 绑定状态（仅非敏感 hash/kind）。
            'embedded_search_prelude_bound': bool(
                envelope.get('embedded_search_prelude_bound')),
            'embedded_search_backend_kind': envelope.get('embedded_search_backend_kind'),
            'embedded_search_prelude_sha256': envelope.get('embedded_search_prelude_sha256'),
            'permission_audit_events': envelope.get('permission_audit_events'),
            'permission_states': envelope.get('permission_states') or {},
        })
        summary['protocol_success'] = (child.returncode == 0
                                       and envelope.get('ok') is True
                                       and envelope.get('preflight_ok') is True
                                       and not envelope.get('errors'))
        if args.preflight_only:
            summary['protocol_success'] = (child.returncode == 0
                                           and envelope.get('ok') is True)
            report_state = {'bound': False, 'body_ok': False, 'preflight_only': True,
                            'reasons': ['preflight-only run creates no app/session and '
                                        'submits no prompt; there is no report to bind']}
        else:
            response_path = out / 'response.md'
            if not response_path.is_file():
                report_state = {'bound': False, 'body_ok': False, 'carrier_missing': True,
                                'reasons': ['runner wrote no response.md']}
            else:
                disk = response_path.read_bytes()
                digest = hashlib.sha256(disk).hexdigest()
                summary['response_sha256'] = digest
                summary['response_bytes'] = len(disk)
                readback_match = (digest == envelope.get('response_sha256')
                                  and len(disk) == envelope.get('response_bytes'))
                try:
                    text = disk.decode('utf-8')
                except UnicodeDecodeError as exc:
                    text = None
                    report_state = {'bound': False, 'body_ok': False,
                                    'reasons': [f'response.md is not valid UTF-8: {exc}']}
                if text is not None:
                    body = analyze_report(text, args.stage, str(work))
                    binding = finalize_binding(
                        body['body_ok'], body['reasons'],
                        protocol_success=summary['protocol_success'],
                        session_id=summary.get('session_id'),
                        requested_session_id=args.session_id,
                        readback_match=readback_match)
                    report_state = {**body, 'response_sha256': digest,
                                    'readback_match': readback_match,
                                    'protocol_success': summary['protocol_success'],
                                    'session_id': summary.get('session_id'),
                                    'requested_session_id': args.session_id,
                                    'binding': binding,
                                    'bound': binding['bound'],
                                    'reasons': binding['reasons']}
    if report_state is not None:
        report_state.setdefault('protocol_success', summary['protocol_success'])
        report_state.setdefault('session_id', summary.get('session_id'))
        (out / 'report-state.json').write_text(
            json.dumps(report_state, ensure_ascii=False, indent=2), encoding='utf-8')
        summary['report_bound'] = report_state.get('bound', False)
        summary['report_state_file'] = str(out / 'report-state.json')
    events = _read_events(out / 'events.jsonl')
    disk_read = lambda p: Path(p).read_bytes() if Path(p).is_file() else None  # noqa: E731
    env_dict = envelope if isinstance(envelope, dict) else {}
    evidence = zee.evaluate_execution_evidence(
        events, execution_contract,
        session_id=env_dict.get('session_id'),
        turn_id=env_dict.get('turn_id'),
        workspace=str(work), read_bytes=disk_read,
        baseline=contract_baseline, preflight_only=args.preflight_only)
    if execution_contract is not None:
        evidence['contract_sha256'] = zee.contract_sha256(execution_contract)
        evidence['baseline'] = contract_baseline
    (out / 'execution-evidence.json').write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    summary['execution_evidence_ok'] = evidence['execution_evidence_ok']
    summary['execution_evidence_status'] = evidence['execution_evidence_status']
    summary['execution_evidence_reasons'] = evidence['reasons']
    summary['execution_evidence_file'] = str(out / 'execution-evidence.json')
    # 本次子进程自己写出的 stderr（有界读取，仅交给白名单帧解析器，绝不回显/落进证据）。
    stderr_path = out / 'stderr.log'
    stderr_bytes = stderr_path.read_bytes() if stderr_path.is_file() else None
    facts = _zcode_failure_facts(summary, envelope if isinstance(envelope, dict) else None,
                                 events, stderr_bytes=stderr_bytes, provider=args.provider)
    # 两个业务证据分类只来自结构化门禁结果，不扫描提示/报告关键词。
    status = evidence['execution_evidence_status']
    facts['no_required_execution'] = status == 'no_required_execution'
    facts['execution_claim_mismatch'] = status == 'execution_claim_mismatch'
    summary['diagnostics'] = ec.diagnose(facts)
    wrapper = facts['evidence'].get('wrapper_quota_error')
    raw_errors = (envelope or {}).get('errors') if isinstance(envelope, dict) else []
    raw_errors_info = (envelope or {}).get('errors_info') if isinstance(envelope, dict) else None
    merged_errors_info = list(raw_errors_info) if isinstance(raw_errors_info, list) else []
    if wrapper is not None:
        merged_errors_info = merged_errors_info + [wrapper]
    classification = None
    if summary['protocol_success'] is not True and facts.get('quota_429') and not timed_out:
        classification = qc.classify_quota_failure(
            raw_errors or [], merged_errors_info,
            retry_after=qc.extract_retry_after(raw_errors or [], merged_errors_info))
    # S6 缺陷 2：本次原始错误载体判定（与接续/结算共用一份，先于任何提前 return 落定）。
    # ZCode 的结构化失败信封（S2 同形 string-only errors）落在**本次 out/stdout.json**；
    # SDK ProviderBusinessError 帧落在**本次 out/stderr.log**。二者都属真实失败载体：
    # - 只有 stdout.json 有失败信封、stderr 为空或只有 warning 时，原错误 ref 直接指向
    #   本次 stdout.json 失败信封，SHA 对其原始字节计算；绝不因 stderr 空就漏记原错误。
    # - stderr 只有 warning（无白名单错误帧）时，_provider_business_error_from_stderr 返回
    #   None，绝不把 warning 冒充原始错误。
    # - 两载体同时存在时，stdout 失败信封为主 original_error_ref，SDK stderr 帧的 path+SHA+
    #   白名单分类字段一并保留（不复制 headers/raw 字节）。
    # report-state.json 只是独立格式诊断，永不作 original_error_ref；历史 spec 引用另列。
    stdout_carrier = out / 'stdout.json'
    stderr_carrier = out / 'stderr.log'
    protocol_failed = summary.get('protocol_success') is not True
    sdk_stderr_frame = (_provider_business_error_from_stderr(stderr_bytes, args.provider)
                        if protocol_failed and stderr_bytes else None)
    if protocol_failed and stdout_carrier.is_file() and stdout_carrier.stat().st_size > 0:
        current_error = stdout_carrier
    elif sdk_stderr_frame is not None:
        current_error = stderr_carrier
    else:
        current_error = None  # 仅 warning/无错误结构 → 不冒充原错误，如实保留缺失
    if current_error is not None:
        carrier_bytes = current_error.read_bytes()
        ref = {'path': str(current_error), 'sha256': hashlib.sha256(carrier_bytes).hexdigest(),
               'carrier': current_error.name,
               'note': 'this run\'s failure envelope (stdout.json) or SDK ProviderBusinessError '
                       'stderr frame; raw bytes & headers never echoed; report-state.json is a '
                       'separate format diagnostic, never the original error; warning-only or '
                       'empty stderr without a real frame cannot masquerade as the original error'}
        if sdk_stderr_frame is not None:
            sdk_bytes = stderr_carrier.read_bytes() if stderr_carrier.is_file() else b''
            ref['sdk_stderr_frame'] = {
                'path': str(stderr_carrier), 'sha256': hashlib.sha256(sdk_bytes).hexdigest(),
                'status': sdk_stderr_frame.get('status'),
                'provider_code': sdk_stderr_frame.get('provider_code'),
                'wrapper_code': sdk_stderr_frame.get('wrapper_code'),
                'category': sdk_stderr_frame.get('category'),
                'reset_timezone': sdk_stderr_frame.get('reset_timezone', 'unverified')}
        summary['original_error_reference'] = ref
    # 保全优先于结算（S4 缺陷 4/6）：仍在持有工作区占位时，先落盘当前原始报告/失败错误，
    # 冻结终态源副本+回读、生成接续包，随后再做原子额度结算+释放。这样另一执行器在冻结
    # 阶段看不到被过早释放的空窗。当前报告引用只从 out 固定路径派生（response.md 存在与否
    # 如实反映本次），历史 spec 引用另列，绝不冒充当前。
    if continuation_baseline is not None:
        def _resolve_ws_ref(rel):
            if rel is None:
                return None
            return str(Path(os.path.join(str(work), str(rel))))
        # 当前 run 的真实报告：response.md（成功才有）。原始错误绑定上面统一判定的本次失败
        # 载体（stdout.json 失败信封优先，SDK stderr 帧另存），绝不指向空 report-state.json。
        current_report = out / 'response.md'
        try:
            evaluation = cc.evaluate(continuation_baseline, str(work),
                                     copies_dir=continuation_copies)
            test_binding = None
            test_stale = None
            tev = (continuation_spec or {}).get('test_evidence')
            if tev:
                test_binding = cc.bind_registered_test(
                    tev['evidence_path'], str(work), tev['registered_inputs'])
                test_stale = cc.test_inputs_stale(test_binding)
            handoff = cc.build_handoff(
                continuation_baseline, evaluation, test_binding, test_stale,
                (continuation_spec or {}).get('todos'),
                original_report_ref=str(current_report) if current_report.is_file() else None,
                original_error_ref=str(current_error)
                if current_error is not None and current_error.is_file() else None,
                write_receipts=cc.extract_write_receipts(
                    events, env_dict.get('session_id'), env_dict.get('turn_id')),
                terminal_state='probe_timeout' if timed_out else 'confirmed_exit')
            handoff['historical_refs'] = {
                'original_report_ref': _resolve_ws_ref(
                    (continuation_spec or {}).get('original_report_ref')),
                'original_error_ref': _resolve_ws_ref(
                    (continuation_spec or {}).get('original_error_ref')),
                'note': 'spec-carried refs are prior-handoff history only; the bound '
                        'current report/error above are derived from this run\'s out/ '
                        'path and never impersonated by older refs'}
            handoff['declared_diffs'] = cc.diff_declared(continuation_baseline, evaluation)
            handoff_bytes = json.dumps(handoff, ensure_ascii=False, indent=2).encode('utf-8')
            (out / 'continuation.json').write_bytes(handoff_bytes)
            summary['continuation'] = {
                'file': str(out / 'continuation.json'),
                'sha256': hashlib.sha256(handoff_bytes).hexdigest(),
                'workspace_realpath': handoff['workspace_realpath'],
                'final_worker_report_present': handoff['final_worker_report_present'],
                'any_changed': handoff['any_changed'],
                'test_inputs_stale': (handoff.get('test_inputs_stale') or {}).get('stale')}
        except Exception as exc:
            # 保全失败：保留真实错误 + 在途占位（settle unknown，不释放），不先释放再假装保全。
            summary['continuation_persist_failed'] = str(exc)
            settlement = qc.settle_attempt(quota_store, quota_gate, terminal='unknown')
            summary['quota_settlement'] = {
                'settled': settlement.get('settled'), 'released': settlement.get('released'),
                'terminal_state': settlement.get('terminal_state'),
                'purpose': quota_gate.get('purpose')}
            summary['placeholders_retained'] = True
            (out / 'summary.json').write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 3
    else:
        summary['continuation_status'] = 'unverified_no_contract'
    # 额度终态结算（缺陷 C/E/F）：确认终态 → 同一事务原子写冷却/清除 + 释放占位。非 429
    # 失败/取消/probe 超时 → 只结算占位，既不写冷却也绝不写 healthy。迟到成功不得覆盖新冷却。
    settlement = qc.settle_attempt(
        quota_store, quota_gate, terminal='confirmed_exit',
        success=bool(summary['protocol_success'] is True and not args.preflight_only),
        classification=classification,
        source='zcode_direct terminal envelope (exit '
               f'{child.returncode}, protocol_success={summary["protocol_success"]}, '
               f'timed_out={timed_out}, preflight_only={args.preflight_only})')
    quota_outcome = settlement.get('quota_outcome')
    summary['quota_settlement'] = {'settled': settlement.get('settled'),
                                   'released': settlement.get('released'),
                                   'terminal_state': settlement.get('terminal_state'),
                                   'purpose': quota_gate.get('purpose')}
    if timed_out:
        summary['quota_probe_timed_out'] = True
    if wrapper is not None:
        # 保留原始引用与来源 SHA（脱敏白名单，不含 headers），不猜时区、不自动重派。
        summary['quota_wrapper_reference'] = {
            'status': wrapper.get('status'), 'provider_code': wrapper.get('provider_code'),
            'provider_code_raw': wrapper.get('code'), 'wrapper_code': wrapper.get('wrapper_code'),
            'category': wrapper.get('category'),
            'provider': wrapper.get('provider'),
            'reset_timezone': wrapper.get('reset_timezone', 'unverified'),
            'source_stderr_sha256': wrapper.get('source_stderr_sha256'),
            'original_source': wrapper.get('original_source'),
            'event_count': summary.get('event_count'),
            'note': 'whitelist fields only; HTTP headers and raw stderr never read '
                    'or emitted; no UTC offset guessed; no auto re-dispatch; inner numeric '
                    'provider_code (1308) distinguished from the outer wrapper_code'}
    summary['quota_outcome'] = quota_outcome
    if plan_block is not None:
        summary['dispatch_plan'] = plan_block
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary['protocol_success'] is not True:
        return 3
    # 明确契约下，执行证据未通过（false 或未验 null）不能以成功退出码交付给调度。
    if execution_contract is not None and summary.get('execution_evidence_ok') is not True:
        return 3
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
