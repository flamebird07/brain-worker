"""stage_selection —— 只读的“下一阶段派工规划入口”（BW-STAGE-SELECTION-S1/S2）。

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

SELECTION_MODES = ('auto', 'fixed', 'executor', 'temporary_fallback')
EXECUTORS = ('auto', 'qoder', 'zcode', dp.CN_RUNTIME, 'claude')
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


def _filter_candidates(conn, allowed, zcode_blocked, now):
    """逐候选只读过滤，返回 (通过集合, {pk: 拒绝原因})。
    未授权/时段/明确限额/容量 分开记录；ZCode 复用既有 availability 口径。"""
    filters, passed = {}, []
    for pk in allowed:
        if not _allowed_candidate(pk):
            filters[pk] = 'unauthorized'
            continue
        if pk == dp.ZCODE_POOL_KEY and zcode_blocked:
            filters[pk] = 'zcode_unavailable'
            continue
        if dp._max_window_blocks(pk, now):
            filters[pk] = 'window_closed'
            continue
        if dp._main_force_limit_active(conn, pk):
            filters[pk] = 'explicitly_limited'
            continue
        cap = dp.capacity_for(pk)
        if cap is not None and dp._count_active(conn, pk) >= cap:
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
                         task_id=None, stage=None, workspace=None, now=None):
    """只读规划：返回最小 receipt；任何冲突整项拒绝（selected=None + refused 原因）。"""
    now = now or dp.utcnow()
    auth = authorization if isinstance(authorization, dict) else {}
    mode = auth.get('selection_mode')
    receipt = {
        'preview_only': True, 'sent': False, 'placeholder': False,
        'task_id': task_id, 'stage': stage, 'workspace': workspace,
        'observed_at_utc': dp._iso(now),
        'selection_mode': mode,
        'authorization_quote': auth.get('quote'),
        'authorization_scope': auth.get('authorization_scope'),
        'allowed_candidates': list(auth.get('allowed_candidates') or []),
        'fallback_scope': auth.get('fallback_scope'),
        'reselection_due': False,
        'session_reuse_allowed': False,
        'selected': None, 'selected_reason': None,
        'candidate_filters': {}, 'zcode_blocked': False, 'refused': None,
        'snapshot_note': ('snapshot time is not live capacity; the authoritative entry '
                          're-checks atomically; this snapshot is never a placeholder, a '
                          'dispatch, or a real-time success proof'),
    }

    def _refuse(reason, detail=None):
        receipt['refused'] = reason
        if detail:
            receipt['refused_detail'] = detail
        return receipt

    if not isinstance(authorization, dict):
        return _refuse('missing_authorization')
    # 上下文必须齐备，否则无法核对防重/工作区
    if not task_id or not stage or not workspace:
        return _refuse('missing_context', 'task_id/stage/workspace are required')
    if mode not in SELECTION_MODES:
        return _refuse('unknown_selection_mode', repr(mode))
    quote = auth.get('quote')
    if not isinstance(quote, str) or not quote.strip():
        return _refuse('no_source_quote')

    allowed = list(auth.get('allowed_candidates') or [])
    if not allowed:
        return _refuse('empty_allowed_candidates')
    for pk in allowed:
        if not _allowed_candidate(pk):
            return _refuse('unauthorized_candidate', repr(pk))

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
            eff_allowed = [k for k in p_allowed if k in set(allowed)]  # 交集，不扩权
            if not eff_allowed:
                return _refuse('parent_authorization_required', 'empty intersection with base')
            eff_mode, eff_auth = parent['selection_mode'], parent
            # 有效父授权体现在本轮 receipt；旧 temp 的 quote/scope/mode 另存追溯。
            receipt['selection_mode'] = eff_mode
            receipt['authorization_quote'] = parent.get('quote')
            receipt['authorization_scope'] = parent.get('authorization_scope')
            receipt['allowed_candidates'] = list(eff_allowed)
            receipt['source_selection_mode'] = mode
            receipt['source_authorization_quote'] = quote
            receipt['source_authorization_scope'] = old_scope

        # executor 限定用 dp._scope_allowed_candidates 给出的本地区集合交集，绝不跨区。
        if eff_mode == 'executor':
            local = set(dp._scope_allowed_candidates(
                {'executor': eff_auth.get('executor')}, zcode_blocked))
            eff_allowed = [k for k in eff_allowed if k in local]
            if not eff_allowed:
                return _refuse('no_eligible_candidate')

        passed, filters = _filter_candidates(conn, eff_allowed, zcode_blocked, now)
        receipt['candidate_filters'] = _per_candidate_view(eff_allowed, filters)

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
        task_id=args.task_id, stage=args.stage, workspace=args.workspace)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt.get('selected') is not None else 2


if __name__ == '__main__':
    raise SystemExit(main())
