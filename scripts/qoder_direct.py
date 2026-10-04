"""Direct Qoder invocation: transport layer for the brain-worker skill (direct mode).

从已验证的 validation/QODER-DIRECT-01/qoder_direct.py 最小改动而来：
- 运行时不再假设与脚本同目录，改由本机入口配置（--config，默认脚本旁
  local-entry.json）提供 node 与 qodercli 绝对路径；
- response.md 以字节原样落盘（不做换行翻译），新增 report-state.json 汇总
  报告验收证据（标记独占行/阶段字段/项目路径/九节/哈希回读）；
- 协议成功不等于业务验收：退出码只反映协议终态，报告绑定结论以
  report-state.json 为准，缺失或不符一律不宣称已绑定。
- 正文格式检查与最终绑定分离：analyze_report 只做机械正文核对（body_ok），
  finalize_binding 再综合协议终态、session_id、resume 精确一致与哈希回读
  得出最终 bound；机械正文合格不冒充已绑定。
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / 'local-entry.json'
DEFAULT_MODEL = 'Qwen3.8-Flash'
REPORT_START = 'WORKER_REPORT_START'
REPORT_END = 'WORKER_REPORT_END'
# 明确可接受的字段名：阶段字段允许"阶段编号"或模板中的"阶段编号与执行方式"，
# 且必须紧跟中/英文冒号；其余同前缀写法一律视为非本字段（拒绝误认）。
STAGE_FIELD_NAMES = ('阶段编号', '阶段编号与执行方式')
PATH_FIELD_NAMES = ('实际项目绝对路径',)
COLONS = ('：', ':')  # U+FF1A full-width and ASCII colon
SECTION_HEADERS = ('一、当前基线与授权', '二、实际执行范围', '三、已验证事实',
                   '四、推断（必须与事实分开）', '五、测试与验证',
                   '六、未完成项与剩余风险', '七、实际副作用与越界检查',
                   '八、本阶段状态', '九、建议下一步（只提出建议，不执行）')


def load_entry_config(path=None) -> dict:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.is_absolute():
        raise ValueError(f'entry config path must be absolute: {cfg_path}')
    if not cfg_path.is_file():
        raise FileNotFoundError(f'entry config file not found: {cfg_path}')
    cfg_path = cfg_path.resolve()
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
    if not isinstance(cfg, dict):
        raise ValueError(f'entry config must be a JSON object: {cfg_path}')
    for key in ('node', 'qodercli'):
        if key not in cfg:
            raise KeyError(f'entry config missing key {key!r}: {cfg_path}')
        p = Path(cfg[key])
        if not p.is_absolute() or not p.is_file():
            raise FileNotFoundError(f'entry config {key!r} must be an existing absolute file: {p}')
    return cfg


def build_argv(cfg: dict, workspace: str, model: str, tools: str,
               session_id: str | None) -> list[str]:
    """与 QODER-DIRECT-01 已验证调用形状一致的安全参数数组（无 shell）。"""
    argv = [cfg['node'], cfg['qodercli'],
            '--cwd', workspace, '--model', model,
            '--tools', tools, '--permission-mode', 'dont_ask',
            '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--output-format', 'json', '-p']
    if tools:
        argv += ['--allowed-tools', tools]
    if session_id:
        argv += ['--resume', session_id]
    return argv


def _norm_path(p: str) -> str:
    try:
        return os.path.normpath(str(Path(p.strip().strip('"')))).rstrip(os.sep).lower()
    except Exception:
        return p.strip().lower()


def _extract_field(body: str, names) -> tuple[str | None, str | None]:
    """严格字段抽取：仅接受明确字段名后紧跟中/英文冒号的行。

    - 同前缀但非本字段（如“阶段编号XYZ：”）不匹配，视为该字段缺失；
    - 出现多行匹配（重复/歧义）返回错误，不做宽松的第一匹配处理；
    返回 (值, 错误)；错误为 None 表示抽取成功且无歧义（值可能为空串）。
    """
    hits = []
    for raw in body.splitlines():
        core = raw.strip()
        if core.startswith('-'):
            core = core.lstrip('-').strip()
        for name in names:
            if core.startswith(name) and core[len(name):len(name) + 1] in COLONS:
                hits.append((name, core[len(name) + 1:].strip()))
                break
    if not hits:
        return None, None
    if len(hits) > 1:
        return None, f'ambiguous duplicate field lines: {hits!r}'
    return hits[0][1], None


def analyze_report(text: str, stage: str | None, project_root: str) -> dict:
    """对报告正文做机械格式核对，仅得出 body_ok（格式合格与否）；任何一项不符即
    body_ok=false 并保留原因，不修改原文、不替执行器补写或删除前言。最终是否绑定由
    finalize_binding 综合传输层事实后决定，本函数不单独宣称完成绑定。"""
    lines = text.splitlines(keepends=True)
    lines_ns = text.splitlines()
    start_lines, end_lines = [], []
    for i, line in enumerate(lines):
        core = line[:-1] if line.endswith('\n') else line
        if core.endswith('\r'):
            core = core[:-1]
        if core == REPORT_START:
            start_lines.append(i)
        elif core == REPORT_END:
            end_lines.append(i)
    reasons = []
    markers_ok = True
    if len(start_lines) != 1 or len(end_lines) != 1:
        markers_ok = False
        reasons.append(f'boundary markers not exactly one pair '
                       f'(start={len(start_lines)}, end={len(end_lines)})')
    elif start_lines[0] >= end_lines[0]:
        markers_ok = False
        reasons.append('WORKER_REPORT_START does not precede WORKER_REPORT_END')
    if text.count(REPORT_START) != len(start_lines) \
            or text.count(REPORT_END) != len(end_lines):
        markers_ok = False
        reasons.append('marker substring appears on non-exclusive lines')
    if lines_ns and lines_ns[0].rstrip('\r') != REPORT_START:
        markers_ok = False
        reasons.append('first line is not exclusively WORKER_REPORT_START '
                       '(preface or text outside markers)')
    if lines_ns and lines_ns[-1].rstrip('\r') != REPORT_END:
        markers_ok = False
        reasons.append('last line is not exclusively WORKER_REPORT_END '
                       '(trailing text outside markers)')
    body = ''
    if markers_ok:
        body = ''.join(lines[start_lines[0]:end_lines[0] + 1])
        if body.splitlines()[0] != REPORT_START \
                or body.splitlines()[-1].rstrip('\r') != REPORT_END:
            markers_ok = False
            reasons.append('boundary markers not exclusive lines')

    stage_val, stage_err = _extract_field(body, STAGE_FIELD_NAMES)
    stage_token_match = None
    if stage_err:
        stage_token_match = False
        reasons.append(stage_err)
    if not stage:
        stage_token_match = False
        reasons.append('no expected --stage provided; binding cannot be confirmed')
    elif stage_val is None:
        stage_token_match = False
        reasons.append('report has no 阶段编号/阶段编号与执行方式 field line')
    else:
        token_re = re.compile(
            r'(?<![A-Za-z0-9._-])' + re.escape(stage) + r'(?![A-Za-z0-9._-])')
        stage_token_match = bool(token_re.search(stage_val))
        if not stage_token_match:
            reasons.append(f'阶段编号 field value does not carry stage {stage!r} '
                           f'as an exact token')

    path_val, path_err = _extract_field(body, PATH_FIELD_NAMES)
    path_match = path_val is not None and _norm_path(path_val) == _norm_path(project_root)
    if path_err:
        reasons.append(path_err)
    if path_val is None:
        reasons.append('report has no 实际项目绝对路径 field line')
    elif not path_match:
        reasons.append(f'实际项目绝对路径 {path_val!r} does not equal workspace '
                       f'{project_root!r}')

    positions = []
    for h in SECTION_HEADERS:
        pos = None
        for i, line in enumerate(body.splitlines()):
            if line.strip().startswith(h):
                pos = i
                break
        positions.append(pos)
    missing = [h for h, pos in zip(SECTION_HEADERS, positions) if pos is None]
    found = [pos for pos in positions if pos is not None]
    sections_in_order = bool(found) and found == sorted(found)
    if missing:
        reasons.append(f'missing report sections: {missing}')
    if not sections_in_order and not missing:
        reasons.append('report sections out of order')
    closing_line = any(line.strip() == '本阶段汇报结束；等待主脑验收。'
                       for line in body.splitlines())

    body_ok = (markers_ok and bool(stage) and stage_token_match is True
               and path_match and not missing and sections_in_order)
    return {'note': 'transport-layer body-format check only; body_ok is the '
                    'mechanical text gate, NOT the final binding. The final '
                    'binding is produced by finalize_binding, which additionally '
                    'requires protocol success, a non-empty session_id, exact '
                    'resume match and a matching response readback hash.',
            'body_ok': body_ok,
            'bound': body_ok,
            'reasons': reasons,
            'markers': {'ok': markers_ok,
                        'start_exclusive_lines': len(start_lines),
                        'end_exclusive_lines': len(end_lines),
                        'start_substring_count': text.count(REPORT_START),
                        'end_substring_count': text.count(REPORT_END)},
            'stage_field': stage_val,
            'stage_checked': bool(stage),
            'stage_token_match': stage_token_match,
            'path_field': path_val,
            'path_match': path_match,
            'sections_found': len(SECTION_HEADERS) - len(missing),
            'sections_missing': missing,
            'sections_in_order': sections_in_order,
            'closing_line_present': closing_line}


def finalize_binding(body_ok: bool, body_reasons, *, protocol_success: bool,
                     session_id, requested_session_id, readback_match: bool) -> dict:
    """最终绑定汇总（纯函数，可离线注入）：在正文格式之外，综合协议终态、
    非空 session_id、resume 精确一致与响应哈希回读。任一不满足即 bound=false，
    且不改动正文。"""
    reasons = list(body_reasons)
    bound = bool(body_ok) and bool(protocol_success) and bool(readback_match)
    if not protocol_success:
        reasons.append('protocol not successful (protocol_success=false)')
    if not (isinstance(session_id, str) and session_id.strip()):
        bound = False
        reasons.append('envelope lacks a non-empty session_id')
    if requested_session_id is not None and session_id != requested_session_id:
        bound = False
        reasons.append(f'resume requested session {requested_session_id!r} but '
                       f'actual session_id {session_id!r} is not an exact match')
    if not readback_match:
        bound = False
        reasons.append('response readback hash does not match in-memory response')
    return {'bound': bound,
            'body_ok': bool(body_ok),
            'protocol_success': bool(protocol_success),
            'session_id': session_id,
            'requested_session_id': requested_session_id,
            'readback_match': bool(readback_match),
            'reasons': reasons}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workspace', required=True)
    ap.add_argument('--prompt-file', required=True)
    ap.add_argument('--output-dir', required=True)
    ap.add_argument('--stage', required=True,
                    help='Required non-empty stage id that the report 阶段编号 field '
                         'must carry as an exact token; the call is refused if empty.')
    ap.add_argument('--model', default=DEFAULT_MODEL,
                    help=f'Qoder model to request; defaults to {DEFAULT_MODEL}. An '
                         'explicit alternate model may be passed, but the default never '
                         'falls back to a GLM model.')
    ap.add_argument('--tools', default='', help='Explicit tools, e.g. Read. Empty disables tools.')
    ap.add_argument('--resume-session-id', '--session-id', dest='session_id', default=None,
                    help='Resume a known Qoder session using the official --resume flag.')
    ap.add_argument('--config', default=str(DEFAULT_CONFIG),
                    help='Local entry config JSON with absolute node/qodercli paths.')
    args = ap.parse_args()
    if not args.stage or not args.stage.strip():
        ap.error('--stage is required and must be non-empty')
    cfg = load_entry_config(args.config)
    work = Path(args.workspace).resolve(strict=True)
    prompt_path = Path(args.prompt_file).resolve(strict=True)
    prompt = prompt_path.read_text(encoding='utf-8')
    if not work.is_dir() or not prompt.strip():
        ap.error('Existing workspace and non-empty prompt required')
    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=False)  # refuse overwriting/replaying an existing invocation
    argv = build_argv(cfg, str(work), args.model, args.tools, args.session_id)
    request = {'started_at': datetime.now(timezone.utc).isoformat(),
               'workspace': str(work), 'prompt_file': str(prompt_path),
               'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
               'model_requested': args.model, 'tools': args.tools,
               'stage': args.stage, 'resume_session_id': args.session_id,
               'entry_config': str(Path(args.config).resolve()),
               'runtime': {'node': cfg['node'], 'qodercli': cfg['qodercli']},
               'argv': argv}
    (out / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
    with (out / 'stdout.json').open('wb') as stdout, (out / 'stderr.log').open('wb') as stderr:
        child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr)
        (out / 'process.json').write_text(json.dumps({'pid': child.pid, 'state': 'running'}),
                                          encoding='utf-8')
        child.communicate(input=prompt.encode('utf-8'))
    summary = {'exit_code': child.returncode, 'finished_at': datetime.now(timezone.utc).isoformat(),
               'model_requested': args.model, 'protocol_success': False,
               'business_verified': False, 'output_dir': str(out)}
    report_state = None
    try:
        result = json.loads((out / 'stdout.json').read_text(encoding='utf-8'))
        summary.update(protocol_success=(child.returncode == 0 and result.get('type') == 'result'
                                         and result.get('subtype') == 'success' and result.get('is_error') is False
                                         and result.get('stop_reason') == 'end_turn'),
                       session_id=result.get('session_id'), stop_reason=result.get('stop_reason'),
                       total_credits=result.get('total_credits'), model_usage=result.get('modelUsage'),
                       permission_denials=result.get('permission_denials'))
        response = result.get('result')
        if isinstance(response, str):
            (out / 'response.md').write_bytes(response.encode('utf-8'))
            disk = (out / 'response.md').read_bytes()
            digest = hashlib.sha256(disk).hexdigest()
            summary['response_sha256'] = digest
            readback_match = digest == hashlib.sha256(response.encode('utf-8')).hexdigest()
            body = analyze_report(response, args.stage, str(work))
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
        else:
            report_state = {'bound': False, 'body_ok': False,
                            'reasons': ['envelope result is not a text response'],
                            'carrier_missing': True}
    except (ValueError, OSError) as exc:
        summary['parse_error'] = str(exc)
        report_state = {'bound': False, 'body_ok': False,
                        'reasons': [f'stdout parse error: {exc}'],
                        'carrier_missing': True}
    if report_state is not None:
        report_state.setdefault('protocol_success', summary['protocol_success'])
        report_state.setdefault('session_id', summary.get('session_id'))
        (out / 'report-state.json').write_text(
            json.dumps(report_state, ensure_ascii=False, indent=2), encoding='utf-8')
        summary['report_bound'] = report_state.get('bound', False)
        summary['report_state_file'] = str(out / 'report-state.json')
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding='utf-8')
    (out / 'process.json').write_text(json.dumps({'pid': child.pid, 'state': 'exited',
                                                  'exit_code': child.returncode}),
                                      encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary['protocol_success'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
