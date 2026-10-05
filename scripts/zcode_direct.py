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
DEFAULT_PROVIDER = 'account:bigmodel-individual-coding-plan'
DEFAULT_MODEL = 'GLM-5.3-Flash'
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


def build_node_argv(cfg: dict, runner: Path, request_path: Path) -> list:
    """无 shell 的参数数组。真实 node 走 tsx loader；测试可用 node_args/runner 换载体。"""
    node_args = cfg.get('node_args')
    if node_args is None:
        node_args = ['--import', Path(cfg['tsx_loader']).as_uri()]
    return [cfg['node'], *node_args, str(runner), '--request', str(request_path)]


def build_request(cfg: dict, *, workspace: str, prompt: str, out_dir: Path,
                  stage: str, provider: str, model: str, reasoning: str,
                  mode: str, tools: list, resume_session_id,
                  preflight_only: bool, runner: Path) -> dict:
    selection = {'providerId': provider, 'modelId': model,
                 'options': {'reasoningLevel': reasoning}}
    return {
        'carrier': 'zcode-sdk',
        'started_at_utc': _now(),
        'stage': stage,
        'workspace': workspace,
        'output_dir': str(out_dir),
        'prompt': prompt,
        'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
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
    ap.add_argument('--config', default=str(DEFAULT_CONFIG),
                    help='Local ZCode entry config JSON with absolute runtime paths.')
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
        prompt_text = prompt_path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        print(json.dumps({'refused_before_dispatch': True,
                          'reasons': [f'prompt file unreadable as UTF-8: {exc}']},
                         ensure_ascii=False))
        return 2
    if not prompt_text.strip():
        print(json.dumps({'refused_before_dispatch': True,
                          'reasons': ['prompt file is empty']}, ensure_ascii=False))
        return 2

    runner = Path(cfg['runner']) if cfg.get('runner') else DEFAULT_RUNNER
    if not runner.is_file():
        print(json.dumps({'refused_before_dispatch': True,
                          'reasons': [f'runner script missing: {runner}']},
                         ensure_ascii=False))
        return 2

    out = Path(args.output_dir).resolve()
    try:
        out.mkdir(parents=True, exist_ok=False)  # 拒绝覆盖或重放已有证据目录
    except FileExistsError:
        print(json.dumps({'refused_before_dispatch': True,
                          'reasons': [f'output dir already exists: {out}']},
                         ensure_ascii=False))
        return 2

    # 报告格式指令拼在原任务之前，逐字保留任务；不回换行、不修剪模型输出。
    contract = ('Final response must contain only the complete nine-section report. '
                'First line: WORKER_REPORT_START. Last line: WORKER_REPORT_END. '
                'Those markers must appear exactly once each; never quote them in the body. '
                'No preface, epilogue, or code fences. Keep field names and values on the same line: '
                f'阶段编号与执行方式：{args.stage}；direct。 '
                f'实际项目绝对路径：{work}. Use the exact path without punctuation in its field.')
    request = build_request(cfg, workspace=str(work),
                            prompt=(contract + '\n\n' + prompt_text)
                            if not args.preflight_only else prompt_text,
                            out_dir=out, stage=args.stage, provider=args.provider,
                            model=args.model, reasoning=args.reasoning, mode=args.mode,
                            tools=tools, resume_session_id=args.session_id,
                            preflight_only=args.preflight_only, runner=runner)
    argv = build_node_argv(cfg, runner, out / 'request.json')
    request['argv'] = argv
    try:
        (out / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2),
                                          encoding='utf-8')
    except (OSError, TypeError, ValueError) as exc:
        print(json.dumps({'refused_before_dispatch': False, 'dispatch_failed': True,
                          'reasons': [f'request.json serialization failed: {exc}']},
                         ensure_ascii=False))
        return 3

    with (out / 'stdout.json').open('wb') as stdout, (out / 'stderr.log').open('wb') as stderr:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout,
                                 stderr=stderr, cwd=str(work))
        (out / 'process.json').write_text(json.dumps({'pid': child.pid, 'state': 'running',
                                                      'started_at_utc': _now()}),
                                          encoding='utf-8')
        child.wait()
    (out / 'process.json').write_text(json.dumps({'pid': child.pid, 'state': 'exited',
                                                  'exit_code': child.returncode,
                                                  'finished_at_utc': _now()}),
                                      encoding='utf-8')

    envelope, parse_error = _read_envelope(out / 'stdout.json')
    summary = {'exit_code': child.returncode, 'finished_at_utc': _now(),
               'stage': args.stage, 'workspace': str(work), 'output_dir': str(out),
               'provider_requested': args.provider, 'model_requested': args.model,
               'reasoning_requested': args.reasoning, 'mode': args.mode,
               'allowed_tools': tools,
               'tool_disallowlist_base': request['tool_disallowlist_base'],
               'resume_session_id': args.session_id,
               'preflight_only': args.preflight_only,
               'protocol_success': False, 'business_verified': False,
               'free_quota_verified': False}
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
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary['protocol_success'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
