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
- POOL_PRIORITY 权威数字（BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023）：两 CN Max=1 > zcode=2 >
  claude=3 > 国际 Qoder Max=4 > codebuddy deepseek=5 > 两 OpenCode=6 > codebuddy glm=7 >
  两区 Flash=8；
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

QMAX = 'qoder:Qwen3.8-Max'
CN_MAX = 'qodercn:Qwen3.8-Max'
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
        # BW-QODERCN-TOKEN-MAX-DISABLE-20261028：三 Max 互相兜底的层内语义属禁用前
        # 历史口径——目标池现行被用户禁用，这里临时用空禁用集复现历史层内兜底场景。
        with user_disabled_pools(frozenset()):
            self.seed(QMAX, 1)        # qoder Max 容量 1 → 满
            self.limit(CN_MAX)        # CN 内置 Max 明确限额 → 剔除
            target, _ = self.select(ZCODE, NIGHT)
            self.assertEqual(target, CN_CUSTOM_MAX)

    def test_zcode_restored_preferred_over_claude_when_both_free(self):
        # BW-ZCODE-RESTORE-1-20261022：GAP（两个 CN Max 窗外）下 ZCode（层=2）与 claude
        # （层=3）都有空位 → 按新层序 ZCode 优先于 Claude，即使 claude 有更高的历史提交
        # （child_pid 真实启动行）且请求点名 zcode。BW-QODER-INTL-MAX-ALLDAY-20261023 起
        # 国际 Max 白天也可派，故先占满其唯一名额，层=2 才是最高可用层。
        self.seed(QMAX, 1)
        self.seed(CLAUDE, 3, state='finished', child_pid=4321)
        self.bump(ZCODE, 0)
        target, reason = self.select(ZCODE, GAP)
        self.assertEqual(reason, 'main_force_selected')
        self.assertEqual(target, ZCODE)

    def test_claude_when_zcode_full(self):
        # ZCode 容量严格 1：占满后（GAP，两个 CN Max 窗外、国际 Max 已占满）按层序落到
        # claude:deepseek-flash。
        self.seed(QMAX, 1)
        self.seed(ZCODE, 1)                       # zcode 容量 1 → 满
        self.assertEqual(self.select(ZCODE, GAP)[0], CLAUDE)

    def test_claude_history_limit_no_longer_yields_to_zcode(self):
        # BW-SIMPLE-PRIORITY-RETRY-S3：历史明确限额不再挡普通新轮——claude 带限额标记
        # 仍进候选集合；ZCode 唯一槽占满后（层=2 无空位、国际 Max 已占满）按层序落到
        # claude（并非跳过）。
        self.seed(QMAX, 1)
        self.limit(CLAUDE)
        self.seed(ZCODE, 1)
        self.assertEqual(self.select(ZCODE, GAP)[0], CLAUDE)

    def test_zcode_before_claude_when_both_free(self):
        # PEAK：两个 CN Max 窗外 + 国际 Max 已占满 → ZCode（层=2）直接选中，先于 claude（3）；
        # CodeBuddy deepseek 夹具已占、GLM tier 7 更低。
        self.seed(QMAX, 1)
        self.assertEqual(self.select(ZCODE, PEAK)[0], ZCODE)

    def test_history_zcode_hold_no_longer_makes_auto_full(self):
        # BW-SIMPLE-PRIORITY-RETRY-S3：旧 availability 阻断不再改变候选集合；PEAK 下
        # 两个 CN Max 窗外、国际 Max 已占满，ZCode 恢复可派（BW-ZCODE-RESTORE-1-20261022）
        # → AUTO 新轮按层序直接落 ZCode，绝不再 main_force_full。
        self.seed(QMAX, 1)
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
        # 同上：自定义 Max 参与层内竞争属禁用前历史口径，临时空禁用集复现历史场景。
        with user_disabled_pools(frozenset()):
            target, _ = self.select(CN_CUSTOM_MAX, NIGHT)
            self.assertEqual(target, CN_CUSTOM_MAX)

    def test_same_tier_full_tie_uses_stable_pool_key_order(self):
        # 自定义 Max 占满后，CN 内置 Max（同层=1）唯一有空 → 选中它。
        # BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023：国际 Qoder Max 已独立为层=4，不再与本层
        # 同层竞争，故这里不再出现"两内置平衡"的旧期望。
        self.seed(CN_CUSTOM_MAX, 2)
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
        with user_disabled_pools(frozenset()),                 closing(dp.connect(self.store)) as conn:
            self.assertIn(QMAX, dp._eligible_main_forces(conn, False, NIGHT_22))
            self.assertIn(CN_CUSTOM_MAX, dp._eligible_main_forces(conn, False, NIGHT_22))

    def test_beijing_08_boundary_max_not_eligible(self):
        with closing(dp.connect(self.store)) as conn:
            elig = dp._eligible_main_forces(conn, False, DAY_08)
        # weak（默认）档：三个 Max 在 08:00 全部被恢复后的夜间门挡下。
        for pk in MAXES:
            self.assertNotIn(pk, elig)
        # BW-ZCODE-RESTORE-1-20261022：ZCode 无时段门，白天照常在候选（层=2 最先承接）。
        self.assertIn(ZCODE, elig)
        self.assertIn(OC_DS, elig)
        # strong 档：三个 Max 全部无时段门 → 08:00 也在候选里（三 Max 时代历史口径，
        # 目标池现行被用户禁用，临时空禁用集复现）。
        with user_disabled_pools(frozenset()),                 dispatch_mode('strong'), closing(dp.connect(self.store)) as conn:
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
        # BW-ZCODE-RESTORE-1-20261022：zcode 恢复为正常可派主力（容量 1），满主力夹具
        # 一并占住其唯一槽，其余层占满方式不变。
        self.assertTrue(self.reserve('z0', 'zcode', 'GLM-5.3', 'zcode').get('allowed'))
        self.assertTrue(self.reserve('qm', 'qoder', 'Qwen3.8-Max', 'qoder').get('allowed'))
        self.assertTrue(self.reserve('cm', 'qodercn', 'Qwen3.8-Max', 'qodercn').get('allowed'))
        # BW-QODERCN-TOKEN-MAX-DISABLE-20261028：目标池两槽按禁用前 legacy 占位构造
        # 「全部主力占满」物理场景（临时空禁用集，仅限这两次 reserve）。
        with user_disabled_pools(frozenset()):
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

    def test_explicit_zcode_scope_claims_zcode_itself(self):
        # BW-ZCODE-RESTORE-1-20261022：显式 executor=zcode 恢复为权威原子门——有空位
        # （容量 1）直接 claim 自己，绝不强制改派 Max、绝不改派别池；第二个同组合在
        # 容量满时如实拒绝（full，绝不回落 Flash）。
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
        self.seed(QMAX, 1, state='unknown')
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(dp._count_active(conn, QMAX), 1)
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
        # BW-STRONG-SONUS-20261024 当前权威物理总数 16：原 15 槽 + strong-only
        # `qoder:Sonus` 独立 1 槽（弱/中档仍只能新派 15，Sonus 仅 strong 可新派）。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 16)
        self.assertEqual(dp.capacity_for(dp.SONUS_POOL_KEY), 1)
        self.assertEqual(sum(dp.capacity_for(k) for k in dm.ALL_POOL_KEYS), 15)
        self.assertEqual(dp.capacity_for(CODEBUDDY), 1)
        self.assertEqual(dp.capacity_for('codebuddy:glm-5.3-flash'), 1)
        self.assertEqual(dp.capacity_for(dp.OPENCODE_DEEPSEEK_POOL_KEY), 1)
        self.assertEqual(dp.capacity_for(dp.OPENCODE_GLM_POOL_KEY), 1)
        self.assertEqual(dp.capacity_for(ZCODE), 1)
        self.assertIn(ZCODE, dp.MAIN_FORCE_KEYS)
        self.assertIn(ZCODE, dp.POOL_PRIORITY)
        # BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023（用户明确指令，当前权威）：ZCode 层=2 紧跟
        # 两个 CN Max 之后、claude 之前；国际 Qoder Max 层=4 紧邻 claude 之后；CodeBuddy
        # deepseek 层=5；整表即唯一权威层序，任何漂移都直接失败。
        self.assertEqual(dp.POOL_PRIORITY[CODEBUDDY], 5)
        self.assertEqual(dp.POOL_PRIORITY, {
            CN_CUSTOM_MAX: 1, CN_MAX: 1,
            ZCODE: 2,
            CLAUDE: 3,
            QMAX: 4,
            CODEBUDDY: 5,
            dp.OPENCODE_DEEPSEEK_POOL_KEY: 6, dp.OPENCODE_GLM_POOL_KEY: 6,
            'codebuddy:glm-5.3-flash': 7,
            QFLASH: 8, CN_FLASH: 8})
        # 用户要求的三条相邻关系：CN Max < ZCode < Claude < 国际 Qoder < CodeBuddy。
        self.assertLess(dp.POOL_PRIORITY[CN_MAX], dp.POOL_PRIORITY[ZCODE])
        self.assertLess(dp.POOL_PRIORITY[ZCODE], dp.POOL_PRIORITY[CLAUDE])
        self.assertLess(dp.POOL_PRIORITY[CLAUDE], dp.POOL_PRIORITY[QMAX])
        self.assertLess(dp.POOL_PRIORITY[QMAX], dp.POOL_PRIORITY[CODEBUDDY])
        self.assertEqual(dp.POOL_PRIORITY[QMAX], dp.POOL_PRIORITY[CLAUDE] + 1)
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

    def test_stage_selection_zcode_when_max_window_closed(self):
        # BW-ZCODE-RESTORE-1-20261022：GAP（两个 CN Max 窗外）且 ZCode 有空位 → 层=2 的
        # ZCode 先于 claude 被选中。BW-QODER-INTL-MAX-ALLDAY-20261023 起国际 Max 白天也
        # 可派，故先占满其唯一名额。
        self.seed(QMAX, 1)
        auth = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        r = self.plan(auth, now=GAP)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], ZCODE)

    def test_stage_selection_claude_when_zcode_full(self):
        # ZCode 唯一槽占满后（GAP，国际 Max 也已占满），按层序落到 claude:deepseek-flash。
        self.seed(QMAX, 1)
        self.seed(ZCODE, 1)
        auth = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        r = self.plan(auth, now=GAP)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CLAUDE)

    def test_stage_selection_zcode_before_claude(self):
        # PEAK 下两个 CN Max 窗外、CB deepseek 夹具已占、国际 Max 已占满 → 按层序选中恢复
        # 可派的 ZCode（层=2，BW-ZCODE-RESTORE-1-20261022），先于 claude（层=3）。
        self.seed(QMAX, 1)
        auth = self.auth('auto', 'T', 's2', ALL_MAINS + ALL_FLASH)
        r = self.plan(auth, now=PEAK)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], ZCODE)
        # ZCode 出现在逐候选回执中且为 eligible（未被任何资格门过滤）。
        self.assertEqual(r['candidate_filters'].get(ZCODE), 'eligible', r)

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


