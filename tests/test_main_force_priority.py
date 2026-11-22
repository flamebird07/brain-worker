"""BW-MAIN-PRIORITY-20261011-S1 离线回归：主力优先级 Max > Z（GLM）> Claude，覆盖旧跨层 1:1。

只用临时 SQLite（每用例独立 store）+ mock ZCode availability / process_identity，零真实
模型 / 零凭据 / 零真实池 / 零网络。断言：
- 夜间两个 CN Max 优先（即使其历史 committed 更高、请求偏好别池）；
- 一个 CN Max 满/限额时选另一 CN Max，绝不落到 Z/Claude；
- CN Max 全不可用时，ZCode（层=2，BW-ZCODE-RESTORE-1-20261022 恢复可派、容量 1）优先于
  Claude（层=3）；Z 满/占位时才 Claude；
- 同层内保持现有在途平衡与 tie 方式；rotation 历史计数照旧保留；
- 全部主力无可用槽才 Flash；只允许 Z / 固定 Flash 时不强制 Max；CN 约束不跨区；
- 15 总槽（BW-OPENCODE-20261019 起两 CodeBuddy + 两 OpenCode 免费模型各独立 1 槽；
  BW-ZCODE-RESTORE-1-20261022：ZCode 恢复为正常可派主力、容量严格 1；
  BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023：CodeBuddy deepseek 层=5，其唯一名额仍由
  build_store 预占以保留旧层序断言）、unknown 在途、workspace 冲突、北京 22/08 边界与
  claude 全天（无时段门）不变；
- POOL_PRIORITY 权威数字（BW-WEAK-RESTRUCTURE-20261116 起弱档新表，与 dm.WEAK_PRIORITY
  同一数值）：两 OpenCode=1 > codebuddy glm=2 > claude=3 > zcode:GLM-5.3-Flash=4；
  全部 Qoder 池与 codebuddy deepseek、zcode:GLM-5.3 已移出弱档（mode_excluded）；
  Max/zcode 层序场景改在 medium（Qwen4×2=1 > claude=2 > zcode=3）复现；
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

import dispatch_mode as dm  # noqa: E402
import dispatch_pool as dp  # noqa: E402
import stage_selection as ss  # noqa: E402
from dispatch_mode_support import dispatch_mode, user_disabled_pools  # noqa: E402

QMAX = 'qoder:Qwen4-Max-Preview'
CN_MAX = 'qodercn:Qwen4-Max-Preview'
CN_CUSTOM_MAX = 'qodercn:Qwen-3.8-Max'
CLAUDE = 'claude:deepseek-flash'
ZCODE = 'zcode:GLM-5.3'  # BW-ZCODE-RESTORE-1-20261022：恢复可派主力（容量 1、层=2）
OC_DS = dp.OPENCODE_DEEPSEEK_POOL_KEY
OC_GLM = dp.OPENCODE_GLM_POOL_KEY
OPENCODES = (OC_DS, OC_GLM)
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
# BW-CODEBUDDY-MAIN-S3 + BW-CB-REORDER-20261020 + BW-ZCODE-RESTORE-1-20261022：本文件
# 验证的是三 Qoder Max > ZCode > claude 的层序与各守卫；CodeBuddy deepseek 现为层=4
#（ZCode/claude 之后、两 OpenCode 之前）。为不放宽任何断言地保留既有层序验证（如 claude
# 满后直接落 OpenCode、绝不被 CodeBuddy 空位截胡），本文件在共享夹具里先**占用 CodeBuddy
# 唯一名额**（capacity=1 置满），使其在 `_select_main_force` 因 active>=cap 被剔除；
# CodeBuddy 层=4 的新位置由独立的 tests/test_codebuddy_reactivation.py 单独验证。
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
            # 占用 CodeBuddy（层=4）唯一名额，保留既有层序断言原样成立（见模块常量注释）。
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

    def claim_auto(self, tid, runtime='qoder', model='Qwen4-Max-Preview', now=NIGHT,
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
        # BW-WEAK-RESTRUCTURE-20261116: 弱档已无任何 Qoder Max/zcode:GLM-5.3——改在
        # medium 档（Qwen4×2(1) > claude(2) > zcode(3)）验证跨层不变量。
        self.bump(QMAX, 5)
        with dispatch_mode('medium'):
            target, reason = self.select(ZCODE, NIGHT)
        self.assertEqual(reason, 'main_force_selected')
        self.assertIn(target, MAXES)

    def test_night_prefers_max_even_when_requested_prefers_zcode(self):
        # BW-WEAK-RESTRUCTURE-20261116: 同上改 medium 档验证。
        self.bump(ZCODE, 0)  # zcode committed 保持 0，仍绝不让低层抢高层
        with dispatch_mode('medium'):
            target, _ = self.select(ZCODE, NIGHT)
        self.assertIn(target, MAXES)

    def test_one_max_full_or_limited_selects_another_max(self):
        # BW-QODERCN-TOKEN-MAX-DISABLE-20261028：三 Max 互相兜底的层内语义属禁用前
        # 历史口径——目标池现行被用户禁用，这里临时用空禁用集复现历史层内兜底场景。
        # BW-WEAK-RESTRUCTURE-20261116: 弱档已移出全部 Max——改在 medium 档复现。
        with user_disabled_pools(frozenset()), dispatch_mode('medium'):
            self.seed(QMAX, 1)        # qoder Max 容量 1 → 满
            self.limit(CN_MAX)        # CN 内置 Max 明确限额 → 剔除
            target, _ = self.select(ZCODE, NIGHT)
            self.assertEqual(target, CN_CUSTOM_MAX)

    def test_claude_precedes_zcode_when_both_free(self):
        # BW-WEAK-RESTRUCTURE-20261116: claude > zcode 层序属 strong/medium——medium 档
        # GAP 时刻（Max 夜窗关、claude 午休开）两池同时可派时 claude 优先。
        self.seed(QMAX, 1)
        self.seed(CLAUDE, 3, state='finished', child_pid=4321)
        self.bump(ZCODE, 0)
        with dispatch_mode('medium'):
            target, reason = self.select(ZCODE, GAP)
        self.assertEqual(reason, 'main_force_selected')
        self.assertEqual(target, CLAUDE)

    def test_claude_when_zcode_full(self):
        # ZCode 容量严格 1：占满后（GAP，Max 夜窗关、国际 Max 已占满）按层序落到
        # claude:deepseek-flash。BW-WEAK-RESTRUCTURE-20261116: 改 medium 档验证。
        self.seed(QMAX, 1)
        self.seed(ZCODE, 1)                       # zcode 容量 1 → 满
        with dispatch_mode('medium'):
            self.assertEqual(self.select(ZCODE, GAP)[0], CLAUDE)

    def test_claude_history_limit_no_longer_yields_to_zcode(self):
        # BW-SIMPLE-PRIORITY-RETRY-S3：历史明确限额不再挡普通新轮——claude 带限额标记
        # 仍进候选集合；ZCode 唯一槽占满后（层内无空位、Max 夜窗关）按层序落到
        # claude（并非跳过）。BW-WEAK-RESTRUCTURE-20261116: 改 medium 档验证。
        self.seed(QMAX, 1)
        self.limit(CLAUDE)
        self.seed(ZCODE, 1)
        with dispatch_mode('medium'):
            self.assertEqual(self.select(ZCODE, GAP)[0], CLAUDE)

    def test_weak_peak_opencode_precedes_zcode(self):
        self.seed(QMAX, 1)
        self.assertIn(self.select(ZCODE, PEAK)[0], dp.OPENCODE_POOL_KEYS)

    def test_history_zcode_hold_no_longer_makes_auto_full(self):
        # BW-WEAK-RESTRUCTURE-20261116: 弱档已移出 zcode:GLM-5.3——改在 medium 档
        # （PEAK 时刻 Max 夜窗关、claude 高峰门关，zcode 是唯一合格主力）验证历史
        # availability 阻断不再把 AUTO 打成 full。
        self.seed(QMAX, 1)
        for key in (*dp.OPENCODE_POOL_KEYS, dp.CODEBUDDY_GLM_POOL_KEY):
            self.seed(key, 1)
        with dispatch_mode('medium'):
            target, reason = self.select(ZCODE, PEAK, zcode_blocked=True)
        self.assertEqual(target, ZCODE)
        self.assertEqual(reason, 'main_force_selected')


class SameTierBalanceTests(_Base):
    def test_same_tier_balances_by_real_inflight_first(self):
        # BW-CUSTOM-MAX-RESUME-20261018-S1：自定义 Max 在 Max 层内先于两内置；先占满自定义
        # 2 槽，再验证两内置之间仍按真实在途平衡——国际内置已满（1/1）则选 CN 内置（0/1）。
        # BW-WEAK-RESTRUCTURE-20261116: Max 层内语义属 strong/medium——改 medium 档复现。
        self.seed(CN_CUSTOM_MAX, 2)   # 自定义 2 槽满 → 落到两内置
        self.seed(QMAX, 1)            # 国际内置容量 1 → 满
        with user_disabled_pools(frozenset()), dispatch_mode('medium'):
            target, _ = self.select(CN_CUSTOM_MAX, NIGHT)
        self.assertEqual(target, CN_MAX)

    def test_same_tier_tie_prefers_requested_combo(self):
        # 同上：自定义 Max 参与层内竞争属禁用前历史口径，临时空禁用集复现历史场景。
        # BW-WEAK-RESTRUCTURE-20261116: 改 medium 档复现（弱档已无任何 Max）。
        with user_disabled_pools(frozenset()), dispatch_mode('medium'):
            target, _ = self.select(CN_CUSTOM_MAX, NIGHT)
            self.assertEqual(target, CN_CUSTOM_MAX)

    def test_same_tier_full_tie_uses_stable_pool_key_order(self):
        # 自定义 Max 占满后，CN 内置 Max（同层=1）唯一有空 → 选中它。
        # BW-WEAK-RESTRUCTURE-20261116: Max 层内语义改 medium 档复现；国际 Max 同层
        # 子优先键更小会先于 CN 内置，故同时占满国际 Max 让 CN 内置成为唯一层内空位。
        self.seed(CN_CUSTOM_MAX, 2)
        self.seed(QMAX, 1)
        with dispatch_mode('medium'):
            target, _ = self.select(ZCODE, NIGHT)
        self.assertEqual(target, CN_MAX)

    def test_committed_history_still_recorded(self):
        # 历史计数保留：rotation 两列照旧记账，只是不再参与跨层竞争。
        self.bump(QMAX, 2)
        self.bump(ZCODE, 1)
        st = dp.status(self.store, now=NIGHT)
        self.assertEqual(st['rotation']['committed_qoder'], 2)
        self.assertEqual(st['rotation']['committed_zcode'], 1)


class WindowAndGateTests(_Base):
    def test_beijing_22_boundary_max_eligible(self):
        # 历史三 Max 夜间门口径（目标池现行被用户禁用，临时空禁用集复现历史语义）。
        # BW-WEAK-RESTRUCTURE-20261116: Max 已全部移出弱档——改在 medium 档验证 22:00
        # 左闭边界（夜窗开 → Max 合格）。
        with user_disabled_pools(frozenset()), dispatch_mode('medium'), \
                closing(dp.connect(self.store)) as conn:
            self.assertIn(QMAX, dp._eligible_main_forces(conn, False, NIGHT_22))
            self.assertIn(CN_CUSTOM_MAX, dp._eligible_main_forces(conn, False, NIGHT_22))

    def test_beijing_08_boundary_max_not_eligible(self):
        with closing(dp.connect(self.store)) as conn:
            elig = dp._eligible_main_forces(conn, False, DAY_08)
        # weak（默认）档：三个 Max 在 08:00 全部被恢复后的夜间门挡下。
        for pk in MAXES:
            self.assertNotIn(pk, elig)
        # BW-WEAK-RESTRUCTURE-20261116: 弱档可派集合收窄——zcode:GLM-5.3 移出弱档
        # （mode_excluded）；08:00 claude 午前离峰开、其余弱档池（含 zcode
        # GLM-5.3-Flash，已入 MAIN_FORCE_POOLS）无时段门。锁定真实合格集合。
        self.assertNotIn(ZCODE, elig)
        self.assertEqual(elig, {OC_DS, OC_GLM, dp.CODEBUDDY_GLM_POOL_KEY, CLAUDE,
                                dp.ZCODE_FLASH_POOL_KEY})
        # BW-ZCODE-RESTORE-1-20261022：medium 档 ZCode 无时段门，白天照常在候选
        # （Max 夜窗 08:00 右开 → 三个 Max 仍被挡）。
        with dispatch_mode('medium'), closing(dp.connect(self.store)) as conn:
            elig_m = dp._eligible_main_forces(conn, False, DAY_08)
        self.assertIn(ZCODE, elig_m)
        for pk in MAXES:
            self.assertNotIn(pk, elig_m)
        # strong 档：三个 Max 全部无时段门 → 08:00 也在候选里（三 Max 时代历史口径，
        # 目标池现行被用户禁用，临时空禁用集复现）。
        with user_disabled_pools(frozenset()), dispatch_mode('strong'), \
                closing(dp.connect(self.store)) as conn:
            elig_s = dp._eligible_main_forces(conn, False, DAY_08)
        for pk in MAXES:
            self.assertIn(pk, elig_s)

    def test_claude_gate_is_mode_specific(self):
        # BW-MANUAL-THREE-MODE-20261023：strong 档 claude 全天可派（工作日高峰/午间/夜间/
        # 08:00 全部在候选里）；weak/medium 恢复工作日高峰门 → PEAK 被挡、GAP/NIGHT 保留。
        with dispatch_mode('strong'), closing(dp.connect(self.store)) as conn:
            for now in (PEAK, GAP, NIGHT, DAY_08):
                self.assertIn(CLAUDE, dp._eligible_main_forces(conn, False, now), now)
        for mode in ('weak', 'medium'):
            with dispatch_mode(mode), closing(dp.connect(self.store)) as conn:
                self.assertNotIn(CLAUDE, dp._eligible_main_forces(conn, False, PEAK), mode)
                for now in (GAP, NIGHT, DAY_08):
                    self.assertIn(CLAUDE, dp._eligible_main_forces(conn, False, now),
                                  (mode, now))
        self.assertIn(CLAUDE, dp.WINDOW_GATED_MAIN_FORCES)
        self.assertEqual(tuple(dp.window_gated_pools('strong')), ())

    def test_retired_deepseek_never_candidate_and_no_window_gate(self):
        self.assertNotIn(RETIRED_DS, ALL_MAINS)
        self.assertNotIn(RETIRED_DS, ALL_FLASH)
        self.assertFalse(dp._max_window_blocks(RETIRED_DS, PEAK))
        cands = dp._scope_allowed_candidates({}, False)
        self.assertNotIn(RETIRED_DS, cands)


class HighLevelDispatchTests(_Base):
    def occupy_all_mains(self):
        # BW-WEAK-RESTRUCTURE-20261116: 弱档 AUTO 可选主力 = Claude／两 OpenCode／
        # CodeBuddy GLM／ZCode GLM-5.3-Flash（全部 Qoder 池、codebuddy deepseek、
        # zcode:GLM-5.3 已移出弱档；zcode:GLM-5.3-Flash 已入 MAIN_FORCE_POOLS 可真实
        # claim）。「全部主力占满」按弱档真实可占面逐一真实 claim。
        self.assertTrue(self.reserve('cl0', 'claude', 'deepseek-flash',
                                     'claude').get('allowed'))
        self.assertTrue(self.reserve('cl1', 'claude', 'deepseek-flash',
                                     'claude').get('allowed'))
        self.assertTrue(self.reserve('od1', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL,
                                     'opencode').get('allowed'))
        self.assertTrue(self.reserve('od2', 'opencode', dp.OPENCODE_GLM_MODEL,
                                     'opencode').get('allowed'))
        self.assertTrue(self.reserve('cbg', 'codebuddy', dp.CODEBUDDY_GLM_MODEL,
                                     'codebuddy').get('allowed'))
        self.assertTrue(self.reserve('zf1', 'zcode', 'GLM-5.3-Flash',
                                     'zcode').get('allowed'))

    def test_flash_only_when_all_mains_unavailable(self):
        # 旧口径：全部主力占满 → 兜底两地区 Flash。BW-WEAK-RESTRUCTURE-20261116: 两
        # Flash（qoder/qodercn:Qwen3.8-Flash）已从**所有档位**（含弱档）移除新派资格
        # ——弱档主力占满后绝不静默换模型，如实 mode_excluded（sent=false、零新占位）。
        self.occupy_all_mains()
        r = self.claim_auto('auto1', runtime='claude', model='deepseek-flash')
        self.assertFalse(r.get('allowed'), r)
        self.assertFalse(r.get('sent'), r)
        self.assertEqual(r['reason'], 'mode_excluded', r)
        with closing(dp.connect(self.store)) as conn:
            n = conn.execute('SELECT COUNT(*) AS n FROM attempts '
                             "WHERE task_id='auto1'").fetchone()['n']
        self.assertEqual(n, 0, n)

    def test_explicit_zcode_scope_claims_zcode_itself(self):
        # BW-ZCODE-RESTORE-1-20261022：显式 executor=zcode 恢复为权威原子门——有空位
        # （容量 1）直接 claim 自己，绝不强制改派 Max、绝不改派别池；第二个同组合在
        # 容量满时如实拒绝（full，绝不回落 Flash）。
        # BW-WEAK-RESTRUCTURE-20261116: zcode:GLM-5.3 已移出弱档——改在 medium 档验证。
        with dispatch_mode('medium'):
            r = dp.select_and_claim(self.store, task_id='zonly', runtime='zcode',
                                    model='GLM-5.3', workspace=self.ws('zonly'),
                                    prompt_sha256=SHA, stage='s', now=NIGHT,
                                    executor='zcode', _preclaim=False)
            self.assertTrue(r.get('allowed'), r)
            self.assertEqual(r['pool_key'], ZCODE, r)
            second = dp.select_and_claim(self.store, task_id='zonly2', runtime='zcode',
                                         model='GLM-5.3', workspace=self.ws('zonly2'),
                                         prompt_sha256=SHA, stage='s', now=NIGHT,
                                         executor='zcode', _preclaim=False)
        self.assertFalse(second.get('allowed'), second)
        self.assertEqual(second['reason'], 'capacity_full', second)
        self.assertFalse(second.get('routing_required'), second)

    def test_cn_executor_never_crosses_region(self):
        cands = dp._scope_allowed_candidates({'executor': 'qodercn'}, False)
        self.assertEqual(cands, [CN_MAX, CN_CUSTOM_MAX, CN_FLASH])
        for pk in cands:
            self.assertTrue(pk.startswith('qodercn:'))

    def test_unknown_state_still_counts_inflight(self):
        # BW-WEAK-RESTRUCTURE-20261116: Max 层内语义改 medium 档复现（弱档已无 Max）。
        self.seed(QMAX, 1, state='unknown')
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, QMAX), 1)
        with dispatch_mode('medium'):
            target, _ = self.select(QMAX, NIGHT)
        self.assertNotEqual(target, QMAX)   # unknown 在途占容量 → 不选满池
        self.assertIn(target, MAXES)

    def test_workspace_conflict_guard_unchanged(self):
        # 同工作区单写守卫语义原样：两个不同 task 共享同一真实 workspace 时第二个拒绝。
        shared = self.ws('shared')
        r1 = self.reserve('wa', 'claude', 'deepseek-flash', 'claude', ws=shared)
        self.assertTrue(r1.get('allowed'), r1)
        r2 = self.reserve('wb', 'claude', 'deepseek-flash', 'claude', ws=shared)
        self.assertFalse(r2.get('allowed'), r2)
        self.assertEqual(r2['reason'], 'workspace_in_flight')

    def test_total_capacity_and_slots(self):
        # BW-WEAK-RESTRUCTURE-20261116 当前权威物理总数 17：原 16 槽 + 弱档新独立池
        # `zcode:GLM-5.3-Flash` 独立 1 槽（与 strong/medium 的 zcode:GLM-5.3 互不占位）。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 17)
        self.assertEqual(dp.capacity_for(dp.SONUS_POOL_KEY), 1)
        self.assertEqual(dp.capacity_for(dp.ZCODE_FLASH_POOL_KEY), 1)
        # dm.ALL_POOL_KEYS = 全档位策略池并集 12 池：容量合计 = 物理 17 − strong 专属
        # Sonus 1 = 16；弱档可新派容量 = 6。
        self.assertEqual(sum(dp.capacity_for(k) for k in dm.ALL_POOL_KEYS), 16)
        self.assertEqual(sum(dp.capacity_for(k) for k in dm.WEAK_POOL_KEYS), 6)
        self.assertEqual(dp.capacity_for(CODEBUDDY), 1)
        self.assertEqual(dp.capacity_for('codebuddy:glm-5.3-flash'), 1)
        self.assertEqual(dp.capacity_for(dp.OPENCODE_DEEPSEEK_POOL_KEY), 1)
        self.assertEqual(dp.capacity_for(dp.OPENCODE_GLM_POOL_KEY), 1)
        self.assertEqual(dp.capacity_for(ZCODE), 1)
        self.assertIn(ZCODE, dp.MAIN_FORCE_KEYS)
        # BW-WEAK-RESTRUCTURE-20261116: POOL_PRIORITY 是 dm.WEAK_PRIORITY 的兼容别名
        # （单一来源 dispatch_mode，同一数值），弱档层序重新连续编号。
        self.assertEqual(dp.POOL_PRIORITY, dm.WEAK_PRIORITY)
        self.assertEqual(dp.POOL_PRIORITY, {
            dp.OPENCODE_DEEPSEEK_POOL_KEY: 1, dp.OPENCODE_GLM_POOL_KEY: 1,
            'codebuddy:glm-5.3-flash': 2,
            CLAUDE: 3,
            dp.ZCODE_FLASH_POOL_KEY: 4})
        # 弱档新层序：两 OpenCode 同层第一 > CodeBuddy GLM > Claude > ZCode Flash。
        self.assertLess(dp.POOL_PRIORITY[dp.OPENCODE_DEEPSEEK_POOL_KEY],
                        dp.POOL_PRIORITY['codebuddy:glm-5.3-flash'])
        self.assertLess(dp.POOL_PRIORITY['codebuddy:glm-5.3-flash'],
                        dp.POOL_PRIORITY[CLAUDE])
        self.assertLess(dp.POOL_PRIORITY[CLAUDE], dp.POOL_PRIORITY[dp.ZCODE_FLASH_POOL_KEY])
        # 被移出弱档的池不再持有弱档层号（weak 一律 mode_excluded，绝不静默换模型）。
        self.assertNotIn(CODEBUDDY, dp.POOL_PRIORITY)
        self.assertNotIn(ZCODE, dp.POOL_PRIORITY)
        self.assertNotIn(QMAX, dp.POOL_PRIORITY)
        self.assertNotIn(CN_MAX, dp.POOL_PRIORITY)
        self.assertNotIn(CN_CUSTOM_MAX, dp.POOL_PRIORITY)
        self.assertNotIn(QFLASH, dp.POOL_PRIORITY)
        self.assertNotIn(CN_FLASH, dp.POOL_PRIORITY)
        # 容量登记不变（被移出弱档的池仍真实登记、历史在途照常计数结算）。
        self.assertEqual(dp.capacity_for(QMAX), 1)
        self.assertEqual(dp.capacity_for(CN_MAX), 1)
        self.assertEqual(dp.capacity_for(CN_CUSTOM_MAX), 2)
        self.assertEqual(dp.capacity_for(CLAUDE), 2)
        self.assertEqual(dp.capacity_for(QFLASH), 2)
        self.assertEqual(dp.capacity_for(CN_FLASH), 2)


class StageSelectionPriorityTests(_Base):
    def test_stage_selection_reuses_single_authoritative_selector(self):
        # BW-WEAK-RESTRUCTURE-20261116: 弱档已无任何 Max——改在 medium 档（Max 夜窗内）
        # 验证 stage_selection 复用单一权威选择器与层序。
        auth = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        with dispatch_mode('medium'), \
                mock.patch.object(dp, '_select_main_force',
                                  wraps=dp._select_main_force) as m:
            r = self.plan(auth)
        self.assertEqual(r['refused'], None, r)
        self.assertTrue(m.called, 'stage_selection 必须复用 _select_main_force')
        self.assertIn(r['selected']['pool_key'], MAXES)

    def test_stage_selection_night_max_priority_with_higher_committed(self):
        # BW-WEAK-RESTRUCTURE-20261116: 同上改 medium 档验证。
        self.bump(QMAX, 5)
        auth = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        with dispatch_mode('medium'):
            r = self.plan(auth)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected_reason'], 'main_force_selected')
        self.assertIn(r['selected']['pool_key'], MAXES)

    def test_stage_selection_claude_when_max_window_closed(self):
        # BW-WEAK-RESTRUCTURE-20261116: Max 夜窗关的层序回落属 medium 档——改 medium
        # 验证（GAP：Max 窗外、claude 午休开）。
        self.seed(QMAX, 1)
        auth = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        with dispatch_mode('medium'):
            r = self.plan(auth, now=GAP)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CLAUDE)

    def test_stage_selection_claude_when_zcode_full(self):
        # ZCode 唯一槽占满后（GAP，Max 夜窗关），按层序落到 claude:deepseek-flash。
        # BW-WEAK-RESTRUCTURE-20261116: 改 medium 档验证。
        self.seed(QMAX, 1)
        self.seed(ZCODE, 1)
        auth = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        with dispatch_mode('medium'):
            r = self.plan(auth, now=GAP)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CLAUDE)

    def test_stage_selection_keeps_zcode_eligible_at_lowest_priority(self):
        # BW-WEAK-RESTRUCTURE-20261116: 弱档不再有 zcode:GLM-5.3（mode_excluded），弱档
        # 高峰时刻先落两 OpenCode；「zcode 最低层仍合格」的层序语义改在 medium 档验证
        # （PEAK：Max 夜窗关、claude 高峰门关，zcode 是唯一合格主力）。
        self.seed(QMAX, 1)
        auth = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        r = self.plan(auth, now=PEAK)
        self.assertEqual(r['refused'], None, r)
        self.assertIn(r['selected']['pool_key'], dp.OPENCODE_POOL_KEYS)
        # 弱档 zcode:GLM-5.3 已移出——如实 mode_excluded，绝不冒充 not_authorized。
        self.assertEqual(r['candidate_filters'].get(ZCODE), 'mode_excluded', r)
        with dispatch_mode('medium'):
            r_m = self.plan(auth, now=PEAK)
        self.assertEqual(r_m['refused'], None, r_m)
        self.assertEqual(r_m['selected']['pool_key'], ZCODE, r_m)
        self.assertEqual(r_m['candidate_filters'].get(ZCODE), 'eligible', r_m)

    def test_stage_selection_fixed_flash_not_forced_to_max(self):
        # BW-WEAK-RESTRUCTURE-20261116: 两地区 Flash 已从所有档位移除新派资格——固定
        # 点名 Flash 在弱档被如实拒绝（fixed_candidate_unavailable + mode_excluded），
        # 绝不强制改派 Max、绝不静默换模型。
        auth = self.auth('fixed', 'T', 's2', [QFLASH], runtime='qoder',
                         model='Qwen3.8-Flash')
        auth['user_direct_selection'] = True
        r = self.plan(auth)
        self.assertIsNone(r['selected'], r)
        self.assertEqual(r['refused'], 'fixed_candidate_unavailable', r)
        self.assertEqual(r['candidate_filters'][QFLASH], 'mode_excluded', r)

    def test_stage_selection_executor_cn_never_crosses_region(self):
        # executor 只在本 runtime 内、绝不跨区。
        # BW-WEAK-RESTRUCTURE-20261116: CN Flash 已移出所有档位——GAP（CN Max 窗外、
        # CN Flash mode_excluded）如实无候选，绝不跨区换 qoder:*；夜窗内仍选回 CN 内置
        # Max（executor_region_main），同样绝不跨到国际 Max。
        auth = self.auth('executor', 'T', 's2', ALL_MAINS + ALL_FLASH,
                         executor='qodercn')
        auth['user_direct_selection'] = True
        with dispatch_mode('medium'):
            r = self.plan(auth, now=GAP)   # GAP：CN 两 Max 窗外 → CN Flash 已移出该档
            self.assertIsNone(r['selected'], r)
            self.assertEqual(r['refused'], 'no_eligible_candidate', r)
            r2 = self.plan(auth, now=NIGHT)
        self.assertEqual(r2['refused'], None, r2)
        self.assertEqual(r2['selected_reason'], 'executor_region_main', r2)
        self.assertEqual(r2['selected']['pool_key'], CN_MAX, r2)


class ClaudeThenIntlMaxOrderTests(_Base):
    """BW-WEAK-RESTRUCTURE-20261116：弱档已移出全部 Qoder Max（mode_excluded）——
    「Claude 高于国际 Qoder Max」的弱档跨层序不复存在；claude 仍是弱档第 3 层，
    国际 Max 的层序语义（夜间门内可用、与 claude 的先后）改在 medium 档验证
    （medium：Max(1) > Claude(2) > ZCode(3)，Max 夜间门与 Claude 高峰门都已恢复）。

    weak 档下「claude 与三个 Max 同时授权」时：claude 照常可选，Max 一律如实
    mode_excluded（绝不静默换模型、绝不冒充 not_authorized / capacity_full）。"""

    OFFPEAK_1230 = datetime(2026, 10, 12, 4, 30, 0, tzinfo=timezone.utc)  # 北京周一 12:30
    OFFPEAK_1300 = datetime(2026, 10, 12, 5, 0, 0, tzinfo=timezone.utc)   # 北京周一 13:00
    DAY_1000 = datetime(2026, 10, 12, 2, 0, 0, tzinfo=timezone.utc)   # 北京周一 10:00 高峰
    DAY_1500 = datetime(2026, 10, 12, 7, 0, 0, tzinfo=timezone.utc)   # 北京周一 15:00 高峰

    def test_weak_offpeak_claude_eligible(self):
        for now in (self.OFFPEAK_1230, self.OFFPEAK_1300):
            with self.subTest(now=now):
                auth = self.auth('auto', 'T', 's2', [CLAUDE])
                r = self.plan(auth, now=now)
                self.assertEqual(r['refused'], None, r)
                self.assertEqual(r['selected']['pool_key'], CLAUDE, r)
                self.assertEqual(r['candidate_filters'][CLAUDE], 'eligible', r)

    def test_strong_peak_claude_eligible(self):
        # strong：工作日高峰也无门（claude 不套高峰门）。
        for now in (self.DAY_1000, self.DAY_1500):
            with self.subTest(now=now), dispatch_mode('strong'):
                auth = self.auth('auto', 'T', 's2', [CLAUDE])
                r = self.plan(auth, now=now)
                self.assertEqual(r['refused'], None, r)
                self.assertEqual(r['selected']['pool_key'], CLAUDE, r)
                self.assertEqual(r['candidate_filters'][CLAUDE], 'eligible', r)

    def test_weak_peak_claude_is_window_closed(self):
        # weak（默认）：工作日高峰恢复高峰门 → claude 如实 window_closed（不是 mode_excluded）。
        for now in (self.DAY_1000, self.DAY_1500):
            with self.subTest(now=now):
                auth = self.auth('auto', 'T', 's2', [CLAUDE])
                r = self.plan(auth, now=now)
                self.assertEqual(r['candidate_filters'][CLAUDE], 'window_closed', r)
                self.assertEqual(r['refused'], 'no_eligible_candidate', r)

    def test_auto_authorized_both_picks_claude(self):
        # BW-WEAK-RESTRUCTURE-20261116: 弱档同时授权 claude + 国际 Max → claude 照常
        # 当选（层=3）；国际 Max 不再持有弱档层号，如实 mode_excluded（无 next_candidate）。
        auth = self.auth('auto', 'T', 's2', [CLAUDE, QMAX])
        r = self.plan(auth, now=NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CLAUDE, r)
        self.assertEqual(r['selected_priority'], 3, r)
        self.assertIsNone(r['next_candidate'], r)
        self.assertEqual(r['candidate_filters'][QMAX], 'mode_excluded', r)

    def test_auto_falls_to_intl_max_when_claude_full(self):
        # 旧口径：弱档 claude(3) 满后跨层落到国际 Max(4)。BW-WEAK-RESTRUCTURE-20261116:
        # 弱档 Max 已全部移出——「claude 满后仍选合格的国际 Max」改在 medium 档验证
        # （claude(2) 容量 2 占满 → 层内更高优先的国际 Max(1) 当选，绝不静默换模型）。
        self.seed(CLAUDE, 2)
        auth = self.auth('auto', 'T', 's2', [CLAUDE, QMAX])
        with dispatch_mode('medium'):
            r = self.plan(auth, now=NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], QMAX, r)
        self.assertEqual(r['selected_priority'], 1, r)
        self.assertEqual(r['candidate_filters'][CLAUDE], 'capacity_full', r)

    def test_intl_max_full_does_not_block_claude(self):
        # BW-WEAK-RESTRUCTURE-20261116: 国际 Max 满不挡 claude 的层序语义改在 medium 档
        # 验证（Max(1) 满 → Claude(2) 当选；满池如实 capacity_full，绝不冒充排除）。
        self.seed(QMAX, 1)
        auth = self.auth('auto', 'T', 's2', [CLAUDE, QMAX])
        with dispatch_mode('medium'):
            r = self.plan(auth, now=NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CLAUDE, r)
        self.assertEqual(r['candidate_filters'][QMAX], 'capacity_full', r)


if __name__ == '__main__':
    unittest.main()
