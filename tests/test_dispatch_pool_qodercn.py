#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BW-QODER-CN-20261010-A2 容量池 Qoder CN 内置候选离线测试（纯标准库 unittest）。

BW-POOL-SPLIT-20261010-S3 五池真实布局：CN 不再走 DeepSeek-Flash（该池已从正常候选
退出，capacity=0，仅保留池名让真实旧在途到达终态释放）。CN 内置候选改为
qodercn:Qwen3.8-Max（主力，1 槽）与 qodercn:Qwen3.8-Flash（同级兜底，2 槽）。
覆盖任务书明确要求、且**只有这些**新行为：
1. CN 内置 Max=1、CN Flash=2、国内总容量 14（BW-ZCODE-RETIRE-20261020：zcode 已退役、
   capacity=0，不再占国内槽）；国际 Max=1、国际 Flash=2。CN 组合既可经显式 executor=
   'qodercn' claim；AUTO 主力/国际 Flash 顺序按 best-effort 1:1 与同级兜底裁决。原 retired
   的 qodercn:DeepSeek-Flash 与 zcode:GLM-5.3 一律 unknown_pool，绝不新派。
2. CN 与国际跨区域不同 workspace 可并行；同一真实 workspace 仍单写入（互斥）。
3. 跨 runtime 绝不消费旧 claim（CN token 不能被国际入口消费，反之亦然）。
4. 子进程真实结束立即释放 CN 名额（终态释放）。
5. BW-SIMPLE-PRIORITY-RETRY-S4：ZCode 被持久 availability hard_hold 时 AUTO 仍按最高优先级
   真实尝试（诊断不挡新轮）。BW-ZCODE-RETIRE-20261020 起 ZCode 池整体退役：任何 zcode
   组合（含显式 executor='zcode'）一律 honest refuse=unknown_pool，hard_hold 诊断行仍
   可作历史回显。

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
import dispatch_pool as dp  # noqa: E402
dp.LUNA_DISABLED = False
dp.CAPACITY[dp.LUNA_KEY] = None
try:
    import quota_control as qc  # noqa: E402 - 仅 Z-hold 子例需要
except Exception:  # noqa: BLE001 - 隔离树无 quota_control 时跳过 Z-hold 子例
    qc = None

