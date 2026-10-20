"""BW-CUSTOM-MAX-RESUME-20261018-S1 离线回归：CN 自定义 Max 在 Max 层内优先。

只用临时 SQLite（每用例独立 store）+ mock ZCode availability / process_identity，
零真实模型 / 零凭据 / 零真实池 / 零网络。断言（BW-ZCODE-RETIRE-20261020 起 zcode 已全局
退役，请求组合/占位改用现役池）：
- AUTO 顺序 CodeBuddy > CN 自定义 Max > 两内置 Max（同级在途平衡）> Claude > 两 OpenCode；
- 夜间自定义即使已有 1 在途、两内置 0 在途，仍先选自定义直到其 2 槽满；
- 自定义 2 槽满 → 两内置（真实在途平衡/stable tie 保持）；
- 全部 Max 满 → Claude；白天三 Max 全部剔除；
- 显式指定内置 Max 的固定选择不被新优先级改写；
- stage_selection 规划与正式原子入口 select_and_claim 同序；
- 当前权威总容量 14（BW-ZCODE-RETIRE-20261020 起 zcode 2 槽退役，原 16 口径作废）、
  同 workspace 单写与 unknown 在途保护保留。
"""
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_pool as dp  # noqa: E402
import stage_selection as ss  # noqa: E402

CODEBUDDY = 'codebuddy:deepseek-v4.1-flash'
QMAX = 'qoder:Qwen3.8-Max'
CN_MAX = 'qodercn:Qwen3.8-Max'
CN_CUSTOM_MAX = 'qodercn:Qwen-3.8-Max'
CLAUDE = 'claude:deepseek-flash'
ZCODE = 'zcode:GLM-5.3'  # BW-ZCODE-RETIRE-20261020：已全局退役，仅用于拒绝/层序断言
OPENCODE_DS = 'opencode:deepseek-v4.1-flash-free'
QFLASH = 'qoder:Qwen3.8-Flash'
CN_FLASH = 'qodercn:Qwen3.8-Flash'
MAXES = (QMAX, CN_MAX, CN_CUSTOM_MAX)

# 北京 23:00 周四：三 Max 夜间窗内，claude 非高峰可派
NIGHT = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)
# 北京 10:00 周四：Max 夜间窗外；claude 工作日高峰门关；ZCode 全天
DAY = datetime(2026, 10, 8, 2, 0, 0, tzinfo=timezone.utc)

ALL_MAINS = list(dp.MAIN_FORCE_KEYS)
ALL_FLASH = list(dp.OVERFLOW_KEYS)
SHA = 'p' + '0' * 63


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-custom-max-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        self._seed_n = 0
        z = mock.patch.object(dp, '_zcode_availability_blocked',
                              lambda *a, **k: None)
        pid = mock.patch.object(dp, 'process_identity',
                                return_value={'state': 'alive', 'created': 'c1'})
        z.start()
        pid.start()
        self.addCleanup(z.stop)
        self.addCleanup(pid.stop)
        self.addCleanup(self._tmp.cleanup)
        self.build_store()

    def build_store(self):
        with closing(dp.connect(self.store)) as conn:
            dp._rotation_row(conn)
            conn.execute('PRAGMA journal_mode=DELETE')

    def ws(self, tid):
        d = self.base / 'ws' / str(tid)
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def seed(self, pk, n=1, state='reserved'):
        runtime, model = pk.split(':', 1)
        with closing(dp.connect(self.store)) as conn:
            for _ in range(n):
                self._seed_n += 1
                tok = f'tok-{self._seed_n}'
                conn.execute(
                    'INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                    'state, child_pid, reserved_at_utc) VALUES(?,?,?,?,?,?,?,?)',
                    (tok, f'task-{tok}', runtime, model, pk, state, None,
                     dp._iso(NIGHT)))
            conn.execute('PRAGMA journal_mode=DELETE')

    def fill_codebuddy(self):
        """占住最高层唯一名额，使后续断言聚焦 Max 层内部次序。"""
        self.seed(CODEBUDDY, 1)

    def select(self, requested, now, zcode_blocked=False):
        with closing(dp.connect(self.store)) as conn:
            elig = dp._eligible_main_forces(conn, zcode_blocked, now)
            return dp._select_main_force(conn, requested, now, eligible=elig)

    def reserve(self, tid, runtime, model, executor='auto', now=NIGHT, ws=None):
        return dp.reserve(self.store, task_id=tid, runtime=runtime, model=model,
                          workspace=ws or self.ws(tid), prompt_sha256=SHA, stage='s',
                          now=now, executor=executor, _preclaim=False)

    def claim_auto(self, tid, runtime='qoder', model='Qwen3.8-Flash', now=NIGHT):
        # BW-ZCODE-RETIRE-20261020：原默认请求组合 zcode:GLM-5.3 已退役（会先撞
        # unknown_pool），改用现役国际 Flash 请求组合验证 AUTO 层序。
        return dp.select_and_claim(self.store, task_id=tid, runtime=runtime, model=model,
                                   workspace=self.ws(tid), prompt_sha256=SHA, stage='s',
                                   now=now, executor='auto', _preclaim=False)

    def auth(self, allowed, task='T', stage='s2'):
        return {'selection_mode': 'auto', 'quote': '用户真实原话',
                'authorization_scope': {'task_id': task, 'stage': stage},
                'allowed_candidates': allowed}

    def plan(self, auth, task='T', stage='s2', now=NIGHT):
        return ss.plan_stage_selection(
            auth, previous=None, store_path=self.store, task_id=task, stage=stage,
            workspace=self.ws(task), now=now)


