"""stage_selection —— 只读的“下一阶段派工规划入口”（BW-STAGE-SELECTION-S1/S2；
BW-TASK-PERMISSION-S1 增加可选 permission_grants：规划前按当阶段实际权限需求复用
execution_control.CAPABILITY 做能力过滤，不支持的授权内候选标 permission_incompatible，
不修改 CAPABILITY、不新增授权开关/容量/额度机制；BW-PERMISSION-SCOPE-S1 增加可选
task_scope：普通隔离工程的文件/测试目标清单，只进回执/任务书，与硬合同 permission_grants
分开，绝不驱动能力过滤/授权，也绝不自动降级或改动既有 grants）。

它不是新调度器/服务/数据库表，也**绝不** claim/reserve/select_and_claim/reconcile/
mark/finish 任何 attempt：只以 sqlite `mode=ro` 只读打开**既有** dispatch store，复用
dispatch_pool 的权威方法（`CAPACITY`/`MAIN_FORCE_KEYS`/`OVERFLOW_KEYS`/`capacity_for`/
`_count_active`/`_active_task`/`_workspace_conflict`/`_domestic_active`/
`_zcode_availability_blocked`/`_main_force_limit_active`/`_max_window_blocks`/
`_select_main_force`/`_scope_allowed_candidates`），在主脑生成**精确**入口参数之前给出
一份规划快照 receipt。

快照时间**不等于**当前实时能力；最终正式入口仍原子复核，本快照**绝不**作为占位、
派发或实时成功证明。它不复制公平轮换/日历/额度算法，缺库/缺 schema/不可读快照一律
如实拒绝且**绝不**创建或修改（**不使用 `immutable=1`**——它可能忽略 WAL 中最新在途
记录，不能作为安全快照）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import dispatch_pool as dp  # noqa: E402
import execution_control as ec  # noqa: E402
import codebuddy_fallback as cbf  # noqa: E402
import qoder_permissions as qp  # noqa: E402
import entry_runtime_probe as erp  # noqa: E402  只读 entry 实存可见性（非资格门）

SELECTION_MODES = ('auto', 'fixed', 'executor', 'temporary_fallback')
USER_DIRECT_SELECTION_FIELD = 'user_direct_selection'
EXECUTORS = ('auto', 'qoder', 'zcode', dp.CN_RUNTIME, 'claude', dp.CODEBUDDY_RUNTIME,
             dp.OPENCODE_RUNTIME)
TERMINAL_PREVIOUS_STATES = ('finished', 'start_failed', 'cancelled', 'reconciled_exit',
                            'native_failed', 'failed')
BLOCKING_PREVIOUS_STATES = ('reserved', 'running', 'unknown', 'executing',
                            'awaiting_acceptance')


class Refused(Exception):
    """只读规划阶段的诚实拒绝（缺库/缺 schema/不可读快照等），不是业务裁决。"""

    def __init__(self, code, detail=''):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _pk_parts(pk):
    runtime, model = pk.split(':', 1)
    return {'runtime': runtime, 'model': model, 'pool_key': pk}


def _open_store_ro(store_path):
    """以 sqlite `mode=ro` 打开**既有** store：绝不 mkdir、绝不建库/建表/迁移/WAL，
    **绝不使用 `immutable=1`**（会忽略 WAL 中最新在途记录，不是安全快照）。打开后
    BEGIN 一个只读事务以保持一份一致快照。相对路径先 resolve。"""
    if store_path in (None, '', ':memory:'):
        raise Refused('store_refused', 'a real persistent sqlite file is required')
    path = Path(store_path).resolve()
    if not path.is_file():
        raise Refused('store_refused', f'dispatch store not found (never created): {path}')
    # WAL 存在但 -shm 缺失：只读打开会尝试创建 sidecar（绝不），直接如实拒绝，不用 immutable。
    if Path(str(path) + '-wal').exists() and not Path(str(path) + '-shm').exists():
        raise Refused('store_snapshot_unavailable',
                      'WAL present without -shm; refused rather than creating a sidecar')
    uri = path.as_uri()
    conn = None
    try:
        conn = sqlite3.connect(uri + '?mode=ro', uri=True, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')  # 只读事务，保持一份一致快照
    except (sqlite3.Error, OSError) as exc:
        if conn is not None:      # connect 已成功则先关闭再抛，绝不泄漏只读连接
            conn.close()
        raise Refused('store_snapshot_unavailable', str(exc))
    return conn


def _require_schema(conn, store_path):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempts'"
                    ).fetchone() is None:
        raise Refused('store_refused',
                      f'store {store_path} has no attempts schema (refused, never migrated)')


def _rotation_present(conn) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM rotation WHERE pool_group='main'").fetchone() is not None
    except sqlite3.Error:
        return False


def _validate_scope(scope, task_id, stage):
    """authorization_scope 至少 task_id/task 或 stage；与入参冲突即拒。"""
    if not isinstance(scope, dict):
        return False, 'missing_authorization_scope'
    if not any(scope.get(k) for k in ('task_id', 'task', 'stage')):
        return False, 'missing_authorization_scope'
    for key in ('task_id', 'task'):
        if task_id and scope.get(key) and scope[key] != task_id:
            return False, 'scope_conflict'
    if stage and scope.get('stage') and scope['stage'] != stage:
        return False, 'scope_conflict'
    return True, ''


def _allowed_candidate(pk) -> bool:
    """只认已知真实池；Luna、已退休组合、未知组合绝不因列表文字自动授权。"""
    return (isinstance(pk, str) and pk in dp.CAPACITY
            and pk not in dp.LEGACY_RETIRED_POOL_KEYS and pk != dp.LUNA_KEY)


def _normalize_permission_grants(grants):
    """permission_grants 严格直查（与 authorization 分开，不做通用 schema 框架）：
    复用 execution_control 的 GRANT_KEYS 字典结构（edits/bash/read_dirs 列表）。
    None=本轮未声明需求；三键均可缺省（缺省=空列表），畸形（非对象/未知键/非列表/
    空或带空白项）一律抛错，绝不宽容猜测——工具可见性不算按文件权限，不接受误把
    整工具隔离当精确 grants。"""
    if grants is None:
        return None
    if not isinstance(grants, dict):
        raise ValueError('permission_grants must be a JSON object')
    unknown = [k for k in grants if k not in ec.GRANT_KEYS]
    if unknown:
        raise ValueError(f'permission_grants has unknown keys: {unknown}')
    norm = {}
    for key in ec.GRANT_KEYS:
        value = grants.get(key, [])
        if not isinstance(value, list) or any(
                not isinstance(x, str) or not x or x != x.strip() for x in value):
            raise ValueError(f'permission_grants.{key} must be a list of '
                             'non-empty, untrimmed strings')
        norm[key] = value
    return norm


def _normalize_round_failed(raw):
    """本轮已真实尝试失败的 pool_key 列表（BW-SIMPLE-PRIORITY-RETRY）：由**主脑当前轮**
    在 authorization JSON 里维护的内存入参，只作本轮过滤输入，不落新库/表/服务/定时器，
    下一新轮不传或传空即重新从最高优先级开始。严格校验，绝不猜：
    - None/缺省 → 空列表（新轮，无历史失败）；
    - 裸字符串一律拒绝（防把 'zcode:GLM-5.3' 逐字符当 set）；
    - 必须是非空 pool_key 字符串列表，空白/非字符串元素拒绝；
    - 只过滤本轮，绝不改变完整 allowed_candidates 授权集合，也不因失败项扩权。
    返回归一后的列表副本，绝不突变调用方原对象。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        raise ValueError('round_failed_candidates must be a list of pool_key strings, '
                         'not a bare string (would be split char-by-char)')
    if not isinstance(raw, list):
        raise ValueError('round_failed_candidates must be a JSON list')
    out = []
    for x in raw:
        if not isinstance(x, str) or not x or x != x.strip():
            raise ValueError('round_failed_candidates must contain non-empty, '
                             'untrimmed pool_key strings')
        out.append(x)
    return out


