#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BW-QODER-CN-20261010-A2 容量池 Qoder CN 内置候选离线测试（纯标准库 unittest）。

BW-POOL-SPLIT-20261010-S3 五池真实布局：CN 不再走 DeepSeek-Flash（该池已从正常候选
退出，capacity=0，仅保留池名让真实旧在途到达终态释放）。CN 内置候选改为
qodercn:Qwen4-Max-Preview（主力，1 槽）与 qodercn:Qwen3.8-Flash（同级兜底，2 槽）。
覆盖任务书明确要求、且**只有这些**新行为：
1. CN 内置 Max=1、CN Flash=2、国内总容量 15（BW-ZCODE-RESTORE-1-20261022：ZCode 恢复为
   正常可派主力、严格 1 槽）；国际 Max=1、国际 Flash=2。CN 组合既可经显式 executor=
   'qodercn' claim；AUTO 主力/国际 Flash 顺序按 best-effort 1:1 与同级兜底裁决。原
   retired 的 qodercn:DeepSeek-Flash 一律 unknown_pool，绝不新派。
2. CN 与国际跨区域不同 workspace 可并行；同一真实 workspace 仍单写入（互斥）。
3. 跨 runtime 绝不消费旧 claim（CN token 不能被国际入口消费，反之亦然）。
4. 子进程真实结束立即释放 CN 名额（终态释放）。
5. BW-SIMPLE-PRIORITY-RETRY-S4 + BW-ZCODE-RESTORE-1-20261022：ZCode 被持久 availability
   hard_hold 时 AUTO 仍按最高优先级真实尝试（诊断不挡新轮）；显式 executor='zcode'
   权威入口同样不再据历史 hold 拒绝。ZCode 已恢复为正常可派主力（容量严格 1、层=2，
   紧跟三 Qwen Max）。

绝不读写真实/共享池：每例显式临时 store；真实额度/账单不在范围内。
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
_SCRIPTS = HERE.parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import dispatch_pool as dp  # noqa: E402
from dispatch_mode_support import dispatch_mode  # noqa: E402
from dispatch_mode_support import (insert_sonus_leftover,
                                   weak_new_dispatch_capacity)  # noqa: E402
dp.LUNA_DISABLED = False
dp.CAPACITY[dp.LUNA_KEY] = None
try:
    import quota_control as qc  # noqa: E402 - 仅 Z-hold 子例需要
except Exception:  # noqa: BLE001 - 隔离树无 quota_control 时跳过 Z-hold 子例
    qc = None

T0 = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)  # 北京时间 23:00（主力时段内）
CN = 'qodercn:DeepSeek-Flash'  # 已 retired：capacity=0，任何新派一律 unknown_pool
CN_MAX = 'qodercn:Qwen4-Max-Preview'
CN_FLASH = 'qodercn:Qwen3.8-Flash'
ZCODE = 'zcode:GLM-5.3'  # BW-ZCODE-RESTORE-1-20261022：恢复可派主力（严格 1 槽、层=2）
QMAX = 'qoder:Qwen4-Max-Preview'
QFLASH = 'qoder:Qwen3.8-Flash'
PROVIDER = 'account:bigmodel-individual-coding-plan'


