"""BW-AVAILABILITY-20261009-B2 离线确定性回归：ZCode 独立持久 availability 状态 +
资格过滤路由 + Luna 降级/回复状态机 + 真实入口接入（合成 stub，sent=false / 零模型调用）。

无网络、无真实模型、无真实 CodeBuddy/ZCode/Qoder CLI；全部使用显式临时 sqlite store
与临时路由配置，绝不读写真实 ~/.brain-worker 或 offline-state 池。ZCode 入口回放复用
mock subprocess.Popen（零 OS 子进程），Qoder 入口回放复用仓库固定合成 stub_qodercli.py。
"""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
TESTS = REPO / 'tests'
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import quota_control as qc  # noqa: E402
import dispatch_pool as dp  # noqa: E402
import zcode_direct as zd  # noqa: E402
import prompt_contract as pc  # noqa: E402

T0 = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
PROVIDER = 'account:bigmodel-individual-coding-plan'      # 受信任（含 bigmodel）
UNTRUSTED = 'account:some-other-vendor-plan'              # 非受信任渠道
ZCODE = 'zcode:GLM-5.3'
QMAX = 'qoder:Qwen3.8-Max'
QFLASH = 'qoder:Qwen3.8-Flash'
SECTIONS = pc.SECTION_HEADERS
CLOSING = pc.CLOSING_LINE


def _hard(code=1310, provider=PROVIDER, msg='已达到每周使用上限', until=None,
          trusted_label='rate_limit'):
    """构造一条受信任 429 硬上限分类结果（供 record 直接落库）。"""
    return {'is_trusted_429': True, 'hard_quota': True,
            'kind': 'hard_hold' if until is None else 'hard_reset_window',
            'provider_code': code, 'provider': provider,
            'blocked_until_utc': until,
            'reason': f'synthetic hard limit {code} ({trusted_label} label overridden)'}


def _temp_backoff(provider=PROVIDER):
    return {'is_trusted_429': True, 'hard_quota': False, 'kind': 'temporary_backoff',
            'provider_code': None, 'provider': provider, 'blocked_until_utc': None,
            'reason': 'synthetic ordinary temporary 429'}


class _TmpBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.dispatch = self.tmp / 'dispatch.sqlite3'
        self.quota = self.tmp / 'quota.sqlite3'
        self.routes = self.tmp / 'routes-absent.json'  # 不存在 → unknown-shared
        self.group = qc.resolve_group(qc.load_routes(self.routes), 'zcode',
                                      {'provider': PROVIDER})['quota_group']
        self.ws = self.tmp / 'ws'
        self.ws.mkdir()
        # 绝不使用真实/共享池：显式临时库，且断言不是默认路径。
        self.assertNotEqual(str(self.dispatch), str(dp.default_store_path()))

    def tearDown(self):
        self._tmp.cleanup()

    def _ws_for(self, name):
        d = self.tmp / f'ws-{name}'
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _scope(self, task_id, ws=None):
        return {'task_id': task_id, 'stage': 's1', 'chat_id': 'c1',
                'workspace': str(ws or self._ws_for(task_id)),
                'prompt_sha256': 'p' * 64}

    def _hold_zcode(self, code=1310, provider=PROVIDER, group=None, until=None):
        return qc.record_zcode_unavailability(
            self.quota, provider=provider, quota_group=group or self.group,
            classification=_hard(code, provider, until=until), now=T0)

    def _active(self, pk):
        return dp.status(self.dispatch, now=T0)['pools'][pk]['active']

    def _fill_six(self):
        toks = []
        combos = [('zcode', 'GLM-5.3'), ('zcode', 'GLM-5.3'),
                  ('qoder', 'Qwen3.8-Max'), ('qoder', 'Qwen3.8-Max'),
                  ('qoder', 'Qwen3.8-Flash'), ('qoder', 'Qwen3.8-Flash')]
        for i, (rt, md) in enumerate(combos):
            r = dp.reserve(self.dispatch, task_id=f'f{i}', runtime=rt, model=md,
                           workspace=str(self._ws_for(f'f{i}')), prompt_sha256='p',
                           stage='s1', now=T0, executor=('zcode' if rt == 'zcode' else 'qoder'))
            self.assertTrue(r['reserved'], r)
            toks.append(r['token'])
        return toks

    def _luna_active(self):
        return dp.status(self.dispatch, now=T0)['pools'][dp.LUNA_KEY]['active']

    def _free_slot(self, token):
        # finish 不能释放仍在 reserved 的占位（reserved 不是 running）；直接把该 token
        # 的 attempt 置为终态 finished，模拟一个真实结束、腾出物理空位，供回收测试使用。
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            conn.execute("UPDATE attempts SET state='finished', terminal='finished', "
                         'ended_at_utc=? WHERE token=?', (qc._iso(T0), token))


# ============================================================ A. availability 分类/状态
class AvailabilityClassifyTests(_TmpBase):
    def test_hard_limit_no_timezone_is_indefinite_hold(self):
        # 现场事故：HTTP 429 + 受信任渠道 1310 + "已达到…使用上限"，reset 文本无时区
        # → 不猜窗口、不复用旧 24h → 无限期 hard_hold（等待显式用户重置/新恢复证据）。
        cls = qc.classify_zcode_availability(
            [], [{'status': 429, 'provider': PROVIDER, 'provider_code': 1310,
                  'category': 'rate_limit',
                  'message': '已达到每周使用上限，将在 2026-10-15 03:00:00 重置'}],
            provider=PROVIDER, now=T0)
        self.assertTrue(cls['is_trusted_429'], cls)
        self.assertTrue(cls['hard_quota'], cls)
        self.assertEqual(cls['kind'], 'hard_hold')
        self.assertIsNone(cls['blocked_until_utc'])

    def test_1310_trusted_vs_untrusted_provider(self):
        trusted = qc.classify_zcode_availability(
            [], [{'status': 429, 'provider': PROVIDER, 'provider_code': 1310,
                  'message': 'rate limited'}], provider=PROVIDER, now=T0)
        self.assertTrue(trusted['hard_quota'], trusted)
        untrusted = qc.classify_zcode_availability(
            [], [{'status': 429, 'provider': UNTRUSTED, 'provider_code': 1310,
                  'message': 'rate limited'}], provider=UNTRUSTED, now=T0)
        # 同一数字来自非受信任渠道不泛化为硬额度 → 普通临时退避。
        self.assertFalse(untrusted['hard_quota'], untrusted)
        self.assertEqual(untrusted['kind'], 'temporary_backoff')

    def test_hard_message_overrides_rate_limit_label(self):
        cls = qc.classify_zcode_availability(
            [], [{'status': 429, 'category': 'rate_limit', 'provider': PROVIDER,
                  'message': '您的用量已达每月使用上限'}], provider=PROVIDER, now=T0)
        self.assertTrue(cls['hard_quota'])
        self.assertEqual(cls['kind'], 'hard_hold')

    def test_ordinary_429_is_bounded_backoff(self):
        cls = qc.classify_zcode_availability(
            [], [{'status': 429, 'provider': PROVIDER, 'message': 'too many requests'}],
            provider=PROVIDER, now=T0)
        self.assertFalse(cls['hard_quota'])
        self.assertEqual(cls['kind'], 'temporary_backoff')

    def test_separate_non_429_quota_entry_does_not_pollute(self):
        # 另一条不带 429 的 quota 条目不能把普通 429 升格为硬额度。
        cls = qc.classify_zcode_availability(
            [], [{'status': 429, 'provider': PROVIDER, 'message': 'too many requests'},
                 {'category': 'quota', 'provider': PROVIDER, 'message': '使用上限'}],
            provider=PROVIDER, now=T0)
        # 只认同一条目内绑定的 429 事实；非 429 的 quota 条目被跳过。
        self.assertFalse(cls['hard_quota'], cls)
        self.assertEqual(cls['kind'], 'temporary_backoff')

    def test_latest_valid_reset_wins_skipping_invalid(self):
        cls = qc.classify_zcode_availability(
            [], [{'status': 429, 'provider': PROVIDER, 'provider_code': 1308,
                  'message': '限额将在 2026-13-45 99:99:99 UTC+8 重置，'
                             '也会在 2026-10-08 20:00:00 UTC+8 重置'}],
            provider=PROVIDER, now=T0)
        self.assertTrue(cls['hard_quota'])
        self.assertEqual(cls['kind'], 'hard_reset_window')
        # 2026-10-08 20:00 UTC+8 == 12:00Z
        self.assertEqual(cls['blocked_until_utc'],
                         '2026-10-08T12:00:00+00:00')

    def test_record_never_touches_legacy_cooldowns(self):
        self._hold_zcode()
        status = qc.get_status(self.quota, now=T0)
        self.assertEqual(status['cooldowns'], {})
        self.assertIn('zcode_availability', status)
        rows = qc.zcode_blocking_rows(self.quota, now=T0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['state'], 'hard_hold')

    def test_manual_reset_is_probe_verifiable_not_healthy(self):
        self._hold_zcode()
        res = qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                                    routes_path=self.routes, evidence_ref='user-reset-1',
                                    now=T0)
        self.assertTrue(res['reset'])
        self.assertEqual(res['state'], 'recovery_unverified')
        st = qc.zcode_availability_status(self.quota, now=T0)
        key = qc.zcode_channel_key(PROVIDER, self.group)
        self.assertEqual(st[key]['state'], 'recovery_unverified')
        # 仍阻断完整派工（recovery_unverified），只放行单次 probe。
        self.assertTrue(qc.zcode_blocking_rows(self.quota, now=T0))

    def test_manual_reset_requires_evidence_ref(self):
        self._hold_zcode()
        res = qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                                    routes_path=self.routes, evidence_ref=None, now=T0)
        self.assertFalse(res['reset'])

    def test_probe_single_winner_then_failed_probe_blocks_reprobe(self):
        self._hold_zcode()
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                             routes_path=self.routes, evidence_ref='ev', now=T0)
        g1 = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                        routes_path=self.routes, purpose='probe', now=T0)
        self.assertTrue(g1['allowed'])
        self.assertTrue(g1['probe_granted'])
        # 第二个并发 probe 被拒（CAS 单赢家）。
        g2 = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                        routes_path=self.routes, purpose='probe',
                                        now=T0 + timedelta(seconds=1))
        self.assertFalse(g2['allowed'])
        # probe 真实失败结算 → 设再探测退避窗口，不能立即再 probe。
        qc.settle_zcode_attempt(self.quota, provider=PROVIDER, quota_group=self.group,
                                attempt_id=g1['attempt_id'], epoch=g1['availability_epoch'],
                                success=False, executed=True, now=T0)
        g3 = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                        routes_path=self.routes, purpose='probe',
                                        now=T0 + timedelta(seconds=2))
        self.assertFalse(g3['allowed'])

    def test_probe_success_clears_to_healthy(self):
        self._hold_zcode()
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                             routes_path=self.routes, evidence_ref='ev', now=T0)
        g = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                       routes_path=self.routes, purpose='probe', now=T0)
        res = qc.settle_zcode_attempt(self.quota, provider=PROVIDER, quota_group=self.group,
                                      attempt_id=g['attempt_id'], epoch=g['availability_epoch'],
                                      success=True, executed=True, now=T0)
        self.assertTrue(res['cleared'])
        self.assertEqual(res['state'], 'healthy')
        self.assertEqual(qc.zcode_blocking_rows(self.quota, now=T0), [])

    def test_late_success_cannot_clear_newer_failure_epoch(self):
        self._hold_zcode()
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                             routes_path=self.routes, evidence_ref='ev', now=T0)
        g = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                       routes_path=self.routes, purpose='probe', now=T0)
        old_epoch = g['availability_epoch']
        # 一个更新的失败推进 epoch（模拟另一次真实 429 落库）。
        self._hold_zcode(code=1308)
        res = qc.settle_zcode_attempt(self.quota, provider=PROVIDER, quota_group=self.group,
                                      attempt_id=g['attempt_id'], epoch=old_epoch,
                                      success=True, executed=True, now=T0)
        self.assertFalse(res['cleared'])
        # 更新失败仍在（未被迟到成功清除）。
        self.assertTrue(qc.zcode_blocking_rows(self.quota, now=T0))

    def test_codebuddy_future_cooldown_does_not_block_zcode_availability(self):
        # CodeBuddy 冷却行（旧 cooldowns 表）与 ZCode availability 完全独立。
        cb_classification = {'is_quota_429': True, 'kind': 'quota_reset',
                             'cooldown_until_utc': qc._iso(T0 + timedelta(hours=24)),
                             'reason': 'cb', 'provider_code': None}
        qc.record_quota_event(self.quota, 'cb-group', cb_classification,
                              source='cb', now=T0)
        self.assertEqual(qc.zcode_blocking_rows(self.quota, now=T0), [])
        # ZCode hold 不写旧 cooldowns 表。
        self._hold_zcode()
        self.assertEqual(qc.get_status(self.quota, now=T0)['cooldowns'].get(self.group),
                         None)

    def test_alias_rename_conservative_blocking(self):
        # routes 缺失 → group=unknown-shared；即便以另一 provider 别名查询，同 group 行仍阻断。
        self._hold_zcode()
        g = qc.zcode_availability_gate(self.quota, provider=PROVIDER + '-alias',
                                       routes_path=self.routes, purpose='dispatch', now=T0)
        self.assertFalse(g['allowed'])