class ClaudeThenIntlMaxOrderTests(_Base):
    """BW-MANUAL-THREE-MODE-20261023：weak（默认）档层序仍是 Claude(3) 紧邻高于国际
    Qoder Max(4)，且三个 Max 的夜间门与 claude 的工作日高峰门都已恢复。

    因此 weak 档下「claude 与三个 Max 同时可派」的白天时刻只有工作日**离峰**（12:00–14:00、
    18:00 之后）；高峰时刻（10:00 / 15:00）由 strong 档覆盖（无任何时段门）。"""

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
        auth = self.auth('auto', 'T', 's2', [CLAUDE, QMAX])
        r = self.plan(auth, now=NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CLAUDE, r)
        self.assertEqual(r['selected_priority'], 3, r)
        self.assertEqual(r['next_candidate']['pool_key'], QMAX, r)

    def test_auto_falls_to_intl_max_when_claude_full(self):
        self.seed(CLAUDE, 2)
        auth = self.auth('auto', 'T', 's2', [CLAUDE, QMAX])
        r = self.plan(auth, now=NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], QMAX, r)
        self.assertEqual(r['selected_priority'], 4, r)
        self.assertEqual(r['candidate_filters'][CLAUDE], 'capacity_full', r)

    def test_intl_max_full_does_not_block_claude(self):
        self.seed(QMAX, 1)
        auth = self.auth('auto', 'T', 's2', [CLAUDE, QMAX])
        r = self.plan(auth, now=NIGHT)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], CLAUDE, r)
        self.assertEqual(r['candidate_filters'][QMAX], 'capacity_full', r)


if __name__ == '__main__':
    unittest.main()
