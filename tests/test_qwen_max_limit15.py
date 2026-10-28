"""BW-QWEN-MAX-LIMIT15-20261028 两个 Qoder 内置 Max 15 分钟限额短窗离线回归。

零真实模型/零网络/零 Popen，只用临时 SQLite 与合成错误载体，绝不触碰真实共享池/
凭据/生产库。覆盖：
- 权威映射：qoder:Qwen3.8-Max 与 qodercn:Qwen3.8-Max = 900 秒；原三池仍 1800 秒；
  带短横线的 CN 自定义 Max（qodercn:Qwen-3.8-Max）与 Sonus 不进窗口表；
- 899 秒仍拒绝新派发 / 恰好 900 秒放行且不删证据、不声称恢复；
- 重复新限额由更晚 ended_at_utc 重新计时；
- 未来/畸形/无时区时间不伪造有效窗口（沿用既有 fail-open 语义：不阻断，但证据保留）；
- 两个内置 Max 池互不连坐；ZCode 1800 秒窗口不受影响；
- 派发门（reserve/consume/status/quota_limit_window_status）与浮窗纯函数同口径；
- 浮窗：运行中不显示历史限额；900 秒外撤下“上轮限额”；详情文案含 15 分钟口径。
"""
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dispatch_pool as dp  # noqa: E402
import worker_status_widget as wsw  # noqa: E402

QMAX = 'qoder:Qwen3.8-Max'
CNMAX = 'qodercn:Qwen3.8-Max'
CN_CUSTOM_MAX = 'qodercn:Qwen-3.8-Max'
ZC = 'zcode:GLM-5.3'
T0 = datetime(2026, 10, 28, 18, 0, 0, tzinfo=timezone.utc)  # 北京 02:00，Max 夜间门开
SHA64 = 'p' + '0' * 63