def _normalize_task_scope(scope):
    """task_scope（BW-PERMISSION-SCOPE-S1）：普通已授权隔离工程的文件/测试/只读参考
    目标清单，结构复用 grants 的 edits/bash/read_dirs 字典。与 permission_grants（硬
    合同）**完全分开**：只进回执/任务书，绝不驱动能力过滤/授权集合/fine_grained/
    read_dirs 判定，也绝不自动授权或改动正式 grants；畸形输入严格拒绝。返回归一副本，
    绝不突变调用方原对象。"""
    if scope is None:
        return None
    if not isinstance(scope, dict):
        raise ValueError('task_scope must be a JSON object')
    unknown = [k for k in scope if k not in ec.GRANT_KEYS]
    if unknown:
        raise ValueError(f'task_scope has unknown keys: {unknown}')
    norm = {}
    for key in ec.GRANT_KEYS:
        value = scope.get(key, [])
        if not isinstance(value, list) or any(
                not isinstance(x, str) or not x or x != x.strip() for x in value):
            raise ValueError(f'task_scope.{key} must be a list of '
                             'non-empty, untrimmed strings')
        norm[key] = list(value)
    return norm


def _permission_requirements(grants):
    """从当阶段实际所需 grants 推导能力需求：fine_grained=逐文件 Edit 或原样 Bash；
    read_dirs=外部只读目录 grant。与 execution_control.preflight 同一口径。"""
    if not grants:
        return {'fine_grained': False, 'read_dirs': False}
    return {'fine_grained': bool(grants['edits'] or grants['bash']),
            'read_dirs': bool(grants['read_dirs'])}


def _controlled_file_profile_applicable(grants):
    """本轮是否“仅需精确文件 Edit、无 Bash、无 read_dirs”，可走 ZCode 受控文件 profile。
    与 execution_control._zcode_controlled_file_reasons 同一判定口径，避免规划与正式入口分歧：
    只有纯 Edit（含 Edit(/工作区相对单文件)）且无整目录/Bash 时才成立；混合 Edit+Bash 整段任务
    不适用，ZCode 继续 permission_incompatible，由主脑按用户选择串行拆分。"""
    if not grants:
        return False
    if grants['bash'] or grants['read_dirs']:
        return False
    return bool(grants['edits'])


