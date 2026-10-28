"""dispatch_mode 直接离线回归（BW-MANUAL-THREE-MODE-CLOSEOUT-20261023）。

只测 scripts/dispatch_mode.py 的策略事实与状态文件读写：默认 weak、缺文件/畸形 JSON/
非对象/非法值/不可读路径的安全回落、三档允许集/优先级表/时段门、Claude 高峰门依赖
加载/运行异常时 **fail-closed**（当作窗口关闭，绝不 fail-open），以及必查项「一次
reserve / select-and-claim / consume / claim-due 裁决内档位只读一次、切档不令同一事务
前后不一致」的确认。

绝不读写真实用户档位文件（tests/conftest.py 已把默认路径指到临时目录内不存在的文件；
本文件另用显式临时路径/临时 store），绝不 Popen 真实 CLI、绝不联网、绝不碰凭据。
"""
import os
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'scripts'
for p in (str(SCRIPTS), str(REPO / 'tests')):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_mode as dm  # noqa: E402
import dispatch_pool as dp  # noqa: E402
from dispatch_mode_support import dispatch_mode  # noqa: E402

T0 = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)    # 北京 23:00 周四（夜窗内 + claude 离峰）
PEAK_AM = datetime(2026, 10, 8, 2, 0, 0, tzinfo=timezone.utc)   # 北京 10:00 周四（claude 高峰）
PEAK_PM = datetime(2026, 10, 8, 7, 0, 0, tzinfo=timezone.utc)   # 北京 15:00 周四（claude 高峰）
LUNCH = datetime(2026, 10, 8, 4, 30, 0, tzinfo=timezone.utc)    # 北京 12:30 周四（claude 离峰）
WEEKEND = datetime(2026, 10, 10, 2, 0, 0, tzinfo=timezone.utc)  # 北京 10:00 周六（claude 全天开）
HOLIDAY = datetime(2026, 10, 1, 3, 0, 0, tzinfo=timezone.utc)   # 北京 11:00（法定假日，claude 开）
IN_2200 = datetime(2026, 10, 8, 14, 0, 0, tzinfo=timezone.utc)    # 北京 22:00（夜窗含）
OUT_2159 = datetime(2026, 10, 8, 13, 59, 0, tzinfo=timezone.utc)  # 北京 21:59（夜窗外）
IN_0759 = datetime(2026, 10, 8, 23, 59, 0, tzinfo=timezone.utc)   # 北京 次日 07:59（夜窗内）
OUT_0800 = datetime(2026, 10, 9, 0, 0, 0, tzinfo=timezone.utc)    # 北京 08:00（夜窗不含）

FIVE_POOLS = {'zcode:GLM-5.3', 'claude:deepseek-flash', 'qoder:Qwen3.8-Max',
              'qodercn:Qwen3.8-Max', 'qodercn:Qwen-3.8-Max'}