class _Store(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-cn-')
        self.store = str(Path(self._tmp.name) / 'pool.sqlite3')
        self.assertNotEqual(self.store, str(dp.default_store_path()))
        # 隔离 quota：把默认配额库指向不存在的临时路径，令 select/reserve 的只读 availability
        # 回核读不到任何 blocking 行（ZCode 恒可用），绝不触碰机器上的真实/共享配额库。
        # Z-hold 用例另行显式传 quota_store，覆盖此默认。
        self.quota = str(Path(self._tmp.name) / 'quota-isolated.sqlite3')
        os = __import__('os')
        self.saved = {k: os.environ.pop(k, None)
                      for k in ('BRAIN_WORKER_DISPATCH_STORE',
                                'BRAIN_WORKER_QUOTA_STORE')}
        os.environ['BRAIN_WORKER_QUOTA_STORE'] = self.quota
        # BW-CODEBUDDY-MAIN-S3 + BW-WORKER-WIDGET-20261018-S3 + BW-OPENCODE-20261019：
        # 本文件的历史层序（Max > claude > Z）夹具写于 CodeBuddy/OpenCode 进层之前。先真实
        # 占住 CodeBuddy deepseek 与 GLM 各自的独立 1 槽（GLM 为额度条件池，夹具只作占位，
        # 不经 AUTO 候选），使旧层序断言原样成立；两 OpenCode 免费槽（tier4）在用例内需要
        # 满池语义时经真实 reserve/claim 逐一对齐补齐（BW-WEAK-RESTRUCTURE-20261116 起
        # 物理总量 17，绝不删断言迁就）。新层序由独立回归验证。
        # BW-WEAK-RESTRUCTURE-20261116：codebuddy:deepseek-v4.1-flash 已从所有档位可派
        # 集合移除——本夹具两行即「真实遗留形态」直插（pool_key 均已登记，切档不追杀）。
        with contextlib.closing(dp.connect(self.store)) as conn:
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
        import os
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def ws(self, name):
        d = Path(self._tmp.name) / f'ws-{name}'
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def cn(self, task, workspace=None):
        """CN 内置主力 qodercn:Qwen4-Max-Preview（capacity=1）。"""
        return dp.select_and_claim(self.store, task_id=task, runtime='qodercn',
                                   model='Qwen4-Max-Preview',
                                   workspace=workspace or self.ws(task),
                                   prompt_sha256='c' * 64, executor='qodercn',
                                   now=T0, _preclaim=False)

    def cn_flash(self, task, workspace=None):
        """CN 同级兜底 qodercn:Qwen3.8-Flash（capacity=2，不动 rotation committed）。"""
        return dp.select_and_claim(self.store, task_id=task, runtime='qodercn',
                                   model='Qwen3.8-Flash',
                                   workspace=workspace or self.ws(task),
                                   prompt_sha256='c' * 64, executor='qodercn',
                                   now=T0, _preclaim=False)

    def active(self, pk):
        return dp.status(self.store, now=T0)['pools'][pk]['active']

    def _insert_leftover(self, token, pool_key, *, task_id=None):
        """BW-WEAK-RESTRUCTURE-20261116：按真实遗留形态直插一条已登记池的在途 reserved
        行（该池已从所有档位可派集合移除，绝不经 reserve/select 伪造新派；
        pool_key 必须是 CAPACITY 已登记池）。"""
        runtime, model = pool_key.split(':', 1)
        with contextlib.closing(dp.connect(self.store)) as conn:
            conn.execute(
                'INSERT INTO attempts(token, task_id, runtime, model, pool_key, state, '
                'reserved_at_utc) VALUES(?,?,?,?,?,?,?)',
                (token, task_id or f'T-{token}', runtime, model, pool_key, 'reserved',
                 dp._iso(T0)))
            conn.commit()


class CapacityAndRotationTests(_Store):
    def test_cn_capacity_and_total_fifteen(self):
        # BW-CODEBUDDY-MAIN-S3 + BW-WORKER-WIDGET-20261018-S3 + BW-OPENCODE-20261019
        # + BW-ZCODE-RESTORE-1-20261022 真实布局：CN 内置 Max=1、
        # CN 自定义 Max=2、CN Flash=2、claude=2、codebuddy deepseek=1、codebuddy GLM=1
        # （两 CodeBuddy 模型各自独立 1 槽）、opencode 两免费模型各独立 1 槽；
        # retired DeepSeek capacity=0。
        self.assertEqual(dp.capacity_for(CN), 0)
        self.assertEqual(dp.capacity_for(CN_MAX), 1)
        self.assertEqual(dp.capacity_for('qodercn:Qwen-3.8-Max'), 2)
        self.assertEqual(dp.capacity_for(CN_FLASH), 2)
        self.assertEqual(dp.capacity_for('claude:deepseek-flash'), 2)
        self.assertEqual(dp.capacity_for('codebuddy:deepseek-v4.1-flash'), 1)
        self.assertEqual(dp.capacity_for('codebuddy:glm-5.3-flash'), 1)
        self.assertEqual(dp.capacity_for(dp.OPENCODE_DEEPSEEK_POOL_KEY), 1)
        self.assertEqual(dp.capacity_for(dp.OPENCODE_GLM_POOL_KEY), 1)
        # BW-STRONG-SONUS-20261024：物理总 16（strong 专属 Sonus 1 槽）；
        # BW-WEAK-RESTRUCTURE-20261116：新增弱档专用 zcode:GLM-5.3-Flash 独立 1 槽，
        # 物理总 16→17；弱档可新派集合收窄为 5 池共 6 槽（两 OpenCode 各 1 +
        # CodeBuddy GLM 1 + claude 2 + GLM-5.3-Flash 1）。
        self.assertEqual(dp.capacity_for(dp.SONUS_POOL_KEY), 1)
        self.assertEqual(dp.capacity_for(dp.ZCODE_FLASH_POOL_KEY), 1)
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 17)
        self.assertEqual(weak_new_dispatch_capacity(), 6)
        # ZCode（BW-ZCODE-RESTORE-1-20261022 恢复、严格 1 槽）/ 国际组合容量。
        self.assertEqual(dp.capacity_for(ZCODE), 1)
        self.assertEqual(dp.capacity_for(QMAX), 1)
        self.assertEqual(dp.capacity_for(QFLASH), 2)

    def test_explicit_cn_flash_claims_two_then_full(self):
        # BW-WEAK-RESTRUCTURE-20261116：qodercn:Qwen3.8-Flash 已从弱档移除且不属于
        # strong/medium 集合——所有档位显式新派一律 mode_excluded（sent=false、零新占位、
        # 绝不静默换模型）；容量登记 2 槽与历史在途结算语义保留（capacity_for 断言）。
        for mode in ('strong', 'medium', 'weak'):
            with dispatch_mode(mode):
                out = self.cn_flash(f'cn-{mode}')
                self.assertFalse(out['allowed'], (mode, out))
                self.assertEqual(out['reason'], 'mode_excluded', (mode, out))
                self.assertFalse(out.get('sent'), (mode, out))
                self.assertEqual(self.active(CN_FLASH), 0, mode)
        # 登记容量仍是 2（历史/物理登记不变，仅新派被档位硬排除）。
        self.assertEqual(dp.capacity_for(CN_FLASH), 2)

    def test_auto_prefers_main_then_flash_then_full_at_fifteen(self):
        # 真实目标：主力优先 → 同级 Flash 兜底 → 物理满才 capacity_full。
        # BW-WEAK-RESTRUCTURE-20261116：zcode:GLM-5.3 已移出弱档（strong/medium 保留），
        # AUTO 层序语义改在中档验证（T0=北京周四 23:00，夜间窗开、claude 离峰）：中档层序
        # Max(1) > claude(2) > zcode(3)，空池 AUTO 请求 zcode 被改派到更高层；Max 层内
        # 国际内置 Max 子优先键 0 先于 CN 内置 Max 子键 1（同层同在途平票按子键+stable
        # tie），故选中国际 Max。
        with dispatch_mode('medium'):
            first = dp.select_and_claim(self.store, task_id='p0', runtime='zcode',
                                        model='GLM-5.3', workspace=self.ws('p0'),
                                        prompt_sha256='p', executor='auto', now=T0,
                                        _preclaim=False)
            self.assertFalse(first['allowed'], first)
            self.assertTrue(first['routing_required'], first)
            self.assertEqual(first['selected']['pool_key'], dp.QMAX_POOL_KEY, first)
        self.assertEqual(self.active(ZCODE), 0)
        # 用明确 executor 约束补满其余槽位（BW-WEAK-RESTRUCTURE-20261116 组合）：
        # 中档真实 claim：Z 1 + 两区内置 Max 各 1 + claude 2；弱档真实 claim：
        # 两 OpenCode 免费槽各 1 + zcode:GLM-5.3-Flash 1；已移出全部档位可派集合的池
        # （CN 自定义 Max 2 + 两区 Flash 各 2）按真实遗留形态直插；加上 setUp 的
        # CodeBuddy deepseek 1 与 GLM 1 → 物理 16；补 strong 遗留 Sonus 1 → 17/17 满。
        fill_medium = [('z0', 'zcode', 'GLM-5.3', 'zcode'),
                       ('q0', 'qoder', 'Qwen4-Max-Preview', 'qoder'),
                       ('c0', 'qodercn', 'Qwen4-Max-Preview', 'qodercn'),
                       ('cl0', 'claude', 'deepseek-flash', 'claude'),
                       ('cl1', 'claude', 'deepseek-flash', 'claude')]
        fill_weak = [('oc0', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, 'opencode'),
                     ('oc1', 'opencode', dp.OPENCODE_GLM_MODEL, 'opencode'),
                     ('zf0', 'zcode', 'GLM-5.3-Flash', 'zcode')]
        with dispatch_mode('medium'):
            for tid, rt, md, ex in fill_medium:
                r = dp.select_and_claim(self.store, task_id=tid, runtime=rt, model=md,
                                        workspace=self.ws(tid), prompt_sha256='p',
                                        executor=ex, now=T0, _preclaim=False)
                self.assertTrue(r['allowed'], (tid, r))
        with dispatch_mode('weak'):
            for tid, rt, md, ex in fill_weak:
                r = dp.select_and_claim(self.store, task_id=tid, runtime=rt, model=md,
                                        workspace=self.ws(tid), prompt_sha256='p',
                                        executor=ex, now=T0, _preclaim=False)
                self.assertTrue(r['allowed'], (tid, r))
        # BW-WEAK-RESTRUCTURE-20261116：CN 自定义 Max（用户禁用）与两区 Flash 已不在任何
        # 档位可派集合——满池基线按真实遗留形态直插在途 reserved 行（已登记池）。
        for tid, pk in (('cu0', dp.CN_CUSTOM_MAX_POOL_KEY),
                        ('cu1', dp.CN_CUSTOM_MAX_POOL_KEY),
                        ('fl0', QFLASH), ('fl1', QFLASH),
                        ('cf0', CN_FLASH), ('cf1', CN_FLASH)):
            self._insert_leftover(f'left-{tid}', pk, task_id=tid)
        st = dp.status(self.store, now=T0)
        self.assertEqual(st['domestic']['active'], 16)
        # 补充一条 strong 遗留 Sonus 在途 → 物理 17/17。
        insert_sonus_leftover(self.store)
        st = dp.status(self.store, now=T0)
        self.assertEqual(st['domestic']['active'], 17)
        self.assertTrue(st['domestic']['full'])
        self.assertEqual(self.active(CN_FLASH), 2)
        # 物理 17/17 满时的诚实裁决（BW-WEAK-RESTRUCTURE-20261116）：
        # - 弱档 AUTO 请求弱档现役池（zcode:GLM-5.3-Flash）→ 迁移债务总额守卫如实
        #   capacity_full + domestic_full（总守卫先于档位兜底分支）；
        # - 弱档 AUTO/显式请求已移出弱档的 zcode:GLM-5.3 → 档位硬策略先于总守卫，
        #   mode_excluded（sent=false、零占位，绝不跨档派发）；
        # - 中档显式 executor='zcode' 请求 → 物理 capacity_full + domestic_full。
        with dispatch_mode('weak'):
            auto_full = dp.select_and_claim(self.store, task_id='all-full-weak',
                                            runtime='zcode', model='GLM-5.3-Flash',
                                            workspace=self.ws('all-full-weak'),
                                            prompt_sha256='p', executor='auto', now=T0,
                                            _preclaim=False)
            self.assertFalse(auto_full['allowed'], auto_full)
            self.assertEqual(auto_full['reason'], 'capacity_full', auto_full)
            self.assertTrue(auto_full['domestic_full'], auto_full)
            weak_excluded = dp.select_and_claim(self.store, task_id='all-full-old-z',
                                                runtime='zcode', model='GLM-5.3',
                                                workspace=self.ws('all-full-old-z'),
                                                prompt_sha256='p', executor='zcode',
                                                now=T0, _preclaim=False)
            self.assertFalse(weak_excluded['allowed'], weak_excluded)
            self.assertEqual(weak_excluded['reason'], 'mode_excluded', weak_excluded)
        with dispatch_mode('medium'):
            full = dp.select_and_claim(self.store, task_id='all-full', runtime='zcode',
                                       model='GLM-5.3', workspace=self.ws('all-full'),
                                       prompt_sha256='p', executor='zcode', now=T0,
                                       _preclaim=False)
            self.assertFalse(full['allowed'], full)
            self.assertEqual(full['reason'], 'capacity_full')
            self.assertTrue(full['domestic_full'], full)

    def test_retired_deepseek_never_dispatched(self):
        # retired DeepSeek-Flash：AUTO/显式 executor 一律 unknown_pool，绝不新派。
        auto = dp.select_and_claim(self.store, task_id='ds-a', runtime='qodercn',
                                   model='DeepSeek-Flash', workspace=self.ws('ds-a'),
                                   prompt_sha256='p', executor='auto', now=T0,
                                   _preclaim=False)
        self.assertFalse(auto['allowed'], auto)
        self.assertEqual(auto['reason'], 'unknown_pool')
        ex = dp.select_and_claim(self.store, task_id='ds-e', runtime='qodercn',
                                 model='DeepSeek-Flash', workspace=self.ws('ds-e'),
                                 prompt_sha256='p', executor='qodercn', now=T0,
                                 _preclaim=False)
        self.assertFalse(ex['allowed'], ex)
        self.assertEqual(ex['reason'], 'unknown_pool')

    def test_cn_flash_does_not_disturb_rotation_counts(self):
        # 先按 1:1 提交一次 Z、一次国际 Max（committed 各 bump）；用显式 executor 确保
        # 在 CodeBuddy 已占最高层的基线下仍真实落到目标池。
        # BW-WEAK-RESTRUCTURE-20261116：qodercn:Qwen4-Max-Preview 与 zcode:GLM-5.3 已移出
        # 弱档——两个真实 claim 改在中档执行（T0=北京周四 23:00 夜间窗开）。
        with dispatch_mode('medium'):
            draft = dp.select_and_claim(self.store, task_id='r-z', runtime='zcode',
                                        model='GLM-5.3', workspace=self.ws('r-z'),
                                        prompt_sha256='p', executor='zcode', now=T0,
                                        _preclaim=False)
            self.assertTrue(draft['allowed'], draft)
            draft = dp.select_and_claim(self.store, task_id='r-q', runtime='qoder',
                                        model='Qwen4-Max-Preview', workspace=self.ws('r-q'),
                                        prompt_sha256='p', executor='qoder', now=T0,
                                        _preclaim=False)
            self.assertTrue(draft['allowed'], draft)
        st_before = dp.status(self.store, now=T0)['rotation']
        # BW-WEAK-RESTRUCTURE-20261116：qodercn:Qwen3.8-Flash 已从所有档位可派集合移除，
        # 显式 claim 一律 mode_excluded（sent=false、零占位）——被拒请求同样绝不 disturb
        # rotation 计数。
        out = self.cn_flash('r-cn')
        self.assertFalse(out['allowed'], out)
        self.assertEqual(out['reason'], 'mode_excluded', out)
        st_after = dp.status(self.store, now=T0)['rotation']
        self.assertEqual(st_before['committed_zcode'], st_after['committed_zcode'])
        self.assertEqual(st_before['committed_qoder'], st_after['committed_qoder'])
        # 同语义正向补验：弱档现役的 zcode:GLM-5.3-Flash（独立 1 槽、非 rotation 记分池）
        # 真实 claim 后 rotation 两列同样不动。
        with dispatch_mode('weak'):
            zf = dp.select_and_claim(self.store, task_id='r-zf', runtime='zcode',
                                     model='GLM-5.3-Flash', workspace=self.ws('r-zf'),
                                     prompt_sha256='p', executor='zcode', now=T0,
                                     _preclaim=False)
            self.assertTrue(zf['allowed'], zf)
        st_after_zf = dp.status(self.store, now=T0)['rotation']
        self.assertEqual(st_before['committed_zcode'], st_after_zf['committed_zcode'])
        self.assertEqual(st_before['committed_qoder'], st_after_zf['committed_qoder'])


