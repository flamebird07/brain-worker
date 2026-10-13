"""stage_selection 只读规划入口的离线回归（BW-STAGE-SELECTION-S1/S2）。

只用临时 SQLite（每用例独立 store，经既有 `dispatch_pool.connect` 建库与
`_rotation_row` 建 rotation 行）、mock 掉 ZCode availability 只读回核
（`dp._zcode_availability_blocked`），绝不真实模型 / 真实共享库 / 凭据 / 余额。
断言规划入口只读（读写前后文件哈希不变）、缺库/缺 schema/不可读快照不创建、冲突整项
拒绝、退出码 0/2。S2 补：真实旧 scope 到期重选、同阶段按指定 Flash、父授权交集约束、
跨 task/无验收/在途不重选、缺 parent、CN 不跨区、WAL 活 writer 可见、上下文缺项。
BW-TASK-PERMISSION-S1 补：派工规划前的任务权限需求（可选 permission_grants，复用
execution_control grants 字典结构与 CAPABILITY 能力表）过滤——精确 Edit/Bash/read_dirs
需求下不支持的授权内候选标 permission_incompatible（与 not_authorized/zcode_unavailable
可辨）、fixed 精确要求诚实拒绝、无需求维持旧兼容行为、临时降级/父授权重选应用当前需求、
畸形输入拒绝、CLI 最小新输入。
"""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_pool as dp  # noqa: E402
import execution_control as ec  # noqa: E402
import stage_selection as ss  # noqa: E402

T_NIGHT = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)  # 北京 23:00（主力窗口内）
T_DAY = datetime(2026, 10, 8, 2, 0, 0, tzinfo=timezone.utc)     # 北京 10:00（工作日高峰）

ALL_MAINS = list(dp.MAIN_FORCE_KEYS)
ALL_FLASH = list(dp.OVERFLOW_KEYS)
CN_MAINS = [k for k in ALL_MAINS if k.startswith('qodercn:')]
CN_FLASH = 'qodercn:Qwen3.8-Flash'


def _patch_zcode(available=True):
    return unittest.mock.patch.object(
        dp, '_zcode_availability_blocked',
        lambda *a, **k: None if available else {'blocked': True, 'reasons': ['mock']})


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-stage-sel-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        self.saved_env = os.environ.pop('BRAIN_WORKER_DISPATCH_STORE', None)
        self._zpatch = _patch_zcode(True)
        self._zpatch.start()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._zpatch.stop)

    def tearDown(self):
        if self.saved_env is not None:
            os.environ['BRAIN_WORKER_DISPATCH_STORE'] = self.saved_env

    def _ws(self, name):
        d = self.base / 'ws' / str(name)
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def _normalize(self):
        """把临时库从 WAL 归到 delete 模式，使只读 mode=ro 打开无需 -shm（测试确定性）。"""
        with closing(dp.connect(self.store)) as conn:
            conn.execute('PRAGMA journal_mode=DELETE')

    def build_store(self):
        with closing(dp.connect(self.store)) as conn:
            dp._rotation_row(conn)
            conn.execute('PRAGMA journal_mode=DELETE')
        return self.store

    def reserve(self, task, runtime, model, ws=None, executor='auto'):
        r = dp.reserve(self.store, task_id=task, runtime=runtime, model=model,
                       workspace=ws or self._ws(task), prompt_sha256='p',
                       executor=executor, now=T_NIGHT, _preclaim=False)
        self.assertTrue(r.get('reserved') or r.get('allowed'), r)
        self._normalize()
        return r['token']

    def set_limit(self, pk):
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO main_force_limits(pool_key, limited) VALUES(?,1)',
                         (pk,))
            conn.execute('PRAGMA journal_mode=DELETE')

    def auth(self, mode, task, stage, allowed, **kw):
        a = {'selection_mode': mode, 'quote': kw.pop('quote', '用户真实原话'),
             'authorization_scope': kw.pop('scope', {'task_id': task, 'stage': stage}),
             'allowed_candidates': allowed}
        a.update(kw)
        return a

    def plan(self, auth, *, previous=None, task='T', stage='s2', ws=None, now=T_NIGHT,
             permission_grants=None):
        return ss.plan_stage_selection(
            auth, previous=previous, store_path=self.store, task_id=task, stage=stage,
            workspace=ws or self._ws(task), now=now, permission_grants=permission_grants)

    def _digest(self):
        h = hashlib.sha256()
        for suffix in ('', '-wal', '-shm'):
            p = Path(self.store + suffix)
            if p.exists():
                h.update(p.read_bytes())
        return h.hexdigest()