# BW-STRONG-SONUS-20261024：qoder:Sonus（国际 Qoder CLI --list-models 核验字面名）是
# strong 专属独立 1 槽；strong 可派集合 = 五池 + Sonus，medium/weak 仍各为五池/11 池。
SONUS_KEY = 'qoder:Sonus'
SIX_POOLS = FIVE_POOLS | {SONUS_KEY}
ELEVEN_POOLS = FIVE_POOLS | {
    'codebuddy:deepseek-v4.1-flash', 'opencode:deepseek-v4.1-flash-free',
    'opencode:glm-5.3-flash-free', 'codebuddy:glm-5.3-flash',
    'qoder:Qwen3.8-Flash', 'qodercn:Qwen3.8-Flash'}


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-dm-state-')
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_missing_file_falls_back_to_default_weak(self):
        missing = self.base / 'missing.json'
        record = dm.load_mode(missing)
        self.assertEqual(record['mode'], 'weak')
        self.assertEqual(record['source'], 'default')
        self.assertIsNone(record['error'])
        self.assertIsNone(record['raw_mode'])
        self.assertEqual(record['default_mode'], 'weak')
        self.assertFalse(missing.exists())  # 永不因读而建文件
        self.assertEqual(dm.current_mode(missing), 'weak')

    def test_corrupt_json_falls_back_to_weak(self):
        p = self.base / 'corrupt.json'
        p.write_text('{not json', encoding='utf-8')
        record = dm.load_mode(p)
        self.assertEqual(record['mode'], 'weak')
        self.assertEqual(record['source'], 'error_fallback')
        self.assertEqual(record['error'], 'malformed_json')

    def test_non_object_json_falls_back_to_weak(self):
        p = self.base / 'array.json'
        p.write_text('[1, 2]', encoding='utf-8')
        record = dm.load_mode(p)
        self.assertEqual(record['mode'], 'weak')
        self.assertEqual(record['error'], 'not_an_object')

    def test_invalid_mode_value_falls_back_to_weak(self):
        p = self.base / 'ultra.json'
        p.write_text('{"mode": "ultra"}', encoding='utf-8')
        record = dm.load_mode(p)
        self.assertEqual(record['mode'], 'weak')
        self.assertEqual(record['source'], 'error_fallback')
        self.assertEqual(record['error'], 'invalid_mode')
        self.assertEqual(record['raw_mode'], 'ultra')

    def test_non_string_mode_falls_back_to_weak(self):
        p = self.base / 'number.json'
        p.write_text('{"mode": 3}', encoding='utf-8')
        record = dm.load_mode(p)
        self.assertEqual(record['mode'], 'weak')
        self.assertEqual(record['error'], 'invalid_mode')
        self.assertIsNone(record['raw_mode'])

    def test_unreadable_path_falls_back_to_weak(self):
        directory = self.base / 'unreadable'
        directory.mkdir()
        record = dm.load_mode(directory)
        self.assertEqual(record['mode'], 'weak')
        self.assertEqual(record['source'], 'error_fallback')
        self.assertEqual(record['error'], 'unreadable')

    def test_valid_file_loads_with_normalization(self):
        p = self.base / 'ok.json'
        p.write_text('{"mode": "  Strong  "}', encoding='utf-8')
        record = dm.load_mode(p)
        self.assertEqual(record['mode'], 'strong')
        self.assertEqual(record['source'], 'file')
        self.assertIsNone(record['error'])

    def test_save_rejects_invalid_mode_without_touching_path(self):
        for bad in ('ultra', '', None, 3):
            with self.assertRaises(ValueError, msg=repr(bad)):
                dm.save_mode(bad, self.base / 'never.json')
        self.assertEqual(list(self.base.iterdir()), [])

    def test_save_is_atomic_and_loads_back(self):
        p = self.base / 'dispatch-mode.json'
        record = dm.save_mode('medium', p)
        self.assertEqual(record['mode'], 'medium')
        self.assertEqual(record['source'], 'file')
        loaded = dm.load_mode(p)
        self.assertEqual(loaded['mode'], 'medium')
        # 原子写：同目录绝不残留 .dispatch-mode-*.tmp 半成品。
        self.assertEqual([x.name for x in self.base.iterdir()],
                         ['dispatch-mode.json'])

    def test_current_mode_rereads_file_every_call(self):
        # conftest 已把默认 env 指向不存在的文件（→ weak）；本用例经 env 覆盖到真实
        # 临时文件，验证 current_mode 每次现读、绝不 import 时缓存。
        with dispatch_mode('strong') as path:
            self.assertEqual(dm.current_mode(), 'strong')
            dm.save_mode('medium', path)
            self.assertEqual(dm.current_mode(), 'medium')
            dm.save_mode('weak', path)
            self.assertEqual(dm.current_mode(), 'weak')
        self.assertEqual(dm.current_mode(), 'weak')  # env 恢复后回到缺文件默认


