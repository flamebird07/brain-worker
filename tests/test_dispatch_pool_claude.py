"""BW-CLAUDE-DEEPSEEK-S3 离线回归：claude:deepseek-flash 作为第 11 个 AUTO 可选主力槽。

覆盖：物理 DOMESTIC_TOTAL_CAPACITY=11；claude 在 MAIN_FORCE_KEYS 且容量 1（第 2 请求满、绝不
回退别模型）；AUTO 在其它主力占满时可选中 claude（无需用户显式点名）；claude 在途计入国内总在途
聚合、但绝不污染 committed_qoder；显式 executor='claude' 点名；时段门（工作日高峰拒 / 周末 /
2026 假日 / 夜间放行）；跨高峰 reserve→consume 只释放本 token；明确限额落标/释放；以及既有
10 槽关键守卫不被破坏。全部显式临时 sqlite store，绝不读写真实 ~/.brain-worker。零网络/零模型。
"""
import hashlib
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import dispatch_pool as dp  # noqa: E402
import deepseek_offpeak as off  # noqa: E402

CLAUDE = 'claude:deepseek-flash'
MAINS = ('zcode:GLM-5.3', 'qoder:Qwen3.8-Max', 'qodercn:Qwen3.8-Max', 'qodercn:Qwen-3.8-Max')
OFFPEAK_NIGHT = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)   # 北京 23:00 周四 offhours
PEAK_WEEKDAY = datetime(2026, 10, 12, 1, 0, 0, tzinfo=timezone.utc)    # 北京 09:00 周一 高峰
WEEKEND = datetime(2026, 10, 10, 2, 0, 0, tzinfo=timezone.utc)         # 北京 10:00 周六 空闲
HOLIDAY_PEAK = datetime(2026, 10, 1, 2, 0, 0, tzinfo=timezone.utc)     # 北京 10:00 十一 假日


class _ClaudeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = self.base / 'dispatch.sqlite3'
        self._mocker = mock.patch.object(dp, 'process_identity',
                                         return_value={'state': 'alive', 'created': 'c1'})
        self._mocker.start()
        self.addCleanup(self._mocker.stop)
        self.addCleanup(self.tmp.cleanup)

    def ws(self, tid):
        p = self.base / f'ws-{tid}'
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    def claim_claude(self, tid, now=OFFPEAK_NIGHT, executor='claude'):
        return dp.select_and_claim(self.store, task_id=tid, runtime='claude',
                                   model='deepseek-flash', workspace=self.ws(tid),
                                   prompt_sha256='p' + '0' * 63, stage='s', now=now,
                                   executor=executor, _preclaim=False)

    def _reserve(self, tid, runtime, model, executor, now=OFFPEAK_NIGHT):
        return dp.reserve(self.store, task_id=tid, runtime=runtime, model=model,
                          workspace=self.ws(tid), prompt_sha256='p' + '0' * 63, stage='s',
                          now=now, executor=executor, _preclaim=False)

    def occupy_non_claude_mains(self, now=OFFPEAK_NIGHT):
        """占满 6 个非 claude 主力槽（zcode2 + qoder Max1 + cn Max1 + cn 自定义 Max2）。"""
        for i in range(2):
            self.assertTrue(self._reserve(f'z{i}', 'zcode', 'GLM-5.3', 'zcode', now).get('allowed'))
        self.assertTrue(self._reserve('qm', 'qoder', 'Qwen3.8-Max', 'qoder', now).get('allowed'))
        self.assertTrue(self._reserve('cnm', 'qodercn', 'Qwen3.8-Max', 'qodercn', now).get('allowed'))
        for i in range(2):
            self.assertTrue(self._reserve(f'cnx{i}', 'qodercn', 'Qwen-3.8-Max', 'qodercn',
                                          now).get('allowed'))


