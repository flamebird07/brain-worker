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

T0 = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)


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

    def fill_domestic(self):
        """按 1:1 轮转把六个国内名额填满：zcode、Max 交替各 2，主力满后 Flash 2。

        返回 token 列表，顺序为 [zcode, Max, zcode, Max, Flash, Flash]。每个 task 独立
        workspace；请求组合严格匹配轮转选中组合，绝不放松 1:1 或共享目录。
        """
        seq = [('f0', 'zcode', 'GLM-5.3'), ('f1', 'qoder', 'Qwen3.8-Max'),
               ('f2', 'zcode', 'GLM-5.3'), ('f3', 'qoder', 'Qwen3.8-Max'),
               ('f4', 'qoder', 'Qwen3.8-Flash'), ('f5', 'qoder', 'Qwen3.8-Flash')]
        toks = [self.reserve(t, rt, md) for (t, rt, md) in seq]
        st = dp.status(self.store, now=T0)
        self.assertEqual(st['domestic']['active'], dp.DOMESTIC_TOTAL_CAPACITY)
        self.assertTrue(st['domestic']['full'])
        self.assertEqual(st['pools']['zcode:GLM-5.3']['active'], 2)
        self.assertEqual(st['pools']['qoder:Qwen3.8-Max']['active'], 2)
        self.assertEqual(st['pools']['qoder:Qwen3.8-Flash']['active'], 2)
        return toks


class CapacityAndKnownPoolTests(_PoolBase):
    def test_capacity_table_matches_allocation_decision(self):
        self.assertEqual(dp.capacity_for('zcode:GLM-5.3'), 2)
        self.assertEqual(dp.capacity_for('qoder:Qwen3.8-Max'), 2)
        self.assertEqual(dp.capacity_for('qoder:Qwen3.8-Flash'), 2)
        self.assertIsNone(dp.capacity_for('luna:native'))
        self.assertEqual(dp.capacity_for('qoder:Unknown-Model'), 0)
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 6)

    def test_is_known_pool(self):
        for pk in ('zcode:GLM-5.3', 'qoder:Qwen3.8-Max',
                   'qoder:Qwen3.8-Flash', 'luna:native'):
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
        self.reserve('m0', 'zcode', 'GLM-5.3')
        self.reserve('m1', 'qoder', 'Qwen3.8-Max')
        self.reserve('m2', 'zcode', 'GLM-5.3')
        self.reserve('m3', 'qoder', 'Qwen3.8-Max')
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
        self.reserve('m0', 'zcode', 'GLM-5.3')
        self.reserve('m1', 'qoder', 'Qwen3.8-Max')
        self.reserve('m2', 'zcode', 'GLM-5.3')
        self.reserve('m3', 'qoder', 'Qwen3.8-Max')
        r = self.select('fok', 'qoder', 'Qwen3.8-Flash')
        self.assertClaimed(r, 'qoder:Qwen3.8-Flash')

    def test_flash_capacity_full_after_two(self):
        self.reserve('m0', 'zcode', 'GLM-5.3')
        self.reserve('m1', 'qoder', 'Qwen3.8-Max')
        self.reserve('m2', 'zcode', 'GLM-5.3')
        self.reserve('m3', 'qoder', 'Qwen3.8-Max')
        self.reserve('f0', 'qoder', 'Qwen3.8-Flash')
        self.reserve('f1', 'qoder', 'Qwen3.8-Flash')
        r = self.select('f2', 'qoder', 'Qwen3.8-Flash')
        self.assertRejected(r, 'capacity_full')


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
        self.assertEqual(st['domestic']['capacity'], 6)
        self.assertEqual(st['domestic']['active'], 2)
        self.assertEqual(st['domestic']['free'], 4)
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
        # Luna 在途不占国内名额口径（国内仍为 6/6，Luna 单列且无上限）。
        self.assertEqual(st['domestic']['active'], 6)
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