def _permission_incompatible(pk, requirements, controlled_file_ok=False):
    """复用 execution_control.CAPABILITY（按 pool_key 的 runtime 前缀查表）判能力匹配；
    绝不修改 CAPABILITY 把 ZCode 伪装成 fine_grained=True。无需求时任何候选都兼容
    （维持旧行为：ZCode 正常 eligible）。当且仅当本轮仅需精确 Edit、无 Bash/read_dirs，且
    候选是 ZCode 时，标记可选受控文件 profile（回执 profile 由正式入口独立复核后放行）。"""
    if not (requirements['fine_grained'] or requirements['read_dirs']):
        return False
    cap = ec.CAPABILITY.get(pk.split(':', 1)[0])
    if cap is None:
        return True
    if requirements['read_dirs'] and not cap['read_dirs']:
        return True
    if requirements['fine_grained'] and not cap['fine_grained']:
        runtime = pk.split(':', 1)[0]
        if runtime == 'zcode' and controlled_file_ok:
            return False
        return True
    return False


def _permission_incompatibility_details(filters, requirements):
    """仅对 permission_incompatible 候选说明实际冲突与可选路线（BW-PERMISSION-SCOPE-S1）。
    只按本轮 grants 推导的能力需求描述**真实冲突**（逐文件 Edit/原样 Bash、read_dirs），
    不自动拆阶段/降权限/扩候选；permission_incompatible 是能力冲突，不是模型/认证/额度
    失败，未知可用性不由本字段验证。"""
    details = {}
    for pk, why in filters.items():
        if why != 'permission_incompatible':
            continue
        unsupported = []
        cap = ec.CAPABILITY.get(pk.split(':', 1)[0])
        if cap is None:
            unsupported.append('runtime not in CAPABILITY')
        else:
            if requirements['fine_grained'] and not cap['fine_grained']:
                unsupported.append('per-file edits/verbatim Bash grants')
            if requirements['read_dirs'] and not cap['read_dirs']:
                unsupported.append('external read_dirs grant')
        details[pk] = {
            'unsupported': unsupported,
            'note': ('hard permission_grants remain a binding contract and are not '
                     'weakened by task_scope; ordinary_task_tools route: re-plan as '
                     'an ordinary whole-tool edit+Bash task carrying task_scope only '
                     '(no hard grants) before selecting this runtime; pure Edit '
                     'controlled-file profile is the existing optional strict route '
                     'only when bash/read_dirs are empty; a mixed Edit+Bash hard '
                     'contract is still not directly accepted by this runtime'),
        }
    return details


def _filter_candidates(conn, allowed, zcode_blocked, now, requirements=None,
                       controlled_file_ok=False, round_failed=None,
                       codebuddy_glm_allowed=False, entry_missing=None):
    """逐候选只读过滤，返回 (通过集合, {pk: 拒绝原因})。
    未授权/权限能力/本轮已试/时段/容量 分开记录。
    `permission_incompatible`（能力冲突）与 `not_authorized`（不在授权集合）分别为可辨
    原因，绝不互相冒充。
    BW-SIMPLE-PRIORITY-RETRY-S3 用户政策：普通轮次不再据**历史** ZCode availability
    （`zcode_blocked`）或**明确限额**标记（`main_force_limits`）剔除候选——每轮都从最高
    优先级真实候选起，真实 429/失败才落库保留诊断原件、成功/失败 CAS 结算不变；历史
    标记仅作诊断，不再挡新轮真实尝试。`zcode_blocked` 仍作只读诊断回显在 receipt，
    但不再产生 `zcode_unavailable` 过滤原因。时段/容量/权限能力/明确 model 约束/
    workspace/unknown 在途/审批拒绝**照常**，绝不通过重试绕过。
    BW-UNIFIED-DESKTOP-ENTRY-20261020-R1G（用户 2026-10-20 授权由只读可见性升级）：
    `entry_missing` 是入口运行时**确凿缺失**（探针三态 missing，物理不存在，如整树被
    清）的 pool_key 集合，命中者标 `entry_runtime_missing` 直接挡——这与 S3 的“历史
    可用性/限额不挡新轮”不冲突：那些是**瞬态**状态，缺文件是**物理事实**，重试不会
    让文件回来。探针 `unknown`（无法判定）绝不挡，探针异常整体降级 unknown 后
    fail-open，绝不让可见性探针把规划器搞崩。回执仍保留逐候选可见性字段。
    调用方传入本轮已真实尝试失败的 pool_key 列表 `round_failed_candidates`（内存入参，
    不落新库/新表/新服务）时，本轮命中者标 `already_tried_this_round`，同一 runtime:model
    本轮最多一次；下一新轮调用者不传或传空列表即重新从最高优先级开始，绝不永久挡新轮。
    allowed_candidates 授权集合本身不变，仍完整回显；失败项若本就不在授权集合内只是被
    忽略，绝不因此扩大授权集合。"""
    requirements = requirements or {'fine_grained': False, 'read_dirs': False}
    failed_this_round = set(round_failed or [])
    entry_missing = set(entry_missing or ())
    filters, passed = {}, []
    for pk in allowed:
        if not _allowed_candidate(pk):
            filters[pk] = 'unauthorized'
            continue
        if _permission_incompatible(pk, requirements, controlled_file_ok):
            filters[pk] = 'permission_incompatible'
            continue
        if pk in entry_missing:
            filters[pk] = 'entry_runtime_missing'
            continue
        # BW-LIMIT30-20261019-S3：ZCode/GLM-5.3 与 Claude/deepseek-flash 的最近真实
        # 结构化 429/402 在 1800 秒只读窗口内跳过；恰好 1800 秒及更久按现有优先级重新
        # 可选，不删除原标记/证据、不宣称额度恢复。每个精确池独立。
        if dp._quota_limit_window_active(conn, pk, now):
            filters[pk] = 'main_force_limited'
            continue
        if pk in failed_this_round:
            filters[pk] = 'already_tried_this_round'
            continue
        if dp._max_window_blocks(pk, now):
            filters[pk] = 'window_closed'
            continue
        # BW-WORKER-WIDGET-20261018-S3：两个 CodeBuddy 模型各自独立 1 槽，容量按自身
        # pool_key 独立判断，绝不做跨模型占位互斥。
        if not dp._has_own_capacity(conn, pk):
            filters[pk] = 'capacity_full'
            continue
        passed.append(pk)
    return passed, filters