# ============================================================ B. 资格过滤路由
class DispatchRoutingTests(_TmpBase):
    def _seed_rotation(self, zcode, qoder):
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            conn.execute("INSERT INTO rotation(pool_group, committed_zcode, committed_qoder, "
                         "updated_at_utc) VALUES('main',?,?,?) "
                         'ON CONFLICT(pool_group) DO UPDATE SET committed_zcode=excluded.'
                         'committed_zcode, committed_qoder=excluded.committed_qoder',
                         (zcode, qoder, qc._iso(T0)))

    def test_auto_skips_unavailable_zcode_routes_to_qoder_zero_z_starts(self):
        self._hold_zcode()
        out = dp.select_and_claim(self.dispatch, task_id='t1', runtime='zcode',
                                  model='GLM-5.3', workspace=str(self._ws_for('t1')),
                                  prompt_sha256='p', stage='s1', chat_id='c1',
                                  executor='auto', quota_store=self.quota, now=T0)
        self.assertFalse(out['allowed'])
        self.assertTrue(out['routing_required'])
        self.assertEqual(out['selected']['pool_key'], QMAX)
        self.assertEqual(self._active(ZCODE), 0)  # 零 ZCode 启动

    def test_explicit_qoder_not_one_on_one_rerouted_to_zcode(self):
        # 历史 committed_zcode 远高于 qoder，但显式 Qoder 入口有空位一律直接 claim Max。
        self._seed_rotation(5, 0)
        out = dp.consume_for_entry(self.dispatch, task_id='t2', runtime='qoder',
                                   model='Qwen3.8-Max', workspace=str(self._ws_for('t2')),
                                   prompt_sha256='p', stage='s1', chat_id='c1',
                                   executor='qoder', quota_store=self.quota, now=T0)
        self.assertTrue(out['allowed'], out)
        self.assertEqual(out['pool_key'], QMAX)
        self.assertEqual(self._active(ZCODE), 0)

    def test_auto_balances_eligible_only(self):
        self._hold_zcode()
        # 反复 auto 派工只会落到 Qwen3.8-Max（ZCode 不合格），绝不落 ZCode。
        for i in range(2):
            out = dp.select_and_claim(self.dispatch, task_id=f'a{i}', runtime='zcode',
                                      model='GLM-5.3', workspace=str(self._ws_for(f'a{i}')),
                                      prompt_sha256='p', stage='s1',
                                      executor='auto', quota_store=self.quota, now=T0)
            self.assertTrue(out['routing_required'])
            self.assertEqual(out['selected']['pool_key'], QMAX)
        self.assertEqual(self._active(ZCODE), 0)

    def test_zcode_executor_refused_when_unavailable(self):
        self._hold_zcode()
        out = dp.consume_for_entry(self.dispatch, task_id='z1', runtime='zcode',
                                   model='GLM-5.3', workspace=str(self._ws_for('z1')),
                                   prompt_sha256='p', stage='s1',
                                   executor='zcode', quota_store=self.quota, now=T0)
        self.assertFalse(out['allowed'])
        self.assertEqual(out['reason'], 'zcode_unavailable')
        self.assertEqual(self._active(ZCODE), 0)

    def test_executor_conflict(self):
        out = dp.consume_for_entry(self.dispatch, task_id='x1', runtime='zcode',
                                   model='GLM-5.3', workspace=str(self._ws_for('x1')),
                                   prompt_sha256='p', stage='s1',
                                   executor='qoder', quota_store=self.quota, now=T0)
        self.assertFalse(out['allowed'])
        self.assertEqual(out['reason'], 'executor_conflict')

    def test_consume_token_recheck_releases_placeholder_no_leak(self):
        # 先在没有 hold 时预留一个 ZCode token，再落 hold，消费时必须再核验并释放本占位。
        r = dp.reserve(self.dispatch, task_id='tk', runtime='zcode', model='GLM-5.3',
                       workspace=str(self._ws_for('tk')), prompt_sha256='p', stage='s1',
                       chat_id='c1', now=T0)
        self.assertTrue(r['reserved'], r)
        self.assertEqual(self._active(ZCODE), 1)
        self._hold_zcode()
        out = dp.consume_for_entry(self.dispatch, task_id='tk', runtime='zcode',
                                   model='GLM-5.3', workspace=str(self._ws_for('tk')),
                                   prompt_sha256='p', stage='s1', chat_id='c1',
                                   claim_token=r['token'], executor='zcode',
                                   quota_store=self.quota, now=T0)
        self.assertFalse(out['allowed'])
        self.assertEqual(out['reason'], 'zcode_unavailable')
        self.assertTrue(out['capacity_released'])
        # 占位已释放（start_failed），不再占容量：无泄漏。
        self.assertEqual(self._active(ZCODE), 0)

    def test_reclaim_skips_blocked_zcode(self):
        # 降级票据（capacity）在国内未满时创建；claim_due 原子回收只选可用主力（跳过 ZCode）。
        # B4 缺陷 7：capacity 降级必须客观核验受信任候选确实排空——先把 QMax+Flash 占满
        # （ZCode 已 blocked 不在合格候选），再释放一个 QMax 名额，回收才会原子落到 QMax。
        self._hold_zcode()
        toks = []
        for i in range(2):
            r = dp.reserve(self.dispatch, task_id=f'q{i}', runtime='qoder',
                           model='Qwen3.8-Max', workspace=str(self._ws_for(f'q{i}')),
                           prompt_sha256='p', stage='s1', now=T0, executor='qoder')
            self.assertTrue(r['reserved'], r)
            toks.append(r['token'])
        for i in range(2):
            r = dp.reserve(self.dispatch, task_id=f'fl{i}', runtime='qoder',
                           model='Qwen3.8-Flash', workspace=str(self._ws_for(f'fl{i}')),
                           prompt_sha256='p', stage='s1', now=T0, executor='qoder')
            self.assertTrue(r['reserved'], r)
            toks.append(r['token'])
        sc = self._scope('rc')
        ask = dp.ask_record(self.dispatch, task_id='rc', scope=sc, ask_message_id='m1',
                            now=T0, degradation_reason='capacity',
                            degradation_detail='no available main force for this task',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        # 释放一个 QMax 名额 → 合格候选出现空位 → 原子回收落到 QMax（绝不落 ZCode）。
        self._free_slot(toks[0])
        out = dp.claim_due(self.dispatch, task_id='rc', now=T0, quota_store=self.quota)
        self.assertTrue(out['claimed'], out)
        self.assertEqual(out['mode'], 'domestic_reclaim')
        self.assertEqual(out['pool_key'], QMAX)


# ============================================================ C. Luna 状态机
class LunaStateMachineTests(_TmpBase):
    def test_domestic_reply_never_luna_at_any_elapsed(self):
        for elapsed in (10, 299, 300, 301):
            with self.subTest(elapsed=elapsed):
                self.setUp()
                try:
                    self._fill_six()
                    sc = self._scope('dl')
                    ask = dp.ask_record(self.dispatch, task_id='dl', scope=sc,
                                        ask_message_id='m1', now=T0)
                    self.assertTrue(ask['recorded'], ask)
                    rep = dp.reply(self.dispatch, task_id='dl', choice='domestic', now=T0)
                    self.assertTrue(rep['recorded'])
                    out = dp.claim_due(self.dispatch, task_id='dl',
                                       now=T0 + timedelta(seconds=elapsed))
                    self.assertFalse(out['claimed'], out)
                    self.assertEqual(out.get('reply_choice'), 'domestic')
                    self.assertEqual(self._luna_active(), 0)
                finally:
                    self.tearDown()

    def test_external_reply_stops_auto_luna(self):
        self._fill_six()
        dp.ask_record(self.dispatch, task_id='ex', scope=self._scope('ex'),
                      ask_message_id='m1', now=T0)
        dp.reply(self.dispatch, task_id='ex', choice='external_agent', now=T0)
        out = dp.claim_due(self.dispatch, task_id='ex', now=T0 + timedelta(seconds=400))
        self.assertFalse(out['claimed'])
        self.assertEqual(out.get('reply_choice'), 'external_agent')
        self.assertEqual(self._luna_active(), 0)

    def test_cancel_stops(self):
        self._fill_six()
        dp.ask_record(self.dispatch, task_id='cz', scope=self._scope('cz'),
                      ask_message_id='m1', now=T0)
        dp.reply(self.dispatch, task_id='cz', choice='cancel', now=T0)
        out = dp.claim_due(self.dispatch, task_id='cz', now=T0 + timedelta(seconds=400))
        self.assertFalse(out['claimed'])
        self.assertEqual(self._luna_active(), 0)

    def test_six_full_no_reply_claims_luna_after_deadline(self):
        self._fill_six()
        dp.ask_record(self.dispatch, task_id='lu', scope=self._scope('lu'),
                      ask_message_id='m1', now=T0)
        early = dp.claim_due(self.dispatch, task_id='lu', now=T0 + timedelta(seconds=299))
        self.assertFalse(early['claimed'])
        self.assertTrue(early.get('not_due'))
        due = dp.claim_due(self.dispatch, task_id='lu', now=T0 + timedelta(seconds=301))
        self.assertTrue(due['claimed'], due)
        self.assertEqual(due['pool_key'], dp.LUNA_KEY)
        self.assertEqual(self._luna_active(), 1)

    def test_repeated_ask_never_resets_reply_or_timer(self):
        self._fill_six()
        first = dp.ask_record(self.dispatch, task_id='ra', scope=self._scope('ra'),
                              ask_message_id='m1', now=T0)
        dp.reply(self.dispatch, task_id='ra', choice='luna', now=T0)
        again = dp.ask_record(self.dispatch, task_id='ra', scope=self._scope('ra'),
                              ask_message_id='m2', now=T0 + timedelta(seconds=500))
        self.assertFalse(again['recorded'])
        self.assertEqual(again.get('reply_choice'), 'luna')
        # pending 计时不因重复询问重置。
        st = dp.status(self.dispatch, now=T0)['luna_tickets']
        self.assertEqual(st[0]['deadline_utc'], first['deadline_utc'])

    def test_corrupt_scope_fails_closed(self):
        self._fill_six()
        dp.ask_record(self.dispatch, task_id='cs', scope=self._scope('cs'),
                      ask_message_id='m1', now=T0)
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            conn.execute("UPDATE luna_tickets SET scope='{not json' WHERE task_id='cs'")
        out = dp.claim_due(self.dispatch, task_id='cs', now=T0 + timedelta(seconds=400))
        self.assertFalse(out['claimed'])
        self.assertTrue(out.get('scope_corrupt'))
        self.assertEqual(self._luna_active(), 0)

    def test_degradation_ask_allowed_below_six_full_with_evidence(self):
        self._hold_zcode()
        ask = dp.ask_record(self.dispatch, task_id='dg', scope=self._scope('dg'),
                            ask_message_id='m1', now=T0, degradation_reason='quota',
                            degradation_detail='ZCode hard hold; Qwen3.8-Max pool full',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)

    def test_degradation_quota_without_evidence_refused(self):
        ask = dp.ask_record(self.dispatch, task_id='dg2', scope=self._scope('dg2'),
                            ask_message_id='m1', now=T0, degradation_reason='quota',
                            degradation_detail='hunch', quota_store=self.quota)
        self.assertFalse(ask['recorded'])
        self.assertTrue(any('objective evidence' in r for r in ask['reasons']), ask['reasons'])

    def test_degradation_timeout_never_substitutes_luna_authorization(self):
        self._hold_zcode()
        dp.ask_record(self.dispatch, task_id='dg3', scope=self._scope('dg3'),
                      ask_message_id='m1', now=T0, degradation_reason='quota',
                      degradation_detail='ZCode hard hold', quota_store=self.quota)
        # 填满六席以进入 Luna 分支（否则国内回收）。
        self._fill_six()
        out = dp.claim_due(self.dispatch, task_id='dg3',
                           now=T0 + timedelta(seconds=400), quota_store=self.quota)
        self.assertFalse(out['claimed'], out)
        self.assertEqual(out.get('degradation_reason'), 'quota')
        self.assertEqual(self._luna_active(), 0)

    def test_degradation_explicit_luna_reply_claims(self):
        self._hold_zcode()
        dp.ask_record(self.dispatch, task_id='dg4', scope=self._scope('dg4'),
                      ask_message_id='m1', now=T0, degradation_reason='quota',
                      degradation_detail='ZCode hard hold', quota_store=self.quota)
        self._fill_six()
        dp.reply(self.dispatch, task_id='dg4', choice='luna', now=T0)
        out = dp.claim_due(self.dispatch, task_id='dg4', now=T0, quota_store=self.quota)
        self.assertTrue(out['claimed'], out)
        self.assertEqual(out['pool_key'], dp.LUNA_KEY)

    def test_claim_due_non_idempotent_and_launch_unknown(self):
        self._fill_six()
        dp.ask_record(self.dispatch, task_id='ni', scope=self._scope('ni'),
                      ask_message_id='m1', now=T0)
        first = dp.claim_due(self.dispatch, task_id='ni', now=T0 + timedelta(seconds=400))
        self.assertTrue(first['claimed'])
        second = dp.claim_due(self.dispatch, task_id='ni', now=T0 + timedelta(seconds=401))
        self.assertFalse(second['claimed'])
        self.assertTrue(second.get('already_claimed'))
        mu = dp.mark_launch_unknown(self.dispatch, task_id='ni', now=T0)
        self.assertTrue(mu['recorded'])
        third = dp.claim_due(self.dispatch, task_id='ni', now=T0 + timedelta(seconds=402))
        self.assertFalse(third['claimed'])

    def test_repeated_ask_after_cancelled_not_reset(self):
        self._fill_six()
        dp.ask_record(self.dispatch, task_id='cn', scope=self._scope('cn'),
                      ask_message_id='m1', now=T0)
        dp.cancel_pending(self.dispatch, task_id='cn', now=T0)
        again = dp.ask_record(self.dispatch, task_id='cn', scope=self._scope('cn'),
                              ask_message_id='m2', now=T0)
        self.assertFalse(again['recorded'])
        self.assertEqual(again.get('state'), 'cancelled')


# ============================================================ D. 真实入口接入（合成 stub）
class ZCodeEntryAvailabilityTests(_TmpBase):
    """复用 mock Popen 的完整 ZCode 入口离线回放（零 OS 子进程/网络/模型）。"""

    FRAME_1310 = ('ProviderBusinessError: [1310][已达到每月使用上限。'
                  '您的限额将在 2026-10-15 03:25:56 重置。]\n'
                  "code: 'PROVIDER_BUSINESS_ERROR',\ncode: '1310',\n"
                  'responseStatus: 429,\n').encode('utf-8')

    def setUp(self):
        super().setUp()
        self.prompt = self.tmp / 'prompt.md'
        self.prompt.write_text('请原样汇报。', encoding='utf-8')
        self.z_files = {}
        for key in ('node', 'bootstrap', 'tsx_loader',
                    'builtin_provider_config', 'personal_provider_config'):
            p = self.tmp / (key.replace('_', '-') + '.bin')
            p.write_text('runtime-stub\n', encoding='utf-8')
            self.z_files[key] = str(p)
        self.z_config = self.tmp / 'z.json'
        self.z_config.write_text(json.dumps(self.z_files), encoding='utf-8')

    def _argv(self, out, provider=PROVIDER):
        out = Path(out)
        return ['zcode_direct.py', '--workspace', str(self.ws),
                '--prompt-file', str(self.prompt), '--output-dir', str(out),
                '--stage', 'BW-AVAILABILITY-20261009-B2', '--provider', provider,
                '--config', str(self.z_config), '--quota-store', str(self.quota),
                '--quota-routes', str(self.routes),
                '--dispatch-store', str(self.tmp / f'dispatch-{out.name}.sqlite3')]

    class _Child:
        def __init__(self, rc):
            self.returncode = rc
            self.pid = 424242

        def wait(self):
            return self.returncode

        def communicate(self, *a, **k):
            return (b'', b'')

        def kill(self):
            pass

    def _run(self, out, stdout_bytes, stderr_bytes, rc, counter, provider=PROVIDER):
        def _popen(argv, *a, **k):
            counter['n'] += 1
            k['stdout'].write(stdout_bytes)
            k['stderr'].write(stderr_bytes)
            return self._Child(rc)

        buf = io.StringIO()
        with mock.patch.object(sys, 'argv', self._argv(out, provider)), \
                mock.patch.dict(os.environ, {'PYTHONIOENCODING': 'utf-8'}), \
                mock.patch.object(zd.subprocess, 'Popen', side_effect=_popen):
            with contextlib.redirect_stdout(buf):
                code = zd.main()
        sp = Path(out) / 'summary.json'
        summary = json.loads(sp.read_text(encoding='utf-8')) if sp.is_file() else None
        return code, summary, buf.getvalue()

    def _env_429(self, code=1310, msg='已达到每月使用上限'):
        return {'carrier': 'zcode-sdk', 'ok': False, 'preflight_ok': True,
                'errors': [f'ProviderBusinessError: [{code}][{msg}]'],
                'errors_info': None, 'event_count': 0}

    def test_hard_frame_records_hold_and_blocks_second_dispatch(self):
        counter = {'n': 0}
        stdout = json.dumps(self._env_429(), ensure_ascii=False).encode('utf-8')
        out1 = self.tmp / 'o1'
        code, summary, _ = self._run(out1, stdout, self.FRAME_1310, 5, counter)
        self.assertEqual(code, 3, summary)
        self.assertEqual(counter['n'], 1)
        rec = summary['zcode_availability_recorded']
        self.assertTrue(rec['recorded'], rec)
        self.assertEqual(rec['state'], 'hard_hold')
        # 旧 cooldowns 表不落（ZCode 自动冷却禁用）。
        self.assertEqual(qc.get_status(self.quota, now=T0)['cooldowns'], {})
        # 第二次独立新 output 被 availability 门在 Popen 前挡住：sent=false、退出 2、零 Popen。
        out2 = self.tmp / 'o2'
        code2, summary2, stdout2 = self._run(out2, stdout, self.FRAME_1310, 5, counter)
        self.assertEqual(counter['n'], 1)
        self.assertEqual(code2, 2)
        self.assertIn('zcode_availability_rejected', stdout2)
        self.assertIsNone(summary2)

    def test_cancel_exit_code_not_recorded(self):
        counter = {'n': 0}
        stdout = json.dumps(self._env_429(), ensure_ascii=False).encode('utf-8')
        out = self.tmp / 'cancel'
        code, summary, _ = self._run(out, stdout, self.FRAME_1310, 4294967295, counter)
        self.assertNotIn('zcode_availability_recorded', summary)
        self.assertEqual(qc.zcode_blocking_rows(self.quota, now=T0), [])

    def test_body_429_without_structured_frame_not_recorded(self):
        counter = {'n': 0}
        # 正文里出现 429 字样，但没有结构化失败信封/错误帧 → 不触发 availability。
        env = {'carrier': 'zcode-sdk', 'ok': True, 'preflight_ok': True,
               'errors': [], 'errors_info': None, 'event_count': 0}
        stdout = json.dumps(env, ensure_ascii=False).encode('utf-8')
        out = self.tmp / 'body429'
        code, summary, _ = self._run(out, stdout, b'log mentions 429 in passing\n',
                                     0, counter)
        self.assertEqual(qc.zcode_blocking_rows(self.quota, now=T0), [])

    def test_manual_reset_probe_success_restores_dispatch(self):
        counter = {'n': 0}
        stdout = json.dumps(self._env_429(), ensure_ascii=False).encode('utf-8')
        self._run(self.tmp / 'r1', stdout, self.FRAME_1310, 5, counter)
        self.assertEqual(counter['n'], 1)
        # 人工重置（绑定证据）→ recovery_unverified。
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                              routes_path=self.routes, evidence_ref='user-reset',
                              now=T0)
        # 完整派工仍被拒（必须先过单次 probe）。
        out2 = self.tmp / 'r2'
        code2, _, stdout2 = self._run(out2, stdout, self.FRAME_1310, 5, counter)
        self.assertEqual(code2, 2)
        self.assertEqual(counter['n'], 1)
        self.assertIn('zcode_availability_rejected', stdout2)
        # 直接把 availability 结算为 healthy（模拟一次成功 probe 核验）。
        st = qc.zcode_availability_status(self.quota, now=T0)
        key = qc.zcode_channel_key(PROVIDER, self.group)
        epoch = st[key]['epoch']
        g = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                       routes_path=self.routes, purpose='probe', now=T0)
        self.assertTrue(g['allowed'] and g['probe_granted'])
        qc.settle_zcode_attempt(self.quota, provider=PROVIDER, quota_group=self.group,
                                attempt_id=g['attempt_id'], epoch=g['availability_epoch'],
                                success=True, executed=True, now=T0)
        self.assertEqual(qc.zcode_blocking_rows(self.quota, now=T0), [])
        # healthy 后正常派工放行（成功信封）。
        ok_env = {'type': 'result', 'subtype': 'success', 'is_error': False,
                  'stop_reason': 'end_turn', 'session_id': 's', 'result': '报告',
                  'errors': [], 'errors_info': None, 'event_count': 0}
        ok_stdout = json.dumps(ok_env, ensure_ascii=False).encode('utf-8')
        out3 = self.tmp / 'r3'
        code3, summary3, _ = self._run(out3, ok_stdout, b'', 0, counter)
        self.assertEqual(counter['n'], 2)  # 第三次确实派工（probe 后放行）
        self.assertEqual(qc.zcode_blocking_rows(self.quota, now=T0), [])

    # ---- BW-AVAILABILITY-20261009-B5 缺陷 2：真实 probe 在 Popen 处抛错（child=None） ----
    def _run_probe_raise(self, out):
        def _popen(argv, *a, **k):
            raise OSError('simulated spawn failure before start')
        buf = io.StringIO()
        argv = self._argv(out) + ['--quota-recovery-probe']
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.dict(os.environ, {'PYTHONIOENCODING': 'utf-8'}), \
                mock.patch.object(zd.subprocess, 'Popen', side_effect=_popen):
            with contextlib.redirect_stdout(buf):
                code = zd.main()
        return code, buf.getvalue()

    def test_probe_popen_failure_returns_eligibility_not_left_active(self):
        # 拿到合法 recovery probe 资格却在 Popen 前抛错、从未真正启动：必须归还本次绑定的
        # row/epoch/attempt 为“未执行”资格（executed=False），绝不留 probe_active、绝不假称
        # 恢复、也不消耗该代际唯一一次资格（无子进程不算真实失败）。用完整 mock 入口证明，
        # 而非只直接调用 settle。
        self._hold_zcode()
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                              routes_path=self.routes, evidence_ref='ev', now=T0)
        out = self.tmp / 'probecrash'
        code, stdout = self._run_probe_raise(out)
        self.assertEqual(code, 3, stdout)
        self.assertIn('availability_settled_unexecuted', stdout)
        st = qc.zcode_availability_status(self.quota, now=T0)
        key = qc.zcode_channel_key(PROVIDER, self.group)
        self.assertFalse(st[key]['probe_active'])
        self.assertEqual(st[key]['state'], 'recovery_unverified')
        self.assertFalse(st[key]['recovery_eligibility_consumed'])

    # ---- BW-AVAILABILITY-20261009-B6 缺陷 4 收紧：真正携带合法接续契约 + 输入 hash ----
    def test_hard_429_terminal_releases_capacity_after_freeze(self):
        # 覆盖真实“接续冻结成功”终态：不可用 429 事实先发布；随后必须携带合法
        # continuation contract，走真实 cc.evaluate/build_handoff 保存 continuation.json
        # （含输入 sha256），冻结 hook 期间同 workspace 的第二个 writer 仍被单写入守卫拒绝
        # （原 owner 的 attempt 尚未 finish）；只有冻结落盘后才正常释放容量名额并放行后续
        # writer。capacity_released 与 placeholders_retained 绝不同时为真。（收紧前只断言
        # 无契约终态，未触及真正冻结成功路径。）
        counter = {'n': 0}
        (self.ws / 'a.txt').write_text('seed\n', encoding='utf-8')
        contract = self.tmp / 'cc.json'
        contract.write_text(json.dumps({'files': ['a.txt']}), encoding='utf-8')
        stdout = json.dumps(self._env_429(), ensure_ascii=False).encode('utf-8')
        out = self.tmp / 'g4rel'
        dispatch_store = str(self.tmp / 'dispatch-g4rel.sqlite3')
        observed = {}
        real_evaluate = zd.cc.evaluate

        def spy_evaluate(baseline, work, copies_dir=None):
            # 冻结进行中：原 zcode attempt 仍 active → 同 workspace 的第二 writer 必须被拒。
            second = dp.select_and_claim(dispatch_store, task_id='second-writer',
                                         runtime='qoder', model='Qwen3.8-Max',
                                         workspace=str(self.ws), prompt_sha256='x' * 64,
                                         stage='s2', executor='qoder', now=T0,
                                         _preclaim=False)
            observed['during_freeze'] = second
            return real_evaluate(baseline, work, copies_dir=copies_dir)

        argv = self._argv(out) + ['--continuation-contract', str(contract)]
        argv = argv + ['--task-id', 'g4-owner']

        def _popen(a2, *aa, **kk):
            counter['n'] += 1
            kk['stdout'].write(stdout)
            kk['stderr'].write(self.FRAME_1310)
            return self._Child(5)

        buf = io.StringIO()
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.dict(os.environ, {'PYTHONIOENCODING': 'utf-8'}), \
                mock.patch.object(zd.subprocess, 'Popen', side_effect=_popen), \
                mock.patch.object(zd.cc, 'evaluate', side_effect=spy_evaluate):
            with contextlib.redirect_stdout(buf):
                code = zd.main()
        sp = out / 'summary.json'
        summary = json.loads(sp.read_text(encoding='utf-8'))
        # 不可用事实先发布。
        self.assertEqual(summary['zcode_availability_recorded']['state'], 'hard_hold')
        # 真正走了合法接续冻结：continuation.json 落盘且带输入 hash。
        self.assertIn('continuation', summary, summary)
        self.assertTrue(Path(summary['continuation']['file']).is_file(), summary)
        self.assertTrue(len(summary['continuation']['sha256']) == 64
                        and all(c in '0123456789abcdef'
                                for c in summary['continuation']['sha256']),
                        summary['continuation'])
        self.assertNotEqual(summary.get('continuation_status'), 'unverified_no_contract')
        # 冻结 hook 内第二个 writer 被同 workspace 单写入守卫拒绝（原 attempt 尚未释放）。
        self.assertEqual(observed['during_freeze']['allowed'], False, observed)
        self.assertEqual(observed['during_freeze']['reason'], 'workspace_in_flight',
                         observed)
        # 冻结真实落盘后才释放容量；released 与 retained 绝不同时为真。
        self.assertTrue(summary.get('capacity_released'), summary)
        self.assertNotIn('placeholders_retained', summary)
        self.assertFalse(summary.get('capacity_released')
                         and summary.get('placeholders_retained'))
        # 释放后同 workspace 第二 writer 现可放行（守卫确已打开，名额最终归还）。
        after = dp.select_and_claim(dispatch_store, task_id='second-writer',
                                    runtime='qoder', model='Qwen3.8-Max',
                                    workspace=str(self.ws), prompt_sha256='x' * 64,
                                    stage='s2', executor='qoder', now=T0,
                                    _preclaim=False)
        self.assertTrue(after['allowed'], after)

    def test_freeze_failure_retains_placeholder_never_falsely_released(self):
        # 接续冻结抛错 → 保留在途占位、真实标注 placeholders_retained，绝不释放、
        # 绝不伪称 capacity_released=true 又 placeholders_retained=true。
        counter = {'n': 0}
        ws_file = self.ws / 'a.txt'
        ws_file.write_text('seed\n', encoding='utf-8')
        contract = self.tmp / 'cc.json'
        contract.write_text(json.dumps({'files': ['a.txt']}), encoding='utf-8')
        ok_env = {'type': 'result', 'subtype': 'success', 'is_error': False,
                  'stop_reason': 'end_turn', 'session_id': 's', 'result': '报告',
                  'errors': [], 'errors_info': None, 'event_count': 0}
        ok_stdout = json.dumps(ok_env, ensure_ascii=False).encode('utf-8')
        out = self.tmp / 'g4freeze'
        argv = self._argv(out) + ['--continuation-contract', str(contract)]

        def _popen(a2, *aa, **kk):
            counter['n'] += 1
            kk['stdout'].write(ok_stdout)
            kk['stderr'].write(b'')
            return self._Child(0)
        buf = io.StringIO()
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.dict(os.environ, {'PYTHONIOENCODING': 'utf-8'}), \
                mock.patch.object(zd.subprocess, 'Popen', side_effect=_popen), \
                mock.patch.object(zd.cc, 'evaluate', side_effect=RuntimeError('freeze boom')):
            with contextlib.redirect_stdout(buf):
                code = zd.main()
        sp = out / 'summary.json'
        summary = json.loads(sp.read_text(encoding='utf-8'))
        self.assertEqual(code, 3, summary)
        self.assertTrue(summary.get('placeholders_retained'), summary)
        self.assertFalse(summary.get('capacity_released', False), summary)
        self.assertFalse(summary.get('capacity_released')
                         and summary.get('placeholders_retained'))


