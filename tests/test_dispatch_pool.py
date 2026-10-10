"""dispatch_pool 离线验收：并发容量池 + 1:1 路由 + Luna 票据竞争裁决。

只用临时 sqlite（每个用例独立 store），绝不触碰真实 ~/.brain-worker 池，绝不 Popen 真实
CLI。reconcile 用可注入 prober（假 PID 存活/创建身份），不做任何真实进程探测。
覆盖 references/global-dispatch.md 列出的全部裁决口径。

Z4/最终候选修订（BW-GITHUB-CLOSEOUT-20261008-S6）：
- fixture 修正：不同 task 不再共享同一虚拟 workspace（真实单写入守卫下会被正确拒绝），
  每个 task 用 base/ws/<task> 独立真实目录；生产守卫语义不放松，只改错误 fixture。
- 用户语义：2 Zcode + 2 Max + 2 Flash，六满 + 真实询问 + 300 秒才 Luna 救援（无 cap）；
  国内释放优先同事务回收，同 task 绝不双派。fill_domestic 按 1:1 轮转交替填充。
- 移除已废弃的 exact_combo（plan 不得绕过 1:1 轮转）。
- finish/reconcile 断言对齐当前语义：reserved 无 child 只能 start_failed/cancelled 释放；
  child 死（真实 Windows 死 PID created=None）释放；child 活且出生匹配保持；child 活但
  出生漂移/未知（PID 复用嫌疑）→ unknown 不抢占；从未消费的合法预留 reconcile 保持
  reserved（绝不翻 unknown），仍可被 consume 或 cancel 单赢家结算。
"""
import os
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_pool as dp  # noqa: E402

T0 = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)  # 北京时间 23:00（主力时段内）