class ModePolicyTests(unittest.TestCase):
    def test_mode_constants(self):
        self.assertEqual(dm.MODES, ('strong', 'medium', 'weak'))
        self.assertEqual(dm.DEFAULT_MODE, 'weak')

    def test_normalize_mode(self):
        self.assertEqual(dm.normalize_mode('Strong'), 'strong')
        self.assertEqual(dm.normalize_mode(' MEDIUM '), 'medium')
        self.assertEqual(dm.normalize_mode('weak'), 'weak')
        for bad in ('ultra', '', '  ', None, 3, b'weak'):
            self.assertEqual(dm.normalize_mode(bad), 'weak', repr(bad))

    def test_allowed_sets_per_mode(self):
        # BW-SONUS-TEMP-DISABLE-20261028-R2：Sonus 全档暂时禁用——strong 新派也是
        # 四池（五窄池去 CN 自定义 Max）；STRONG_POOL_KEYS 常量表仍保留 Sonus 历史。
        self.assertEqual(set(dm.allowed_pools_for_mode('strong')),
                         FIVE_POOLS - {dm.QMAX_CN_CUSTOM_POOL_KEY})
        self.assertEqual(set(dm.allowed_pools_for_mode('medium')),
                         FIVE_POOLS - {dm.QMAX_CN_CUSTOM_POOL_KEY})
        self.assertEqual(set(dm.allowed_pools_for_mode('weak')),
                         ELEVEN_POOLS - {dm.QMAX_CN_CUSTOM_POOL_KEY})
        for m in ('strong', 'medium', 'weak'):
            self.assertNotIn(SONUS_KEY, dm.allowed_pools_for_mode(m), m)
        # 非法档位按默认 weak 口径给全表；缺省读当前档（conftest 隔离下 weak）。
        self.assertEqual(set(dm.allowed_pools_for_mode('ultra')),
                         ELEVEN_POOLS - {dm.QMAX_CN_CUSTOM_POOL_KEY})
        self.assertEqual(set(dm.allowed_pools_for_mode()),
                         ELEVEN_POOLS - {dm.QMAX_CN_CUSTOM_POOL_KEY})

    def test_strong_priority_table(self):
        # BW-STRONG-SONUS-20261024：Sonus=1 全档最前，ZCode=2、Claude=3、三个 Max=4。
        self.assertEqual(dm.priority_map_for_mode('strong'), {
            SONUS_KEY: 1,
            'zcode:GLM-5.3': 2,
            'claude:deepseek-flash': 3,
            'qoder:Qwen3.8-Max': 4,
            'qodercn:Qwen3.8-Max': 4,
            'qodercn:Qwen-3.8-Max': 4,
        })
        # medium/weak 层表完全不含 Sonus（档位排除，绝不给层号）。
        self.assertNotIn(SONUS_KEY, dm.priority_map_for_mode('medium'))
        self.assertNotIn(SONUS_KEY, dm.priority_map_for_mode('weak'))
        self.assertIsNone(dm.priority_for_pool(SONUS_KEY, 'medium'))
        self.assertIsNone(dm.priority_for_pool(SONUS_KEY, 'weak'))
        self.assertEqual(dm.priority_for_pool(SONUS_KEY, 'strong'), 1)

    def test_medium_priority_table(self):
        self.assertEqual(dm.priority_map_for_mode('medium'), {
            'qodercn:Qwen-3.8-Max': 1,
            'qodercn:Qwen3.8-Max': 1,
            'qoder:Qwen3.8-Max': 1,
            'claude:deepseek-flash': 2,
            'zcode:GLM-5.3': 3,
        })

    def test_weak_priority_table(self):
        self.assertEqual(dm.priority_map_for_mode('weak'), {
            'qodercn:Qwen-3.8-Max': 1,
            'qodercn:Qwen3.8-Max': 1,
            'zcode:GLM-5.3': 2,
            'claude:deepseek-flash': 3,
            'qoder:Qwen3.8-Max': 4,
            'codebuddy:deepseek-v4.1-flash': 5,
            'opencode:deepseek-v4.1-flash-free': 6,
            'opencode:glm-5.3-flash-free': 6,
            'codebuddy:glm-5.3-flash': 7,
            'qoder:Qwen3.8-Flash': 8,
            'qodercn:Qwen3.8-Flash': 8,
        })

    def test_priority_maps_are_copies(self):
        first = dm.priority_map_for_mode('weak')
        first['zcode:GLM-5.3'] = 99
        self.assertEqual(dm.priority_map_for_mode('weak')['zcode:GLM-5.3'], 2)

    def test_pool_key_literals_match_dispatch_pool(self):
        # 键名漂移守卫：dispatch_mode 的独立字面量必须与 dispatch_pool 真实池表一致。
        # BW-STRONG-SONUS-20261024：MAIN_FORCE 含 strong 专属的 qoder:Sonus，但它不在
        # weak 全候选（ALL_POOL_KEYS 保持 11 池）——known = 全表 ∪ strong 专属池。
        self.assertEqual(set(dm.ALL_POOL_KEYS),
                         (set(dp.MAIN_FORCE_KEYS) | set(dp.OVERFLOW_KEYS))
                         - set(dm.STRONG_ONLY_POOL_KEYS))
        self.assertTrue(set(dm.ALL_POOL_KEYS) <= set(dp.CAPACITY))
        self.assertTrue(set(dm.STRONG_ONLY_POOL_KEYS) <= set(dp.CAPACITY))
        self.assertEqual(set(dm.KNOWN_POLICY_POOL_KEYS),
                         set(dp.MAIN_FORCE_KEYS) | set(dp.OVERFLOW_KEYS))
        self.assertEqual(set(dm.NARROW_POOL_KEYS), FIVE_POOLS)
        self.assertEqual(set(dm.STRONG_POOL_KEYS), SIX_POOLS)
        self.assertEqual(dm.STRONG_ONLY_POOL_KEYS, (SONUS_KEY,))
        self.assertEqual(dm.SONUS_POOL_KEY, dp.SONUS_POOL_KEY)
        self.assertEqual(dp.capacity_for(SONUS_KEY), 1)
        self.assertNotIn(SONUS_KEY, dp.LEGACY_RETIRED_POOL_KEYS)
        # Sonus 绝不吃任何 Qwen Max 限额标记/窗口池（不连坐 Max 夜间门/限额标记）。
        self.assertNotIn(SONUS_KEY, dp.QUOTA_LIMITABLE_POOLS)
        self.assertNotIn(SONUS_KEY, dp.QUOTA_LIMIT_WINDOW_POOLS)
        self.assertNotIn(SONUS_KEY, dm.MAX_POOL_KEYS)
        self.assertEqual(set(dm.MAX_POOL_KEYS),
                         {'qoder:Qwen3.8-Max', 'qodercn:Qwen3.8-Max',
                          'qodercn:Qwen-3.8-Max'})
        # 容量口径：全局物理总 16；weak 可新派容量仍为原 15（Sonus 不参与 weak）。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 16)
        weak_slots = sum(dp.capacity_for(k) for k in dm.ALL_POOL_KEYS)
        self.assertEqual(weak_slots, 15, weak_slots)

    def test_gated_pools_per_mode(self):
        self.assertEqual(tuple(dm.window_gated_pools_for_mode('strong')), ())
        for m in ('medium', 'weak'):
            self.assertEqual(set(dm.window_gated_pools_for_mode(m)),
                             {'qoder:Qwen3.8-Max', 'qodercn:Qwen3.8-Max',
                              'qodercn:Qwen-3.8-Max', 'claude:deepseek-flash'}, m)

    def test_night_window_boundaries(self):
        # 北京 22:00（含）开、21:59 关；08:00（不含）关、07:59 开。
        self.assertTrue(dm.night_window_open(IN_2200))
        self.assertFalse(dm.night_window_open(OUT_2159))
        self.assertTrue(dm.night_window_open(IN_0759))
        self.assertFalse(dm.night_window_open(OUT_0800))
        self.assertTrue(dm.night_window_open(T0))
        self.assertFalse(dm.night_window_open(PEAK_AM))

    def test_strong_mode_has_no_windows(self):
        # BW-STRONG-SONUS-20261024：strong 对 Sonus 同样不加任何时段门。
        for now in (PEAK_AM, PEAK_PM, LUNCH, T0, WEEKEND, OUT_0800):
            for pk in ELEVEN_POOLS | {SONUS_KEY}:
                self.assertFalse(dm.window_blocks_for_mode(pk, now, 'strong'),
                                 (pk, now))

    def test_medium_weak_windows(self):
        maxes = ('qoder:Qwen3.8-Max', 'qodercn:Qwen3.8-Max', 'qodercn:Qwen-3.8-Max')
        ungated = ('zcode:GLM-5.3', 'codebuddy:deepseek-v4.1-flash',
                   'opencode:deepseek-v4.1-flash-free',
                   'opencode:glm-5.3-flash-free', 'codebuddy:glm-5.3-flash',
                   'qoder:Qwen3.8-Flash', 'qodercn:Qwen3.8-Flash')
        for m in ('medium', 'weak'):
            # 三个 Max：夜间门——白天/周末关、北京 23:00 开。
            for pk in maxes:
                for now in (PEAK_AM, LUNCH, WEEKEND, OUT_0800):
                    self.assertTrue(dm.window_blocks_for_mode(pk, now, m),
                                   (pk, m, now))
                self.assertFalse(dm.window_blocks_for_mode(pk, T0, m), (pk, m))
            # claude：工作日高峰门——09:00-12:00 / 14:00-18:00 关，
            # 午休/夜间/周末/法定假日开。
            self.assertTrue(
                dm.window_blocks_for_mode(dm.CLAUDE_POOL_KEY, PEAK_AM, m), m)
            self.assertTrue(
                dm.window_blocks_for_mode(dm.CLAUDE_POOL_KEY, PEAK_PM, m), m)
            for now in (LUNCH, T0, WEEKEND, HOLIDAY):
                self.assertFalse(
                    dm.window_blocks_for_mode(dm.CLAUDE_POOL_KEY, now, m),
                    (m, now))
            # 其它池：任何时刻无门。
            for pk in ungated:
                for now in (PEAK_AM, T0, WEEKEND):
                    self.assertFalse(dm.window_blocks_for_mode(pk, now, m),
                                     (pk, m, now))