class QoderEntryRoutingTests(_TmpBase):
    """真实 Qoder 入口 + 仓库固定合成 stub_qodercli.py（子进程，零模型/网络）。"""

    def _report(self, stage, work):
        lines = ['WORKER_REPORT_START',
                 f'阶段编号与执行方式：{stage}；direct。',
                 f'实际项目绝对路径：{work}',
                 '汇报时间与执行环境：离线测试', '']
        for h in SECTIONS:
            lines += [h, '- 无', '']
        lines += [CLOSING, 'WORKER_REPORT_END']
        return '\n'.join(lines) + '\n'

    def test_qoder_claims_max_despite_zcode_hold_and_history(self):
        stage = 'BW-AVAILABILITY-20261009-B2'
        work = self._ws_for('q')
        self._hold_zcode()
        # 历史 committed_zcode 高，验证显式 Qoder 不被 1:1 改派到 ZCode。
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            conn.execute("INSERT INTO rotation(pool_group, committed_zcode, committed_qoder, "
                         "updated_at_utc) VALUES('main',9,0,?)", (qc._iso(T0),))
        cfg = self.tmp / 'q-entry.json'
        cfg.write_text(json.dumps({'node': sys.executable,
                                   'qodercli': str(TESTS / 'stub_qodercli.py')}),
                       encoding='utf-8')
        prompt = self.tmp / 'q-prompt.md'
        prompt.write_text('离线链路测试。', encoding='utf-8')
        report = self.tmp / 'q-report.txt'
        report.write_text(self._report(stage, str(work)), encoding='utf-8')
        out = self.tmp / 'q-out'
        env = {**os.environ.copy(), 'STUB_MODE': 'ok',
               'STUB_REPORT_FILE': str(report), 'PYTHONUTF8': '1',
               'BRAIN_WORKER_DISPATCH_STORE': str(self.dispatch)}
        cmd = [sys.executable, str(SCRIPTS / 'qoder_direct.py'),
               '--workspace', str(work), '--prompt-file', str(prompt),
               '--output-dir', str(out), '--config', str(cfg), '--stage', stage,
               '--model', 'Qwen3.8-Max', '--dispatch-store', str(self.dispatch),
               '--quota-store', str(self.quota), '--quota-routes', str(self.routes)]
        proc = subprocess.run(cmd, capture_output=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode('utf-8', 'replace')
                          + proc.stderr.decode('utf-8', 'replace'))
        summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
        self.assertTrue(summary['protocol_success'], summary)
        # 落库 attempt 必须是 qoder:Qwen3.8-Max，绝无 ZCode attempt。
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            rows = [dict(r) for r in conn.execute(
                'SELECT runtime, model, pool_key FROM attempts').fetchall()]
        self.assertTrue(rows)
        self.assertTrue(all(r['runtime'] == 'qoder' for r in rows), rows)
        self.assertFalse(any(r['runtime'] == 'zcode' for r in rows), rows)