class CustomMaxPriorityTests(_Base):
    def test_codebuddy_precedes_custom_max(self):
        target, reason = self.select(ZCODE, NIGHT)
        self.assertEqual(reason, 'main_force_selected')
        self.assertEqual(target, CODEBUDDY)
        self.fill_codebuddy()
        self.assertEqual(self.select(ZCODE, NIGHT)[0], CN_CUSTOM_MAX)

    def test_night_custom_precedes_builtins_even_with_more_inflight(self):
        # 自定义 1 在途（未满 2），两内置均 0 在途 → 仍先选自定义。
        self.fill_codebuddy()
        self.seed(CN_CUSTOM_MAX, 1)
        target, _ = self.select(QMAX, NIGHT)
        self.assertEqual(target, CN_CUSTOM_MAX)

    def test_custom_full_falls_to_builtins(self):
        self.fill_codebuddy()
        self.seed(CN_CUSTOM_MAX, 2)
        target, _ = self.select(ZCODE, NIGHT)
        self.assertIn(target, (QMAX, CN_MAX))

    def test_all_max_full_falls_to_claude(self):
        self.fill_codebuddy()
        self.seed(CN_CUSTOM_MAX, 2)
        self.seed(QMAX, 1)
        self.seed(CN_MAX, 1)
        target, _ = self.select(ZCODE, NIGHT)
        self.assertEqual(target, CLAUDE)

    def test_day_all_max_excluded(self):
        self.fill_codebuddy()
        with closing(dp.connect(self.store)) as conn:
            elig = dp._eligible_main_forces(conn, False, DAY)
        for pk in MAXES:
            self.assertNotIn(pk, elig)
        # BW-ZCODE-RETIRE-20261020：zcode 已全局退役、不在候选；白天 Max 全剔除、
        # claude 高峰门关 → 落现役下一层两 OpenCode 免费槽，且绝不静默改派 Max。
        self.assertNotIn(ZCODE, elig)
        self.assertIn(self.select(ZCODE, DAY)[0], dp.OPENCODE_POOL_KEYS)

    def test_explicit_builtin_selection_not_rewritten(self):
        r = self.reserve('qm', 'qoder', 'Qwen3.8-Max', executor='qoder')
        self.assertTrue(r.get('allowed'), r)
        self.assertEqual(r['pool_key'], QMAX)
        r = self.reserve('cm', 'qodercn', 'Qwen3.8-Max', executor='qodercn')
        self.assertTrue(r.get('allowed'), r)
        self.assertEqual(r['pool_key'], CN_MAX)


class SameOrderAndGuardsTests(_Base):
    def test_stage_preview_and_atomic_entry_same_order(self):
        self.fill_codebuddy()
        auth = self.auth(ALL_MAINS + ALL_FLASH)
        r = self.plan(auth)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CN_CUSTOM_MAX, r)
        out = self.claim_auto('t1')
        self.assertEqual(out['selected']['pool_key'], CN_CUSTOM_MAX, out)

    def test_capacity_16_current_authority(self):
        # BW-ZCODE-RETIRE-20261020：zcode 2 槽退役，权威总容量 16→14（旧 16 口径作废）。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 14)
        self.assertEqual(dp.capacity_for(CN_CUSTOM_MAX), 2)
        self.assertEqual(sum(dp.MAIN_FORCE_CAPACITY.values())
                         + sum(dp.OVERFLOW_CAPACITY.values()), 14)

    def test_workspace_single_write_guard_unchanged(self):
        # BW-ZCODE-RETIRE-20261020：原 zcode executor 入口已退役（retired_pool 拒绝），
        # 单写守卫改用现役 codebuddy executor，验证同 workspace 在途互斥语义不变。
        shared = self.ws('shared')
        r1 = self.reserve('w1', 'codebuddy', 'deepseek-v4.1-flash',
                          executor='codebuddy', ws=shared)
        self.assertTrue(r1.get('allowed'), r1)
        r2 = self.reserve('w2', 'codebuddy', 'deepseek-v4.1-flash',
                          executor='codebuddy', ws=shared)
        self.assertFalse(r2.get('allowed'), r2)
        self.assertEqual(r2['reason'], 'workspace_in_flight')

    def test_unknown_inflight_still_holds_capacity(self):
        self.fill_codebuddy()
        self.seed(CN_CUSTOM_MAX, 2, state='unknown')
        target, _ = self.select(ZCODE, NIGHT)
        self.assertNotEqual(target, CN_CUSTOM_MAX)
        self.assertIn(target, (QMAX, CN_MAX))


if __name__ == '__main__':
    unittest.main()