class ClaudeGateFailClosedTests(unittest.TestCase):
    """必查项 1：Claude 高峰门依赖（deepseek_offpeak）加载/运行异常时必须
    fail-closed（当作窗口关闭），绝不 fail-open。"""

    def test_dependency_import_failure_closes_window(self):
        with unittest.mock.patch.dict(sys.modules, {'deepseek_offpeak': None}):
            self.assertFalse(dm._claude_peak_open(T0))
            # 即便此刻本应离峰放行（北京 23:00），依赖坏了也一律当作窗口关闭。
            for m in ('weak', 'medium'):
                self.assertTrue(
                    dm.window_blocks_for_mode(dm.CLAUDE_POOL_KEY, T0, m), m)
                self.assertTrue(
                    dm.window_blocks_for_mode(dm.CLAUDE_POOL_KEY, LUNCH, m), m)
            # strong 档不调用该门：依赖故障绝不波及 strong 的「无门」口径。
            self.assertFalse(
                dm.window_blocks_for_mode(dm.CLAUDE_POOL_KEY, T0, 'strong'))

    def test_dependency_runtime_failure_closes_window(self):
        import deepseek_offpeak as offpeak
        with unittest.mock.patch.object(offpeak, 'can_dispatch',
                                        side_effect=RuntimeError('boom')):
            self.assertFalse(dm._claude_peak_open(T0))
            self.assertTrue(
                dm.window_blocks_for_mode(dm.CLAUDE_POOL_KEY, T0, 'weak'))
            self.assertTrue(
                dm.window_blocks_for_mode(dm.CLAUDE_POOL_KEY, PEAK_AM, 'weak'))

    def test_gate_delegates_to_offpeak_when_healthy(self):
        import deepseek_offpeak as offpeak
        with unittest.mock.patch.object(offpeak, 'can_dispatch',
                                        return_value=False):
            self.assertFalse(dm._claude_peak_open(T0))
            self.assertTrue(
                dm.window_blocks_for_mode(dm.CLAUDE_POOL_KEY, T0, 'weak'))
        with unittest.mock.patch.object(offpeak, 'can_dispatch',
                                        return_value=True):
            self.assertTrue(dm._claude_peak_open(PEAK_AM))
            self.assertFalse(
                dm.window_blocks_for_mode(dm.CLAUDE_POOL_KEY, PEAK_AM, 'weak'))
        # 真实纯时间判定：工作日高峰关、午休/夜间/周末/假日开。
        self.assertFalse(dm._claude_peak_open(PEAK_AM))
        self.assertFalse(dm._claude_peak_open(PEAK_PM))
        self.assertTrue(dm._claude_peak_open(LUNCH))
        self.assertTrue(dm._claude_peak_open(T0))
        self.assertTrue(dm._claude_peak_open(WEEKEND))
        self.assertTrue(dm._claude_peak_open(HOLIDAY))

    def test_gate_dependency_failure_does_not_leak_to_other_pools(self):
        # Max 的夜间门与其它池的「无门」都不依赖 deepseek_offpeak：
        # 依赖故障只关 claude 一个池。
        with unittest.mock.patch.dict(sys.modules, {'deepseek_offpeak': None}):
            self.assertTrue(
                dm.window_blocks_for_mode('qoder:Qwen3.8-Max', PEAK_AM, 'weak'))
            self.assertFalse(
                dm.window_blocks_for_mode('qoder:Qwen3.8-Max', T0, 'weak'))
            for pk in ('zcode:GLM-5.3', 'opencode:glm-5.3-flash-free',
                       'qodercn:Qwen3.8-Flash'):
                self.assertFalse(
                    dm.window_blocks_for_mode(pk, PEAK_AM, 'weak'), pk)