# ============================================================ E. B4 缺陷回归（先失败后修）
class B4GenerationCasTests(_TmpBase):
    def test_stateless_late_success_cannot_clear_newer_failure(self):
        # 缺陷 1：两个无行(normal)任务，gate 都回代际 0；A 落 429(行 epoch=1)，
        # B 的迟到成功(携带 epoch 0)绝不能把 A 的新失败清成 healthy。
        gA = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                        routes_path=self.routes, purpose='dispatch', now=T0)
        self.assertTrue(gA['allowed'])
        self.assertEqual(gA['availability_epoch'], 0)  # 无行 → 明确初始代际 0，非 None
        self._hold_zcode()  # A 的真实 429 落库 → 行 epoch=1
        gB = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                        routes_path=self.routes, purpose='dispatch', now=T0)
        # B 在 hold 之下不会被授予 dispatch（拒绝票不含 attempt_id）；用一个绑定旧代际 0 的
        # 合成 attempt 强制结算成功，也不得把 A 更新后的失败行清成 healthy。
        res = qc.settle_zcode_attempt(self.quota, provider=PROVIDER,
                                      quota_group=self.group, attempt_id='b-late',
                                      epoch=0, success=True, executed=True,
                                      channel_key=qc.zcode_channel_key(PROVIDER, self.group),
                                      now=T0)
        self.assertFalse(res['cleared'], res)
        self.assertTrue(qc.zcode_blocking_rows(self.quota, now=T0))

    def test_gate_returns_granted_row_key_for_settle(self):
        # 缺陷 1：gate 回传授予行的真实 channel_key；settle 用该 key 才能命中并清 probe。
        self._hold_zcode()
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                              routes_path=self.routes, evidence_ref='ev', now=T0)
        g = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                       routes_path=self.routes, purpose='probe', now=T0)
        self.assertTrue(g['probe_granted'])
        granted_key = g['granted_channel_key']
        st = qc.zcode_availability_status(self.quota, now=T0)
        self.assertTrue(st[granted_key]['probe_active'])
        qc.settle_zcode_attempt(self.quota, provider=PROVIDER, quota_group=self.group,
                                attempt_id=g['attempt_id'], epoch=g['availability_epoch'],
                                success=False, executed=False, channel_key=granted_key,
                                now=T0)
        st2 = qc.zcode_availability_status(self.quota, now=T0)
        self.assertFalse(st2[granted_key]['probe_active'])