class AuthorizationTests(_Base):
    def test_fixed_cn_flash_kept_even_with_free_main(self):
        self.build_store()
        auth = self.auth('fixed', 'T', 's2', [CN_FLASH], runtime='qodercn',
                         model='Qwen3.8-Flash')
        r = self.plan(auth)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CN_FLASH)

    def test_executor_cn_never_crosses_region(self):
        self.build_store()
        auth = self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH, executor='qodercn')
        r = self.plan(auth)
        self.assertEqual(r['selected_reason'], 'executor_region_main', r)
        self.assertTrue(r['selected']['pool_key'].startswith('qodercn:'), r)
        # 别区/别执行器绝不显示为 eligible，如实 not_authorized
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'not_authorized')
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'not_authorized')

    def test_executor_falls_to_own_flash_never_cross(self):
        self.build_store()
        # 白天：国际 Max 被时段门剔除 → 国际 Flash；绝不落到 CN。
        auth = self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH, executor='qoder')
        r = self.plan(auth, now=T_DAY)
        self.assertEqual(r['selected_reason'], 'executor_region_flash', r)
        self.assertEqual(r['selected']['pool_key'], 'qoder:Qwen3.8-Flash')
        self.assertEqual(r['candidate_filters'][CN_FLASH], 'not_authorized')

    def test_unknown_pool_matrix_rejects_luna_and_retired(self):
        self.build_store()
        for bad in ('luna:native', 'qodercn:DeepSeek-Flash', 'qoder:GLM-5.3'):
            r = self.plan(self.auth('auto', 'T', 's2', [bad]))
            self.assertEqual(r['refused'], 'unauthorized_candidate', bad)

    def test_no_quote_and_empty_allowed_rejected(self):
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', [dp.ZCODE_POOL_KEY], quote='   '))
        self.assertEqual(r['refused'], 'no_source_quote')
        r = self.plan(self.auth('auto', 'T', 's2', []))
        self.assertEqual(r['refused'], 'empty_allowed_candidates')

    def test_executor_missing_or_illegal_field_refused(self):
        self.build_store()
        r = self.plan(self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH))
        self.assertEqual(r['refused'], 'invalid_executor', r)
        r = self.plan(self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH, executor=123))
        self.assertEqual(r['refused'], 'invalid_executor', r)


class WindowAndLimitTests(_Base):
    def test_day_drops_gated_max_and_claude_keeps_zcode(self):
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY)
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_POOL_KEY, r)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'window_closed')
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'window_closed')

    def test_night_admits_gated_max_and_keeps_fairness(self):
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_NIGHT)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'eligible')
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'eligible')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible')

    def test_explicit_limit_skips_candidate_without_claiming(self):
        self.build_store()
        self.set_limit('qoder:Qwen3.8-Max')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_NIGHT)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'explicitly_limited')
        self.assertNotEqual(r['selected']['pool_key'], 'qoder:Qwen3.8-Max')

    def test_capacity_full_filter_and_fallback(self):
        self.build_store()
        self.reserve('a', 'zcode', 'GLM-5.3', executor='zcode')
        self.reserve('b', 'zcode', 'GLM-5.3', executor='zcode')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_NIGHT)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'capacity_full')
        self.assertEqual(r['selected']['pool_key'], 'qoder:Qwen3.8-Max', r)


