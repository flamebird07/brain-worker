"""Production direct transport for the reactivated standalone CodeBuddy CLI.

BW-CODEBUDDY-MAIN-S3 / BW-CODEBUDDY-GLM-FALLBACK-20261018：这是**唯一**允许对
CodeBuddy 精确模型执行正式 Popen 的生产入口，且只服务两个组合 ——
`codebuddy` + `deepseek-v4.1-flash`（界面名 Deepseek-V4.1-Flash，最高优先主力）；
`codebuddy` + `glm-5.3-flash`（界面名 GLM-5.3-Flash，**仅当轮明确 DeepSeek 限额证据成立**
时的低层补位，与 deepseek 共用同一物理 CodeBuddy 1 槽）。其它 CodeBuddy 旧模型（含其它
旧 GLM 组合）与 WorkBuddy 一律由 `codebuddy_direct.main()` 的 `manual_relay_only` 拒绝路径
在**任何副作用之前**挡下，绝不进入本模块。GLM 还必须在读取用户私有配置、创建 outdir、
容量认领或 Popen 之前，由 `codebuddy_fallback.verify_quota_evidence` 复验当轮 summary.json；
缺证据/普通失败/认证/网络/权限/成功正文里的 429 字样一律拒绝，零副作用。

设计边界（严格对齐任务政策，不新增服务/库/配置系统）：
- 复用 `codebuddy_direct` 已保留的纯函数：`build_argv` / `parse_stream` /
  `effective_permission_rules` / `load_entry_config` / `parse_tools_arg` /
  `validate_windows_scoped_file_rules` / `_cb_failure_facts` / `_tool_failure_stats` 及
  `qoder_direct.analyze_report` / `finalize_binding`（经 `cb` 再导出）。**绝不 import
  测试离线 harness**，也绝不拿其 stub 白名单当生产功能。
- 容量认领只用 `dispatch_pool.consume_for_entry`（executor='codebuddy'，无时段门），
  绑定自身子 PID + 创建身份（`bind_child`），真实确认退出后 `finish` 释放，启动前失败
  `finish(start_failed)` 释放，已启动但无法确认终态则 `mark_unknown` 保留占位；取消/异常
  绝不泄漏或误清别人的锁。绝不抢占/删除旧 attempt，绝不新建 24h 冷却，绝不查余额，绝不
  写假 healthy，绝不跨模型回退。
- 传输契约：`shell=False`；stdin 传完整载荷（九节契约 + 用户原文，不裁剪）；`cwd` 指定
  隔离 workspace；argv 已含 `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`、
  `--settings '{"disableAllHooks":true}'`、`--no-session-persistence`、`--agent cli`
  （MCP 空、hooks 禁用）；子 env 仅为宿主 environ 副本 + `DISABLE_AUTOUPDATER=1`，
  **不注入凭据、不自动登录/升级/子代理、不传 `--fallback-model`、绝不自动指向 WorkBuddy 入口**。
- 结果判定：CLI `exit 0` **不算任务成功**；`protocol_success = parse_stream 协议绿灯 AND
  exit==0`；报告绑定独立记录 `report_bound`；JSON 429/认证按事实失败记录诊断，但**不据此
  写冷却**。原始 stdout/stderr/九节报告原文分别按字节留证；exit/protocol/report_bound/
  工具事件/用量原值分别保留；`business_verified` / `free_quota_verified` /
  `model_backend_identity_verified` 恒为 false（不保证免费或具体后端身份）。
- 时间口径：`request.json` 采集**真实开始与结束**两个时点；显式注入 `now` 的离线用例两个
  时点都取同一注入值以保持确定性，未注入时分别取 Popen 前与 communicate 后的真实 UTC，
  绝不把派工开始时间一直当作 finish 时间（终态释放同样用真实结束时钟）。
- 超时口径：普通新派发**没有**默认墙钟限制（不新增 30 分钟/2 小时上限），只有调用方显式
  给出正数 `timeout_seconds` 才生效；`<=0` 在任何副作用之前诚实拒绝。显式超时仍 kill 子
  进程、回收、按失败结算并保留释放/unknown 保护。
- 工具证据：`execution_evidence` 只公开原始 tool_use/tool_result 数量、成功匹配回执与缺失
  回执，`execution_evidence_ok` 仅作工具证据口径；零工具纯文本任务仍可合法通过传输，
  但格式合格且零工具的“工程报告”不具备工程执行证据，绝不据此宣称业务已验收。
- 退出码：未派发（模型不符/预检/凭据/容量门）→ 2；已派发但失败（协议/绑定/执行）→ 3；
  已派发且 `passed`（协议绿灯 + 报告绑定 + 真实终态释放）→ 0。

离线自测通过注入 `popen_factory`（可移植 synthetic stub）完成，不产生真实 OS 子进程、不联网、
不调真实模型；真实小任务现场验收由主脑用既有私有登录态另行完成。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import dispatch_pool as dp  # 共享原子容量池（同目录，标准库）
import execution_control as ec  # 控制面预检（同目录）
import prompt_contract as pc  # 三入口共享九节契约（同目录）
import codebuddy_direct as cb  # 已保留的纯解析/argv/权限/失败事实函数（同目录）
import codebuddy_fallback as cbf  # 当轮明确 429/额度证据只读复验（同目录）

RUNTIME = 'codebuddy'
MODEL = 'deepseek-v4.1-flash'  # 默认/最高优先模型（界面名 Deepseek-V4.1-Flash）
GLM_MODEL = 'glm-5.3-flash'    # 仅额度条件成立时的低层补位（界面名 GLM-5.3-Flash）
MODELS = (MODEL, GLM_MODEL)
POOL_KEY = dp.pool_key(RUNTIME, MODEL)  # 兼容默认；真实 pool_key 始终按 model 计算
DEFAULT_CONFIG = cb.DEFAULT_CONFIG

# 普通新派发**不设**默认/最长墙钟限制：只有调用方显式给出正数 timeout 才生效；
# <=0 在零副作用阶段诚实拒绝。显式 timeout 仍保留 kill + 回收 + 释放/unknown 保护。


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _evidence_summary(verified) -> dict:
    """把证据复验结果压缩为脱敏回执（路径/哈希/原因码/分类）；绝不回显原始错误文本、
    Cookie、请求头、密钥或令牌。"""
    verified = verified if isinstance(verified, dict) else {}
    return {'provided': bool(verified.get('path')),
            'path': verified.get('path'),
            'sha256': verified.get('sha256'),
            'ok': bool(verified.get('ok')),
            'reason': verified.get('reason'),
            'failure_types': list(verified.get('failure_types') or []),
            'source': 'current_round_summary_json',
            'raw_error_text_included': False}


def execution_evidence(parsed: dict) -> dict:
    """只按原始 tool_use / tool_result 事件给出**工具证据口径**：
    - tool_call_count / tool_result_count：原始调用与结果事件数量（不推断语义）；
    - successful_tool_receipts：有同 ID 结果、is_error 不为 true 且未进 tool_failures 的调用 ID；
    - missing_tool_receipts：有调用但缺同 ID 结果的调用 ID（含空 id，如实记录）；
    - execution_evidence_ok：至少一次调用且每次调用都有成功匹配结果。
    该字段**只**表示工具回执层面有执行证据，不等于业务目标已验收、也不证明真实文件系统结果
    （合成协议桩同样可满足）；零工具纯文本任务的传输仍可合法通过，只是没有工程执行证据。"""
    uses = parsed.get('tool_use_events') or []
    results = parsed.get('tool_results') or []
    result_by_id = {}
    for result in results:
        rid = result.get('tool_use_id')
        if isinstance(rid, str) and rid:
            result_by_id[rid] = result
    failed_ids = {f.get('tool_use_id') for f in (parsed.get('tool_failures') or [])
                  if isinstance(f.get('tool_use_id'), str) and f.get('tool_use_id')}
    successful, missing = [], []
    for use in uses:
        uid = use.get('id')
        result = result_by_id.get(uid) if isinstance(uid, str) and uid else None
        if result is None:
            missing.append(uid)
        elif result.get('is_error') is not True and uid not in failed_ids:
            successful.append(uid)
    return {'tool_call_count': len(uses),
            'tool_result_count': len(results),
            'successful_tool_receipts': successful,
            'missing_tool_receipts': missing,
            'execution_evidence_ok': bool(uses) and not missing
            and len(successful) == len(uses),
            'evidence_scope': ('tool receipts only; not business acceptance and not '
                               'independent file-system truth')}


def build_subprocess_env(environ=None) -> dict:
    """子进程环境：宿主 environ 副本 + DISABLE_AUTOUPDATER=1。绝不注入凭据、绝不改用户全局，
    不触发自动登录/升级。"""
    env = dict(os.environ if environ is None else environ)
    env['DISABLE_AUTOUPDATER'] = '1'
    return env


def _build_actual(*, task_id, stage, model, workspace, prompt_sha256, argv,
                  tools_items, perms):
    """构造 execution_control 预检所需 actual（与 plan 做双向精确集合比对）。runtime 固定
    codebuddy，read_dirs 由 CAPABILITY=False 保证不会带外部只读目录（不谎报能力）。"""
    grants = ec.grants_from_rules(perms['allowed_tools'], add_dirs=[],
                                  disallowed_rules=perms['disallowed_tools'],
                                  tools_items=tools_items)
    return {'task_id': task_id, 'stage': stage, 'runtime': RUNTIME, 'model': model,
            'workspace': str(workspace), 'cwd': str(workspace),
            'prompt_sha256': prompt_sha256, 'argv': list(argv), 'shell': False,
            'grants': grants}


def run(cfg, *, workspace, prompt_file, out_dir, stage, task_id, model, tools_items,
        allowed_tools=None, disallowed_tools=None, session_id=None, prompt_sha256=None,
        claim_token=None, now=None, popen_factory=subprocess.Popen, environ=None,
        dispatch_store=None, child_prober=None, bind_child_pid=True, plan=None,
        active_tasks=None, timeout_seconds=None, codebuddy_quota_evidence=None) -> dict:
    """正式 CodeBuddy 传输：模型硬门 → outdir/argv/权限/预检 → 容量认领 → Popen（绑定子 PID
    + 创建）→ 解析/留证 → 真实终态释放/unknown 保留。任一前置失败零 Popen、零占位泄漏。
    `timeout_seconds=None`（缺省）表示不设墙钟限制；显式值必须为正数，否则在创建目录/认领
    之前诚实拒绝。"""
    clock_injected = now is not None
    now = now or dp.utcnow()
    # 真实 pool_key 始终由本轮 runtime:model 计算，绝不硬编码为 deepseek。
    pool_key = dp.pool_key(RUNTIME, model)

    # (0a) 显式超时只接受正数；<=0 诚实拒绝且零副作用（不建目录、不认领、不 Popen）。
    if timeout_seconds is not None and timeout_seconds <= 0:
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                'reason': 'timeout_out_of_bounds',
                'reasons': [f'timeout_seconds must be a positive number of seconds when '
                            f'explicitly supplied, got {timeout_seconds!r}; omit it for no '
                            'wall-clock limit (no silent default is applied)']}

    # (0) 模型硬门：只服务两个精确模型，其它一律在**任何副作用之前**拒绝（不建目录、不认领、
    #     不 Popen）。生产正式 main 只为这两个精确 model 开放。
    if model not in MODELS:
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                'manual_relay_only': True, 'reason': 'cli_model_rejected',
                'reasons': [f'--model must be one of {MODELS!r} on this CodeBuddy live '
                            f'entry, got {model!r}; no other CodeBuddy model / WorkBuddy / '
                            'fallback is reactivated here (zero side effects before '
                            'dispatch)']}

    # (0b) GLM 额度条件：必须在 outdir/配置/认领/Popen 之前复验当轮 summary.json。claim token
    #      不是证明；普通网络/认证/权限/容量失败、旧轮证据、成功正文含 429 字样均不通过。
    #      DeepSeek（默认）调用完全不受该参数影响。
    glm_allowed = False
    quota_evidence = None
    if model == GLM_MODEL:
        quota_evidence = cbf.verify_quota_evidence(codebuddy_quota_evidence)
        if not quota_evidence.get('ok'):
            return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                    'reason': 'codebuddy_quota_condition_not_met',
                    'codebuddy_quota_evidence': _evidence_summary(quota_evidence),
                    'reasons': ['CodeBuddy GLM-5.3-Flash is a quota-conditional low-tier '
                                "backup: the current round's codebuddy:"
                                'deepseek-v4.1-flash summary.json must show a terminal '
                                'error failure envelope with explicit 429/quota evidence '
                                f'(verification reason: {quota_evidence.get("reason")!r}); '
                                'no config read, no outdir, no claim and no Popen are '
                                'performed']}
        glm_allowed = True

    # (1) outdir 必须不存在（存在即 0 Popen，绝不覆写旧证据）。
    if Path(out_dir).exists():
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                'reason': 'output_dir_exists',
                'reasons': [f'output dir {str(out_dir)!r} already exists; refusing to '
                            'overwrite prior evidence (choose a fresh dir)']}

    # (2) 提示词/argv/权限/Windows scoped 校验（全部在建目录与认领之前，0 Popen、0 占位）。
    try:
        prompt_bytes = Path(prompt_file).read_bytes()
        actual_prompt_sha = _sha(prompt_bytes)
        if prompt_sha256 and str(prompt_sha256).lower() != actual_prompt_sha:
            raise ValueError(f'prompt sha256 recheck failed: file hashes {actual_prompt_sha} '
                             f'but caller asserted {prompt_sha256!r}')
        task_text = prompt_bytes.decode('utf-8')
        perms = cb.effective_permission_rules(tools_items, allowed_tools, disallowed_tools)
        problems = cb.validate_windows_scoped_file_rules(
            perms['allowed_tools'], perms['disallowed_tools'], str(workspace))
        if problems:
            raise ValueError('invalid Windows scoped file grant: ' + ' | '.join(problems))
        argv = cb.build_argv(cfg, str(workspace), model, tools_items, session_id,
                             allowed_tools=allowed_tools, disallowed_tools=disallowed_tools)
        if session_id:
            raise ValueError('session resume is not accepted on the reactivated CodeBuddy '
                             'live entry (continuity is not an authorization)')
    except (ValueError, KeyError, OSError, UnicodeDecodeError) as exc:
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                'reason': 'precheck_failed', 'error_class': type(exc).__name__,
                'reasons': [f'argv/prompt/permission precheck failed before any claim: {exc!r}']}

    # (3) --dispatch-plan 精确预检（占位之前，0 Popen）。
    if plan is not None:
        try:
            actual = _build_actual(task_id=task_id, stage=stage, model=model,
                                   workspace=workspace, prompt_sha256=actual_prompt_sha,
                                   argv=argv, tools_items=tools_items, perms=perms)
            pf = ec.preflight(plan, actual, active_tasks=active_tasks)
        except Exception as exc:  # noqa: BLE001
            return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                    'reason': 'dispatch_plan_invalid', 'error_class': type(exc).__name__,
                    'reasons': [f'dispatch plan validation failed: {exc!r}']}
        if not pf.get('ok'):
            return {'dispatch_failed': True, 'sent': False, 'dispatched': False,
                    'reason': 'dispatch_plan_rejected', 'preflight': pf,
                    'reasons': pf.get('reasons') or []}

    # (4) 容量认领：executor='codebuddy'，精确组合，无时段门。占位发生在 consume_for_entry 内。
    gate = dp.consume_for_entry(
        dispatch_store, task_id=task_id, runtime=RUNTIME, model=model,
        workspace=str(workspace), prompt_sha256=actual_prompt_sha, stage=stage,
        claim_token=claim_token, now=now, executor=RUNTIME,
        codebuddy_glm_allowed=glm_allowed)
    if not gate.get('allowed'):
        return {'dispatch_failed': True, 'sent': False, 'dispatched': False, 'gate': gate}
    claim = gate['token']

    # 载荷：九节契约前置 + 用户原文，整体进 stdin（完整，不裁剪）。
    # 本轮身份来自入口真实 runtime 与本轮请求模型（不读旧提示词推断）。
    contract_text = pc.build_contract(stage, str(workspace), runtime=RUNTIME,
                                      model=model)
    sent_payload = pc.compose_task_payload(contract_text, task_text).encode('utf-8')

    child_env = build_subprocess_env(environ)

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
    try:
        Path(out_dir).mkdir(parents=True, exist_ok=False)
        # 真实开始点：Popen 之前采集；显式注入时钟时用注入值保持离线确定性。
        started_at_utc = now if clock_injected else datetime.now(timezone.utc)
        proc = popen_factory(argv, cwd=str(workspace), stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=child_env)
        started = True
        dp.mark_running(dispatch_store, claim, wrapper_pid=os.getpid(), now=started_at_utc)
        if bind_child_pid:
            bound = None
            if getattr(proc, 'pid', None) is not None:
                bound = dp.bind_child(dispatch_store, claim, int(proc.pid),
                                      wrapper_pid=os.getpid(), now=started_at_utc)
            child_verified = bool(isinstance(bound, dict) and bound.get('bound') is True)
            if not child_verified:
                # 绑定未明确成功：绝不写 stdin、不杀进程、不假释放；保留活/未知占位供核查。
                try:
                    unknown_guard = dp.mark_unknown(dispatch_store, claim, now=started_at_utc)
                except Exception as uexc:  # noqa: BLE001
                    unknown_guard = {'marked': False, 'error': type(uexc).__name__}
                return {'dispatch_failed': False, 'sent': False, 'dispatched': True,
                        'launch_unknown': True, 'started': True, 'token': claim,
                        'child_verified': False, 'bind_required': True,
                        'unknown_guard': unknown_guard, 'business_verified': False}
        try:
            # 未显式给 timeout → None（无墙钟限制）；显式正数才真正生效。
            stdout_bytes, stderr_bytes = proc.communicate(input=sent_payload,
                                                          timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            # 有界墙钟超时：kill 后回收，按失败结算，绝不写 healthy、不写冷却。
            timed_out = True
            proc.kill()
            stdout_bytes, stderr_bytes = proc.communicate()
        exit_code = getattr(proc, 'returncode', None)
        # 真实结束点：communicate 返回后采集，绝不沿用派工开始的 now。
        finished_at_utc = now if clock_injected else datetime.now(timezone.utc)
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
                        'unknown_guard': unknown_guard,
                        'error_class': type(exc).__name__, 'business_verified': False}
            exit_code = getattr(proc, 'returncode', None) if exit_code is None else exit_code
        else:
            try:
                start_release = dp.finish(dispatch_store, claim, terminal='start_failed',
                                          now=now, wrapper_pid=os.getpid())
            except Exception as fexc:  # noqa: BLE001
                start_release = {'released': False, 'error': type(fexc).__name__}
            return {'dispatch_failed': False, 'sent': False, 'dispatched': False,
                    'started': False, 'token': claim,
                    'capacity_release': start_release,
                    'error_class': type(exc).__name__, 'business_verified': False}

    # 已确认真实退出：解析/留证/终态释放全部进受保护流程，释放无条件执行、保留真实结果。
    ec_exit = exit_code if exit_code is not None else -1
    out = Path(out_dir)
    facts = {}
    binding = {}
    report_text = ''
    parsed = {}
    ev_files = {}
    evidence = {}
    try:
        (out / 'stdout.jsonl').write_bytes(stdout_bytes)
        (out / 'stderr.log').write_bytes(stderr_bytes)
        (out / 'sent-payload-stdin.bin').write_bytes(sent_payload)
        parsed = cb.parse_stream(stdout_bytes, model, tools_items)
        protocol_success = bool(parsed['protocol_success'] and ec_exit == 0)
        if ec_exit != 0:
            parsed['parse_errors'].append(f'process exited non-zero: {ec_exit}')
        if timed_out:
            parsed['parse_errors'].append(
                f'bounded child wall-clock timeout after {timeout_seconds}s (settled as '
                'failure, never healthy)')
        failure_facts = cb._cb_failure_facts(parsed, ec_exit)
        evidence = execution_evidence(parsed)
        report_text = parsed['response_text'] or ''
        if report_text:
            (out / 'response.md').write_bytes(parsed['response_text'].encode('utf-8'))
            disk = (out / 'response.md').read_bytes()
            readback_match = (_sha(disk)
                              == _sha(parsed['response_text'].encode('utf-8')))
            body = cb.analyze_report(parsed['response_text'], stage, str(workspace))
            binding = cb.finalize_binding(
                body['body_ok'], body.get('reasons'),
                protocol_success=protocol_success,
                session_id=parsed['session_id'],
                requested_session_id=None,
                readback_match=bool(readback_match))
            report_state = {**body, 'readback_match': readback_match,
                            'protocol_success': protocol_success,
                            'session_id': parsed['session_id'],
                            'binding': binding, 'bound': binding['bound'],
                            'reasons': binding['reasons']}
        else:
            binding = {'bound': False, 'reasons': ['no parseable text result in stream']}
            report_state = {'bound': False, 'protocol_success': protocol_success,
                            'parse_success': parsed.get('parse_success'),
                            'failure_envelope_valid': parsed.get('failure_envelope_valid'),
                            'primary_failure': parsed.get('primary_failure'),
                            'reasons': ['no final report carrier',
                                        *parsed['parse_errors']],
                            'carrier_missing': True}
        (out / 'report-state.json').write_bytes(json.dumps(
            report_state, ensure_ascii=False, indent=2).encode('utf-8'))
        summary = {
            'runtime': RUNTIME, 'model': model, 'pool_key': pool_key,
            'codebuddy_glm_allowed': glm_allowed,
            'codebuddy_quota_evidence': (_evidence_summary(quota_evidence)
                                         if quota_evidence else None),
            'exit_code': exit_code, 'protocol_success': protocol_success,
            'report_bound': bool(report_state.get('bound', False)),
            'timed_out': timed_out, 'session_id': parsed['session_id'],
            'observed_models': parsed['observed_models'],
            'observed_tool_calls': parsed['observed_tool_calls'],
            'tool_use_events': parsed.get('tool_use_events'),
            'usage': parsed['usage'], 'model_usage': parsed['model_usage'],
            'permission_denials': parsed['permission_denials'],
            'parse_errors': parsed['parse_errors'],
            'terminal_state': parsed.get('terminal_state'),
            'failure_envelope_valid': parsed.get('failure_envelope_valid'),
            'primary_failure': parsed.get('primary_failure'),
            'tool_failure_stats': cb._tool_failure_stats(parsed.get('tool_failures')),
            'execution_evidence': evidence,
            'diagnostics': ec.diagnose(failure_facts),
            'business_verified': False, 'free_quota_verified': False,
            'model_backend_identity_verified': False,
            'usage_billing_basis': 'unknown; raw usage saved without interpretation',
            'output_dir': str(out)}
        (out / 'summary.json').write_bytes(json.dumps(
            summary, ensure_ascii=False, indent=2).encode('utf-8'))
        (out / 'request.json').write_bytes(json.dumps(
            {'started_at_utc': started_at_utc.isoformat(),
             'finished_at_utc': finished_at_utc.isoformat(),
             'workspace': str(workspace), 'argv': argv, 'cwd': str(workspace),
             'runtime': RUNTIME, 'model': model, 'pool_key': pool_key,
             'codebuddy_glm_allowed': glm_allowed,
             'codebuddy_quota_evidence': (_evidence_summary(quota_evidence)
                                          if quota_evidence else None),
             'task_id': task_id, 'stage': stage, 'tools_items': tools_items,
             'allowed_tools': list(perms['allowed_tools']),
             'disallowed_tools': list(perms['disallowed_tools']),
             'prompt_sha256': actual_prompt_sha, 'shell': False,
             'timeout_seconds': timeout_seconds,
             'executor_identity': {
                 'runtime': RUNTIME, 'request_model': model,
                 'source': 'entry runtime + requested model; not a verified backend '
                           'identity',
                 'older_identity_does_not_override': True},
             'contract_sha256': _sha(contract_text.encode('utf-8')),
             'contract_sha256_caliber': 'utf-8 bytes of the contract prepended to stdin',
             'entry_config': {'node': cfg.get('node'), 'cli': cfg.get('cli')},
             'no_fallback_model': True},
            ensure_ascii=False, indent=2).encode('utf-8'))
        ev_files = {'stdout_jsonl': str(out / 'stdout.jsonl'),
                    'stderr_log': str(out / 'stderr.log'),
                    'response_md': str(out / 'response.md') if report_text else None,
                    'report_state': str(out / 'report-state.json'),
                    'summary_json': str(out / 'summary.json'),
                    'request_json': str(out / 'request.json')}
    except Exception as exc:  # noqa: BLE001 - 解析/落盘失败不外泄；终态仍按真实退出释放
        facts = dict(facts, downstream_error=type(exc).__name__)

    execution_success = bool(parsed.get('protocol_success') and ec_exit == 0 and not timed_out)
    success = bool(execution_success and binding.get('bound'))
    try:
        release = dp.finish(dispatch_store, claim, terminal='finished', success=success,
                            now=finished_at_utc, prober=child_prober)
    except Exception as fexc:  # noqa: BLE001
        release = {'released': False, 'error': type(fexc).__name__}
    bind_ok = child_verified if bind_child_pid else True
    release_ok = bool(isinstance(release, dict) and release.get('released') is True)
    passed = bool(success and not facts.get('downstream_error') and bind_ok and release_ok)
    return {'sent': True, 'dispatched': True, 'started': started,
            'child_verified': child_verified, 'token': claim, 'exit_code': exit_code,
            'pool_key': pool_key, 'codebuddy_glm_allowed': glm_allowed,
            'codebuddy_quota_evidence': (_evidence_summary(quota_evidence)
                                         if quota_evidence else None),
            'protocol_success': bool(parsed.get('protocol_success') and ec_exit == 0),
            'report_bound': bool(binding.get('bound')), 'timed_out': timed_out,
            'evidence': ev_files, 'launched': True, 'capacity_release': release,
            'execution_evidence': evidence,
            'started_at_utc': started_at_utc.isoformat(),
            'finished_at_utc': finished_at_utc.isoformat(),
            'passed': passed, 'business_verified': False,
            'usage': parsed.get('usage'), 'model_usage': parsed.get('model_usage'),
            'observed_models': parsed.get('observed_models'),
            'report_text_sha256': _sha(report_text.encode('utf-8')) if report_text else None}


def build_parser():
    ap = argparse.ArgumentParser(prog='codebuddy_live')
    ap.add_argument('--workspace', required=True)
    ap.add_argument('--prompt-file', required=True, dest='prompt_file')
    ap.add_argument('--output-dir', required=True, dest='output_dir')
    ap.add_argument('--stage', required=True)
    ap.add_argument('--task-id', required=True, dest='task_id')
    ap.add_argument('--model', default=MODEL,
                    help=f'must be one of the two exact reactivated models {MODELS}; no '
                         'fallback, no other CodeBuddy model, never WorkBuddy. '
                         f'{GLM_MODEL} additionally requires '
                         '--codebuddy-quota-evidence.')
    ap.add_argument('--codebuddy-quota-evidence', dest='codebuddy_quota_evidence',
                    default=None,
                    help='Absolute path to the CURRENT round summary.json with explicit '
                         'terminal 429/quota evidence for codebuddy:deepseek-v4.1-flash. '
                         'Only used for --model glm-5.3-flash; DeepSeek calls ignore it. '
                         'Missing/invalid evidence refuses GLM before any config read, '
                         'outdir, claim or Popen.')
    ap.add_argument('--config', default=str(DEFAULT_CONFIG),
                    help='entry config JSON with absolute node/cli (user-side; never '
                         'commit copies of credentials)')
    ap.add_argument('--tools', default='',
                    help='Comma list limited to Read,Write,Edit,Bash,Glob,Grep; empty '
                         'means zero tool visibility.')
    ap.add_argument('--allowed-tools', dest='allowed_tools', action='append', default=None)
    ap.add_argument('--disallowed-tools', dest='disallowed_tools', action='append',
                    default=None)
    ap.add_argument('--resume-session-id', dest='session_id', default=None,
                    help='Rejected on this entry (continuity is not an authorization).')
    ap.add_argument('--dispatch-claim', dest='dispatch_claim', default=None,
                    help='Pre-reserved dispatch_pool claim token to consume atomically.')
    ap.add_argument('--dispatch-store', dest='dispatch_store', default=None,
                    help='dispatch_pool sqlite store path (default from env/home).')
    ap.add_argument('--dispatch-plan', dest='dispatch_plan', default=None,
                    help='execution_control dispatch plan JSON (precise preflight before '
                         'any claim/Popen).')
    ap.add_argument('--active-tasks', dest='active_tasks', default=None,
                    help='JSON file of the active-task snapshot for the plan preflight.')
    ap.add_argument('--timeout-seconds', dest='timeout_seconds', type=int,
                    default=None,
                    help='Optional explicit positive wall-clock timeout for the child in '
                         'seconds. Omitted means no wall-clock limit is added for an '
                         'ordinary new dispatch; zero/negative values are honestly '
                         'refused. An explicit timeout still kills, reaps and settles the '
                         'child as a failure, never healthy.')
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if str(args.model) not in MODELS:
        print(json.dumps({'dispatch_failed': True, 'sent': False, 'dispatched': False,
                          'manual_relay_only': True, 'reason': 'cli_model_rejected',
                          'reasons': [f'--model must be one of {MODELS!r} on this entry, '
                                      f'got {args.model!r}; no model substitution, no '
                                      'WorkBuddy']},
                         ensure_ascii=False))
        return 2
    if args.timeout_seconds is not None and args.timeout_seconds <= 0:
        print(json.dumps({'dispatch_failed': True, 'sent': False,
                          'dispatched': False,
                          'reason': 'timeout_out_of_bounds',
                          'reasons': [f'--timeout-seconds must be positive when supplied, '
                                      f'got {args.timeout_seconds!r}; omit it for no '
                                      'wall-clock limit (no silent default is applied)']},
                         ensure_ascii=False))
        return 2
    # GLM：在读取用户私有配置之前先复验当轮明确限额证据；不通过则零配置读取、零 outdir、
    # 零认领、零 Popen。run() 内部还会在真正启动前再次复验（claim token 不是证明）。
    if str(args.model) == GLM_MODEL:
        early_evidence = cbf.verify_quota_evidence(args.codebuddy_quota_evidence)
        if not early_evidence.get('ok'):
            print(json.dumps({'dispatch_failed': True, 'sent': False, 'dispatched': False,
                              'reason': 'codebuddy_quota_condition_not_met',
                              'codebuddy_quota_evidence': _evidence_summary(early_evidence),
                              'reasons': ['GLM-5.3-Flash requires current-round explicit '
                                          'DeepSeek quota evidence; verification refused '
                                          f'with reason {early_evidence.get("reason")!r} '
                                          'before reading the private entry config']},
                             ensure_ascii=False))
            return 2
    cfg = cb.load_entry_config(args.config)
    tools_items = cb.parse_tools_arg(args.tools)
    plan = None
    active_tasks = None
    if args.dispatch_plan:
        plan = json.loads(Path(args.dispatch_plan).read_text(encoding='utf-8'))
        if args.active_tasks:
            active_tasks = json.loads(Path(args.active_tasks).read_text(encoding='utf-8'))
    prompt_bytes = Path(args.prompt_file).read_bytes()
    # 缺省 None：普通新派发不加墙钟限制；只有显式正数才交给 run 生效。
    timeout_seconds = args.timeout_seconds
    out = run(cfg, workspace=args.workspace, prompt_file=Path(args.prompt_file),
              out_dir=Path(args.output_dir), stage=args.stage, task_id=args.task_id,
              model=args.model, tools_items=tools_items,
              allowed_tools=args.allowed_tools, disallowed_tools=args.disallowed_tools,
              session_id=args.session_id, prompt_sha256=_sha(prompt_bytes),
              claim_token=args.dispatch_claim, dispatch_store=args.dispatch_store,
              plan=plan, active_tasks=active_tasks, timeout_seconds=timeout_seconds,
              codebuddy_quota_evidence=args.codebuddy_quota_evidence)
    print(json.dumps(out, ensure_ascii=False))
    if not out.get('dispatched'):
        return 2
    return 0 if out.get('passed') else 3


if __name__ == '__main__':
    raise SystemExit(main())
