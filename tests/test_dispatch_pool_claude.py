"""BW-CLAUDE-DEEPSEEK-S3 / BW-CLAUDE-CAPACITY2 离线回归：claude:deepseek-flash AUTO 可选主力槽。

覆盖：物理 DOMESTIC_TOTAL_CAPACITY=15（BW-ZCODE-RESTORE-1-20261022：含 ZCode 恢复的
严格 1 槽、CodeBuddy 1 槽、GLM 独立 1 槽与两
OpenCode 免费槽各 1，BW-OPENCODE-20261019 现行口径）；claude 在 MAIN_FORCE_KEYS 且容量 2（第 2 个请求可认领、
第 3 个满且绝不回退别模型；完成释放后可再用；同 workspace 仍拒绝；reserved/running/unknown 均
正确占位）；AUTO 在其它主力占满时可选中 claude（无需用户显式点名）；claude 在途计入国内总在途
聚合、但绝不污染 committed_qoder；显式 executor='claude' 点名；**时段门已整体解除
（BW-CLAUDE-ALLDAY-QMAX-AFTER-20261023：任何注入时间都可新派）**、1800 秒真实限额短窗
照旧；明确限额落标/释放；总 15 守卫包含历史迁移占位且绝不派第 16 个。全部显式临时 sqlite
store，绝不读写真实 ~/.brain-worker。零网络/零模型。
"""
import hashlib
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import dispatch_pool as dp  # noqa: E402
import deepseek_offpeak as off  # noqa: E402
from dispatch_mode_support import dispatch_mode, insert_sonus_leftover  # noqa: E402

CLAUDE = 'claude:deepseek-flash'
MAINS = ('zcode:GLM-5.3', 'qoder:Qwen3.8-Max', 'qodercn:Qwen3.8-Max', 'qodercn:Qwen-3.8-Max')
OFFPEAK_NIGHT = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)   # 北京 23:00 周四 offhours
PEAK_WEEKDAY = datetime(2026, 10, 12, 1, 0, 0, tzinfo=timezone.utc)    # 北京 09:00 周一 高峰
PEAK_WEEKDAY_1500 = datetime(2026, 10, 12, 7, 0, 0, tzinfo=timezone.utc)  # 北京 15:00 周一 高峰
WEEKEND = datetime(2026, 10, 10, 2, 0, 0, tzinfo=timezone.utc)         # 北京 10:00 周六 空闲
HOLIDAY_PEAK = datetime(2026, 10, 1, 2, 0, 0, tzinfo=timezone.utc)     # 北京 10:00 十一 假日
UNKNOWN_YEAR_PEAK = datetime(2027, 1, 7, 2, 0, 0, tzinfo=timezone.utc)  # 北京 10:00 未知年度
# BW-MANUAL-THREE-MODE-20261023：weak 档下「三个 Max 被夜间门挡、claude 不在高峰门」的
# 唯一白天时段——工作日离峰（北京周一 13:00）。用于验证 claude 在 weak 仍是可派候选。
OFFPEAK_WEEKDAY_1300 = datetime(2026, 10, 12, 5, 0, 0, tzinfo=timezone.utc)  # 北京 13:00 周一


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
        """占满 8 个非 claude 主力槽（codebuddy deepseek 1 + zcode 1（BW-ZCODE-RESTORE-
        1-20261022 恢复、容量严格 1）+ qoder Max 1 + cn Max 1 + cn 自定义 Max 2 +
        两 OpenCode 免费槽各 1，BW-OPENCODE-20261019），使 AUTO 在 claude 仍空闲时按
        现行层序（三 Max(1) > zcode(2) > claude(3) > CB deepseek(4) > 两 OpenCode(5)）
        落到 claude。"""
        self.assertTrue(self._reserve('cb', 'codebuddy', 'deepseek-v4.1-flash',
                                      'codebuddy', now).get('allowed'))
        self.assertTrue(self._reserve('z0', 'zcode', 'GLM-5.3', 'zcode', now).get('allowed'))
        self.assertTrue(self._reserve('qm', 'qoder', 'Qwen3.8-Max', 'qoder', now).get('allowed'))
        self.assertTrue(self._reserve('cnm', 'qodercn', 'Qwen3.8-Max', 'qodercn', now).get('allowed'))
        with mock.patch.object(dp.dm, 'USER_DISABLED_POOL_KEYS', frozenset()):
            for i in range(2):
                self.assertTrue(self._reserve(f'cnx{i}', 'qodercn', 'Qwen-3.8-Max', 'qodercn',
                                              now).get('allowed'))
        for i, model in enumerate((dp.OPENCODE_DEEPSEEK_MODEL, dp.OPENCODE_GLM_MODEL)):
            self.assertTrue(self._reserve(f'oc{i}', 'opencode', model, 'opencode',
                                          now).get('allowed'))


