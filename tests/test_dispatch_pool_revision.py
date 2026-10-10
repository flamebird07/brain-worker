# -*- coding: utf-8 -*-
"""BW-GLOBAL-POOL-20261008 修订回归测试（Z1 基础 + Z2 定点补修；仅编写，本轮未运行）。

Z4 增补（BW-GLOBAL-POOL-20261008-Z4，仅编写未运行）：
- TestWorkspaceGuardZ4：同真实 workspace 单写入守卫（select/reserve/consume 显式
  token；同 task 原 token 除外；释放后恢复写入）；
- fixture 修正：不同 task 不再共享虚拟同一路径（fill 六槽按 task 后缀、_scope 按
  task 目录）——生产守卫语义不放松，只改错误 fixture。

覆盖 dispatch_pool.py 修订后的负例与竞争语义（全部使用真实临时 SQLite，绝不读默认库）：
- 空库 reserve Flash / Luna 拒绝（统一国内策略，无生产后门）；
- 合法 1:1 填满六槽，第七个拒绝；
- 重复/并发 consume 同一 token 只有一个 allowed（CAS，一槽不双 Popen）；
  auto consume 记录 wrapper 身份；显式 token 漂移（stage/chat）拒绝；
- bind_child 单次绑定，不覆盖已有 child；Z2：有 wrapper 时必须当前真实调用者一致；
- 活 child 直接 finish 拒绝；child 死（真实 Windows 死 PID created=None）释放；
  重复释放幂等；Z2：unknown/无 child 的 Popen-bind 窗口只有 owner 一致才可
  start_failed/cancelled，reserved 未消费的取消仍合法；
- reconcile：wrapper 活 child 死 → 释放；child 活且出生匹配 → 保持；child 活出生
  漂移/未知（PID 复用嫌疑）→ unknown 不抢占；wrapper 死无 child → unknown 不凭租期放行；
- ask_record：缺 ask_message_id / 缺 scope / 六未满 / 缩短 deadline 全部拒绝；合法
  票据固定 300 秒；重启不重置；重复 reply 幂等；
- claim_due：国内空位原子回收恰好一方成功；六满到期 claim Luna；不同 task Luna 无
  数量 cap；重复 claim-due / launch_unknown 不重启；同 task native 启动后拒绝国内启动；
- Z2-1：主力分配先按持久 committed 选主力；Z 释放后再请求 Z → routing Max，重启计数
  保持；空闲连续任务 Z/Max 交替；
- Z2-2：两主力满时请求 Z/Max 在 select/reserve 两条路径都 routing_required 到 Flash，
  绝无 pool_key 与 runtime/model 不一致的偷换落库；实际请求 Flash 且主力满才 claim；
- Z2-3：同 task 国内 claim 同事务取消旧 pending 票（原票据到期不得再 Luna）；
  用户回复 external_agent/cancel 阻断国内启动；
- Z2-4/Z2-5：claim_due 先看国内空位（30 秒释放也立即 domestic_reclaim），六满才判
  300 秒（299 拒/300 Luna）；回收 attempt 写回票据原 scope 真实绑定字段，原 scope
  consume 成功、字段漂移拒绝；scope 不一致在 Luna 分支同样拒绝。

Z3（BW-GLOBAL-POOL-20261008-Z3）针对主脑独立验收（46 tests / 5 failures + 3 errors，
exit 1）的定点修复，均为实质/fixture 问题，未放松任何绑定/轮转/容量语义：
- _insert_claim wrapper 分支 15 列对 15 VALUES（生产 auto-consume OperationalError）；
- consume 严格 stage/chat 校验后，fixture reserve 必须带原 stage/chat（不放松绑定）；
- 多票 fill_domestic 使用不同 token 前缀（UNIQUE 冲突是 fixture 重复 token）；
- sc2 票据在确实六满时询问（先补 refill 再 ask）；
- reconcile 无 wrapper 无 child（含 running）明确 held_unknown；
- native-on-Luna 防重理由优先判（task_already_on_luna，语义仍拒绝）；
- 1:1 轮转下后续请求按选中组合（Max），且 reserve 返回先核验再取 token。

运行登记（由 Qoder 在原登记命令下执行，本文件作者未运行）：
  cd <repo-root>
  python -m unittest tests.test_dispatch_pool_revision -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import dispatch_pool as dp  # noqa: E402

T0 = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)


def _probe(state, created):
    return lambda pid: {'pid': pid, 'state': state(pid) if callable(state) else state,
                        'created': created(pid) if callable(created) else created}


class TempStoreMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-dispatch-test-')
        self.store = str(Path(self._tmp.name) / 'pool.sqlite3')
        self.assertNotEqual(str(self.store), str(dp.default_store_path()))
        # 显式确保没有意外读到默认库/env 覆盖。
        self.saved_env = os.environ.pop('BRAIN_WORKER_DISPATCH_STORE', None)

    def tearDown(self):
        if self.saved_env is not None:
            os.environ['BRAIN_WORKER_DISPATCH_STORE'] = self.saved_env
        self._tmp.cleanup()

    # ---- 夹具 ------------------------------------------------------------
    def seed_attempt(self, token, task_id, pool, state='running', child_pid=None,
                     child_created=None, wrapper_pid=None, wrapper_created=None):
        with closing(dp.connect(self.store)) as conn:
            runtime, model = pool.split(':', 1)
            conn.execute(
                'INSERT INTO attempts(token, task_id, runtime, model, pool_key, state, '
                'child_pid, child_created, wrapper_pid, wrapper_created) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (token, task_id, runtime, model, pool, state, child_pid, child_created,
                 wrapper_pid, wrapper_created))

    def fill_domestic(self, prefix='seed', n=8):
        pools = ['zcode:GLM-5.3'] * 2 + ['qoder:Qwen3.8-Max'] * 2 \
            + ['qoder:Qwen3.8-Flash'] * 2 + ['qodercn:DeepSeek-Flash'] * 2
        for i in range(n):
            self.seed_attempt(f'{prefix}-{i}', f'{prefix}-task-{i}', pools[i])

    def active_count(self, pool):
        with closing(dp.connect(self.store)) as conn:
            return dp._count_active(conn, pool)

    def row(self, table, key, value):
        with closing(dp.connect(self.store)) as conn:
            r = conn.execute(f'SELECT * FROM {table} WHERE {key}=?', (value,)).fetchone()
            return dict(r) if r is not None else None


class TestUnifiedPolicy(TempStoreMixin, unittest.TestCase):
    def test_reserve_flash_rejected_on_empty_store(self):
        out = dp.reserve(self.store, task_id='t', runtime='qoder', model='Qwen3.8-Flash',
                         workspace='C:/w', prompt_sha256='a' * 64, now=T0,
                         _preclaim=False)
        self.assertFalse(out['allowed'])
        self.assertTrue(out['routing_required'])
        self.assertEqual(out['selected']['pool_key'], 'zcode:GLM-5.3')

    def test_reserve_luna_rejected(self):
        out = dp.reserve(self.store, task_id='t', runtime='luna', model='native',
                         workspace='C:/w', prompt_sha256='a' * 64, now=T0,
                         _preclaim=False)
        self.assertFalse(out['allowed'])
        self.assertEqual(out['reason'], 'luna_requires_ticket')

    def test_select_and_claim_luna_rejected(self):
        out = dp.select_and_claim(self.store, task_id='t', runtime='luna', model='native',
                                  workspace='C:/w', prompt_sha256='a' * 64, now=T0,
                                  _preclaim=False)
        self.assertFalse(out['allowed'])
        self.assertEqual(out['reason'], 'luna_requires_ticket')

    def test_fill_six_legit_then_seventh_rejected(self):
        # 1:1 主力：交替请求两主力各得 2。Z4：不同 task 用独立 workspace 目录（真实
        # 单写入守卫下不同任务共享 C:/w 会被正确拒绝，fixture 不得再虚拟同一路径）。
        for i, (rt, md) in enumerate([('zcode', 'GLM-5.3'), ('qoder', 'Qwen3.8-Max'),
                                      ('zcode', 'GLM-5.3'), ('qoder', 'Qwen3.8-Max')]):
            out = dp.select_and_claim(self.store, task_id=f't{i}', runtime=rt, model=md,
                                      workspace=f'C:/w/t{i}', prompt_sha256=f'{i}' * 64,
                                      now=T0, _preclaim=False)
            self.assertTrue(out['allowed'], out)
        # 主力满：请求 Flash 被 select（溢出合法），再填 2 个。
        for i in (4, 5):
            out = dp.select_and_claim(self.store, task_id=f't{i}', runtime='qoder',
                                      model='Qwen3.8-Flash', workspace=f'C:/w/t{i}',
                                      prompt_sha256=f'{i}' * 64, now=T0, _preclaim=False)
            self.assertTrue(out['allowed'], out)
            self.assertEqual(out['pool_key'], 'qoder:Qwen3.8-Flash')
        self.assertEqual(dp.status(self.store, now=T0)['domestic']['active'], 6)
        # 七：AUTO 主力+溢出组合已全满 → 不再报整池 full，而是 routing 到仍有空位的 CN
        # 补充候选（旧 6 满 + CN 空 = routing CN），且此时并未真正占槽。
        out = dp.select_and_claim(self.store, task_id='t6', runtime='zcode',
                                  model='GLM-5.3', workspace='C:/w/t6',
                                  prompt_sha256='6' * 64, now=T0, _preclaim=False)
        self.assertFalse(out['allowed'], out)
        self.assertTrue(out['routing_required'], out)
        self.assertEqual(out['selected']['pool_key'], 'qodercn:DeepSeek-Flash')
        self.assertFalse(out['domestic_full'])
        # 八/九：经公开 API 把 CN 2 槽也填满（AUTO 入口点名 CN 且有空位即 claim），每次
        # assert allowed；不造 PID、不跳测试。
        for i in (7, 8):
            cn = dp.select_and_claim(self.store, task_id=f't{i}', runtime='qodercn',
                                     model='DeepSeek-Flash', workspace=f'C:/w/t{i}',
                                     prompt_sha256=f'{i}' * 64, now=T0, _preclaim=False)
            self.assertTrue(cn['allowed'], cn)
            self.assertEqual(cn['pool_key'], 'qodercn:DeepSeek-Flash')
        self.assertEqual(dp.status(self.store, now=T0)['domestic']['active'], 8)
        # 第九次（八槽全满）：AUTO 任意国内组合才 capacity_full 且 domestic_full。
        out = dp.select_and_claim(self.store, task_id='t9', runtime='zcode',
                                  model='GLM-5.3', workspace='C:/w/t9',
                                  prompt_sha256='9' * 64, now=T0, _preclaim=False)
        self.assertFalse(out['allowed'], out)
        self.assertEqual(out['reason'], 'capacity_full')
        self.assertTrue(out['domestic_full'])

    def test_duplicate_task_rejected(self):
        kw = dict(workspace='C:/w', prompt_sha256='a' * 64, now=T0, _preclaim=False)
        self.assertTrue(dp.select_and_claim(self.store, task_id='dup', runtime='zcode',
                                            model='GLM-5.3', **kw)['allowed'])
        out = dp.select_and_claim(self.store, task_id='dup', runtime='qoder',
                                  model='Qwen3.8-Max', **kw)
        self.assertFalse(out['allowed'])
        self.assertEqual(out['reason'], 'duplicate_task_in_flight')


class TestConsume(TempStoreMixin, unittest.TestCase):
    def _reserve_one(self, task_id='ct'):
        # Z3 修复：consume 现在做严格 stage/chat 绑定校验，reserve 必须带原 stage/chat，
        # 否则合法 consume 会因 None != 's1' 被误判漂移（放松绑定不可取，补齐原字段）。
        return dp.reserve(self.store, task_id=task_id, runtime='zcode', model='GLM-5.3',
                          workspace='C:/w', prompt_sha256='a' * 64, stage='s1',
                          chat_id='c1', now=T0, _preclaim=False)

    def test_double_consume_single_winner(self):
        tok = self._reserve_one()['token']
        kw = dict(task_id='ct', runtime='zcode', model='GLM-5.3', workspace='C:/w',
                  prompt_sha256='a' * 64, stage='s1', chat_id='c1',
                  wrapper_pid=4321, wrapper_created='w-created-1', now=T0)
        first = dp.consume_for_entry(self.store, claim_token=tok, **kw)
        second = dp.consume_for_entry(self.store, claim_token=tok, **kw)
        self.assertTrue(first['allowed'])
        self.assertFalse(second['allowed'])
        self.assertEqual(second['reason'], 'consume_conflict')
        row = self.row('attempts', 'token', tok)
        self.assertEqual(row['state'], 'running')
        self.assertEqual(row['wrapper_pid'], 4321)
        self.assertEqual(row['wrapper_created'], 'w-created-1')

    def test_explicit_token_drift_stage_chat_rejected(self):
        tok = self._reserve_one()
        tok = tok['token']
        base = dict(task_id='ct', runtime='zcode', model='GLM-5.3', workspace='C:/w',
                    prompt_sha256='a' * 64, wrapper_pid=99, now=T0)
        out = dp.consume_for_entry(self.store, claim_token=tok, stage='OTHER', **base)
        self.assertFalse(out['allowed'])
        self.assertEqual(out['reason'], 'claim_invalid')
        out = dp.consume_for_entry(self.store, claim_token=tok, chat_id='OTHER', **base)
        self.assertFalse(out['allowed'])
        self.assertEqual(out['reason'], 'claim_invalid')
        # token 从未被消费，状态仍 reserved。
        self.assertEqual(self.row('attempts', 'token', tok)['state'], 'reserved')

    def test_auto_consume_records_wrapper_identity(self):
        out = dp.consume_for_entry(self.store, task_id='auto1', runtime='zcode',
                                   model='GLM-5.3', workspace='C:/w',
                                   prompt_sha256='b' * 64, stage='s', chat_id='c',
                                   wrapper_pid=777, wrapper_created='auto-created',
                                   now=T0)
        self.assertTrue(out['allowed'])
        row = self.row('attempts', 'token', out['token'])
        self.assertEqual(row['state'], 'running')
        self.assertEqual(row['wrapper_pid'], 777)
        self.assertEqual(row['wrapper_created'], 'auto-created')

    def test_bind_child_single_shot(self):
        tok = self._reserve_one()['token']
        first = dp.bind_child(self.store, tok, 111, child_created='cc1', now=T0)
        second = dp.bind_child(self.store, tok, 222, child_created='cc2', now=T0)
        self.assertTrue(first['bound'])
        self.assertFalse(second['bound'])
        row = self.row('attempts', 'token', tok)
        self.assertEqual(row['child_pid'], 111)
        self.assertEqual(row['child_created'], 'cc1')


class TestFinish(TempStoreMixin, unittest.TestCase):
    def _with_child(self, child_pid=555, child_created='cc'):
        dp.reserve(self.store, task_id='ft', runtime='zcode', model='GLM-5.3',
                   workspace='C:/w', prompt_sha256='a' * 64, now=T0, _preclaim=False)
        with closing(dp.connect(self.store)) as conn:
            conn.execute("UPDATE attempts SET state='running', child_pid=?, "
                         'child_created=? WHERE task_id=?', (child_pid, child_created, 'ft'))
        return self.row('attempts', 'task_id', 'ft')['token']

    def test_finish_live_child_refused(self):
        tok = self._with_child()
        out = dp.finish(self.store, tok, terminal='finished', now=T0,
                        prober=_probe('alive', 'cc'))
        self.assertFalse(out['released'])
        self.assertEqual(out.get('reason'), 'child_alive')
        self.assertEqual(self.row('attempts', 'token', tok)['state'], 'running')

    def test_finish_dead_child_created_none_released(self):
        # 真实 Windows 死 PID created=None 属正常。
        tok = self._with_child()
        out = dp.finish(self.store, tok, terminal='finished', now=T0,
                        prober=_probe('dead', None))
        self.assertTrue(out['released'])
        self.assertEqual(self.row('attempts', 'token', tok)['state'], 'finished')

    def test_finish_unknown_child_held_not_start_failed(self):
        tok = self._with_child()
        out = dp.finish(self.store, tok, terminal='start_failed', now=T0,
                        prober=_probe('unknown', None))
        self.assertFalse(out['released'])
        self.assertEqual(self.row('attempts', 'token', tok)['state'], 'unknown')

    def test_finish_without_child_only_start_failed_or_cancelled(self):
        dp.reserve(self.store, task_id='nc', runtime='zcode', model='GLM-5.3',
                   workspace='C:/w', prompt_sha256='a' * 64, now=T0, _preclaim=False)
        tok = self.row('attempts', 'task_id', 'nc')['token']
        out = dp.finish(self.store, tok, terminal='finished', now=T0)
        self.assertFalse(out['released'])
        self.assertEqual(out.get('reason'), 'no_child_terminal_unverified')
        out = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertTrue(out['released'])

    def test_finish_idempotent_and_new_attempt_unaffected(self):
        tok = self._with_child()
        self.assertTrue(dp.finish(self.store, tok, terminal='finished', now=T0,
                                  prober=_probe('dead', None))['released'])
        again = dp.finish(self.store, tok, terminal='finished', now=T0,
                          prober=_probe('dead', None))
        self.assertFalse(again['released'])
        self.assertTrue(again.get('idempotent'))
        # 释放后可再 claim（新 attempt 不受旧 token 影响）。Z3 修复：ft 的 reserve 已把
        # committed_zcode 计到 1，按 1:1 轮转本请求应走 Max，不能因 Z 池已空就回 Z。
        out = dp.select_and_claim(self.store, task_id='nc2', runtime='qoder',
                                  model='Qwen3.8-Max', workspace='C:/w',
                                  prompt_sha256='c' * 64, now=T0, _preclaim=False)
        self.assertTrue(out['allowed'], out)
        self.assertEqual(out['pool_key'], 'qoder:Qwen3.8-Max')


class TestReconcile(TempStoreMixin, unittest.TestCase):
    def _mk(self, token, wrapper_state, child_pid, child_created, recorded_created):
        self.seed_attempt(token, f'task-{token}', 'zcode:GLM-5.3', state='running',
                          child_pid=child_pid, child_created=recorded_created,
                          wrapper_pid=900 if wrapper_state else None,
                          wrapper_created='w' if wrapper_state else None)
        return wrapper_state

    def test_wrapper_alive_child_dead_is_retained_not_auto_released(self):
        # BW-AVAILABILITY-20261009-B5 Gap3（安全强化，非弱化旧断言）：child 死亡不足以
        # 自动释放。原 wrapper 仍活（owner 可能还在写终态/接续）时，任何 auto/public
        # reclaim 路径都必须保留占位——否则同 workspace 新 writer 会在原 owner 空窗被放行。
        # 只有原身份齐备且 child+wrapper 双方确认死亡、创建身份匹配才自动 reconciled_exit；
        # owner 显式 finish（亲证已绑定 child 真实退出）仍正常释放。
        self._mk('a', wrapper_state=True, child_pid=101, child_created='c101',
                 recorded_created='c101')
        prober = lambda pid: ({'pid': pid, 'state': 'alive', 'created': 'w'}
                              if pid == 900 else
                              {'pid': pid, 'state': 'dead', 'created': None})
        out = dp.reconcile(self.store, now=T0, prober=prober)
        self.assertNotIn('a', out['released'])
        self.assertEqual(self.row('attempts', 'token', 'a')['state'], 'unknown')
        # owner 显式 finish：已绑定 child 探针确认已退出 → 仍可释放（不影响原释放能力）。
        fin = dp.finish(self.store, 'a', terminal='start_failed', now=T0,
                        prober=lambda pid: {'pid': pid, 'state': 'dead', 'created': None},
                        wrapper_pid=900, wrapper_created='w')
        self.assertTrue(fin['released'], fin)
        self.assertEqual(self.row('attempts', 'token', 'a')['state'], 'start_failed')

    def test_dead_child_no_wrapper_identity_is_retained_not_auto_released(self):
        # BW-AVAILABILITY-20261009-B5 Gap3：缺原记录 wrapper 身份时，即便 child 已死也
        # 不得 auto-release（无身份无法证明 owner 调用已结束）→ 保守 unknown 保留。
        # 真实死 PID 探针回读 created=None 属正常，但“缺身份”仍触发保留；owner 显式
        # finish（亲证 child 真退出）仍是合法释放出口。
        self._mk('b', wrapper_state=False, child_pid=102, child_created='c102',
                 recorded_created='c102')
        out = dp.reconcile(self.store, now=T0,
                           prober=_probe('dead', None))
        self.assertNotIn('b', out['released'])
        self.assertEqual(self.row('attempts', 'token', 'b')['state'], 'unknown')
        fin = dp.finish(self.store, 'b', terminal='start_failed', now=T0,
                        prober=lambda pid: {'pid': pid, 'state': 'dead', 'created': None})
        self.assertTrue(fin['released'], fin)
        self.assertEqual(self.row('attempts', 'token', 'b')['state'], 'start_failed')

    def test_both_dead_full_identity_reconciled_exit(self):
        # Gap3 唯一自动释放出口：原身份齐备（wrapper 记录在案）且 child+wrapper 双方
        # 确认死亡、创建身份匹配 → reconcile 自动 reconciled_exit（证明保留规则不过度收紧）。
        self._mk('g', wrapper_state=True, child_pid=106, child_created='c106',
                 recorded_created='c106')
        prober = lambda pid: ({'pid': pid, 'state': 'dead', 'created': 'w'}
                              if pid == 900 else
                              {'pid': pid, 'state': 'dead', 'created': None})
        out = dp.reconcile(self.store, now=T0, prober=prober)
        self.assertIn('g', out['released'])
        self.assertEqual(self.row('attempts', 'token', 'g')['state'], 'reconciled_exit')

    def test_child_alive_birth_match_kept(self):
        self._mk('c', wrapper_state=True, child_pid=103, child_created='c103',
                 recorded_created='c103')
        prober = lambda pid: ({'pid': pid, 'state': 'alive', 'created': 'w'}
                              if pid == 900 else
                              {'pid': pid, 'state': 'alive', 'created': 'c103'})
        out = dp.reconcile(self.store, now=T0, prober=prober)
        self.assertNotIn('c', out['released'])
        self.assertEqual(self.row('attempts', 'token', 'c')['state'], 'running')

    def test_child_alive_pid_reuse_held_unknown(self):
        # child PID 现在活着但出生时刻与记录不同（PID 复用）→ unknown 不抢占。
        self._mk('d', wrapper_state=True, child_pid=104, child_created='c104',
                 recorded_created='c104')
        prober = lambda pid: ({'pid': pid, 'state': 'alive', 'created': 'w'}
                              if pid == 900 else
                              {'pid': pid, 'state': 'alive', 'created': 'REUSED'})
        out = dp.reconcile(self.store, now=T0, prober=prober)
        self.assertNotIn('d', out['released'])
        self.assertEqual(self.row('attempts', 'token', 'd')['state'], 'unknown')

    def test_child_alive_created_unknown_held(self):
        self._mk('e', wrapper_state=True, child_pid=105, child_created='c105',
                 recorded_created='c105')
        prober = lambda pid: ({'pid': pid, 'state': 'alive', 'created': 'w'}
                              if pid == 900 else
                              {'pid': pid, 'state': 'alive', 'created': None})
        out = dp.reconcile(self.store, now=T0, prober=prober)
        self.assertNotIn('e', out['released'])
        self.assertEqual(self.row('attempts', 'token', 'e')['state'], 'unknown')

    def test_wrapper_dead_no_child_held_unknown(self):
        self._mk('f', wrapper_state=False, child_pid=None, child_created=None,
                 recorded_created=None)
        out = dp.reconcile(self.store, now=T0, prober=_probe('dead', None))
        self.assertNotIn('f', out['released'])
        self.assertEqual(self.row('attempts', 'token', 'f')['state'], 'unknown')


def _scope(task_id='at', workspace=None, prompt_sha256='p' * 64,
           stage='s1', chat_id='c1'):
    # Z4：默认按 task 后缀独立目录——不同 task 的票据/裁决不得共享同一虚拟 workspace
    # （同真实 workspace 单写入守卫会正确拒绝，fixture 不放松生产守卫）。
    return {'task_id': task_id, 'stage': stage, 'chat_id': chat_id,
            'workspace': workspace or f'C:/w/{task_id}',
            'prompt_sha256': prompt_sha256}


class TestAskTicket(TempStoreMixin, unittest.TestCase):
    def test_ask_missing_message_id_rejected(self):
        self.fill_domestic()
        out = dp.ask_record(self.store, task_id='at', scope=_scope(),
                            ask_message_id=None, now=T0)
        self.assertFalse(out['recorded'])

    def test_ask_missing_scope_rejected(self):
        self.fill_domestic()
        out = dp.ask_record(self.store, task_id='at', scope=None,
                            ask_message_id='msg-1', now=T0)
        self.assertFalse(out['recorded'])

    def test_ask_domestic_not_full_rejected(self):
        out = dp.ask_record(self.store, task_id='at', scope=_scope(),
                            ask_message_id='msg-1', now=T0)
        self.assertFalse(out['recorded'])
        self.assertTrue(any('not full' in r for r in out['reasons']), out['reasons'])

    def test_ask_shortened_deadline_rejected(self):
        self.fill_domestic()
        out = dp.ask_record(self.store, task_id='at', scope=_scope(),
                            ask_message_id='msg-1', now=T0, deadline_seconds=10)
        self.assertFalse(out['recorded'])
        self.assertTrue(any('shorten' in r for r in out['reasons']), out['reasons'])

    def test_ask_valid_fixed_300_and_restart_keeps_deadline(self):
        self.fill_domestic()
        out = dp.ask_record(self.store, task_id='at', scope=_scope(),
                            ask_message_id='msg-1', now=T0)
        self.assertTrue(out['recorded'])
        self.assertEqual(out['deadline_seconds'], 300)
        self.assertEqual(out['deadline_utc'],
                         (T0 + timedelta(seconds=300)).isoformat())
        # 重启（重复 ask）不重置 deadline。
        again = dp.ask_record(self.store, task_id='at', scope=_scope(),
                              ask_message_id='msg-1', now=T0 + timedelta(minutes=10))
        self.assertFalse(again['recorded'])
        self.assertEqual(again['deadline_utc'], out['deadline_utc'])

    def test_reply_idempotent(self):
        self.fill_domestic()
        dp.ask_record(self.store, task_id='at', scope=_scope(), ask_message_id='msg-1',
                      now=T0)
        first = dp.reply(self.store, task_id='at', choice='cancel', now=T0)
        second = dp.reply(self.store, task_id='at', choice='luna', now=T0)
        self.assertTrue(first['recorded'])
        self.assertFalse(second['recorded'])
        self.assertEqual(self.row('luna_tickets', 'task_id', 'at')['reply_choice'],
                         'cancel')


class TestClaimDue(TempStoreMixin, unittest.TestCase):
    def _ticket(self, task_id='dt'):
        # Z3 修复：多张票各自 fill 时必须用不同 token 前缀，否则第二张票的六满基线
        # 因 attempts.token UNIQUE 冲突直接 IntegrityError。
        self.fill_domestic(prefix=f'seed-{task_id}')
        self.assertTrue(dp.ask_record(self.store, task_id=task_id, scope=_scope(task_id),
                                      ask_message_id='msg-1', now=T0)['recorded'])

    def test_due_domestic_reclaim_exactly_one_winner(self):
        self._ticket()
        due = T0 + timedelta(seconds=301)
        # 六满记录票据后，真实释放一个国内槽（seed 无 child：cancelled 即确认取消释放）。
        self.assertTrue(dp.finish(self.store, 'seed-dt-0', terminal='cancelled',
                                  now=due)['released'])
        first = dp.claim_due(self.store, task_id='dt', now=due,
                             scope=json.dumps(_scope('dt')), _preclaim=False)
        second = dp.claim_due(self.store, task_id='dt', now=due,
                              scope=json.dumps(_scope('dt')), _preclaim=False)
        self.assertTrue(first['claimed'], first)
        self.assertEqual(first['mode'], 'domestic_reclaim')
        self.assertIn(first['pool_key'], dp.MAIN_FORCE_KEYS)
        self.assertIsNotNone(first['token'])
        self.assertFalse(second['claimed'])  # 票据已 cancelled，槽只此一方。
        ticket = self.row('luna_tickets', 'task_id', 'dt')
        self.assertEqual(ticket['state'], 'cancelled')
        self.assertEqual(self.active_count('luna:native'), 0)
        # 重新 claim 的 attempt 属于原 task，占真实国内容量。
        self.assertEqual(self.row('attempts', 'token', first['token'])['task_id'], 'dt')

    def test_due_full_claim_luna_no_cap_across_tasks(self):
        self._ticket('lt1')
        self._ticket('lt2')
        due = T0 + timedelta(seconds=301)
        a = dp.claim_due(self.store, task_id='lt1', now=due, _preclaim=False)
        b = dp.claim_due(self.store, task_id='lt2', now=due, _preclaim=False)
        self.assertTrue(a['claimed'] and b['claimed'])
        self.assertEqual(a['mode'], 'luna')
        self.assertEqual(self.active_count('luna:native'), 2)  # 不同 task 无数量 cap。

    def test_due_repeat_claim_no_restart(self):
        self._ticket('lt3')
        due = T0 + timedelta(seconds=301)
        first = dp.claim_due(self.store, task_id='lt3', now=due, _preclaim=False)
        self.assertTrue(first['claimed'])
        dup = dp.claim_due(self.store, task_id='lt3', now=due, _preclaim=False)
        self.assertFalse(dup['claimed'])
        self.assertTrue(dup['already_claimed'])
        self.assertEqual(self.active_count('luna:native'), 1)
        # launch_unknown 后同样绝不重启。
        self.assertTrue(dp.mark_launch_unknown(self.store, task_id='lt3', now=due)
                        ['recorded'])
        again = dp.claim_due(self.store, task_id='lt3', now=due, _preclaim=False)
        self.assertFalse(again['claimed'])
        self.assertEqual(again['launch_state'], 'launch_unknown')
        self.assertEqual(self.active_count('luna:native'), 1)

    def test_not_due_and_replied_rejected(self):
        self._ticket('lt4')
        early = dp.claim_due(self.store, task_id='lt4', now=T0 + timedelta(seconds=10),
                             _preclaim=False)
        self.assertFalse(early['claimed'])
        self.assertTrue(early['not_due'])
        due = T0 + timedelta(seconds=301)
        dp.reply(self.store, task_id='lt4', choice='external_agent', now=T0)
        after = dp.claim_due(self.store, task_id='lt4', now=due, _preclaim=False)
        self.assertFalse(after['claimed'])

    def test_reply_luna_is_authorized_immediate_path(self):
        self._ticket('lt5')
        dp.reply(self.store, task_id='lt5', choice='luna', now=T0)
        out = dp.claim_due(self.store, task_id='lt5', now=T0 + timedelta(seconds=1),
                           _preclaim=False)
        self.assertTrue(out['claimed'])
        self.assertEqual(out['mode'], 'luna')

    def test_native_started_blocks_domestic_start(self):
        self._ticket('lt6')
        due = T0 + timedelta(seconds=301)
        self.assertTrue(dp.claim_due(self.store, task_id='lt6', now=due,
                                     _preclaim=False)['claimed'])
        out = dp.select_and_claim(self.store, task_id='lt6', runtime='zcode',
                                  model='GLM-5.3', workspace='C:/w',
                                  prompt_sha256='z' * 64, now=due, _preclaim=False)
        self.assertFalse(out['allowed'])
        self.assertEqual(out['reason'], 'task_already_on_luna')

    def test_agent_id_recorded_once_claimed(self):
        self._ticket('lt7')
        due = T0 + timedelta(seconds=301)
        dp.claim_due(self.store, task_id='lt7', now=due, _preclaim=False)
        out = dp.record_agent_id(self.store, task_id='lt7', agent_id='agent-abc',
                                 now=due)
        self.assertTrue(out['recorded'])
        self.assertEqual(self.row('luna_tickets', 'task_id', 'lt7')['agent_id'],
                         'agent-abc')


class TestMainForceRotationZ2(TempStoreMixin, unittest.TestCase):
    """Z2-1：主力分配必须先按持久 committed 计数选择，不能因本池有空绕过 1:1。"""

    def test_z_release_then_z_again_routes_to_max_and_survives_restart(self):
        kw = dict(workspace='C:/w', prompt_sha256='a' * 64, now=T0, _preclaim=False)
        first = dp.select_and_claim(self.store, task_id='r1', runtime='zcode',
                                    model='GLM-5.3', **kw)
        self.assertTrue(first['allowed'])
        self.assertEqual(first['pool_key'], 'zcode:GLM-5.3')
        # 真实终态释放（reserved 无 wrapper，cancelled 即确认取消）。
        self.assertTrue(dp.finish(self.store, first['token'], terminal='cancelled',
                                  now=T0)['released'])
        # 再请求 Z：committed_z=1 > committed_q=0 → routing_required 到 Max，
        # 不能因为 Z 池现在有空就直接再 claim Z。
        again = dp.select_and_claim(self.store, task_id='r2', runtime='zcode',
                                    model='GLM-5.3', prompt_sha256='b' * 64,
                                    workspace='C:/w', now=T0, _preclaim=False)
        self.assertFalse(again['allowed'])
        self.assertTrue(again['routing_required'])
        self.assertEqual(again['selected']['pool_key'], 'qoder:Qwen3.8-Max')
        # “重启”（所有调用本就各自新连接；再显式读一次 status）后计数仍在。
        st = dp.status(self.store, now=T0)
        self.assertEqual(st['rotation']['committed_zcode'], 1)
        self.assertEqual(st['rotation']['committed_qoder'], 0)

    def test_idle_consecutive_z_max_alternate(self):
        kw = dict(workspace='C:/w', prompt_sha256='a' * 64, now=T0, _preclaim=False)
        a = dp.select_and_claim(self.store, task_id='a1', runtime='zcode',
                                model='GLM-5.3', **kw)
        self.assertEqual(a['pool_key'], 'zcode:GLM-5.3')
        b = dp.select_and_claim(self.store, task_id='a2', runtime='zcode',
                                model='GLM-5.3', prompt_sha256='b' * 64,
                                workspace='C:/w/a2', now=T0, _preclaim=False)
        self.assertFalse(b['allowed'])
        self.assertEqual(b['selected']['pool_key'], 'qoder:Qwen3.8-Max')


class TestOverflowNotSubstitutedZ2(TempStoreMixin, unittest.TestCase):
    """Z2-2：两主力满时请求 Z/Max 必须 routing_required，绝不偷换成 Flash 落库。"""

    def _fill_mains(self):
        for i, pool in enumerate(['zcode:GLM-5.3'] * 2 + ['qoder:Qwen3.8-Max'] * 2):
            self.seed_attempt(f'm-{i}', f'm-task-{i}', pool)

    def test_select_and_claim_main_full_routes_to_flash_no_substitution(self):
        self._fill_mains()
        out = dp.select_and_claim(self.store, task_id='ov1', runtime='zcode',
                                  model='GLM-5.3', workspace='C:/w',
                                  prompt_sha256='c' * 64, now=T0, _preclaim=False)
        self.assertFalse(out['allowed'])
        self.assertTrue(out['routing_required'])
        self.assertEqual(out['selected']['pool_key'], 'qoder:Qwen3.8-Flash')
        # 原主力计数绝不 >2，且没有任何 zcode runtime 被记到 Flash 池。
        self.assertEqual(self.active_count('zcode:GLM-5.3'), 2)
        self.assertEqual(self.active_count('qoder:Qwen3.8-Max'), 2)
        with closing(dp.connect(self.store)) as conn:
            rows = conn.execute('SELECT runtime, model, pool_key FROM attempts '
                                "WHERE pool_key='qoder:Qwen3.8-Flash'").fetchall()
        self.assertEqual(rows, [])

    def test_reserve_main_full_routes_to_flash_no_substitution(self):
        self._fill_mains()
        out = dp.reserve(self.store, task_id='ov2', runtime='qoder', model='Qwen3.8-Max',
                         workspace='C:/w', prompt_sha256='d' * 64, now=T0,
                         _preclaim=False)
        self.assertFalse(out['allowed'])
        self.assertTrue(out['routing_required'])
        self.assertEqual(out['selected']['pool_key'], 'qoder:Qwen3.8-Flash')
        self.assertEqual(self.active_count('qoder:Qwen3.8-Max'), 2)
        with closing(dp.connect(self.store)) as conn:
            rows = conn.execute('SELECT runtime, model, pool_key FROM attempts '
                                "WHERE pool_key='qoder:Qwen3.8-Flash'").fetchall()
        self.assertEqual(rows, [])

    def test_real_flash_request_claimed_when_mains_full(self):
        self._fill_mains()
        out = dp.select_and_claim(self.store, task_id='ov3', runtime='qoder',
                                  model='Qwen3.8-Flash', workspace='C:/w',
                                  prompt_sha256='e' * 64, now=T0, _preclaim=False)
        self.assertTrue(out['allowed'])
        self.assertEqual(out['pool_key'], 'qoder:Qwen3.8-Flash')
        row = self.row('attempts', 'token', out['token'])
        self.assertEqual(row['pool_key'], f"{row['runtime']}:{row['model']}")


class TestDomesticRescueCancelsTicketZ2(TempStoreMixin, unittest.TestCase):
    """Z2-3：同 task 国内救回须同事务取消旧 pending 票；external_agent/cancel 阻断。"""

    def test_domestic_claim_cancels_pending_and_blocks_later_luna(self):
        self.fill_domestic()
        self.assertTrue(dp.ask_record(self.store, task_id='rt', scope=_scope('rt'),
                                      ask_message_id='msg-1', now=T0)['recorded'])
        self.assertTrue(dp.finish(self.store, 'seed-0', terminal='cancelled',
                                  now=T0 + timedelta(seconds=30))['released'])
        out = dp.select_and_claim(self.store, task_id='rt', runtime='zcode',
                                  model='GLM-5.3', workspace='C:/w',
                                  prompt_sha256='p' * 64, now=T0 + timedelta(seconds=31),
                                  _preclaim=False)
        self.assertTrue(out['allowed'], out)
        ticket = self.row('luna_tickets', 'task_id', 'rt')
        self.assertEqual(ticket['state'], 'cancelled')
        # 原任务真实终态后，其它 task 把空位填满；原票据到期不得再 Luna。
        self.assertTrue(dp.finish(self.store, out['token'], terminal='cancelled',
                                  now=T0 + timedelta(seconds=60))['released'])
        self.seed_attempt('refill', 'refill-task', 'zcode:GLM-5.3')
        late = dp.claim_due(self.store, task_id='rt', now=T0 + timedelta(seconds=301),
                            _preclaim=False)
        self.assertFalse(late['claimed'])
        self.assertEqual(late.get('state'), 'cancelled')

    def test_replied_external_agent_blocks_domestic_start(self):
        self.fill_domestic()
        self.assertTrue(dp.ask_record(self.store, task_id='ex', scope=_scope('ex'),
                                      ask_message_id='msg-1', now=T0)['recorded'])
        dp.reply(self.store, task_id='ex', choice='external_agent', now=T0)
        self.assertTrue(dp.finish(self.store, 'seed-0', terminal='cancelled',
                                  now=T0 + timedelta(seconds=30))['released'])
        for fn in (dp.select_and_claim, dp.reserve):
            out = fn(self.store, task_id='ex', runtime='zcode', model='GLM-5.3',
                     workspace='C:/w', prompt_sha256='p' * 64,
                     now=T0 + timedelta(seconds=31), _preclaim=False)
            self.assertFalse(out['allowed'])
            self.assertEqual(out['reason'], 'task_answered_external')


class TestClaimDueScopeZ2(TempStoreMixin, unittest.TestCase):
    """Z2-4/Z2-5：claim_due 顺序与原 scope 绑定。"""

    def test_early_domestic_release_reclaims_immediately_with_real_scope(self):
        self.fill_domestic()
        sc = _scope('sc')
        self.assertTrue(dp.ask_record(self.store, task_id='sc', scope=sc,
                                      ask_message_id='msg-1', now=T0)['recorded'])
        # 询问仅 30 秒：国内释放 → 立即 domestic_reclaim（不必等到期）。
        self.assertTrue(dp.finish(self.store, 'seed-0', terminal='cancelled',
                                  now=T0 + timedelta(seconds=30))['released'])
        out = dp.claim_due(self.store, task_id='sc', now=T0 + timedelta(seconds=30),
                           _preclaim=False)
        self.assertTrue(out['claimed'], out)
        self.assertEqual(out['mode'], 'domestic_reclaim')
        self.assertEqual(out['scope']['stage'], sc['stage'])
        self.assertEqual(out['scope']['chat_id'], sc['chat_id'])
        row = self.row('attempts', 'token', out['token'])
        self.assertEqual(row['stage'], sc['stage'])
        self.assertEqual(row['chat_id'], sc['chat_id'])
        self.assertEqual(row['prompt_sha256'], sc['prompt_sha256'])
        # 原 task/stage/chat/workspace/promptSHA 的 consume 必须成功。
        cons = dp.consume_for_entry(self.store, claim_token=out['token'],
                                    task_id='sc', runtime=row['runtime'],
                                    model=row['model'], workspace=sc['workspace'],
                                    prompt_sha256=sc['prompt_sha256'],
                                    stage=sc['stage'], chat_id=sc['chat_id'],
                                    wrapper_pid=1234, wrapper_created='wc-z2',
                                    now=T0 + timedelta(seconds=31))
        self.assertTrue(cons['allowed'], cons)
        # 任何字段漂移（stage）必须拒绝：再造一张票走一次完整回收。
        sc2 = _scope('sc2')
        # Z3 修复：释放 seed-1 后再补一个真实在途 attempt，使国内回到满容量（总 8，
        # BW-QODER-CN-20261010-A2 起含 2 个 CN 补充候选）；sc2 的票据必须在确实满容量时
        # 询问（未满询问会被正确拒绝）。
        with closing(dp.connect(self.store)) as conn:
            conn.execute("UPDATE attempts SET state='cancelled' WHERE token='seed-1'")
        self.seed_attempt('refill-sc2', 'refill-sc2-task', 'qoder:Qwen3.8-Flash')
        self.assertTrue(dp.ask_record(self.store, task_id='sc2', scope=sc2,
                                      ask_message_id='msg-2',
                                      now=T0 + timedelta(seconds=40))['recorded'])
        with closing(dp.connect(self.store)) as conn:
            conn.execute("UPDATE attempts SET state='finished' WHERE token='seed-2'")
        out2 = dp.claim_due(self.store, task_id='sc2', now=T0 + timedelta(seconds=41),
                            scope=json.dumps(sc2), _preclaim=False)
        self.assertTrue(out2['claimed'], out2)
        drift = dp.consume_for_entry(self.store, claim_token=out2['token'],
                                     task_id='sc2', runtime=out2['selected']['runtime'],
                                     model=out2['selected']['model'],
                                     workspace=sc2['workspace'],
                                     prompt_sha256=sc2['prompt_sha256'],
                                     stage='DRIFT', chat_id=sc2['chat_id'],
                                     wrapper_pid=1235, now=T0 + timedelta(seconds=42))
        self.assertFalse(drift['allowed'])
        self.assertEqual(drift['reason'], 'claim_invalid')
        self.assertEqual(self.row('attempts', 'token', out2['token'])['state'],
                         'reserved')

    def test_scope_mismatch_rejected_on_luna_branch_too(self):
        self.fill_domestic()
        self.assertTrue(dp.ask_record(self.store, task_id='ls', scope=_scope('ls'),
                                      ask_message_id='msg-1', now=T0)['recorded'])
        bad = dict(_scope('ls'))
        bad['stage'] = 'TAMPERED'
        out = dp.claim_due(self.store, task_id='ls', now=T0 + timedelta(seconds=301),
                           scope=json.dumps(bad), _preclaim=False)
        self.assertFalse(out['claimed'])
        self.assertTrue(any('scope' in r for r in out['reasons']), out['reasons'])

    def test_six_full_299s_refused_300s_luna(self):
        self.fill_domestic()
        self.assertTrue(dp.ask_record(self.store, task_id='w299', scope=_scope('w299'),
                                      ask_message_id='msg-1', now=T0)['recorded'])
        early = dp.claim_due(self.store, task_id='w299',
                             now=T0 + timedelta(seconds=299), _preclaim=False)
        self.assertFalse(early['claimed'])
        self.assertTrue(early['not_due'])
        ontime = dp.claim_due(self.store, task_id='w299',
                              now=T0 + timedelta(seconds=300), _preclaim=False)
        self.assertTrue(ontime['claimed'])
        self.assertEqual(ontime['mode'], 'luna')
        row = self.row('attempts', 'token', ontime['token'])
        self.assertEqual(row['stage'], 's1')
        self.assertEqual(row['workspace'],
                         dp._norm_workspace(_scope('w299')['workspace']))


class TestOwnerIdentityZ2(TempStoreMixin, unittest.TestCase):
    """Z2-6：bind_child/finish 的 owner 身份与 unknown 状态保护。"""

    def _owned_attempt(self, wrapper_pid=4321):
        dp.reserve(self.store, task_id='ow', runtime='zcode', model='GLM-5.3',
                   workspace='C:/w', prompt_sha256='a' * 64, now=T0, _preclaim=False)
        tok = self.row('attempts', 'task_id', 'ow')['token']
        dp.consume_for_entry(self.store, claim_token=tok, task_id='ow', runtime='zcode',
                             model='GLM-5.3', workspace='C:/w', prompt_sha256='a' * 64,
                             wrapper_pid=wrapper_pid, wrapper_created='w-owned',
                             now=T0)
        return tok

    def test_bind_child_requires_current_owner(self):
        tok = self._owned_attempt()
        stranger = dp.bind_child(self.store, tok, 111, child_created='c1', now=T0)
        self.assertFalse(stranger['bound'])
        self.assertEqual(stranger.get('reason'), 'owner_mismatch')
        owner = dp.bind_child(self.store, tok, 111, child_created='c1',
                              wrapper_pid=4321, wrapper_created='w-owned', now=T0)
        self.assertTrue(owner['bound'])

    def test_finish_unknown_no_child_not_released_on_argument_alone(self):
        tok = self._owned_attempt()
        dp.mark_unknown(self.store, tok, now=T0)
        anon = dp.finish(self.store, tok, terminal='start_failed', now=T0)
        self.assertFalse(anon['released'])
        self.assertEqual(anon.get('reason'), 'owner_unverified')
        self.assertEqual(self.row('attempts', 'token', tok)['state'], 'unknown')
        stranger = dp.finish(self.store, tok, terminal='start_failed', now=T0,
                             wrapper_pid=9999)
        self.assertFalse(stranger['released'])
        # 真实 owner 亲证 Popen 失败才走 start_failed。
        owner = dp.finish(self.store, tok, terminal='start_failed', now=T0,
                          wrapper_pid=4321, wrapper_created='w-owned')
        self.assertTrue(owner['released'])

    def test_running_no_child_owner_start_failed_allowed_reserved_cancel_kept(self):
        tok = self._owned_attempt()
        out = dp.finish(self.store, tok, terminal='start_failed', now=T0,
                        wrapper_pid=4321, wrapper_created='w-owned')
        self.assertTrue(out['released'])
        # reserved 未消费的合法取消仍可释放。Z3 修复：ow 的 reserve 已 committed_z=1>0，
        # 按 1:1 此时应 reserve Max；先核验 reserve 真实返回，绝不对 None 直接下标。
        rz = dp.reserve(self.store, task_id='rz', runtime='qoder', model='Qwen3.8-Max',
                        workspace='C:/w', prompt_sha256='b' * 64, now=T0,
                        _preclaim=False)
        self.assertTrue(rz['allowed'], rz)
        tok2 = self.row('attempts', 'task_id', 'rz')['token']
        self.assertIsNotNone(tok2)
        self.assertTrue(dp.finish(self.store, tok2, terminal='cancelled',
                                  now=T0)['released'])


class TestWorkspaceGuardZ4(TempStoreMixin, unittest.TestCase):
    """Z4：同真实 workspace 单写入守卫——select/reserve 拒绝别 task 同目录在途；
    显式 token consume 同 task 原消费除外，别 task 同目录在途拒绝且 sent=false。"""

    def test_select_and_reserve_reject_other_task_same_workspace(self):
        kw = dict(prompt_sha256='a' * 64, now=T0, _preclaim=False)
        first = dp.select_and_claim(self.store, task_id='wga', runtime='zcode',
                                    model='GLM-5.3', workspace='C:/wg/dir', **kw)
        self.assertTrue(first['allowed'], first)
        for fn in (dp.select_and_claim, dp.reserve):
            out = fn(self.store, task_id='wgb', runtime='qoder',
                     model='Qwen3.8-Max', workspace='C:/wg/dir', **kw)
            self.assertFalse(out['allowed'])
            self.assertEqual(out['reason'], 'workspace_in_flight')
            self.assertFalse(out['sent'])
        # 别的组合/入口（Flash）也绕不过。
        fl = dp.select_and_claim(self.store, task_id='wgc', runtime='qoder',
                                 model='Qwen3.8-Flash', workspace='C:/wg/dir', **kw)
        self.assertFalse(fl['allowed'])
        # 原路释放后同目录才可再次写入。
        self.assertTrue(dp.finish(self.store, first['token'], terminal='cancelled',
                                  now=T0)['released'])
        again = dp.reserve(self.store, task_id='wgb', runtime='qoder',
                           model='Qwen3.8-Max', workspace='C:/wg/dir', **kw)
        self.assertTrue(again['allowed'], again)

    def test_consume_same_token_allowed_other_task_same_dir_rejected(self):
        res = dp.reserve(self.store, task_id='wgd', runtime='zcode', model='GLM-5.3',
                         workspace='C:/wg/d2', prompt_sha256='b' * 64, stage='s',
                         chat_id='c', now=T0, _preclaim=False)
        self.assertTrue(res['allowed'], res)
        # 直接播种一个别的 task 的在途行占同一真实目录（模拟 adopt/旧库既有写入）。
        with closing(dp.connect(self.store)) as conn:
            conn.execute(
                "INSERT INTO attempts(token, task_id, runtime, model, pool_key, state, "
                'workspace) VALUES(?, ?, ?, ?, ?, ?, ?)',
                ('intruder', 'intruder-task', 'qoder', 'Qwen3.8-Max',
                 'qoder:Qwen3.8-Max', 'running',
                 dp._norm_workspace('C:/wg/d2')))
        # 同 task 原 token 消费被守卫拒绝（写入方已是别的 task）。
        out = dp.consume_for_entry(self.store, claim_token=res['token'],
                                   task_id='wgd', runtime='zcode', model='GLM-5.3',
                                   workspace='C:/wg/d2', prompt_sha256='b' * 64,
                                   stage='s', chat_id='c', wrapper_pid=11, now=T0)
        self.assertFalse(out['allowed'])
        self.assertEqual(out['reason'], 'workspace_in_flight')
        self.assertFalse(out['sent'])
        # 占用者释放后原 token 消费恢复合法（同 task 除外条款）。
        self.assertTrue(dp.finish(self.store, 'intruder', terminal='cancelled',
                                  now=T0)['released'])
        ok = dp.consume_for_entry(self.store, claim_token=res['token'],
                                  task_id='wgd', runtime='zcode', model='GLM-5.3',
                                  workspace='C:/wg/d2', prompt_sha256='b' * 64,
                                  stage='s', chat_id='c', wrapper_pid=11, now=T0)
        self.assertTrue(ok['allowed'], ok)


if __name__ == '__main__':
    unittest.main()