class B4ClassificationTests(_TmpBase):
    def test_rate_limit_exceeded_text_is_not_hard(self):
        # 缺陷 2：普通限流语义 "Rate limit exceeded" 不得升格为硬 hold。
        cls = qc.classify_zcode_availability(
            [], [{'status': 429, 'provider': PROVIDER, 'message': 'Rate limit exceeded'}],
            provider=PROVIDER, now=T0)
        self.assertFalse(cls['hard_quota'], cls)
        self.assertEqual(cls['kind'], 'temporary_backoff')

    def test_per_minute_request_cap_is_not_hard(self):
        # 缺陷 2：每分钟请求上限（频率）而非周期/套餐额度耗尽 → 普通有界退避。
        cls = qc.classify_zcode_availability(
            [], [{'status': 429, 'provider': PROVIDER, 'message': '已达到每分钟请求上限'}],
            provider=PROVIDER, now=T0)
        self.assertFalse(cls['hard_quota'], cls)
        self.assertEqual(cls['kind'], 'temporary_backoff')

    def test_self_reported_bigmodel_code_on_untrusted_request_not_hard(self):
        # 缺陷 2：核验请求来自其它渠道，条目自报 BigModel 1310 也不泛化为硬额度。
        cls = qc.classify_zcode_availability(
            [], [{'status': 429, 'provider': PROVIDER, 'provider_code': 1310,
                  'message': 'other vendor'}], provider=UNTRUSTED, now=T0)
        self.assertFalse(cls['hard_quota'], cls)
        self.assertEqual(cls['kind'], 'temporary_backoff')

    def test_weekly_plan_cap_is_hard(self):
        # 缺陷 2 正向：周期/套餐额度耗尽文本仍是硬 hold（不被窄化误伤）。
        cls = qc.classify_zcode_availability(
            [], [{'status': 429, 'provider': PROVIDER,
                  'message': '已达到每周使用上限'}], provider=PROVIDER, now=T0)
        self.assertTrue(cls['hard_quota'], cls)
        self.assertEqual(cls['kind'], 'hard_hold')