class CapacityAndRouting(_ClaudeBase):
    def test_claude_is_known_main_force_with_capacity_two(self):
        self.assertTrue(dp.is_known_pool(CLAUDE))
        self.assertEqual(dp.capacity_for(CLAUDE), 2)
        self.assertEqual(dp.CLAUDE_CAPACITY, 2)
        self.assertEqual(dp.CLAUDE_CAPACITY,
                         dp.MAIN_FORCE_CAPACITY[dp.CLAUDE_POOL_KEY])
        self.assertIn(CLAUDE, dp.MAIN_FORCE_KEYS)

    def test_physical_domestic_total_is_sixteen(self):
        # BW-STRONG-SONUS-20261024 当前权威总数：12 主力（含两 CodeBuddy 各独立 1 槽、
        # 两 OpenCode 免费模型各独立 1 槽、ZCode 恢复的严格 1 槽与 strong 专属 Sonus
        # 1 槽）+ 4 Flash = 16；weak 可新派仍为 15（Sonus 弱档不可新派）。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 16)
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY,
                         sum(dp.MAIN_FORCE_CAPACITY.values())
                         + sum(dp.OVERFLOW_CAPACITY.values()))

    def test_explicit_claude_claim(self):
        r = self.claim_claude('c1')
        self.assertTrue(r.get('allowed'), r)
        self.assertEqual(r['pool_key'], CLAUDE)

    def test_two_concurrent_claude_slots_then_third_is_full(self):
        r1 = self.claim_claude('a')
        r2 = self.claim_claude('b')   # 容量 2 → 第 2 个并发 claude 仍可认领
        r3 = self.claim_claude('c')   # 第 3 个满，绝不回退别模型
        self.assertTrue(r1.get('allowed'), r1)
        self.assertTrue(r2.get('allowed'), r2)
        self.assertEqual(r1['pool_key'], CLAUDE)
        self.assertEqual(r2['pool_key'], CLAUDE)
        self.assertFalse(r3.get('allowed'), r3)
        self.assertEqual(r3['reason'], 'capacity_full')
        self.assertIn('capacity', r3['reasons'][0].lower())
        self.assertEqual(dp.status(self.store, now=OFFPEAK_NIGHT)['pools'][CLAUDE]['active'], 2)

    def test_same_workspace_second_claude_still_refused(self):
        ws = self.ws('shared')
        r1 = dp.select_and_claim(self.store, task_id='w1', runtime='claude',
                                 model='deepseek-flash', workspace=ws,
                                 prompt_sha256='p' + '0' * 63, stage='s',
                                 now=OFFPEAK_NIGHT, executor='claude', _preclaim=False)
        r2 = dp.select_and_claim(self.store, task_id='w2', runtime='claude',
                                 model='deepseek-flash', workspace=ws,
                                 prompt_sha256='p' + '0' * 63, stage='s',
                                 now=OFFPEAK_NIGHT, executor='claude', _preclaim=False)
        self.assertTrue(r1.get('allowed'), r1)
        self.assertFalse(r2.get('allowed'), r2)
        self.assertEqual(r2['reason'], 'workspace_in_flight', r2)

    def test_terminal_release_allows_claude_reuse(self):
        r1 = self.claim_claude('a')
        r2 = self.claim_claude('b')
        self.assertTrue(r1.get('allowed'), r1)
        self.assertTrue(r2.get('allowed'), r2)
        rel = dp.finish(self.store, r1['token'], terminal='cancelled',
                        now=OFFPEAK_NIGHT)
        self.assertTrue(rel['released'], rel)
        r3 = self.claim_claude('c')
        self.assertTrue(r3.get('allowed'), r3)
        self.assertEqual(dp.status(self.store, now=OFFPEAK_NIGHT)['pools'][CLAUDE]['active'], 2)

    def test_reserved_running_unknown_all_occupy_claude_capacity(self):
        r1 = self._reserve('r1', 'claude', 'deepseek-flash', 'claude', OFFPEAK_NIGHT)
        r2 = self._reserve('r2', 'claude', 'deepseek-flash', 'claude', OFFPEAK_NIGHT)
        self.assertTrue(r1.get('allowed'), r1)
        self.assertTrue(r2.get('allowed'), r2)
        # reserved 两条已占满 → 第 3 条拒绝。
        r3 = self.claim_claude('r3')
        self.assertFalse(r3.get('allowed'), r3)
        self.assertEqual(r3['reason'], 'capacity_full', r3)
        # 把两条真实占位分别推进到 running / unknown：仍占位，绝不因状态变化被漏算。
        with closing(dp.connect(self.store)) as conn:
            conn.execute("UPDATE attempts SET state='running' WHERE token=?",
                         (r1['token'],))
            conn.execute("UPDATE attempts SET state='unknown' WHERE token=?",
                         (r2['token'],))
        st = dp.status(self.store, now=OFFPEAK_NIGHT)
        self.assertEqual(st['pools'][CLAUDE]['active'], 2, st['pools'][CLAUDE])
        r4 = self.claim_claude('r4')
        self.assertFalse(r4.get('allowed'), r4)
        self.assertEqual(r4['reason'], 'capacity_full', r4)

    def test_auto_selects_claude_when_other_mains_full(self):
        # 另一主力的 AUTO 请求（未点名 claude）：其余 9 个主力槽（含 CodeBuddy 与两
        # OpenCode）占满、
        # claude 空闲 → 选中 claude
        self.occupy_non_claude_mains()
        r = dp.select_and_claim(self.store, task_id='auto', runtime='qoder',
                                model='Qwen3.8-Max', workspace=self.ws('auto'),
                                prompt_sha256='p', stage='s', now=OFFPEAK_NIGHT,
                                executor='auto', _preclaim=False)
        self.assertTrue(r.get('routing_required'), r)
        self.assertEqual(r['selected']['pool_key'], CLAUDE)

    def test_claude_inflight_counted_in_domestic_aggregate(self):
        self.claim_claude('c1')
        self.claim_claude('c2')
        st = dp.status(self.store, now=OFFPEAK_NIGHT)
        self.assertEqual(st['domestic']['active'], 2)   # claude 在途计入国内总数
        self.assertEqual(st['pools'][CLAUDE]['active'], 2)

    def test_claude_does_not_pollute_committed_qoder(self):
        self.claim_claude('c1')
        st = dp.status(self.store, now=OFFPEAK_NIGHT)
        self.assertEqual(st['rotation']['committed_qoder'], 0)
        self.assertEqual(st['rotation']['committed_zcode'], 0)

    def test_auto_claude_entry_does_not_bypass_fair_selection(self):
        # BW-CLAUDE-DEEPSEEK-S4 反例：AUTO 的 claude 入口请求绝不绕过公平选择直接认领
        # 自己那槽——先占住最高层 CodeBuddy（新最高层由独立回归验证），claude 请求按层序
        # routing 到 Max 层；BW-CUSTOM-MAX-RESUME-20261018-S1 起 Max 层内自定义优先。
        self.assertTrue(self._reserve('cb', 'codebuddy', 'deepseek-v4.1-flash',
                                      'codebuddy').get('allowed'))
        r = dp.select_and_claim(self.store, task_id='afc', runtime='claude',
                                model='deepseek-flash', workspace=self.ws('afc'),
                                prompt_sha256='p' + '0' * 63, stage='s',
                                now=OFFPEAK_NIGHT, executor='auto', _preclaim=False)
        self.assertFalse(r.get('allowed'), r)
        self.assertTrue(r.get('routing_required'), r)
        self.assertEqual(r['selected']['pool_key'], dp.CN_MAX_POOL_KEY, r)

    def test_auto_claude_entry_peak_now_claims_in_strong_mode(self):
        # BW-MANUAL-THREE-MODE-20261023：**strong 档**解除 claude 工作日高峰门——AUTO 的
        # claude 入口在高峰绝不返回 `main_force_window_closed`。strong 常态层序 Sonus(1) >
        # ZCode(2) > claude(3) > 三个 Max(4)，占满 ZCode 后 claude 即最优先可用候选。
        # BW-SONUS-TEMP-DISABLE-20261028-R2：Sonus 全档暂时禁用——strong 显式 reserve
        # 如实 mode_excluded（零占位），占满 ZCode 后 claude 即第一候选，断言强度不变。
        with dispatch_mode('strong'):
            refused = self._reserve('hi0', 'qoder', 'Sonus', 'qoder',
                                    PEAK_WEEKDAY)
            self.assertFalse(refused.get('allowed'), refused)
            self.assertEqual(refused.get('reason'), 'mode_excluded', refused)
            self.assertTrue(self._reserve('hi1', 'zcode', 'GLM-5.3', 'zcode',
                                          PEAK_WEEKDAY).get('allowed'))
            r = self.claim_claude('apc', now=PEAK_WEEKDAY, executor='auto')
            self.assertTrue(r.get('allowed'), r)
            self.assertEqual(r['pool_key'], CLAUDE, r)

    def test_auto_claude_entry_peak_refused_in_weak_mode(self):
        # weak（默认）档恢复既有工作日高峰门：高峰 AUTO 的 claude 入口如实
        # `main_force_window_closed`（sent=false、零占位），离峰则正常可派。
        self.assertTrue(self._reserve('hw1', 'zcode', 'GLM-5.3', 'zcode',
                                      PEAK_WEEKDAY).get('allowed'))
        r = self.claim_claude('apw', now=PEAK_WEEKDAY, executor='auto')
        self.assertFalse(r.get('allowed'), r)
        self.assertEqual(r['reason'], 'main_force_window_closed', r)
        self.assertFalse(r.get('sent', True), r)
        r2 = self.claim_claude('apw2', now=OFFPEAK_WEEKDAY_1300, executor='auto')
        self.assertTrue(r2.get('allowed'), r2)
        self.assertEqual(r2['pool_key'], CLAUDE, r2)

    def test_total_guard_blocks_claude_with_migration_debt(self):
        # BW-CODEBUDDY-MAIN-S3 + BW-WORKER-WIDGET-20261018-S3 + BW-OPENCODE-20261019：
        # 真实国内在途已达物理总 16（8 个非 claude 主力含 CodeBuddy deepseek、恢复的
        # ZCode 1 槽 + GLM 独立 1 槽在途 + 4 个 Flash + 1 个 claude 在途 + 1 条 retired
        # 池超额迁移在途 + 1 条 strong 遗留 Sonus），claude 自身按容量看似仍空（1/2），
        # 但总守卫对 claude 同样生效，绝不派第 17 个。显式点名也如实拒绝。
        self.occupy_non_claude_mains()
        for i in range(2):
            self.assertTrue(self._reserve(f'of{i}', 'qoder', 'Qwen3.8-Flash',
                                          'qoder').get('allowed'))
            self.assertTrue(self._reserve(f'cf{i}', 'qodercn', 'Qwen3.8-Flash',
                                          'qodercn').get('allowed'))
        self.assertTrue(self.claim_claude('pre').get('allowed'))
        with closing(dp.connect(self.store)) as conn:
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                         "state, reserved_at_utc) VALUES(?,?,?,?,?,?,?)",
                         ('glm-inflight', 'gi', 'codebuddy', 'glm-5.3-flash',
                          'codebuddy:glm-5.3-flash', 'running',
                          dp._iso(OFFPEAK_NIGHT)))
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                         "state, reserved_at_utc) VALUES(?,?,?,?,?,?,?)",
                         ('legacy-mig', 'lm', 'qodercn', 'DeepSeek-Flash',
                          'qodercn:DeepSeek-Flash', 'running',
                          dp._iso(OFFPEAK_NIGHT)))
            conn.execute('INSERT INTO attempts(token, task_id, runtime, model, pool_key, '
                         "state, reserved_at_utc) VALUES(?,?,?,?,?,?,?)",
                         ('sonus-inflight', 'si', 'qoder', 'Sonus',
                          dp.SONUS_POOL_KEY, 'running',
                          dp._iso(OFFPEAK_NIGHT)))
        st = dp.status(self.store, now=OFFPEAK_NIGHT)
        self.assertEqual(st['domestic']['active'], 16, st['domestic'])
        self.assertEqual(st['pools'][CLAUDE]['active'], 1, st['pools'][CLAUDE])
        r = self.claim_claude('late', executor='claude')
        self.assertFalse(r.get('allowed'), r)
        self.assertEqual(r['reason'], 'capacity_full', r)
        self.assertTrue(r.get('domestic_full'), r)


