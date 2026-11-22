"""BW-OPENCODE-20261019-S3 离线验收：dispatch_pool 的两个 OpenCode 免费模型主力槽。

只用临时 sqlite（每用例独立 store），绝不触碰真实 ~/.brain-worker 池。覆盖用户任务
验收口径（BW-WEAK-RESTRUCTURE-20261116 起弱档收窄为 5 池：两 OpenCode(1) > CodeBuddy
GLM(2) > Claude(3) > ZCode GLM-5.3-Flash(4)；全部 Qoder 池与 codebuddy deepseek 移出弱档）：
- 各独立 1 槽、互不占位：填满一个不影响另一个；释放只释放自己；
- unknown 保容量：标 unknown 后新 claim 仍拒，绝不抢占；
- 同 workspace 单写入守卫跨池生效；
- AUTO 层序：弱档两 OpenCode 同级(tier1)、按真实在途平衡（同级公平），先于 CBGLM(2)；
- 精确点名：executor 冲突/未知 model 一律拒绝，绝不跨执行器改派；
- 无时段门（北京白天照常）且无 CodeBuddy 额度前提（BW-LIMIT30-20261019-S3 起 GLM 也无需证据）；
- 物理总容量 17（BW-WEAK-RESTRUCTURE-20261116 起含弱档专用 zcode:GLM-5.3-Flash 独立 1 槽）：
  两 OpenCode 计入国内总在途，全满 17 绝不派第 18 个。
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
from dispatch_mode_support import (dispatch_mode, insert_sonus_leftover,  # noqa: E402
                                               user_disabled_pools)

DEEPSEEK = dp.OPENCODE_DEEPSEEK_MODEL
GLM = dp.OPENCODE_GLM_MODEL
DPOOL = dp.OPENCODE_DEEPSEEK_POOL_KEY
GPOOL = dp.OPENCODE_GLM_POOL_KEY
NIGHT = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)   # 北京 23:00（Max 时段内）
DAY = datetime(2026, 10, 12, 2, 0, 0, tzinfo=timezone.utc)     # 北京 10:00 周一（Max 时段外、
                                                               # Claude 高峰门关闭）


def insert_legacy_inflight(store, *, token, task_id, pool_key, runtime, model):
    """BW-WEAK-RESTRUCTURE-20261116: 直插一条「档位重构前派出、仍在途」的历史 attempt 行。

    codebuddy:deepseek-v4.1-flash 与两地区 Flash（qoder/qodercn:Qwen3.8-Flash）已被移出
    **所有档位**的可新派集合，但按现行权威语义其历史在途 attempt 继续真实计数
    （计入 _domestic_active / 物理总 17 守卫）并按既有安全语义自然结束——绝不追杀、
    绝不假释放。要离线构造「物理全满 17」的总守卫场景，只能按真实形态直插这类遗留行；
    测试专用，绝不经 reserve/consume 伪造这些池的新派发。"""
    import dispatch_pool as _dp
    conn = _dp.connect(store)
    try:
        conn.execute(
            'INSERT INTO attempts(token, task_id, runtime, model, pool_key, state, '
            'reserved_at_utc) VALUES(?,?,?,?,?,?,?)',
            (token, task_id, runtime, model, pool_key, 'reserved',
             _dp._iso(_dp.utcnow())))
        conn.commit()
    finally:
        conn.close()


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

    def fill_non_opencode_weak_pools(self):
        """BW-WEAK-RESTRUCTURE-20261116: 弱档收窄为 5 池（两 OpenCode(1) > CBGLM(2) >
        Claude(3) > ZCode GLM-5.3-Flash(4)）；OpenCode 之上不再有旧弱档的 Max/ZCode/
        CodeBuddy deepseek 层。本方法真实占满**全部非 OpenCode 弱档池**（CBGLM 1 +
        claude 2 + ZCode GLM-5.3-Flash 1 = 4），全部经显式 executor 逐一 claim 保证
        确定性；此后 AUTO 只可能落到两 OpenCode 免费槽。"""
        toks = [self.reserve('cbg', 'codebuddy', dp.CODEBUDDY_GLM_MODEL,
                             executor='codebuddy')]
        toks.append(self.reserve('cl1', 'claude', 'deepseek-flash', executor='claude'))
        toks.append(self.reserve('cl2', 'claude', 'deepseek-flash', executor='claude'))
        toks.append(self.reserve('zcf1', 'zcode', 'GLM-5.3-Flash', executor='zcode'))
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
        # BW-WEAK-RESTRUCTURE-20261116: 物理总 17（原 16 + 弱档专用 zcode:GLM-5.3-Flash
        # 独立 1 槽；Sonus 槽仍在册）。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 17)


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
        # BW-WEAK-RESTRUCTURE-20261116: qoder:Qwen4-Max-Preview 已移出弱档（weak 一律
        # mode_excluded），本意「显式 qoder Max 绝不改派更低价 OpenCode」改在 strong 档
        # 验证（Max 是 strong 常态池、无时段门）。
        with dispatch_mode('strong'):
            # 夜间显式 qoder Max 请求：直接 claim 自身池，绝不改派更低价 OpenCode。
            r = self.select('q1', 'qoder', 'Qwen4-Max-Preview', executor='qoder')
            self.assertTrue(r.get('allowed'), r)
            self.assertEqual(r['pool_key'], 'qoder:Qwen4-Max-Preview')

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
        # 同一时刻两个 Qwen4-Max-Preview 都时段外拒派——对照证明 OpenCode 自身无时段门。
        # BW-WEAK-RESTRUCTURE-20261116: 全部 Qoder 池已移出弱档（weak 一律 mode_excluded），
        # 「Max 夜间门白天拒派」的本意改在 medium 档验证（medium 保留 Max 夜间门，
        # 北京 10:00 处于门外 → main_force_window_closed）。
        with dispatch_mode('medium'):
            # BW-QODERCN-TOKEN-MAX-DISABLE-20261028：带短横线的自定义 Max 现行被用户禁用
            # （medium 也在可派集合但被禁用集过滤）——禁用拒绝（mode_excluded，「用户要求
            # 禁用」）优先于时段门，先单独断言它；两个内置 Max 白天照旧
            # main_force_window_closed。
            r2d = dp.reserve(self.store, task_id='m2', runtime='qodercn',
                             model='Qwen-3.8-Max', workspace=str(self._ws_for('m2')),
                             prompt_sha256='p', executor='qodercn', now=DAY,
                             _preclaim=False)
            self.assertEqual(r2d.get('reason'), 'mode_excluded', r2d)
            for task, runtime, model, executor in (
                    ('m1', 'qodercn', 'Qwen4-Max-Preview', 'qodercn'),
                    ('m3', 'qoder', 'Qwen4-Max-Preview', 'qoder')):
                r2 = dp.reserve(self.store, task_id=task, runtime=runtime, model=model,
                                workspace=str(self._ws_for(task)), prompt_sha256='p',
                                executor=executor, now=DAY, _preclaim=False)
                self.assertEqual(r2.get('reason'), 'main_force_window_closed', r2)
        # strong 档：三个 Max 无夜间门，同一白天时刻照常可派；OpenCode 则被档位排除。
        with dispatch_mode('strong'):
            r3 = dp.reserve(self.store, task_id='s3', runtime='qoder',
                            model='Qwen4-Max-Preview', workspace=str(self._ws_for('s3')),
                            prompt_sha256='p', executor='qoder', now=DAY,
                            _preclaim=False)
            self.assertTrue(r3.get('allowed'), r3)
            self.assertEqual(r3['pool_key'], 'qoder:Qwen4-Max-Preview', r3)
            dp.finish(self.store, r3['token'], terminal='cancelled', now=DAY)
            r4 = dp.reserve(self.store, task_id='s4', runtime='opencode', model=DEEPSEEK,
                            workspace=str(self._ws_for('s4')), prompt_sha256='p',
                            executor='opencode', now=DAY, _preclaim=False)
            self.assertFalse(r4.get('allowed'), r4)
            self.assertEqual(r4.get('reason'), 'mode_excluded', r4)
            self.assertFalse(r4.get('sent', True), r4)

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
    def test_codebuddy_tier3_before_opencode_tier4(self):
        # BW-WEAK-RESTRUCTURE-20261116: codebuddy:deepseek-v4.1-flash 已从弱档移除
        # （weak 一律 mode_excluded），旧「CB deepseek 层先于 OpenCode」的前提不复存在。
        # 本用例保留两件事的真实口径：(1) weak 显式 CB deepseek 请求一律 mode_excluded
        # （sent=false、绝不静默改派 OpenCode）；(2) AUTO 层序里 OpenCode(tier1) 仍先于
        # 同为 CodeBuddy 的 GLM(tier2)。
        r_cb = dp.reserve(self.store, task_id='cb1', runtime='codebuddy',
                          model=dp.CODEBUDDY_MODEL,
                          workspace=str(self._ws_for('cb1')), prompt_sha256='p',
                          executor='codebuddy', now=NIGHT, _preclaim=False)
        self.assertFalse(r_cb.get('allowed'), r_cb)
        self.assertEqual(r_cb.get('reason'), 'mode_excluded', r_cb)
        self.assertFalse(r_cb.get('sent', True), r_cb)
        r = self.select('a0', 'codebuddy', dp.CODEBUDDY_GLM_MODEL)
        self.assertTrue(r.get('routing_required'), r)
        self.assertIn(r['selected']['pool_key'], (DPOOL, GPOOL), r)

    def test_auto_after_zcode_before_conditional_cbglm(self):
        # BW-WEAK-RESTRUCTURE-20261116: 弱档 ZCode 只用 GLM-5.3-Flash(tier4)，
        # zcode:GLM-5.3 已移出弱档（mode_excluded）。本用例改验：显式 zcode:GLM-5.3 请求
        # 在 weak 被拒；非 OpenCode 弱档池全满后 AUTO 仍落到 OpenCode(tier1)、先于 CBGLM。
        r_zc = dp.reserve(self.store, task_id='zc0', runtime='zcode', model='GLM-5.3',
                          workspace=str(self._ws_for('zc0')), prompt_sha256='p',
                          executor='zcode', now=NIGHT, _preclaim=False)
        self.assertFalse(r_zc.get('allowed'), r_zc)
        self.assertEqual(r_zc.get('reason'), 'mode_excluded', r_zc)
        self.fill_non_opencode_weak_pools()
        # 非 OpenCode 弱档池 4 槽全满 → AUTO 落到 tier1 的 OpenCode（而非 CBGLM）。
        r = self.select('a1', 'codebuddy', dp.CODEBUDDY_GLM_MODEL)
        self.assertTrue(r.get('routing_required'), r)
        self.assertFalse(r.get('sent', True), r)
        self.assertIn(r['selected']['pool_key'], (DPOOL, GPOOL), r)
        # 把 AUTO 选中的池真实占住（经显式 opencode 入口，确定性）。
        self.reserve('a1', 'opencode',
                     DEEPSEEK if r['selected']['pool_key'] == DPOOL else GLM)
        # 只剩另一个 OpenCode 槽：tier1 OpenCode 仍先于 tier2 CBGLM（GLM 额度独立始终候选）。
        r2 = self.select('a2', 'codebuddy', dp.CODEBUDDY_GLM_MODEL)
        self.assertTrue(r2.get('routing_required'), r2)
        self.assertIn(r2['selected']['pool_key'], (DPOOL, GPOOL), r2)

    def test_auto_falls_to_cbglm_then_flash_when_opencode_full(self):
        # BW-WEAK-RESTRUCTURE-20261116: 两地区 Flash（qoder/qodercn:Qwen3.8-Flash）已从
        # 弱档移除，弱档兜底链改为 OpenCode(1) → CBGLM(2) → Claude(3) → ZCode
        # GLM-5.3-Flash(4)；本用例沿新链验证逐层回落，末层 GLM-5.3-Flash 直接 claim。
        self.reserve('of1', 'opencode', DEEPSEEK)
        self.reserve('of2', 'opencode', GLM)
        # 两 OpenCode(1) 满 → tier2 CBGLM 先于 Claude/ZCodeFlash（GLM 额度独立、始终候选）。
        r = self.select('a1', 'zcode', 'GLM-5.3-Flash')
        self.assertTrue(r.get('routing_required'), r)
        self.assertEqual(r['selected']['pool_key'], dp.CODEBUDDY_GLM_POOL_KEY, r)
        # CBGLM 与 claude 也满 → 弱档末层 ZCode GLM-5.3-Flash 兜底。
        self.reserve('cbg', 'codebuddy', dp.CODEBUDDY_GLM_MODEL, executor='codebuddy')
        self.reserve('cl1', 'claude', 'deepseek-flash', executor='claude')
        self.reserve('cl2', 'claude', 'deepseek-flash', executor='claude')
        r2 = self.select('a2', 'zcode', 'GLM-5.3-Flash')
        # 请求入口本身已是末层组合：直接 claim 自身查询池，正确返回 allowed/claimed
        # 而非 routing_required。
        self.assertTrue(r2.get('allowed'), r2)
        self.assertTrue(r2.get('claimed'), r2)
        self.assertEqual(r2['pool_key'], dp.ZCODE_FLASH_POOL_KEY, r2)

    def test_two_opencode_pools_balanced_same_tier(self):
        # BW-WEAK-RESTRUCTURE-20261116: OpenCode 现为弱档 tier1，无需再预填上层；
        # 用 AUTO 请求 CBGLM(tier2) 组合确保每轮都 routing 到 OpenCode 层。
        picks = []
        for i in range(2):
            r = self.select(f'bal{i}', 'codebuddy', dp.CODEBUDDY_GLM_MODEL)
            self.assertTrue(r.get('routing_required'), r)
            pk = r['selected']['pool_key']
            self.assertIn(pk, (DPOOL, GPOOL), r)
            picks.append(pk)
            self.reserve(f'bal{i}', 'opencode',
                         DEEPSEEK if pk == DPOOL else GLM)
        # 两槽同层按真实在途平衡：两轮 AUTO 应分到两个不同池。
        self.assertEqual(set(picks), {DPOOL, GPOOL}, picks)

    def test_opencode_beats_conditional_cbglm_even_when_cbglm_first_free(self):
        # BW-WEAK-RESTRUCTURE-20261116: 新弱档层序下 OpenCode(tier1) 仍先于 CBGLM(tier2)。
        self.reserve('od', 'opencode', DEEPSEEK)
        # deepseek 槽已满、glm-5.3-flash-free 仍空；GLM 始终参与候选 → tier1 OpenCode
        # 仍先于 tier2 CodeBuddy GLM。
        r = self.select('a1', 'codebuddy', dp.CODEBUDDY_GLM_MODEL)
        self.assertTrue(r.get('routing_required'), r)
        self.assertEqual(r['selected']['pool_key'], GPOOL, r)


class TotalGuardSixteenTests(_PoolBase):
    def test_all_fifteen_weak_slots_plus_sonus_leftover(self):
        # BW-WEAK-RESTRUCTURE-20261116: weak 可新派容量收窄为 6（5 池：两 OpenCode +
        # CBGLM + claude 2 + ZCode GLM-5.3-Flash）；物理总 16→17。旧弱档独占池（两地区
        # Flash、codebuddy deepseek）与 strong/medium 专属池（三 Max、zcode:GLM-5.3、
        # Sonus）按各自真实形态补齐，构造物理全满 17 的总守卫场景。
        toks = []                                        # weak 6 槽（真实新派）
        toks.append(self.reserve('od', 'opencode', DEEPSEEK))
        toks.append(self.reserve('og', 'opencode', GLM))
        toks.append(self.reserve('cbg', 'codebuddy', dp.CODEBUDDY_GLM_MODEL,
                                 executor='codebuddy'))
        toks.append(self.reserve('cl1', 'claude', 'deepseek-flash', executor='claude'))
        toks.append(self.reserve('cl2', 'claude', 'deepseek-flash', executor='claude'))
        toks.append(self.reserve('zcf', 'zcode', 'GLM-5.3-Flash', executor='zcode'))
        # strong 档专属池 3+2 槽（Qwen4-Max-Preview 两内置 + zcode:GLM-5.3；
        # CN 自定义 Max 2 槽按禁用前 legacy 占位构造——临时空禁用集）。
        with dispatch_mode('strong'):
            toks.append(self.reserve('qm', 'qoder', 'Qwen4-Max-Preview', executor='qoder'))
            toks.append(self.reserve('cm', 'qodercn', 'Qwen4-Max-Preview', executor='qodercn'))
            toks.append(self.reserve('zc1', 'zcode', 'GLM-5.3', executor='zcode'))
            with user_disabled_pools(frozenset()):
                toks.append(self.reserve('cx1', 'qodercn', 'Qwen-3.8-Max',
                                         executor='qodercn'))
                toks.append(self.reserve('cx2', 'qodercn', 'Qwen-3.8-Max',
                                         executor='qodercn'))
        # strong 遗留 Sonus 在途（weak/medium 永不可新派）按真实形态直插。
        insert_sonus_leftover(self.store)                 # 12
        st = dp.status(self.store, now=NIGHT)
        self.assertEqual(st['domestic']['active'], 12)
        self.assertFalse(st['domestic']['full'])
        # 已被移出所有档位的池（CB deepseek 1 + 两地区 Flash 各 2 = 5 槽）按「重构前派出、
        # 仍在途」的历史形态直插，补齐物理全满 17。
        insert_legacy_inflight(self.store, token='cbds-left', task_id='cbds-left',
                               pool_key=dp.CODEBUDDY_POOL_KEY, runtime='codebuddy',
                               model=dp.CODEBUDDY_MODEL)
        insert_legacy_inflight(self.store, token='qf1-left', task_id='qf1-left',
                               pool_key=dp.QFLASH_POOL_KEY, runtime='qoder',
                               model='Qwen3.8-Flash')
        insert_legacy_inflight(self.store, token='qf2-left', task_id='qf2-left',
                               pool_key=dp.QFLASH_POOL_KEY, runtime='qoder',
                               model='Qwen3.8-Flash')
        insert_legacy_inflight(self.store, token='cf1-left', task_id='cf1-left',
                               pool_key=dp.CN_FLASH_POOL_KEY, runtime='qodercn',
                               model='Qwen3.8-Flash')
        insert_legacy_inflight(self.store, token='cf2-left', task_id='cf2-left',
                               pool_key=dp.CN_FLASH_POOL_KEY, runtime='qodercn',
                               model='Qwen3.8-Flash')
        st = dp.status(self.store, now=NIGHT)
        self.assertEqual(st['domestic']['active'], 17)
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 17)
        self.assertTrue(st['domestic']['full'])
        r = dp.reserve(self.store, task_id='over', runtime='opencode', model=DEEPSEEK,
                       workspace=str(self._ws_for('over')), prompt_sha256='p',
                       executor='opencode', now=NIGHT, _preclaim=False)
        self.assertFalse(r.get('allowed'), r)
        self.assertEqual(r.get('reason'), 'capacity_full', r)
        self.assertTrue(r.get('domestic_full'), r)
        self.assertFalse(r.get('sent', True), r)
        # 释放一个 OpenCode 槽（toks[0]=od）→ 恢复可派（自己结束只影响自己）。
        fin = dp.finish(self.store, toks[0], terminal='start_failed', now=NIGHT)
        self.assertTrue(fin.get('released'), fin)
        r2 = dp.reserve(self.store, task_id='after', runtime='opencode', model=DEEPSEEK,
                        workspace=str(self._ws_for('after')), prompt_sha256='p',
                        executor='opencode', now=NIGHT, _preclaim=False)
        self.assertTrue(r2.get('allowed'), r2)


if __name__ == '__main__':
    unittest.main()
