#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BW-QODER-CN-20261010-A2 容量池 Qoder CN 补充候选离线测试（纯标准库 unittest）。

覆盖任务书明确要求、且**只有这些**新行为：
1. CN 池固定 2 槽、国内总容量 8；原 Z2/国际 Max2/国际 Flash2 容量与主力间 AUTO
   best-effort 1:1 顺序不变。CN 既可经显式 executor='qodercn' claim，也作为 AUTO 在原
   合格主力/国际 Flash 不可用或已满后的**最后补充候选**（6 满 + CN 空 → AUTO routing 选
   CN；入口已为 CN 则允许 claim；原 8 全满才物理 full）。明确指定 Qoder/ZCode/CN 的
   任务约束不互换、不扩大。
2. CN 与国际跨区域不同 workspace 可并行；同一真实 workspace 仍单写入（互斥）。
3. 跨 runtime 绝不消费旧 claim（CN token 不能被国际入口消费，反之亦然）。
4. 子进程真实结束立即释放 CN 名额（终态释放）。
5. ZCode 被持久 availability hard_hold 时 AUTO 绝不选中 Z（跳过 Z）。

绝不读写真实/共享池：每例显式临时 store；真实额度/账单不在范围内。
"""
from __future__ import annotations

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
try:
    import quota_control as qc  # noqa: E402
except Exception:  # noqa: BLE001 - 隔离树无 quota_control 时跳过 Z-hold 子例
    qc = None

T0 = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
CN = 'qodercn:DeepSeek-Flash'
ZCODE = 'zcode:GLM-5.3'
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
        return dp.select_and_claim(self.store, task_id=task, runtime='qodercn',
                                   model='DeepSeek-Flash',
                                   workspace=workspace or self.ws(task),
                                   prompt_sha256='c' * 64, executor='qodercn',
                                   now=T0, _preclaim=False)

    def active(self, pk):
        return dp.status(self.store, now=T0)['pools'][pk]['active']


class CapacityAndRotationTests(_Store):
    def test_cn_capacity_and_total_eight(self):
        self.assertEqual(dp.capacity_for(CN), 2)
        self.assertEqual(dp.DOMESTIC_TOTAL_CAPACITY, 8)
        # 原三组合容量原样不变。
        self.assertEqual(dp.capacity_for(ZCODE), 2)
        self.assertEqual(dp.capacity_for(QMAX), 2)
        self.assertEqual(dp.capacity_for(QFLASH), 2)

    def test_explicit_cn_claims_two_then_full(self):
        a = self.cn('cn-a')
        b = self.cn('cn-b')
        self.assertTrue(a['allowed'], a)
        self.assertTrue(b['allowed'], b)
        self.assertEqual(self.active(CN), 2)
        c = self.cn('cn-c')
        self.assertFalse(c['allowed'])
        self.assertEqual(c['reason'], 'capacity_full')
        self.assertEqual(self.active(CN), 2)

    def test_auto_prefers_main_then_selects_cn_then_full_at_eight(self):
        # 真实目标：主力优先 → 原 6 满 + CN 空时 AUTO 可选择 CN → 原 8 满时才确满。
        # 空池 AUTO 请求主力：先选主力（绝不先给 CN）。
        first = dp.select_and_claim(self.store, task_id='p0', runtime='zcode',
                                    model='GLM-5.3', workspace=self.ws('p0'),
                                    prompt_sha256='p', executor='auto', now=T0,
                                    _preclaim=False)
        self.assertTrue(first['allowed'], first)
        self.assertEqual(first['pool_key'], ZCODE)
        # 用明确 executor 约束填满原 6 槽（Z 共 2 + Max 2 + Flash 2），逐次核验 allowed。
        fill = [('z1', 'zcode', 'GLM-5.3', 'zcode'),
                ('q0', 'qoder', 'Qwen3.8-Max', 'qoder'),
                ('q1', 'qoder', 'Qwen3.8-Max', 'qoder'),
                ('fl0', 'qoder', 'Qwen3.8-Flash', 'qoder'),
                ('fl1', 'qoder', 'Qwen3.8-Flash', 'qoder')]
        for tid, rt, md, ex in fill:
            r = dp.select_and_claim(self.store, task_id=tid, runtime=rt, model=md,
                                    workspace=self.ws(tid), prompt_sha256='p',
                                    executor=ex, now=T0, _preclaim=False)
            self.assertTrue(r['allowed'], (tid, r))
        # 原 6 满、CN 仍空：domestic 未达总 8，CN active 为 0。
        st = dp.status(self.store, now=T0)
        self.assertEqual(st['domestic']['active'], 6)
        self.assertFalse(st['domestic']['full'])
        self.assertEqual(self.active(CN), 0)
        # 6 满 + CN 空：AUTO 请求主力 → routing_required 选 CN，绝不报整池 full。
        routed = dp.select_and_claim(self.store, task_id='cn-route', runtime='zcode',
                                     model='GLM-5.3', workspace=self.ws('cn-route'),
                                     prompt_sha256='p', executor='auto', now=T0,
                                     _preclaim=False)
        self.assertFalse(routed['allowed'], routed)
        self.assertTrue(routed['routing_required'], routed)
        self.assertEqual(routed['selected']['pool_key'], CN)
        self.assertFalse(routed.get('domestic_full'), routed)
        # 若调用入口已经 CN → 允许 claim：显式 executor='qodercn' 补 CN 两槽 → 总 8 满。
        self.assertTrue(self.cn('c0')['allowed'])
        self.assertTrue(self.cn('c1')['allowed'])
        st = dp.status(self.store, now=T0)
        self.assertEqual(self.active(CN), 2)
        self.assertTrue(st['domestic']['full'])
        # 原 8 全满：AUTO 请求主力 → 物理 capacity_full（不再 routing 到已满 CN）。
        full = dp.select_and_claim(self.store, task_id='all-full', runtime='zcode',
                                   model='GLM-5.3', workspace=self.ws('all-full'),
                                   prompt_sha256='p', executor='auto', now=T0,
                                   _preclaim=False)
        self.assertFalse(full['allowed'], full)
        self.assertEqual(full['reason'], 'capacity_full')
        self.assertTrue(full['domestic_full'], full)

    def test_cn_does_not_disturb_rotation_counts(self):
        # 先按 1:1 提交一次 Z、一次 Max（committed 各 1）。
        dp.select_and_claim(self.store, task_id='r-z', runtime='zcode', model='GLM-5.3',
                            workspace=self.ws('r-z'), prompt_sha256='p', now=T0,
                            _preclaim=False)
        dp.select_and_claim(self.store, task_id='r-q', runtime='qoder', model='Qwen3.8-Max',
                            workspace=self.ws('r-q'), prompt_sha256='p', now=T0,
                            _preclaim=False)
        st_before = dp.status(self.store, now=T0)['rotation']
        self.assertTrue(self.cn('r-cn')['allowed'])
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
        self.assertEqual(st['active'], 2)
        self.assertLess(st['active'], st['capacity'])  # 8 槽未满，二者共存

    def test_same_workspace_single_writer_across_runtimes(self):
        shared = self.ws('shared')
        intl = dp.select_and_claim(self.store, task_id='sw-i', runtime='qoder',
                                   model='Qwen3.8-Max', workspace=shared,
                                   prompt_sha256='p', executor='qoder', now=T0,
                                   _preclaim=False)
        self.assertTrue(intl['allowed'], intl)
        # CN 用同一真实目录 → 单写入守卫拒绝（守卫与 runtime 无关）。
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
        self.assertEqual(dp.status(self.store, now=T0)['pools'][CN]['active'], 1)

    def test_international_token_rejected_by_cn_entry(self):
        intl = dp.reserve(self.store, task_id='ic', runtime='qoder', model='Qwen3.8-Max',
                          workspace=self.ws('ic'), prompt_sha256='i' * 64,
                          now=T0, _preclaim=False)
        out = dp.consume_for_entry(self.store, task_id='ic', runtime='qodercn',
                                   model='DeepSeek-Flash', workspace=self.ws('ic'),
                                   prompt_sha256='i' * 64, claim_token=intl['token'],
                                   wrapper_pid=4321, wrapper_created='w', now=T0,
                                   executor='qodercn')
        self.assertFalse(out['allowed'], out)
        self.assertEqual(out['reason'], 'claim_invalid')


class TerminalReleaseTests(_Store):
    def test_release_on_real_terminal(self):
        cnr = self.cn('rel')
        tok = cnr['token']
        self.assertEqual(self.active(CN), 1)
        res = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertTrue(res['released'], res)
        self.assertEqual(self.active(CN), 0)

    def test_cn_counts_toward_domestic_full_for_luna_threshold(self):
        # 用明确 executor 约束逐次核验 allowed 占满原 6 槽 + 二 CN = 8 满；
        # Luna 阈值随总 8 更新（6/8 不算 full，8/8 才满）。
        fill = [('f0', 'zcode', 'GLM-5.3', 'zcode'), ('f1', 'qoder', 'Qwen3.8-Max', 'qoder'),
                ('f2', 'zcode', 'GLM-5.3', 'zcode'), ('f3', 'qoder', 'Qwen3.8-Max', 'qoder'),
                ('f4', 'qoder', 'Qwen3.8-Flash', 'qoder'),
                ('f5', 'qoder', 'Qwen3.8-Flash', 'qoder')]
        for tid, rt, md, ex in fill:
            r = dp.select_and_claim(self.store, task_id=tid, runtime=rt, model=md,
                                    workspace=self.ws(tid), prompt_sha256='p',
                                    executor=ex, now=T0, _preclaim=False)
            self.assertTrue(r['allowed'], (tid, r))
        st = dp.status(self.store, now=T0)['domestic']
        self.assertEqual(st['active'], 6)
        self.assertFalse(st['full'])  # 6/8 不算满
        self.assertTrue(self.cn('fc0')['allowed'])
        self.assertTrue(self.cn('fc1')['allowed'])
        st = dp.status(self.store, now=T0)['domestic']
        self.assertTrue(st['full'])  # 8/8 才满


class DomesticReclaimTests(_Store):
    def test_reclaim_after_eight_full_prefers_cn_not_luna(self):
        # 原 8 槽真实占满后记录真实 ask；仅释放 1 个 CN 名额 → 国内恢复空位 → claim_due
        # 原子 domestic_reclaim 复用既有 Flash→CN 候选顺序选中 CN（原先只试 Flash 会漏掉
        # 仅 CN 空的情形），绝不因未满而错误升级 Luna。
        fill = [('r0', 'zcode', 'GLM-5.3', 'zcode'),
                ('r1', 'zcode', 'GLM-5.3', 'zcode'),
                ('r2', 'qoder', 'Qwen3.8-Max', 'qoder'),
                ('r3', 'qoder', 'Qwen3.8-Max', 'qoder'),
                ('r4', 'qoder', 'Qwen3.8-Flash', 'qoder'),
                ('r5', 'qoder', 'Qwen3.8-Flash', 'qoder'),
                ('r6', 'qodercn', 'DeepSeek-Flash', 'qodercn'),
                ('r7', 'qodercn', 'DeepSeek-Flash', 'qodercn')]
        toks = {}
        for tid, rt, md, ex in fill:
            r = dp.reserve(self.store, task_id=tid, runtime=rt, model=md,
                           workspace=self.ws(tid), prompt_sha256='p', stage='s1',
                           chat_id='c1', executor=ex, now=T0, _preclaim=False)
            self.assertTrue(r['reserved'], (tid, r))
            toks[tid] = r['token']
        st = dp.status(self.store, now=T0)['domestic']
        self.assertEqual(st['active'], 8)
        self.assertTrue(st['full'])
        scope = {'task_id': 'ask-rc', 'stage': 's1', 'chat_id': 'c1',
                 'workspace': self.ws('ask-rc'), 'prompt_sha256': 'a' * 64}
        ask = dp.ask_record(self.store, task_id='ask-rc', scope=scope,
                            ask_message_id='m-ask', now=T0)
        self.assertTrue(ask['recorded'], ask)
        # 仅释放一个 CN 名额（其余 7 仍占用）→ 国内已恢复空位。
        rel = dp.finish(self.store, toks['r6'], terminal='cancelled', now=T0)
        self.assertTrue(rel['released'], rel)
        self.assertEqual(self.active(CN), 1)
        out = dp.claim_due(self.store, task_id='ask-rc', now=T0)
        self.assertTrue(out['claimed'], out)
        self.assertEqual(out['mode'], 'domestic_reclaim')
        self.assertEqual(out['pool_key'], CN)
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

    def test_auto_skips_zcode_when_hard_hold(self):
        quota = self._hold()
        # AUTO 请求 Z 但被 hard_hold → 绝不选中 Z，改道可用主力/溢出。
        r = dp.select_and_claim(self.store, task_id='h', runtime='zcode', model='GLM-5.3',
                                workspace=self.ws('h'), prompt_sha256='p',
                                executor='auto', quota_store=quota, now=T0,
                                _preclaim=False)
        self.assertFalse(r.get('allowed'))
        if r.get('routing_required'):
            self.assertNotEqual(r['selected']['pool_key'], ZCODE)
        # 显式 executor='zcode' 权威门：被阻断 → zcode_unavailable（不改道 CN/别的）。
        z = dp.select_and_claim(self.store, task_id='hz', runtime='zcode', model='GLM-5.3',
                                workspace=self.ws('hz'), prompt_sha256='p',
                                executor='zcode', quota_store=quota, now=T0,
                                _preclaim=False)
        self.assertFalse(z['allowed'])
        self.assertEqual(z['reason'], 'zcode_unavailable')

    def test_cn_claimable_independently_while_zcode_held(self):
        quota = self._hold()
        # Z 被阻断不影响 CN 独立 claim。
        self.assertTrue(self.cn('cnz')['allowed'])
        self.assertEqual(self.active(CN), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