class WindowGate(_ClaudeBase):
    """BW-MANUAL-THREE-MODE-20261023（用户明确指令，当前权威）：claude 的时段门**按档**。

    - strong：解除全部派发时段限制（工作日高峰、周末、假日、未知年度都可新派）；
    - medium/weak（默认 weak）：恢复既有工作日高峰门（北京工作日 09:00–12:00、
      14:00–18:00 关闭；周末/法定假日/离峰放行；未知年度的工作日高峰保守关闭）。
    解除/恢复的只是时段门——1800 秒真实限额短窗、容量 2、总守卫、工作区单写全部照旧。"""

    def test_peak_weekday_blocks_only_outside_strong(self):
        for now in (PEAK_WEEKDAY, PEAK_WEEKDAY_1500):
            with self.subTest(now=now):
                with dispatch_mode('strong'):
                    r = self.claim_claude(f'c-{now.hour}', now=now)
                    self.assertTrue(r.get('allowed'), r)
                    self.assertEqual(r['pool_key'], CLAUDE, r)
                    dp.finish(self.store, r['token'], terminal='cancelled', now=now)
                # weak（默认）与 medium：同一高峰时刻如实拒绝，零占位。
                for mode in ('weak', 'medium'):
                    with dispatch_mode(mode):
                        rw = self.claim_claude(f'w-{mode}-{now.hour}', now=now)
                        self.assertFalse(rw.get('allowed'), rw)
                        self.assertEqual(rw['reason'], 'main_force_window_closed', rw)
                        self.assertFalse(rw.get('sent', True), rw)

    def test_weekend_and_holiday_allow_in_every_mode(self):
        # 周末与 2026 法定假日：三档都放行（strong 本无门；weak/medium 的高峰门只锁工作日高峰）。
        for i, now in enumerate((WEEKEND, HOLIDAY_PEAK, OFFPEAK_NIGHT)):
            for mode in ('strong', 'medium', 'weak'):
                with self.subTest(now=now, mode=mode), dispatch_mode(mode):
                    r = self.claim_claude(f'c{i}-{mode}', now=now)
                    self.assertTrue(r.get('allowed'), r)
                    dp.finish(self.store, r['token'], terminal='cancelled', now=now)

    def test_unknown_year_peak_is_conservatively_closed_outside_strong(self):
        # 未知年度的工作日高峰：offpeak 口径保守关闭（无法确认不是法定假日），故 weak/medium
        # 拒绝；strong 无时段门则放行。绝不臆测未知年度为假日。
        with dispatch_mode('strong'):
            r = self.claim_claude('u-strong', now=UNKNOWN_YEAR_PEAK)
            self.assertTrue(r.get('allowed'), r)
            dp.finish(self.store, r['token'], terminal='cancelled', now=UNKNOWN_YEAR_PEAK)
        for mode in ('medium', 'weak'):
            with dispatch_mode(mode):
                rw = self.claim_claude(f'u-{mode}', now=UNKNOWN_YEAR_PEAK)
                self.assertFalse(rw.get('allowed'), rw)
                self.assertEqual(rw['reason'], 'main_force_window_closed', rw)

    def test_reserve_then_consume_across_peak_keeps_token_in_strong(self):
        # strong：预留发生在 offhours、真正启动跨到工作日高峰 → 无时段门，consume 正常放行，
        # 绝不误释放本 token（容量 2 中已占 1）。
        with dispatch_mode('strong'):
            tok = self._reserve('c1', 'claude', 'deepseek-flash', 'claude',
                                OFFPEAK_NIGHT)['token']
            c = dp.consume_for_entry(self.store, task_id='c1', runtime='claude',
                                     model='deepseek-flash', workspace=self.ws('c1'),
                                     prompt_sha256='p' + '0' * 63, stage='s',
                                     claim_token=tok, now=PEAK_WEEKDAY, executor='claude')
            self.assertTrue(c['allowed'], c)
            self.assertEqual(c['pool_key'], CLAUDE, c)
            self.assertEqual(dp.status(self.store, now=PEAK_WEEKDAY)['pools'][CLAUDE]['active'],
                             1)

    def test_reserve_then_consume_across_peak_releases_in_weak(self):
        # weak（默认）：预留发生在 offhours、启动已跨到工作日高峰 → 按恢复后的高峰门把本
        # reserved 真实释放为 start_failed（绝不泄漏、绝不抢占别人的名额），不冒充别的原因。
        tok = self._reserve('c1w', 'claude', 'deepseek-flash', 'claude',
                            OFFPEAK_NIGHT)['token']
        c = dp.consume_for_entry(self.store, task_id='c1w', runtime='claude',
                                 model='deepseek-flash', workspace=self.ws('c1w'),
                                 prompt_sha256='p' + '0' * 63, stage='s',
                                 claim_token=tok, now=PEAK_WEEKDAY, executor='claude')
        self.assertFalse(c['allowed'], c)
        self.assertEqual(c['reason'], 'main_force_window_closed', c)
        self.assertTrue(c['capacity_released'], c)
        self.assertEqual(dp.status(self.store, now=PEAK_WEEKDAY)['pools'][CLAUDE]['active'], 0)

    def test_quota_short_window_still_blocks_in_peak(self):
        # "无时段门"不等于无额度限制：strong 档高峰若仍处于 1800 秒真实限额短窗内，必须照常拒绝。
        with dispatch_mode('strong'):
            tok = self._reserve('q1', 'claude', 'deepseek-flash', 'claude',
                                PEAK_WEEKDAY)['token']
            ev_path = self.base / 'peak-stdout.json'
            ev_path.write_text('{"result_errors":[{"status":429}]}\n',
                               encoding='utf-8', newline='\n')
            ev_sha = hashlib.sha256(ev_path.read_bytes()).hexdigest()
            rec = dp.record_main_force_limit(self.store, CLAUDE, task_id='q1',
                                             attempt_token=tok, evidence_path=str(ev_path),
                                             evidence_sha256=ev_sha, now=PEAK_WEEKDAY)
            self.assertTrue(rec['recorded'], rec)
            r = self.claim_claude('q2', now=PEAK_WEEKDAY)
            self.assertFalse(r.get('allowed'), r)
            self.assertEqual(r['reason'], 'main_force_limited', r)