def _per_candidate_view(allowed, filters):
    """逐候选（全部已知主/兜底池）原因：不在本轮有效授权集合内 / 时段 / 明确限额 /
    容量 / 合格。**只把授权集合内的候选**显示为可用，集合外一律 `not_authorized`，
    绝不把未选范围（别的执行器/地区）显示成 eligible。"""
    view = {}
    aset = set(allowed)
    for pk in tuple(dp.MAIN_FORCE_KEYS) + tuple(dp.OVERFLOW_KEYS):
        if pk not in aset:
            view[pk] = 'not_authorized'
        else:
            view[pk] = filters.get(pk, 'eligible')
    return view


def _relational_select(conn, mode, auth, allowed, passed, now):
    """auto/executor 的关系选择：先主力（复用 `_select_main_force` 公平轮换）再 Flash；
    executor 只在本 runtime 内、绝不跨区；返回 (pk_or_None, reason)。"""
    pset = set(passed)
    ex = str(auth.get('executor')) if mode == 'executor' and auth.get('executor') else None
    mains = [k for k in dp.MAIN_FORCE_KEYS
             if k in allowed and (ex is None or k.startswith(ex + ':'))]
    flashes = [k for k in dp.OVERFLOW_KEYS
               if k in allowed and (ex is None or k.startswith(ex + ':'))]
    elig = [k for k in mains if k in pset]
    if elig:
        if not _rotation_present(conn):
            return None, 'rotation_state_unavailable'
        target, _why = dp._select_main_force(conn, elig[0], now, eligible=set(elig))
        if target is not None:
            if ex:
                return target, 'executor_region_main'
            return target, 'main_force_selected'
    fl = [k for k in flashes if k in pset]
    fl.sort(key=lambda pk: (dp._count_active(conn, pk), pk))
    if fl:
        if ex:
            return fl[0], 'executor_region_flash'
        return fl[0], 'flash_fallback_selected'
    return None, 'no_eligible_candidate'