class CrossRegionParallelTests(_Store):
    def test_cn_and_international_parallel_different_workspace(self):
        # BW-WEAK-RESTRUCTURE-20261116：两区内置 Max 已移出弱档——真实并行场景改在中档
        # 验证（T0=北京周四 23:00 夜间窗开，两区 Max 均可派）。
        with dispatch_mode('medium'):
            intl = dp.select_and_claim(self.store, task_id='i1', runtime='qoder',
                                       model='Qwen4-Max-Preview', workspace=self.ws('i1'),
                                       prompt_sha256='p', executor='qoder', now=T0,
                                       _preclaim=False)
            cnr = self.cn('n1')
            self.assertTrue(intl['allowed'], intl)
            self.assertTrue(cnr['allowed'], cnr)
        st = dp.status(self.store, now=T0)['domestic']
        # setUp 的 CodeBuddy deepseek/GLM 真实 fixture 也计入国内在途：
        # 国际 Max 1 + CN 内置 Max 1 + CB 1 + GLM 1 = 4。
        self.assertEqual(st['active'], 4)
        # BW-WEAK-RESTRUCTURE-20261116：物理总 17 槽未满，二者共存。
        self.assertLess(st['active'], st['capacity'])

    def test_same_workspace_single_writer_across_runtimes(self):
        # BW-WEAK-RESTRUCTURE-20261116：两区内置 Max 已移出弱档——单写入守卫场景改在
        # 中档验证（守卫与 runtime/档位无关）。
        shared = self.ws('shared')
        with dispatch_mode('medium'):
            intl = dp.select_and_claim(self.store, task_id='sw-i', runtime='qoder',
                                       model='Qwen4-Max-Preview', workspace=shared,
                                       prompt_sha256='p', executor='qoder', now=T0,
                                       _preclaim=False)
            self.assertTrue(intl['allowed'], intl)
            # CN 内置用同一真实目录 → 单写入守卫拒绝（守卫与 runtime 无关）。
            cnr = self.cn('sw-c', workspace=shared)
            self.assertFalse(cnr['allowed'], cnr)
            self.assertEqual(cnr['reason'], 'workspace_in_flight')