class B4ReadOnlyStateTests(_TmpBase):
    def test_blocking_rows_on_missing_db_writes_zero_files(self):
        # 缺陷 5：缺库查询只读、绝不建库/建表，返回空，且不落任何文件。
        missing = self.tmp / 'no-such-dir' / 'no-such.sqlite3'
        before = sorted(str(p) for p in self.tmp.rglob('*'))
        rows = qc.zcode_blocking_rows(missing, now=T0)
        self.assertEqual(rows, [])
        after = sorted(str(p) for p in self.tmp.rglob('*'))
        self.assertEqual(before, after)  # 零新文件
        self.assertFalse(missing.exists())
        self.assertFalse((self.tmp / 'no-such-dir').exists())

    def test_status_on_missing_db_is_empty_no_creation(self):
        missing = self.tmp / 'ghost' / 'ghost.sqlite3'
        self.assertEqual(qc.zcode_availability_status(missing, now=T0), {})
        self.assertFalse(missing.exists())


class B4RecoveryEligibilityTests(_TmpBase):
    def _arm_probe(self):
        self._hold_zcode()
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                              routes_path=self.routes, evidence_ref='ev-1', now=T0)
        return qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                          routes_path=self.routes, purpose='probe', now=T0)

    def test_failed_probe_consumes_single_eligibility(self):
        # 缺陷 3：唯一一次 recovery 资格被失败 probe 消耗；退避窗口到期后也不再凭空定时
        # 再 probe——必须由新的 manual reset 重新取得。
        g = self._arm_probe()
        qc.settle_zcode_attempt(self.quota, provider=PROVIDER, quota_group=self.group,
                                attempt_id=g['attempt_id'], epoch=g['availability_epoch'],
                                success=False, executed=True, now=T0)
        # 越过退避窗口后仍拒绝（资格已消耗，非"窗口未到"）。
        later = self._arm_probe_later()
        self.assertFalse(later['allowed'], later)
        self.assertTrue(any('consumed' in r for r in later['reasons']), later['reasons'])

    def _arm_probe_later(self):
        return qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                          routes_path=self.routes, purpose='probe',
                                          now=T0 + timedelta(seconds=7200))

    def test_new_evidence_rearms_and_old_replay_is_idempotent(self):
        # 缺陷 3：同事件重复 reset 幂等；旧证据重放不能给新失败重造资格。
        self._hold_zcode()
        r1 = qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                                   routes_path=self.routes, evidence_ref='ev-a', now=T0)
        self.assertTrue(r1['reset'])
        epoch_after = r1['epoch']
        r2 = qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                                   routes_path=self.routes, evidence_ref='ev-a',
                                   epoch=epoch_after, now=T0)
        self.assertTrue(r2['reset'] and r2['idempotent'], r2)
        # 一次更新失败推进代际后，同一旧证据重放被拒（不给新失败重造资格）。
        self._hold_zcode(code=1308)
        r3 = qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                                   routes_path=self.routes, evidence_ref='ev-a', now=T0)
        self.assertFalse(r3['reset'], r3)
        self.assertTrue(any('stale reset replay' in x for x in r3['reasons']), r3['reasons'])


class B4ProbeCapacityGateTests(_TmpBase):
    def test_bound_probe_ticket_passes_capacity_gate(self):
        # 缺陷 4：持真实绑定 probe 票据时容量门放行这一次有界 probe 落到 ZCode 池；
        # 无票据的普通派工仍按 zcode_unavailable 拒绝。绝不信任自报 probe 布尔。
        self._hold_zcode()
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                              routes_path=self.routes, evidence_ref='ev', now=T0)
        g = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                       routes_path=self.routes, purpose='probe', now=T0)
        self.assertTrue(g['probe_granted'])
        ticket = {'provider': PROVIDER, 'channel_key': g['granted_channel_key'],
                  'attempt_id': g['attempt_id'], 'epoch': g['availability_epoch']}
        # 无票据 → zcode_unavailable
        denied = dp.select_and_claim(self.dispatch, task_id='p0', runtime='zcode',
                                     model='GLM-5.3', workspace=str(self._ws_for('p0')),
                                     prompt_sha256='p', stage='s1', executor='zcode',
                                     quota_store=self.quota, now=T0)
        self.assertFalse(denied['allowed'])
        self.assertEqual(denied['reason'], 'zcode_unavailable')
        # 有有效票据 → 放行 claim 到 ZCode 池
        ok = dp.select_and_claim(self.dispatch, task_id='p1', runtime='zcode',
                                 model='GLM-5.3', workspace=str(self._ws_for('p1')),
                                 prompt_sha256='p', stage='s1', executor='zcode',
                                 quota_store=self.quota, probe_ticket=ticket, now=T0)
        self.assertTrue(ok['allowed'], ok)
        self.assertEqual(ok['pool_key'], ZCODE)
        self.assertEqual(self._active(ZCODE), 1)

    def test_forged_probe_bool_is_still_denied(self):
        # 缺陷 4：伪造的 probe_ticket（attempt 不匹配真实在飞 probe）仍被拒。
        self._hold_zcode()
        bad = {'provider': PROVIDER, 'channel_key': qc.zcode_channel_key(PROVIDER, self.group),
               'attempt_id': 'not-a-real-attempt', 'epoch': 0}
        out = dp.select_and_claim(self.dispatch, task_id='p2', runtime='zcode',
                                  model='GLM-5.3', workspace=str(self._ws_for('p2')),
                                  prompt_sha256='p', stage='s1', executor='zcode',
                                  quota_store=self.quota, probe_ticket=bad, now=T0)
        self.assertFalse(out['allowed'])
        self.assertEqual(out['reason'], 'zcode_unavailable')