class _PoolBase(unittest.TestCase):
    """每个用例一个独立临时 store；workspace 用真实存在的临时目录，避免 realpath 归一化噪声。

    不同 task 使用 base/ws/<task> 独立真实目录——真实单写入守卫下不同写入者共享同一目录
    会被正确拒绝，fixture 绝不虚拟同一路径来掩盖守卫。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-pool-w-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(str(self.store), str(dp.default_store_path()))
        self.saved_env = os.environ.pop('BRAIN_WORKER_DISPATCH_STORE', None)
        # 单写入者用例可复用的默认目录。
        self.ws = self._ws_for('default')
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        if self.saved_env is not None:
            os.environ['BRAIN_WORKER_DISPATCH_STORE'] = self.saved_env

    def _ws_for(self, task):
        d = self.base / 'ws' / str(task)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _scope(self, task_id):
        return {'task_id': task_id, 'stage': 's1', 'chat_id': 'c1',
                'workspace': str(self._ws_for(task_id)), 'prompt_sha256': 'p' * 64}

    def _state(self, tok):
        with closing(dp.connect(self.store)) as conn:
            r = conn.execute('SELECT state FROM attempts WHERE token=?', (tok,)).fetchone()
            return r['state'] if r is not None else None

    # ---- 便捷断言 ----
    def assertClaimed(self, res, pk):
        self.assertTrue(res.get('allowed'), res)
        self.assertTrue(res.get('claimed') or res.get('reserved'), res)
        self.assertEqual(res['pool_key'], pk, res)
        self.assertFalse(res.get('routing_required'), res)
        return res['token']

    def assertRouting(self, res, target_pk):
        self.assertFalse(res.get('allowed'), res)
        self.assertTrue(res.get('routing_required'), res)
        self.assertFalse(res.get('sent', True), res)
        self.assertEqual(res['selected']['pool_key'], target_pk, res)

    def assertRejected(self, res, reason):
        self.assertFalse(res.get('allowed'), res)
        self.assertEqual(res.get('reason'), reason, res)

    # ---- 便捷动作 ----
    def reserve(self, task, runtime, model, pk=None, ws=None, prompt='p',
                stage=None, chat_id=None):
        r = dp.reserve(self.store, task_id=task, runtime=runtime, model=model,
                       workspace=str(ws if ws is not None else self._ws_for(task)),
                       prompt_sha256=prompt, stage=stage, chat_id=chat_id,
                       now=T0, _preclaim=False)
        self.assertClaimed(r, pk or dp.pool_key(runtime, model))
        return r['token']

    def select(self, task, runtime, model):
        return dp.select_and_claim(self.store, task_id=task, runtime=runtime, model=model,
                                   workspace=str(self._ws_for(task)), prompt_sha256='p',
                                   now=T0, _preclaim=False)

    def reserve_exec(self, task, runtime, model, executor, pk=None):
        """显式执行器入口的确定性 claim：CN 内置只能经 executor='qodercn' 单独 claim，
        AUTO 永不自动选中 CN；每个显式 executor 都 assert allowed 后取真实 reserved token。"""
        r = dp.reserve(self.store, task_id=task, runtime=runtime, model=model,
                       workspace=str(self._ws_for(task)), prompt_sha256='p',
                       executor=executor, now=T0, _preclaim=False)
        self.assertClaimed(r, pk or dp.pool_key(runtime, model))
        return r['token']

    def fill_domestic(self):
        """把十个国内名额填满（BW-MAX-WINDOW-20261010-S2 真实六池 10 槽）：主力
        zcode:GLM-5.3=2、qoder:Qwen3.8-Max=1、qodercn:Qwen3.8-Max=1、qodercn:Qwen-3.8-Max=2，
        同级兜底 qoder:Qwen3.8-Flash=2、qodercn:Qwen3.8-Flash=2。全部经显式 executor 逐一 claim
        以保证确定性（CN 池只能经 executor='qodercn' 入口，AUTO 永不自动选中 CN 内置）。
        返回 token 列表，顺序为
        [zcode, zcode, qoderMax, cnMax, cnCustomA, cnCustomB,
         qoderFlash, qoderFlash, cnFlash, cnFlash]。
        """
        plan = [('f0', 'zcode', 'GLM-5.3', 'zcode'),
                ('f1', 'zcode', 'GLM-5.3', 'zcode'),
                ('f2', 'qoder', 'Qwen3.8-Max', 'qoder'),
                ('f3', 'qodercn', 'Qwen3.8-Max', 'qodercn'),
                ('f4', 'qodercn', 'Qwen-3.8-Max', 'qodercn'),
                ('f5', 'qodercn', 'Qwen-3.8-Max', 'qodercn'),
                ('f6', 'qoder', 'Qwen3.8-Flash', 'qoder'),
                ('f7', 'qoder', 'Qwen3.8-Flash', 'qoder'),
                ('f8', 'qodercn', 'Qwen3.8-Flash', 'qodercn'),
                ('f9', 'qodercn', 'Qwen3.8-Flash', 'qodercn')]
        toks = [self.reserve_exec(t, rt, md, ex) for (t, rt, md, ex) in plan]
        st = dp.status(self.store, now=T0)
        self.assertEqual(st['domestic']['active'], dp.DOMESTIC_TOTAL_CAPACITY)
        self.assertTrue(st['domestic']['full'])
        self.assertEqual(st['pools']['zcode:GLM-5.3']['active'], 2)
        self.assertEqual(st['pools']['qoder:Qwen3.8-Max']['active'], 1)
        self.assertEqual(st['pools']['qodercn:Qwen3.8-Max']['active'], 1)
        self.assertEqual(st['pools']['qodercn:Qwen-3.8-Max']['active'], 2)
        self.assertEqual(st['pools']['qoder:Qwen3.8-Flash']['active'], 2)
        self.assertEqual(st['pools']['qodercn:Qwen3.8-Flash']['active'], 2)
        return toks


class CapacityAndKnownPoolTests(_PoolBase):
    def test_capacity_table_matches_allocation_decision(self):
        # BW-MAX-WINDOW-20261010-S2 真实六池 10 槽：ZCode=2、两内置 Max 各 1、CN 自定义 Max=2、
        # 两地区 Flash 各 2；CN DeepSeek-Flash 已退出正常池，保留池名但 capacity=0。
        self.assertEqual(dp.capacity_for('zcode:GLM-5.3'), 2)
        self.assertEqual(dp.capacity_for('qoder:Qwen3.8-Max'), 1)
        self.assertEqual(dp.capacity_for('qodercn:Qwen3.8-Max'), 1)
        self.assertEqual(dp.capacity_for('qodercn:Qwen-3.8-Max'), 2)
        self.assertEqual(dp.capacity_for('qoder:Qwen3.8-Flash'), 2)
        self.assertEqual(dp.capacity_for('qodercn:Qwen3.8-Flash'), 2)
        self.assertEqual(dp.capacity_for('qodercn:DeepSeek-Flash'), 0)
        self.assertIsNone(dp.capacity_for('luna:native'))
        self.assertEqual(dp.capacity_for('qoder:Unknown-Model'), 0)
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 10)

    def test_is_known_pool(self):
        for pk in ('zcode:GLM-5.3', 'qoder:Qwen3.8-Max',
                   'qoder:Qwen3.8-Flash', 'qodercn:DeepSeek-Flash', 'luna:native'):
            self.assertTrue(dp.is_known_pool(pk), pk)
        self.assertFalse(dp.is_known_pool('qoder:GLM-5.3'))
        self.assertFalse(dp.is_known_pool('zcode:Qwen3.8-Max'))

    def test_unknown_pool_refused_on_reserve_and_select(self):
        r = dp.reserve(self.store, task_id='u1', runtime='qoder', model='Bogus',
                       workspace=str(self._ws_for('u1')), prompt_sha256='p',
                       now=T0, _preclaim=False)
        self.assertRejected(r, 'unknown_pool')
        self.assertFalse(r.get('sent', True))
        r2 = dp.select_and_claim(self.store, task_id='u2', runtime='openai', model='gpt-x',
                                 workspace=str(self._ws_for('u2')), prompt_sha256='p',
                                 now=T0, _preclaim=False)
        self.assertRejected(r2, 'unknown_pool')

    def test_luna_never_auto_selected_or_reserved_as_normal(self):
        r = dp.select_and_claim(self.store, task_id='l1', runtime='luna', model='native',
                                workspace=str(self._ws_for('l1')), prompt_sha256='p',
                                now=T0, _preclaim=False)
        self.assertRejected(r, 'luna_requires_ticket')
        self.assertFalse(r.get('sent', True))

    def test_reserve_respects_pool_capacity(self):
        # 1:1 轮转下不能连续预留同一主力；合法填满六槽后任何国内组合都 capacity_full。
        self.fill_domestic()
        over = dp.reserve(self.store, task_id='c2', runtime='zcode', model='GLM-5.3',
                          workspace=str(self._ws_for('c2')), prompt_sha256='p',
                          now=T0, _preclaim=False)
        self.assertRejected(over, 'capacity_full')
        self.assertTrue(over.get('domestic_full'))
        # 单池上限 2 从未被突破。
        self.assertEqual(dp.status(self.store, now=T0)['pools']['zcode:GLM-5.3']['active'], 2)

    def test_duplicate_task_in_flight_refused(self):
        self.reserve('dup', 'zcode', 'GLM-5.3')
        r = dp.reserve(self.store, task_id='dup', runtime='qoder', model='Qwen3.8-Max',
                       workspace=str(self._ws_for('dup')), prompt_sha256='p',
                       now=T0, _preclaim=False)
        self.assertRejected(r, 'duplicate_task_in_flight')
        r2 = dp.select_and_claim(self.store, task_id='dup', runtime='qoder',
                                 model='Qwen3.8-Max', workspace=str(self._ws_for('dup')),
                                 prompt_sha256='p', now=T0, _preclaim=False)
        self.assertRejected(r2, 'duplicate_task_in_flight')


class MainForceRotationTests(_PoolBase):
    def test_select_balances_1_1_by_committed_count(self):
        # 请求 zcode，但 committed 平票且 zcode 排在前（MAIN_FORCE_KEYS 顺序）→ 选中 zcode。
        r1 = self.select('t1', 'zcode', 'GLM-5.3')
        self.assertClaimed(r1, 'zcode:GLM-5.3')
        # 现在 committed_zcode=1 > committed_qoder=0：请求 zcode 会被轮换到 qoder。
        r2 = self.select('t2', 'zcode', 'GLM-5.3')
        self.assertRouting(r2, 'qoder:Qwen3.8-Max')
        self.assertFalse(r2.get('sent', True))

    def test_select_prefers_requested_combo_on_tie(self):
        r = self.select('tie', 'qoder', 'Qwen3.8-Max')
        # 平票时优先请求组合 → qoder 被选中而非轮换到 zcode。
        self.assertClaimed(r, 'qoder:Qwen3.8-Max')

    def test_select_when_both_main_full_routes_to_overflow(self):
        # 全部主力填满（zcode×2 + 两内置 Max 各 1 + CN 自定义 Max×2）后经显式 executor 确定性
        # 占位，AUTO select zcode 主力全满 → 同级 Flash 兜底，两区平票按 pool_key stable tie 选国际 Flash。
        self.reserve_exec('m0', 'zcode', 'GLM-5.3', 'zcode')
        self.reserve_exec('m1', 'zcode', 'GLM-5.3', 'zcode')
        self.reserve_exec('m2', 'qoder', 'Qwen3.8-Max', 'qoder')
        self.reserve_exec('m3', 'qodercn', 'Qwen3.8-Max', 'qodercn')
        self.reserve_exec('m4', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
        self.reserve_exec('m5', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
        r = self.select('of', 'zcode', 'GLM-5.3')
        self.assertRouting(r, 'qoder:Qwen3.8-Flash')

    def test_select_capacity_full_when_all_six_occupied(self):
        self.fill_domestic()
        r = self.select('full', 'zcode', 'GLM-5.3')
        self.assertRejected(r, 'capacity_full')
        self.assertTrue(r.get('domestic_full'))
        self.assertFalse(r.get('sent', True))


class OverflowRoutingTests(_PoolBase):
    def test_flash_refused_while_main_force_has_room(self):
        r = self.select('f0', 'qoder', 'Qwen3.8-Flash')
        # 主力仍有空位 → Flash 不允许，routing 到某主力组合。
        self.assertTrue(r.get('routing_required'), r)
        self.assertIn(r['selected']['pool_key'], dp.MAIN_FORCE_KEYS)

    def test_flash_allowed_when_main_force_full(self):
        # 全部主力填满后，AUTO 直接请求国际 Flash → 主力满不再拦，同级 Flash 有空位即 claim。
        self.reserve_exec('m0', 'zcode', 'GLM-5.3', 'zcode')
        self.reserve_exec('m1', 'zcode', 'GLM-5.3', 'zcode')
        self.reserve_exec('m2', 'qoder', 'Qwen3.8-Max', 'qoder')
        self.reserve_exec('m3', 'qodercn', 'Qwen3.8-Max', 'qodercn')
        self.reserve_exec('m4', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
        self.reserve_exec('m5', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
        r = self.select('fok', 'qoder', 'Qwen3.8-Flash')
        self.assertClaimed(r, 'qoder:Qwen3.8-Flash')

    def test_flash_capacity_full_after_two(self):
        self.reserve_exec('m0', 'zcode', 'GLM-5.3', 'zcode')
        self.reserve_exec('m1', 'zcode', 'GLM-5.3', 'zcode')
        self.reserve_exec('m2', 'qoder', 'Qwen3.8-Max', 'qoder')
        self.reserve_exec('m3', 'qodercn', 'Qwen3.8-Max', 'qodercn')
        self.reserve_exec('m4', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
        self.reserve_exec('m5', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
        self.reserve_exec('f0', 'qoder', 'Qwen3.8-Flash', 'qoder')
        self.reserve_exec('f1', 'qoder', 'Qwen3.8-Flash', 'qoder')
        # AUTO 入口请求国际 Flash：三主力与国际 Flash 均满，CN Flash 同级兜底仍有空位 →
        # routing_required 选 CN Flash（不因自身满就报整池 full），且未真正占槽。
        r = self.select('f2', 'qoder', 'Qwen3.8-Flash')
        self.assertFalse(r.get('allowed'), r)
        self.assertTrue(r.get('routing_required'), r)
        self.assertEqual(r['selected']['pool_key'], dp.CN_FLASH_POOL_KEY)
        self.assertFalse(r.get('domestic_full'), r)
        # 显式 executor='qoder'：Flash 满仍按该 combo 自身容量判满，绝不扩指定执行器到 CN。
        q = dp.reserve(self.store, task_id='fq', runtime='qoder', model='Qwen3.8-Flash',
                       workspace=str(self._ws_for('fq')), prompt_sha256='p',
                       executor='qoder', now=T0, _preclaim=False)
        self.assertRejected(q, 'capacity_full')
        self.assertFalse(q.get('domestic_full'), q)


class SameWorkspaceGuardTests(_PoolBase):
    def test_same_real_workspace_two_writers_only_one_claims(self):
        # 真正同一真实目录：不同 task 竞争同一 workspace，只有一个能 claim（守卫不放松）。
        shared = self._ws_for('shared')
        r1 = dp.reserve(self.store, task_id='w1', runtime='zcode', model='GLM-5.3',
                        workspace=str(shared), prompt_sha256='p', now=T0, _preclaim=False)
        self.assertClaimed(r1, 'zcode:GLM-5.3')
        r2 = dp.reserve(self.store, task_id='w2', runtime='qoder', model='Qwen3.8-Max',
                        workspace=str(shared), prompt_sha256='p', now=T0, _preclaim=False)
        self.assertFalse(r2.get('allowed'), r2)
        self.assertEqual(r2.get('reason'), 'workspace_in_flight')
        # 释放 w1 后，同一真实目录恢复可写。
        dp.finish(self.store, r1['token'], terminal='cancelled', now=T0)
        r3 = dp.reserve(self.store, task_id='w2', runtime='qoder', model='Qwen3.8-Max',
                        workspace=str(shared), prompt_sha256='p', now=T0, _preclaim=False)
        self.assertTrue(r3.get('allowed'), r3)


class ValidateClaimTests(_PoolBase):
    def test_validate_ok_when_exact(self):
        tok = self.reserve('v', 'zcode', 'GLM-5.3', prompt='abc123')
        chk = dp.validate_claim(self.store, tok, task_id='v', runtime='zcode',
                                model='GLM-5.3', workspace=str(self._ws_for('v')),
                                prompt_sha256='abc123')
        self.assertTrue(chk['ok'], chk['reasons'])
        self.assertFalse(chk['drift'])

    def test_validate_detects_each_drift(self):
        tok = self.reserve('v', 'zcode', 'GLM-5.3', prompt='abc123')
        ws = str(self._ws_for('v'))
        self.assertTrue(dp.validate_claim(self.store, tok, model='Qwen3.8-Max')['drift'])
        self.assertTrue(dp.validate_claim(self.store, tok, runtime='qoder')['drift'])
        self.assertTrue(dp.validate_claim(self.store, tok, task_id='other')['drift'])
        self.assertTrue(dp.validate_claim(self.store, tok, prompt_sha256='zzz')['drift'])
        self.assertTrue(dp.validate_claim(self.store, tok,
                                          workspace=str(self._ws_for('other')))['drift'])

    def test_validate_unknown_token(self):
        chk = dp.validate_claim(self.store, 'no-such-token')
        self.assertFalse(chk['ok'])
        self.assertTrue(chk['drift'])

    def test_validate_terminal_claim_cannot_consume(self):
        tok = self.reserve('v', 'zcode', 'GLM-5.3')
        # reserved 无 child：只有 cancelled/start_failed 合法释放（finished 需 child 证据）。
        dp.finish(self.store, tok, terminal='cancelled', now=T0)
        chk = dp.validate_claim(self.store, tok, task_id='v')
        self.assertFalse(chk['ok'])
        self.assertTrue(any('terminal' in r for r in chk['reasons']), chk['reasons'])


class ConsumeForEntryTests(_PoolBase):
    def test_consume_with_valid_claim_token_marks_running(self):
        tok = self.reserve('e', 'qoder', 'Qwen3.8-Max', prompt='pp')
        r = dp.consume_for_entry(self.store, task_id='e', runtime='qoder',
                                 model='Qwen3.8-Max', workspace=str(self._ws_for('e')),
                                 prompt_sha256='pp', claim_token=tok,
                                 wrapper_pid=4321, wrapper_created='w1', now=T0)
        self.assertTrue(r['allowed'], r)
        self.assertTrue(r['claimed'])
        self.assertEqual(r['token'], tok)
        self.assertEqual(self._state(tok), 'running')
        st = dp.status(self.store, now=T0)['pools']['qoder:Qwen3.8-Max']
        self.assertEqual(st['active'], 1)

    def test_consume_with_drifted_claim_refused(self):
        tok = self.reserve('e', 'qoder', 'Qwen3.8-Max', prompt='pp')
        r = dp.consume_for_entry(self.store, task_id='e', runtime='qoder',
                                 model='Qwen3.8-Flash', workspace=str(self._ws_for('e')),
                                 prompt_sha256='pp', claim_token=tok,
                                 wrapper_pid=4321, wrapper_created='w1', now=T0)
        self.assertFalse(r['allowed'], r)
        self.assertEqual(r['reason'], 'claim_invalid')
        self.assertFalse(r.get('sent', True))
        self.assertTrue(r.get('drift'))
        # 漂移拒绝后 token 从未被消费，仍 reserved。
        self.assertEqual(self._state(tok), 'reserved')

    def test_consume_without_token_routes(self):
        # 无 token → 走 select_and_claim；请求主力在空池 → claim 成功。
        r = dp.consume_for_entry(self.store, task_id='n', runtime='zcode',
                                 model='GLM-5.3', workspace=str(self._ws_for('n')),
                                 prompt_sha256='p', wrapper_pid=4321, wrapper_created='w1',
                                 now=T0)
        self.assertTrue(r['allowed'], r)
        self.assertTrue(r['claimed'])
        self.assertEqual(r['pool_key'], 'zcode:GLM-5.3')

    def test_consume_without_token_can_route_wrong_combo(self):
        self.reserve('z0', 'zcode', 'GLM-5.3')
        r = dp.consume_for_entry(self.store, task_id='n2', runtime='zcode',
                                 model='GLM-5.3', workspace=str(self._ws_for('n2')),
                                 prompt_sha256='p', wrapper_pid=4321, wrapper_created='w1',
                                 now=T0)
        self.assertFalse(r['allowed'], r)
        self.assertTrue(r['routing_required'])
        self.assertFalse(r.get('sent', True))

    def test_consume_without_token_enforces_capacity_and_dedup(self):
        # exact_combo 已废弃：无 token 的自动消费仍严格受容量与防重约束，不得绕过 1:1。
        self.fill_domestic()
        over = dp.consume_for_entry(self.store, task_id='new', runtime='qoder',
                                    model='Qwen3.8-Flash',
                                    workspace=str(self._ws_for('new')),
                                    prompt_sha256='p', wrapper_pid=4321,
                                    wrapper_created='w1', now=T0)
        self.assertRejected(over, 'capacity_full')
        dup = dp.consume_for_entry(self.store, task_id='f0', runtime='zcode',
                                   model='GLM-5.3', workspace=str(self._ws_for('f0')),
                                   prompt_sha256='p', wrapper_pid=4321,
                                   wrapper_created='w1', now=T0)
        self.assertRejected(dup, 'duplicate_task_in_flight')


class ReleaseAndUnknownTests(_PoolBase):
    def test_cancel_releases_reserved_capacity_immediately(self):
        tok = self.reserve('r', 'zcode', 'GLM-5.3')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['zcode:GLM-5.3']['active'], 1)
        res = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertTrue(res['released'])
        self.assertEqual(dp.status(self.store, now=T0)['pools']['zcode:GLM-5.3']['active'], 0)

    def test_finished_on_reserved_no_child_refused(self):
        # finished 需要 child 真实退出证据；reserved 无 child 用 finished → 拒绝，不释放。
        tok = self.reserve('r', 'zcode', 'GLM-5.3')
        res = dp.finish(self.store, tok, terminal='finished', now=T0)
        self.assertFalse(res['released'], res)
        self.assertEqual(res.get('reason'), 'no_child_terminal_unverified')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['zcode:GLM-5.3']['active'], 1)

    def test_start_failed_and_cancelled_release(self):
        t1 = self.reserve('a', 'zcode', 'GLM-5.3')
        t2 = self.reserve('b', 'qoder', 'Qwen3.8-Max')
        dp.finish(self.store, t1, terminal='start_failed', now=T0)
        dp.finish(self.store, t2, terminal='cancelled', now=T0)
        st = dp.status(self.store, now=T0)['pools']
        self.assertEqual(st['zcode:GLM-5.3']['active'], 0)
        self.assertEqual(st['qoder:Qwen3.8-Max']['active'], 0)

    def test_finish_rejects_non_release_terminal(self):
        tok = self.reserve('r', 'zcode', 'GLM-5.3')
        with self.assertRaises(ValueError):
            dp.finish(self.store, tok, terminal='unknown')

    def test_double_finish_second_is_noop(self):
        tok = self.reserve('r', 'zcode', 'GLM-5.3')
        self.assertTrue(dp.finish(self.store, tok, terminal='cancelled', now=T0)['released'])
        self.assertFalse(dp.finish(self.store, tok, terminal='cancelled', now=T0)['released'])

    def test_mark_unknown_holds_capacity(self):
        tok = self.reserve('u', 'qoder', 'Qwen3.8-Max')
        res = dp.mark_unknown(self.store, tok, now=T0)
        self.assertTrue(res['capacity_held'])
        self.assertFalse(res['released'])
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen3.8-Max']['active'], 1)

    def test_finish_unknown_no_child_requires_owner(self):
        # 进入 Popen-bind 窗口（有 wrapper）后 mark_unknown：无 owner 身份绝不释放；
        # 记录的真实 owner 亲证取消才释放（绝不凭一个终态参数假定未启动）。
        tok = self.reserve('u', 'qoder', 'Qwen3.8-Max')
        dp.mark_running(self.store, tok, wrapper_pid=7000, wrapper_created='c7000', now=T0)
        dp.mark_unknown(self.store, tok, now=T0)
        refused = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertFalse(refused['released'], refused)
        self.assertEqual(refused.get('reason'), 'owner_unverified')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen3.8-Max']['active'], 1)
        released = dp.finish(self.store, tok, terminal='cancelled', now=T0,
                             wrapper_pid=7000, wrapper_created='c7000')
        self.assertTrue(released['released'], released)
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen3.8-Max']['active'], 0)

    def test_real_child_death_releases_before_report(self):
        # 绑定真实 child 后其死亡（真实 Windows 死 PID created=None）→ finish 释放容量。
        tok = self.reserve('d', 'zcode', 'GLM-5.3')
        dp.mark_running(self.store, tok, wrapper_pid=8000, wrapper_created='c8000', now=T0)
        dp.bind_child(self.store, tok, 8001, child_created='c8001',
                      wrapper_pid=8000, wrapper_created='c8000', now=T0)
        res = dp.finish(self.store, tok, terminal='finished', now=T0,
                        prober=_fake_prober({8001: ('dead', None)}))
        self.assertTrue(res['released'], res)
        self.assertEqual(dp.status(self.store, now=T0)['pools']['zcode:GLM-5.3']['active'], 0)


def _fake_prober(live_map):
    """可注入 prober：pid -> (state, created)。绝不真实探测进程。"""
    def probe(pid):
        state, created = live_map.get(int(pid), ('dead', None))
        return {'pid': int(pid), 'state': state, 'created': created}
    return probe


class ReconcileTests(_PoolBase):
    def _bind(self, tok, wrapper_pid, child_pid=None, wrapper_created='w',
              child_created=None):
        dp.mark_running(self.store, tok, wrapper_pid=wrapper_pid,
                        wrapper_created=wrapper_created, now=T0)
        if child_pid is not None:
            dp.bind_child(self.store, tok, child_pid, child_created=child_created,
                          wrapper_pid=wrapper_pid, wrapper_created=wrapper_created, now=T0)

    def test_wrapper_alive_promotes_reserved_to_running(self):
        tok = self.reserve('p', 'zcode', 'GLM-5.3')
        dp.mark_running(self.store, tok, wrapper_pid=1000, wrapper_created='c1000', now=T0)
        # 手动退回 reserved 以验证 promote 分支（closing 确保 Windows 下句柄释放）。
        with closing(dp.connect(self.store)) as conn:
            conn.execute("UPDATE attempts SET state='reserved' WHERE token=?", (tok,))
        res = dp.reconcile(self.store, prober=_fake_prober({1000: ('alive', 'c1000')}),
                           now=T0)
        self.assertIn(tok, res['promoted'])
        self.assertEqual(self._state(tok), 'running')

    def test_wrapper_dead_child_alive_match_kept(self):
        tok = self.reserve('q', 'zcode', 'GLM-5.3')
        self._bind(tok, wrapper_pid=2000, child_pid=2001,
                   wrapper_created='c2000', child_created='c2001')
        res = dp.reconcile(self.store, prober=_fake_prober(
            {2000: ('dead', 'c2000'), 2001: ('alive', 'c2001')}), now=T0)
        # child 活且出生匹配 → 保持在途，绝不释放（wrapper 死不影响正在运行的 child）。
        self.assertNotIn(tok, res['released'])
        self.assertFalse(any(h['token'] == tok for h in res['held']), res)
        self.assertEqual(self._state(tok), 'running')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['zcode:GLM-5.3']['active'], 1)

    def test_wrapper_dead_child_dead_created_match_released(self):
        tok = self.reserve('r', 'zcode', 'GLM-5.3')
        self._bind(tok, wrapper_pid=3000, child_pid=3001,
                   wrapper_created='c3000', child_created='c3001')
        res = dp.reconcile(self.store, prober=_fake_prober(
            {3000: ('dead', 'c3000'), 3001: ('dead', 'c3001')}), now=T0)
        self.assertIn(tok, res['released'])
        self.assertEqual(dp.status(self.store, now=T0)['pools']['zcode:GLM-5.3']['active'], 0)

    def test_real_dead_pid_created_none_released(self):
        # 真实 Windows 死 PID 的 created=None 属正常，不额外要求创建时刻。
        tok = self.reserve('rd', 'zcode', 'GLM-5.3')
        self._bind(tok, wrapper_pid=3100, child_pid=3101,
                   wrapper_created='c3100', child_created='c3101')
        res = dp.reconcile(self.store, prober=_fake_prober(
            {3100: ('dead', None), 3101: ('dead', None)}), now=T0)
        self.assertIn(tok, res['released'])

    def test_child_alive_pid_reuse_held(self):
        tok = self.reserve('s', 'zcode', 'GLM-5.3')
        self._bind(tok, wrapper_pid=4000, child_pid=4001,
                   wrapper_created='c4000', child_created='c4001')
        # child 现在活着但出生时刻与记录不同（PID 复用嫌疑）→ unknown 不抢占。
        res = dp.reconcile(self.store, prober=_fake_prober(
            {4000: ('dead', 'c4000'), 4001: ('alive', 'DIFFERENT')}), now=T0)
        self.assertTrue(any(h['token'] == tok for h in res['held']), res)
        self.assertNotIn(tok, res['released'])
        self.assertEqual(self._state(tok), 'unknown')

    def test_child_alive_created_unknown_held(self):
        tok = self.reserve('t', 'zcode', 'GLM-5.3')
        self._bind(tok, wrapper_pid=5000, child_pid=5001,
                   wrapper_created='c5000', child_created='c5001')
        res = dp.reconcile(self.store, prober=_fake_prober(
            {5000: ('dead', 'c5000'), 5001: ('alive', None)}), now=T0)
        self.assertTrue(any(h['token'] == tok for h in res['held']), res)
        self.assertEqual(self._state(tok), 'unknown')

    def test_wrapper_dead_child_never_bound_held(self):
        tok = self.reserve('u', 'qoder', 'Qwen3.8-Max')
        dp.mark_running(self.store, tok, wrapper_pid=6000, wrapper_created='c6000', now=T0)
        res = dp.reconcile(self.store, prober=_fake_prober({6000: ('dead', 'c6000')}), now=T0)
        self.assertTrue(any(h['token'] == tok for h in res['held']), res)
        self.assertEqual(self._state(tok), 'unknown')

    def test_never_consumed_reserved_left_reserved(self):
        # 从未消费的合法预留（reserved、无 wrapper、无 child）：reconcile 保持 reserved，
        # 绝不翻 unknown（否则会破坏 consume 的同事务 CAS 竞争并令合法 cancel 误报）。
        tok = self.reserve('v', 'qoder', 'Qwen3.8-Max')
        res = dp.reconcile(self.store, prober=_fake_prober({}), now=T0)
        self.assertNotIn(tok, res['released'])
        self.assertFalse(any(h['token'] == tok for h in res['held']), res)
        self.assertEqual(self._state(tok), 'reserved')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['qoder:Qwen3.8-Max']['active'], 1)


class NeverConsumedReservationTests(_PoolBase):
    """item-2 第二缺陷：合法未消费预留必须保持可合法取消，且与 consume 单赢家结算。"""

    def test_reserve_reconcile_cancel_releases(self):
        tok = self.reserve('nc', 'zcode', 'GLM-5.3')
        # reconcile 绝不把从未消费的 reserved 翻成 unknown。
        res = dp.reconcile(self.store, prober=_fake_prober({}), now=T0)
        self.assertNotIn(tok, res['released'])
        self.assertFalse(any(h['token'] == tok for h in res['held']), res)
        self.assertEqual(self._state(tok), 'reserved')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['zcode:GLM-5.3']['active'], 1)
        # 合法取消释放这个干净名额（不再误报 owner_unverified）。
        out = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertTrue(out['released'], out)
        self.assertEqual(self._state(tok), 'cancelled')
        self.assertEqual(dp.status(self.store, now=T0)['pools']['zcode:GLM-5.3']['active'], 0)

    def test_consume_wins_then_non_owner_cancel_refused(self):
        tok = self.reserve('sw', 'zcode', 'GLM-5.3', stage='s1', chat_id='c1')
        ws = str(self._ws_for('sw'))
        c = dp.consume_for_entry(self.store, task_id='sw', runtime='zcode', model='GLM-5.3',
                                 workspace=ws, prompt_sha256='p', stage='s1', chat_id='c1',
                                 claim_token=tok, wrapper_pid=4321, wrapper_created='w1',
                                 now=T0)
        self.assertTrue(c['allowed'], c)
        self.assertEqual(self._state(tok), 'running')
        # consume 已赢：非 owner 的 cancel 不得抢占释放（单赢家，绝不双结算）。
        out = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertFalse(out['released'], out)
        self.assertEqual(out.get('reason'), 'owner_unverified')
        self.assertEqual(self._state(tok), 'running')

    def test_cancel_wins_then_consume_refused(self):
        tok = self.reserve('cf', 'zcode', 'GLM-5.3', stage='s1', chat_id='c1')
        ws = str(self._ws_for('cf'))
        out = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertTrue(out['released'], out)
        # cancel 已赢：终态 claim 不能再被消费。
        c = dp.consume_for_entry(self.store, task_id='cf', runtime='zcode', model='GLM-5.3',
                                 workspace=ws, prompt_sha256='p', stage='s1', chat_id='c1',
                                 claim_token=tok, wrapper_pid=4321, wrapper_created='w1',
                                 now=T0)
        self.assertFalse(c['allowed'], c)
        self.assertEqual(c['reason'], 'claim_invalid')


class LunaTicketTests(_PoolBase):
    def test_ask_record_starts_300s_and_persists(self):
        self.fill_domestic()
        r = dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                          ask_message_id='m1', now=T0)
        self.assertTrue(r['recorded'], r)
        self.assertEqual(r['state'], 'pending')
        self.assertEqual(r['deadline_seconds'], dp.LUNA_ASK_TIMEOUT_SECONDS)
        deadline = dp._parse(r['deadline_utc'])
        asked = dp._parse(r['asked_at_utc'])
        self.assertEqual(int((deadline - asked).total_seconds()),
                         dp.LUNA_ASK_TIMEOUT_SECONDS)

    def test_ask_record_requires_full_domestic(self):
        # 六未满 → 优先国内，不该升级询问。
        r = dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                          ask_message_id='m1', now=T0)
        self.assertFalse(r['recorded'], r)
        self.assertTrue(any('not full' in x for x in r['reasons']), r['reasons'])

    def test_ask_record_requires_message_id(self):
        self.fill_domestic()
        r = dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                          ask_message_id=None, now=T0)
        self.assertFalse(r['recorded'], r)
        self.assertTrue(any('ask_message_id' in x for x in r['reasons']), r['reasons'])

    def test_ask_record_not_reset_by_restart(self):
        self.fill_domestic()
        first = dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                              ask_message_id='m1', now=T0)
        later = dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                              ask_message_id='m1', now=T0 + timedelta(seconds=500))
        self.assertFalse(later['recorded'])
        self.assertEqual(later['deadline_utc'], first['deadline_utc'])
        self.assertEqual(later['state'], 'pending')

    def test_reply_records_choice(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        r = dp.reply(self.store, task_id='L', choice='external_agent', note='user picked',
                     now=T0)
        self.assertTrue(r['recorded'])
        self.assertEqual(r['state'], 'replied')
        self.assertEqual(r['choice'], 'external_agent')

    def test_reply_invalid_choice_raises(self):
        with self.assertRaises(ValueError):
            dp.reply(self.store, task_id='L', choice='bogus')

    def test_reply_without_ticket(self):
        r = dp.reply(self.store, task_id='ghost', choice='luna', now=T0)
        self.assertFalse(r['recorded'])

    def test_claim_due_not_yet_due(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        r = dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=10),
                         _preclaim=False)
        self.assertFalse(r['claimed'])
        self.assertTrue(r.get('not_due'))

    def test_claim_due_after_external_reply_refused(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        dp.reply(self.store, task_id='L', choice='external_agent', now=T0)
        r = dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=400),
                         _preclaim=False)
        self.assertFalse(r['claimed'])
        self.assertEqual(r.get('state'), 'replied')
        self.assertEqual(r.get('reply_choice'), 'external_agent')

    def test_claim_due_domestic_slot_freed_reclaims(self):
        toks = self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        # 释放一个国内名额 → claim_due 竞争发现空位 → 同事务原子回收，不双派、不丢槽。
        dp.finish(self.store, toks[0], terminal='cancelled', now=T0)
        r = dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=400),
                         _preclaim=False)
        self.assertTrue(r['claimed'], r)
        self.assertEqual(r['mode'], 'domestic_reclaim')
        self.assertTrue(r['token'])
        self.assertIn(r['pool_key'], dp.MAIN_FORCE_KEYS)

    def test_claim_due_six_full_no_reply_claims_luna(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        r = dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=400),
                         _preclaim=False)
        self.assertTrue(r['claimed'], r)
        self.assertEqual(r['pool_key'], dp.LUNA_KEY)
        self.assertEqual(r['launch_state'], 'host_must_call_native')
        self.assertTrue(r['token'])

    def test_claim_due_without_ticket_refused(self):
        self.fill_domestic()
        r = dp.claim_due(self.store, task_id='ghost', now=T0, _preclaim=False)
        self.assertFalse(r['claimed'])

    def test_claim_due_non_idempotent(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        first = dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=400),
                             _preclaim=False)
        self.assertTrue(first['claimed'])
        second = dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=401),
                              _preclaim=False)
        self.assertFalse(second['claimed'])
        self.assertEqual(second.get('state'), 'claimed')
        self.assertTrue(second.get('already_claimed'))

    def test_luna_has_no_global_cap(self):
        self.fill_domestic()
        for i in range(3):
            dp.ask_record(self.store, task_id=f'L{i}', scope=self._scope(f'L{i}'),
                          ask_message_id=f'm{i}', now=T0)
            r = dp.claim_due(self.store, task_id=f'L{i}',
                             now=T0 + timedelta(seconds=400), _preclaim=False)
            self.assertTrue(r['claimed'], r)
        st = dp.status(self.store, now=T0)['pools'][dp.LUNA_KEY]
        self.assertEqual(st['active'], 3)
        self.assertIsNone(st['capacity'])

    def test_mark_launch_unknown_records(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=400),
                     _preclaim=False)
        r = dp.mark_launch_unknown(self.store, task_id='L', now=T0)
        self.assertTrue(r['recorded'])
        self.assertEqual(r['launch_state'], 'launch_unknown')
        tickets = dp.status(self.store, now=T0)['luna_tickets']
        self.assertEqual(tickets[0]['launch_state'], 'launch_unknown')

    def test_cancel_pending_only_cancels_pending(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        r = dp.cancel_pending(self.store, task_id='L', now=T0)
        self.assertTrue(r['cancelled'])
        # 再次取消（已非 pending）→ no-op。
        r2 = dp.cancel_pending(self.store, task_id='L', now=T0)
        self.assertFalse(r2['cancelled'])

    def test_cancel_pending_after_reply_is_noop(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        dp.reply(self.store, task_id='L', choice='luna', now=T0)
        r = dp.cancel_pending(self.store, task_id='L', now=T0)
        self.assertFalse(r['cancelled'])


class StatusTests(_PoolBase):
    def test_status_shape_and_domestic_total(self):
        self.reserve('z', 'zcode', 'GLM-5.3')
        self.reserve('q', 'qoder', 'Qwen3.8-Max')
        st = dp.status(self.store, now=T0)
        self.assertEqual(set(st['pools']), set(dp.CAPACITY))
        self.assertEqual(st['domestic']['capacity'], 10)
        self.assertEqual(st['domestic']['active'], 2)
        self.assertEqual(st['domestic']['free'], 8)
        self.assertFalse(st['domestic']['full'])
        self.assertEqual(st['rotation']['committed_zcode'], 1)
        self.assertEqual(st['rotation']['committed_qoder'], 1)

    def test_luna_not_counted_in_domestic(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='L', scope=self._scope('L'),
                      ask_message_id='m1', now=T0)
        dp.claim_due(self.store, task_id='L', now=T0 + timedelta(seconds=400),
                     _preclaim=False)
        st = dp.status(self.store, now=T0)
        # Luna 在途不占国内名额口径（国内仍为 10/10，Luna 单列且无上限）。
        self.assertEqual(st['domestic']['active'], 10)
        self.assertEqual(st['pools'][dp.LUNA_KEY]['active'], 1)


class CliSmokeTests(_PoolBase):
    def test_parser_builds_and_status_runs(self):
        ap = dp.build_parser()
        args = ap.parse_args(['--store', self.store, 'status'])
        rc = dp.main(['--store', self.store, 'status'])
        self.assertEqual(rc, 0)
        self.assertEqual(args.command, 'status')

    def test_cli_reserve_then_status(self):
        self.assertEqual(dp.main(['--store', self.store, 'reserve', '--task-id', 'cli',
                                  '--runtime', 'zcode', '--model', 'GLM-5.3',
                                  '--workspace', str(self._ws_for('cli')),
                                  '--prompt-sha256', 'deadbeef']), 0)
        st = dp.status(self.store)
        self.assertEqual(st['pools']['zcode:GLM-5.3']['active'], 1)


# ============================================================================
# BW-POOL-SPLIT-20261010-S5 需求 1 回归：明确限额落标/读取/解除 + consume 前检查
# ============================================================================
class MainForceLimitTests(_PoolBase):
    """record_main_force_limit 绑定真实 attempt + 错误证据；写读解再读全程往返；
    drift（task/pool 漂移、证据 sha 不一致、attempt 缺失、非限额池）一律拒；
    consume_for_entry 消费预留票据前先查限额并把本 reserved 释放为 start_failed。"""

    QMAX = 'qoder:Qwen3.8-Max'
    CNMAX = 'qodercn:Qwen3.8-Max'
    CUSTOMMAX = 'qodercn:Qwen-3.8-Max'  # 自定义 CN 主力：可限额、不套时段、复用同一标记机制

    def _evidence(self, name='stdout.json', payload='{"result_errors":["You\'ve reached '
                                                        'your credit usage limit."]}\n'):
        """写一个真实存在的证据文件并返回 (path_str, sha256_hex)。"""
        import hashlib as _h
        p = self.base / name
        p.write_text(payload, encoding='utf-8', newline='\n')
        return str(p), _h.sha256(p.read_bytes()).hexdigest()

    def test_record_read_release_read_roundtrip(self):
        """temp 库完整往返：record → main_force_limited/Limits → release → 再读恒 False。"""
        tok = self.reserve_exec('rt', 'qoder', 'Qwen3.8-Max', 'qoder', pk=self.QMAX)
        ev_path, ev_sha = self._evidence()
        rec = dp.record_main_force_limit(self.store, self.QMAX, task_id='rt',
                                         attempt_token=tok, evidence_path=ev_path,
                                         evidence_sha256=ev_sha, now=T0)
        self.assertTrue(rec['recorded'], rec)
        self.assertTrue(rec['limited'], rec)
        self.assertEqual(rec['pool_key'], self.QMAX)
        self.assertEqual(rec['task_id'], 'rt')
        self.assertEqual(rec['attempt_token'], tok)
        self.assertEqual(rec['evidence_sha256'], ev_sha)
        self.assertTrue(dp.main_force_limited(self.store, self.QMAX, now=T0))
        # CN Max 独立池不受国际 QMAX 标记影响。
        self.assertFalse(dp.main_force_limited(self.store, self.CNMAX, now=T0))
        snaps = dp.main_force_limits(self.store, now=T0)
        self.assertIn(self.QMAX, snaps)
        self.assertEqual(snaps[self.QMAX]['limited'], 1)
        rel = dp.release_main_force_limit(self.store, self.QMAX, note='user confirmed '
                                          'credit refill', now=T0 + timedelta(minutes=5))
        self.assertTrue(rel['released'], rel)
        self.assertFalse(dp.main_force_limited(self.store, self.QMAX, now=T0))
        # 保留历史行：main_force_limits 仍可见，limited=0 + released_at/note 落库。
        snaps2 = dp.main_force_limits(self.store, now=T0)
        self.assertEqual(snaps2[self.QMAX]['limited'], 0)
        self.assertEqual(snaps2[self.QMAX]['release_note'], 'user confirmed credit refill')
        self.assertIsNotNone(snaps2[self.QMAX]['released_at_utc'])

    def test_record_rejects_non_limitable_pools(self):
        """ZCode、任一 Flash、退休 DeepSeek、Luna 均不落标；limited 恒 False。"""
        for pk in ('zcode:GLM-5.3', 'qoder:Qwen3.8-Flash',
                   'qodercn:Qwen3.8-Flash', 'qodercn:DeepSeek-Flash',
                   dp.LUNA_KEY):
            with self.subTest(pk=pk):
                r = dp.record_main_force_limit(self.store, pk, task_id='t',
                                               attempt_token='x',
                                               evidence_path=str(self.base),
                                               evidence_sha256='0' * 64, now=T0)
                self.assertFalse(r['recorded'], r)
                self.assertFalse(r.get('drift'), r)
                self.assertIn('not a quota-limitable', r['reasons'][0])
                self.assertFalse(dp.main_force_limited(self.store, pk, now=T0))

    def test_record_rejects_missing_attempt(self):
        ev_path, ev_sha = self._evidence()
        r = dp.record_main_force_limit(self.store, self.QMAX, task_id='ghost',
                                       attempt_token='no-such-token',
                                       evidence_path=ev_path, evidence_sha256=ev_sha,
                                       now=T0)
        self.assertFalse(r['recorded'], r)
        self.assertTrue(r['drift'], r)
        self.assertIn('no attempt found', r['reasons'][0])
        self.assertFalse(dp.main_force_limited(self.store, self.QMAX, now=T0))

    def test_record_rejects_task_and_pool_drift(self):
        """attempt 已存在但 pool_key 或 task_id 与 marker 参数不一致 → drift 拒。"""
        tok = self.reserve_exec('real', 'qoder', 'Qwen3.8-Max', 'qoder', pk=self.QMAX)
        ev_path, ev_sha = self._evidence('ev1.json')
        # task_id 漂移
        r1 = dp.record_main_force_limit(self.store, self.QMAX, task_id='someone-else',
                                        attempt_token=tok, evidence_path=ev_path,
                                        evidence_sha256=ev_sha, now=T0)
        self.assertFalse(r1['recorded'], r1)
        self.assertTrue(r1['drift'], r1)
        self.assertIn('task_id drift', r1['reasons'][0])
        # pool_key 漂移（拿 QMax 的 token 想给 CNMax 落标）
        r2 = dp.record_main_force_limit(self.store, self.CNMAX, task_id='real',
                                        attempt_token=tok, evidence_path=ev_path,
                                        evidence_sha256=ev_sha, now=T0)
        self.assertFalse(r2['recorded'], r2)
        self.assertTrue(r2['drift'], r2)
        self.assertIn('pool_key drift', r2['reasons'][0])
        self.assertFalse(dp.main_force_limited(self.store, self.QMAX, now=T0))
        self.assertFalse(dp.main_force_limited(self.store, self.CNMAX, now=T0))

    def test_record_rejects_evidence_sha_mismatch(self):
        """证据文件存在但 sha256 与声明值不一致 → drift 拒。"""
        tok = self.reserve_exec('es', 'qoder', 'Qwen3.8-Max', 'qoder', pk=self.QMAX)
        ev_path, _ = self._evidence('ev2.json', payload='real bytes\n')
        r = dp.record_main_force_limit(self.store, self.QMAX, task_id='es',
                                       attempt_token=tok, evidence_path=ev_path,
                                       evidence_sha256='f' * 64, now=T0)
        self.assertFalse(r['recorded'], r)
        self.assertTrue(r['drift'], r)
        self.assertIn('evidence_sha256 does not match', r['reasons'][0])
        self.assertFalse(dp.main_force_limited(self.store, self.QMAX, now=T0))

    def test_record_rejects_missing_evidence_file(self):
        tok = self.reserve_exec('ef', 'qoder', 'Qwen3.8-Max', 'qoder', pk=self.QMAX)
        missing = str(self.base / 'no-such-file.json')
        r = dp.record_main_force_limit(self.store, self.QMAX, task_id='ef',
                                       attempt_token=tok, evidence_path=missing,
                                       evidence_sha256='0' * 64, now=T0)
        self.assertFalse(r['recorded'], r)
        self.assertTrue(r['drift'], r)
        self.assertIn('does not exist', r['reasons'][0])

    def test_record_requires_binding_fields(self):
        """缺 attempt_token/task_id 或 evidence_path/evidence_sha256 → drift 拒。"""
        tok = self.reserve_exec('bf', 'qoder', 'Qwen3.8-Max', 'qoder', pk=self.QMAX)
        ev_path, ev_sha = self._evidence('ev3.json')
        for kwargs in (
            dict(task_id='bf', attempt_token=tok),  # 缺 evidence 字段
            dict(evidence_path=ev_path, evidence_sha256=ev_sha),  # 缺 attempt 字段
            dict(task_id='bf', evidence_path=ev_path, evidence_sha256=ev_sha),  # 缺 token
        ):
            with self.subTest(kwargs=sorted(kwargs)):
                r = dp.record_main_force_limit(self.store, self.QMAX, now=T0, **kwargs)
                self.assertFalse(r['recorded'], r)
                self.assertTrue(r['drift'], r)

    def test_record_is_idempotent_and_clears_release_traces(self):
        """同一 pool 重复 record 只刷新证据/时间戳，仍 limited=1，清空旧的 released_at/note。"""
        tok = self.reserve_exec('idp', 'qoder', 'Qwen3.8-Max', 'qoder', pk=self.QMAX)
        ev1, s1 = self._evidence('a.json', payload='first\n')
        r1 = dp.record_main_force_limit(self.store, self.QMAX, task_id='idp',
                                        attempt_token=tok, evidence_path=ev1,
                                        evidence_sha256=s1, now=T0)
        self.assertTrue(r1['recorded'])
        dp.release_main_force_limit(self.store, self.QMAX, note='temp release',
                                    now=T0 + timedelta(seconds=10))
        # 再次落标：应覆盖 limited=1，清空 released_at/note，指向新证据。
        (self.base / 'a.json').write_text('second\n', encoding='utf-8', newline='\n')
        import hashlib as _h
        s2 = _h.sha256((self.base / 'a.json').read_bytes()).hexdigest()
        r2 = dp.record_main_force_limit(self.store, self.QMAX, task_id='idp',
                                        attempt_token=tok, evidence_path=ev1,
                                        evidence_sha256=s2,
                                        now=T0 + timedelta(seconds=20))
        self.assertTrue(r2['recorded'])
        snap = dp.main_force_limits(self.store, now=T0)[self.QMAX]
        self.assertEqual(snap['limited'], 1)
        self.assertEqual(snap['evidence_sha256'], s2)
        self.assertIsNone(snap['released_at_utc'])
        self.assertIsNone(snap['release_note'])

    def test_consume_for_entry_releases_reserved_on_limited(self):
        """consume 前发现本池已 limited → 在同一事务内把本次 reserved CAS 到
        start_failed；返回 allowed=False/sent=False/capacity_released=True；
        另一 attempt 的 reserved 完全不受影响（不越权释放别人的占位）。"""
        # 先用显式 executor 把 QMax 名额 reserved 出来（此时尚未落标 → claim OK）
        tok = self.reserve_exec('c1', 'qoder', 'Qwen3.8-Max', 'qoder', pk=self.QMAX)
        # 独立池 CN Max 的另一个 reserved：本用例结束后必须仍是 reserved（不被越权释放）。
        tok_cn = self.reserve_exec('c2', 'qodercn', 'Qwen3.8-Max', 'qodercn',
                                   pk=self.CNMAX)
        # 再对该 QMax 池落标（绑定当前 attempt 与真实证据文件）
        ev, sha = self._evidence('c1.json', payload='limited evidence\n')
        rec = dp.record_main_force_limit(self.store, self.QMAX, task_id='c1',
                                         attempt_token=tok, evidence_path=ev,
                                         evidence_sha256=sha, now=T0)
        self.assertTrue(rec['recorded'], rec)
        self.assertTrue(dp.main_force_limited(self.store, self.QMAX, now=T0))
        # consume 前先查限额命中 → 释放本次 reserved 到 start_failed，拒启动。
        out = dp.consume_for_entry(self.store, task_id='c1', runtime='qoder',
                                   model='Qwen3.8-Max',
                                   workspace=str(self._ws_for('c1')),
                                   prompt_sha256='p', claim_token=tok,
                                   now=T0 + timedelta(seconds=2))
        self.assertFalse(out['allowed'], out)
        self.assertFalse(out['sent'], out)
        self.assertFalse(out['claimed'], out)
        self.assertEqual(out['reason'], 'main_force_limited', out)
        self.assertEqual(out['pool_key'], self.QMAX, out)
        self.assertTrue(out['capacity_released'], out)
        # 本次 reserved 已释放到 start_failed（不泄漏）。
        self.assertEqual(self._state(tok), 'start_failed')
        # 别人的 CN Max reserved 完全不动。
        self.assertEqual(self._state(tok_cn), 'reserved')

    def test_consume_for_entry_running_token_not_falsely_released(self):
        """running 旧 token 遇限额：UPDATE WHERE state=reserved 命中 0 行，绝不谎称
        capacity_released。保留原 running 状态与在途占位，只拒绝重复启动。"""
        tok = self.reserve_exec('rn', 'qoder', 'Qwen3.8-Max', 'qoder', pk=self.QMAX)
        ws = str(self._ws_for('rn'))
        first = dp.consume_for_entry(self.store, task_id='rn', runtime='qoder',
                                     model='Qwen3.8-Max', workspace=ws,
                                     prompt_sha256='p', claim_token=tok,
                                     wrapper_pid=4321, wrapper_created='w1', now=T0)
        self.assertTrue(first['allowed'], first)
        self.assertEqual(self._state(tok), 'running')
        ev, sha = self._evidence('rn.json', payload='limited after running\n')
        self.assertTrue(dp.record_main_force_limit(
            self.store, self.QMAX, task_id='rn', attempt_token=tok,
            evidence_path=ev, evidence_sha256=sha,
            now=T0 + timedelta(seconds=1))['recorded'])
        out = dp.consume_for_entry(self.store, task_id='rn', runtime='qoder',
                                   model='Qwen3.8-Max', workspace=ws,
                                   prompt_sha256='p', claim_token=tok,
                                   wrapper_pid=4321, wrapper_created='w1',
                                   now=T0 + timedelta(seconds=2))
        self.assertFalse(out['allowed'], out)
        self.assertFalse(out['sent'], out)
        self.assertEqual(out['reason'], 'main_force_limited', out)
        self.assertEqual(out['pool_key'], self.QMAX, out)
        self.assertFalse(out['capacity_released'], out)
        self.assertEqual(self._state(tok), 'running')
        self.assertEqual(dp.status(self.store, now=T0)['pools'][self.QMAX]['active'], 1)

    def test_consume_for_entry_unknown_token_not_falsely_released(self):
        """ACTIVE_STATES 实际含 'unknown'（非 launch_unknown）：unknown 旧 token 遇限额同样
        不得假释放，保留原 unknown 状态与占位，拒绝重复启动。"""
        self.assertIn('unknown', dp.ACTIVE_STATES)
        tok = self.reserve_exec('uk', 'qoder', 'Qwen3.8-Max', 'qoder', pk=self.QMAX)
        ws = str(self._ws_for('uk'))
        dp.mark_running(self.store, tok, wrapper_pid=5000, wrapper_created='c5000', now=T0)
        dp.mark_unknown(self.store, tok, now=T0)
        self.assertEqual(self._state(tok), 'unknown')
        ev, sha = self._evidence('uk.json', payload='limited while unknown\n')
        self.assertTrue(dp.record_main_force_limit(
            self.store, self.QMAX, task_id='uk', attempt_token=tok,
            evidence_path=ev, evidence_sha256=sha,
            now=T0 + timedelta(seconds=1))['recorded'])
        out = dp.consume_for_entry(self.store, task_id='uk', runtime='qoder',
                                   model='Qwen3.8-Max', workspace=ws,
                                   prompt_sha256='p', claim_token=tok,
                                   wrapper_pid=5000, wrapper_created='c5000',
                                   now=T0 + timedelta(seconds=2))
        self.assertFalse(out['allowed'], out)
        self.assertFalse(out['sent'], out)
        self.assertEqual(out['reason'], 'main_force_limited', out)
        self.assertEqual(out['pool_key'], self.QMAX, out)
        self.assertFalse(out['capacity_released'], out)
        self.assertEqual(self._state(tok), 'unknown')
        self.assertEqual(dp.status(self.store, now=T0)['pools'][self.QMAX]['active'], 1)

    def test_consume_for_entry_allows_when_pool_released(self):
        """release_main_force_limit 后同一 reserved 可正常 consume 到 running。"""
        tok = self.reserve_exec('rl', 'qoder', 'Qwen3.8-Max', 'qoder', pk=self.QMAX)
        ev, sha = self._evidence('rl.json', payload='limited once\n')
        self.assertTrue(dp.record_main_force_limit(
            self.store, self.QMAX, task_id='rl', attempt_token=tok,
            evidence_path=ev, evidence_sha256=sha, now=T0)['recorded'])
        self.assertTrue(dp.release_main_force_limit(
            self.store, self.QMAX, note='user refill',
            now=T0 + timedelta(seconds=5))['released'])
        out = dp.consume_for_entry(self.store, task_id='rl', runtime='qoder',
                                   model='Qwen3.8-Max',
                                   workspace=str(self._ws_for('rl')),
                                   prompt_sha256='p', claim_token=tok,
                                   now=T0 + timedelta(seconds=6))
        self.assertTrue(out['allowed'], out)
        self.assertEqual(self._state(tok), 'running')

    def test_custom_max_limit_record_read_release_no_builtin_collateral(self):
        """自定义 CN 主力 qodercn:Qwen-3.8-Max 复用同一限额标记机制：record/read/release
        全程往返可用；给自定义落标绝不误伤两内置 Max（无连带 collateral），释放亦只解自定义。"""
        tok = self.reserve_exec('cx', 'qodercn', 'Qwen-3.8-Max', 'qodercn', pk=self.CUSTOMMAX)
        ev, sha = self._evidence('cx.json')
        rec = dp.record_main_force_limit(self.store, self.CUSTOMMAX, task_id='cx',
                                         attempt_token=tok, evidence_path=ev,
                                         evidence_sha256=sha, now=T0)
        self.assertTrue(rec['recorded'], rec)
        self.assertTrue(rec['limited'], rec)
        self.assertEqual(rec['pool_key'], self.CUSTOMMAX)
        self.assertTrue(dp.main_force_limited(self.store, self.CUSTOMMAX, now=T0))
        # 绝不连带：自定义被限额，两内置 Max 完全不受影响。
        self.assertFalse(dp.main_force_limited(self.store, self.QMAX, now=T0))
        self.assertFalse(dp.main_force_limited(self.store, self.CNMAX, now=T0))
        snaps = dp.main_force_limits(self.store, now=T0)
        self.assertEqual(snaps[self.CUSTOMMAX]['limited'], 1)
        rel = dp.release_main_force_limit(self.store, self.CUSTOMMAX,
                                          note='user confirmed refill', now=T0)
        self.assertTrue(rel['released'], rel)
        self.assertFalse(dp.main_force_limited(self.store, self.CUSTOMMAX, now=T0))

    def test_consume_for_entry_custom_max_limited_releases_to_start_failed(self):
        """自定义 CN 主力命中限额：consume_for_entry 前查限额，把本 reserved 释放为
        start_failed（真释放不泄漏），拒启动；同区内置 CN Max 的 reserved 完全不动。"""
        tok_cx = self.reserve_exec('cxl', 'qodercn', 'Qwen-3.8-Max', 'qodercn',
                                   pk=self.CUSTOMMAX)
        tok_cn = self.reserve_exec('cnkeep', 'qodercn', 'Qwen3.8-Max', 'qodercn',
                                   pk=self.CNMAX)
        ev, sha = self._evidence('cxl.json', payload='custom limit hit\n')
        self.assertTrue(dp.record_main_force_limit(
            self.store, self.CUSTOMMAX, task_id='cxl', attempt_token=tok_cx,
            evidence_path=ev, evidence_sha256=sha, now=T0)['recorded'])
        out = dp.consume_for_entry(self.store, task_id='cxl', runtime='qodercn',
                                   model='Qwen-3.8-Max',
                                   workspace=str(self._ws_for('cxl')),
                                   prompt_sha256='p', claim_token=tok_cx,
                                   now=T0 + timedelta(seconds=2))
        self.assertFalse(out['allowed'], out)
        self.assertFalse(out['sent'], out)
        self.assertEqual(out['reason'], 'main_force_limited', out)
        self.assertEqual(out['pool_key'], self.CUSTOMMAX, out)
        self.assertTrue(out['capacity_released'], out)
        self.assertEqual(self._state(tok_cx), 'start_failed')
        # 内置 CN Max 的 reserved 不受自定义限额影响。
        self.assertEqual(self._state(tok_cn), 'reserved')


class BeijingMainForceWindowTests(_PoolBase):
    """BW-MAX-WINDOW-20261010-S1：两个 Qoder 内置 Max 只在北京时间 22:00（含）至次日
    08:00（不含）作为主力。全部使用确定性时间夹具，绝不依赖宿主墙钟；跨午夜/08:00 边界、
    AUTO 白天跳过、显式 Max 白天如实拒绝、reserved 跨 08 点真实释放、running/unknown 不
    假释放、降级 claim_due 使用同一时段候选过滤都覆盖。"""

    QMAX = 'qoder:Qwen3.8-Max'
    CNMAX = 'qodercn:Qwen3.8-Max'

    # 确定性时间：一律 UTC datetime，按固定 +08:00 折算北京时间。
    IN_2200 = datetime(2026, 10, 8, 14, 0, 0, tzinfo=timezone.utc)   # 北京 22:00 含 → 可选
    IN_0759 = datetime(2026, 10, 8, 23, 59, 0, tzinfo=timezone.utc)  # 北京 07:59 → 可选
    OUT_0800 = datetime(2026, 10, 8, 0, 0, 0, tzinfo=timezone.utc)   # 北京 08:00 不含 → 不可选
    OUT_2159 = datetime(2026, 10, 8, 13, 59, 0, tzinfo=timezone.utc)  # 北京 21:59 → 不可选
    NOON = datetime(2026, 10, 8, 4, 0, 0, tzinfo=timezone.utc)       # 北京 12:00 → 不可选
    LATE = datetime(2026, 10, 8, 0, 30, 0, tzinfo=timezone.utc)      # 北京 08:30 → 不可选

    def test_window_boundary_inclusive_22_exclusive_08(self):
        self.assertTrue(dp._main_force_window_open(self.IN_2200))
        self.assertTrue(dp._main_force_window_open(self.IN_0759))
        self.assertFalse(dp._main_force_window_open(self.OUT_0800))
        self.assertFalse(dp._main_force_window_open(self.OUT_2159))
        self.assertFalse(dp._main_force_window_open(self.NOON))
        self.assertEqual(dp._beijing_hour(self.IN_2200), 22)
        self.assertEqual(dp._beijing_hour(self.OUT_0800), 8)

    def test_beijing_hour_independent_of_host_timezone(self):
        # 同一 UTC 瞬间，用非 UTC tzinfo 表达也必须得到相同北京小时（先归一到 UTC）。
        from datetime import timezone as _tz
        other = datetime(2026, 10, 8, 9, 0, 0, tzinfo=_tz(timedelta(hours=-5)))  # = 14:00 UTC
        self.assertEqual(dp._beijing_hour(other), 22)
        self.assertEqual(dp._beijing_hour(self.IN_2200), 22)
        # naive 按 UTC 理解。
        self.assertEqual(dp._beijing_hour(datetime(2026, 10, 8, 14, 0, 0)), 22)

    def test_only_builtin_qoder_max_are_window_gated(self):
        for pk in (self.QMAX, self.CNMAX):
            self.assertTrue(dp._max_window_blocks(pk, self.NOON), pk)
            self.assertFalse(dp._max_window_blocks(pk, self.IN_2200), pk)
        # ZCode 主力、CN 自定义 Max 主力、两地区 Flash 兜底、Luna、retired DeepSeek
        # 一律不受时段门约束（自定义 Qwen-3.8-Max 不套内置时段）。
        for pk in ('zcode:GLM-5.3', 'qodercn:Qwen-3.8-Max',
                   'qoder:Qwen3.8-Flash', 'qodercn:Qwen3.8-Flash',
                   'qodercn:DeepSeek-Flash', dp.LUNA_KEY):
            self.assertFalse(dp._max_window_blocks(pk, self.NOON), pk)

    def test_auto_daytime_skips_both_builtin_max(self):
        # 白天 AUTO 请求国际内置 Max：不得新落任一个 Qoder Max，改派到 ZCode 主力（有空位）。
        r = dp.select_and_claim(self.store, task_id='a1', runtime='qoder',
                                model='Qwen3.8-Max', workspace=str(self._ws_for('a1')),
                                prompt_sha256='p', now=self.NOON, _preclaim=False)
        self.assertTrue(r['routing_required'], r)
        self.assertFalse(r.get('sent', True), r)
        self.assertNotIn(r['selected']['pool_key'], (self.QMAX, self.CNMAX), r)

    def test_auto_daytime_falls_to_flash_when_zcode_full(self):
        # ZCode 两槽占满 + 白天（两内置 Max 时段外、不可选）+ 自定义 Max 也占满 → AUTO 请求
        # 内置 Max 应兜底到同级 Flash，绝不落任一个 Qoder 内置 Max。用显式 executor 逐一确定性
        # 填满 ZCode 与自定义 Max（AUTO 轮换会先派内置 Max，此处不受影响）。
        self.reserve_exec('z1', 'zcode', 'GLM-5.3', 'zcode', pk='zcode:GLM-5.3')
        self.reserve_exec('z2', 'zcode', 'GLM-5.3', 'zcode', pk='zcode:GLM-5.3')
        self.reserve_exec('cu1', 'qodercn', 'Qwen-3.8-Max', 'qodercn',
                          pk='qodercn:Qwen-3.8-Max')
        self.reserve_exec('cu2', 'qodercn', 'Qwen-3.8-Max', 'qodercn',
                          pk='qodercn:Qwen-3.8-Max')
        r = dp.select_and_claim(self.store, task_id='a2', runtime='qodercn',
                                model='Qwen3.8-Max', workspace=str(self._ws_for('a2')),
                                prompt_sha256='p', now=self.NOON, _preclaim=False)
        self.assertTrue(r['routing_required'], r)
        self.assertIn(r['selected']['pool_key'],
                      ('qoder:Qwen3.8-Flash', 'qodercn:Qwen3.8-Flash'), r)

    def test_auto_daytime_prefers_custom_max_over_built_in(self):
        # 白天两内置 Max 时段外，但 CN 自定义 Max 不受时段约束 → AUTO 请求内置 Max 时改派到
        # 自定义 Max（仍主力），而非直接兜底 Flash，也绝不落任一个 Qoder 内置 Max。
        r = dp.select_and_claim(self.store, task_id='cu', runtime='qoder',
                                model='Qwen3.8-Max', workspace=str(self._ws_for('cu')),
                                prompt_sha256='p', now=self.NOON, _preclaim=False)
        self.assertTrue(r['routing_required'], r)
        self.assertEqual(r['selected']['pool_key'], 'qodercn:Qwen-3.8-Max', r)

    def test_explicit_reserve_custom_max_daytime_allowed_not_gated(self):
        # 自定义 CN Max 不套内置时段：白天显式 executor='qodercn' 请求自定义 Max 正常 claim。
        r = dp.reserve(self.store, task_id='cdmax', runtime='qodercn',
                       model='Qwen-3.8-Max', workspace=str(self._ws_for('cdmax')),
                       prompt_sha256='p', now=self.NOON, executor='qodercn',
                       _preclaim=False)
        self.assertTrue(r['allowed'], r)
        self.assertEqual(r['pool_key'], 'qodercn:Qwen-3.8-Max', r)

    def test_explicit_reserve_builtin_max_daytime_honestly_refused(self):
        # 主脑显式 reserve 新任务到内置 Max、白天 → 如实拒绝，绝不静默换成 Flash。
        for ex, rt in (('qoder', 'qoder'), ('qodercn', 'qodercn')):
            r = dp.reserve(self.store, task_id=f'r-{ex}', runtime=rt,
                           model='Qwen3.8-Max', workspace=str(self._ws_for(f'r-{ex}')),
                           prompt_sha256='p', now=self.NOON, executor=ex,
                           _preclaim=False)
            self.assertFalse(r['allowed'], r)
            self.assertFalse(r.get('sent', True), r)
            self.assertEqual(r['reason'], 'main_force_window_closed', r)
            self.assertFalse(r['routing_required'], r)

    def test_explicit_executor_no_model_routes_among_region_candidates(self):
        # 明确 executor 但请求本地区非 Max（Flash）——不受时段约束，正常 claim 本地区候选。
        r = dp.reserve(self.store, task_id='fl', runtime='qoder', model='Qwen3.8-Flash',
                       workspace=str(self._ws_for('fl')), prompt_sha256='p',
                       now=self.NOON, executor='qoder', _preclaim=False)
        self.assertTrue(r['allowed'], r)
        self.assertEqual(r['pool_key'], 'qoder:Qwen3.8-Flash', r)

    def test_reserve_in_window_builtin_max_allowed(self):
        r = dp.reserve(self.store, task_id='ok', runtime='qoder', model='Qwen3.8-Max',
                       workspace=str(self._ws_for('ok')), prompt_sha256='p',
                       now=self.IN_2200, executor='qoder', _preclaim=False)
        self.assertTrue(r['allowed'], r)
        self.assertEqual(r['pool_key'], self.QMAX, r)

    def test_reserved_max_crossing_08_is_really_released(self):
        # 22:00 时段内预留 Max，跨过 08:00 后才启动 → 真实释放本 reserved 占位为 start_failed。
        r = dp.reserve(self.store, task_id='x08', runtime='qoder', model='Qwen3.8-Max',
                       workspace=str(self._ws_for('x08')), prompt_sha256='p',
                       now=self.IN_2200, executor='qoder', _preclaim=False)
        tok = self.assertClaimed(r, self.QMAX)
        self.assertEqual(dp.status(self.store, now=self.IN_2200)['pools'][self.QMAX]['active'], 1)
        out = dp.consume_for_entry(self.store, task_id='x08', runtime='qoder',
                                   model='Qwen3.8-Max', workspace=str(self._ws_for('x08')),
                                   prompt_sha256='p', claim_token=tok,
                                   wrapper_pid=4321, wrapper_created='w1', now=self.LATE)
        self.assertFalse(out['allowed'], out)
        self.assertFalse(out.get('sent', True), out)
        self.assertEqual(out['reason'], 'main_force_window_closed', out)
        self.assertTrue(out['capacity_released'], out)
        self.assertEqual(self._state(tok), 'start_failed')
        self.assertEqual(dp.status(self.store, now=self.LATE)['pools'][self.QMAX]['active'], 0)

    def test_running_max_crossing_08_not_falsely_released(self):
        # 时段内已 consume 到 running，跨 08 点后重复启动 → 拒绝但不假释放，保留 running。
        r = dp.reserve(self.store, task_id='rn', runtime='qoder', model='Qwen3.8-Max',
                       workspace=str(self._ws_for('rn')), prompt_sha256='p',
                       now=self.IN_2200, executor='qoder', _preclaim=False)
        tok = self.assertClaimed(r, self.QMAX)
        c1 = dp.consume_for_entry(self.store, task_id='rn', runtime='qoder',
                                  model='Qwen3.8-Max', workspace=str(self._ws_for('rn')),
                                  prompt_sha256='p', claim_token=tok,
                                  wrapper_pid=7000, wrapper_created='c7000',
                                  now=self.IN_2200 + timedelta(seconds=1))
        self.assertTrue(c1['allowed'], c1)
        self.assertEqual(self._state(tok), 'running')
        c2 = dp.consume_for_entry(self.store, task_id='rn', runtime='qoder',
                                  model='Qwen3.8-Max', workspace=str(self._ws_for('rn')),
                                  prompt_sha256='p', claim_token=tok,
                                  wrapper_pid=7000, wrapper_created='c7000', now=self.LATE)
        self.assertFalse(c2['allowed'], c2)
        self.assertFalse(c2['capacity_released'], c2)
        self.assertEqual(self._state(tok), 'running')
        self.assertEqual(dp.status(self.store, now=self.LATE)['pools'][self.QMAX]['active'], 1)

    def test_claim_due_degradation_uses_same_window_filter(self):
        # 白天降级 claim_due：原 scope 钉住国际 Max、该 Max 恰好有空位，也不回收 Max（同一
        # 时段候选过滤），保持 pending、如实 main_force_window_closed，绝不静默换模型或偷偷
        # 启动 Max。先填满八槽记录票据，再释放国际 Max 制造"有空位但在时段外"。
        toks = self.fill_domestic()
        ws = str(self._ws_for('cd'))
        scope = {'task_id': 'cd', 'stage': 's1', 'chat_id': 'c1', 'workspace': ws,
                 'prompt_sha256': 'p' * 64, 'executor': 'qoder', 'model': 'Qwen3.8-Max'}
        rec = dp.ask_record(self.store, task_id='cd', scope=scope, ask_message_id='m1',
                            now=self.IN_2200)
        self.assertTrue(rec['recorded'], rec)
        # 释放国际 Max（fill 的第三个 token），腾出一个"时段外仍不该回收"的空位。
        dp.finish(self.store, toks[2], terminal='cancelled', now=self.IN_2200)
        self.assertEqual(dp.status(self.store, now=self.NOON)['pools'][self.QMAX]['free'], 1)
        out = dp.claim_due(self.store, task_id='cd', scope=scope, now=self.NOON)
        self.assertFalse(out['claimed'], out)
        self.assertTrue(out.get('main_force_window_closed'), out)
        self.assertTrue(out.get('model_unavailable'), out)
        self.assertNotIn('token', out)
        # Max 空位保持不被回收、也没有被换成别的模型提交。
        self.assertEqual(dp.status(self.store, now=self.NOON)['pools'][self.QMAX]['active'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