class CrossRuntimeClaimTests(_Store):
    def test_cn_token_rejected_by_international_entry(self):
        # BW-WEAK-RESTRUCTURE-20261116：两区内置 Max 已移出弱档——跨 runtime claim 拒绝
        # 场景改在中档执行（两侧池在中档均可派，拒绝原因仍是 claim_invalid 而非档位排除）。
        with dispatch_mode('medium'):
            cnr = self.cn('cr')
            tok = cnr['token']
            out = dp.consume_for_entry(self.store, task_id='cr', runtime='qoder',
                                       model='Qwen4-Max-Preview', workspace=self.ws('cr'),
                                       prompt_sha256='c' * 64, claim_token=tok,
                                       wrapper_pid=4321, wrapper_created='w', now=T0,
                                       executor='qoder')
            self.assertFalse(out['allowed'], out)
            self.assertEqual(out['reason'], 'claim_invalid')
        # 未被消费，仍 reserved。
        self.assertEqual(dp.status(self.store, now=T0)['pools'][CN_MAX]['active'], 1)

    def test_international_token_rejected_by_cn_entry(self):
        # BW-WEAK-RESTRUCTURE-20261116：两区内置 Max 已移出弱档——跨 runtime claim 拒绝
        # 场景改在中档执行。
        with dispatch_mode('medium'):
            intl = dp.reserve(self.store, task_id='ic', runtime='qoder',
                              model='Qwen4-Max-Preview',
                              workspace=self.ws('ic'), prompt_sha256='i' * 64,
                              now=T0, executor='qoder', _preclaim=False)
            out = dp.consume_for_entry(self.store, task_id='ic', runtime='qodercn',
                                       model='Qwen4-Max-Preview', workspace=self.ws('ic'),
                                       prompt_sha256='i' * 64, claim_token=intl['token'],
                                       wrapper_pid=4321, wrapper_created='w', now=T0,
                                       executor='qodercn')
            self.assertFalse(out['allowed'], out)
            self.assertEqual(out['reason'], 'claim_invalid')