class NightAutoCustomMaxRegression(_Base):
    """BW-CANDIDATE-SCOPE-S1 需求 3：独立夜间 AUTO 候选回归。绝不查询额度、不扩权、
    不把某 runtime:model 失败泛化成整个 executor，也不把 not_authorized 说成用户撤权。"""

    def test_night_auto_selects_cn_custom_max_when_builtins_limited(self):
        """7 候选全授权、内置 CN 与国际 Max 已明确限额、Claude 合格时，按既有
        Max>Claude>Z 优先级选择 CN 自定义 Max，绝不落到 Claude 或 Flash，也不因内置
        失败泛化到自定义（自定义独立评估）。allowed 全集与逐候选原因如实保留。"""
        self.build_store()
        self.set_limit('qoder:Qwen3.8-Max')
        self.set_limit('qodercn:Qwen3.8-Max')
        allowed = ALL_MAINS + ALL_FLASH
        r = self.plan(self.auth('auto', 'T', 's2', allowed), now=T_NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(sorted(r['allowed_candidates']), sorted(allowed))  # 不缩权
        self.assertEqual(r['selected']['pool_key'], dp.CN_CUSTOM_MAX_POOL_KEY, r)
        self.assertNotEqual(r['selected']['pool_key'], dp.CLAUDE_POOL_KEY)
        self.assertNotIn(r['selected']['pool_key'], ALL_FLASH)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'explicitly_limited')
        self.assertEqual(r['candidate_filters']['qodercn:Qwen3.8-Max'], 'explicitly_limited')
        self.assertEqual(r['candidate_filters'][dp.CN_CUSTOM_MAX_POOL_KEY], 'eligible')

    def test_auto_narrow_cn_scope_stays_cn_others_not_authorized(self):
        """合法用户把 AUTO 范围收窄到只 CN：选择仍在 CN 内，其余区/池如实 not_authorized
        ——这只表示不在本轮输入里，绝不伪造“用户撤权”，也绝不偷偷扩权到 qoder/claude/zcode。"""
        self.build_store()
        cn_scope = CN_MAINS + [CN_FLASH]
        r = self.plan(self.auth('auto', 'T', 's2', cn_scope), now=T_NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(sorted(r['allowed_candidates']), sorted(cn_scope))
        self.assertTrue(r['selected']['pool_key'].startswith('qodercn:'), r)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.CLAUDE_POOL_KEY], 'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'not_authorized')

    def test_only_flash_input_marks_mains_not_authorized_without_fake_revocation(self):
        """错误输入只给 Flash 时，主力的 not_authorized 只反映未列入输入（不是撤权）；
        选择仍是该 Flash，合法窄范围不被偷偷扩到主力。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', [CN_FLASH]), now=T_NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CN_FLASH, r)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Max'], 'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.CN_CUSTOM_MAX_POOL_KEY], 'not_authorized')


class PreviousAndConflictTests(_Base):
    def test_previous_in_flight_and_not_reviewed_block(self):
        self.build_store()
        auth = self.auth('auto', 'T', 's2', ALL_MAINS)
        for st in ('reserved', 'running', 'unknown', 'executing', 'awaiting_acceptance'):
            r = self.plan(auth, previous={'state': st, 'acceptance_reviewed': True})
            self.assertEqual(r['refused'], 'previous_in_flight', st)
        r = self.plan(auth, previous={'state': 'finished', 'acceptance_reviewed': False})
        self.assertEqual(r['refused'], 'previous_awaiting_acceptance')

    def test_task_in_flight_blocks_new_decision(self):
        self.build_store()
        self.reserve('T', 'zcode', 'GLM-5.3', executor='zcode')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH))
        self.assertEqual(r['refused'], 'task_in_flight')

    def test_workspace_conflict_rejects_whole_item(self):
        self.build_store()
        shared = self._ws('shared')
        self.reserve('a', 'zcode', 'GLM-5.3', ws=shared, executor='zcode')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), ws=shared)
        self.assertEqual(r['refused'], 'workspace_conflict')

    def test_scope_drift_rejected(self):
        self.build_store()
        auth = self.auth('auto', 'T', 's2', [dp.ZCODE_POOL_KEY],
                         scope={'task_id': 'OTHER', 'stage': 's2'})
        r = self.plan(auth)
        self.assertEqual(r['refused'], 'scope_conflict')


class TemporaryFallbackTests(_Base):
    def test_expired_temp_scope_reselects_via_parent(self):
        # 独立反例：temp 原 quote/scope 与 fallback_scope 都是 T/s1，previous=T/s1 已验收，
        # 调用 stage=s2 必须先识别旧 scope 到期（不是先用 s2 验证旧 s1 scope）。
        self.build_store()
        parent = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash',
                         parent_authorization=parent)
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1', 'runtime': 'qodercn', 'model': 'Qwen3.8-Flash'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], None, r)
        self.assertTrue(r['reselection_due'], r)
        self.assertEqual(r['fallback_scope'], {'task_id': 'T', 'stage': 's1'})  # 旧值不改写
        self.assertEqual(r['selection_mode'], 'auto')                # 有效父授权
        self.assertEqual(r['source_selection_mode'], 'temporary_fallback')
        self.assertEqual(r['source_authorization_scope'],
                         {'task_id': 'T', 'stage': 's1'})
        self.assertIn(r['selected']['pool_key'], ALL_MAINS, r)
        self.assertEqual(sorted(r['allowed_candidates']), sorted(ALL_MAINS + ALL_FLASH))

    def test_new_stage_without_parent_refused(self):
        self.build_store()
        auth = self.auth('temporary_fallback', 'T', 's1', [CN_FLASH],
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash')
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], 'parent_authorization_required')

    def test_expired_temp_cross_task_previous_no_reselect(self):
        self.build_store()
        parent = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash',
                         parent_authorization=parent)
        prev = {'task_id': 'OTHER', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], 'fallback_scope_mismatch', r)

    def test_expired_temp_no_acceptance_and_in_flight_no_reselect(self):
        self.build_store()
        parent = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash',
                         parent_authorization=parent)
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': False,
                'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], 'previous_awaiting_acceptance', r)
        prev = {'task_id': 'T', 'state': 'running', 'acceptance_reviewed': True,
                'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], 'previous_in_flight', r)

    def test_expired_temp_parent_candidates_intersect_base(self):
        self.build_store()
        # 父授权只允许国际 Max，base 只授权 CN Flash → 交集空 → 拒绝（不扩权）
        parent = self.auth('auto', 'T', 's2', ['qoder:Qwen3.8-Max'])
        auth = self.auth('temporary_fallback', 'T', 's1', [CN_FLASH],
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash',
                         parent_authorization=parent)
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], 'parent_authorization_required', r)
        self.assertEqual(r['refused_detail'], 'empty intersection with base')

    def test_expired_temp_parent_intersection_drops_extra(self):
        self.build_store()
        parent = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)  # 允许全部
        auth = self.auth('temporary_fallback', 'T', 's1', [CN_FLASH],  # base 只授权 CN Flash
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash',
                         parent_authorization=parent)
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s2')
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['allowed_candidates'], [CN_FLASH])   # 交集不扩权
        self.assertEqual(r['selected']['pool_key'], CN_FLASH, r)

    def test_same_stage_fallback_uses_designated_flash(self):
        # 同阶段：即使 base 授权集合里有可用主力，也按当阶段明确 runtime+model 的 Flash
        # 临时降级（不是 AUTO 关系选择、也不是永久 fixed）。
        self.build_store()
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash')
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True, 'stage': 's1',
                'runtime': 'qodercn', 'model': 'Qwen3.8-Flash'}
        r = self.plan(auth, previous=prev, stage='s1')
        self.assertFalse(r['reselection_due'])
        self.assertEqual(r['selection_mode'], 'temporary_fallback')
        self.assertEqual(r['allowed_candidates'], ALL_MAINS + ALL_FLASH)
        self.assertEqual(r['selected']['pool_key'], CN_FLASH, r)
        self.assertTrue(r['session_reuse_allowed'])

    def test_same_stage_temp_non_flash_combo_refused(self):
        self.build_store()
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='zcode', model='GLM-5.3')
        prev = {'state': 'finished', 'acceptance_reviewed': True, 'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s1')
        self.assertEqual(r['refused'], 'temporary_not_flash', r)

    def test_temp_fallback_scope_missing_task_id_refused(self):
        # 缺 task_id 不得猜授权：即使 stage 相同也如实拒绝，不凭 previous/集合蒙混。
        self.build_store()
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash')
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True, 'stage': 's1'}
        r = self.plan(auth, previous=prev, stage='s1')
        self.assertEqual(r['refused'], 'fallback_scope_mismatch', r)

    def test_session_reuse_requires_same_task_and_combo(self):
        # 复用仅限 previous 显式属本 task 且组合相同、已验收终态；OTHER/missing task 一律 false。
        self.build_store()
        auth = self.auth('fixed', 'T', 's2', [CN_FLASH], runtime='qodercn',
                         model='Qwen3.8-Flash')
        base = {'state': 'finished', 'acceptance_reviewed': True, 'stage': 's1',
                'runtime': 'qodercn', 'model': 'Qwen3.8-Flash'}
        r = self.plan(auth, previous=dict(base, task_id='T'))
        self.assertEqual(r['refused'], None, r)
        self.assertTrue(r['session_reuse_allowed'], r)
        r = self.plan(auth, previous=dict(base, task_id='OTHER'))
        self.assertEqual(r['refused'], None, r)
        self.assertFalse(r['session_reuse_allowed'], r)
        r = self.plan(auth, previous=dict(base))  # 缺 task_id → 不复用
        self.assertEqual(r['refused'], None, r)
        self.assertFalse(r['session_reuse_allowed'], r)

    def test_fallback_scope_mismatch_refused(self):
        self.build_store()
        auth = self.auth('temporary_fallback', 'T', 's9', [CN_FLASH],
                         fallback_scope={'task_id': 'T', 'stage': 's9'})
        r = self.plan(auth)
        self.assertEqual(r['refused'], 'fallback_scope_mismatch')

    def test_auto_not_constrained_by_previous_model(self):
        self.build_store()
        prev = {'state': 'finished', 'acceptance_reviewed': True, 'stage': 's1',
                'runtime': 'qodercn', 'model': 'Qwen3.8-Flash'}
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), previous=prev)
        # 上一轮是 CN Flash，但 auto 仍按授权集合选主力，绝不从旧模型猜长期约束。
        self.assertIn(r['selected']['pool_key'], ALL_MAINS, r)


class ContextAndReadOnlyTests(_Base):
    def test_missing_context_refused(self):
        self.build_store()
        auth = self.auth('auto', 'T', 's2', [dp.ZCODE_POOL_KEY])
        ws = self._ws('T')
        for kw in ({'task_id': None, 'stage': 's2', 'workspace': ws},
                   {'task_id': 'T', 'stage': None, 'workspace': ws},
                   {'task_id': 'T', 'stage': 's2', 'workspace': None}):
            r = ss.plan_stage_selection(auth, store_path=self.store, now=T_NIGHT, **kw)
            self.assertEqual(r['refused'], 'missing_context', kw)

    def test_plan_does_not_modify_database(self):
        self.build_store()
        before = self._digest()
        self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH))
        self.assertEqual(before, self._digest())

    def test_wal_writer_latest_active_must_be_read(self):
        # 临时 WAL 库 + 打开的 writer 保留最新 active；规划只读打开必须读到它。
        writer = dp.connect(self.store)
        try:
            dp._rotation_row(writer)
            r = dp.reserve(self.store, task_id='Q', runtime='zcode', model='GLM-5.3',
                           workspace=self._ws('Q'), prompt_sha256='p', executor='zcode',
                           now=T_NIGHT, _preclaim=False)
            self.assertTrue(r.get('reserved') or r.get('allowed'), r)
            self.assertTrue(Path(self.store + '-wal').exists())
            self.assertTrue(Path(self.store + '-shm').exists())
            plan = self.plan(self.auth('auto', 'Q', 's2', ALL_MAINS + ALL_FLASH), task='Q')
            self.assertEqual(plan['refused'], 'task_in_flight', plan)
            self.assertTrue(Path(self.store + '-shm').exists())  # 不制造/不删除 sidecar
        finally:
            writer.close()

    def test_wal_without_shm_refused_without_creating_sidecar(self):
        p = self.base / 'walonly.sqlite3'
        with closing(dp.connect(str(p))) as conn:
            dp._rotation_row(conn)
        shm = Path(str(p) + '-shm')
        if shm.exists():
            shm.unlink()
        Path(str(p) + '-wal').write_bytes(b'')
        r = ss.plan_stage_selection(
            self.auth('auto', 'T', 's2', [dp.ZCODE_POOL_KEY]), store_path=str(p),
            task_id='T', stage='s2', workspace=self._ws('T'), now=T_NIGHT)
        self.assertEqual(r['refused'], 'store_snapshot_unavailable', r)
        self.assertFalse(shm.exists())

    def test_missing_schema_refused_and_unchanged(self):
        p = self.base / 'noschema.sqlite3'
        conn = sqlite3.connect(str(p))
        conn.execute('CREATE TABLE other(x)')
        conn.commit()
        conn.close()
        ws = self._ws('T')
        before = sorted(fp.name for fp in self.base.iterdir())
        r = ss.plan_stage_selection(
            self.auth('auto', 'T', 's2', [dp.ZCODE_POOL_KEY]), store_path=str(p),
            task_id='T', stage='s2', workspace=ws, now=T_NIGHT)
        self.assertEqual(r['refused'], 'store_refused', r)
        self.assertEqual(before, sorted(fp.name for fp in self.base.iterdir()))

    def test_open_store_ro_closes_connection_on_error(self):
        # connect 已建但 PRAGMA/BEGIN 失败：必须先 close 再抛 Refused，不泄漏只读连接。
        self.build_store()
        fake = unittest.mock.MagicMock()
        fake.execute.side_effect = sqlite3.OperationalError('boom')
        with unittest.mock.patch.object(ss.sqlite3, 'connect', return_value=fake):
            with self.assertRaises(ss.Refused) as ctx:
                ss._open_store_ro(self.store)
        self.assertEqual(ctx.exception.code, 'store_snapshot_unavailable')
        self.assertTrue(fake.close.called)

    def test_missing_store_refused_and_not_created(self):
        missing = self.base / 'nope.sqlite3'
        r = ss.plan_stage_selection(
            self.auth('auto', 'T', 's2', [dp.ZCODE_POOL_KEY]), store_path=str(missing),
            task_id='T', stage='s2', workspace=self._ws('T'), now=T_NIGHT)
        self.assertEqual(r['refused'], 'store_refused')
        self.assertFalse(missing.exists())

    def test_cli_exit_codes(self):
        self.build_store()
        auth = self.auth('auto', 'T', 's2', [dp.ZCODE_POOL_KEY])
        ws = self._ws('T')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ss.main(['--store', self.store, '--task-id', 'T', '--stage', 's2',
                            '--workspace', ws,
                            '--authorization-json', json.dumps(auth)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())['selected']['pool_key'],
                         dp.ZCODE_POOL_KEY)
        buf = io.StringIO()
        missing = str(self.base / 'gone.sqlite3')
        with contextlib.redirect_stdout(buf):
            code = ss.main(['--store', missing, '--task-id', 'T', '--stage', 's2',
                            '--workspace', ws,
                            '--authorization-json', json.dumps(auth)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(buf.getvalue())['refused'], 'store_refused')


class PermissionGrantsTests(_Base):
    """BW-TASK-PERMISSION-S1：派工计划生成之前的任务权限需求 × 运行时能力匹配。
    复用 execution_control.CAPABILITY（fine_grained/read_dirs），授权与权限需求分开；
    能力不支持的授权内候选标 permission_incompatible，绝不写成 not_authorized/quota，
    也绝不修改 CAPABILITY 把 ZCode 伪装成 fine_grained=True。"""

    FINE = {'edits': ['Edit(C:/abs/file.py)'], 'bash': ['Bash(pytest -q)'],
            'read_dirs': []}

    def _assert_capability_table_untouched(self):
        self.assertEqual(ec.CAPABILITY['zcode'],
                         {'fine_grained': False, 'read_dirs': False})
        self.assertEqual(ec.CAPABILITY['qoder']['fine_grained'], True)

    def test_day_fine_grants_skip_z_for_authorized_compatible_flash(self):
        """白天 Z 健康且有全部空位，但任务需精确 Edit/Bash：Z 被能力过滤，
        选兼容的已授权 Flash，绝不启动 Z 计划。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                      now=T_DAY, permission_grants=self.FINE)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': True, 'read_dirs': False})
        self.assertEqual(r['selected']['pool_key'], 'qoder:Qwen3.8-Flash', r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['candidate_filters']['qodercn:Qwen3.8-Flash'], 'eligible')
        self.assertNotEqual(r['selected']['runtime'], 'zcode')
        self._assert_capability_table_untouched()

    def test_day_same_window_without_exact_rules_z_still_eligible(self):
        """同一白天窗口、不要求精确规则（未传 grants）：Z 仍可选（旧兼容行为）。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY)
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_POOL_KEY, r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible')
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': False, 'read_dirs': False})

    def test_fixed_z_with_exact_requirement_refused_not_swapped(self):
        """fixed 明确 ZCode 但任务要求精确 grants：诚实拒绝，不偷偷换执行器。"""
        self.build_store()
        auth = self.auth('fixed', 'T', 's2', [dp.ZCODE_POOL_KEY],
                         runtime='zcode', model='GLM-5.3')
        r = self.plan(auth, permission_grants=self.FINE)
        self.assertEqual(r['refused'], 'fixed_candidate_unavailable', r)
        self.assertEqual(r['refused_detail'], 'permission_incompatible', r)
        self.assertIsNone(r['selected'])

    def test_read_dirs_requirement_is_independent_capability_filter(self):
        """外部 read_dirs 需求走独立能力过滤：只剔除无 read_dirs 能力的候选，
        与 fine_grained 需求分别可辨（codebuddy 类不在池内，此处 Z 两能力皆无）。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                      permission_grants={'edits': [], 'bash': [],
                                         'read_dirs': ['D:/refs']})
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': False, 'read_dirs': True})
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['selected']['pool_key'], 'qoder:Qwen3.8-Flash', r)
        self._assert_capability_table_untouched()

    def test_missing_qoder_authorization_never_expands_via_permission_filter(self):
        """能力过滤只作用于授权集合内候选：缺 Qoder 授权时精确需求绝不把候选扩到
        Qoder，非授权池仍如实 not_authorized，需求兼容也不构成授权。"""
        self.build_store()
        auth = self.auth('auto', 'T', 's2', [CN_FLASH, dp.ZCODE_POOL_KEY])
        r = self.plan(auth, permission_grants=self.FINE)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CN_FLASH, r)
        self.assertEqual(r['candidate_filters']['qoder:Qwen3.8-Flash'], 'not_authorized')
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible')

    def test_quota_unavailable_and_permission_incompatible_distinguishable(self):
        """额度/availability 阻断与权限能力不兼容分别可辨：Z 被阻断且任务需精确
        grants 时，原因记 permission_incompatible（能力门前置），而仅需 read_dirs
        之外的 grants 变化时 zcode_unavailable 单独出现——两原因各归各，绝不混写。"""
        self.build_store()
        with _patch_zcode(False):
            # 混合 Edit+Bash：无法进受控文件 profile（bash 必须为空），能力门前置，
            # 即便 Z 被阻断仍记 permission_incompatible；与下面纯 availability 阻断相区分。
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                          permission_grants=self.FINE)
            self.assertEqual(r['zcode_blocked'], True)
            self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                             'permission_incompatible', r)
        with _patch_zcode(False):
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH))
            self.assertEqual(r['zcode_blocked'], True)
            self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                             'zcode_unavailable', r)

    def test_workspace_inflight_guards_and_readonly_hash_kept_with_grants(self):
        """权限过滤不替代既有守卫：同任务在途/工作区冲突照旧整项拒绝；
        带 grants 的规划仍只读，读写前后快照哈希不变。"""
        self.build_store()
        shared = self._ws('shared')
        self.reserve('a', 'zcode', 'GLM-5.3', ws=shared, executor='zcode')
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                      ws=shared, permission_grants=self.FINE)
        self.assertEqual(r['refused'], 'workspace_conflict')
        self.reserve('T', 'zcode', 'GLM-5.3', executor='zcode')
        before = self._digest()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                      task='T', permission_grants=self.FINE)
        self.assertEqual(r['refused'], 'task_in_flight')
        self.assertEqual(before, self._digest())

    def test_malformed_permission_grants_refused_and_cli_exit_two(self):
        """严格拒绝畸形 grants：非对象/未知键/非列表/空或带空白项 →
        invalid_permission_grants；CLI 最小新输入 --permission-grants-json 畸形
        JSON 或拒绝结果退出码 2。"""
        self.build_store()
        bad = ([1, 2], {'edits': 'not-a-list'}, {'edits': ['']},
               {'edits': [' x ']}, {'edits': [], 'tool_visibility': ['Read']},
               {'editz': []})
        for b in bad:
            r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH),
                          permission_grants=b)
            self.assertEqual(r['refused'], 'invalid_permission_grants', b)
        ws = self._ws('T')
        # 畸形 JSON 字符串输入（与 --authorization-json 同口径）：非零 SystemExit，
        # 绝不宽容；合法 JSON 内的畸形 grants 结构则走拒收回执、退出码 2。
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(io.StringIO()):
                ss.main(['--store', self.store, '--task-id', 'T', '--stage', 's2',
                         '--workspace', ws,
                         '--authorization-json',
                         json.dumps(self.auth('auto', 'T', 's2', ALL_MAINS)),
                         '--permission-grants-json', '{not json'])
        self.assertIn('invalid JSON', str(ctx.exception))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ss.main(['--store', self.store, '--task-id', 'T', '--stage', 's2',
                            '--workspace', ws,
                            '--authorization-json',
                            json.dumps(self.auth('auto', 'T', 's2', ALL_MAINS)),
                            '--permission-grants-json', '{"edits": [" x "]}'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(buf.getvalue())['refused'],
                         'invalid_permission_grants')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ss.main(['--store', self.store, '--task-id', 'T', '--stage', 's2',
                            '--workspace', ws,
                            '--authorization-json',
                            json.dumps(self.auth('fixed', 'T', 's2',
                                                 [dp.ZCODE_POOL_KEY],
                                                 runtime='zcode', model='GLM-5.3')),
                            '--permission-grants-json', json.dumps(self.FINE)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(buf.getvalue())['refused'],
                         'fixed_candidate_unavailable')

    def test_temp_fallback_same_stage_and_parent_reselect_apply_current_need(self):
        """临时降级不丢失当前阶段权限需求：同阶段指定 Flash 与旧 scope 到期经父授权
        重选，都按**本轮** grants 应用能力过滤（Z 标 permission_incompatible），绝不从
        旧 session 继承需求、也不因临时/父授权绕过过滤。"""
        self.build_store()
        # 同阶段：指定已授权且能力兼容的 CN Flash（runtime qodercn fine_grained=True）
        # → 正常 temporary_fallback_selected；Z 在候选集合中如实标 permission_incompatible。
        auth = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                         fallback_scope={'task_id': 'T', 'stage': 's1'},
                         runtime='qodercn', model='Qwen3.8-Flash')
        prev = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                'stage': 's1', 'runtime': 'qodercn', 'model': 'Qwen3.8-Flash'}
        r = self.plan(auth, previous=prev, stage='s1',
                      permission_grants=self.FINE)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected_reason'], 'temporary_fallback_selected', r)
        self.assertEqual(r['selected']['pool_key'], CN_FLASH, r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible')
        # 到期经父授权重选：应用**当前**需求 → 白天三 Max/Claude 窗闭、Z 权限不兼容，
        # 选择落兼容的已授权 Flash；旧 s1 的需求绝不反向继承或改写本轮需求。
        parent = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        auth2 = self.auth('temporary_fallback', 'T', 's1', ALL_MAINS + ALL_FLASH,
                          fallback_scope={'task_id': 'T', 'stage': 's1'},
                          runtime='qodercn', model='Qwen3.8-Flash',
                          parent_authorization=parent)
        prev2 = {'task_id': 'T', 'state': 'finished', 'acceptance_reviewed': True,
                 'stage': 's1', 'runtime': 'qodercn', 'model': 'Qwen3.8-Flash'}
        r = self.plan(auth2, previous=prev2, stage='s2', now=T_DAY,
                      permission_grants=self.FINE)
        self.assertEqual(r['refused'], None, r)
        self.assertTrue(r['reselection_due'])
        self.assertEqual(r['selection_mode'], 'auto')
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': True, 'read_dirs': False})
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible')
        self.assertEqual(r['selected']['pool_key'], 'qoder:Qwen3.8-Flash', r)

    def test_edits_only_grants_make_zcode_eligible_via_controlled_file_profile(self):
        """仅需精确文件 Edit（无 Bash、无 read_dirs）时，ZCode 经**受控文件 profile** 变为可选，
        回执标明 controlled_file_profile_required=True；绝不修改 CAPABILITY 的 fine_grained=False，
        也绝不因此把混合 Bash 任务放行。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                      permission_grants={'edits': ['Edit(C:/a.py)'], 'bash': [],
                                         'read_dirs': []})
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY], 'eligible', r)
        self.assertEqual(r['permission_requirements'],
                         {'fine_grained': True, 'read_dirs': False})
        self.assertTrue(r['controlled_file_profile_required'], r)
        self._assert_capability_table_untouched()

    def test_fixed_z_edits_only_selects_zcode_not_swapped(self):
        """fixed 明确 ZCode 且本轮仅需精确 Edit：受控文件 profile 使其可选，正常选中 ZCode，
        绝不因能力表 fine_grained=False 而偷偷换执行器。"""
        self.build_store()
        auth = self.auth('fixed', 'T', 's2', [dp.ZCODE_POOL_KEY],
                         runtime='zcode', model='GLM-5.3')
        r = self.plan(auth, permission_grants={'edits': ['Edit(C:/a.py)'], 'bash': [],
                                               'read_dirs': []})
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], dp.ZCODE_POOL_KEY, r)
        self.assertTrue(r['controlled_file_profile_required'], r)

    def test_mixed_edit_bash_task_remains_permission_incompatible(self):
        """混合 Edit+Bash 整段任务继续 permission_incompatible：受控文件 profile 只覆盖纯精确
        Edit，Bash 需求一律不成立，回执不标 controlled_file_profile_required，主脑须串行拆分。"""
        self.build_store()
        r = self.plan(self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH), now=T_DAY,
                      permission_grants=self.FINE)
        self.assertEqual(r['candidate_filters'][dp.ZCODE_POOL_KEY],
                         'permission_incompatible', r)
        self.assertFalse(r['controlled_file_profile_required'], r)


if __name__ == '__main__':
    unittest.main()