class QuotaLimitMarker(_ClaudeBase):
    def _evidence(self):
        p = self.base / 'stdout.json'
        p.write_text('{"result_errors":[{"status":429}]}\n', encoding='utf-8', newline='\n')
        return str(p), hashlib.sha256(p.read_bytes()).hexdigest()

    def test_claude_can_carry_and_release_limit_marker(self):
        # BW-LIMIT30-20261019-S3：Claude/deepseek-flash 的真实 429/402 落标后 1800 秒内
        # 新派发（含 consume 已预留 token）一律拒绝；恰好 1800 秒及更久按现有优先级重新
        # 可选。原标记/证据不清除，也不需要手动重置。
        tok2 = self._reserve('c2', 'claude', 'deepseek-flash', 'claude', OFFPEAK_NIGHT)['token']
        ev_path, ev_sha = self._evidence()
        rec = dp.record_main_force_limit(self.store, CLAUDE, task_id='c2', attempt_token=tok2,
                                         evidence_path=ev_path, evidence_sha256=ev_sha,
                                         now=OFFPEAK_NIGHT)
        self.assertTrue(rec['recorded'], rec)
        self.assertTrue(dp.main_force_limited(self.store, CLAUDE, now=OFFPEAK_NIGHT))
        c = dp.consume_for_entry(self.store, task_id='c2', runtime='claude',
                                 model='deepseek-flash', workspace=self.ws('c2'),
                                 prompt_sha256='p' + '0' * 63, stage='s', claim_token=tok2,
                                 now=OFFPEAK_NIGHT, executor='claude')
        self.assertFalse(c['allowed'], c)
        self.assertEqual(c['reason'], 'main_force_limited')
        self.assertTrue(c['capacity_released'], c)
        self.assertEqual(dp.status(self.store, now=OFFPEAK_NIGHT)['pools'][CLAUDE]['active'], 0)
        # 恰好 1800 秒：窗口结束，按现有优先级重新可选；原始标记仍保留。
        tok3 = self._reserve('c3', 'claude', 'deepseek-flash', 'claude',
                             OFFPEAK_NIGHT + timedelta(seconds=1800))['token']
        c2 = dp.consume_for_entry(self.store, task_id='c3', runtime='claude',
                                  model='deepseek-flash', workspace=self.ws('c3'),
                                  prompt_sha256='p' + '0' * 63, stage='s', claim_token=tok3,
                                  now=OFFPEAK_NIGHT + timedelta(seconds=1800),
                                  executor='claude')
        self.assertTrue(c2['allowed'], c2)
        self.assertTrue(dp.main_force_limited(self.store, CLAUDE, now=OFFPEAK_NIGHT))
        rel = dp.release_main_force_limit(self.store, CLAUDE, note='reset', now=OFFPEAK_NIGHT)
        self.assertTrue(rel['released'], rel)
        self.assertFalse(dp.main_force_limited(self.store, CLAUDE, now=OFFPEAK_NIGHT))