class TerminalReleaseTests(_Store):
    def test_release_on_real_terminal(self):
        # BW-WEAK-RESTRUCTURE-20261116：CN 内置 Max 已移出弱档——真实 claim 改在中档
        # （T0=北京周四 23:00 夜间窗开）；终态释放语义原样保留。
        with dispatch_mode('medium'):
            cnr = self.cn('rel')
            tok = cnr['token']
            self.assertEqual(self.active(CN_MAX), 1)
        res = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertTrue(res['released'], res)
        self.assertEqual(self.active(CN_MAX), 0)

    def test_cn_counts_toward_domestic_full_for_luna_threshold(self):
        # BW-CLAUDE-CAPACITY2 + BW-OPENCODE-20261019 + BW-ZCODE-RESTORE-1-20261022：用明确
        # executor 约束逐次核验 allowed 占满可派槽。BW-WEAK-RESTRUCTURE-20261116：中档
        # 真实 claim（Z1+两内置 Max+claude2）、弱档真实 claim（两 OpenCode 免费槽 +
        # zcode:GLM-5.3-Flash 1）；CN 自定义 Max 2 与两区 Flash 各 2 已移出全部档位可派
        # 集合，按真实遗留形态直插；加上 setUp 真实占住的 CodeBuddy deepseek 1 与 GLM 1
        # → 物理 16，补 strong 遗留 Sonus 1 → 17/17 满（Luna 阈值随物理总 17 更新）。
        fill_medium = [('f0', 'zcode', 'GLM-5.3', 'zcode'),
                       ('f1', 'qoder', 'Qwen4-Max-Preview', 'qoder'),
                       ('f3', 'qodercn', 'Qwen4-Max-Preview', 'qodercn'),
                       ('f4', 'claude', 'deepseek-flash', 'claude'),
                       ('f5', 'claude', 'deepseek-flash', 'claude')]
        with dispatch_mode('medium'):
            for tid, rt, md, ex in fill_medium:
                r = dp.select_and_claim(self.store, task_id=tid, runtime=rt, model=md,
                                        workspace=self.ws(tid), prompt_sha256='p',
                                        executor=ex, now=T0, _preclaim=False)
                self.assertTrue(r['allowed'], (tid, r))
        st = dp.status(self.store, now=T0)['domestic']
        # 中档 5 个显式 claim + setUp 的 CodeBuddy/GLM 真实 fixture 2 = 7（7/17 不算满）。
        self.assertEqual(st['active'], 7)
        self.assertFalse(st['full'])  # 7/17 不算满
        # BW-WEAK-RESTRUCTURE-20261116：CN 自定义 Max（用户禁用）与两区 Flash 已不在任何
        # 档位可派集合——物理满池基线按真实遗留形态直插在途 reserved 行（已登记池）。
        for tid, pk in (('cu0', dp.CN_CUSTOM_MAX_POOL_KEY),
                        ('cu1', dp.CN_CUSTOM_MAX_POOL_KEY),
                        ('fl0', QFLASH), ('fl1', QFLASH),
                        ('cf0', CN_FLASH), ('cf1', CN_FLASH)):
            self._insert_leftover(f'th-left-{tid}', pk, task_id=tid)
        self.assertEqual(self.active(CN_FLASH), 2)
        # 两 OpenCode 免费槽与弱档专用 GLM-5.3-Flash 经真实显式 claim 补齐。
        with dispatch_mode('weak'):
            for tid, rt, md in (('oc0', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL),
                                ('oc1', 'opencode', dp.OPENCODE_GLM_MODEL),
                                ('zf0', 'zcode', 'GLM-5.3-Flash')):
                r = dp.select_and_claim(self.store, task_id=tid, runtime=rt, model=md,
                                        workspace=self.ws(tid),
                                        prompt_sha256='p', executor=rt, now=T0,
                                        _preclaim=False)
                self.assertTrue(r['allowed'], (tid, r))
        # 补一条 strong 遗留 Sonus 在途 → 物理 17/17 才满。
        insert_sonus_leftover(self.store)
        st = dp.status(self.store, now=T0)['domestic']
        self.assertEqual(st['active'], 17)
        self.assertTrue(st['full'])  # 17/17 才满