def plan_stage_selection(authorization, *, previous=None, store_path=None, quota_store=None,
                         task_id=None, stage=None, workspace=None, now=None,
                         permission_grants=None, task_scope=None,
                         round_failed_candidates=None):
    """只读规划：返回最小 receipt；任何冲突整项拒绝（selected=None + refused 原因）。

    `permission_grants`（可选，与 authorization 分开）：当阶段实际所需的最小权限
    （edits/bash/read_dirs，复用 execution_control 的 grants 字典结构）。规划前据此
    推导 fine_grained/read_dirs 能力需求，复用 execution_control.CAPABILITY 对授权
    集合内候选做能力过滤（不支持的标 `permission_incompatible`）；未传或无精确需求
    维持旧兼容行为。auto/executor/fixed/temporary_fallback（同阶段与到期重选）都
    应用**当前**任务需求，绝不从旧 session 继承。"""
    now = now or dp.utcnow()
    auth = authorization if isinstance(authorization, dict) else {}
    mode = auth.get('selection_mode')
    receipt = {
        'preview_only': True, 'sent': False, 'placeholder': False,
        'task_id': task_id, 'stage': stage, 'workspace': workspace,
        'observed_at_utc': dp._iso(now),
        'selection_mode': mode,
        'user_direct_selection': auth.get(USER_DIRECT_SELECTION_FIELD) is True,
        'authorization_quote': auth.get('quote'),
        'authorization_scope': auth.get('authorization_scope'),
        'allowed_candidates': list(auth.get('allowed_candidates') or []),
        'round_failed_candidates': [],
        'effective_round_failed_candidates': [],
        'dispatch_round': None,
        'round_transition': None,
        'codebuddy_quota_evidence': None,
        'codebuddy_glm_allowed': False,
        'codebuddy_glm_condition': None,
        'fallback_scope': auth.get('fallback_scope'),
        'reselection_due': False,
        'session_reuse_allowed': False,
        'permission_requirements': None,
        'selected': None, 'selected_reason': None,
        'selected_priority': None,
        'eligible_candidates': [],
        'excluded_candidates_with_reason': [],
        'higher_priority_exclusions': [],
        'next_candidate': None,
        'qoder_permission_plan': None,
        'candidate_filters': {}, 'zcode_blocked': False, 'refused': None,
        'snapshot_note': ('snapshot time is not live capacity; the authoritative entry '
                          're-checks atomically; this snapshot is never a placeholder, a '
                          'dispatch, or a real-time success proof'),
        'task_scope': None,
        'permission_incompatibility_details': {},
    }

    def _refuse(reason, detail=None):
        receipt['refused'] = reason
        if detail:
            receipt['refused_detail'] = detail
        return receipt

    if not isinstance(authorization, dict):
        return _refuse('missing_authorization')
    # 本轮失败列表 BW-SIMPLE-PRIORITY-RETRY-S3：优先取调用方显式关键字入参，否则从
    # authorization JSON 的 round_failed_candidates 读取（主脑当前轮维护、CLI 经
    # --authorization-json 传入）。严格校验、畸形拒绝、绝不猜；只在授权集合内做本轮
    # 过滤，绝不扩大 allowed_candidates，下一新轮不传即空、重新从最高优先级开始。
    raw_round_failed = (round_failed_candidates
                        if round_failed_candidates is not None
                        else auth.get('round_failed_candidates'))
    try:
        round_failed = _normalize_round_failed(raw_round_failed)
    except ValueError as exc:
        return _refuse('invalid_round_failed_candidates', str(exc))
    receipt['round_failed_candidates'] = list(round_failed)
    receipt['effective_round_failed_candidates'] = list(round_failed)
    # task_scope 严格直查（BW-PERMISSION-SCOPE-S1）：畸形输入拒绝、不猜测；只进回执，
    # 不参与能力过滤/授权，绝不突变调用方原对象，也绝不弱化 permission_grants 硬合同。
    try:
        norm_scope = _normalize_task_scope(task_scope)
    except ValueError as exc:
        return _refuse('invalid_task_scope', str(exc))
    receipt['task_scope'] = norm_scope
    # 权限需求严格直查：畸形输入拒绝、不猜测；与授权对象完全分开。
    try:
        req_grants = _normalize_permission_grants(permission_grants)
    except ValueError as exc:
        return _refuse('invalid_permission_grants', str(exc))
    requirements = _permission_requirements(req_grants)
    receipt['permission_requirements'] = requirements
    # 当轮仅需精确文件 Edit、无 Bash/read_dirs 时，ZCode 可选受控文件 profile；混合 Edit+Bash
    # 整段任务不适用，ZCode 继续 permission_incompatible（由主脑串行拆分）。与正式入口同一口径。
    controlled_file_ok = _controlled_file_profile_applicable(req_grants)
    receipt['controlled_file_profile_required'] = bool(
        requirements['fine_grained'] and controlled_file_ok)
    # 上下文必须齐备，否则无法核对防重/工作区
    if not task_id or not stage or not workspace:
        return _refuse('missing_context', 'task_id/stage/workspace are required')
    if mode not in SELECTION_MODES:
        return _refuse('unknown_selection_mode', repr(mode))
    quote = auth.get('quote')
    if not isinstance(quote, str) or not quote.strip():
        return _refuse('no_source_quote')
    # fixed/executor 会有意绕过 AUTO 优先级，只能来自用户当轮明确选择。普通新任务、
    # “沿用上一轮”、会话复用或主脑方便均必须走 auto；严格要求 JSON true，不能把
    # 其它 truthy 值当授权。temporary_fallback 继续由既有同 task/stage scope 约束。
    if (mode in ('fixed', 'executor')
            and auth.get(USER_DIRECT_SELECTION_FIELD) is not True):
        return _refuse('explicit_user_choice_required')

    allowed = list(auth.get('allowed_candidates') or [])
    if not allowed:
        return _refuse('empty_allowed_candidates')
    for pk in allowed:
        if not _allowed_candidate(pk):
            return _refuse('unauthorized_candidate', repr(pk))

    # BW-LIMIT30-20261019-S3 用户最新明确（覆盖 BW-CODEBUDDY-GLM-FALLBACK-20261018 的旧条件）：
    # CodeBuddy / glm-5.3-flash 与 deepseek-v4.1-flash 额度独立、各自独立 1 槽；规划阶段不再
    # 要求“当轮 DeepSeek 真实限额证据”，也不要求 DeepSeek 本轮先失败。GLM 只要求出现在
    # allowed_candidates 授权集合内（未授权绝不扩权），优先级仍是 ZCode 之后、两区 Flash 之前。
    # 旧 `codebuddy_quota_evidence` 仅作兼容诊断：缺失/畸形不再拒绝规划；历史证据不改写。
    raw_evidence = auth.get('codebuddy_quota_evidence')
    quota_evidence = None
    if isinstance(raw_evidence, str):
        quota_evidence = cbf.verify_quota_evidence(raw_evidence)
    glm_authorized = dp.CODEBUDDY_GLM_POOL_KEY in allowed
    glm_deepseek_failed = dp.CODEBUDDY_POOL_KEY in round_failed
    glm_allowed = bool(glm_authorized)
    # 脱敏回执：只有路径/哈希/原因码/分类，不含任何原始错误文本或请求头。
    receipt['codebuddy_quota_evidence'] = {
        'provided': raw_evidence is not None,
        'required': False,
        'path': (quota_evidence or {}).get('path'),
        'sha256': (quota_evidence or {}).get('sha256'),
        'ok': bool((quota_evidence or {}).get('ok')),
        'reason': ((quota_evidence or {}).get('reason')
                   if isinstance(raw_evidence, str) else
                   ('ignored_non_string_legacy_argument' if raw_evidence is not None else
                    'not_required')),
        'failure_types': list((quota_evidence or {}).get('failure_types') or []),
        'source': 'legacy_diagnostic_only',
    }
    receipt['codebuddy_glm_allowed'] = glm_allowed
    receipt['codebuddy_glm_condition'] = {
        'authorized': glm_authorized,
        'independent_from_deepseek': True,
        'quota_evidence_required': False,
        'deepseek_failed_this_round': glm_deepseek_failed,
        'quota_evidence_ok': bool((quota_evidence or {}).get('ok')),
        'allowed': glm_allowed,
    }

    # 有限字段直接校验（不做通用 schema 框架）
    if mode == 'executor' and auth.get('executor') not in EXECUTORS:
        return _refuse('invalid_executor', repr(auth.get('executor')))
    if mode == 'fixed' and not (auth.get('runtime') and auth.get('model')):
        return _refuse('missing_fixed_combo')

    old_scope = auth.get('authorization_scope')
    old_stage = None
    if mode == 'temporary_fallback':
        # 先识别**真实旧 fallback_scope** 的到期（不改写旧值），旧 temp 的
        # authorization_scope 只对其**原 scope** 核验，不跨 task、不凭 previous 误授权。
        fs = auth.get('fallback_scope')
        # 必须显式带 task_id 且等于本 task；缺 task_id 不得猜授权（不凭 previous/集合蒙混）。
        if (not isinstance(fs, dict) or not fs.get('stage') or not fs.get('task_id')
                or fs.get('task_id') != task_id):
            return _refuse('fallback_scope_mismatch')
        old_stage = fs.get('stage')
        ok_t, why_t = _validate_scope(old_scope, task_id, old_stage)
        if not ok_t:
            return _refuse('fallback_scope_mismatch' if why_t == 'scope_conflict' else why_t)
    else:
        ok, why = _validate_scope(old_scope, task_id, stage)
        if not ok:
            return _refuse(why)

    if previous is not None:
        st = previous.get('state')
        if st in BLOCKING_PREVIOUS_STATES:
            return _refuse('previous_in_flight', f'previous state={st!r}')
        if not previous.get('acceptance_reviewed'):
            return _refuse('previous_awaiting_acceptance')
        if st not in TERMINAL_PREVIOUS_STATES:
            return _refuse('previous_not_terminal', f'previous state={st!r}')

    resolved = store_path or dp.default_store_path()
    try:
        conn = _open_store_ro(resolved)
    except Refused as exc:
        return _refuse(exc.code, exc.detail)
    try:
        _require_schema(conn, resolved)
        if task_id:
            running = dp._active_task(conn, task_id)
            if running is not None:
                return _refuse('task_in_flight',
                               f'token={running["token"]} state={running["state"]}')
        conflict = dp._workspace_conflict(conn, workspace, own_task_id=task_id)
        if conflict is not None:
            return _refuse('workspace_conflict',
                           f'workspace {conflict["workspace"]!r} in-flight from '
                           f'other task {conflict["task_id"]!r}')
        if dp._domestic_active(conn) >= dp.DOMESTIC_TOTAL_CAPACITY:
            return _refuse('domestic_total_full')

        # BW-DISPATCH-ROUND-S1：结构化限额短窗到期且当前没有在途（上面的 task/workspace
        # 门已核验）时，结束旧轮并清空**仅属于旧轮**的失败列表。这样“满 1800 秒恢复
        # 原优先级”不会继续被 already_tried_this_round 永久挡住。这里不清额度记录、
        # 不声称额度已经恢复，也不启动模型；正式入口仍会真实验证。
        round_meta = auth.get('dispatch_round')
        round_id = 1
        round_started = None
        if round_meta is not None:
            if (not isinstance(round_meta, dict)
                    or not isinstance(round_meta.get('id'), int)
                    or round_meta.get('id') < 1
                    or not isinstance(round_meta.get('started_at_utc'), str)):
                return _refuse('invalid_dispatch_round')
            round_id = round_meta['id']
            try:
                round_started = dp._parse(round_meta['started_at_utc'])
            except (TypeError, ValueError):
                return _refuse('invalid_dispatch_round')
            if round_started is None or round_started.tzinfo is None:
                return _refuse('invalid_dispatch_round')
        expired = []
        for failed_pk in round_failed:
            if failed_pk not in set(allowed):
                continue
            limit = dp.quota_limit_window_status(conn, failed_pk, now)
            if limit['state'] != 'expired':
                continue
            expiry = dp._parse(limit['expires_at_utc'])
            # 有显式新轮且起点已经在到期点之后时，不重复滚轮；调用方应保存本回执。
            if round_started is None or round_started < expiry:
                expired.append(limit)
        if expired:
            old_id = round_id
            round_id += 1
            round_failed = []
            receipt['effective_round_failed_candidates'] = []
            receipt['round_transition'] = {
                'from_round': old_id,
                'to_round': round_id,
                'reason': 'structured_limit_window_expired_no_inflight',
                'expired_candidates': expired,
            }
            round_started = now
        elif round_started is None:
            round_started = now
        receipt['dispatch_round'] = {
            'id': round_id,
            'started_at_utc': dp._iso(round_started),
        }

        zcode_blocked = dp._zcode_availability_blocked(quota_store, now=now)
        receipt['zcode_blocked'] = bool(zcode_blocked)

        eff_mode, eff_auth, eff_allowed = mode, auth, allowed
        same_stage_temp = False
        if mode == 'temporary_fallback' and old_stage == stage:
            # 同阶段临时降级：按当阶段明确 runtime+model 的 Flash 选择（仍须授权/容量/
            # 时段/工作区门），**不是永久 fixed**，allowed_candidates 保留基础授权集合。
            same_stage_temp = True
            temp_pk = dp.pool_key(str(auth.get('runtime')), str(auth.get('model')))
            if temp_pk not in dp.OVERFLOW_KEYS:
                return _refuse('temporary_not_flash', temp_pk)
        elif mode == 'temporary_fallback':
            # 新阶段到期：必须有真实匹配的 previous 才允许切换到显式 parent 重裁决。
            receipt['reselection_due'] = True
            if (previous is None or previous.get('task_id') != task_id
                    or previous.get('stage') != old_stage):
                return _refuse('fallback_scope_mismatch',
                               'expired fallback scope without matching previous')
            parent = auth.get('parent_authorization')
            if (not isinstance(parent, dict)
                    or parent.get('selection_mode') not in ('auto', 'fixed', 'executor')
                    or not isinstance(parent.get('quote'), str)
                    or not parent['quote'].strip()):
                return _refuse('parent_authorization_required')
            ok_p, why_p = _validate_scope(parent.get('authorization_scope'),
                                          task_id, stage)
            if not ok_p:
                return _refuse('parent_authorization_required', why_p)
            p_allowed = list(parent.get('allowed_candidates') or [])
            if not p_allowed:
                return _refuse('parent_authorization_required')
            if (parent.get('selection_mode') in ('fixed', 'executor')
                    and parent.get(USER_DIRECT_SELECTION_FIELD) is not True):
                return _refuse('parent_authorization_required',
                               'explicit_user_choice_required')
            eff_allowed = [k for k in p_allowed if k in set(allowed)]  # 交集，不扩权
            if not eff_allowed:
                return _refuse('parent_authorization_required', 'empty intersection with base')
            eff_mode, eff_auth = parent['selection_mode'], parent
            # 有效父授权体现在本轮 receipt；旧 temp 的 quote/scope/mode 另存追溯。
            receipt['selection_mode'] = eff_mode
            receipt['user_direct_selection'] = (
                parent.get(USER_DIRECT_SELECTION_FIELD) is True)
            receipt['authorization_quote'] = parent.get('quote')
            receipt['authorization_scope'] = parent.get('authorization_scope')
            receipt['allowed_candidates'] = list(eff_allowed)
            receipt['source_selection_mode'] = mode
            receipt['source_authorization_quote'] = quote
            receipt['source_authorization_scope'] = old_scope

        # executor 限定用 dp._scope_allowed_candidates 给出的本地区集合交集，绝不跨区。
        if eff_mode == 'executor':
            local = set(dp._scope_allowed_candidates(
                {'executor': eff_auth.get('executor')}, zcode_blocked,
                codebuddy_glm_allowed=glm_allowed))
            eff_allowed = [k for k in eff_allowed if k in local]
            if not eff_allowed:
                return _refuse('no_eligible_candidate')

        if isinstance(receipt.get('codebuddy_glm_condition'), dict):
            receipt['codebuddy_glm_condition']['effective_authorized'] = bool(
                dp.CODEBUDDY_GLM_POOL_KEY in set(eff_allowed))
        # R1G 硬资格门（用户 2026-10-20 授权）：探针在过滤前执行一次；只有确凿
        # missing 进入过滤（unknown 绝不挡）；探针自身任何异常整体降级 unknown
        # （fail-open），绝不让可见性探针把规划器搞崩、也绝不改变授权集合。
        try:
            entry_status, entry_missing = erp.probe_entry_runtimes(eff_allowed)
        except Exception:
            entry_status = {pk: erp.UNKNOWN for pk in eff_allowed}
            entry_missing = []
        passed, filters = _filter_candidates(conn, eff_allowed, zcode_blocked, now,
                                             requirements, controlled_file_ok,
                                             round_failed=round_failed,
                                             codebuddy_glm_allowed=glm_allowed,
                                             entry_missing=set(entry_missing))
        receipt['candidate_filters'] = _per_candidate_view(eff_allowed, filters)
        eligible_rows = []
        excluded_rows = []
        for candidate in eff_allowed:
            priority = dp.priority_for_pool(candidate)
            if candidate in set(passed):
                eligible_rows.append({
                    'pool_key': candidate,
                    'priority': priority,
                    'active': dp._count_active(conn, candidate),
                    'capacity': dp.capacity_for(candidate),
                })
            else:
                excluded_rows.append({
                    'pool_key': candidate,
                    'priority': priority,
                    'reason': filters.get(candidate, 'unknown'),
                })
        eligible_rows.sort(key=lambda row: (
            row['priority'] if row['priority'] is not None else 999,
            row['active'], row['pool_key']))
        excluded_rows.sort(key=lambda row: (
            row['priority'] if row['priority'] is not None else 999,
            row['pool_key']))
        receipt['eligible_candidates'] = eligible_rows
        receipt['excluded_candidates_with_reason'] = excluded_rows
        receipt['permission_incompatibility_details'] = _permission_incompatibility_details(
            receipt['candidate_filters'], requirements)
        # BW-UNIFIED-DESKTOP-ENTRY-20261020 R1→R1G：entry runtime 实存可见性字段保留
        # （过滤前已算好）；硬门在 _filter_candidates 的 entry_runtime_missing 原因里
        # 体现，两者同源同一次探测，绝不影响授权集合回显。
        receipt['entry_runtime_status'] = entry_status
        receipt['entry_runtime_missing'] = entry_missing

        if eff_mode == 'fixed':
            pk = dp.pool_key(str(eff_auth.get('runtime')), str(eff_auth.get('model')))
            if pk not in eff_allowed:
                return _refuse('candidate_not_authorized', pk)
            if pk not in set(passed):
                return _refuse('fixed_candidate_unavailable', filters.get(pk, 'unknown'))
            sel_pk, reason = pk, 'fixed_authorized'
        elif same_stage_temp:
            pk = temp_pk
            if pk not in eff_allowed:
                return _refuse('candidate_not_authorized', pk)
            if pk not in set(passed):
                return _refuse('temporary_candidate_unavailable', filters.get(pk, 'unknown'))
            sel_pk, reason = pk, 'temporary_fallback_selected'
        else:
            sel_pk, reason = _relational_select(conn, eff_mode, eff_auth, eff_allowed,
                                                passed, now)
            if sel_pk is None:
                return _refuse(reason)

        receipt['selected'] = _pk_parts(sel_pk)
        receipt['selected_reason'] = reason
        receipt['selected_priority'] = dp.priority_for_pool(sel_pk)
        receipt['next_candidate'] = next(
            (_pk_parts(row['pool_key']) | {'priority': row['priority']}
             for row in eligible_rows if row['pool_key'] != sel_pk), None)
        if receipt['selected_priority'] is not None:
            receipt['higher_priority_exclusions'] = [
                row for row in excluded_rows
                if row['priority'] is not None
                and row['priority'] < receipt['selected_priority']]
        # AUTO 必须选合格集合中最小层号；否则拒绝而不是静默越级。fixed/executor 是用户
        # 显式限定，不能拿全局 AUTO 层级反向覆盖用户选择。
        if eff_mode == 'auto' and eligible_rows:
            min_priority = min(row['priority'] for row in eligible_rows
                               if row['priority'] is not None)
            if receipt['selected_priority'] != min_priority:
                receipt['selected'] = None
                receipt['selected_reason'] = None
                return _refuse('selection_priority_invariant_failed')
        # task_scope 在首次 Qoder 派发前直接生成同一份精确规则，避免先宽泛试错再补派。
        if receipt['selected']['runtime'] in ('qoder', dp.CN_RUNTIME) and norm_scope:
            try:
                receipt['qoder_permission_plan'] = qp.build_permissions(norm_scope)
            except ValueError as exc:
                receipt['selected'] = None
                receipt['selected_reason'] = None
                return _refuse('qoder_permission_plan_invalid', str(exc))
        prev = previous or {}
        # 仅当 previous 显式属本 task、组合(runtime+model)相同、上轮为真实终态且已独立验收
        # 时才允许复用旧 session；缺 task_id 或来自 OTHER 任务一律不复用（绝不跨 task 继承
        # session/ID）。普通 AUTO/fixed 合法新选择照常返回，只是 session_reuse_allowed=false。
        if (prev.get('task_id') is not None
                and prev.get('task_id') == task_id
                and prev.get('runtime') is not None
                and prev.get('runtime') == receipt['selected']['runtime']
                and prev.get('model') == receipt['selected']['model']
                and prev.get('acceptance_reviewed')
                and prev.get('state') in TERMINAL_PREVIOUS_STATES):
            receipt['session_reuse_allowed'] = True
        return receipt
    except Refused as exc:
        return _refuse(exc.code, exc.detail)
    except (sqlite3.Error, OSError) as exc:
        return _refuse('store_snapshot_unavailable', str(exc))
    finally:
        conn.close()