class _AdjudicationBase(unittest.TestCase):
    """单次裁决只读一次档位的共用脚手架：独立临时 store + 真实 workspace 目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-dm-single-')
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.store = str(self.base / 'dispatch.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        self.saved_env = os.environ.pop('BRAIN_WORKER_DISPATCH_STORE', None)
        if self.saved_env is not None:
            self.addCleanup(os.environ.__setitem__,
                            'BRAIN_WORKER_DISPATCH_STORE', self.saved_env)

    def _ws(self, task):
        d = self.base / 'ws' / str(task)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _counting_current_mode(self):
        """包装 dm.current_mode 计数（真实读文件，只记录调用次数与返回值）。"""
        original = dm.current_mode
        calls = []

        def counting():
            value = original()
            calls.append(value)
            return value

        return calls, counting

    def _reserve(self, task, runtime, model, executor, glm=False):
        r = dp.reserve(self.store, task_id=task, runtime=runtime, model=model,
                       workspace=str(self._ws(task)), prompt_sha256='p',
                       executor=executor, codebuddy_glm_allowed=glm,
                       now=T0, _preclaim=False)
        self.assertTrue(r.get('reserved') or r.get('allowed'), (task, r))
        return r


class SingleModeReadPerAdjudicationTests(_AdjudicationBase):
    """必查项 2：reserve / select-and-claim / consume / claim-due 一次事务内档位
    **只读一次**并全程线程传递——切档只影响下一次裁决，绝不令同一事务前后不一致。"""

    def test_reserve_reads_mode_exactly_once(self):
        calls, counting = self._counting_current_mode()
        with unittest.mock.patch.object(dm, 'current_mode', counting):
            r = self._reserve('r1', 'qodercn', 'Qwen3.8-Max', 'qodercn')
        self.assertTrue(r.get('reserved') or r.get('allowed'), r)
        self.assertEqual(calls, ['weak'], calls)

    def test_select_and_claim_reads_mode_exactly_once(self):
        calls, counting = self._counting_current_mode()
        with unittest.mock.patch.object(dm, 'current_mode', counting):
            r = dp.select_and_claim(self.store, task_id='s1', runtime='qoder',
                                    model='Qwen3.8-Max',
                                    workspace=str(self._ws('s1')),
                                    prompt_sha256='p', now=T0, _preclaim=False)
        # 夜间 weak 档 1:1 轮换首选 CN 自定义 Max：请求国际 Max 得到 routing verdict
        # ——无论 claim 还是 routing，整段裁决只用一次档位读。
        self.assertTrue(r.get('routing_required'), r)
        self.assertEqual(r['selected']['pool_key'], 'qodercn:Qwen3.8-Max', r)
        self.assertEqual(calls, ['weak'], calls)

    def test_consume_reads_mode_exactly_once(self):
        tok = self._reserve('c1', 'qodercn', 'Qwen3.8-Max', 'qodercn')['token']
        calls, counting = self._counting_current_mode()
        with unittest.mock.patch.object(dm, 'current_mode', counting):
            out = dp.consume_for_entry(self.store, task_id='c1',
                                       runtime='qodercn', model='Qwen3.8-Max',
                                       workspace=str(self._ws('c1')),
                                       prompt_sha256='p', claim_token=tok,
                                       wrapper_pid=4321, wrapper_created='w1',
                                       now=T0)
        self.assertTrue(out['allowed'], out)
        self.assertEqual(calls, ['weak'], calls)

    def test_claim_due_reads_mode_exactly_once(self):
        # Luna 状态机回归只在测试进程内显式解锁（与 test_dispatch_pool.py 同口径），
        # 用后恢复真值；生产常量仍固定禁用。
        saved_disabled = dp.LUNA_DISABLED
        had_cap = dp.LUNA_KEY in dp.CAPACITY
        saved_cap = dp.CAPACITY.get(dp.LUNA_KEY)
        dp.LUNA_DISABLED = False
        dp.CAPACITY[dp.LUNA_KEY] = None

        def _restore_luna():
            dp.LUNA_DISABLED = saved_disabled
            if had_cap:
                dp.CAPACITY[dp.LUNA_KEY] = saved_cap
            else:
                dp.CAPACITY.pop(dp.LUNA_KEY, None)

        self.addCleanup(_restore_luna)
        # 补满全部 15 个国内名额（显式 executor 逐一真实 claim，确定性填充）。
        self._reserve('cb1', 'codebuddy', 'deepseek-v4.1-flash', 'codebuddy')
        self._reserve('cbg', 'codebuddy', 'glm-5.3-flash', 'codebuddy', glm=True)
        self._reserve('z1', 'zcode', 'GLM-5.3', 'zcode')
        self._reserve('m1', 'qoder', 'Qwen3.8-Max', 'qoder')
        self._reserve('m2', 'qodercn', 'Qwen3.8-Max', 'qodercn')
        with unittest.mock.patch.object(dm, 'USER_DISABLED_POOL_KEYS', frozenset()):
            self._reserve('m3', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
            self._reserve('m4', 'qodercn', 'Qwen-3.8-Max', 'qodercn')
        self._reserve('cl1', 'claude', 'deepseek-flash', 'claude')
        self._reserve('cl2', 'claude', 'deepseek-flash', 'claude')
        self._reserve('f1', 'qoder', 'Qwen3.8-Flash', 'qoder')
        self._reserve('f2', 'qoder', 'Qwen3.8-Flash', 'qoder')
        self._reserve('f3', 'qodercn', 'Qwen3.8-Flash', 'qodercn')
        self._reserve('f4', 'qodercn', 'Qwen3.8-Flash', 'qodercn')
        self._reserve('o1', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, 'opencode')
        self._reserve('o2', 'opencode', dp.OPENCODE_GLM_MODEL, 'opencode')
        # BW-STRONG-SONUS-20261024：物理总量 16。weak 档无法新派 Sonus（档位硬排除），
        # 第 16 个名额只能由「strong 派出后仍在途的 Sonus」占住——这里按该真实形态直插
        # 一条 strong 时代遗留的 Sonus reserved 行（切档不追杀、不假释放）。
        with closing(dp.connect(self.store)) as conn:
            conn.execute(
                'INSERT INTO attempts(token, task_id, runtime, model, pool_key, state, '
                "reserved_at_utc) VALUES(?,?,?,?,?,?,?)",
                ('sonus-fixture', 'sonus-fixture-task', 'qoder', 'Sonus',
                 dp.SONUS_POOL_KEY, 'reserved', dp._iso(T0)))
            active = conn.execute(
                "SELECT COUNT(*) AS n FROM attempts WHERE state IN "
                "('reserved','running','unknown')").fetchone()['n']
        self.assertEqual(active, dp.DOMESTIC_TOTAL_CAPACITY, active)
        scope = {'task_id': 'L', 'stage': 's1', 'chat_id': 'c1',
                 'workspace': str(self._ws('L')), 'prompt_sha256': 'p' * 64}
        asked = dp.ask_record(self.store, task_id='L', scope=scope,
                              ask_message_id='m1', now=T0)
        self.assertTrue(asked['recorded'], asked)
        # 未到期裁决（not_due 分支同样先完成整段回收/档位裁决再判断 deadline）。
        calls, counting = self._counting_current_mode()
        with unittest.mock.patch.object(dm, 'current_mode', counting):
            r = dp.claim_due(self.store, task_id='L',
                             now=T0 + timedelta(seconds=10), _preclaim=False)
        self.assertFalse(r['claimed'], r)
        self.assertTrue(r.get('not_due'), r)
        self.assertEqual(calls, ['weak'], calls)


class SonusTempDisabledR2Tests(_AdjudicationBase):
    """BW-SONUS-TEMP-DISABLE-20261028-R2：用户指令再次暂时禁用 qoder:Sonus 的离线回归。

    口径：SONUS_TEMP_DISABLED=True 为唯一权威——三档（含 strong）可新派集合全部剔除
    Sonus；AUTO/显式 reserve/select-and-claim/claim-due/正式入口一律 mode_excluded
    （sent=false、零新占位、文案明确「temporarily disabled」）；strong 新派 4 池/5 槽；
    容量登记/优先级表历史层号/物理总容量 16/历史 attempt 全部保留；禁用前合法且仍
    reserved 的精确 Sonus claim 照常 consume/mark running；其它 mode_excluded 池不继承
    该例外。"""

    def test_flag_and_registration_facts(self):
        self.assertTrue(dm.SONUS_TEMP_DISABLED)
        # 登记全部保留：池字面量、独立 1 槽、优先级表历史层号、非 legacy/retired。
        self.assertEqual(dm.SONUS_POOL_KEY, 'qoder:Sonus')
        self.assertEqual(dp.capacity_for(SONUS_KEY), 1)
        self.assertEqual(dm.STRONG_PRIORITY[SONUS_KEY], 1)
        self.assertNotIn(SONUS_KEY, dp.LEGACY_RETIRED_POOL_KEYS)
        # 物理总守卫仍为 16；R2 禁用后 strong 新派口径 = medium（4 池/5 槽），weak 13。
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 16)
        self.assertEqual(sum(dp.capacity_for(k)
                             for k in dm.allowed_pools_for_mode('strong')), 5)
        self.assertEqual(sum(dp.capacity_for(k)
                             for k in dm.allowed_pools_for_mode('medium')), 5)
        self.assertEqual(sum(dp.capacity_for(k)
                             for k in dm.allowed_pools_for_mode('weak')), 13)
        self.assertEqual(len(dm.allowed_pools_for_mode('strong')), 4)
        self.assertEqual(len(dm.allowed_pools_for_mode('medium')), 4)
        self.assertEqual(len(dm.allowed_pools_for_mode('weak')), 10)
        # strong 六池层号/无时段门口径不变。
        strong = dm.priority_map_for_mode('strong')
        self.assertEqual(strong[SONUS_KEY], 1)
        self.assertEqual(strong['zcode:GLM-5.3'], 2)
        self.assertEqual(strong['claude:deepseek-flash'], 3)
        self.assertEqual({strong[k] for k in dm.MAX_POOL_KEYS}, {4})
        self.assertEqual(tuple(dm.window_gated_pools_for_mode('strong')), ())

    def test_all_modes_explicit_sonus_refused_with_zero_placeholders(self):
        for m in ('strong', 'medium', 'weak'):
            with dispatch_mode(m):
                r = dp.reserve(self.store, task_id=f'x-{m}', runtime='qoder',
                               model='Sonus', workspace=str(self._ws(f'x-{m}')),
                               prompt_sha256='p', executor='qoder', now=T0)
            self.assertFalse(r.get('allowed'), (m, r))
            self.assertFalse(r.get('sent'), (m, r))
            self.assertIsNone(r.get('token'), (m, r))
            self.assertEqual(r.get('reason'), 'mode_excluded', (m, r))
            # R2 禁用文案：明确写 temporarily disabled（用户指令），绝不写通用档位
            # 排除，也绝不伪装成额度/认证/时段/容量/永久退役。
            self.assertIn('temporarily disabled', r['reasons'][0], (m, r))
            self.assertIn('user instruction', r['reasons'][0], (m, r))
            self.assertNotIn('permanent retirement', r['reasons'][0].split('(')[0])
            with closing(dp.connect(self.store)) as conn:
                n = conn.execute('SELECT COUNT(*) AS n FROM attempts').fetchone()['n']
            self.assertEqual(n, 0, (m, n))

    def test_strong_auto_never_selects_sonus_first_is_zcode(self):
        # strong AUTO：禁用期间 Sonus 绝不被选；第一可用落 ZCode(2)。
        with dispatch_mode('strong'):
            r = dp.select_and_claim(self.store, task_id='a1', runtime='qoder',
                                    model='Qwen3.8-Max',
                                    workspace=str(self._ws('a1')),
                                    prompt_sha256='p', now=T0, _preclaim=False)
        self.assertTrue(r.get('routing_required'), r)
        self.assertEqual(r['selected']['pool_key'], 'zcode:GLM-5.3', r)

    def test_legacy_reserved_sonus_claim_still_consumes(self):
        # 禁用前合法取得且仍 reserved 的精确 Sonus claim：照常通过完整校验并 mark
        # running（不追杀、不假释放）。用开关临时回 False 真实 reserve 出该遗留 claim，
        # 再回 True 消费——绝不放开禁用期间的新 reserve。
        with dispatch_mode('strong'), \
                unittest.mock.patch.object(dm, 'SONUS_TEMP_DISABLED', False):
            r = dp.reserve(self.store, task_id='se1', runtime='qoder',
                           model='Sonus', workspace=str(self._ws('se1')),
                           prompt_sha256='p', executor='qoder', now=T0)
        self.assertTrue(r.get('allowed'), r)
        self.assertEqual(r.get('pool_key'), SONUS_KEY, r)
        tok = r['token']
        self.assertTrue(dm.SONUS_TEMP_DISABLED)  # 消费时开关已是真禁用
        with dispatch_mode('strong'):
            out = dp.consume_for_entry(self.store, task_id='se1', runtime='qoder',
                                       model='Sonus', workspace=str(self._ws('se1')),
                                       prompt_sha256='p', claim_token=tok,
                                       wrapper_pid=4321, wrapper_created='w1', now=T0)
        self.assertTrue(out['allowed'], out)
        self.assertEqual(out['token'], tok, out)
        with closing(dp.connect(self.store)) as conn:
            state = conn.execute('SELECT state FROM attempts WHERE token=?',
                                 (tok,)).fetchone()['state']
        self.assertEqual(state, 'running', state)

    def test_other_mode_excluded_pools_release_semantics_unchanged(self):
        """对照：非 Sonus 的 mode_excluded 池（strong 下的 CodeBuddy）consume 仍按既有
        语义把本 token reserved 释放为 start_failed 并拒绝——Sonus 恢复不影响该口径。"""
        with dispatch_mode('weak'):
            r = dp.reserve(self.store, task_id='cb1', runtime='codebuddy',
                           model='deepseek-v4.1-flash',
                           workspace=str(self._ws('cb1')), prompt_sha256='p',
                           executor='codebuddy', now=T0)
        self.assertTrue(r.get('allowed'), r)
        tok = r['token']
        with dispatch_mode('strong'):
            out = dp.consume_for_entry(self.store, task_id='cb1', runtime='codebuddy',
                                       model='deepseek-v4.1-flash',
                                       workspace=str(self._ws('cb1')),
                                       prompt_sha256='p', claim_token=tok,
                                       wrapper_pid=5321, wrapper_created='c1', now=T0)
        self.assertFalse(out.get('allowed'), out)
        self.assertEqual(out.get('reason'), 'mode_excluded', out)
        self.assertTrue(out.get('capacity_released'), out)
        with closing(dp.connect(self.store)) as conn:
            state = conn.execute('SELECT state FROM attempts WHERE token=?',
                                 (tok,)).fetchone()['state']
        self.assertEqual(state, 'start_failed', state)


if __name__ == '__main__':
    unittest.main()