T0 = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)  # 北京时间 23:00（主力时段内）
CN = 'qodercn:DeepSeek-Flash'  # 已 retired：capacity=0，任何新派一律 unknown_pool
CN_MAX = 'qodercn:Qwen3.8-Max'
CN_FLASH = 'qodercn:Qwen3.8-Flash'
ZCODE = 'zcode:GLM-5.3'  # BW-ZCODE-RETIRE-20261020：整池退役，同 DeepSeek-Flash 口径
QMAX = 'qoder:Qwen3.8-Max'
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
        # 满池语义时经真实 reserve/claim 逐一对齐补齐（BW-ZCODE-RETIRE-20261020 起总量 14，
        # 绝不删断言迁就）。新层序
        # 由独立回归验证。
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
        """CN 内置主力 qodercn:Qwen3.8-Max（capacity=1）。"""
        return dp.select_and_claim(self.store, task_id=task, runtime='qodercn',
                                   model='Qwen3.8-Max',
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


class CapacityAndRotationTests(_Store):
    def test_cn_capacity_and_total_fourteen(self):
        # BW-CODEBUDDY-MAIN-S3 + BW-WORKER-WIDGET-20261018-S3 + BW-OPENCODE-20261019
        # + BW-ZCODE-RETIRE-20261020 真实布局：CN 内置 Max=1、
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
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 14)
        # BW-ZCODE-RETIRE-20261020：zcode 退役 capacity=0；国际组合容量原样不变。
        self.assertEqual(dp.capacity_for(ZCODE), 0)
        self.assertEqual(dp.capacity_for(QMAX), 1)
        self.assertEqual(dp.capacity_for(QFLASH), 2)

    def test_explicit_cn_flash_claims_two_then_full(self):
        a = self.cn_flash('cn-a')
        b = self.cn_flash('cn-b')
        self.assertTrue(a['allowed'], a)
        self.assertTrue(b['allowed'], b)
        self.assertEqual(self.active(CN_FLASH), 2)
        c = self.cn_flash('cn-c')
        self.assertFalse(c['allowed'])
        self.assertEqual(c['reason'], 'capacity_full')
        self.assertEqual(self.active(CN_FLASH), 2)

    def test_auto_prefers_main_then_flash_then_full_at_fourteen(self):
        # 真实目标：主力优先 → 同级 Flash 兜底 → 14 满才物理满。
        # 新优先级：三 Qoder Max > claude > opencode/codebuddy 层 > Flash；CodeBuddy
        # deepseek 已在 setUp 占住最高层。BW-ZCODE-RETIRE-20261020 起 zcode 整池退役，
        # AUTO 请求 zcode 组合与显式一样一律 unknown_pool、绝不改派；BW-CUSTOM-MAX-
        # RESUME-20261018-S1 起 Max 层内 CN 自定义先于两内置。
        retired = dp.select_and_claim(self.store, task_id='p0', runtime='zcode',
                                      model='GLM-5.3', workspace=self.ws('p0'),
                                      prompt_sha256='p', executor='auto', now=T0,
                                      _preclaim=False)
        self.assertFalse(retired['allowed'], retired)
        self.assertEqual(retired['reason'], 'unknown_pool', retired)
        self.assertFalse(retired['routing_required'], retired)
        self.assertIsNone(retired['token'], retired)
        self.assertEqual(self.active(ZCODE), 0)
        first = dp.select_and_claim(self.store, task_id='p1', runtime='qoder',
                                    model='Qwen3.8-Max', workspace=self.ws('p1'),
                                    prompt_sha256='p', executor='auto', now=T0,
                                    _preclaim=False)
        self.assertFalse(first['allowed'], first)
        self.assertTrue(first['routing_required'], first)
        self.assertEqual(first['selected']['pool_key'], dp.CN_CUSTOM_MAX_POOL_KEY)
        # 用明确 executor 约束补满其余十二槽（两区内置 Max 各 1 + CN 自定义 Max 2
        # + claude 2 + 两区 Flash 各 2 + 两 OpenCode 免费槽各 1）+ setUp 的 CodeBuddy
        # deepseek 1 与 GLM 1 → 总 14。
        fill = [('q0', 'qoder', 'Qwen3.8-Max', 'qoder'),
                ('c0', 'qodercn', 'Qwen3.8-Max', 'qodercn'),
                ('cu0', 'qodercn', 'Qwen-3.8-Max', 'qodercn'),
                ('cu1', 'qodercn', 'Qwen-3.8-Max', 'qodercn'),
                ('cl0', 'claude', 'deepseek-flash', 'claude'),
                ('cl1', 'claude', 'deepseek-flash', 'claude'),
                ('fl0', 'qoder', 'Qwen3.8-Flash', 'qoder'),
                ('fl1', 'qoder', 'Qwen3.8-Flash', 'qoder'),
                ('cf0', 'qodercn', 'Qwen3.8-Flash', 'qodercn'),
                ('cf1', 'qodercn', 'Qwen3.8-Flash', 'qodercn'),
                ('oc0', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, 'opencode'),
                ('oc1', 'opencode', dp.OPENCODE_GLM_MODEL, 'opencode')]
        for tid, rt, md, ex in fill:
            r = dp.select_and_claim(self.store, task_id=tid, runtime=rt, model=md,
                                    workspace=self.ws(tid), prompt_sha256='p',
                                    executor=ex, now=T0, _preclaim=False)
            self.assertTrue(r['allowed'], (tid, r))
        st = dp.status(self.store, now=T0)
        self.assertEqual(st['domestic']['active'], 14)
        self.assertTrue(st['domestic']['full'])
        self.assertEqual(self.active(CN_FLASH), 2)
        # 14 满：AUTO 请求主力 → 物理 capacity_full。
        full = dp.select_and_claim(self.store, task_id='all-full', runtime='qoder',
                                   model='Qwen3.8-Max', workspace=self.ws('all-full'),
                                   prompt_sha256='p', executor='auto', now=T0,
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
        # BW-ZCODE-RETIRE-20261020：zcode 已退役不再 bump committed_zcode（退役请求一律
        # unknown_pool，历史计数不再新增）；先按组各提交一次 CN 自定义 Max 与国际 Max
        # （committed_qoder bump），确认 committed_zcode 恒 0；CN Flash 兜底绝不扰动任何计数。
        zret = dp.select_and_claim(self.store, task_id='r-z', runtime='zcode',
                                  model='GLM-5.3', workspace=self.ws('r-z'),
                                  prompt_sha256='p', executor='zcode', now=T0,
                                  _preclaim=False)
        self.assertFalse(zret['allowed'], zret)
        self.assertEqual(zret['reason'], 'unknown_pool', zret)
        draft = dp.select_and_claim(self.store, task_id='r-cu', runtime='qodercn',
                                    model='Qwen-3.8-Max', workspace=self.ws('r-cu'),
                                    prompt_sha256='p', executor='qodercn', now=T0,
                                    _preclaim=False)
        self.assertTrue(draft['allowed'], draft)
        draft = dp.select_and_claim(self.store, task_id='r-q', runtime='qoder',
                                    model='Qwen3.8-Max', workspace=self.ws('r-q'),
                                    prompt_sha256='p', executor='qoder', now=T0,
                                    _preclaim=False)
        self.assertTrue(draft['allowed'], draft)
        st_before = dp.status(self.store, now=T0)['rotation']
        self.assertEqual(st_before['committed_zcode'], 0)
        self.assertTrue(self.cn_flash('r-cn')['allowed'])
        st_after = dp.status(self.store, now=T0)['rotation']
        self.assertEqual(st_before['committed_zcode'], st_after['committed_zcode'])
        self.assertEqual(st_before['committed_qoder'], st_after['committed_qoder'])


class CrossRegionParallelTests(_Store):
    def test_cn_and_international_parallel_different_workspace(self):
        intl = dp.select_and_claim(self.store, task_id='i1', runtime='qoder',
                                    model='Qwen3.8-Max', workspace=self.ws('i1'),
                                    prompt_sha256='p', executor='qoder', now=T0,
                                    _preclaim=False)
        cnr = self.cn('n1')
        self.assertTrue(intl['allowed'], intl)
        self.assertTrue(cnr['allowed'], cnr)
        st = dp.status(self.store, now=T0)['domestic']
        # setUp 的 CodeBuddy deepseek/GLM 真实 fixture 也计入国内在途：
        # 国际 Max 1 + CN 内置 Max 1 + CB 1 + GLM 1 = 4。
        self.assertEqual(st['active'], 4)
        self.assertLess(st['active'], st['capacity'])  # 16 槽未满，二者共存

    def test_same_workspace_single_writer_across_runtimes(self):
        shared = self.ws('shared')
        intl = dp.select_and_claim(self.store, task_id='sw-i', runtime='qoder',
                                   model='Qwen3.8-Max', workspace=shared,
                                   prompt_sha256='p', executor='qoder', now=T0,
                                   _preclaim=False)
        self.assertTrue(intl['allowed'], intl)
        # CN 内置用同一真实目录 → 单写入守卫拒绝（守卫与 runtime 无关）。
        cnr = self.cn('sw-c', workspace=shared)
        self.assertFalse(cnr['allowed'], cnr)
        self.assertEqual(cnr['reason'], 'workspace_in_flight')


class CrossRuntimeClaimTests(_Store):
    def test_cn_token_rejected_by_international_entry(self):
        cnr = self.cn('cr')
        tok = cnr['token']
        out = dp.consume_for_entry(self.store, task_id='cr', runtime='qoder',
                                   model='Qwen3.8-Max', workspace=self.ws('cr'),
                                   prompt_sha256='c' * 64, claim_token=tok,
                                   wrapper_pid=4321, wrapper_created='w', now=T0,
                                   executor='qoder')
        self.assertFalse(out['allowed'], out)
        self.assertEqual(out['reason'], 'claim_invalid')
        # 未被消费，仍 reserved。
        self.assertEqual(dp.status(self.store, now=T0)['pools'][CN_MAX]['active'], 1)

    def test_international_token_rejected_by_cn_entry(self):
        intl = dp.reserve(self.store, task_id='ic', runtime='qoder', model='Qwen3.8-Max',
                          workspace=self.ws('ic'), prompt_sha256='i' * 64,
                          now=T0, executor='qoder', _preclaim=False)
        out = dp.consume_for_entry(self.store, task_id='ic', runtime='qodercn',
                                   model='Qwen3.8-Max', workspace=self.ws('ic'),
                                   prompt_sha256='i' * 64, claim_token=intl['token'],
                                   wrapper_pid=4321, wrapper_created='w', now=T0,
                                   executor='qodercn')
        self.assertFalse(out['allowed'], out)
        self.assertEqual(out['reason'], 'claim_invalid')


class TerminalReleaseTests(_Store):
    def test_release_on_real_terminal(self):
        cnr = self.cn('rel')
        tok = cnr['token']
        self.assertEqual(self.active(CN_MAX), 1)
        res = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertTrue(res['released'], res)
        self.assertEqual(self.active(CN_MAX), 0)

    def test_cn_counts_toward_domestic_full_for_luna_threshold(self):
        # BW-CLAUDE-CAPACITY2 + BW-OPENCODE-20261019 + BW-ZCODE-RETIRE-20261020：zcode
        # 退役不再占槽；用明确 executor 约束逐次核验 allowed 占满六槽（两内置 Max+
        # 国际 Flash2+claude2），再补 CN 自定义 Max 2 +
        # 两 CN Flash + 两 OpenCode 免费槽 → 其余 12 满，加上
        # setUp 真实占住的 CodeBuddy deepseek 1 与 GLM 1 → 总 14 满；Luna 阈值随总 14 更新
        # （8/14 不算 full，14/14 才满）。
        fill = [('f1', 'qoder', 'Qwen3.8-Max', 'qoder'),
                ('f3', 'qodercn', 'Qwen3.8-Max', 'qodercn'),
                ('f4', 'claude', 'deepseek-flash', 'claude'),
                ('f5', 'claude', 'deepseek-flash', 'claude'),
                ('f6', 'qoder', 'Qwen3.8-Flash', 'qoder'),
                ('f7', 'qoder', 'Qwen3.8-Flash', 'qoder')]
        for tid, rt, md, ex in fill:
            r = dp.select_and_claim(self.store, task_id=tid, runtime=rt, model=md,
                                    workspace=self.ws(tid), prompt_sha256='p',
                                    executor=ex, now=T0, _preclaim=False)
            self.assertTrue(r['allowed'], (tid, r))
        st = dp.status(self.store, now=T0)['domestic']
        # 上面 6 个显式 claim + setUp 的 CodeBuddy/GLM 真实 fixture 2 = 8（8/14 不算满）。
        self.assertEqual(st['active'], 8)
        self.assertFalse(st['full'])  # 8/14 不算满
        # 补齐 CN 自定义 Max 2（now=T0 为北京 23:00、在主力时段内，正常 executor='qodercn' claim）。
        for tid in ('cu0', 'cu1'):
            r = dp.select_and_claim(self.store, task_id=tid, runtime='qodercn',
                                    model='Qwen-3.8-Max', workspace=self.ws(tid),
                                    prompt_sha256='p', executor='qodercn', now=T0,
                                    _preclaim=False)
            self.assertTrue(r['allowed'], (tid, r))
        self.assertTrue(self.cn_flash('fc0')['allowed'])
        self.assertTrue(self.cn_flash('fc1')['allowed'])
        # 两 OpenCode 免费槽经真实显式 claim 补齐 → 14/14 才满。
        for tid, model in (('oc0', dp.OPENCODE_DEEPSEEK_MODEL),
                           ('oc1', dp.OPENCODE_GLM_MODEL)):
            r = dp.select_and_claim(self.store, task_id=tid, runtime='opencode',
                                    model=model, workspace=self.ws(tid),
                                    prompt_sha256='p', executor='opencode', now=T0,
                                    _preclaim=False)
            self.assertTrue(r['allowed'], (tid, r))
        st = dp.status(self.store, now=T0)['domestic']
        self.assertTrue(st['full'])  # 14/14 才满


class DomesticReclaimTests(_Store):
    def test_reclaim_after_fourteen_full_prefers_cn_not_luna(self):
        # BW-ZCODE-RETIRE-20261020：zcode 退役不再占槽。十四槽真实占满（setUp
        # CodeBuddy deepseek 1 + GLM 1 + 其余 12，含 BW-OPENCODE-
        # 20261019 两 OpenCode 免费槽）后记录真实 ask；
        # 仅释放 1 个 CN Flash
        # 名额 → 国内恢复空位 → claim_due 原子 domestic_reclaim 复用既有候选顺序选中该
        # CN Flash，绝不因未满而错误升级 Luna。
        fill = [('r2', 'qoder', 'Qwen3.8-Max', 'qoder'),
                ('r3', 'qodercn', 'Qwen3.8-Max', 'qodercn'),
                ('r3a', 'qodercn', 'Qwen-3.8-Max', 'qodercn'),
                ('r3b', 'qodercn', 'Qwen-3.8-Max', 'qodercn'),
                ('r3c', 'claude', 'deepseek-flash', 'claude'),
                ('r3d', 'claude', 'deepseek-flash', 'claude'),
                ('r4', 'qoder', 'Qwen3.8-Flash', 'qoder'),
                ('r5', 'qoder', 'Qwen3.8-Flash', 'qoder'),
                ('r6', 'qodercn', 'Qwen3.8-Flash', 'qodercn'),
                ('r7', 'qodercn', 'Qwen3.8-Flash', 'qodercn'),
                ('r8', 'opencode', dp.OPENCODE_DEEPSEEK_MODEL, 'opencode'),
                ('r9', 'opencode', dp.OPENCODE_GLM_MODEL, 'opencode')]
        toks = {}
        for tid, rt, md, ex in fill:
            r = dp.reserve(self.store, task_id=tid, runtime=rt, model=md,
                           workspace=self.ws(tid), prompt_sha256='p', stage='s1',
                           chat_id='c1', executor=ex, now=T0, _preclaim=False)
            self.assertTrue(r.get('allowed') and r.get('token'), (tid, r))
            toks[tid] = r['token']
        st = dp.status(self.store, now=T0)['domestic']
        self.assertEqual(st['active'], 14)
        self.assertTrue(st['full'])
        scope = {'task_id': 'ask-rc', 'stage': 's1', 'chat_id': 'c1',
                 'workspace': self.ws('ask-rc'), 'prompt_sha256': 'a' * 64}
        ask = dp.ask_record(self.store, task_id='ask-rc', scope=scope,
                            ask_message_id='m-ask', now=T0)
        self.assertTrue(ask['recorded'], ask)
        # 仅释放一个 CN Flash 名额（其余仍占用）→ 国内已恢复空位。
        rel = dp.finish(self.store, toks['r6'], terminal='cancelled', now=T0)
        self.assertTrue(rel['released'], rel)
        self.assertEqual(self.active(CN_FLASH), 1)
        out = dp.claim_due(self.store, task_id='ask-rc', scope=scope, now=T0)
        self.assertTrue(out.get('claimed'), out)
        self.assertEqual(out['pool_key'], CN_FLASH, out)
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

    def test_zcode_retired_rejects_honestly_under_hard_hold(self):
        # BW-ZCODE-RETIRE-20261020（用户明确指令）：zcode:GLM-5.3 已全局退役，任何入口
        # （AUTO 或显式 executor='zcode'）的新 claim 一律 honest refuse=unknown_pool，
        # 绝不改派、绝不给 token；`_eligible_main_forces` 不再含 ZCODE；历史 hard_hold
        # availability 诊断行仍原样保留（不假装 healthy、不清 hold）。
        quota = self._hold()
        r = dp.select_and_claim(self.store, task_id='h', runtime='zcode', model='GLM-5.3',
                                workspace=self.ws('h'), prompt_sha256='p',
                                executor='auto', quota_store=quota, now=T0,
                                _preclaim=False)
        self.assertFalse(r.get('allowed'), r)
        self.assertEqual(r.get('reason'), 'unknown_pool', r)
        self.assertFalse(r.get('routing_required'), r)
        self.assertIsNone(r.get('token'), r)
        self.assertEqual(self.active(ZCODE), 0)
        # 退役池不再进候选集合：_eligible_main_forces 无论 zcode_blocked 与否都不含 ZCODE。
        with contextlib.closing(dp.connect(self.store)) as conn:
            elig = dp._eligible_main_forces(conn, zcode_blocked=True, now=T0)
        self.assertNotIn(ZCODE, elig)
        # 显式 executor='zcode' 权威入口同样 honest refuse（LEGACY_RETIRED 门在 executor
        # 分支之前），绝不真实尝试 ZCode 池。
        z = dp.select_and_claim(self.store, task_id='hz', runtime='zcode', model='GLM-5.3',
                                workspace=self.ws('hz'), prompt_sha256='p',
                                executor='zcode', quota_store=quota, now=T0,
                                _preclaim=False)
        self.assertFalse(z['allowed'], z)
        self.assertEqual(z['reason'], 'unknown_pool', z)
        self.assertEqual(self.active(ZCODE), 0)
        # 历史 availability 诊断原样保留（不假装 healthy、不清 hold）。
        self.assertTrue(qc.zcode_blocking_rows(quota, now=T0))

    def test_cn_claimable_independently_while_zcode_held(self):
        quota = self._hold()
        # Z 的历史 hold 只是诊断，不挡 CN 独立 claim；新轮 CN 也照常最高优先。
        self.assertTrue(self.cn('cnz')['allowed'])
        self.assertEqual(self.active(CN_MAX), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
