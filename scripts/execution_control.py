"""execution_control — brain-worker 任务级控制面（标准库，无常驻调度服务）。

提供三件事，全部是主脑快照式预检，不是原子跨进程锁：

1. `preflight(plan, actual, active_tasks=..., max_concurrency=...)`：把计划 JSON 与
   实际入口选择、工具 grant、cwd 等做精确集合比对。允许的 Edit/原样 Bash/外部只读
   目录必须逐项有对应计划 grant，不得新增未计划 grant，也不得缺 grant；绑定 task_id、
   stage、workspace、model、prompt hash。含并发与共享写入/依赖冲突检查：默认并发 1，
   可显式 4；相同文件在独立 workspace 可并行，共享服务/外部对象写不能隔离即拒绝；
   同 task_id 在途不能改 prompt/重发。所有预检失败都必须在 Popen 之前退出 2。

2. `diagnose(facts)` / `classify_*`：六类 failure_types —— quota_429（必须观测到具体
   429 状态/错误码，quota 类别本身不够）、permission_rule_denied、
   permission_client_missing（从 ZCode 原始 events.jsonl 工具错误提取，即使 summary
   permission_denials=0）、protocol_parse_failure、model_execution_failure、
   test_failure（只来自实际测试退出码）。只检查错误/拒绝/工具失败结构，绝不扫描正常
   prompt/report 里的 429 字样；证据原文、码、reset hint 原样保留；平台/渠道/账号来源
   未知不推断；多类可共存。

3. `run-test` CLI：已登记测试执行器，只跑注册表里的精确 argv，日志固定 workspace/handoff，
   返回真正退出码/stdout/stderr/evidence.json；fingerprint 绑定全部输入文件 bytes + argv +
   cwd + 必要环境。只有相同 fingerprint 且退出 0 且日志 sha 回读一致才可 reuse；禁止
   shell=True、猜命令、覆盖旧证据；输入或日志逃出 workspace 一律拒绝。测试用 unittest，无
   pytest 依赖。

调用方（三执行入口）先自行建好实际 argv，再调用本模块预检，预检 ok 才 Popen。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

FAILURE_TYPES = ('quota_429', 'permission_rule_denied', 'permission_client_missing',
                 'protocol_parse_failure', 'model_execution_failure', 'test_failure')

# 各运行时的真实能力：fine_grained 表示能否表达逐文件 Edit(/原样 Bash()) 规则；
# read_dirs 表示是否有外部只读目录 grant。ZCode 只有整工具开关，不能假称细粒度文件权限。
CAPABILITY = {
    'qoder': {'fine_grained': True, 'read_dirs': True},
    'codebuddy': {'fine_grained': True, 'read_dirs': False},
    'zcode': {'fine_grained': False, 'read_dirs': False},
}

# 控制面五状态（不用第六 verified）：待派发/执行中/卡住/待验收/完成。
TASK_STATES = ('pending', 'executing', 'blocked', 'awaiting_acceptance', 'completed')
# 仍占用 task_id 与并发槽位、门禁不能被绕过的“在途”状态。
HELD_STATES = ('executing', 'awaiting_acceptance', 'blocked')
# 只有 completed 且 acceptance_result=passed 的依赖才可用；failed/cancelled/未知不放行。
DEPENDENCY_PASS_RESULT = 'passed'

PLAN_REQUIRED = ('task_id', 'stage', 'runtime', 'model', 'workspace', 'cwd',
                 'prompt_sha256', 'grants')
GRANT_KEYS = ('edits', 'bash', 'read_dirs')
# 计划与实际必须“双向精确集合相等”的规则/可见性口径（缺计划项或未计划新增都拒）。
PLAN_LIST_KEYS = ('tool_visibility', 'visible_tools', 'allowed_tools', 'disallowed_tools')
# ZCode 工具结果里“缺少 Bash 权限客户端”的原始文案片段（仅匹配错误/工具失败结构）。
CLIENT_MISSING_MARKERS = (
    'No permission client configured for Bash',
    'No permission client configured',
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _norm_dir(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def validate_plan(plan) -> dict:
    """结构校验并补默认值；非法一律抛异常（调用方在 Popen 前退出 2）。"""
    if not isinstance(plan, dict):
        raise ValueError('dispatch plan must be a JSON object')
    missing = [k for k in PLAN_REQUIRED if k not in plan]
    if missing:
        raise ValueError(f'dispatch plan missing keys: {missing}')
    grants = plan['grants']
    if not isinstance(grants, dict):
        raise ValueError('dispatch plan grants must be an object')
    for key in GRANT_KEYS:
        value = grants.get(key, [])
        if not isinstance(value, list) or any(
                not isinstance(x, str) or not x or x != x.strip() for x in value):
            raise ValueError(f'grants.{key} must be a list of non-empty, untrimmed strings')
        grants[key] = value
    runtime = plan['runtime']
    if runtime not in CAPABILITY:
        raise ValueError(f'unknown runtime {runtime!r}; one of {sorted(CAPABILITY)}')
    if not isinstance(plan['prompt_sha256'], str) or len(plan['prompt_sha256']) != 64:
        raise ValueError('prompt_sha256 must be a 64-char hex digest')
    for key in ('depends_on', 'shared_writes'):
        value = plan.get(key, [])
        if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
            raise ValueError(f'{key} must be a list of non-empty strings')
        plan[key] = value
    # 计划与实际须双向精确集合相等的规则/可见性口径。
    for key in PLAN_LIST_KEYS:
        value = plan.get(key, [])
        if not isinstance(value, list) or any(not isinstance(x, str) or not x
                                              or x != x.strip() for x in value):
            raise ValueError(f'{key} must be a list of non-empty, untrimmed strings')
        plan[key] = value
    plan.setdefault('max_concurrency', 1)
    plan.setdefault('isolation', 'independent_workspace')
    if not isinstance(plan['max_concurrency'], int) or plan['max_concurrency'] < 1:
        raise ValueError('max_concurrency must be a positive int')
    # active_tasks 是任务快照数组（可为空数组，表示确认无在途工作）；缺失不等于“没有”。
    if 'active_tasks' not in plan:
        if plan['max_concurrency'] > 1:
            raise ValueError('parallel dispatch (max_concurrency>1) must declare an '
                             'explicit active_tasks snapshot; absence is NOT treated as '
                             '“no in-flight work”')
        plan['active_tasks'] = []
    if not isinstance(plan['active_tasks'], list) or any(
            not isinstance(t, dict) or 'task_id' not in t or 'state' not in t
            for t in plan['active_tasks']):
        raise ValueError('active_tasks must be a list of {task_id, state, ...} objects')
    for task in plan['active_tasks']:
        if task['state'] not in TASK_STATES:
            raise ValueError(f"active_tasks state {task['state']!r} not in {TASK_STATES}")
    return plan


def load_plan(path) -> dict:
    plan = json.loads(Path(path).read_text(encoding='utf-8'))
    return validate_plan(plan)


def plan_hash(plan) -> str:
    canon = json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return _sha256_bytes(canon.encode('utf-8'))


def grants_from_rules(allowed_rules, add_dirs=None, disallowed_rules=None,
                      tools_items=None):
    """把实际 argv 里的规则/目录/可见工具拆成可比对的集合。Edit(...)/Bash(...) 归入
    对应 grant 类别，其余 bare 名称视为工具可见性；同时原样保留完整 allow/deny 规则与
    --tools 可见工具集合，供双向精确集合比对（不能只比“新增”而漏“缺失”）。"""
    allowed_rules = list(allowed_rules or [])
    disallowed_rules = list(disallowed_rules or [])
    tools_items = list(tools_items or [])
    edits = [r for r in allowed_rules if r.startswith('Edit(')]
    bash = [r for r in allowed_rules if r.startswith('Bash(')]
    visibility = [r for r in allowed_rules if not r.startswith(('Edit(', 'Bash('))]
    return {'edits': edits, 'bash': bash,
            'read_dirs': list(add_dirs or []), 'tool_visibility': visibility,
            'allowed_tools': allowed_rules, 'disallowed_tools': disallowed_rules,
            'visible_tools': tools_items}


def _set_diff_reasons(label, planned, applied) -> list:
    """双向精确集合比对：既报未计划新增（权限扩大），也报缺计划项（授权不足）。"""
    planned = set(planned or [])
    applied = set(applied or [])
    reasons = []
    extra = sorted(applied - planned)
    absent = sorted(planned - applied)
    if extra:
        reasons.append(f'{label}: unplanned/expansion (permission widened): {extra}')
    if absent:
        reasons.append(f'{label}: missing planned grant/visibility: {absent}')
    return reasons


def check_parallel(plan, active_tasks, max_concurrency, actual_prompt_sha=None) -> list:
    reasons = []
    held = [t for t in active_tasks if t.get('state') in HELD_STATES]
    if len(held) + 1 > max_concurrency:
        reasons.append(f'concurrency limit {max_concurrency} exceeded '
                       f'({len(held)} task(s) already in flight: '
                       f'{sorted(t.get("task_id") for t in held)})')
    plan_ws = _norm_dir(plan['workspace'])
    plan_shared = set(plan['shared_writes'])
    plan_writes = bool(plan['grants']['edits'] or plan['grants']['bash']
                       or plan.get('visible_tools') and any(
                           v in ('Edit', 'Write', 'Bash')
                           for v in plan.get('visible_tools', [])))
    for task in active_tasks:
        tid = task.get('task_id')
        if tid == plan['task_id'] and task.get('state') in HELD_STATES:
            # 同 task_id 门禁不能被 awaiting_acceptance/blocked 绕过：锁定提示词不可改，
            # 也不可重发。核验“看板(board)记录的 hash”与“本次实际下发 hash”是否一致。
            board_prompt = task.get('prompt_sha256')
            incoming = actual_prompt_sha if actual_prompt_sha is not None \
                else plan['prompt_sha256']
            if board_prompt is not None and incoming != board_prompt:
                reasons.append(f'task_id {tid} is {task.get("state")} with a different '
                               f'board prompt than the dispatched prompt; cannot change '
                               f'the locked prompt or resubmit')
            else:
                reasons.append(f'task_id {tid} already in flight ({task.get("state")}); '
                               f'cannot resubmit')
            continue
        if task.get('state') not in HELD_STATES:
            continue
        t_ws = _norm_dir(task.get('workspace', ''))
        if t_ws == plan_ws and (plan_writes or task.get('writes')):
            reasons.append(f'workspace write conflict with {tid} at {plan["workspace"]}; '
                           f'parallel tasks need independent copies')
        overlap = plan_shared & set(task.get('shared_writes') or [])
        if overlap:
            reasons.append(f'shared external object/service write with {tid}: '
                           f'{sorted(overlap)} cannot be isolated; must serialize')
    # 依赖：五状态里没有 verified；只有 completed 且 acceptance_result=passed 才可用。
    by_id = {t.get('task_id'): t for t in active_tasks}
    for dep in plan['depends_on']:
        match = by_id.get(dep)
        if match is None:
            reasons.append(f'dependency {dep} is unknown; cannot dispatch')
            continue
        if match.get('state') != 'completed':
            reasons.append(f'dependency {dep} is not completed '
                           f'(state {match.get("state")!r})')
        elif match.get('acceptance_result') != DEPENDENCY_PASS_RESULT:
            reasons.append(f'dependency {dep} completed but acceptance_result='
                           f'{match.get("acceptance_result")!r} is not '
                           f'{DEPENDENCY_PASS_RESULT!r}; failed/cancelled/unknown '
                           f'does not unblock')
    return reasons


def preflight(plan, actual, *, active_tasks=None, max_concurrency=None) -> dict:
    """精确集合比对 + 并发/冲突检查。返回 dict，含 ok/reasons/plan_hash/sent。
    这是主脑快照预检，不是原子跨进程锁：调用方在返回 not ok 时必须退出 2、零 Popen。
    active_tasks 若未显式传入，取自 plan['active_tasks']（validate_plan 已要求并行时
    显式声明，缺失不当作“无在途工作”）。"""
    plan = validate_plan(dict(plan))
    if active_tasks is None:
        active_tasks = plan.get('active_tasks') or []
    if max_concurrency is None:
        max_concurrency = plan['max_concurrency']
    reasons = []

    for field in ('task_id', 'stage', 'runtime', 'model', 'workspace', 'cwd'):
        if plan.get(field) != actual.get(field):
            reasons.append(f'{field} mismatch: plan {plan.get(field)!r} vs '
                           f'actual {actual.get(field)!r}')
    if plan.get('prompt_sha256') != actual.get('prompt_sha256'):
        reasons.append('prompt_sha256 mismatch: the dispatched prompt must match the plan '
                       '(in-flight prompt is locked)')

    cap = CAPABILITY[plan['runtime']]
    a_grants = actual.get('grants') or {}
    fine_needed = bool(plan['grants']['edits'] or plan['grants']['bash'])
    if fine_needed and not cap['fine_grained']:
        reasons.append(f"runtime {plan['runtime']} cannot express per-file Edit or "
                       f"verbatim Bash grants; adopt an isolated workspace with "
                       f"tool-visibility only, or refuse")
    if plan['grants']['read_dirs'] and not cap['read_dirs']:
        reasons.append(f"runtime {plan['runtime']} has no external read-only directory "
                       f"grant; refuse")

    # grant 三类双向精确集合相等（缺计划/未计划新增都拒）。
    for key in GRANT_KEYS:
        reasons.extend(_set_diff_reasons(f'grants.{key}', plan['grants'][key],
                                         a_grants.get(key)))
    # 完整规则/可见性口径双向相等：bare 可见性、--tools 可见工具、allow 全量、deny 全量。
    for key in PLAN_LIST_KEYS:
        reasons.extend(_set_diff_reasons(key, plan[key], a_grants.get(key)))

    argv = actual.get('argv')
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        reasons.append('actual argv must be built (list[str]) before preflight')
    if actual.get('shell') is True:
        reasons.append('shell=True is never allowed for dispatch')

    reasons.extend(check_parallel(plan, active_tasks, max_concurrency,
                                  actual_prompt_sha=actual.get('prompt_sha256')))

    ok = not reasons
    return {'ok': ok, 'reasons': reasons, 'will_dispatch': ok, 'sent': False,
            'plan_hash': plan_hash(plan), 'task_id': plan['task_id'],
            'stage': plan['stage'], 'runtime': plan['runtime'],
            'max_concurrency': max_concurrency,
            'active_task_count': len(active_tasks),
            'is_atomic_lock': False,
            'argv_sha256': (_sha256_bytes(json.dumps(argv).encode('utf-8'))
                            if isinstance(argv, list) else None),
            'preflight_at': _now(),
            'note': 'brain snapshot preflight, not an atomic cross-process lock; '
                    'argv is recorded (argv_sha256) for traceability but is not diffed '
                    'against a planned argv — exact-authorization is enforced via the '
                    'grants/visible_tools/allowed_tools/deny set comparison above'}


def preflight_from_plan_file(plan_path, actual, *, active_tasks_path=None,
                             max_concurrency=None) -> dict:
    plan = load_plan(plan_path)
    active_tasks = None
    if active_tasks_path:
        active_tasks = json.loads(Path(active_tasks_path).read_text(encoding='utf-8'))
    return preflight(plan, actual, active_tasks=active_tasks,
                     max_concurrency=max_concurrency)


# 错误文本开头的独立 429 错误码（"429 ..."/"429："/"HTTP 429"）。仅认开头作为独立
# 错误码、随后为分隔/结尾者；不认 id/正文中间的 429 数字，也不以 category=quota 推断。
import re as _re
_LEADING_429_RE = _re.compile(r'^\s*(?:HTTP[\s/]*)?429(?=$|[\s:：，,、；;.。！!])',
                              _re.IGNORECASE)
_RESET_HINT_RE = _re.compile(r'将在[^。，,；;]*?重置')


def _leading_429_text(text) -> bool:
    return isinstance(text, str) and bool(_LEADING_429_RE.match(text))


def explicit_429(errors, errors_info) -> bool:
    """认：错误结构里明确的 status==429 / code==429（dict 字段），或错误文本条目
    **开头**的独立 429 / HTTP 429 码（与原 CodeBuddy 口径一致，含带 reset hint 的
    "429 上游限流，将在…重置"）。只 category=quota、或 id/正文中间的 429 数字一律不算。
    绝不扫描正常 prompt/report 正文里的 429 字样（调用方只把错误/拒绝结构传进来）。"""
    def hit(container):
        if isinstance(container, list):
            for item in container:
                if isinstance(item, dict) and (item.get('status') == 429
                                               or item.get('code') == 429):
                    return True
                if isinstance(item, str) and _leading_429_text(item):
                    return True
        return False
    return hit(errors_info) or hit(errors)


def extract_reset_hint(errors, errors_info):
    """从错误/拒绝结构文本里原样提取“将在 … 重置”窗口；无则 None，不臆造。"""
    pool = []
    for container in (errors, errors_info):
        if isinstance(container, list):
            for item in container:
                if isinstance(item, str):
                    pool.append(item)
                elif isinstance(item, dict):
                    for key in ('details', 'message', 'reason'):
                        val = item.get(key)
                        if isinstance(val, str):
                            pool.append(val)
    for text in pool:
        match = _RESET_HINT_RE.search(text)
        if match:
            return match.group(0)
    return None


def _event_payload(line) -> dict:
    payload = line.get('payload')
    return payload if isinstance(payload, dict) else {}


def permission_client_missing_from_events(events_lines) -> list:
    """从 ZCode 原始 events.jsonl 提取“缺少 Bash 权限客户端”的证据行，即使 summary
    permission_denials=0。识别真实结构：
    - type=permission_resolved, payload.decision=deny, payload.reason 含 marker；
    - type=tool_call_result/tool_result 且 isError/error/result（或顶层 is_error/content）
      显示该 marker。
    只检查权限/工具失败结构；绝不扫描 model_request 携带的历史消息/prompt/report 正文，
    以免把正常文本里的“缺 client”“429”误报。"""
    hits = []
    for line in events_lines:
        if not isinstance(line, dict):
            continue
        etype = line.get('type')
        if etype == 'model_request' or etype is None:
            continue  # 不扫描正常请求/历史文本
        payload = _event_payload(line)
        reason = payload.get('reason')
        if (etype == 'permission_resolved' and payload.get('decision') == 'deny'
                and isinstance(reason, str)
                and any(m in reason for m in CLIENT_MISSING_MARKERS)):
            hits.append(line)
            continue
        if etype in ('tool_call_result', 'tool_result', 'tool_error'):
            texts = []
            if payload.get('isError') or line.get('is_error'):
                for candidate in (payload.get('error'), payload.get('result'),
                                  line.get('content')):
                    if isinstance(candidate, str):
                        texts.append(candidate)
                    elif isinstance(candidate, dict):
                        texts.append(json.dumps(candidate, ensure_ascii=False))
            joined = ' '.join(texts)
            if any(m in joined for m in CLIENT_MISSING_MARKERS):
                hits.append(line)
    return hits


def permission_rule_denied_from_events(events_lines) -> list:
    """从同一批原始事件里提取“权限规则拒绝”（decision=deny 但原因不是缺客户端），
    与缺客户端分类分开；ZCode 规则拒绝也要提取。不扫描 model_request 正文。"""
    hits = []
    for line in events_lines:
        if not isinstance(line, dict):
            continue
        etype = line.get('type')
        if etype == 'model_request' or etype is None:
            continue
        payload = _event_payload(line)
        if (etype == 'permission_resolved' and payload.get('decision') == 'deny'):
            reason = payload.get('reason') or ''
            if not any(m in reason for m in CLIENT_MISSING_MARKERS):
                hits.append(line)
    return hits


def diagnose(facts) -> dict:
    """把调用方从错误/拒绝/工具失败结构中提炼的布尔标记映射为 failure_types。
    绝不扫描正常 prompt/report 文本；不推断平台/渠道/账号来源；账单未知。"""
    types = []
    if facts.get('quota_429'):
        types.append('quota_429')
    if facts.get('permission_rule_denied'):
        types.append('permission_rule_denied')
    if facts.get('permission_client_missing'):
        types.append('permission_client_missing')
    if facts.get('protocol_parse_failure'):
        types.append('protocol_parse_failure')
    if facts.get('model_execution_failure'):
        types.append('model_execution_failure')
    test_exit = facts.get('test_exit_code')
    if isinstance(test_exit, int) and test_exit != 0:
        types.append('test_failure')
    return {'failure_types': types,
            'reset_hint': facts.get('reset_hint'),
            'evidence': facts.get('evidence') or {},
            'billing_basis': 'unknown; raw usage preserved, cost 0/placeholder is not '
                             'evidence of free, cache tokens not re-added',
            'source_attribution': 'platform/channel/account source unknown; not inferred',
            'note': 'report-body format failure is NOT counted as code failure; '
                    'normal prompt/report 429 text is never scanned'}


# ---------------------------------------------------------------- run-test 已登记执行器
def compute_fingerprint(argv, cwd, input_files, env) -> str:
    files = {}
    for path in input_files:
        norm = os.path.normpath(str(path))
        files[norm] = _sha256_bytes(Path(path).read_bytes())
    payload = {'argv': [str(a) for a in argv], 'cwd': _norm_dir(cwd),
               'files': files, 'env': dict(sorted((env or {}).items()))}
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return _sha256_bytes(canon.encode('utf-8'))


def _within(root, target) -> bool:
    root_n = os.path.normcase(os.path.abspath(str(root)))
    target_n = os.path.normcase(os.path.abspath(str(target)))
    return target_n == root_n or target_n.startswith(root_n + os.sep)


def _real_within(root, target) -> bool:
    """用 realpath 解析 symlink/junction 后再判是否在工作区内，防止软链/目录联接越界。"""
    root_n = os.path.normcase(os.path.realpath(str(root)))
    target_n = os.path.normcase(os.path.realpath(str(target)))
    return target_n == root_n or target_n.startswith(root_n + os.sep)


def run_registered_test(registry_path, name, workspace, evidence_root) -> dict:
    """只跑注册表里登记的 argv/cwd/inputs；日志固定写到 workspace/handoff 下；
    证据写 evidence_root/<name>/<fingerprint>/，绝不覆盖旧证据。reuse 条件：相同
    fingerprint + 上次退出 0 + 两份日志都存在且 SHA 回读一致 + argv/cwd/输入哈希等
    元数据吻合。任何越界、空输入、损坏/缺失证据在真正运行前拒绝或不 reuse。"""
    ws_root = os.path.realpath(str(workspace))
    registry = json.loads(Path(registry_path).read_text(encoding='utf-8'))
    runs = registry.get('runs') if isinstance(registry, dict) else None
    if not isinstance(runs, dict) or name not in runs:
        raise ValueError(f'registered test {name!r} not found; only registered argv runs')
    spec = runs[name]
    argv = spec.get('argv')
    if not isinstance(argv, list) \
            or not argv or not all(isinstance(x, str) for x in argv):
        raise ValueError('registered argv must be a non-empty list[str] (no shell)')
    if spec.get('shell') is True:
        raise ValueError('shell=True is refused for registered tests')
    cwd = os.path.realpath(str(spec.get('cwd', ws_root)))
    if not _real_within(ws_root, cwd):
        raise ValueError(f'cwd escapes workspace: {cwd}')
    # 注册 spec 必须声明完整 code/test/fixture 输入（不得空），且都解析在工作区内。
    inputs = spec.get('inputs')
    if not isinstance(inputs, list) or not inputs:
        raise ValueError('registered spec must declare a non-empty inputs list '
                         '(code/test/fixture files) bound into the fingerprint')
    for path in inputs:
        if not _real_within(ws_root, path):
            raise ValueError(f'input escapes workspace: {path}')
        if not Path(path).is_file():
            raise ValueError(f'input file missing: {path}')
    # evidence_root 必须落在真实解析后的工作区内（含 symlink/junction 不能绕）。
    ev_root = os.path.realpath(str(evidence_root))
    if not _real_within(ws_root, ev_root):
        raise ValueError(f'evidence_root escapes workspace: {ev_root}')
    env = spec.get('env') or {}
    if not isinstance(env, dict):
        raise ValueError('registered env must be an object')
    run_env = os.environ.copy()
    run_env.update({str(k): str(v) for k, v in env.items()})
    log_dir = Path(ws_root) / 'handoff' / 'logs' / name
    if not _real_within(ws_root, log_dir):
        raise ValueError(f'log dir escapes workspace: {log_dir}')
    # 继承 env 只保存 hash（不泄明文凭据），并绑定运行环境必要版本口径。
    env_canon = json.dumps(dict(sorted(run_env.items())), sort_keys=True,
                           ensure_ascii=False, separators=(',', ':'))
    env_sha = _sha256_bytes(env_canon.encode('utf-8'))
    runtime_version = '%d.%d.%d' % sys.version_info[:3]

    fingerprint = compute_fingerprint(argv, cwd, inputs,
                                      {'env_sha256': env_sha, 'runtime_version': runtime_version})
    fp_dir = Path(ev_root) / name / fingerprint
    if not _real_within(ev_root, fp_dir):
        raise ValueError('test evidence path escapes evidence_root')
    input_hashes = {os.path.normcase(os.path.realpath(str(p))):
                    _sha256_bytes(Path(p).read_bytes()) for p in inputs}
    # reuse：既有 run 子目录里，证据 JSON 可解析、退出 0、两份日志都存在且 SHA 一致、
    # 且 argv/cwd/输入哈希/环境指纹等元数据与本次一致才 reuse；损坏/缺失都新起 attempt。
    for run_dir in sorted(fp_dir.glob('run-*')) if fp_dir.is_dir() else []:
        prior = run_dir / 'evidence.json'
        stdout_p = run_dir / 'stdout.log'
        stderr_p = run_dir / 'stderr.log'
        if not (prior.is_file() and stdout_p.is_file() and stderr_p.is_file()):
            continue
        try:
            prev = json.loads(prior.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            continue
        meta_ok = (prev.get('fingerprint') == fingerprint
                   and prev.get('argv') == argv
                   and os.path.normcase(os.path.realpath(str(prev.get('cwd')))) == os.path.normcase(cwd)
                   and prev.get('inputs') == input_hashes
                   and prev.get('env_sha256') == env_sha
                   and prev.get('runtime_version') == runtime_version)
        if (meta_ok and prev.get('exit_code') == 0 and prev.get('inputs_unchanged') is True
                and _sha256_bytes(stdout_p.read_bytes()) == prev.get('stdout_sha256')
                and _sha256_bytes(stderr_p.read_bytes()) == prev.get('stderr_sha256')):
            return {'reused': True, 'fingerprint': fingerprint, 'name': name,
                    'exit_code': prev['exit_code'], 'evidence': str(prior),
                    'failure_types': prev.get('failure_types', []),
                    'stdout': stdout_p.read_text(encoding='utf-8'),
                    'stderr': stderr_p.read_text(encoding='utf-8')}

    # 无可 reuse 证据：新建 run 子目录（带序号，绝不覆盖旧证据），日志固定 workspace/handoff。
    log_dir.mkdir(parents=True, exist_ok=True)
    fp_dir.mkdir(parents=True, exist_ok=True)
    seq = len(list(fp_dir.glob('run-*')))
    run_dir = fp_dir / f'run-{seq:06d}-{fingerprint[:8]}'
    if run_dir.exists():
        raise ValueError(f'evidence run dir already exists (refusing to overwrite): '
                         f'{run_dir}')
    run_dir.mkdir(parents=True, exist_ok=False)
    child = subprocess.Popen(argv, cwd=cwd, env=run_env, shell=False,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = child.communicate()
    stdout_text = out.decode('utf-8', 'replace')
    stderr_text = err.decode('utf-8', 'replace')
    (run_dir / 'stdout.log').write_bytes(out)
    (run_dir / 'stderr.log').write_bytes(err)
    (log_dir / f'{run_dir.name}.stdout.log').write_bytes(out)
    (log_dir / f'{run_dir.name}.stderr.log').write_bytes(err)
    diag = diagnose({'test_exit_code': child.returncode})
    inputs_unchanged = all(Path(p).is_file() and _sha256_bytes(Path(p).read_bytes()) == h
                           for p, h in input_hashes.items())
    evidence = {'name': name, 'fingerprint': fingerprint, 'argv': argv, 'cwd': cwd,
                'inputs': input_hashes, 'inputs_unchanged': inputs_unchanged,
                'env_keys': sorted(env.keys()), 'env_sha256': env_sha,
                'runtime_version': runtime_version, 'exit_code': child.returncode,
                'stdout_sha256': _sha256_bytes(out), 'stderr_sha256': _sha256_bytes(err),
                'failure_types': diag['failure_types'], 'ran_at': _now(), 'shell': False}
    (run_dir / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False,
                                                       indent=2), encoding='utf-8')
    return {'reused': False, 'fingerprint': fingerprint, 'name': name,
            'exit_code': child.returncode, 'evidence': str(run_dir / 'evidence.json'),
            'failure_types': diag['failure_types'],
            'stdout': stdout_text, 'stderr': stderr_text}


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description='brain-worker task-level control plane')
    sub = ap.add_subparsers(dest='command', required=True)
    pp = sub.add_parser('preflight', help='plan vs actual exact-set preflight (exit 2 on reject)')
    pp.add_argument('--plan', required=True)
    pp.add_argument('--actual', required=True,
                    help='JSON file describing the actual chosen entry (grants/argv/etc.)')
    pp.add_argument('--active-tasks', dest='active_tasks', default=None)
    pp.add_argument('--max-concurrency', dest='max_concurrency', type=int, default=None)
    rt = sub.add_parser('run-test', help='run a registered test via exact argv')
    rt.add_argument('--registry', required=True)
    rt.add_argument('--name', required=True)
    rt.add_argument('--workspace', required=True)
    rt.add_argument('--evidence-root', dest='evidence_root', required=True)
    return ap


def main(argv=None) -> int:
    ap = build_argparser()
    args = ap.parse_args(argv)
    if args.command == 'preflight':
        actual = json.loads(Path(args.actual).read_text(encoding='utf-8'))
        active_tasks = None
        if args.active_tasks:
            active_tasks = json.loads(Path(args.active_tasks).read_text(encoding='utf-8'))
        try:
            plan = load_plan(args.plan)
        except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({'ok': False, 'reasons': [str(exc)], 'sent': False,
                              'exit_code': 2}, ensure_ascii=False))
            return 2
        result = preflight(plan, actual, active_tasks=active_tasks,
                           max_concurrency=args.max_concurrency)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['ok'] else 2
    if args.command == 'run-test':
        try:
            outcome = run_registered_test(args.registry, args.name, args.workspace,
                                          args.evidence_root)
        except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
            print(json.dumps({'ran': False, 'refused': True, 'reason': str(exc),
                              'exit_code': 2}, ensure_ascii=False))
            return 2
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        return outcome['exit_code']
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
