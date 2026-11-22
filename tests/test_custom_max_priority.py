"""CN token plan Max 用户禁用的离线回归；临时 SQLite、假入口、零真实模型。

BW-WEAK-RESTRUCTURE-20261116（用户明确指令，本文件按其同步期望）：弱档收窄为 5 池 6 槽
（两 OpenCode／CodeBuddy GLM／Claude／ZCode GLM-5.3-Flash），全部 Qoder 池与
codebuddy:deepseek-v4.1-flash 移出弱档；物理总容量 16→17（新增 zcode:GLM-5.3-Flash
独立 1 槽）。Qwen Max 层序与用户禁用语义只属 strong/medium——相应对照改在对应档位复现；
codebuddy:deepseek 相关载体换到弱档层2的 codebuddy:glm-5.3-flash；遗留 reserved 的
精确 claim 改在池仍可派的 strong 档取得。绝不删除测试、绝不弱化断言。
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
from dispatch_mode_support import dispatch_mode  # noqa: E402
import stage_selection as ss  # noqa: E402

CODEBUDDY = 'codebuddy:deepseek-v4.1-flash'
QMAX = 'qoder:Qwen4-Max-Preview'
CN_MAX = 'qodercn:Qwen4-Max-Preview'
CN_CUSTOM_MAX = 'qodercn:Qwen-3.8-Max'
CLAUDE = 'claude:deepseek-flash'
ZCODE = 'zcode:GLM-5.3'  # BW-ZCODE-RESTORE-1-20261022：恢复可派主力（容量 1、层=2）
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
        """占住 CodeBuddy 层=3 的唯一名额，使后续断言聚焦 Max 层内部次序。"""
        self.seed(CODEBUDDY, 1)

    def select(self, requested, now, zcode_blocked=False):
        with closing(dp.connect(self.store)) as conn:
            elig = dp._eligible_main_forces(conn, zcode_blocked, now)
            return dp._select_main_force(conn, requested, now, eligible=elig)

    def reserve(self, tid, runtime, model, executor='auto', now=NIGHT, ws=None):
        return dp.reserve(self.store, task_id=tid, runtime=runtime, model=model,
                          workspace=ws or self.ws(tid), prompt_sha256=SHA, stage='s',
                          now=now, executor=executor, _preclaim=False)

    def claim_auto(self, tid, runtime='opencode',
                   model='deepseek-v4.1-flash-free', now=NIGHT):
        # BW-ZCODE-RETIRE-20261020：原默认请求组合 zcode:GLM-5.3 已退役（会先撞
        # unknown_pool），改用现役组合验证 AUTO 层序。
        # BW-WEAK-RESTRUCTURE-20261116: 弱档层1已是两 OpenCode——缺省请求组合改用
        # opencode:deepseek-v4.1-flash-free（弱档可派；两地区 Flash 已移出弱档，原
        # 国际 Flash 请求组合在现行弱档会在档位硬门被拒）；其它档位对照显式传入该档
        # 可派组合（如中档的 qoder:Qwen4-Max-Preview）。
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
    def test_disabled_custom_skipped_builtin_precedes_codebuddy(self):
        # BW-WEAK-RESTRUCTURE-20261116: 全部 Qoder Max 与 codebuddy:deepseek 已移出弱档
        # ——「被禁用自定义 Max 被跳过、选中内置 Max」的层序对照改在中档（北京 23:00
        # Max 夜间窗内）复现；中档层1三个 Max 平层，CN 自定义被用户禁用后国际内置
        # Qwen4-Max-Preview 为层内首选；codebuddy:deepseek 不再出现在任何档位的层表。
        with dispatch_mode('medium'):
            target, reason = self.select(ZCODE, NIGHT)
            self.assertEqual(reason, 'main_force_selected')
            self.assertEqual(target, QMAX)
        self.assertNotIn(CODEBUDDY, dm.priority_map_for_mode('medium'))
        self.assertFalse(dm.pool_allowed(CODEBUDDY, 'medium'))
        self.assertNotIn(CODEBUDDY, dm.priority_map_for_mode('weak'))
        self.assertFalse(dm.pool_allowed(CODEBUDDY, 'weak'))

    def test_disabled_custom_with_legacy_inflight_still_skipped(self):
        # BW-WEAK-RESTRUCTURE-20261116: 遗留在途对照在中档复现（Max 只属 strong/medium）；
        # 被禁用自定义 Max 的 legacy 在途照旧跳过，请求的国际内置 Max（层1平层、请求
        # tie-break 优先）被选中，绝不落被禁用池。
        self.fill_codebuddy()
        self.seed(CN_CUSTOM_MAX, 1)
        with dispatch_mode('medium'):
            target, _ = self.select(QMAX, NIGHT)
        self.assertEqual(target, QMAX)

    def test_custom_full_falls_to_builtins(self):
        # BW-WEAK-RESTRUCTURE-20261116: Max 满后落内置 Max 的对照在中档复现。
        self.fill_codebuddy()
        self.seed(CN_CUSTOM_MAX, 2)
        with dispatch_mode('medium'):
            target, _ = self.select(ZCODE, NIGHT)
        self.assertIn(target, (QMAX, CN_MAX))

    def test_all_max_full_falls_to_claude_then_opencode(self):
        # BW-WEAK-RESTRUCTURE-20261116: 弱档不再含任何 Max/OpenCode 旧走位——「三 Max
        # 全满后逐层下落」的走位在中档复现：三 Max（4 槽）满 → claude（层2）→ claude
        # （2 槽）满 → zcode:GLM-5.3（层3，中档最后）。两 OpenCode 在现行弱档是层1，
        # 绝不在 Max 之后；本用例不再断言旧的「Max 后落 OpenCode」弱档走位。
        self.fill_codebuddy()
        self.seed(CN_CUSTOM_MAX, 2)
        self.seed(QMAX, 1)
        self.seed(CN_MAX, 1)
        with dispatch_mode('medium'):
            target, _ = self.select(ZCODE, NIGHT)
            self.assertEqual(target, CLAUDE)
            self.seed(CLAUDE, 2)
            target, _ = self.select(ZCODE, NIGHT)
        self.assertEqual(target, ZCODE)

    def test_day_all_max_excluded_in_weak_mode(self):
        # BW-WEAK-RESTRUCTURE-20261116: 弱档只剩 Claude 高峰门——白天三个 Max 与
        # zcode:GLM-5.3 都整档不在 weak 候选（档位排除先于时段门）；弱档 ZCode 用
        # zcode:GLM-5.3-Flash；claude 工作日 10:00 属高峰 → 高峰门照旧关。
        self.fill_codebuddy()
        with closing(dp.connect(self.store)) as conn:
            elig = dp._eligible_main_forces(conn, False, DAY)
        for pk in (CN_MAX, CN_CUSTOM_MAX, QMAX, ZCODE, CODEBUDDY):
            self.assertNotIn(pk, elig)
        self.assertIn(dp.ZCODE_FLASH_POOL_KEY, elig)
        self.assertNotIn(CLAUDE, elig)  # 工作日 10:00 属高峰 → claude 恢复高峰门
        self.assertIn(self.select(ZCODE, DAY)[0], dp.OPENCODE_POOL_KEYS)

    def test_day_max_all_day_in_strong_mode(self):
        # 强档白天仍按两个内置 Max、Claude、ZCode 的顺序择优。
        with dispatch_mode('strong'):
            self.fill_codebuddy()
            with closing(dp.connect(self.store)) as conn:
                elig = dp._eligible_main_forces(conn, False, DAY)
            self.assertNotIn(CN_CUSTOM_MAX, elig)
            self.assertNotIn(dp.SONUS_POOL_KEY, elig)
            for pk in (CN_MAX, QMAX, ZCODE, CLAUDE):
                self.assertIn(pk, elig)
            first = self.select(ZCODE, DAY)[0]
            self.assertIn(first, (QMAX, CN_MAX))
            second = CN_MAX if first == QMAX else QMAX
            self.seed(first, 1)
            self.assertEqual(self.select(ZCODE, DAY)[0], second)
            self.seed(second, 1)
            self.assertEqual(self.select(ZCODE, DAY)[0], CLAUDE)
            self.seed(CLAUDE, 2)
            self.assertEqual(self.select(ZCODE, DAY)[0], ZCODE)

    def test_explicit_builtin_selection_not_rewritten(self):
        # BW-WEAK-RESTRUCTURE-20261116: 显式内置 Max 不被改写的对照在中档复现
        # （北京 23:00 Max 夜间窗内可派；弱档已无任何 Qoder 池）。
        with dispatch_mode('medium'):
            r = self.reserve('qm', 'qoder', 'Qwen4-Max-Preview', executor='qoder')
            self.assertTrue(r.get('allowed'), r)
            self.assertEqual(r['pool_key'], QMAX)
            r = self.reserve('cm', 'qodercn', 'Qwen4-Max-Preview', executor='qodercn')
            self.assertTrue(r.get('allowed'), r)
            self.assertEqual(r['pool_key'], CN_MAX)


class SameOrderAndGuardsTests(_Base):
    def test_stage_preview_and_atomic_entry_same_order(self):
        # BW-WEAK-RESTRUCTURE-20261116: 弱档层1已是两 OpenCode（全部 Qoder Max 移出弱档）
        # ——「stage 预览与原子入口同序」在弱档新层序下断言（两者都选弱档层1的
        # opencode:deepseek-v4.1-flash-free）；被禁用自定义 Max 的 mode_excluded 标记
        # 不变；并补中档对照（预览与原子入口同选国际内置 Max，层序口径跨档一致）。
        self.fill_codebuddy()
        auth = self.auth(ALL_MAINS + ALL_FLASH)
        r = self.plan(auth)
        self.assertEqual(r['refused'], None, r)
        self.assertEqual(r['selected']['pool_key'], dp.OPENCODE_DEEPSEEK_POOL_KEY, r)
        self.assertEqual(r['candidate_filters'][CN_CUSTOM_MAX], 'mode_excluded')
        out = self.claim_auto('t1')
        # 原子入口直接认领（请求组合 == 选中组合）：pool_key 与 selected 同指弱档层1。
        # BW-WEAK-RESTRUCTURE-20261116: 直接认领回执的 selected 只含 runtime/model
        # （routing 回执才含 pool_key），故这里同时断言 pool_key 与 selected 内容。
        self.assertEqual(out['pool_key'], dp.OPENCODE_DEEPSEEK_POOL_KEY, out)
        self.assertEqual(out['selected'], {'runtime': 'opencode',
                                           'model': 'deepseek-v4.1-flash-free'},
                         out)
        with dispatch_mode('medium'):
            r_m = self.plan(self.auth(ALL_MAINS + ALL_FLASH))
            self.assertEqual(r_m['refused'], None, r_m)
            self.assertEqual(r_m['selected']['pool_key'], QMAX, r_m)
            # BW-WEAK-RESTRUCTURE-20261116: 中档原子入口显式传中档可派组合
            # （opencode 请求在中档会被档位硬门拒绝）。
            out_m = self.claim_auto('t1-medium', 'qoder', 'Qwen4-Max-Preview')
            self.assertEqual(out_m['pool_key'], QMAX, out_m)
            self.assertEqual(out_m['selected'], {'runtime': 'qoder',
                                                 'model': 'Qwen4-Max-Preview'},
                             out_m)

    def test_capacity_17_current_authority(self):
        # BW-STRONG-SONUS-20261024：物理总量 16（原 15 + strong 专属 Sonus 1 槽）；
        # BW-WEAK-RESTRUCTURE-20261116: 16→17（新增弱档专用 zcode:GLM-5.3-Flash 独立
        # 1 槽）；ALL_POOL_KEYS 为全档位策略池并集（12 池、不含 Sonus）合计 16；
        # weak 可新派 5 池 6 槽（全部 Qoder 与 codebuddy:deepseek 移出弱档）。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 17)
        self.assertEqual(dp.capacity_for(CN_CUSTOM_MAX), 2)
        self.assertEqual(dp.capacity_for(ZCODE), 1)
        self.assertEqual(dp.capacity_for(dp.ZCODE_FLASH_POOL_KEY), 1)
        self.assertEqual(dp.capacity_for(dp.SONUS_POOL_KEY), 1)
        self.assertEqual(sum(dp.MAIN_FORCE_CAPACITY.values())
                         + sum(dp.OVERFLOW_CAPACITY.values()), 17)
        self.assertEqual(sum(dp.capacity_for(k) for k in dm.ALL_POOL_KEYS), 16)
        self.assertEqual(sum(dp.capacity_for(k)
                             for k in dm.allowed_pools_for_mode('weak')), 6)

    def test_workspace_single_write_guard_unchanged(self):
        # 同 workspace 在途互斥语义不变（恢复后的 zcode executor 与 codebuddy executor
        # 同受单写守卫约束）。BW-WEAK-RESTRUCTURE-20261116: codebuddy:deepseek-v4.1-flash
        # 已无任何档位可新派——单写守卫用仍处弱档层2的 codebuddy:glm-5.3-flash 复现
        # （同 workspace 第二请求仍拒）。
        shared = self.ws('shared')
        r1 = self.reserve('w1', 'codebuddy', 'glm-5.3-flash',
                          executor='codebuddy', ws=shared)
        self.assertTrue(r1.get('allowed'), r1)
        r2 = self.reserve('w2', 'codebuddy', 'glm-5.3-flash',
                          executor='codebuddy', ws=shared)
        self.assertFalse(r2.get('allowed'), r2)
        self.assertEqual(r2['reason'], 'workspace_in_flight')

    def test_unknown_inflight_still_holds_capacity(self):
        # BW-WEAK-RESTRUCTURE-20261116: unknown 在途占容量、被禁自定义 Max 被跳过的
        # 对照在中档复现（Max 只属 strong/medium）。
        self.fill_codebuddy()
        self.seed(CN_CUSTOM_MAX, 2, state='unknown')
        with dispatch_mode('medium'):
            target, _ = self.select(ZCODE, NIGHT)
        self.assertNotEqual(target, CN_CUSTOM_MAX)
        self.assertIn(target, (QMAX, CN_MAX))


class UserDisabledCustomMaxTests(_Base):
    def legacy_claim(self, task):
        # BW-WEAK-RESTRUCTURE-20261116: 遗留 reserved 的「取得」必须在池仍可派的档位
        # 完成——CN 自定义 Max 现只在 strong/medium 策略集合内，且需临时清空用户禁用集
        # 复现禁用前历史；取得统一在 strong 档完成（无时段门、确定性最高），消费/重复
        # 消费/终态仍跨全档验证（reserved 状态的 legacy claim 经 custom_max_legacy_
        # reserved 例外在所有档位放行消费）。
        with dispatch_mode('strong'):
            with mock.patch.object(dm, 'USER_DISABLED_POOL_KEYS', frozenset()):
                claim = self.reserve(task, 'qodercn', 'Qwen-3.8-Max',
                                     executor='qodercn')
        self.assertTrue(claim['allowed'], claim)
        return claim['token']

    def consume(self, task, token, **changes):
        args = dict(task_id=task, runtime='qodercn', model='Qwen-3.8-Max',
                    workspace=self.ws(task), prompt_sha256=SHA, stage='s',
                    claim_token=token, wrapper_pid=4321, wrapper_created='c1', now=NIGHT)
        args.update(changes)
        return dp.consume_for_entry(self.store, **args)

    def test_exact_disabled_registration_and_mode_counts(self):
        self.assertEqual(dm.USER_DISABLED_POOL_KEYS, {CN_CUSTOM_MAX})
        self.assertNotIn(CN_CUSTOM_MAX, dp.LEGACY_RETIRED_POOL_KEYS)
        self.assertEqual(dp.capacity_for(CN_CUSTOM_MAX), 2)
        # BW-WEAK-RESTRUCTURE-20261116: 物理总 17（16 + 弱档专用 zcode:GLM-5.3-Flash
        # 独立 1 槽）。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 17)
        import install_desktop_skill as installer
        self.assertIn(CN_CUSTOM_MAX, installer.HOST_EXCLUDED['qoder'])
        # BW-QWEN4-SONUS-REMOVE-20261115：Sonus 从所有档位去除——strong/medium 4 池/5 槽。
        # BW-WEAK-RESTRUCTURE-20261116: weak 收窄为 5 池/6 槽（两 OpenCode／CodeBuddy
        # GLM／Claude／ZCode GLM-5.3-Flash；全部 Qoder 池与 codebuddy:deepseek 移出）。
        for mode, count, slots in (('strong', 4, 5), ('medium', 4, 5), ('weak', 5, 6)):
            pools = dm.allowed_pools_for_mode(mode)
            self.assertEqual(len(pools), count)
            self.assertEqual(sum(dp.capacity_for(pk) for pk in pools), slots)
            self.assertNotIn(CN_CUSTOM_MAX, pools)
            # BW-WEAK-RESTRUCTURE-20261116: CN 内置 Max 在 strong/medium 仍可派；weak 已
            # 移出全部 Qoder 池——改断言弱档新集合（两 OpenCode／CodeBuddy GLM／Claude／
            # ZCode GLM-5.3-Flash）。
            if mode == 'weak':
                for pk in (dp.OPENCODE_DEEPSEEK_POOL_KEY, dp.OPENCODE_GLM_POOL_KEY,
                           dp.CODEBUDDY_GLM_POOL_KEY, CLAUDE,
                           dp.ZCODE_FLASH_POOL_KEY):
                    self.assertIn(pk, pools)
                self.assertNotIn(CN_MAX, pools)
                self.assertNotIn(QMAX, pools)
            else:
                self.assertIn(CN_MAX, pools)
            self.assertIn('用户要求禁用', dm.mode_excluded_reason(CN_CUSTOM_MAX, mode))

    def test_new_dispatch_refused_in_all_modes_day_and_night(self):
        for mode in dm.MODES:
            with dispatch_mode(mode):
                for now in (DAY, NIGHT):
                    for operation in (dp.reserve, dp.select_and_claim, dp.consume_for_entry):
                        for executor in ('auto', 'qodercn'):
                            out = operation(self.store, task_id='new', runtime='qodercn',
                                            model='Qwen-3.8-Max', workspace=self.ws('new'),
                                            prompt_sha256=SHA, executor=executor, now=now)
                            self.assertFalse(out['allowed'], out)
                            self.assertFalse(out['sent'], out)
                            self.assertEqual(out['reason'], 'mode_excluded', out)
                            self.assertIn('用户要求禁用', out['reasons'][0])
                            self.assertFalse(out.get('routing_required'), out)
                            self.assertIsNone(out.get('token'))
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)

    def test_auto_cannot_select_disabled_even_as_last_free_main(self):
        for pk in dp.MAIN_FORCE_KEYS:
            if pk != CN_CUSTOM_MAX:
                self.seed(pk, dp.capacity_for(pk))
        for mode in dm.MODES:
            with dispatch_mode(mode):
                target, _ = self.select(CN_CUSTOM_MAX, NIGHT)
                self.assertIsNone(target)
                plan = self.plan(self.auth([CN_CUSTOM_MAX, CN_MAX]))
                self.assertEqual(plan['candidate_filters'][CN_CUSTOM_MAX], 'mode_excluded')
                self.assertIsNone(plan['selected'])

    def test_legacy_exact_claim_consumes_once_and_finishes_in_all_modes(self):
        for mode in dm.MODES:
            task = 'legacy-' + mode
            token = self.legacy_claim(task)
            with dispatch_mode(mode):
                out = self.consume(task, token)
                self.assertTrue(out['allowed'], out)
                duplicate = self.consume(task, token)
                self.assertFalse(duplicate['allowed'], duplicate)
                self.assertFalse(duplicate.get('capacity_released'))
                bound = dp.bind_child(self.store, token, child_pid=9876,
                                      child_created='child', wrapper_pid=4321,
                                      wrapper_created='c1', now=NIGHT)
                self.assertTrue(bound['bound'], bound)
                with mock.patch.object(dp, 'process_identity',
                                       return_value={'state': 'dead', 'created': None}):
                    ended = dp.finish(self.store, token, terminal='finished', success=True,
                                      wrapper_pid=4321, wrapper_created='c1', now=NIGHT)
                self.assertTrue(ended['released'], ended)
            self.assertEqual(dp.status(self.store, now=NIGHT)['pools'][CN_CUSTOM_MAX]['active'], 0)

    def test_legacy_claim_drift_and_workspace_guard_remain(self):
        token = self.legacy_claim('old')
        for drift in ({'task_id': 'wrong'}, {'runtime': 'qoder'},
                      {'model': 'Qwen4-Max-Preview'}, {'workspace': self.ws('wrong')},
                      {'prompt_sha256': 'wrong'}, {'stage': 'wrong'}, {'chat_id': 'wrong'}):
            out = self.consume('old', token, **drift)
            self.assertEqual(out['reason'], 'claim_invalid', out)
        with closing(dp.connect(self.store)) as conn:
            dp._insert_claim(conn, task_id='other', stage='s', chat_id=None,
                             runtime='qodercn', model='Qwen4-Max-Preview', pk=CN_MAX,
                             workspace=self.ws('old'), prompt_sha256=SHA,
                             token=None, now=NIGHT, origin='offline-legacy-fixture')
        out = self.consume('old', token)
        self.assertEqual(out['reason'], 'workspace_in_flight', out)
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(conn.execute('SELECT state FROM attempts WHERE token=?',
                                          (token,)).fetchone()[0], 'reserved')

    def test_running_unknown_and_reserved_never_mutated_by_new_refusal(self):
        for state in dp.ACTIVE_STATES:
            self.seed(CN_CUSTOM_MAX, state=state)
        with closing(dp.connect(self.store)) as conn:
            before = [tuple(row) for row in conn.execute('SELECT * FROM attempts')]
        for mode in dm.MODES:
            with dispatch_mode(mode):
                out = self.reserve('new', 'qodercn', 'Qwen-3.8-Max', executor='qodercn')
                self.assertEqual(out['reason'], 'mode_excluded')
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual([tuple(row) for row in conn.execute('SELECT * FROM attempts')],
                             before)

    def test_claim_due_exact_disabled_ticket_stays_pending(self):
        import json
        scope = {'task_id': 'ticket', 'stage': 's', 'chat_id': 'c',
                 'workspace': self.ws('ticket'), 'prompt_sha256': SHA,
                 'executor': 'qodercn', 'model': 'Qwen-3.8-Max',
                 'allowed_candidates': [CN_CUSTOM_MAX]}
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO luna_tickets(task_id, scope, state, asked_at_utc, '
                         'deadline_utc, updated_at_utc) VALUES(?,?,?,?,?,?)',
                         ('ticket', json.dumps(scope, sort_keys=True), 'pending',
                          dp._iso(NIGHT), dp._iso(NIGHT), dp._iso(NIGHT)))
        for mode in dm.MODES:
            with dispatch_mode(mode), mock.patch.object(dp, 'LUNA_DISABLED', False):
                out = dp.claim_due(self.store, task_id='ticket', now=NIGHT, _preclaim=False)
                with mock.patch.object(dp, '_domestic_active', return_value=16):
                    full = dp.claim_due(self.store, task_id='ticket', now=NIGHT, _preclaim=False)
            for result in (out, full):
                self.assertFalse(result['claimed'], result)
                self.assertEqual(result['reason'], 'mode_excluded', result)
                self.assertIn('用户要求禁用', ' '.join(result['reasons']))
        with closing(dp.connect(self.store)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT state FROM luna_tickets').fetchone()[0],
                             'pending')

    def test_claim_due_default_path_disabled_beats_luna_gate(self):
        # BW-QODERCN-TOKEN-MAX-DISABLE-20261028 S2：生产默认 LUNA_DISABLED=True 时，
        # claim_due 的「用户要求禁用」必须先于 Luna 总门——scope 唯一指向目标池的票据
        # 收到 mode_excluded（用户要求禁用），绝不被 luna_disabled 遮蔽；指向其它池
        # （如 CN 内置 Max）的票据保持原 luna_disabled 总门，不误伤。
        import json
        def _ticket(task_id, model):
            scope = {'task_id': task_id, 'stage': 's', 'chat_id': 'c',
                     'workspace': self.ws(task_id), 'prompt_sha256': SHA,
                     'executor': 'qodercn', 'model': model}
            with closing(dp.connect(self.store)) as conn:
                conn.execute('INSERT INTO luna_tickets(task_id, scope, state, '
                             'asked_at_utc, deadline_utc, updated_at_utc) '
                             'VALUES(?,?,?,?,?,?)',
                             (task_id, json.dumps(scope, sort_keys=True), 'pending',
                              dp._iso(NIGHT), dp._iso(NIGHT), dp._iso(NIGHT)))
        _ticket('ticket-default', 'Qwen-3.8-Max')
        _ticket('ticket-builtin', 'Qwen4-Max-Preview')
        # 显式钉住生产默认 True（其它测试模块可能全局改写 dp.LUNA_DISABLED，这里不依赖
        # 全局状态，只验证 True 时的默认路径顺序）。
        with mock.patch.object(dp, 'LUNA_DISABLED', True):
            out = dp.claim_due(self.store, task_id='ticket-default', now=NIGHT,
                               _preclaim=False)
            self.assertFalse(out['claimed'], out)
            self.assertEqual(out['reason'], 'mode_excluded', out)
            self.assertIn('用户要求禁用', ' '.join(out['reasons']))
            other = dp.claim_due(self.store, task_id='ticket-builtin', now=NIGHT,
                                 _preclaim=False)
            self.assertFalse(other['claimed'], other)
            self.assertEqual(other['reason'], 'luna_disabled', other)

    def test_formal_cn_entry_disabled_gate_precedes_cn_config_read(self):
        # S2 生产缺口 2：无 --dispatch-claim 的正式 CN 入口请求精确目标时，必须在读取
        # 私有 CN 配置/模型映射之前拒绝——用不存在的 CN 配置路径反证顺序：若先读配置
        # 会打印 cn_entry_rejected，本用例要求打印用户禁用拒绝（mode_excluded）。内置
        # 名 Qwen4-Max-Preview 同路径必须仍是配置错误（不误伤）。
        import io
        import json
        import qoder_direct as qd
        from contextlib import redirect_stdout
        prompt = self.base / 'prompt2.txt'
        prompt.write_text('offline refusal ordering', encoding='utf-8')
        missing_cfg = str(self.base / 'no-such-cn-config.json')
        def _run(model):
            output = self.base / ('output-order-' + model)
            argv = ['qoder_direct.py', '--runtime', 'qodercn', '--model', model,
                    '--workspace', self.ws('entry2'), '--prompt-file', str(prompt),
                    '--stage', 's', '--output-dir', str(output),
                    '--dispatch-store', self.store, '--cn-config', missing_cfg,
                    '--tools', 'Read']
            stdout = io.StringIO()
            with mock.patch.object(sys, 'argv', argv),                     mock.patch.object(qd.subprocess, 'Popen') as popen,                     redirect_stdout(stdout):
                code = qd.main()
            return code, json.loads(stdout.getvalue()), popen, output
        code, receipt, popen, output = _run('Qwen-3.8-Max')
        self.assertEqual(code, 2)
        self.assertTrue(receipt['cn_user_disabled_pool_rejected'], receipt)
        self.assertEqual(receipt['reason'], 'mode_excluded')
        self.assertEqual(receipt['pool_key'], 'qodercn:Qwen-3.8-Max')
        self.assertIn('用户要求禁用', ' '.join(receipt['reasons']))
        popen.assert_not_called()
        self.assertFalse(output.exists())
        code2, receipt2, _popen2, output2 = _run('Qwen4-Max-Preview')
        self.assertEqual(code2, 2)
        self.assertTrue(receipt2['cn_entry_rejected'], receipt2)
        self.assertFalse(output2.exists())

    def test_formal_cn_entry_refuses_without_popen(self):
        import io
        import json
        import qoder_direct as qd
        from contextlib import redirect_stdout
        prompt = self.base / 'prompt.txt'
        prompt.write_text('offline refusal', encoding='utf-8')
        for mode in dm.MODES:
            output = self.base / ('output-' + mode)
            argv = ['qoder_direct.py', '--runtime', 'qodercn', '--model', 'Qwen-3.8-Max',
                    '--workspace', self.ws('entry'), '--prompt-file', str(prompt),
                    '--stage', 's', '--output-dir', str(output),
                    '--dispatch-store', self.store, '--tools', 'Read']
            stdout = io.StringIO()
            with dispatch_mode(mode), mock.patch.object(sys, 'argv', argv), \
                    mock.patch.object(qd, 'load_cn_config', return_value={
                        'cli': 'offline-cli', 'config_dir': str(self.base),
                        'model_ids': {'Qwen-3.8-Max': 'offline-model-id'}}), \
                    mock.patch.object(qd.subprocess, 'Popen') as popen, redirect_stdout(stdout):
                code = qd.main()
            self.assertEqual(code, 2)
            receipt = json.loads(stdout.getvalue())
            self.assertFalse(receipt['sent'])
            self.assertEqual(receipt['reason'], 'mode_excluded')
            self.assertIn('用户要求禁用', ' '.join(receipt['reasons']))
            popen.assert_not_called()
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