class B4LunaReplyAndConstraintTests(_TmpBase):
    def test_settled_ticket_rejects_late_reply(self):
        # 缺陷 7：settled 票据绝不被迟到/重复 reply 重新打开。
        self._fill_six()
        dp.ask_record(self.dispatch, task_id='st', scope=self._scope('st'),
                      ask_message_id='m1', now=T0)
        first = dp.claim_due(self.dispatch, task_id='st', now=T0 + timedelta(seconds=400))
        self.assertTrue(first['claimed'])
        # 模拟原生结算（settle-native 走 mark 之外的路径这里直接改库到 settled）。
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            conn.execute("UPDATE luna_tickets SET state='settled' WHERE task_id='st'")
        rep = dp.reply(self.dispatch, task_id='st', choice='luna', now=T0)
        self.assertFalse(rep['recorded'], rep)
        self.assertEqual(rep.get('state'), 'settled')

    def test_reclaim_honors_scope_qoder_executor(self):
        # 缺陷 7 + B4 combo：scope 声明 qoder/Max 时，即便 ZCode 有空位也绝不改派 ZCode。
        sc = self._scope('qe')
        sc = {**sc, 'executor': 'qoder', 'runtime': 'qoder', 'model': 'Qwen3.8-Max'}
        # 占满 QMax 两席 + Flash 两席以正当化 capacity 降级。
        toks = []
        for i in range(2):
            r = dp.reserve(self.dispatch, task_id=f'a{i}', runtime='qoder',
                           model='Qwen3.8-Max', workspace=str(self._ws_for(f'a{i}')),
                           prompt_sha256='p', stage='s1', now=T0, executor='qoder')
            toks.append(r['token'])
        for i in range(2):
            r = dp.reserve(self.dispatch, task_id=f'b{i}', runtime='qoder',
                           model='Qwen3.8-Flash', workspace=str(self._ws_for(f'b{i}')),
                           prompt_sha256='p', stage='s1', now=T0, executor='qoder')
            toks.append(r['token'])
        ask = dp.ask_record(self.dispatch, task_id='qe', scope=sc, ask_message_id='m1',
                            now=T0, degradation_reason='capacity',
                            degradation_detail='authorized qoder candidates exhausted')
        self.assertTrue(ask['recorded'], ask)
        # 释放一个 Flash → 只有满足 scope(qoder) 的候选可回收；绝不因 ZCode 空位改派 ZCode。
        self._free_slot(toks[0])
        out = dp.claim_due(self.dispatch, task_id='qe', now=T0, quota_store=self.quota)
        self.assertTrue(out['claimed'], out)
        self.assertEqual(out['pool_key'], QMAX)
        self.assertEqual(out['scope'].get('executor'), 'qoder')

    def test_zcode_bound_scope_waits_not_luna_when_zcode_blocked(self):
        # 缺陷 7：scope 明确绑 ZCode，但 ZCode 当前 blocked → 国内回收保持 pending，不 Luna。
        self._hold_zcode()
        sc = self._scope('zb')
        sc = {**sc, 'executor': 'zcode', 'runtime': 'zcode', 'model': 'GLM-5.3'}
        ask = dp.ask_record(self.dispatch, task_id='zb', scope=sc, ask_message_id='m1',
                            now=T0, degradation_reason='quota',
                            degradation_detail='ZCode hard hold', quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        out = dp.claim_due(self.dispatch, task_id='zb', now=T0, quota_store=self.quota)
        self.assertFalse(out['claimed'], out)
        self.assertTrue(any('pins this task to ZCode' in r for r in out['reasons']),
                        out['reasons'])
        self.assertEqual(dp.status(self.dispatch, now=T0)['pools'][dp.LUNA_KEY]['active'], 0)


class B4ComboExplicitTests(_TmpBase):
    def test_explicit_qoder_flash_combo_claimed_despite_max_free(self):
        # B4 combo：明确授权 qoder:Qwen3.8-Flash 组合，即便 Max 有空位也一律 claim Flash，
        # 绝不因"别的候选有空位"拒绝明确已授权组合。
        out = dp.consume_for_entry(self.dispatch, task_id='cf', runtime='qoder',
                                   model='Qwen3.8-Flash', workspace=str(self._ws_for('cf')),
                                   prompt_sha256='p', stage='s1', chat_id='c1',
                                   executor='qoder', quota_store=self.quota, now=T0)
        self.assertTrue(out['allowed'], out)
        self.assertEqual(out['pool_key'], QFLASH)
        self.assertEqual(self._active(QMAX), 0)

    def test_explicit_qoder_max_with_new_seed_still_succeeds(self):
        # 变更的显式-Q 种子：Z healthy 且有空位、committed 只反映 Z=0/Q=5，显式 Q 仍必成功。
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            conn.execute("INSERT INTO rotation(pool_group, committed_zcode, committed_qoder, "
                         "updated_at_utc) VALUES('main',0,5,?)", (qc._iso(T0),))
        out = dp.consume_for_entry(self.dispatch, task_id='nq', runtime='qoder',
                                   model='Qwen3.8-Max', workspace=str(self._ws_for('nq')),
                                   prompt_sha256='p', stage='s1', chat_id='c1',
                                   executor='qoder', quota_store=self.quota, now=T0)
        self.assertTrue(out['allowed'], out)
        self.assertEqual(out['pool_key'], QMAX)

    def test_auto_with_zcode_healthy_prefers_main_rotation(self):
        # 独立的 AUTO 用例：ZCode healthy 且有空位，auto 仍按 1:1 committed 轮换。
        # 种子 committed_zcode(3)>qoder(0) → 即便 Z 空也轮换到 QMax（不因 Z 空就必选 Z），
        # 选中组合与请求入口(runtime=zcode)不同 → routing_required、改派 QMax、零 ZCode 启动。
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            conn.execute("INSERT INTO rotation(pool_group, committed_zcode, committed_qoder, "
                         "updated_at_utc) VALUES('main',3,0,?)", (qc._iso(T0),))
        out = dp.select_and_claim(self.dispatch, task_id='au', runtime='zcode',
                                  model='GLM-5.3', workspace=str(self._ws_for('au')),
                                  prompt_sha256='p', stage='s1',
                                  executor='auto', quota_store=self.quota, now=T0)
        self.assertFalse(out['allowed'], out)
        self.assertTrue(out['routing_required'], out)
        self.assertEqual(out['selected']['pool_key'], QMAX)  # committed_qoder(0)<zcode(3)
        self.assertEqual(self._active(ZCODE), 0)             # 零 ZCode 启动


# ==================================================== B5 集中回归（缺口 1/3/5/6）
class B5GapRecoveryTests(_TmpBase):
    """缺口 1：manual-reset 幂等 + recovery 资格真实失败即原子消耗。"""

    def test_repeat_reset_with_pre_reset_epoch_is_idempotent(self):
        # 缺陷 1A：重复 reset 携"重置前的旧代际"epoch 也必须稳定幂等，绝不因 epoch 参数
        # 不匹配被判 stale。B4 顺序先比 epoch 后判重放 → 这里会被误判 stale（本用例失败）。
        self._hold_zcode()
        r1 = qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                                   routes_path=self.routes, evidence_ref='E', now=T0)
        self.assertTrue(r1['reset'])
        pre_epoch = r1['epoch'] - 1  # 首次 reset 之前的代际
        r2 = qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                                   routes_path=self.routes, evidence_ref='E',
                                   epoch=pre_epoch, now=T0)
        self.assertTrue(r2.get('reset') and r2.get('idempotent'), r2)
        self.assertNotIn('stale', json.dumps(r2))

    def test_real_429_failing_probe_consumes_and_old_reset_cannot_rearm(self):
        # 缺陷 1B：失败 probe 落真实 429 时原子消耗该代际唯一一次核验资格；≤ 该失败观测的
        # 同一（已过期）可信 reset 重放绝不能再取得资格；只有新的显式 reset 才重新授一次 probe。
        self._hold_zcode()
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                              routes_path=self.routes, evidence_ref='ev', now=T0)
        g = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                       routes_path=self.routes, purpose='probe', now=T0)
        self.assertTrue(g['allowed'] and g['probe_granted'], g)
        # 失败 probe 把真实 429 落库（attempt 匹配在飞 probe）→ 原子 consumed=1。
        qc.record_zcode_unavailability(self.quota, provider=PROVIDER,
                                       quota_group=self.group, classification=_hard(1310),
                                       attempt_id=g['attempt_id'], now=T0)
        st = qc.zcode_availability_status(self.quota, now=T0)
        key = qc.zcode_channel_key(PROVIDER, self.group)
        self.assertTrue(st[key]['recovery_eligibility_consumed'], st[key])
        # 同一旧 reset 'ev'（≤ 本次失败观测）重放 → stale replay 拒绝，绝不再授 probe。
        rr = qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                                   routes_path=self.routes, evidence_ref='ev', now=T0)
        self.assertFalse(rr['reset'], rr)
        self.assertTrue(any('stale reset replay' in x for x in rr['reasons']), rr['reasons'])
        # 新的可信 reset 重新授且只授一次 probe。
        rn = qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                                   routes_path=self.routes, evidence_ref='ev-new', now=T0)
        self.assertTrue(rn['reset'], rn)
        g2 = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                        routes_path=self.routes, purpose='probe', now=T0)
        self.assertTrue(g2['allowed'] and g2['probe_granted'], g2)
        g3 = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                       routes_path=self.routes, purpose='probe',
                                       now=T0 + timedelta(seconds=1))
        self.assertFalse(g3['allowed'], g3)  # 同时在飞第二 probe 被拒


class B6ResetIdempotencyTests(_TmpBase):
    """缺口（B6）：同一 reset 证据代际在**成功核验后 healthy** 时的重放也必须稳定幂等，
    绝不因省略 epoch（正式 API 默认）或带原 epoch 而重新推进代次/重开核验资格。"""

    def _to_healthy(self, evidence='E'):
        self._hold_zcode()
        r1 = qc.zcode_manual_reset(self.quota, provider=PROVIDER,
                                   quota_group=self.group, routes_path=self.routes,
                                   evidence_ref=evidence, now=T0)
        self.assertTrue(r1['reset'])
        g = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                       routes_path=self.routes, purpose='probe', now=T0)
        settle = qc.settle_zcode_attempt(self.quota, provider=PROVIDER,
                                         quota_group=self.group,
                                         attempt_id=g['attempt_id'],
                                         epoch=g['availability_epoch'],
                                         success=True, executed=True, now=T0)
        self.assertTrue(settle['cleared'] and settle['state'] == 'healthy', settle)
        return r1['epoch']

    def _state_epoch(self):
        st = qc.zcode_availability_status(self.quota, now=T0)
        key = qc.zcode_channel_key(PROVIDER, self.group)
        return st[key]['state'], st[key]['epoch'], st[key].get(
            'recovery_eligibility_consumed')

    def test_replay_bound_evidence_omitting_epoch_on_healthy_is_idempotent(self):
        # 缺陷主形：reset(E)→healthy@2 后，正式 API 默认省略 epoch 重放同一 E。
        # 必须幂等返回实际 healthy 状态、不推进代次、不重开资格；B4 只认
        # recovery_unverified，healthy 会误落入新 reset → epoch=3 重开（本用例失败）。
        epoch = self._to_healthy()
        r2 = qc.zcode_manual_reset(self.quota, provider=PROVIDER,
                                   quota_group=self.group, routes_path=self.routes,
                                   evidence_ref='E', now=T0)
        self.assertTrue(r2['reset'] and r2.get('idempotent'), r2)
        self.assertEqual(r2.get('state'), 'healthy', r2)
        self.assertEqual(r2['epoch'], epoch, r2)
        st, ep, consumed = self._state_epoch()
        self.assertEqual(st, 'healthy')
        self.assertEqual(ep, epoch)  # 代次未被推进
        self.assertFalse(json.dumps(r2).count('stale'))

    def test_replay_bound_evidence_with_orig_epoch_on_healthy_is_idempotent(self):
        # 带原 epoch（成功代际）重放同一 E 也必须幂等、不推进。
        epoch = self._to_healthy()
        r2 = qc.zcode_manual_reset(self.quota, provider=PROVIDER,
                                   quota_group=self.group, routes_path=self.routes,
                                   evidence_ref='E', epoch=epoch, now=T0)
        self.assertTrue(r2['reset'] and r2.get('idempotent'), r2)
        self.assertEqual(r2['epoch'], epoch, r2)
        st, ep, _ = self._state_epoch()
        self.assertEqual(st, 'healthy')
        self.assertEqual(ep, epoch)

    def test_healthy_replay_does_not_rearm_eligibility_then_newer_failure_rejects_old(self):
        # healthy 重放不得重开核验资格；随后新失败推进代次后，同一旧 E 无论 epoch 形状
        # （省略/旧代际/新代际）都必须被拒（stale replay / stale reset），绝不清更晚失败。
        epoch = self._to_healthy()
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                              routes_path=self.routes, evidence_ref='E', now=T0)
        # 幂等重放没授新 probe：此刻无在飞、资格已消费/healthy，再 probe 不额外放行。
        st, ep, _ = self._state_epoch()
        self.assertEqual((st, ep), ('healthy', epoch))
        # 一次更新的真实失败推进代次。
        self._hold_zcode(code=1308)
        for shape in ({}, {'epoch': epoch}, {'epoch': epoch + 1}):
            rr = qc.zcode_manual_reset(self.quota, provider=PROVIDER,
                                       quota_group=self.group, routes_path=self.routes,
                                       evidence_ref='E', now=T0, **shape)
            self.assertFalse(rr['reset'], (shape, rr))
        # 更新失败仍在（旧 E 任何形状都没清掉它）。
        self.assertTrue(qc.zcode_blocking_rows(self.quota, now=T0))

    def test_forged_later_success_not_trusted_by_replay(self):
        # 幂等修复绝不能凭 healthy 就信任伪造成功：healthy@2 后若有更新失败，重放旧 E 仍拒。
        epoch = self._to_healthy()
        g = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                       routes_path=self.routes, purpose='dispatch', now=T0)
        self.assertTrue(g['allowed'], g)  # healthy 时普通派工本可放行
        self._hold_zcode(code=1310)       # 真实新失败覆盖 healthy
        rr = qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                                   routes_path=self.routes, evidence_ref='E', now=T0)
        self.assertFalse(rr['reset'], rr)
        self.assertTrue(any('stale' in x for x in rr['reasons']), rr['reasons'])


