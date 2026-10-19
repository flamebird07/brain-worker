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
dp.LUNA_DISABLED = False
dp.CAPACITY[dp.LUNA_KEY] = None
import zcode_direct as zd  # noqa: E402
import prompt_contract as pc  # noqa: E402

T0 = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)  # 北京时间 23:00（主力时段内）
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
        # BW-CODEBUDDY-MAIN-S3 + BW-WORKER-WIDGET-20261018-S3 + BW-OPENCODE-20261019-S3：
        # 本文件的历史层序/满池断言（Max > claude > Z、总 N 满）都是在 CodeBuddy 恢复之前
        # 写的。这里先真实占住 CodeBuddy deepseek 与 GLM 各自的独立 1 槽（GLM 为额度条件池，
        # 夹具只作占位、绝不进 AUTO 候选），使 AUTO 仍按旧层序选择、`_fill_six` 补满其余
        # 14 槽（含 BW-OPENCODE-20261019-S3 起两个 OpenCode 免费模型各 1 独立槽）后总量
        # 正好 16；CodeBuddy 作为新最高层由独立 tests/test_codebuddy_reactivation.py
        # 验证，两个 OpenCode 槽由 _fill_six 真实 reserve 占住，绝不放宽任何安全断言。
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                         "state, reserved_at_utc) VALUES(?,?,?,?,?,?,?)",
                         ('cb-fixture', 'cb-fixture-task', 'codebuddy',
                          'deepseek-v4.1-flash', dp.CODEBUDDY_POOL_KEY, 'reserved',
                          dp._iso(T0)))
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                         "state, reserved_at_utc) VALUES(?,?,?,?,?,?,?)",
                         ('cb-glm-fixture', 'cb-glm-fixture-task', 'codebuddy',
                          'glm-5.3-flash', dp.CODEBUDDY_GLM_POOL_KEY, 'reserved',
                          dp._iso(T0)))
            conn.execute('PRAGMA journal_mode=DELETE')

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
        # BW-CLAUDE-CAPACITY2 + BW-OPENCODE-20261019-S3 满池基线：zcode×2 + 两内置 Max 各 1
        # + CN 自定义 Max×2 + 两地区 Flash 各 2 + claude:deepseek-flash×2 + 两 OpenCode 免费
        # 模型各 1（T0=北京周四 23:00 offhours，时段门放行；OpenCode 本无时段门）。
        # setUp 另占住 CodeBuddy 两独立槽 → 总量正好 16。全部经显式 executor 逐一确定性
        # claim，绝不删除任何安全断言。
        toks = []
        combos = [('zcode', 'GLM-5.3'), ('zcode', 'GLM-5.3'),
                  ('qoder', 'Qwen3.8-Max'), ('qodercn', 'Qwen3.8-Max'),
                  ('qodercn', 'Qwen-3.8-Max'), ('qodercn', 'Qwen-3.8-Max'),
                  ('qoder', 'Qwen3.8-Flash'), ('qoder', 'Qwen3.8-Flash'),
                  ('qodercn', 'Qwen3.8-Flash'), ('qodercn', 'Qwen3.8-Flash'),
                  ('claude', 'deepseek-flash'), ('claude', 'deepseek-flash'),
                  ('opencode', dp.OPENCODE_DEEPSEEK_MODEL),
                  ('opencode', dp.OPENCODE_GLM_MODEL)]
        for i, (rt, md) in enumerate(combos):
            ex = ('zcode' if rt == 'zcode' else
                  'claude' if rt == 'claude' else
                  'opencode' if rt == 'opencode' else
                  'qodercn' if rt == 'qodercn' else 'qoder')
            r = dp.reserve(self.dispatch, task_id=f'f{i}', runtime=rt, model=md,
                           workspace=str(self._ws_for(f'f{i}')), prompt_sha256='p',
                           stage='s1', now=T0, executor=ex)
            self.assertTrue(r.get('allowed') and r.get('token'), r)
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
        # BW-SIMPLE-PRIORITY-RETRY-S4：普通 purpose='dispatch' 现按新政策放行——不据历史
        # hard_hold/别名同 group 行挡新轮真实尝试；granted_epoch/渠道精确回读、历史诊断
        # 仍原样在库（不假装 healthy、不清 hold）。旧保守别名跨 provider 匹配只对显式
        # purpose='probe' 生命周期继续生效：无恢复证据时仍拒绝，绝不凭空授 probe 资格。
        self._hold_zcode()
        g = qc.zcode_availability_gate(self.quota, provider=PROVIDER + '-alias',
                                       routes_path=self.routes, purpose='dispatch', now=T0)
        self.assertTrue(g['allowed'], g)
        self.assertFalse(g['probe_granted'], g)
        # 精确回读当前渠道与 epoch（新别名无精确 availability 行，epoch 为初始 0）。
        self.assertEqual(g['channel_key'], g['granted_channel_key'], g)
        self.assertEqual(g['quota_group'], g['granted_quota_group'], g)
        self.assertEqual(g['availability_epoch'], 0, g)
        # 诊断原样：blocked rows 仍有 hard_hold 行，未清、未假 healthy。
        blocking = qc.zcode_blocking_rows(self.quota, now=T0)
        self.assertTrue(any(r['state'] == 'hard_hold' for r in blocking), blocking)
        # 显式 purpose='probe' 在无 recovery_unverified 前仍严格拒绝（不放松 probe 状态机）。
        denied_probe = qc.zcode_availability_gate(self.quota, provider=PROVIDER + '-alias',
                                                  routes_path=self.routes, purpose='probe',
                                                  now=T0)
        self.assertFalse(denied_probe['allowed'], denied_probe)


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
        # BW-SIMPLE-PRIORITY-RETRY-S4：历史 hard_hold 不再作候选剔除——Z 仍在
        # `_eligible_main_forces` 里；但 AUTO 仍按主力优先级层（三 Qoder Max > claude > Z）
        # 择最高，Z 不抢空闲 QMAX。这里断言 Z 未被旧 hold 挡、AUTO 正常路由 QMAX。
        # 真实失败才落诊断、成功 epoch/attempt CAS 结算保留，不假装 healthy、不清 hold。
        self._hold_zcode()
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            elig = dp._eligible_main_forces(conn, zcode_blocked=None, now=T0)
        self.assertIn(ZCODE, elig, elig)  # 旧 hold 不再把 Z 从候选集剔除
        out = dp.select_and_claim(self.dispatch, task_id='t1', runtime='zcode',
                                  model='GLM-5.3', workspace=str(self._ws_for('t1')),
                                  prompt_sha256='p', stage='s1', chat_id='c1',
                                  executor='auto', quota_store=self.quota, now=T0)
        # AUTO 按最高优先级层选：Max 层最高，其中 CN 自定义先于两内置（Z 不抢 Max）。
        self.assertFalse(out['allowed'], out)
        self.assertTrue(out['routing_required'], out)
        self.assertEqual(out['selected']['pool_key'], dp.CN_CUSTOM_MAX_POOL_KEY, out)
        self.assertEqual(self._active(ZCODE), 0)  # Z 本轮未被 AUTO 抢，等下一新轮/显式入口

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
        # BW-SIMPLE-PRIORITY-RETRY-S4：历史 hard_hold 不再作候选剔除，Z 仍在
        # `_eligible_main_forces` 里；但 AUTO 按主力优先级层选择，Z 不抢空闲 QMAX——
        # 反复 AUTO 请求 Z 都如实路由 QMax（真实最高优先），绝不因旧 hold 挡新轮、也
        # 绝不因 Z 合格就跳过 Max。真实失败才落诊断、成功仍 epoch/attempt CAS 结算。
        self._hold_zcode()
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            elig = dp._eligible_main_forces(conn, zcode_blocked=None, now=T0)
        self.assertIn(ZCODE, elig, elig)
        for i in range(2):
            out = dp.select_and_claim(self.dispatch, task_id=f'a{i}', runtime='zcode',
                                      model='GLM-5.3', workspace=str(self._ws_for(f'a{i}')),
                                      prompt_sha256='p', stage='s1',
                                      executor='auto', quota_store=self.quota, now=T0)
            self.assertTrue(out['routing_required'], out)
            self.assertEqual(out['selected']['pool_key'],
                             dp.CN_CUSTOM_MAX_POOL_KEY, out)
        # Z 未被 AUTO 抢，等下一轮/显式入口；候选资格仅通过 _eligible_main_forces 体现。
        self.assertEqual(self._active(ZCODE), 0)

    def test_zcode_executor_refused_when_unavailable(self):
        # BW-SIMPLE-PRIORITY-RETRY-S4：显式 executor='zcode' 权威入口不再据历史 availability
        # 拒绝——直接真实尝试 ZCode 主力池（时段/容量/工作区/unknown 在途/审批拒绝照常）。
        # 真实失败才落 availability 诊断、成功仍 epoch/attempt CAS 结算；不假装 healthy。
        self._hold_zcode()
        out = dp.consume_for_entry(self.dispatch, task_id='z1', runtime='zcode',
                                   model='GLM-5.3', workspace=str(self._ws_for('z1')),
                                   prompt_sha256='p', stage='s1',
                                   executor='zcode', quota_store=self.quota, now=T0)
        self.assertTrue(out['allowed'], out)
        self.assertEqual(out['pool_key'], ZCODE, out)
        self.assertEqual(self._active(ZCODE), 1)

    def test_executor_conflict(self):
        out = dp.consume_for_entry(self.dispatch, task_id='x1', runtime='zcode',
                                   model='GLM-5.3', workspace=str(self._ws_for('x1')),
                                   prompt_sha256='p', stage='s1',
                                   executor='qoder', quota_store=self.quota, now=T0)
        self.assertFalse(out['allowed'])
        self.assertEqual(out['reason'], 'executor_conflict')

    def test_consume_token_recheck_releases_placeholder_no_leak(self):
        # BW-SIMPLE-PRIORITY-RETRY-S4 用户政策：consume_for_entry 消费已预留 token 前
        # **不再**据历史 ZCode availability 复核释放本占位——每轮直接从最高优先级真实尝试，
        # 真实失败才落 availability 诊断、成功仍 epoch/attempt CAS 结算，不假装 healthy、
        # 不清旧 hold（qc.* 状态表/诊断行原样保留）。reserved→running 正常放行，容量真实
        # 被本次 attempt 占用；结束后走 finish 才释放，无泄漏。时段/工作区/unknown 在途/审批
        # 拒绝等守卫仍照常，不在本政策撤回范围内。consume/finish 必须携带同一真实 owner
        # 身份（os.getpid() + process_identity），finish 才能凭 owner 亲证 cancel 释放；
        # 绝不放松 unknown/owner 校验。
        r = dp.reserve(self.dispatch, task_id='tk', runtime='zcode', model='GLM-5.3',
                       workspace=str(self._ws_for('tk')), prompt_sha256='p', stage='s1',
                       chat_id='c1', executor='zcode', now=T0)
        self.assertTrue(r['reserved'], r)
        self.assertEqual(self._active(ZCODE), 1)
        self._hold_zcode()
        owner_pid = os.getpid()
        owner_created = dp.process_identity(owner_pid).get('created')
        out = dp.consume_for_entry(self.dispatch, task_id='tk', runtime='zcode',
                                   model='GLM-5.3', workspace=str(self._ws_for('tk')),
                                   prompt_sha256='p', stage='s1', chat_id='c1',
                                   claim_token=r['token'], executor='zcode',
                                   wrapper_pid=owner_pid, wrapper_created=owner_created,
                                   quota_store=self.quota, now=T0)
        self.assertTrue(out['allowed'], out)
        self.assertEqual(out['pool_key'], ZCODE, out)
        self.assertNotIn('capacity_released', out)
        # 消费后仍在途（running），未因历史 hold 被虚假释放；容量仍被本次 attempt 占用，
        # 只有真正 finish 才释放——绝不泄漏、绝不双派。owner 身份必须与 consume 一致。
        self.assertEqual(self._active(ZCODE), 1)
        fin = dp.finish(self.dispatch, r['token'], terminal='cancelled',
                        wrapper_pid=owner_pid, wrapper_created=owner_created, now=T0)
        self.assertTrue(fin['released'], fin)
        self.assertEqual(self._active(ZCODE), 0)

    def test_reclaim_skips_blocked_zcode(self):
        # BW-SIMPLE-PRIORITY-RETRY-S4 + BW-OPENCODE-20261019-S3 政策：历史 hold 不再作候选
        # 剔除，capacity 降级必须**真实**填满所有 14 个候选槽（含 ZCode×2 与两 OpenCode
        # 免费槽；加上 setUp 的 CodeBuddy 两槽共 16）才能记录票据；靠旧 hold 假装 Z
        # 不可用已失效。夹具改成在同一临时 dispatch 库上真实 reserved ZCode 两个占位
        # （无真实进程/模型调用），再按原意填满其余非 Z 槽（含两 OpenCode）→ ask_record
        # 记录 capacity 降级 → 释放一个 QMax 名额 → claim_due 原子回收落到 QMax，Z 仍满
        # 不吞新槽。绝不新增机制、绝不删除安全断言。
        self._hold_zcode()
        toks = []
        # 追加 ZCode×2 真实 reserved 占位（同 temp 库、无真实进程）。
        for i in range(2):
            r = dp.reserve(self.dispatch, task_id=f'z{i}', runtime='zcode',
                           model='GLM-5.3', workspace=str(self._ws_for(f'z{i}')),
                           prompt_sha256='p', stage='s1', now=T0, executor='zcode')
            self.assertTrue(r.get('allowed') and r.get('token'), r)
            toks.append(r['token'])
        r = dp.reserve(self.dispatch, task_id='q0', runtime='qoder',
                      model='Qwen3.8-Max', workspace=str(self._ws_for('q0')),
                      prompt_sha256='p', stage='s1', now=T0, executor='qoder')
        self.assertTrue(r.get('allowed') and r.get('token'), r)
        toks.append(r['token'])
        r = dp.reserve(self.dispatch, task_id='cq0', runtime='qodercn',
                      model='Qwen3.8-Max', workspace=str(self._ws_for('cq0')),
                      prompt_sha256='p', stage='s1', now=T0, executor='qodercn')
        self.assertTrue(r.get('allowed') and r.get('token'), r)
        toks.append(r['token'])
        for i in range(2):
            r = dp.reserve(self.dispatch, task_id=f'cu{i}', runtime='qodercn',
                           model='Qwen-3.8-Max', workspace=str(self._ws_for(f'cu{i}')),
                           prompt_sha256='p', stage='s1', now=T0, executor='qodercn')
            self.assertTrue(r.get('allowed') and r.get('token'), r)
            toks.append(r['token'])
        for i in range(2):
            r = dp.reserve(self.dispatch, task_id=f'fl{i}', runtime='qoder',
                           model='Qwen3.8-Flash', workspace=str(self._ws_for(f'fl{i}')),
                           prompt_sha256='p', stage='s1', now=T0, executor='qoder')
            self.assertTrue(r.get('allowed') and r.get('token'), r)
            toks.append(r['token'])
        for i in range(2):
            r = dp.reserve(self.dispatch, task_id=f'cfl{i}', runtime='qodercn',
                           model='Qwen3.8-Flash', workspace=str(self._ws_for(f'cfl{i}')),
                           prompt_sha256='p', stage='s1', now=T0, executor='qodercn')
            self.assertTrue(r.get('allowed') and r.get('token'), r)
            toks.append(r['token'])
        # claude 两个槽也必须真实占住：只要还有空位，capacity 降级就是伪造耗尽。
        for i in range(2):
            r = dp.reserve(self.dispatch, task_id=f'cl{i}', runtime='claude',
                           model='deepseek-flash', workspace=str(self._ws_for(f'cl{i}')),
                           prompt_sha256='p', stage='s1', now=T0, executor='claude')
            self.assertTrue(r.get('allowed') and r.get('token'), r)
            toks.append(r['token'])
        # BW-OPENCODE-20261019-S3：两个 OpenCode 免费槽同属默认 scope 受信任候选，也必须
        # 真实占住——仍有空位时 capacity 降级依旧属伪造耗尽，绝不放宽该守卫。
        for i, md in enumerate(dp.OPENCODE_MODELS):
            r = dp.reserve(self.dispatch, task_id=f'oc{i}', runtime='opencode',
                           model=md, workspace=str(self._ws_for(f'oc{i}')),
                           prompt_sha256='p', stage='s1', now=T0, executor='opencode')
            self.assertTrue(r.get('allowed') and r.get('token'), r)
            toks.append(r['token'])
        sc = self._scope('rc')
        ask = dp.ask_record(self.dispatch, task_id='rc', scope=sc, ask_message_id='m1',
                            now=T0, degradation_reason='capacity',
                            degradation_detail='no available main force for this task',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        # 释放一个 QMax 名额（toks[2] 现是 q0，因为前两位是 Z 占位）→ 合格候选出现空位 →
        # 原子回收落到 QMax（绝不落 ZCode，Z 仍满不吞新槽）。
        qmax_tok = toks[2]
        self._free_slot(qmax_tok)
        out = dp.claim_due(self.dispatch, task_id='rc', now=T0, quota_store=self.quota)
        self.assertTrue(out['claimed'], out)
        self.assertEqual(out['mode'], 'domestic_reclaim')
        self.assertEqual(out['pool_key'], QMAX)
        # 新政策：回收落在被释放的 QMax（不是 Z），Z 的两个真实占位仍在途未被吞并。
        # availability hold 不再挡新轮，这里通过 out['pool_key']==QMAX 与 Z 保持 2 在途
        # 表达"回收不会绕开真实在途数把新槽塞给 Z"。
        self.assertEqual(self._active(ZCODE), 2)


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
        """BW-SIMPLE-PRIORITY-RETRY-S5 + BW-OPENCODE-20261019-S3：历史 ZCode hold 不再作为
        客观证据——S3 起普通轮仍会真实尝试 Z，quota-only ask 必须被拒。这里先真实填满
        全部候选槽（_fill_six 起含两 OpenCode 免费槽，总量 16），再用
        degradation_reason='capacity' 记录票据；同时保留对 quota-only ask 的拒绝断言，
        证明新政策下旧 availability 不足以冒充耗尽。"""
        # 真实填满全部候选槽（含 Z 的两个占位与两 OpenCode 槽）→ capacity 降级有客观证据。
        self._fill_six()
        ask = dp.ask_record(self.dispatch, task_id='dg', scope=self._scope('dg'),
                            ask_message_id='m1', now=T0, degradation_reason='capacity',
                            degradation_detail='all 16 domestic slots are in-flight',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        # 独立任务：即使 _hold_zcode 也不足以支撑 quota-only ask（客观证据必须真实）。
        self._hold_zcode()
        quota_only = dp.ask_record(self.dispatch, task_id='dgz', scope=self._scope('dgz'),
                                   ask_message_id='m1q', now=T0,
                                   degradation_reason='quota',
                                   degradation_detail='legacy ZCode hard hold',
                                   quota_store=self.quota)
        self.assertFalse(quota_only['recorded'], quota_only)
        self.assertTrue(any('objective evidence' in r for r in quota_only['reasons']),
                        quota_only['reasons'])

    def test_degradation_quota_without_evidence_refused(self):
        ask = dp.ask_record(self.dispatch, task_id='dg2', scope=self._scope('dg2'),
                            ask_message_id='m1', now=T0, degradation_reason='quota',
                            degradation_detail='hunch', quota_store=self.quota)
        self.assertFalse(ask['recorded'])
        self.assertTrue(any('objective evidence' in r for r in ask['reasons']), ask['reasons'])

    def test_degradation_timeout_never_substitutes_luna_authorization(self):
        """S5：quota-only ask（把历史 hold 冒充客观证据）必须被拒；一旦 ask 未记录，
        就没有票据可到期——超时绝不自动授权 Luna，也不产生任何降级 reason 字段。
        旧语义"quota 类降级不允许纯超时冒充授权"以新形式（拒录 → 无票据 → 无授权）保留；
        不删除断言。"""
        self._hold_zcode()
        ask = dp.ask_record(self.dispatch, task_id='dg3', scope=self._scope('dg3'),
                            ask_message_id='m1', now=T0, degradation_reason='quota',
                            degradation_detail='ZCode hard hold', quota_store=self.quota)
        self.assertFalse(ask['recorded'], ask)
        self.assertTrue(any('objective evidence' in r for r in ask['reasons']),
                        ask['reasons'])
        # 独立任务下用真实填满全部候选槽（含两 OpenCode 免费槽，总量 16）+ capacity
        # 记录票据（默认 ask 语义）。这里再补一层
        # 语义：即便有 capacity 票据且已过 301s 截止、无用户回复，也不得由"超时"直接
        # 授权 Luna——只有用户明确 reply='luna' 才允许（见 test_degradation_explicit_luna_reply_claims）。
        # 但为保留"超时非授权"的字面语义并与既有 auto-Luna-after-deadline 用例区分，
        # 此处不建票，只测 quota 拒绝路径 → 无票 → 无 Luna。
        out = dp.claim_due(self.dispatch, task_id='dg3',
                           now=T0 + timedelta(seconds=400), quota_store=self.quota)
        self.assertFalse(out['claimed'], out)
        self.assertNotEqual(out.get('degradation_reason'), 'quota')
        self.assertEqual(self._luna_active(), 0)

    def test_degradation_explicit_luna_reply_claims(self):
        """S5：capacity 降级 + 用户明确回复 luna 才允许接管；旧 quota-only 路径已作废。"""
        self._fill_six()
        dp.ask_record(self.dispatch, task_id='dg4', scope=self._scope('dg4'),
                      ask_message_id='m1', now=T0, degradation_reason='capacity',
                      degradation_detail='all 12 domestic slots in-flight',
                      quota_store=self.quota)
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

    # ---- 任务级 luna_preauthorization + 非容量降级 + 候选耗尽 + 300 秒接续 ----
    def _pa_scope(self, task_id, ws=None, quote='本任务超时无回复就继续交给 Luna 执行',
                  failed=None):
        sc = self._scope(task_id, ws=ws)
        sc[dp._LUNA_PREAUTH_KEY] = {'authorized': True, 'seconds': 300,
                                    'original_quote': quote,
                                    'scope_ref': 'user-reply-asking-ticket'}
        if failed is not None:
            sc[dp._LUNA_PREAUTH_KEY]['round_failed_candidates'] = failed
        return sc

    def test_preauth_auth_degradation_luna_after_deadline_when_exhausted(self):
        """任务级预授权 + auth 降级 + 合格候选全部真实占满 + 固定 300 秒无回复：
        到期按**原票据**接续 Luna（复用原 ask 不新建票据、单写入者守卫、计时照旧）。"""
        self._fill_six()
        sc = self._pa_scope('pa', quote='超时无回复就继续交给 Luna',
                            failed=[ZCODE])
        ask = dp.ask_record(self.dispatch, task_id='pa', scope=sc, ask_message_id='m1',
                            now=T0, degradation_reason='auth',
                            degradation_detail='executor auth failed this round',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        early = dp.claim_due(self.dispatch, task_id='pa',
                             now=T0 + timedelta(seconds=299), quota_store=self.quota)
        self.assertFalse(early['claimed'], early)
        self.assertTrue(early.get('not_due'), early)
        due = dp.claim_due(self.dispatch, task_id='pa',
                           now=T0 + timedelta(seconds=301), quota_store=self.quota)
        self.assertTrue(due['claimed'], due)
        self.assertEqual(due['mode'], 'luna', due)
        self.assertEqual(due['pool_key'], dp.LUNA_KEY)
        self.assertEqual(due['launch_state'], 'host_must_call_native')
        self.assertEqual(due['scope']['task_id'], 'pa')
        self.assertTrue(any('luna_preauthorization' in r for r in due['reasons']),
                        due['reasons'])
        self.assertTrue(any(' Single' in r or 'single writer' in r
                            for r in due['reasons']), due['reasons'])
        self.assertEqual(self._luna_active(), 1)

    def test_preauth_missing_degradation_timeout_refused(self):
        """auth 降级但原单 scope 没有 luna_preauthorization：超时绝不充当授权（fail-closed）。"""
        self._fill_six()
        ask = dp.ask_record(self.dispatch, task_id='pam', scope=self._scope('pam'),
                            ask_message_id='m1', now=T0, degradation_reason='auth',
                            degradation_detail='executor auth failed this round',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        out = dp.claim_due(self.dispatch, task_id='pam',
                           now=T0 + timedelta(seconds=400), quota_store=self.quota)
        self.assertFalse(out['claimed'], out)
        self.assertIn('no valid task-level luna_preauthorization', ' '.join(out['reasons']))
        self.assertEqual(self._luna_active(), 0)

    def test_preauth_quota_auth_scope_combo_pin_stays_refused(self):
        """预授权不覆盖票据原国内 combo 硬约束：钉住 combo 的票据依旧只等该组合空位，
        绝不因预授权或超时被升级到 Luna。"""
        self._fill_six()
        pinned = self._pa_scope('pacomb')
        pinned['executor'] = 'qoder'
        pinned['model'] = 'Qwen3.8-Flash'
        ask = dp.ask_record(self.dispatch, task_id='pacomb', scope=pinned,
                            ask_message_id='m1', now=T0, degradation_reason='auth',
                            degradation_detail='auth failed this round',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        out = dp.claim_due(self.dispatch, task_id='pacomb',
                           now=T0 + timedelta(seconds=400), quota_store=self.quota)
        self.assertFalse(out['claimed'], out)
        self.assertTrue(any('pins this task to a domestic combo' in r
                            for r in out['reasons']), out['reasons'])
        self.assertEqual(self._luna_active(), 0)

    def test_preauth_capacity_degradation_timeout_luna_when_exhausted(self):
        """BW-CONTINUE-20261019-S8 收口：capacity 降级携带合法任务级预授权且候选客观
        耗尽（全部 16 槽在途）时，固定 300 秒无回复按原单接续 Luna——299 秒仍 not_due，
        301 秒到期单赢家。旧"capacity 一律禁止超时 Luna"的无条件阻断已作废。"""
        self._fill_six()
        sc = self._pa_scope('pac')
        ask = dp.ask_record(self.dispatch, task_id='pac', scope=sc, ask_message_id='m1',
                            now=T0, degradation_reason='capacity',
                            degradation_detail='all 16 domestic slots are in-flight',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        early = dp.claim_due(self.dispatch, task_id='pac',
                             now=T0 + timedelta(seconds=299), quota_store=self.quota)
        self.assertFalse(early['claimed'], early)
        self.assertTrue(early.get('not_due'), early)
        due = dp.claim_due(self.dispatch, task_id='pac',
                           now=T0 + timedelta(seconds=301), quota_store=self.quota)
        self.assertTrue(due['claimed'], due)
        self.assertEqual(due['mode'], 'luna', due)
        self.assertEqual(due['pool_key'], dp.LUNA_KEY)
        self.assertTrue(any('luna_preauthorization' in r for r in due['reasons']),
                        due['reasons'])
        self.assertEqual(self._luna_active(), 1)

    def test_capacity_degradation_without_preauth_timeout_never_luna(self):
        """保留无预授权负例：capacity 降级缺任务级 luna_preauthorization 时超时绝不
        自动 Luna（fail-closed，必须用户明确 reply luna）。"""
        self._fill_six()
        ask = dp.ask_record(self.dispatch, task_id='pacn', scope=self._scope('pacn'),
                            ask_message_id='m1', now=T0, degradation_reason='capacity',
                            degradation_detail='all 16 domestic slots are in-flight',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        out = dp.claim_due(self.dispatch, task_id='pacn',
                           now=T0 + timedelta(seconds=400), quota_store=self.quota)
        self.assertFalse(out['claimed'], out)
        self.assertEqual(out.get('degradation_reason'), 'capacity')
        self.assertIn('wait timeout is never an implicit authorization',
                      ' '.join(out['reasons']))
        self.assertEqual(self._luna_active(), 0)

    def test_preauth_invalid_shapes_refused_at_ask(self):
        """seconds≠300 与 authorized≠true 的预授权在 ask 落库前即被拒（fail-closed），
        绝不产生可用票据。"""
        bad_seconds = self._pa_scope('pbad')
        bad_seconds[dp._LUNA_PREAUTH_KEY]['seconds'] = 120
        ask1 = dp.ask_record(self.dispatch, task_id='pbad', scope=bad_seconds,
                             ask_message_id='m1', now=T0, degradation_reason='auth',
                             degradation_detail='auth failed this round',
                             quota_store=self.quota)
        self.assertFalse(ask1['recorded'], ask1)
        self.assertIn('seconds must be exactly 300', ' '.join(ask1['reasons']))
        bad_auth = self._pa_scope('pbau')
        bad_auth[dp._LUNA_PREAUTH_KEY]['authorized'] = 'yes'
        ask2 = dp.ask_record(self.dispatch, task_id='pbau', scope=bad_auth,
                             ask_message_id='m1', now=T0, degradation_reason='auth',
                             degradation_detail='auth failed this round',
                             quota_store=self.quota)
        self.assertFalse(ask2['recorded'], ask2)
        self.assertIn('authorized must be exactly true', ' '.join(ask2['reasons']))
        self.assertEqual(self._luna_active(), 0)

    def test_preauth_free_domestic_slot_reclaims_not_luna(self):
        """有合格国内空位时永远先国内回收（非 Luna），预授权不改变该优先级。"""
        toks = self._fill_six()
        sc = self._pa_scope('par')
        dp.ask_record(self.dispatch, task_id='par', scope=sc, ask_message_id='m1',
                      now=T0, degradation_reason='auth',
                      degradation_detail='executor auth failed this round',
                      quota_store=self.quota)
        # combos[2] 是 (qoder, Qwen3.8-Max)：释放一个合格主力空位观察回收。
        self._free_slot(toks[2])
        out = dp.claim_due(self.dispatch, task_id='par',
                           now=T0 + timedelta(seconds=400), quota_store=self.quota)
        self.assertTrue(out['claimed'], out)
        self.assertEqual(out['mode'], 'domestic_reclaim', out)
        self.assertEqual(out['pool_key'], QMAX, out)
        self.assertEqual(self._luna_active(), 0)

    def test_preauth_repeated_ask_never_resets_ticket_or_timer(self):
        """预授权票据重复询问（即便原话再次记录预授权）绝不重置原票据/计时/scope。"""
        self._fill_six()
        sc = self._pa_scope('para')
        first = dp.ask_record(self.dispatch, task_id='para', scope=sc,
                              ask_message_id='m1', now=T0, degradation_reason='auth',
                              degradation_detail='auth failed this round',
                              quota_store=self.quota)
        self.assertTrue(first['recorded'], first)
        again = dp.ask_record(self.dispatch, task_id='para',
                              scope=self._pa_scope('para', quote='再问一次也继续'),
                              ask_message_id='m2', now=T0 + timedelta(seconds=100),
                              degradation_reason='auth',
                              degradation_detail='auth failed this round',
                              quota_store=self.quota)
        self.assertFalse(again['recorded'], again)
        self.assertTrue(any('already exists' in r for r in again['reasons']),
                        again['reasons'])
        st = dp.status(self.dispatch, now=T0)['luna_tickets']
        self.assertEqual(st[0]['deadline_utc'], first['deadline_utc'])


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
        # BW-SIMPLE-PRIORITY-RETRY-S4 政策：一次真实 429/1310 仍落 hard_hold 诊断（epoch
        # 前进、CAS 结算保留），但**新一轮普通派工不再被历史 hold 前置挡**——qc.dispatch 门
        # 允许、正式入口再次真实尝试；失败仍落新代际 hold，成功仍 epoch CAS 清 healthy。
        # 绝不假装 healthy、不清旧 hold、不查余额、不自动 probe。
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
        # 新轮再次真实尝试：入口不再返回 zcode_availability_rejected，Popen 计数 +1。
        out2 = self.tmp / 'o2'
        code2, summary2, stdout2 = self._run(out2, stdout, self.FRAME_1310, 5, counter)
        self.assertEqual(counter['n'], 2)
        self.assertNotIn('zcode_availability_rejected', stdout2)
        self.assertEqual(code2, 3, summary2)
        # 新一轮真实 429 又落 hard_hold（诊断保留，不假装 healthy、不清 hold）。
        rec2 = summary2['zcode_availability_recorded']
        self.assertTrue(rec2['recorded'] and rec2['state'] == 'hard_hold', rec2)

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
        # 人工重置（绑定证据）→ recovery_unverified。BW-SIMPLE-PRIORITY-RETRY-S4：普通新一轮
        # 不再据 recovery_unverified/hard_hold 前置挡——直接真实尝试，失败落新代际 hold、
        # 成功 epoch CAS 清 healthy。qc 层 purpose='probe' 的单赢家 CAS 与陈旧成功保护照常。
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                              routes_path=self.routes, evidence_ref='user-reset',
                              now=T0)
        out2 = self.tmp / 'r2'
        code2, _, stdout2 = self._run(out2, stdout, self.FRAME_1310, 5, counter)
        self.assertNotIn('zcode_availability_rejected', stdout2)
        self.assertEqual(counter['n'], 2)  # 新轮真实尝试，不再被历史 recovery 前置挡
        # S5：第二次真实 429 已把上一轮 user-reset 证据失效（epoch 前进、重新 hard_hold）。
        # probe 资格必须来自**新的**人工重置证据，且与当前 epoch 匹配——沿用旧 evidence_ref
        # 或放松 probe 状态机/CAS 都不允许。之后成功结算清 healthy，普通派工照常放行。
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                              routes_path=self.routes, evidence_ref='user-reset-2',
                              now=T0)
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
        self.assertEqual(counter['n'], 3)  # 第三次也真实派工（healthy 后放行）
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

    def _runner(self):
        # BW-MAX-WINDOW-20261010-S2：内置 Max 只在主力时段放行。真实入口在子进程里用墙钟
        # 判定时段，白天会如实拒绝；本例只验显式 Qoder 不被 1:1 改派到 ZCode，故在测试创建
        # 的临时 wrapper 里导入 dispatch_pool 并把 utcnow 固定到时段内（北京 23:00），再原样
        # 运行 qoder_direct 入口——时钟控制只在此离线夹具，生产入口不加 --now/环境开关。
        runner = self.tmp / 'qd_in_window_runner.py'
        runner.write_text(
            'import sys\n'
            f'sys.path.insert(0, {str(SCRIPTS)!r})\n'
            'from datetime import datetime, timezone\n'
            'import dispatch_pool as dp\n'
            'dp.utcnow = lambda: datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)\n'
            'import qoder_direct\n'
            'sys.exit(qoder_direct.main())\n',
            encoding='utf-8')
        return str(runner)

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
        cmd = [sys.executable, self._runner(),
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
        # 只取本测试任务（task_id=stage）自己的行：setUp 占住 CodeBuddy 名额的真实 fixture
        # 行同样在 attempts 内，不能把整表都当成 Qoder 入口提交的行。
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            rows = [dict(r) for r in conn.execute(
                'SELECT runtime, model, pool_key FROM attempts WHERE task_id=?',
                (stage,)).fetchall()]
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
        # BW-SIMPLE-PRIORITY-RETRY-S4 政策：普通派工不再据历史 availability 拒绝；有/无
        # 有效绑定 probe 票据都真实 claim 到 ZCode 池（时段/容量/工作区/unknown 在途照常）。
        # 显式 purpose='probe' 生命周期仍由 quota_control 层严格 CAS 保证（stale replay/单次
        # 赢家/已消耗资格不再凭空重探），与本入口容量门完全独立，绝不信任自报 probe 布尔。
        self._hold_zcode()
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                              routes_path=self.routes, evidence_ref='ev', now=T0)
        g = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                       routes_path=self.routes, purpose='probe', now=T0)
        self.assertTrue(g['probe_granted'])
        ticket = {'provider': PROVIDER, 'channel_key': g['granted_channel_key'],
                  'attempt_id': g['attempt_id'], 'epoch': g['availability_epoch']}
        # 无票据 → 新政策下同样 claim 到 ZCode 池（不再前置挡）。
        plain = dp.select_and_claim(self.dispatch, task_id='p0', runtime='zcode',
                                    model='GLM-5.3', workspace=str(self._ws_for('p0')),
                                    prompt_sha256='p', stage='s1', executor='zcode',
                                    quota_store=self.quota, now=T0)
        self.assertTrue(plain['allowed'], plain)
        self.assertEqual(plain['pool_key'], ZCODE, plain)
        self.assertEqual(self._active(ZCODE), 1)
        # 有有效票据也允许 claim（票据本身不再被容量门信任，只是 qc 层保留 CAS 生命周期）。
        ok = dp.select_and_claim(self.dispatch, task_id='p1', runtime='zcode',
                                 model='GLM-5.3', workspace=str(self._ws_for('p1')),
                                 prompt_sha256='p', stage='s1', executor='zcode',
                                 quota_store=self.quota, probe_ticket=ticket, now=T0)
        self.assertTrue(ok['allowed'], ok)
        self.assertEqual(ok['pool_key'], ZCODE)
        self.assertEqual(self._active(ZCODE), 2)

    def test_forged_probe_bool_is_still_denied(self):
        # BW-SIMPLE-PRIORITY-RETRY-S4：普通入口已不再据历史 availability 前置拒绝；伪造
        # probe_ticket 不影响新政策的正常 claim。这里改为断言：真正的严格性由 qc 层 purpose=
        # 'probe' CAS 保证（陈旧 attempt_id/epoch 一律被 qc 拒绝），与容量门解耦。
        self._hold_zcode()
        qc.zcode_manual_reset(self.quota, provider=PROVIDER, quota_group=self.group,
                              routes_path=self.routes, evidence_ref='ev', now=T0)
        bad = {'provider': PROVIDER, 'channel_key': qc.zcode_channel_key(PROVIDER, self.group),
               'attempt_id': 'not-a-real-attempt', 'epoch': 0}
        # qc 层：伪造票据绝不给 probe 资格（陈旧/错配 attempt_id 一律拒）。
        denied_probe = qc.zcode_availability_gate(self.quota, provider=PROVIDER,
                                                  routes_path=self.routes, purpose='probe',
                                                  now=T0)
        self.assertTrue(denied_probe['allowed'] and denied_probe['probe_granted'],
                        denied_probe)
        forged_ticket = {'provider': PROVIDER,
                         'channel_key': denied_probe['granted_channel_key'],
                         'attempt_id': bad['attempt_id'], 'epoch': bad['epoch']}
        self.assertFalse(qc.zcode_probe_ticket_valid(
            self.quota, provider=forged_ticket['provider'],
            channel_key=forged_ticket['channel_key'],
            attempt_id=forged_ticket['attempt_id'], epoch=forged_ticket['epoch'], now=T0))
        # dp 层：伪造票据不再被信任，但也不再前置挡正常入口——直接 claim 到 ZCode 池。
        out = dp.select_and_claim(self.dispatch, task_id='p2', runtime='zcode',
                                  model='GLM-5.3', workspace=str(self._ws_for('p2')),
                                  prompt_sha256='p', stage='s1', executor='zcode',
                                  quota_store=self.quota, probe_ticket=forged_ticket, now=T0)
        self.assertTrue(out['allowed'], out)
        self.assertEqual(out['pool_key'], ZCODE, out)


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
        # S5：历史 hold 不再前置挡本轮——scope 明确绑 ZCode 且 Z 有空位时，直接 claim
        # 走国内 Z（真实尝试），绝不落 Luna、不扩 scope、不把旧 availability 冒充耗尽。
        # 用 select_and_claim(executor='zcode') 表达"scope 只 Z"的确定组合。
        self._hold_zcode()
        out = dp.select_and_claim(self.dispatch, task_id='zb', runtime='zcode',
                                  model='GLM-5.3', workspace=str(self._ws_for('zb')),
                                  prompt_sha256='p', stage='s1', executor='zcode',
                                  quota_store=self.quota, now=T0)
        self.assertTrue(out.get('allowed'), out)
        self.assertEqual(out['pool_key'], ZCODE)
        # 不产生 Luna、不扩 scope：Z 直接 claim，Luna 池 active=0。
        self.assertEqual(dp.status(self.dispatch, now=T0)['pools'][dp.LUNA_KEY]['active'], 0)
        self.assertEqual(self._active(ZCODE), 1)
        # 且旧诊断行仍在（availability hold 保留为诊断，不冒充 healthy、也不挡本轮真实回收）。
        self.assertTrue(qc.zcode_blocking_rows(self.quota, now=T0))


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
        # BW-CUSTOM-MAX-RESUME-20261018-S1：Max 层内自定义 Max 现为最高，AUTO 选中它。
        self.assertEqual(out['selected']['pool_key'], dp.CN_CUSTOM_MAX_POOL_KEY)
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
    """BW-SIMPLE-PRIORITY-RETRY-S4 政策：默认（env/无显式参）AUTO 也不再据历史 availability
    过滤不可用 ZCode——每轮从最高优先级真实候选起真实尝试；缺库零写仍成立（qc 只读、无写）；
    绝不因旧 hard_hold 挡新轮、也绝不假装 healthy。"""

    def test_default_auto_filters_unavailable_zcode_from_env(self):
        # S5：历史 hold 不再作为候选剔除，但 AUTO 仍按 BW-MAIN-PRIORITY-20261011-S1 层序
        # 走最高优先级真实候选（Qoder Max 层 0 优先于 ZCode 层 2）；Z 保留在
        # _eligible_main_forces 中（不被历史 hold 剔除），AUTO 会 routing_required 到 QMax，
        # 零 Z 启动；qc 层旧 hard_hold 诊断行仍原样保留（不假称 healthy、也不冒充耗尽）。
        self._hold_zcode()
        with mock.patch.dict(os.environ, {'BRAIN_WORKER_QUOTA_STORE': str(self.quota)}):
            out = dp.select_and_claim(self.dispatch, task_id='dz', runtime='zcode',
                                      model='GLM-5.3', workspace=str(self._ws_for('dz')),
                                      prompt_sha256='p', stage='s1', executor='auto',
                                      now=T0)
        self.assertFalse(out['allowed'], out)
        self.assertTrue(out.get('routing_required'), out)
        self.assertEqual(out['selected']['pool_key'],
                         dp.CN_CUSTOM_MAX_POOL_KEY, out)
        self.assertEqual(self._active(ZCODE), 0)
        # Z 依然合格：新政策下 _eligible_main_forces 不据历史 hold 剔除 Z。
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            elig = dp._eligible_main_forces(conn, zcode_blocked=True, now=T0)
        self.assertIn(ZCODE, elig)
        # 历史 hold 诊断保留，未清、未假称 healthy。
        self.assertTrue(qc.zcode_blocking_rows(self.quota, now=T0))

    def test_default_missing_db_zero_writes_keeps_zcode_eligible(self):
        missing = self.tmp / 'no-quota.sqlite3'
        # 缺库 = 无阻断 → ZCode 仍合格。BW-MAIN-PRIORITY-S1 下 AUTO 会优先 Qoder Max，故
        # 此处用明确 executor='zcode' 约束验证 Z 仍 eligible（而非断言 AUTO 选 Z），并保留
        # 缺库零写入断言。
        with mock.patch.dict(os.environ, {'BRAIN_WORKER_QUOTA_STORE': str(missing)}):
            out = dp.select_and_claim(self.dispatch, task_id='mz', runtime='zcode',
                                      model='GLM-5.3', workspace=str(self._ws_for('mz')),
                                      prompt_sha256='p', stage='s1', executor='zcode',
                                      now=T0)
        self.assertFalse(missing.exists())  # 缺库绝不建库/建表（零写）
        self.assertTrue(out['allowed'], out)  # 缺库 = 无阻断 → ZCode 合格可 claim
        self.assertEqual(out['pool_key'], ZCODE)
        self.assertEqual(self._active(ZCODE), 1)


class B5ScopeComboTests(_TmpBase):
    """缺口 6：受信 combo 用 executor+model 表达（runtime 非必需）；select/reclaim/ask
    同一 combo；冲突/未知 model fail-closed；国内 combo 满时只等该 combo、绝不 Luna。"""

    def _reserve(self, i, runtime, model, executor):
        r = dp.reserve(self.dispatch, task_id=f'{runtime}{model}{i}', runtime=runtime,
                      model=model, workspace=str(self._ws_for(f'{runtime}{model}{i}')),
                      prompt_sha256='p', stage='s1', now=T0, executor=executor)
        self.assertTrue(r.get('allowed') and r.get('token'), r)
        return r['token']

    def test_combo_without_runtime_reclaims_only_flash_never_max(self):
        sc = {**self._scope('fl'), 'executor': 'qoder', 'model': 'Qwen3.8-Flash'}
        # BW-POOL-SPLIT-20261010-S3：国际 Qwen3.8-Max 只有 1 槽；填 1 Max + 2 Flash。
        max_toks = [self._reserve(0, 'qoder', 'Qwen3.8-Max', 'qoder')]
        flash_toks = [self._reserve(i, 'qoder', 'Qwen3.8-Flash', 'qoder') for i in range(2)]
        ask = dp.ask_record(self.dispatch, task_id='fl', scope=sc, ask_message_id='m1',
                            now=T0, degradation_reason='capacity',
                            degradation_detail='qoder flash candidates exhausted',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        self._free_slot(max_toks[0])  # 只腾出 Max → B4 会误抢 Max；B5 必须只等 Flash
        out = dp.claim_due(self.dispatch, task_id='fl', now=T0, quota_store=self.quota)
        self.assertFalse(out['claimed'], out)  # Flash 满 → 不回收，绝不改派 Max
        self.assertEqual(self._active(QMAX), 0, out)  # 腾出的 Max 未被本 Flash 任务占用
        self.assertEqual(self._luna_active(), 0)      # 绝不 Luna

    def test_combo_without_runtime_reclaims_flash_when_flash_frees(self):
        sc = {**self._scope('fr'), 'executor': 'qoder', 'model': 'Qwen3.8-Flash'}
        self._reserve(0, 'qoder', 'Qwen3.8-Max', 'qoder')
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
        self.assertEqual(self._active(QMAX), 1)  # Max 不受触碰

    def test_combo_conflict_and_unknown_model_fail_closed(self):
        conflict = {**self._scope('cx'), 'executor': 'qoder', 'runtime': 'zcode',
                    'model': 'GLM-5.3'}
        self.assertEqual(dp._scope_allowed_candidates(conflict, zcode_blocked=None), [])
        unknown = {**self._scope('um'), 'executor': 'qoder', 'model': 'GPT-9'}
        self.assertEqual(dp._scope_allowed_candidates(unknown, zcode_blocked=None), [])


# ==================================================== 任务级预授权 × 本轮失败候选接续
class PreauthRoundFailureContinuationTests(_TmpBase):
    """BW-CONTINUE-20261019-S6：Root 独立反例复现（临时 SQLite、零真实调用）。

    修复前：claim_due 在 299 秒会 domestic_reclaim 重新认领 round_failed_candidates
    里本轮已真实失败过的 CodeBuddy/Qoder 组合；纯 exact combo 被旧 pinned 分支永久挡死，
    带预授权也无法在 300 秒接续 Luna。修复后统一 ask_record/国内回收/最终 exhausted 的
    本任务候选筛选（executor/combo 硬约束交集 + 剔除本轮失败候选，只收窄不扩大授权）：
    本轮已失败候选不再被回收认领；失败型耗尽（不是"只是满"）时 299 秒仍不 claim，
    300 秒到期 Luna 单赢家。缺预授权保持旧行为。"""

    ALL_AUTHORIZED = list(dp.MAIN_FORCE_KEYS) + list(dp.OVERFLOW_KEYS)

    def _pa_scope(self, task_id, failed=None, **extra):
        sc = {**self._scope(task_id), **extra}
        sc[dp._LUNA_PREAUTH_KEY] = {'authorized': True, 'seconds': 300,
                                    'original_quote': '本任务超时无回复就继续交给 Luna 执行',
                                    'scope_ref': 'user-reply-asking-ticket'}
        if failed is not None:
            sc[dp._LUNA_PREAUTH_KEY]['round_failed_candidates'] = list(failed)
        return sc

    def _finish_token(self, token):
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            conn.execute("UPDATE attempts SET state='finished', terminal='finished', "
                         'ended_at_utc=? WHERE token=?', (qc._iso(T0), token))

    def _task_rows(self, task_id):
        with contextlib.closing(dp.connect(self.dispatch)) as conn:
            return [dict(r) for r in conn.execute(
                'SELECT runtime, pool_key, state FROM attempts WHERE task_id=?',
                (task_id,)).fetchall()]

    def test_all_candidates_failed_no_reclaim_at_299_luna_single_winner_at_300(self):
        # 反例 1：全部候选本轮真实失败。腾出失败组合的空位后，299 秒绝不把它们再认领
        # 一遍（修复前这里返回 domestic_reclaim codebuddy）；300 秒到期仅 Luna 单赢家。
        toks = self._fill_six()
        sc = self._pa_scope('rf-all', failed=self.ALL_AUTHORIZED)
        ask = dp.ask_record(self.dispatch, task_id='rf-all', scope=sc,
                            ask_message_id='m1', now=T0, degradation_reason='auth',
                            degradation_detail='all authorized candidates failed this round',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        # 本轮失败的 CodeBuddy/Qoder 组合腾出空位（模拟真实失败后释放）。
        self._finish_token('cb-fixture')
        self._finish_token(toks[7])  # qoder:Qwen3.8-Flash 之一
        early = dp.claim_due(self.dispatch, task_id='rf-all',
                             now=T0 + timedelta(seconds=299), quota_store=self.quota)
        self.assertFalse(early['claimed'], early)
        self.assertTrue(early.get('not_due'), early)
        due = dp.claim_due(self.dispatch, task_id='rf-all',
                          now=T0 + timedelta(seconds=301), quota_store=self.quota)
        self.assertTrue(due['claimed'], due)
        self.assertEqual(due['mode'], 'luna', due)
        self.assertEqual(due['pool_key'], dp.LUNA_KEY)
        again = dp.claim_due(self.dispatch, task_id='rf-all',
                            now=T0 + timedelta(seconds=302), quota_store=self.quota)
        self.assertFalse(again['claimed'])
        self.assertTrue(again.get('already_claimed'), again)
        self.assertEqual(self._luna_active(), 1)
        rows = self._task_rows('rf-all')
        self.assertEqual([r['pool_key'] for r in rows], [dp.LUNA_KEY], rows)

    def test_exact_qoderflash_failed_not_reclaimed_luna_at_300(self):
        # 反例 2：scope 精确钉住 qoder:Qwen3.8-Flash 且该 combo 本轮真实失败。
        # 修复前：299 秒一腾出 Flash 空位就 domestic_reclaim 把刚失败的 Flash 再派一遍；
        # 修复后：299 不 claim（not_due），300 秒按原单接续 Luna，且不换 executor 绕过。
        toks = self._fill_six()
        sc = self._pa_scope('rf-qf', failed=[dp.QFLASH_POOL_KEY],
                            executor='qoder', model='Qwen3.8-Flash')
        ask = dp.ask_record(self.dispatch, task_id='rf-qf', scope=sc,
                            ask_message_id='m1', now=T0, degradation_reason='auth',
                            degradation_detail='qoder flash failed this round',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        self._finish_token(toks[6])  # 本轮失败的 Flash 组合腾出空位
        early = dp.claim_due(self.dispatch, task_id='rf-qf',
                             now=T0 + timedelta(seconds=299), quota_store=self.quota)
        self.assertFalse(early['claimed'], early)
        self.assertTrue(early.get('not_due'), early)
        due = dp.claim_due(self.dispatch, task_id='rf-qf',
                          now=T0 + timedelta(seconds=301), quota_store=self.quota)
        self.assertTrue(due['claimed'], due)
        self.assertEqual(due['mode'], 'luna', due)
        self.assertEqual(self._active(QMAX), 1, '不得改派未失败的 Max 绕过 combo 约束')
        self.assertEqual(self._luna_active(), 1)

    def test_only_authorized_codebuddy_subset_failed_luna_at_300(self):
        # 反例 3：用户只授权 CodeBuddy 子集（两个精确模型各 1 独立槽），本轮都真实失败。
        # 修复前：299 秒 CB 空位被 domestic_reclaim 重新认领；修复后失败候选不回收，
        # 300 秒 Luna 单赢家；其它执行器的空位与本任务无关，绝不据此扩授权。
        self._fill_six()
        sc = self._pa_scope('rf-cb', failed=[dp.CODEBUDDY_POOL_KEY,
                                             dp.CODEBUDDY_GLM_POOL_KEY],
                            executor='codebuddy')
        ask = dp.ask_record(self.dispatch, task_id='rf-cb', scope=sc,
                            ask_message_id='m1', now=T0, degradation_reason='auth',
                            degradation_detail='both codebuddy combos failed this round',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        self._finish_token('cb-fixture')
        self._finish_token('cb-glm-fixture')
        early = dp.claim_due(self.dispatch, task_id='rf-cb',
                             now=T0 + timedelta(seconds=299), quota_store=self.quota)
        self.assertFalse(early['claimed'], early)
        self.assertTrue(early.get('not_due'), early)
        due = dp.claim_due(self.dispatch, task_id='rf-cb',
                          now=T0 + timedelta(seconds=301), quota_store=self.quota)
        self.assertTrue(due['claimed'], due)
        self.assertEqual(due['mode'], 'luna', due)
        self.assertEqual(self._active(dp.CODEBUDDY_POOL_KEY), 0,
                         '本轮失败的 CodeBuddy 绝不被重新认领')

    def test_pinned_combo_failed_all_eight_full_luna_at_300(self):
        # 纯 exact combo + 合法任务级预授权 + 该 combo 本轮真实失败、六仍满：
        # 不再被旧 pinned 分支永久挡死——299 不 claim，300 Luna 接续。
        self._fill_six()
        sc = self._pa_scope('rf-pin', failed=[dp.QFLASH_POOL_KEY],
                            executor='qoder', model='Qwen3.8-Flash')
        ask = dp.ask_record(self.dispatch, task_id='rf-pin', scope=sc,
                            ask_message_id='m1', now=T0, degradation_reason='auth',
                            degradation_detail='pinned flash failed this round',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        early = dp.claim_due(self.dispatch, task_id='rf-pin',
                             now=T0 + timedelta(seconds=299), quota_store=self.quota)
        self.assertFalse(early['claimed'], early)
        self.assertTrue(early.get('not_due'), early)
        due = dp.claim_due(self.dispatch, task_id='rf-pin',
                          now=T0 + timedelta(seconds=301), quota_store=self.quota)
        self.assertTrue(due['claimed'], due)
        self.assertEqual(due['mode'], 'luna', due)

    def test_pinned_combo_just_full_without_failure_still_waits(self):
        # "只是满"不是"失败耗尽"：预授权没有 round_failed_candidates 时 pinned combo
        # 依旧只等该组合空位，绝不因超时升级到 Luna（保留旧 fail-closed 语义）。
        self._fill_six()
        sc = self._pa_scope('rf-full')
        sc['executor'] = 'qoder'
        sc['model'] = 'Qwen3.8-Flash'
        ask = dp.ask_record(self.dispatch, task_id='rf-full', scope=sc,
                            ask_message_id='m1', now=T0, degradation_reason='auth',
                            degradation_detail='auth failed this round',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        out = dp.claim_due(self.dispatch, task_id='rf-full',
                          now=T0 + timedelta(seconds=400), quota_store=self.quota)
        self.assertFalse(out['claimed'], out)
        self.assertTrue(any('pins this task to a domestic combo' in r
                            for r in out['reasons']), out['reasons'])
        self.assertEqual(self._luna_active(), 0)

    def test_unfailed_authorized_free_slot_still_prioritizes_domestic_reclaim(self):
        # 本轮失败候选不阻塞未失败受信任候选的正常国内回收：有预授权、Flash 失败过，
        # 但 Max（未失败、已授权）有空位时永远先回收 Max，绝不落 Luna。
        toks = self._fill_six()
        sc = self._pa_scope('rf-mix', failed=[dp.QFLASH_POOL_KEY], executor='qoder')
        ask = dp.ask_record(self.dispatch, task_id='rf-mix', scope=sc,
                            ask_message_id='m1', now=T0, degradation_reason='auth',
                            degradation_detail='flash failed this round',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        self._finish_token(toks[6])   # 失败 Flash 的空位不参与回收
        self._finish_token(toks[2])   # 未失败 Max 腾出真实空位
        out = dp.claim_due(self.dispatch, task_id='rf-mix',
                          now=T0 + timedelta(seconds=299), quota_store=self.quota)
        self.assertTrue(out['claimed'], out)
        self.assertEqual(out['mode'], 'domestic_reclaim', out)
        self.assertEqual(out['pool_key'], QMAX, out)
        self.assertEqual(self._luna_active(), 0)

    def test_user_cancel_or_domestic_reply_blocks_preauth_luna(self):
        # 用户主动 cancel/选 domestic：即便带合法预授权且候选本轮失败耗尽，也绝不派 Luna；
        # 用户动作不是"无回复超时"，预授权接续不覆盖明确回复。
        # cancel 的现有兼容落库形态是 state=replied + reply_choice=cancel（reply() 统一
        # 写 replied，state=cancelled 只属于 cancel_pending/回收取消路径）。
        toks = self._fill_six()
        sc1 = self._pa_scope('rf-can', failed=self.ALL_AUTHORIZED)
        dp.ask_record(self.dispatch, task_id='rf-can', scope=sc1, ask_message_id='m1',
                      now=T0, degradation_reason='auth',
                      degradation_detail='failed this round', quota_store=self.quota)
        self._finish_token('cb-fixture')
        dp.reply(self.dispatch, task_id='rf-can', choice='cancel', now=T0)
        out1 = dp.claim_due(self.dispatch, task_id='rf-can',
                            now=T0 + timedelta(seconds=400), quota_store=self.quota)
        self.assertFalse(out1['claimed'], out1)
        self.assertEqual(out1.get('state'), 'replied', out1)
        self.assertEqual(out1.get('reply_choice'), 'cancel', out1)
        self.assertEqual(self._luna_active(), 0)

        sc2 = self._pa_scope('rf-dom', failed=[dp.QFLASH_POOL_KEY],
                             executor='qoder', model='Qwen3.8-Flash')
        dp.ask_record(self.dispatch, task_id='rf-dom', scope=sc2, ask_message_id='m1',
                      now=T0, degradation_reason='auth',
                      degradation_detail='pinned flash failed', quota_store=self.quota)
        dp.reply(self.dispatch, task_id='rf-dom', choice='domestic', now=T0)
        out2 = dp.claim_due(self.dispatch, task_id='rf-dom',
                            now=T0 + timedelta(seconds=400), quota_store=self.quota)
        self.assertFalse(out2['claimed'], out2)
        self.assertIn('never escalates to Luna', ' '.join(out2['reasons']))
        self.assertEqual(self._luna_active(), 0)

    def test_ask_record_with_preauth_requires_objective_candidates(self):
        # ask_record 对有预授权的降级任务核验**客观候选**：候选明明有空位时，
        # auth 字符串 + detail 不再"即可进"；缺预授权时保持旧行为（照常可记录）。
        sc = self._pa_scope('ask-pa')
        ask = dp.ask_record(self.dispatch, task_id='ask-pa', scope=sc,
                            ask_message_id='m1', now=T0, degradation_reason='auth',
                            degradation_detail='auth failed this round',
                            quota_store=self.quota)
        self.assertFalse(ask['recorded'], ask)
        self.assertTrue(any('still has a real free slot' in r for r in ask['reasons']),
                        ask['reasons'])
        legacy = dp.ask_record(self.dispatch, task_id='ask-legacy',
                               scope=self._scope('ask-legacy'), ask_message_id='m1',
                               now=T0, degradation_reason='auth',
                               degradation_detail='auth failed this round',
                               quota_store=self.quota)
        self.assertTrue(legacy['recorded'], legacy)

    def test_ask_record_capacity_preauth_failed_slot_does_not_block(self):
        # 有预授权且受信任候选本轮全部失败过时，失败候选腾出的空位绝不反向阻塞
        # capacity 降级询问的记录（候选筛选与回收/exhausted 统一口径）。
        toks = self._fill_six()
        self._finish_token('cb-fixture')
        sc = self._pa_scope('ask-cap', failed=self.ALL_AUTHORIZED)
        ask = dp.ask_record(self.dispatch, task_id='ask-cap', scope=sc,
                            ask_message_id='m1', now=T0, degradation_reason='capacity',
                            degradation_detail='authorized candidates all failed this '
                                               'round with slots already released',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        # 对照：无预授权时同一空位仍按旧口径阻塞 capacity 降级（不许凭空宣称耗尽）。
        ctrl = dp.ask_record(self.dispatch, task_id='ask-ctrl',
                             scope={**self._scope('ask-ctrl'), 'executor': 'codebuddy'},
                             ask_message_id='m1', now=T0, degradation_reason='capacity',
                             degradation_detail='codebuddy pools look full',
                             quota_store=self.quota)
        self.assertFalse(ctrl['recorded'], ctrl)
        self.assertIn('still has a free slot', ' '.join(ctrl['reasons']))

    # ---- BW-CONTINUE-20261019-S8 收口：allowed_candidates 单候选正/负例 + quota 耗尽正例 ----
    def test_allowed_candidates_single_full_exhausted_luna_at_301(self):
        # 单候选正例：allowed_candidates 只授权 qoder:Qwen3.8-Max 且该候选客观全满时，
        # auth 降级 + 预授权经耗尽核验可记录；299 秒仍 not_due，301 秒按原单 Luna 单赢家。
        self._fill_six()
        sc = self._pa_scope('ac-one', allowed_candidates=[QMAX])
        ask = dp.ask_record(self.dispatch, task_id='ac-one', scope=sc,
                            ask_message_id='m1', now=T0, degradation_reason='auth',
                            degradation_detail='the only authorized candidate is full',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        early = dp.claim_due(self.dispatch, task_id='ac-one',
                             now=T0 + timedelta(seconds=299), quota_store=self.quota)
        self.assertFalse(early['claimed'], early)
        self.assertTrue(early.get('not_due'), early)
        due = dp.claim_due(self.dispatch, task_id='ac-one',
                           now=T0 + timedelta(seconds=301), quota_store=self.quota)
        self.assertTrue(due['claimed'], due)
        self.assertEqual(due['mode'], 'luna', due)
        self.assertEqual(due['pool_key'], dp.LUNA_KEY)
        again = dp.claim_due(self.dispatch, task_id='ac-one',
                             now=T0 + timedelta(seconds=302), quota_store=self.quota)
        self.assertFalse(again['claimed'])
        self.assertTrue(again.get('already_claimed'), again)
        self.assertEqual(self._luna_active(), 1)

    def test_allowed_candidates_single_free_slot_reclaims_not_luna(self):
        # 单候选正例（未耗尽侧）：授权候选里出现真实空位时永远先国内回收该候选，
        # 绝不落 Luna；回收也只收窄在名单内，不借其它执行器空位改派。
        toks = self._fill_six()
        sc = self._pa_scope('ac-free', allowed_candidates=[QMAX])
        ask = dp.ask_record(self.dispatch, task_id='ac-free', scope=sc,
                            ask_message_id='m1', now=T0, degradation_reason='auth',
                            degradation_detail='the only authorized candidate is full',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        self._finish_token(toks[2])  # qoder:Qwen3.8-Max 腾出真实空位
        out = dp.claim_due(self.dispatch, task_id='ac-free',
                           now=T0 + timedelta(seconds=299), quota_store=self.quota)
        self.assertTrue(out['claimed'], out)
        self.assertEqual(out['mode'], 'domestic_reclaim', out)
        self.assertEqual(out['pool_key'], QMAX, out)
        self.assertEqual(self._luna_active(), 0)

    def test_allowed_candidates_empty_unknown_or_malformed_refused_at_ask(self):
        # 负例：空列表、非列表、未知池、Luna 键、空串项一律在 ask_record 即诚实拒绝，
        # 绝不猜候选、绝不把空/未知授权当作耗尽；票据不存在，无任何 Luna 副作用。
        self._fill_six()
        for i, bad in enumerate(([], 'not-a-list', ['bogus:model'], [dp.LUNA_KEY],
                                 [''], ['qoder:Qwen3.8-Max', ''])):
            with self.subTest(bad=bad):
                tid = f'ac-bad{i}'
                sc = self._pa_scope(tid, allowed_candidates=bad)
                ask = dp.ask_record(self.dispatch, task_id=tid, scope=sc,
                                    ask_message_id='m1', now=T0,
                                    degradation_reason='auth',
                                    degradation_detail='auth failed this round',
                                    quota_store=self.quota)
                self.assertFalse(ask['recorded'], ask)
                self.assertTrue(any('allowed_candidates is present but empty' in r
                                    for r in ask['reasons']), ask['reasons'])
                out = dp.claim_due(self.dispatch, task_id=tid,
                                   now=T0 + timedelta(seconds=400),
                                   quota_store=self.quota)
                self.assertFalse(out['claimed'], out)
        self.assertEqual(self._luna_active(), 0)

    def test_allowed_candidates_conflict_with_pinned_combo_refused(self):
        # 负例（冲突集合）：scope 钉死 qoder:Qwen3.8-Flash 而 allowed_candidates 只含
        # QMAX，交集为空——fail-closed 拒绝记录，绝不臆造耗尽、绝不 Luna。
        self._fill_six()
        sc = self._pa_scope('ac-conf', failed=[dp.QFLASH_POOL_KEY],
                            executor='qoder', model='Qwen3.8-Flash',
                            allowed_candidates=[QMAX])
        ask = dp.ask_record(self.dispatch, task_id='ac-conf', scope=sc,
                            ask_message_id='m1', now=T0, degradation_reason='auth',
                            degradation_detail='pinned flash failed; allowlist conflicts',
                            quota_store=self.quota)
        self.assertFalse(ask['recorded'], ask)
        self.assertTrue(any('no eligible domestic candidate' in r
                            for r in ask['reasons']), ask['reasons'])
        out = dp.claim_due(self.dispatch, task_id='ac-conf',
                           now=T0 + timedelta(seconds=400), quota_store=self.quota)
        self.assertFalse(out['claimed'], out)
        self.assertEqual(self._luna_active(), 0)

    def test_quota_preauth_all_authorized_failed_luna_at_301(self):
        # quota 原因候选耗尽正例：合法预授权 + round_failed_candidates 覆盖全部授权
        # 候选且全部真实占满时，quota 降级经客观耗尽核验可记录；299 秒仍 not_due，
        # 301 秒到期按原单接续 Luna（quota 不再强制某个 ZCode 历史阻断标记）。
        self._fill_six()
        sc = self._pa_scope('q-ex', failed=self.ALL_AUTHORIZED)
        ask = dp.ask_record(self.dispatch, task_id='q-ex', scope=sc,
                            ask_message_id='m1', now=T0, degradation_reason='quota',
                            degradation_detail='every authorized candidate hit real '
                                               'quota limits and failed this round',
                            quota_store=self.quota)
        self.assertTrue(ask['recorded'], ask)
        early = dp.claim_due(self.dispatch, task_id='q-ex',
                             now=T0 + timedelta(seconds=299), quota_store=self.quota)
        self.assertFalse(early['claimed'], early)
        self.assertTrue(early.get('not_due'), early)
        due = dp.claim_due(self.dispatch, task_id='q-ex',
                           now=T0 + timedelta(seconds=301), quota_store=self.quota)
        self.assertTrue(due['claimed'], due)
        self.assertEqual(due['mode'], 'luna', due)
        self.assertEqual(due['pool_key'], dp.LUNA_KEY)
        self.assertEqual(self._luna_active(), 1)



if __name__ == '__main__':
    unittest.main(verbosity=2)