def _load_json_arg(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise SystemExit(f'invalid JSON argument: {exc}')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog='stage_selection',
        description='read-only next-stage dispatch planner (never claims; preview_only)')
    ap.add_argument('--authorization-json', dest='authorization_json', default=None)
    ap.add_argument('--permission-grants-json', dest='permission_grants_json', default=None)
    ap.add_argument('--task-scope-json', dest='task_scope_json', default=None)
    ap.add_argument('--previous-json', dest='previous_json', default=None)
    ap.add_argument('--task-id', dest='task_id', required=True)
    ap.add_argument('--stage', required=True)
    ap.add_argument('--workspace', required=True)
    ap.add_argument('--store', default=None)
    ap.add_argument('--quota-store', dest='quota_store', default=None)
    args = ap.parse_args(argv)

    receipt = plan_stage_selection(
        _load_json_arg(args.authorization_json),
        previous=_load_json_arg(args.previous_json),
        store_path=args.store, quota_store=args.quota_store,
        task_id=args.task_id, stage=args.stage, workspace=args.workspace,
        permission_grants=_load_json_arg(args.permission_grants_json),
        task_scope=_load_json_arg(args.task_scope_json))
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt.get('selected') is not None else 2


if __name__ == '__main__':
    raise SystemExit(main())