class DomesticReclaimTests(_Store):
    def test_reclaim_after_fifteen_full_prefers_cn_not_luna(self):
        # 物理满池后记录真实 ask；仅释放 1 个名额 → 国内恢复空位 → claim_due 原子
        # domestic_reclaim 按当前档候选顺序选中释放池，绝不因未满而错误升级 Luna。
        # BW-WEAK-RESTRUCTURE-20261116：CN Flash 已从所有档位可派集合移除（回收候选绝不含
        # 它），回收场景改在中档释放 CN 内置 Max 名额并核验选中 CN_MAX；满池组合改为
        # 中档真实 claim（Z1+两内置 Max+claude2）+ 弱档真实 claim（两 OpenCode+
        # GLM-5.3-Flash）+ 直插遗留（CN 自定义 Max 2、两区 Flash 各 2）+ setUp CodeBuddy
        # deepseek/GLM 各 1 + strong 遗留 Sonus 1 = 物理 17/17。
        fill_medium = [('r0', 'zcode', 'GLM-5.3', 'zcode'),
                       ('r2', 'qoder', 'Qwen4-Max-Preview', 'qoder'),
                       ('r3', 'qodercn', 'Qwen4-Max-Preview', 'qodercn'),
                       ('r3c', 'claude', 'deepseek-flash', 'claude'),
                       ('r3d', 'claude', 'deepseek-flash', 'claude')]
        fill_weak = [('r8', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, 'opencode'),
                     ('r9', 'opencode', dp.OPENCODE_GLM_MODEL, 'opencode'),
                     ('rzf', 'zcode', 'GLM-5.3-Flash', 'zcode')]
        toks = {}
        with dispatch_mode('medium'):
            for tid, rt, md, ex in fill_medium:
                r = dp.reserve(self.store, task_id=tid, runtime=rt, model=md,
                               workspace=self.ws(tid), prompt_sha256='p', stage='s1',
                               chat_id='c1', executor=ex, now=T0, _preclaim=False)
                self.assertTrue(r.get('allowed') and r.get('token'), (tid, r))
                toks[tid] = r['token']
        with dispatch_mode('weak'):
            for tid, rt, md, ex in fill_weak:
                r = dp.reserve(self.store, task_id=tid, runtime=rt, model=md,
                               workspace=self.ws(tid), prompt_sha256='p', stage='s1',
                               chat_id='c1', executor=ex, now=T0, _preclaim=False)
                self.assertTrue(r.get('allowed') and r.get('token'), (tid, r))
                toks[tid] = r['token']
        # BW-WEAK-RESTRUCTURE-20261116：CN 自定义 Max（用户禁用）与两区 Flash 已不在任何
        # 档位可派集合——按真实遗留形态直插在途 reserved 行（已登记池）。
        for tid, pk in (('r3a', dp.CN_CUSTOM_MAX_POOL_KEY),
                        ('r3b', dp.CN_CUSTOM_MAX_POOL_KEY),
                        ('r4', QFLASH), ('r5', QFLASH),
                        ('r6', CN_FLASH), ('r7', CN_FLASH)):
            self._insert_leftover(f'rc-left-{tid}', pk, task_id=tid)
        st = dp.status(self.store, now=T0)['domestic']
        self.assertEqual(st['active'], 16)
        # 补 strong 遗留 Sonus 1 槽 = 物理 17/17。
        insert_sonus_leftover(self.store)
        st = dp.status(self.store, now=T0)['domestic']
        self.assertEqual(st['active'], 17)
        self.assertTrue(st['full'])
        scope = {'task_id': 'ask-rc', 'stage': 's1', 'chat_id': 'c1',
                 'workspace': self.ws('ask-rc'), 'prompt_sha256': 'a' * 64}
        ask = dp.ask_record(self.store, task_id='ask-rc', scope=scope,
                            ask_message_id='m-ask', now=T0)
        self.assertTrue(ask['recorded'], ask)
        # 仅释放一个 CN 内置 Max 名额（其余槽仍占用）→ 国内已恢复空位。
        rel = dp.finish(self.store, toks['r3'], terminal='cancelled', now=T0)
        self.assertTrue(rel['released'], rel)
        self.assertEqual(self.active(CN_MAX), 0)
        # BW-WEAK-RESTRUCTURE-20261116：回收按当前档裁决——中档（夜间窗开）候选含
        # CN 内置 Max，原子回收选中该池，绝不升级 Luna。
        with dispatch_mode('medium'):
            out = dp.claim_due(self.store, task_id='ask-rc', scope=scope, now=T0)
        self.assertTrue(out.get('claimed'), out)
        self.assertEqual(out['pool_key'], CN_MAX, out)
        self.assertEqual(out['mode'], 'domestic_reclaim')
        # 未满时不错误 Luna：Luna 零在途，票据被原子取消（无丢槽、无双派）。
        self.assertEqual(dp.status(self.store, now=T0)['pools'][dp.LUNA_KEY]['active'], 0)