class _PoolBase(unittest.TestCase):
    """独立临时 dispatch 库；沿用 test_limit30_policy 的合成手法。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-limit15-')
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        self.addCleanup(self._tmp.cleanup)
        with closing(dp.connect(self.store)) as conn:
            conn.execute('PRAGMA journal_mode=DELETE')

    def ws(self, task):
        d = self.base / 'ws' / str(task)
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def insert_attempt(self, token, task, pk, state='running', *, ended=T0):
        runtime, model = pk.split(':', 1)
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, '
                         'pool_key, state, reserved_at_utc, ended_at_utc, workspace) '
                         'VALUES(?,?,?,?,?,?,?,?,?)',
                         (token, task, runtime, model, pk, state, dp._iso(T0),
                          dp._iso(ended) if ended else None, self.ws(task)))

    def mark(self, pk, task, token, *, now=T0):
        p = self.base / f'{task}-limit.json'
        p.write_text(json.dumps({'errors': [{'status': 429,
                                             'message': 'rate limited'}]}),
                     encoding='utf-8')
        rec = dp.record_main_force_limit(
            self.store, pk, task_id=task, attempt_token=token,
            evidence_path=str(p),
            evidence_sha256=hashlib.sha256(p.read_bytes()).hexdigest(), now=now)
        self.assertTrue(rec['recorded'], rec)

    def claim(self, task, runtime, model, executor, now):
        return dp.reserve(self.store, task_id=task, runtime=runtime, model=model,
                          workspace=self.ws(task), prompt_sha256=SHA64, stage='s',
                          executor=executor, now=now, _preclaim=False)


class TestAuthoritativeWindowMap(unittest.TestCase):
    """单一权威映射：两内置 Max 900 秒，原三池 1800 秒，其它池不受影响。"""

    def test_by_pool_seconds(self):
        self.assertEqual(dp.quota_limit_window_seconds(QMAX), 900)
        self.assertEqual(dp.quota_limit_window_seconds(CNMAX), 900)
        self.assertEqual(dp.quota_limit_window_seconds(ZC), 1800)
        self.assertEqual(dp.quota_limit_window_seconds('claude:deepseek-flash'), 1800)
        self.assertEqual(dp.quota_limit_window_seconds(
            'codebuddy:deepseek-v4.1-flash'), 1800)

    def test_hyphen_pool_and_sonus_not_windowed(self):
        # 带短横线的 CN token plan Max 绝不进短窗（保持全档禁用的现状，不被连坐）。
        self.assertIsNone(dp.quota_limit_window_seconds(CN_CUSTOM_MAX))
        self.assertIsNone(dp.quota_limit_window_seconds('qoder:Sonus'))

    def test_widget_shares_the_same_authority(self):
        # 浮窗与派发门同口径：同一映射对象，不在 UI 另设数字。
        self.assertIs(wsw.WINDOW_LIMIT_SECONDS_BY_POOL, dp.QUOTA_LIMIT_WINDOW_BY_POOL)
        self.assertEqual(wsw.WINDOW_LIMIT_POOLS, dp.QUOTA_LIMIT_WINDOW_POOLS)


class TestDispatchWindow(_PoolBase):
    """派发门 899 拒 / 900 放行；重复限额重计时；畸形时间 fail-open 不删证据。"""

    def _bound_and_release(self, pk, tag, *, ended=T0):
        tok = f'bound-{tag}'
        self.insert_attempt(tok, f'b-{tag}', pk, 'running', ended=ended)
        self.mark(pk, f'b-{tag}', tok)
        with closing(dp.connect(self.store)) as conn:
            conn.execute("UPDATE attempts SET state='finished', "
                         "terminal='finished' WHERE token=?", (tok,))
        return tok

    def test_899_rejects_and_exactly_900_allows(self):
        for pk, runtime, tag in ((QMAX, 'qoder', 'qi'), (CNMAX, 'qodercn', 'qc')):
            with self.subTest(pool=pk):
                self._bound_and_release(pk, tag)
                denied = self.claim(f'new-{tag}', runtime, 'Qwen3.8-Max', runtime,
                                    T0 + timedelta(seconds=899))
                self.assertFalse(denied.get('allowed'), denied)
                self.assertEqual(denied['reason'], 'main_force_limited', denied)
                allowed = self.claim(f'exp-{tag}', runtime, 'Qwen3.8-Max', runtime,
                                     T0 + timedelta(seconds=900))
                self.assertTrue(allowed.get('allowed'), allowed)
                self.assertEqual(allowed['pool_key'], pk)
                # 只读放行：原标记与证据仍在，不声称恢复。
                with closing(dp.connect(self.store)) as conn:
                    row = conn.execute('SELECT limited FROM main_force_limits '
                                       'WHERE pool_key=?', (pk,)).fetchone()
                    self.assertEqual(int(row['limited']), 1)

    def test_repeat_limit_retimes_from_newer_ended_at(self):
        self._bound_and_release(QMAX, 'r1', ended=T0)
        later = T0 + timedelta(seconds=600)
        tok2 = 'bound-r2'
        self.insert_attempt(tok2, 'b-r2', QMAX, 'running', ended=later)
        self.mark(QMAX, 'b-r2', tok2, now=later)
        with closing(dp.connect(self.store)) as conn:
            conn.execute("UPDATE attempts SET state='finished', "
                         "terminal='finished' WHERE token=?", (tok2,))
        # 第一窗口到期（1200 秒）但新限额只过了 600 秒：仍拒绝；1500 秒后放行。
        denied = self.claim('re1', 'qoder', 'Qwen3.8-Max', 'qoder',
                            later + timedelta(seconds=899))
        self.assertEqual(denied['reason'], 'main_force_limited', denied)
        allowed = self.claim('re2', 'qoder', 'Qwen3.8-Max', 'qoder',
                             later + timedelta(seconds=900))
        self.assertTrue(allowed.get('allowed'), allowed)

    def test_future_or_malformed_time_fail_open_and_keep_evidence(self):
        # 沿用既有安全语义：非法/未来时间不伪造有效窗口（不阻断新派发），但证据保留。
        for ref_text in ('not-a-time', '2026-10-28T04:00:00',
                         '2099-01-01T00:00:00+00:00'):
            with self.subTest(ref=ref_text):
                tok = f'mal-{abs(hash(ref_text)) % 1000}'
                self.insert_attempt(tok, f'mt-{tok}', QMAX, 'running', ended=None)
                with closing(dp.connect(self.store)) as conn:
                    conn.execute('INSERT INTO main_force_limits(pool_key, limited, '
                                 'attempt_token, recorded_at_utc, updated_at_utc) '
                                 'VALUES(?,?,?,?,?)',
                                 (QMAX, 1, tok, ref_text, ref_text))
                self.assertFalse(self._active(QMAX, T0))
                with closing(dp.connect(self.store)) as conn:
                    row = conn.execute('SELECT limited FROM main_force_limits '
                                       'WHERE pool_key=?', (QMAX,)).fetchone()
                    self.assertEqual(int(row['limited']), 1)
                # 清理本子用例标记，避免串扰后续口径。
                with closing(dp.connect(self.store)) as conn:
                    conn.execute('DELETE FROM main_force_limits WHERE pool_key=?',
                                 (QMAX,))
                    conn.execute('DELETE FROM attempts WHERE token=?', (tok,))

    def _active(self, pk, now):
        with closing(dp.connect(self.store)) as conn:
            return dp._quota_limit_window_active(conn, pk, now)

    def test_two_max_pools_independent_and_zcode_keeps_1800(self):
        self._bound_and_release(QMAX, 'ind-q')
        # 国际 Max 限额不连坐 CN 内置 Max：899 秒时 CN 正常可选。
        ok = self.claim('ind-c', 'qodercn', 'Qwen3.8-Max', 'qodercn',
                        T0 + timedelta(seconds=899))
        self.assertTrue(ok.get('allowed'), ok)
        # 原三池保持 1800 秒：ZCode 限额在 900 秒后仍拒绝。
        self._bound_and_release(ZC, 'ind-z')
        denied = self.claim('ind-z2', 'zcode', 'GLM-5.3', 'zcode',
                            T0 + timedelta(seconds=1500))
        self.assertEqual(denied['reason'], 'main_force_limited', denied)

    def test_consume_within_window_rejected_and_releases_own_reserved(self):
        # consume 与派发门同口径：900 秒内消费已预留 token 一致拒绝，只把本 token 的
        # reserved 真实释放为 start_failed；恰满 900 秒放行。
        mine = self.claim('cm1', 'qoder', 'Qwen3.8-Max', 'qoder', T0)
        self.assertTrue(mine.get('allowed'), mine)
        ev = self.base / 'cm1.json'
        ev.write_text(json.dumps({'errors': [{'status': 429,
                                              'message': 'rate limited'}]}),
                      encoding='utf-8')
        dp.record_main_force_limit(
            self.store, QMAX, task_id='cm1', attempt_token=mine['token'],
            evidence_path=str(ev),
            evidence_sha256=hashlib.sha256(ev.read_bytes()).hexdigest(), now=T0)
        out = dp.consume_for_entry(self.store, task_id='cm1', runtime='qoder',
                                   model='Qwen3.8-Max', workspace=self.ws('cm1'),
                                   prompt_sha256=SHA64, stage='s',
                                   claim_token=mine['token'],
                                   now=T0 + timedelta(seconds=899))
        self.assertFalse(out.get('allowed'), out)
        self.assertEqual(out['reason'], 'main_force_limited', out)
        self.assertTrue(out.get('capacity_released'), out)
        with closing(dp.connect(self.store)) as conn:
            r = conn.execute('SELECT state FROM attempts WHERE token=?',
                             (mine['token'],)).fetchone()
            self.assertEqual(r['state'], 'start_failed')
        later = self.claim('cm2', 'qoder', 'Qwen3.8-Max', 'qoder',
                           # 参照时刻是绑定 attempt 的 ended_at（T0+899 释放时刻）。
                           T0 + timedelta(seconds=899 + 900))
        self.assertTrue(later.get('allowed'), later)

    def test_window_status_reports_pool_specific_expiry(self):
        tok = 'st-q'
        self.insert_attempt(tok, 'st-q', QMAX, 'running', ended=T0)
        self.mark(QMAX, 'st-q', tok)
        with closing(dp.connect(self.store)) as conn:
            s = dp.quota_limit_window_status(conn, QMAX, now=T0 + timedelta(seconds=899))
            self.assertEqual(s['state'], 'active', s)
            self.assertEqual(s['expires_at_utc'],
                             dp._iso(T0 + timedelta(seconds=900)), s)
            s2 = dp.quota_limit_window_status(conn, QMAX,
                                              now=T0 + timedelta(seconds=900))
            self.assertEqual(s2['state'], 'expired', s2)
            s3 = dp.quota_limit_window_status(conn, CN_CUSTOM_MAX, now=T0)
            self.assertEqual(s3['state'], 'not_applicable', s3)


class TestWidgetRules(unittest.TestCase):
    """浮窗纯函数：899 显示 / 900 撤下；运行中不显示；映射同口径。"""

    def _lim(self, ended, token='t1'):
        return {'limited': 1, 'attempt_token': token, 'recorded_at_utc': ended}

    def test_max_pool_899_shows_900_clears(self):
        for pk in (QMAX, CNMAX):
            with self.subTest(pool=pk):
                lim = self._lim(dp._iso(T0))
                latest = {pk: {'token': 't1'}}
                self.assertTrue(wsw._prev_limited_for(
                    pk, lim, latest, dp._iso(T0), T0 + timedelta(seconds=899)))
                self.assertFalse(wsw._prev_limited_for(
                    pk, lim, latest, dp._iso(T0), T0 + timedelta(seconds=900)))
                # 主行标签：空闲且窗口内 → “上轮限额”。
                self.assertEqual(wsw._main_state_display('idle', '空闲', True), '上轮限额')

    def test_running_hides_historical_limit(self):
        lim = self._lim(dp._iso(T0))
        latest = {QMAX: {'token': 't1'}}
        self.assertTrue(wsw._prev_limited_for(
            QMAX, lim, latest, dp._iso(T0), T0 + timedelta(seconds=10)))
        self.assertEqual(wsw._main_state_display('running', '运行中', True,
                                                 has_true_running=True), '运行中')

    def test_old_marker_far_past_900s_naturally_clears(self):
        # 现场两条旧记录已远超 900 秒：新代码加载即自然消退，无需手工清除。
        lim = self._lim('2026-10-27T12:13:41+00:00')
        latest = {QMAX: {'token': 't1'}}
        now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(wsw._prev_limited_for(QMAX, lim, latest,
                                               '2026-10-27T12:13:41+00:00', now))

    def test_detail_text_mentions_15_minutes_for_builtin_max(self):
        row = {'pool_key': QMAX, 'prev_limited': True, 'attempts': [],
               'label': '行', 'note': '注', 'capacity': 1, 'active': 0,
               'state': 'idle', 'state_label': '空闲', 'window_blocked': False,
               'last_result_label': '—', 'last_result': 'unknown'}
        text = wsw.detail_text(row)
        self.assertIn('15 分钟', text)
        self.assertIn('上轮限额', text)
        # 原三池文案仍为 30 分钟口径。
        row30 = dict(row, pool_key=ZC)
        self.assertIn('30 分钟', wsw.detail_text(row30))

    def test_hyphen_pool_keeps_legacy_token_binding_rule(self):
        # 带短横线池不在窗口表：沿用“标记绑定最新 attempt”旧规则。
        self.assertNotIn(CN_CUSTOM_MAX, wsw.WINDOW_LIMIT_POOLS)
        lim = self._lim(dp._iso(T0), token='t1')
        latest = {CN_CUSTOM_MAX: {'token': 't2'}}
        self.assertFalse(wsw._prev_limited_for(
            CN_CUSTOM_MAX, lim, latest, dp._iso(T0), T0 + timedelta(seconds=10)))


if __name__ == '__main__':
    unittest.main()
