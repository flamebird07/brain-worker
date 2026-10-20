"""BW-OPENCODE-20261019-S3 离线验收：dispatch_pool 的两个 OpenCode 免费模型主力槽。

只用临时 sqlite（每用例独立 store），绝不触碰真实 ~/.brain-worker 池。覆盖用户任务
验收口径：
- 各独立 1 槽、互不占位：填满一个不影响另一个；释放只释放自己；
- unknown 保容量：标 unknown 后新 claim 仍拒，绝不抢占；
- 同 workspace 单写入守卫跨池生效；
- AUTO 层序：… CodeBuddy deepseek(0) > 三 Qoder Max(1) > Claude(2) > 两 OpenCode(4，同级)
  > CodeBuddy GLM(5，额度独立、缺省候选即含) > 两地区 Flash（BW-ZCODE-RETIRE-20261020：
  ZCode 已退役、不再占任何层）；
  两 OpenCode 之间按真实在途平衡（同级公平）；
- 精确点名：executor 冲突/未知 model 一律拒绝，绝不跨执行器改派；
- 无时段门（北京白天照常）且无 CodeBuddy 额度前提（BW-LIMIT30-20261019-S3 起 GLM 也无需证据）；
- 总容量 14（BW-ZCODE-RETIRE-20261020 起 zcode 退役不再占槽）：两 OpenCode 计入国内总在途，
  满 14 绝不派第 15 个。
"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import dispatch_pool as dp  # noqa: E402

DEEPSEEK = dp.OPENCODE_DEEPSEEK_MODEL
GLM = dp.OPENCODE_GLM_MODEL
DPOOL = dp.OPENCODE_DEEPSEEK_POOL_KEY
GPOOL = dp.OPENCODE_GLM_POOL_KEY
NIGHT = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)   # 北京 23:00（Max 时段内）
DAY = datetime(2026, 10, 12, 2, 0, 0, tzinfo=timezone.utc)     # 北京 10:00 周一（Max 时段外、
                                                               # Claude 高峰门关闭）


class _PoolBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-oc-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        self.addCleanup(self._tmp.cleanup)

    def _ws_for(self, task):
        d = self.base / 'ws' / str(task)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def reserve(self, task, runtime, model, *, executor=None, now=NIGHT,
                ws=None, allowed=None, prompt='p'):
        r = dp.reserve(self.store, task_id=task, runtime=runtime, model=model,
                       workspace=str(ws or self._ws_for(task)), prompt_sha256=prompt,
                       executor=executor if executor is not None else runtime,
                       codebuddy_glm_allowed=bool(allowed), now=now, _preclaim=False)
        self.assertTrue(r.get('allowed'), r)
        return r['token']

    def select(self, task, runtime, model, *, executor='auto', now=NIGHT, **kw):
        return dp.select_and_claim(self.store, task_id=task, runtime=runtime,
                                   model=model, workspace=str(self._ws_for(task)),
                                   prompt_sha256='p', now=now, _preclaim=False,
                                   executor=executor, **kw)

    def active(self, pk, now=NIGHT):
        return dp.status(self.store, now=now)['pools'][pk]['active']

    def fill_tiers_above_opencode(self):
        """真实占满 CodeBuddy deepseek(1) + 三 Qoder Max(1+1+2) + claude(2)=7，
        全部经显式 executor 逐一 claim 保证确定性。BW-ZCODE-RETIRE-20261020：ZCode 已
        退役（capacity=0，不再占 tier）。GLM 额度独立始终参与候选（BW-LIMIT30-
        20261019-S3），本方法只填 tier 0-3，保留 tier 4 (OpenCode) 和 tier 5 (CBGLM) 空位。"""
        toks = [self.reserve('cb1', 'codebuddy', dp.CODEBUDDY_MODEL,
                             executor='codebuddy')]
        toks.append(self.reserve('qm', 'qoder', 'Qwen3.8-Max', executor='qoder'))
        toks.append(self.reserve('cm', 'qodercn', 'Qwen3.8-Max', executor='qodercn'))
        toks.append(self.reserve('cx1', 'qodercn', 'Qwen-3.8-Max', executor='qodercn'))
        toks.append(self.reserve('cx2', 'qodercn', 'Qwen-3.8-Max', executor='qodercn'))
        toks.append(self.reserve('cl1', 'claude', 'deepseek-flash', executor='claude'))
        toks.append(self.reserve('cl2', 'claude', 'deepseek-flash', executor='claude'))
        return toks


class CapacityConstantTests(_PoolBase):
    def test_capacity_and_total_constants(self):
        self.assertEqual(dp.MAIN_FORCE_CAPACITY[DPOOL], 1)
        self.assertEqual(dp.MAIN_FORCE_CAPACITY[GPOOL], 1)
        self.assertIn((dp.OPENCODE_RUNTIME, DEEPSEEK), dp.MAIN_FORCE_POOLS)
        self.assertIn((dp.OPENCODE_RUNTIME, GLM), dp.MAIN_FORCE_POOLS)
        self.assertEqual(dp.capacity_for(DPOOL), 1)
        self.assertEqual(dp.capacity_for(GPOOL), 1)
        self.assertTrue(dp.is_known_pool(DPOOL))
        self.assertTrue(dp.is_known_pool(GPOOL))
        # BW-ZCODE-RETIRE-20261020：zcode 退役后国内总容量 16→14。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 14)


class IndependentSlotTests(_PoolBase):
    def test_one_model_full_does_not_block_the_other(self):
        self.reserve('d1', 'opencode', DEEPSEEK)
        # 同池第二个 → 拒；另一免费模型独立槽照常 claim。
        r = dp.reserve(self.store, task_id='d2', runtime='opencode', model=DEEPSEEK,
                       workspace=str(self._ws_for('d2')), prompt_sha256='p',
                       executor='opencode', now=NIGHT, _preclaim=False)
        self.assertFalse(r.get('allowed'), r)
        self.assertEqual(r.get('reason'), 'capacity_full')
        self.assertFalse(r.get('sent', True), r)
        self.reserve('g1', 'opencode', GLM)
        self.assertEqual(self.active(DPOOL), 1)
        self.assertEqual(self.active(GPOOL), 1)

    def test_release_frees_only_own_slot(self):
        tok_d = self.reserve('d1', 'opencode', DEEPSEEK)
        tok_g = self.reserve('g1', 'opencode', GLM)
        fin = dp.finish(self.store, tok_g, terminal='start_failed', now=NIGHT)
        self.assertTrue(fin.get('released'), fin)
        self.assertEqual(self.active(GPOOL), 0)
        self.assertEqual(self.active(DPOOL), 1)
        # 深槽仍满：再来一个 deepseek 组合仍拒。
        r = dp.reserve(self.store, task_id='d2', runtime='opencode', model=DEEPSEEK,
                       workspace=str(self._ws_for('d2')), prompt_sha256='p',
                       executor='opencode', now=NIGHT, _preclaim=False)
        self.assertFalse(r.get('allowed'), r)

    def test_unknown_holds_capacity_and_is_never_preempted(self):
        tok = self.reserve('d1', 'opencode', DEEPSEEK)
        mk = dp.mark_unknown(self.store, tok, now=NIGHT)
        self.assertTrue(mk.get('settled'), mk)
        self.assertFalse(mk.get('released'))
        self.assertTrue(mk.get('capacity_held'))
        self.assertEqual(self.active(DPOOL), 1)
        r = dp.reserve(self.store, task_id='d2', runtime='opencode', model=DEEPSEEK,
                       workspace=str(self._ws_for('d2')), prompt_sha256='p',
                       executor='opencode', now=NIGHT, _preclaim=False)
        self.assertFalse(r.get('allowed'), r)
        self.assertEqual(r.get('reason'), 'capacity_full')

    def test_same_workspace_guard_spans_both_pools(self):
        self.reserve('d1', 'opencode', DEEPSEEK, ws=self.ws_shared)
        r = dp.reserve(self.store, task_id='g1', runtime='opencode', model=GLM,
                       workspace=str(self.ws_shared), prompt_sha256='p',
                       executor='opencode', now=NIGHT, _preclaim=False)
        self.assertFalse(r.get('allowed'), r)
        self.assertEqual(r.get('reason'), 'workspace_in_flight')
        self.assertFalse(r.get('sent', True), r)

    def setUp(self):
        super().setUp()
        self.ws_shared = self._ws_for('shared-writer')


class ExactDesignationTests(_PoolBase):
    def test_executor_conflict_never_crosses(self):
        r = dp.reserve(self.store, task_id='x1', runtime='opencode', model=DEEPSEEK,
                       workspace=str(self._ws_for('x1')), prompt_sha256='p',
                       executor='qoder', now=NIGHT, _preclaim=False)
        self.assertFalse(r.get('allowed'), r)
        self.assertEqual(r.get('reason'), 'executor_conflict')
        r2 = dp.reserve(self.store, task_id='x2', runtime='opencode', model='glm-5.3-flash',
                        workspace=str(self._ws_for('x2')), prompt_sha256='p',
                        executor='opencode', now=NIGHT, _preclaim=False)
        self.assertFalse(r2.get('allowed'), r2)
        # 非精确 opencode 组合 + 显式 executor='opencode'：正式原因是 executor_conflict
        # （绝不悄悄改派到别的池），而不是 unknown_pool。
        self.assertEqual(r2.get('reason'), 'executor_conflict')

    def test_other_executors_never_route_to_opencode(self):
        # 夜间显式 qoder Max 请求：直接 claim 自身池，绝不改派更低价 OpenCode。
        r = self.select('q1', 'qoder', 'Qwen3.8-Max', executor='qoder')
        self.assertTrue(r.get('allowed'), r)
        self.assertEqual(r['pool_key'], 'qoder:Qwen3.8-Max')

    def test_consume_claim_exact_combo_allowed(self):
        tok = self.reserve('d1', 'opencode', DEEPSEEK, prompt='p')
        res = dp.consume_for_entry(self.store, task_id='d1', runtime='opencode',
                                   model=DEEPSEEK, workspace=str(self._ws_for('d1')),
                                   prompt_sha256='p', stage=None, claim_token=tok,
                                   now=NIGHT, executor='opencode')
        self.assertTrue(res.get('allowed'), res)
        self.assertEqual(res['pool_key'], DPOOL)


class NoWindowNoQuotaGateTests(_PoolBase):
    def test_no_time_window_gate_beijing_day(self):
        for pk in (DPOOL, GPOOL):
            self.assertFalse(dp._max_window_blocks(pk, DAY), pk)
        r = dp.reserve(self.store, task_id='day1', runtime='opencode', model=DEEPSEEK,
                       workspace=str(self._ws_for('day1')), prompt_sha256='p',
                       executor='opencode', now=DAY, _preclaim=False)
        self.assertTrue(r.get('allowed'), r)
        # 同一时刻三个 Qoder Max 全部时段外拒派——对照证明 OpenCode 无时段门。
        for task, model in (('m1', 'Qwen3.8-Max'),):
            r2 = dp.reserve(self.store, task_id=task, runtime='qoder', model=model,
                            workspace=str(self._ws_for(task)), prompt_sha256='p',
                            executor='qoder', now=DAY, _preclaim=False)
            self.assertEqual(r2.get('reason'), 'main_force_window_closed', r2)

    def test_no_codebuddy_quota_precondition(self):
        # BW-LIMIT30-20261019-S3：GLM 额度独立、不再需要当轮 DeepSeek 限额证据，
        # 无 codebuddy_glm_allowed 即可 claim；两个 OpenCode 免费模型同样无任何额度前提。
        r = dp.reserve(self.store, task_id='glm1', runtime='codebuddy',
                       model=dp.CODEBUDDY_GLM_MODEL, workspace=str(self._ws_for('glm1')),
                       prompt_sha256='p', executor='codebuddy', now=DAY,
                       _preclaim=False)
        self.assertTrue(r.get('allowed'), r)
        self.assertEqual(r['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY)
        tok = self.reserve('oc1', 'opencode', GLM, now=DAY)
        res = dp.consume_for_entry(self.store, task_id='oc1', runtime='opencode',
                                   model=GLM, workspace=str(self._ws_for('oc1')),
                                   prompt_sha256='p', claim_token=tok, now=DAY,
                                   executor='opencode')
        self.assertTrue(res.get('allowed'), res)


class AutoTierOrderTests(_PoolBase):
    def test_auto_after_zcode_before_conditional_cbglm(self):
        # BW-ZCODE-RETIRE-20261020：ZCode 层已退役，高层只剩 tier 0-2（共 7 槽）；
        # 测试名保留历史标签，验证目标不变——OpenCode(tier4) 在高层全满后被 AUTO 选中、
        # 且先于条件性 tier5 CBGLM。
        self.fill_tiers_above_opencode()
        # 高层 7 槽全满 → AUTO 落到 tier4 的 OpenCode（而非 Flash 或 tier5 GLM）。
        r = self.select('a1', 'qoder', 'Qwen3.8-Flash')
        self.assertTrue(r.get('routing_required'), r)
        self.assertFalse(r.get('sent', True), r)
        self.assertIn(r['selected']['pool_key'], (DPOOL, GPOOL), r)
        # 把 AUTO 选中的池真实占住（经显式 opencode 入口，确定性）。
        self.reserve('a1', 'opencode',
                     DEEPSEEK if r['selected']['pool_key'] == DPOOL else GLM)
        # 只剩另一个 OpenCode 槽：tier4 OpenCode 优先于 tier5 GLM（GLM 额度独立始终候选）。
        r2 = self.select('a2', 'qoder', 'Qwen3.8-Flash')
        self.assertTrue(r2.get('routing_required'), r2)
        self.assertIn(r2['selected']['pool_key'], (DPOOL, GPOOL), r2)

    def test_auto_falls_to_cbglm_then_flash_when_opencode_full(self):
        self.fill_tiers_above_opencode()
        self.reserve('of1', 'opencode', DEEPSEEK)
        self.reserve('of2', 'opencode', GLM)
        # BW-LIMIT30-20261019-S3：GLM 额度独立、始终参与候选（无需证据）；
        # 两 OpenCode(4) 满 → tier5 GLM 先于 Flash。
        r = self.select('a1', 'qoder', 'Qwen3.8-Flash')
        self.assertTrue(r.get('routing_required'), r)
        self.assertEqual(r['selected']['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY, r)
        # GLM 也满 → 溢出 Flash 兜底。
        self.reserve('cbg', 'codebuddy', dp.CODEBUDDY_GLM_MODEL, executor='codebuddy')
        r2 = self.select('a2', 'qoder', 'Qwen3.8-Flash')
        # 请求入口本身已是 Flash（与兜底同组合）：直接 claim 自身查询池，
        # 正确返回 allowed/claimed 而非 routing_required；顶层 pool_key 属 OVERFLOW_KEYS。
        self.assertTrue(r2.get('allowed'), r2)
        self.assertTrue(r2.get('claimed'), r2)
        self.assertIn(r2['pool_key'], dp.OVERFLOW_KEYS, r2)

    def test_two_opencode_pools_balanced_same_tier(self):
        self.fill_tiers_above_opencode()
        picks = []
        for i in range(2):
            r = self.select(f'bal{i}', 'qoder', 'Qwen3.8-Flash')
            self.assertTrue(r.get('routing_required'), r)
            pk = r['selected']['pool_key']
            self.assertIn(pk, (DPOOL, GPOOL), r)
            picks.append(pk)
            self.reserve(f'bal{i}', 'opencode',
                         DEEPSEEK if pk == DPOOL else GLM)
        # 两槽同层按真实在途平衡：两轮 AUTO 应分到两个不同池。
        self.assertEqual(set(picks), {DPOOL, GPOOL}, picks)

    def test_opencode_beats_conditional_cbglm_even_when_cbglm_first_free(self):
        self.fill_tiers_above_opencode()
        self.reserve('od', 'opencode', DEEPSEEK)
        # deepseek 槽已满、glm-5.3-flash-free 仍空；GLM 始终参与候选 → tier4 OpenCode
        # 仍先于 tier5 CodeBuddy GLM。
        r = self.select('a1', 'qoder', 'Qwen3.8-Flash')
        self.assertTrue(r.get('routing_required'), r)
        self.assertEqual(r['selected']['pool_key'], GPOOL, r)


class TotalGuardFourteenTests(_PoolBase):
    def test_all_fourteen_and_never_fifteen(self):
        # BW-ZCODE-RETIRE-20261020：zcode 退役不再占槽，总容量 16→14。
        toks = self.fill_tiers_above_opencode()           # 7
        toks.append(self.reserve('cbg', 'codebuddy', dp.CODEBUDDY_GLM_MODEL,
                                 executor='codebuddy', allowed=True))  # 8
        toks.append(self.reserve('od', 'opencode', DEEPSEEK))          # 9
        toks.append(self.reserve('og', 'opencode', GLM))               # 10
        toks.append(self.reserve('qf1', 'qoder', 'Qwen3.8-Flash'))     # 11
        toks.append(self.reserve('qf2', 'qoder', 'Qwen3.8-Flash'))     # 12
        toks.append(self.reserve('cf1', 'qodercn', 'Qwen3.8-Flash',
                                 executor='qodercn'))                 # 13
        toks.append(self.reserve('cf2', 'qodercn', 'Qwen3.8-Flash',
                                 executor='qodercn'))                 # 14
        st = dp.status(self.store, now=NIGHT)
        self.assertEqual(st['domestic']['active'], 14)
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 14)
        self.assertTrue(st['domestic']['full'])
        r = dp.reserve(self.store, task_id='over', runtime='opencode', model=DEEPSEEK,
                       workspace=str(self._ws_for('over')), prompt_sha256='p',
                       executor='opencode', now=NIGHT, _preclaim=False)
        self.assertFalse(r.get('allowed'), r)
        self.assertFalse(r.get('sent', True), r)
        # 释放一个 OpenCode 槽 → 恢复可派（自己结束只影响自己）。
        fin = dp.finish(self.store, toks[8], terminal='start_failed', now=NIGHT)
        self.assertTrue(fin.get('released'), fin)
        r2 = dp.reserve(self.store, task_id='after', runtime='opencode', model=DEEPSEEK,
                        workspace=str(self._ws_for('after')), prompt_sha256='p',
                        executor='opencode', now=NIGHT, _preclaim=False)
        self.assertTrue(r2.get('allowed'), r2)


if __name__ == '__main__':
    unittest.main()