class CapacityAndRouting(_ClaudeBase):
    def test_claude_is_known_main_force_with_capacity_one(self):
        self.assertTrue(dp.is_known_pool(CLAUDE))
        self.assertEqual(dp.capacity_for(CLAUDE), 1)
        self.assertIn(CLAUDE, dp.MAIN_FORCE_KEYS)

    def test_physical_domestic_total_is_eleven(self):
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 11)

    def test_explicit_claude_claim(self):
        r = self.claim_claude('c1')
        self.assertTrue(r.get('allowed'), r)
        self.assertEqual(r['pool_key'], CLAUDE)

    def test_second_concurrent_claude_is_full(self):
        r1 = self.claim_claude('a')
        r2 = self.claim_claude('b')  # 容量 1 → 第 2 个并发 claude 满，绝不回退别模型
        self.assertTrue(r1.get('allowed'), r1)
        self.assertFalse(r2.get('allowed'), r2)
        self.assertEqual(r2['reason'], 'capacity_full')
        self.assertIn('capacity', r2['reasons'][0].lower())

    def test_auto_selects_claude_when_other_mains_full(self):
        # 另一主力的 AUTO 请求（未点名 claude）：其余 6 个主力槽占满、claude 空闲 → 选中 claude
        self.occupy_non_claude_mains()
        r = dp.select_and_claim(self.store, task_id='auto', runtime='qoder',
                                model='Qwen3.8-Max', workspace=self.ws('auto'),
                                prompt_sha256='p', stage='s', now=OFFPEAK_NIGHT,
                                executor='auto', _preclaim=False)
        self.assertTrue(r.get('routing_required'), r)
        self.assertEqual(r['selected']['pool_key'], CLAUDE)

    def test_claude_inflight_counted_in_domestic_aggregate(self):
        self.claim_claude('c1')
        st = dp.status(self.store, now=OFFPEAK_NIGHT)
        self.assertEqual(st['domestic']['active'], 1)   # claude 在途计入国内总数
        self.assertEqual(st['pools'][CLAUDE]['active'], 1)

    def test_claude_does_not_pollute_committed_qoder(self):
        self.claim_claude('c1')
        st = dp.status(self.store, now=OFFPEAK_NIGHT)
        self.assertEqual(st['rotation']['committed_qoder'], 0)
        self.assertEqual(st['rotation']['committed_zcode'], 0)

    def test_auto_claude_entry_does_not_bypass_fair_selection(self):
        # BW-CLAUDE-DEEPSEEK-S4 反例：AUTO 的 claude 入口请求绝不绕过公平选择直接认领
        # 自己那槽——空库三组 committed 全 0 时 claude 平票排 Z2/Qoder 之后 → routing_required。
        r = dp.select_and_claim(self.store, task_id='afc', runtime='claude',
                                model='deepseek-flash', workspace=self.ws('afc'),
                                prompt_sha256='p' + '0' * 63, stage='s',
                                now=OFFPEAK_NIGHT, executor='auto', _preclaim=False)
        self.assertFalse(r.get('allowed'), r)
        self.assertTrue(r.get('routing_required'), r)
        self.assertEqual(r['selected']['pool_key'], 'qoder:Qwen3.8-Max', r)

    def test_auto_claude_entry_peak_is_honestly_closed(self):
        # AUTO 的 claude 入口高峰请求：如实 main_force_window_closed，绝不绕过时段门认领。
        r = self.claim_claude('apc', now=PEAK_WEEKDAY, executor='auto')
        self.assertFalse(r.get('allowed'), r)
        self.assertEqual(r['reason'], 'main_force_window_closed')

    def test_total_guard_blocks_claude_with_migration_debt(self):
        # BW-CLAUDE-DEEPSEEK-S4 反例：真实国内在途已达总 11（10 个在容量的在途 +
        # 1 条 retired 池超额迁移在途），claude 自身按容量看似仍空（0/1），但总守卫
        # 对 claude 同样生效，绝不派第 12 个。显式点名 claude 也如实拒绝。
        self.occupy_non_claude_mains()
        for i in range(2):
            self.assertTrue(self._reserve(f'of{i}', 'qoder', 'Qwen3.8-Flash',
                                          'qoder').get('allowed'))
            self.assertTrue(self._reserve(f'cf{i}', 'qodercn', 'Qwen3.8-Flash',
                                          'qodercn').get('allowed'))
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                         "state, reserved_at_utc) VALUES(?,?,?,?,?,?,?)",
                         ('legacy-mig', 'lm', 'qodercn', 'DeepSeek-Flash',
                          'qodercn:DeepSeek-Flash', 'running',
                          dp._iso(OFFPEAK_NIGHT)))
        st = dp.status(self.store, now=OFFPEAK_NIGHT)
        self.assertEqual(st['domestic']['active'], 11, st['domestic'])
        self.assertEqual(st['pools'][CLAUDE]['active'], 0, st['pools'][CLAUDE])
        r = self.claim_claude('late', executor='claude')
        self.assertFalse(r.get('allowed'), r)
        self.assertEqual(r['reason'], 'capacity_full', r)
        self.assertTrue(r.get('domestic_full'), r)