class B5ReconcileIdentityTests(_TmpBase):
    """缺口 3：所有自动回收路径统一要求原 wrapper 身份齐备且双方确认死亡才释放。"""

    def _insert(self, token, **kw):
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            conn.execute(
                'INSERT INTO attempts(token, task_id, stage, chat_id, prompt_sha256, '
                'workspace, runtime, model, pool_key, state, wrapper_pid, '
                'wrapper_created, child_pid, child_created, reserved_at_utc, '
                "started_at_utc, origin) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (token, 't-' + token, 's1', 'c1', 'p' * 64,
                 str(self._ws_for('t-' + token)), 'zcode', 'GLM-5.3', ZCODE,
                 kw.get('state', 'running'), kw.get('wrapper_pid'),
                 kw.get('wrapper_created'), kw.get('child_pid'),
                 kw.get('child_created'), qc._iso(T0), qc._iso(T0), 'inject'))

    def _state(self, token):
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            r = conn.execute('SELECT state FROM attempts WHERE token=?',
                             (token,)).fetchone()
        return r['state']

    def test_dead_child_without_full_wrapper_identity_is_retained(self):
        CHILD, WRAP = 111, 222
        prober = {CHILD: {'state': 'dead', 'created': None},
                  WRAP: {'state': 'dead', 'created': 'C'}}.get

        def _p(pid):
            return prober(pid, {'state': 'unknown', 'created': None})
        self._insert('a', child_pid=CHILD, wrapper_pid=None, wrapper_created=None)
        self._insert('b', child_pid=CHILD, wrapper_pid=WRAP, wrapper_created=None)
        self._insert('c', child_pid=CHILD, wrapper_pid=WRAP, wrapper_created='REUSED')
        self._insert('e', child_pid=CHILD, wrapper_pid=WRAP, wrapper_created='C')
        res = dp.reconcile(self.dispatch, now=T0, prober=_p)
        self.assertEqual(sorted(res['released']), ['e'], res)
        for t in ('a', 'b', 'c'):
            self.assertEqual(self._state(t), 'unknown', t)  # 保留，绝不自动释放
        self.assertEqual(self._state('e'), 'reconciled_exit')

    def test_dead_child_live_wrapper_is_retained(self):
        CHILD, WRAP = 111, 222
        map_ = {CHILD: {'state': 'dead', 'created': None},
                WRAP: {'state': 'alive', 'created': 'C'}}

        def _p(pid):
            return map_.get(pid, {'state': 'unknown', 'created': None})
        self._insert('w', child_pid=CHILD, wrapper_pid=WRAP, wrapper_created='C')
        res = dp.reconcile(self.dispatch, now=T0, prober=_p)
        self.assertEqual(res['released'], [], res)
        self.assertEqual(self._state('w'), 'unknown')


class B5DefaultAvailabilityTests(_TmpBase):
    """缺口 5：默认（env/无显式参）AUTO 也读取共享 availability，过滤不可用 Z、缺库零写。"""

    def test_default_auto_filters_unavailable_zcode_from_env(self):
        self._hold_zcode()  # hard_hold 写进 self.quota
        with mock.patch.dict(os.environ, {'BRAIN_WORKER_QUOTA_STORE': str(self.quota)}):
            out = dp.select_and_claim(self.dispatch, task_id='dz', runtime='zcode',
                                      model='GLM-5.3', workspace=str(self._ws_for('dz')),
                                      prompt_sha256='p', stage='s1', executor='auto',
                                      now=T0)
        self.assertEqual(self._active(ZCODE), 0, out)  # 默认也过滤：零 ZCode 启动
        self.assertFalse(out['allowed'], out)
        self.assertTrue(out['routing_required'], out)
        self.assertEqual(out['selected']['pool_key'], QMAX)

    def test_default_missing_db_zero_writes_keeps_zcode_eligible(self):
        missing = self.tmp / 'no-quota.sqlite3'
        with mock.patch.dict(os.environ, {'BRAIN_WORKER_QUOTA_STORE': str(missing)}):
            out = dp.select_and_claim(self.dispatch, task_id='mz', runtime='zcode',
                                      model='GLM-5.3', workspace=str(self._ws_for('mz')),
                                      prompt_sha256='p', stage='s1', executor='auto',
                                      now=T0)
        self.assertFalse(missing.exists())  # 缺库绝不建库/建表（零写）
        self.assertTrue(out['allowed'], out)  # 缺库 = 无阻断 → ZCode 合格
        self.assertEqual(self._active(ZCODE), 1)


class B5ScopeComboTests(_TmpBase):
    """缺口 6：受信 combo 用 executor+model 表达（runtime 非必需）；select/reclaim/ask
    同一 combo；冲突/未知 model fail-closed；国内 combo 满时只等该 combo、绝不 Luna。"""

    def _reserve(self, i, runtime, model, executor):
        r = dp.reserve(self.dispatch, task_id=f'{runtime}{model}{i}', runtime=runtime,
                      model=model, workspace=str(self._ws_for(f'{runtime}{model}{i}')),
                      prompt_sha256='p', stage='s1', now=T0, executor=executor)
        self.assertTrue(r['reserved'], r)
        return r['token']

    def test_combo_without_runtime_reclaims_only_flash_never_max(self):
        sc = {**self._scope('fl'), 'executor': 'qoder', 'model': 'Qwen3.8-Flash'}
        max_toks = [self._reserve(i, 'qoder', 'Qwen3.8-Max', 'qoder') for i in range(2)]
        flash_toks = [self._reserve(i, 'qoder', 'Qwen3.8-Flash', 'qoder') for i in range(2)]
        ask = dp.ask_record(self.dispatch, task_id='fl', scope=sc, ask_message_id='m1',
                            now=T0, degradation_reason='capacity',
                            degradation_detail='qoder flash candidates exhausted',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        self._free_slot(max_toks[0])  # 只腾出 Max → B4 会误抢 Max；B5 必须只等 Flash
        out = dp.claim_due(self.dispatch, task_id='fl', now=T0, quota_store=self.quota)
        self.assertFalse(out['claimed'], out)  # Flash 满 → 不回收，绝不改派 Max
        self.assertEqual(self._active(QMAX), 1, out)  # 腾出的 Max 未被本 Flash 任务占用
        self.assertEqual(self._luna_active(), 0)      # 绝不 Luna

    def test_combo_without_runtime_reclaims_flash_when_flash_frees(self):
        sc = {**self._scope('fr'), 'executor': 'qoder', 'model': 'Qwen3.8-Flash'}
        [self._reserve(i, 'qoder', 'Qwen3.8-Max', 'qoder') for i in range(2)]
        flash_toks = [self._reserve(i, 'qoder', 'Qwen3.8-Flash', 'qoder') for i in range(2)]
        ask = dp.ask_record(self.dispatch, task_id='fr', scope=sc, ask_message_id='m1',
                            now=T0, degradation_reason='capacity',
                            degradation_detail='qoder flash candidates exhausted',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        self._free_slot(flash_toks[0])  # 腾出 Flash → 只回收 Flash
        out = dp.claim_due(self.dispatch, task_id='fr', now=T0, quota_store=self.quota)
        self.assertTrue(out['claimed'], out)
        self.assertEqual(out['pool_key'], QFLASH)
        self.assertEqual(self._active(QMAX), 2)  # Max 不受触碰

    def test_combo_conflict_and_unknown_model_fail_closed(self):
        conflict = {**self._scope('cx'), 'executor': 'qoder', 'runtime': 'zcode',
                    'model': 'GLM-5.3'}
        self.assertEqual(dp._scope_allowed_candidates(conflict, zcode_blocked=None), [])
        unknown = {**self._scope('um'), 'executor': 'qoder', 'model': 'GPT-9'}
        self.assertEqual(dp._scope_allowed_candidates(unknown, zcode_blocked=None), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