class WindowGateCaliber(unittest.TestCase):
    """BW-MANUAL-THREE-MODE-20261023：claude 是否可派**按档**决定。

    - strong：不套任何时段门（`window_blocks_for_mode` 恒 False，绝不读 offpeak）；
    - medium/weak：恢复既有工作日高峰门，与 deepseek_offpeak 的纯时间判定完全一致
      （工作日 09:00–12:00 / 14:00–18:00 关闭；周末/假日/离峰放行；未知年度工作日高峰
      保守关闭）。该模块只做纯时间判定，不起进程、不联网。"""

    def test_strong_ignores_offpeak_and_weak_mirrors_it(self):
        for when in (PEAK_WEEKDAY, PEAK_WEEKDAY_1500, WEEKEND, HOLIDAY_PEAK,
                     UNKNOWN_YEAR_PEAK, OFFPEAK_NIGHT):
            with self.subTest(when=when):
                # strong：任何注入时间都不挡。
                with dispatch_mode('strong'):
                    self.assertFalse(dp._max_window_blocks(CLAUDE, when))
                # medium/weak：与 offpeak 判定严格互为镜像（允许 ⇔ 不被挡）。
                expected_blocked = not off.can_dispatch(when)
                for mode in ('medium', 'weak'):
                    with dispatch_mode(mode):
                        self.assertEqual(dp._max_window_blocks(CLAUDE, when),
                                         expected_blocked, mode)
        # offpeak 纯函数口径未变：工作日高峰关闭、离峰放行。
        self.assertFalse(off.can_dispatch(PEAK_WEEKDAY))
        self.assertTrue(off.can_dispatch(OFFPEAK_NIGHT))
        # dispatch_pool 模块级不持有 offpeak 引用：判定统一经 dispatch_mode 的惰性导入。
        self.assertFalse(hasattr(dp, 'offpeak'))


if __name__ == '__main__':
    unittest.main()