@unittest.skipIf(qc is None, 'quota_control 不可用（隔离树）')
class ZcodeHoldSkipTests(_Store):
    def _hold(self):
        quota = str(Path(self._tmp.name) / 'quota.sqlite3')
        group = qc.resolve_group(qc.load_routes(str(Path(self._tmp.name) / 'none.json')),
                                 'zcode', {'provider': PROVIDER})['quota_group']
        classification = {'is_trusted_429': True, 'hard_quota': True, 'kind': 'hard_hold',
                          'provider_code': 1310, 'provider': PROVIDER,
                          'blocked_until_utc': None, 'reason': 'synthetic hard hold'}
        qc.record_zcode_unavailability(quota, provider=PROVIDER, quota_group=group,
                                       classification=classification, now=T0)
        return quota

    def test_auto_no_longer_skips_zcode_under_hard_hold(self):
        # BW-SIMPLE-PRIORITY-RETRY-S5：Z 不再因历史 hard_hold 被剔除候选（`_eligible_main_forces`
        # 仍含 Z）；AUTO 遵循层序，请求 Z 会被 routing_required 改派到更高层、零 Z 启动。
        # 放开旧 hold 只代表 Z 合格，不代表 Z 抢过空闲更高层——显式
        # executor='zcode' 权威入口仍直接 claim Z，成功/失败 CAS 与 availability 诊断保留不变。
        # BW-WEAK-RESTRUCTURE-20261116：zcode:GLM-5.3 已移出弱档——本例（AUTO 层序 + 显式
        # ZCode 入口）改在中档执行（T0=北京周四 23:00 夜间窗开；中档层序 Max(1) >
        # claude(2) > zcode(3)，空池 AUTO 请求 Z 改派到国际内置 Max——Max 层内子优先键
        # 国际 0 先于 CN 内置 1）。
        quota = self._hold()
        with dispatch_mode('medium'):
            r = dp.select_and_claim(self.store, task_id='h', runtime='zcode', model='GLM-5.3',
                                    workspace=self.ws('h'), prompt_sha256='p',
                                    executor='auto', quota_store=quota, now=T0,
                                    _preclaim=False)
            self.assertFalse(r.get('allowed'), r)
            self.assertTrue(r.get('routing_required'), r)
            self.assertEqual(r['selected']['pool_key'], dp.QMAX_POOL_KEY, r)
            self.assertEqual(self.active(ZCODE), 0)
            # Z 仍合格：新政策下 _eligible_main_forces 不据历史 hold 剔除 Z。
            with contextlib.closing(dp.connect(self.store)) as conn:
                elig = dp._eligible_main_forces(conn, zcode_blocked=True, now=T0)
            self.assertIn(ZCODE, elig)
            # 显式 executor='zcode' 权威入口同样不再据历史 hold 拒绝，直接真实尝试 ZCode 池。
            z = dp.select_and_claim(self.store, task_id='hz', runtime='zcode', model='GLM-5.3',
                                    workspace=self.ws('hz'), prompt_sha256='p',
                                    executor='zcode', quota_store=quota, now=T0,
                                    _preclaim=False)
            self.assertTrue(z['allowed'], z)
            self.assertEqual(z['pool_key'], ZCODE, z)
        # 历史 availability 诊断原样保留（不假装 healthy、不清 hold）。
        self.assertTrue(qc.zcode_blocking_rows(quota, now=T0))

    def test_cn_claimable_independently_while_zcode_held(self):
        quota = self._hold()
        # Z 的历史 hold 只是诊断，不挡 CN 独立 claim；新轮 CN 也照常最高优先。
        # BW-WEAK-RESTRUCTURE-20261116：CN 内置 Max 已移出弱档——独立 claim 改在中档验证
        # （T0=北京周四 23:00 夜间窗开）。
        with dispatch_mode('medium'):
            self.assertTrue(self.cn('cnz')['allowed'])
        self.assertEqual(self.active(CN_MAX), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
