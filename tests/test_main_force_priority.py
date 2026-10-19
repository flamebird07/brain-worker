"""BW-MAIN-PRIORITY-20261011-S1 离线回归：主力优先级 Max > Claude > Z（GLM），覆盖旧跨层 1:1。

只用临时 SQLite（每用例独立 store）+ mock ZCode availability / process_identity，零真实
模型 / 零凭据 / 零真实池 / 零网络。断言：
- 夜间三 Qoder Max 优先（即使其历史 committed 更高、请求偏好别池）；
- 一个 Max 满/限额时选另一 Max，绝不落到 Claude/Z；
- Max 全不可用时，Claude 允许窗且有空位优先于 Z；Claude 满/限额/峰值才 Z；
- 同层内保持现有在途平衡与 tie 方式；rotation 历史计数照旧保留；
- 全部主力无可用槽才 Flash；只允许 Z / 固定 Flash 时不强制 Max；CN 约束不跨区；
- 16 总槽（BW-OPENCODE-20261019 起两 CodeBuddy + 两 OpenCode 免费模型各独立 1 槽；
  CodeBuddy 最高层名额由 build_store 预占以保留旧层序断言）、unknown 在途、
  workspace 冲突、北京 22/08 边界与 DeepSeek 独立时间门不变；
- stage_selection 复用单一权威 `_select_main_force`，同一新优先级。
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

QMAX = 'qoder:Qwen3.8-Max'
CN_MAX = 'qodercn:Qwen3.8-Max'
CN_CUSTOM_MAX = 'qodercn:Qwen-3.8-Max'
CLAUDE = 'claude:deepseek-flash'
ZCODE = 'zcode:GLM-5.3'
QFLASH = 'qoder:Qwen3.8-Flash'
CN_FLASH = 'qodercn:Qwen3.8-Flash'
RETIRED_DS = 'qodercn:DeepSeek-Flash'
MAXES = (QMAX, CN_MAX, CN_CUSTOM_MAX)

# 北京 23:00 周四：三 Max 夜间窗内 + claude offpeak + zcode 全天
NIGHT = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)
# 北京 22:00 整（左闭：Max 可选）与 08:00 整（右开：Max 不可选）
NIGHT_22 = datetime(2026, 10, 8, 14, 0, 0, tzinfo=timezone.utc)
DAY_08 = datetime(2026, 10, 8, 0, 0, 0, tzinfo=timezone.utc)
# 北京周一 09:00 工作日高峰：Max 窗外 + claude 高峰门关
PEAK = datetime(2026, 10, 12, 1, 0, 0, tzinfo=timezone.utc)
# 北京周一 12:30（12-14 高峰间隙）：Max 窗外、claude 允许窗、zcode 全天
GAP = datetime(2026, 10, 12, 4, 30, 0, tzinfo=timezone.utc)

ALL_MAINS = list(dp.MAIN_FORCE_KEYS)
ALL_FLASH = list(dp.OVERFLOW_KEYS)
SHA = 'p' + '0' * 63
# BW-CODEBUDDY-MAIN-S3：本文件验证的是**旧层序**（三 Qoder Max > claude > zcode）与各守卫；
# CodeBuddy 现为新的最高层。为不放宽任何断言地保留旧层序验证，本文件在共享夹具里先**占用
# CodeBuddy 唯一名额**（capacity=1 置满），使其在 `_select_main_force` 因 active>=cap 被剔除，
# 于是旧层序用例继续照原断言成立；CodeBuddy 作为新最高层由独立的
# tests/test_codebuddy_reactivation.py 单独验证。
CODEBUDDY = 'codebuddy:deepseek-v4.1-flash'


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-main-prio-')
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
            # 占用 CodeBuddy 最高层唯一名额，保留旧层序断言原样成立（见模块常量注释）。
            self._seed_n += 1
            conn.execute(
                'INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                'state, child_pid, reserved_at_utc) VALUES(?,?,?,?,?,?,?,?)',
                (f'tok-cb-{self._seed_n}', f'task-cb-{self._seed_n}',
                 'codebuddy', 'deepseek-v4.1-flash', CODEBUDDY, 'reserved', None,
                 dp._iso(NIGHT)))
            conn.execute('PRAGMA journal_mode=DELETE')

    def ws(self, tid):
        d = self.base / 'ws' / str(tid)
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def seed(self, pk, n=1, state='reserved', child_pid=None):
        runtime, model = pk.split(':', 1)
        with closing(dp.connect(self.store)) as conn:
            for _ in range(n):
                self._seed_n += 1
                tok = f'tok-{self._seed_n}'
                conn.execute(
                    'INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                    'state, child_pid, reserved_at_utc) VALUES(?,?,?,?,?,?,?,?)',
                    (tok, f'task-{tok}', runtime, model, pk, state, child_pid,
                     dp._iso(NIGHT)))
            conn.execute('PRAGMA journal_mode=DELETE')

    def limit(self, pk):
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO main_force_limits(pool_key, limited) VALUES(?,1)',
                         (pk,))
            conn.execute('PRAGMA journal_mode=DELETE')

    def bump(self, pk, n):
        with closing(dp.connect(self.store)) as conn:
            for _ in range(n):
                dp._bump_rotation(conn, pk, NIGHT)
            conn.execute('PRAGMA journal_mode=DELETE')

    def select(self, requested, now, zcode_blocked=False):
        with closing(dp.connect(self.store)) as conn:
            elig = dp._eligible_main_forces(conn, zcode_blocked, now)
            return dp._select_main_force(conn, requested, now, eligible=elig)

    def reserve(self, tid, runtime, model, executor='auto', now=NIGHT, ws=None):
        return dp.reserve(self.store, task_id=tid, runtime=runtime, model=model,
                          workspace=ws or self.ws(tid), prompt_sha256=SHA, stage='s',
                          now=now, executor=executor, _preclaim=False)

    def claim_auto(self, tid, runtime='qoder', model='Qwen3.8-Max', now=NIGHT,
                   executor='auto'):
        return dp.select_and_claim(self.store, task_id=tid, runtime=runtime, model=model,
                                   workspace=self.ws(tid), prompt_sha256=SHA, stage='s',
                                   now=now, executor=executor, _preclaim=False)

    def auth(self, mode, task, stage, allowed, **kw):
        a = {'selection_mode': mode, 'quote': '用户真实原话',
             'authorization_scope': {'task_id': task, 'stage': stage},
             'allowed_candidates': allowed}
        a.update(kw)
        return a

    def plan(self, auth, task='T', stage='s2', now=NIGHT):
        return ss.plan_stage_selection(
            auth, previous=None, store_path=self.store, task_id=task, stage=stage,
            workspace=self.ws(task), now=now)


class NightMaxPriorityTests(_Base):
    def test_night_prefers_max_even_with_higher_committed_history(self):
        # 旧跨层 1:1 会让 committed 少的一侧优先；新优先级下 committed 历史绝不压过 Max 层。
        self.bump(QMAX, 5)
        target, reason = self.select(ZCODE, NIGHT)
        self.assertEqual(reason, 'main_force_selected')
        self.assertIn(target, MAXES)

    def test_night_prefers_max_even_when_requested_prefers_zcode(self):
        self.bump(ZCODE, 0)  # zcode committed 保持 0，仍绝不让低层抢高层
        target, _ = self.select(ZCODE, NIGHT)
        self.assertIn(target, MAXES)

    def test_one_max_full_or_limited_selects_another_max(self):
        self.seed(QMAX, 1)            # qoder Max 容量 1 → 满
        self.limit(CN_MAX)            # CN 内置 Max 明确限额 → 剔除
        target, _ = self.select(ZCODE, NIGHT)
        self.assertEqual(target, CN_CUSTOM_MAX)

    def test_all_max_unavailable_falls_to_claude_over_zcode(self):
        # GAP：Max 夜间窗外全部剔除；claude 允许窗且有空位 → 优先于 Z，
        # 即使 claude 有更高的历史提交（child_pid 真实启动行）且请求点名 zcode。
        self.seed(CLAUDE, 3, state='finished', child_pid=4321)
        self.bump(ZCODE, 0)
        target, reason = self.select(ZCODE, GAP)
        self.assertEqual(reason, 'main_force_selected')
        self.assertEqual(target, CLAUDE)

    def test_zcode_only_when_claude_full_or_limited_or_peak(self):
        self.seed(CLAUDE, 2)                      # claude 容量 2 → 满
        self.assertEqual(self.select(ZCODE, GAP)[0], ZCODE)

    def test_claude_history_limit_no_longer_yields_to_zcode(self):
        # BW-SIMPLE-PRIORITY-RETRY-S3：历史明确限额不再挡普通新轮——claude 带限额标记
        # 仍进候选集合，按 Max>Claude>Z 优先级仍优先于 Z（并非降回 Z）。
        self.limit(CLAUDE)
        self.assertEqual(self.select(ZCODE, GAP)[0], CLAUDE)

    def test_zcode_when_claude_peak_window_closed(self):
        # PEAK：Max 窗外 + claude 高峰门关 → 只剩 Z（ZCode 全天，不受时段门）
        self.assertEqual(self.select(ZCODE, PEAK)[0], ZCODE)

    def test_history_zcode_hold_no_longer_makes_auto_full(self):
        # BW-SIMPLE-PRIORITY-RETRY-S3：旧 availability 阻断不再把 Z 从候选剔除；PEAK 下
        # 三 Max 窗外、claude 高峰关，但 Z 全天可派 → AUTO 新轮直接落 Z，绝不再 main_force_full。
        target, reason = self.select(QMAX, PEAK, zcode_blocked=True)
        self.assertEqual(target, ZCODE)
        self.assertEqual(reason, 'main_force_selected')


class SameTierBalanceTests(_Base):
    def test_same_tier_balances_by_real_inflight_first(self):
        # BW-CUSTOM-MAX-RESUME-20261018-S1：自定义 Max 在 Max 层内先于两内置；先占满自定义
        # 2 槽，再验证两内置之间仍按真实在途平衡——国际内置已满（1/1）则选 CN 内置（0/1）。
        self.seed(CN_CUSTOM_MAX, 2)   # 自定义 2 槽满 → 落到两内置
        self.seed(QMAX, 1)            # 国际内置容量 1 → 满
        target, _ = self.select(CN_CUSTOM_MAX, NIGHT)
        self.assertEqual(target, CN_MAX)

    def test_same_tier_tie_prefers_requested_combo(self):
        target, _ = self.select(CN_CUSTOM_MAX, NIGHT)
        self.assertEqual(target, CN_CUSTOM_MAX)

    def test_same_tier_full_tie_uses_stable_pool_key_order(self):
        # 自定义 Max 占满后，两内置全空且请求低层 ZCode：同子优先键内按 pool_key stable
        # tie 仍选 qoder:Qwen3.8-Max（旧期望不变，只是先排除自定义子优先层）。
        self.seed(CN_CUSTOM_MAX, 2)
        target, _ = self.select(ZCODE, NIGHT)
        self.assertEqual(target, QMAX)

    def test_committed_history_still_recorded(self):
        # 历史计数保留：rotation 两列照旧记账，只是不再参与跨层竞争。
        self.bump(QMAX, 2)
        self.bump(ZCODE, 1)
        st = dp.status(self.store, now=NIGHT)
        self.assertEqual(st['rotation']['committed_qoder'], 2)
        self.assertEqual(st['rotation']['committed_zcode'], 1)


class WindowAndGateTests(_Base):
    def test_beijing_22_boundary_max_eligible(self):
        with closing(dp.connect(self.store)) as conn:
            self.assertIn(QMAX, dp._eligible_main_forces(conn, False, NIGHT_22))
            self.assertIn(CN_CUSTOM_MAX, dp._eligible_main_forces(conn, False, NIGHT_22))

    def test_beijing_08_boundary_max_not_eligible(self):
        with closing(dp.connect(self.store)) as conn:
            elig = dp._eligible_main_forces(conn, False, DAY_08)
        for pk in MAXES:
            self.assertNotIn(pk, elig)
        self.assertIn(ZCODE, elig)

    def test_claude_independent_offpeak_gate_unchanged(self):
        with closing(dp.connect(self.store)) as conn:
            self.assertNotIn(CLAUDE, dp._eligible_main_forces(conn, False, PEAK))
            self.assertIn(CLAUDE, dp._eligible_main_forces(conn, False, GAP))
            self.assertIn(CLAUDE, dp._eligible_main_forces(conn, False, NIGHT))

    def test_retired_deepseek_never_candidate_and_no_window_gate(self):
        self.assertNotIn(RETIRED_DS, ALL_MAINS)
        self.assertNotIn(RETIRED_DS, ALL_FLASH)
        self.assertFalse(dp._max_window_blocks(RETIRED_DS, PEAK))
        cands = dp._scope_allowed_candidates({}, False)
        self.assertNotIn(RETIRED_DS, cands)


class HighLevelDispatchTests(_Base):
    def occupy_all_mains(self):
        for i in range(2):
            self.assertTrue(self.reserve(f'z{i}', 'zcode', 'GLM-5.3', 'zcode').get('allowed'))
        self.assertTrue(self.reserve('qm', 'qoder', 'Qwen3.8-Max', 'qoder').get('allowed'))
        self.assertTrue(self.reserve('cm', 'qodercn', 'Qwen3.8-Max', 'qodercn').get('allowed'))
        for i in range(2):
            self.assertTrue(self.reserve(f'cx{i}', 'qodercn', 'Qwen-3.8-Max',
                                         'qodercn').get('allowed'))
        for i in range(2):
            self.assertTrue(self.reserve(f'cl{i}', 'claude', 'deepseek-flash',
                                         'claude').get('allowed'))
        # BW-OPENCODE-20261019：两个 OpenCode 免费模型 tier4 也在主力里，必须一并占满，
        # 否则 AUTO 会选中仍空的 OpenCode 而非兜底 Flash（旧层序断言才继续成立）。
        self.assertTrue(self.reserve('od1', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL,
                                     'opencode').get('allowed'))
        self.assertTrue(self.reserve('od2', 'opencode', dp.OPENCODE_GLM_MODEL,
                                     'opencode').get('allowed'))
        # BW-LIMIT30-20261019-S3：CBGLM tier5 额度独立始终参与候选，必须一并占满，
        # 否则 AUTO 会选中仍空的 GLM 而非兜底 Flash。
        self.assertTrue(self.reserve('cbg', 'codebuddy', dp.CODEBUDDY_GLM_MODEL,
                                     'codebuddy').get('allowed'))

    def test_flash_only_when_all_mains_unavailable(self):
        self.occupy_all_mains()
        r = self.claim_auto('auto1')
        self.assertTrue(r.get('routing_required'), r)
        self.assertIn(r['selected']['pool_key'], ALL_FLASH)

    def test_explicit_zcode_scope_not_forced_to_max(self):
        # 夜间 Max 全空，只允许 Z 的显式点名仍直接落 Z，绝不强制改派 Max。
        r = dp.select_and_claim(self.store, task_id='zonly', runtime='zcode',
                                model='GLM-5.3', workspace=self.ws('zonly'),
                                prompt_sha256=SHA, stage='s', now=NIGHT,
                                executor='zcode', _preclaim=False)
        self.assertTrue(r.get('allowed'), r)
        self.assertEqual(r['pool_key'], ZCODE)

    def test_cn_executor_never_crosses_region(self):
        cands = dp._scope_allowed_candidates({'executor': 'qodercn'}, False)
        self.assertEqual(cands, [CN_MAX, CN_CUSTOM_MAX, CN_FLASH])
        for pk in cands:
            self.assertTrue(pk.startswith('qodercn:'))

    def test_unknown_state_still_counts_inflight(self):
        self.seed(QMAX, 1, state='unknown')
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, QMAX), 1)
        target, _ = self.select(QMAX, NIGHT)
        self.assertNotEqual(target, QMAX)   # unknown 在途占容量 → 不选满池
        self.assertIn(target, MAXES)

    def test_workspace_conflict_guard_unchanged(self):
        shared = self.ws('shared')
        r1 = self.reserve('wa', 'zcode', 'GLM-5.3', 'zcode', ws=shared)
        self.assertTrue(r1.get('allowed'), r1)
        r2 = self.reserve('wb', 'zcode', 'GLM-5.3', 'zcode', ws=shared)
        self.assertFalse(r2.get('allowed'), r2)
        self.assertEqual(r2['reason'], 'workspace_in_flight')

    def test_total_capacity_and_slots(self):
        # BW-OPENCODE-20261019 当前权威总数 16：两 CodeBuddy + 两 OpenCode 免费模型各独立 1 槽。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 16)
        self.assertEqual(dp.capacity_for(CODEBUDDY), 1)
        self.assertEqual(dp.capacity_for('codebuddy:glm-5.3-flash'), 1)
        self.assertEqual(dp.capacity_for(dp.OPENCODE_DEEPSEEK_POOL_KEY), 1)
        self.assertEqual(dp.capacity_for(dp.OPENCODE_GLM_POOL_KEY), 1)
        self.assertEqual(dp.capacity_for(ZCODE), 2)
        self.assertEqual(dp.capacity_for(QMAX), 1)
        self.assertEqual(dp.capacity_for(CN_MAX), 1)
        self.assertEqual(dp.capacity_for(CN_CUSTOM_MAX), 2)
        self.assertEqual(dp.capacity_for(CLAUDE), 2)
        self.assertEqual(dp.capacity_for(QFLASH), 2)
        self.assertEqual(dp.capacity_for(CN_FLASH), 2)


class StageSelectionPriorityTests(_Base):
    def test_stage_selection_reuses_single_authoritative_selector(self):
        auth = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        with mock.patch.object(dp, '_select_main_force',
                               wraps=dp._select_main_force) as m:
            r = self.plan(auth)
        self.assertEqual(r['refused'], None, r)
        self.assertTrue(m.called, 'stage_selection 必须复用 _select_main_force')
        self.assertIn(r['selected']['pool_key'], MAXES)

    def test_stage_selection_night_max_priority_with_higher_committed(self):
        self.bump(QMAX, 5)
        auth = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        r = self.plan(auth)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected_reason'], 'main_force_selected')
        self.assertIn(r['selected']['pool_key'], MAXES)

    def test_stage_selection_claude_over_zcode_when_max_window_closed(self):
        auth = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        r = self.plan(auth, now=GAP)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CLAUDE)

    def test_stage_selection_zcode_when_claude_peak_closed(self):
        auth = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        r = self.plan(auth, now=PEAK)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], ZCODE)

    def test_stage_selection_fixed_flash_not_forced_to_max(self):
        auth = self.auth('fixed', 'T', 's2', [QFLASH], runtime='qoder',
                         model='Qwen3.8-Flash')
        auth['user_direct_selection'] = True
        r = self.plan(auth)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], QFLASH)

    def test_stage_selection_executor_cn_never_crosses_region(self):
        auth = self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH,
                         executor='qodercn')
        auth['user_direct_selection'] = True
        r = self.plan(auth, now=GAP)   # GAP：CN 两 Max 窗外 → 只落 CN Flash
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected_reason'], 'executor_region_flash', r)
        self.assertEqual(r['selected']['pool_key'], CN_FLASH)


if __name__ == '__main__':
    unittest.main()