class WindowGate(_ClaudeBase):
    def test_peak_weekday_blocks_new_dispatch(self):
        r = self.claim_claude('c1', now=PEAK_WEEKDAY)
        self.assertFalse(r.get('allowed'), r)
        self.assertEqual(r['reason'], 'main_force_window_closed')
        self.assertIn('deepseek-flash', r['reasons'][0])

    def test_weekend_allows(self):
        self.assertTrue(self.claim_claude('c1', now=WEEKEND).get('allowed'))

    def test_holiday_peak_allows(self):
        self.assertTrue(self.claim_claude('c1', now=HOLIDAY_PEAK).get('allowed'))

    def test_cross_peak_reserve_then_consume_releases_token(self):
        tok = self._reserve('c1', 'claude', 'deepseek-flash', 'claude', OFFPEAK_NIGHT)['token']
        # 预留发生在 offhours、真正启动已跨过工作日高峰 → consume 释放本 token（不误抢别人）。
        # consume 的 stage/prompt_sha256 必须与 reserve 精确一致（'s' / 'p'+63*'0'），
        # 否则校验先以 claim_invalid 拒绝，测不到时段门。
        c = dp.consume_for_entry(self.store, task_id='c1', runtime='claude',
                                 model='deepseek-flash', workspace=self.ws('c1'),
                                 prompt_sha256='p' + '0' * 63, stage='s', claim_token=tok,
                                 now=PEAK_WEEKDAY, executor='claude')
        self.assertFalse(c['allowed'], c)
        self.assertEqual(c['reason'], 'main_force_window_closed')
        self.assertTrue(c['capacity_released'], c)
        self.assertEqual(dp.status(self.store, now=PEAK_WEEKDAY)['pools'][CLAUDE]['active'], 0)


class QuotaLimitMarker(_ClaudeBase):
    def _evidence(self):
        p = self.base / 'stdout.json'
        p.write_text('{"result_errors":[{"status":429}]}\n', encoding='utf-8', newline='\n')
        return str(p), hashlib.sha256(p.read_bytes()).hexdigest()

    def test_claude_can_carry_and_release_limit_marker(self):
        tok2 = self._reserve('c2', 'claude', 'deepseek-flash', 'claude', OFFPEAK_NIGHT)['token']
        ev_path, ev_sha = self._evidence()
        rec = dp.record_main_force_limit(self.store, CLAUDE, task_id='c2', attempt_token=tok2,
                                         evidence_path=ev_path, evidence_sha256=ev_sha,
                                         now=OFFPEAK_NIGHT)
        self.assertTrue(rec['recorded'], rec)
        self.assertTrue(dp.main_force_limited(self.store, CLAUDE, now=OFFPEAK_NIGHT))
        # consume 的 stage/prompt_sha256 与 reserve 精确一致，才能测到限额标记门。
        c = dp.consume_for_entry(self.store, task_id='c2', runtime='claude',
                                 model='deepseek-flash', workspace=self.ws('c2'),
                                 prompt_sha256='p' + '0' * 63, stage='s', claim_token=tok2,
                                 now=OFFPEAK_NIGHT, executor='claude')
        self.assertFalse(c['allowed'], c)
        self.assertEqual(c['reason'], 'main_force_limited')
        self.assertTrue(c['capacity_released'], c)
        rel = dp.release_main_force_limit(self.store, CLAUDE, note='reset', now=OFFPEAK_NIGHT)
        self.assertTrue(rel['released'], rel)


class WindowGateCaliber(unittest.TestCase):
    def test_dispatch_pool_and_offpeak_agree(self):
        for when in (PEAK_WEEKDAY, WEEKEND, HOLIDAY_PEAK, OFFPEAK_NIGHT):
            with self.subTest(when=when):
                self.assertEqual(dp._max_window_blocks(CLAUDE, when),
                                 not off.can_dispatch(when))


if __name__ == '__main__':
    unittest.main()
