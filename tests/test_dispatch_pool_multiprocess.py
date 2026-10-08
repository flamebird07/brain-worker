# -*- coding: utf-8 -*-
"""BW-GLOBAL-POOL-20261008-Z3 真实多进程临时 SQLite 竞争测试（仅编写，本轮未运行）。

Z4 增补（BW-GLOBAL-POOL-20261008-Z4，仅编写未运行）：
- TestWorkspaceSingleWriterZ4：跨 OS spawn 同目录不同 task 竞争恰好一个 claim +
  normpath 别名同目录拒绝（平台 symlink 受限不全局 skip）；
- TestLocalPidProbeZ4：qc 缺席时真实子进程退出必须 dead（GetExitCodeProcess，
  不凭 OpenProcess 句柄判活）；
- TestAdoptLegacyRealSchemaZ4 / TestAdoptLegacyRealSpawnLifecycleZ4：真实旧原件
  schema（Qoder model_requested/qodercli、Zcode carrier/selection、process.json 无
  created + 宿主 child_created 留证）、同 PID+birth 不双计、真实 spawn adopt→退出
  →reconcile 释放、旧原件字节不变；
- fixture 修正：不同 task/票据不再共享 C:/mp 虚拟同一路径（守卫语义不放松）。

全部用 multiprocessing.get_context('spawn') + 顶层 worker 函数，Windows/Linux 都可跑；
只用有限 Barrier/Event/Queue 同步、超时与可靠 join/terminate；绝不碰真实模型/用户库/
真实额度；store 一律临时目录显式路径，且 setUp 摘除 BRAIN_WORKER_DISPATCH_STORE
（不借默认 env 库）。所有 dp 调用方各自 close 连接，子进程全部 join 后才清理临时目录
（避免 Windows TemporaryDirectory WinError 32）。

覆盖：
- 不同 chat 并发合法 claim：各池 ≤2、防重、持久 1:1（committed 差 ≤1 且等于真实
  主力 claim 计数）、pool_key 恒等于 runtime+':'+model（无偷换落库）；
- 同一已预留 token 并发 consume：恰好一个 allowed（CAS），其余 consume_conflict；
- 六满询问后 deadline/reply/国内释放并发：原 task 恰好一个最终 claim，无重复 Luna/
  国内派发；
- PID 复用/unknown 不回收（真实 spawn 子进程 + 故意错误出生身份）；旧 attempt 晚
  finish 不释放新 attempt（幂等且不影响新在途行）。

运行登记（由 Qoder 在原登记命令下执行，本文件作者未运行）：
  cd <repo-root>
  python -m unittest tests.test_dispatch_pool_multiprocess -v
"""
import json
import os
import queue
import sys
import tempfile
import traceback
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import multiprocessing as mp

_SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import dispatch_pool as dp  # noqa: E402

T0 = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
JOIN_TIMEOUT = 60
BARRIER_TIMEOUT = 45

_CTX = mp.get_context('spawn')


# ------------------------------------------------------------------ 顶层 workers
def _claim_worker(store, barrier, q, worker_id, runtime, model, chat_id):
    """并发合法 claim：不同 task、不同 chat，各自独立连接/事务。"""
    try:
        barrier.wait(timeout=BARRIER_TIMEOUT)
        out = dp.select_and_claim(store, task_id=f'mpc-{worker_id}', runtime=runtime,
                                  model=model, workspace=f'C:/mp/{worker_id}',
                                  chat_id=chat_id,
                                  stage=f's{worker_id}',
                                  prompt_sha256=f'{worker_id:x}' * 64, now=T0,
                                  _preclaim=False)
        q.put({'kind': 'claim', 'worker': worker_id,
               'allowed': out['allowed'], 'pool_key': out.get('pool_key'),
               'selected': out.get('selected'),
               'reason': out.get('reason')})
    except Exception:
        q.put({'kind': 'ERR', 'worker': worker_id,
               'tb': traceback.format_exc()})


def _consume_worker(store, barrier, q, worker_id, token):
    """并发消费同一已预留 token：恰好一个 CAS 赢家。"""
    try:
        barrier.wait(timeout=BARRIER_TIMEOUT)
        out = dp.consume_for_entry(store, claim_token=token, task_id='mpk',
                                   runtime='zcode', model='GLM-5.3',
                                   workspace='C:/mp', prompt_sha256='k' * 64,
                                   stage='sk', chat_id='ck', wrapper_pid=os.getpid(),
                                   now=T0)
        q.put({'kind': 'consume', 'worker': worker_id,
               'allowed': out['allowed'], 'reason': out.get('reason')})
    except Exception:
        q.put({'kind': 'ERR', 'worker': worker_id,
               'tb': traceback.format_exc()})


def _finish_worker(store, barrier, q, token, delay_event, deadline_s):
    """国内释放方：barrier 后等开跑事件再真实 finish（reserved 未消费合法取消）。"""
    try:
        barrier.wait(timeout=BARRIER_TIMEOUT)
        delay_event.wait(timeout=BARRIER_TIMEOUT)
        out = dp.finish(store, token, terminal='cancelled',
                        now=T0 + timedelta(seconds=deadline_s))
        q.put({'kind': 'finish', 'released': out['released'],
               'reason': out.get('reason')})
    except Exception:
        q.put({'kind': 'ERR', 'tb': traceback.format_exc()})


def _claim_due_worker(store, barrier, q, task_id, at_s, scope_json):
    """到期/提前裁决方：与国内释放方真实并发竞争同一票据。"""
    try:
        barrier.wait(timeout=BARRIER_TIMEOUT)
        out = dp.claim_due(store, task_id=task_id, now=T0 + timedelta(seconds=at_s),
                           scope=scope_json, _preclaim=False)
        q.put({'kind': 'claim_due', 'task': task_id, 'claimed': out['claimed'],
               'mode': out.get('mode'), 'reason': out.get('reason'),
               'state': out.get('state'),
               'token': out.get('token')})
    except Exception:
        q.put({'kind': 'ERR', 'task': task_id, 'tb': traceback.format_exc()})


def _sleep_worker(alive_event):
    """被 adopt/reconcile 观测的真实存活子进程；alive_event 置位即退出。"""
    alive_event.wait(timeout=JOIN_TIMEOUT)


def _ws_claim_worker(store, barrier, q, worker_id, workspace):
    """Z4：同真实 workspace 不同 task 的并发 claim 竞争（跨 OS spawn）。"""
    try:
        barrier.wait(timeout=BARRIER_TIMEOUT)
        out = dp.select_and_claim(store, task_id=f'ws-{worker_id}', runtime='zcode',
                                  model='GLM-5.3', workspace=workspace,
                                  stage=f'w{worker_id}',
                                  prompt_sha256=f'{worker_id:x}' * 64, now=T0,
                                  _preclaim=False)
        q.put({'kind': 'claim', 'worker': worker_id, 'allowed': out['allowed'],
               'reason': out.get('reason'), 'token': out.get('token')})
    except Exception:
        q.put({'kind': 'ERR', 'worker': worker_id, 'tb': traceback.format_exc()})


# ------------------------------------------------------------------ 基类
class _SpawnMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bw-mp-dispatch-')
        self.store = str(Path(self._tmp.name) / 'pool.sqlite3')
        self.assertNotEqual(str(self.store), str(dp.default_store_path()))
        self.saved_env = os.environ.pop('BRAIN_WORKER_DISPATCH_STORE', None)
        self._procs = []

    def tearDown(self):
        # 先可靠 join/terminate 全部子进程，再清理临时目录（Windows 下避免文件仍被
        # 打开导致 WinError 32；dp 各 API 用 closing 自行关闭连接）。
        for p in self._procs:
            p.join(timeout=JOIN_TIMEOUT)
            if p.is_alive():
                p.terminate()
                p.join(timeout=JOIN_TIMEOUT)
            if p.is_alive():
                self.fail('spawn worker could not be joined/terminated')
        for p in self._procs:
            p.close()
        if self.saved_env is not None:
            os.environ['BRAIN_WORKER_DISPATCH_STORE'] = self.saved_env
        self._tmp.cleanup()

    def _spawn(self, target, args=()):
        p = _CTX.Process(target=target, args=args)
        p.start()
        self._procs.append(p)
        return p

    def _drain(self, q, n):
        out = []
        for _ in range(n):
            try:
                out.append(q.get(timeout=JOIN_TIMEOUT))
            except queue.Empty:
                self.fail('worker result missing (queue timeout)')
        for r in out:
            self.assertNotEqual(r.get('kind'), 'ERR', r.get('tb', r))
        return out

    def _rows(self, sql, params=()):
        with closing(dp.connect(self.store)) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def _fill_six_via_api(self):
        """真实 API 填满六满基线（交替主力 → 各 2，再 Flash 2），返回 token 列表。"""
        toks = []
        plan = [('zcode', 'GLM-5.3'), ('qoder', 'Qwen3.8-Max'),
                ('zcode', 'GLM-5.3'), ('qoder', 'Qwen3.8-Max'),
                ('qoder', 'Qwen3.8-Flash'), ('qoder', 'Qwen3.8-Flash')]
        for i, (rt, md) in enumerate(plan):
            out = dp.select_and_claim(self.store, task_id=f'base-{i}', runtime=rt,
                                      model=md, workspace=f'C:/mp/base-{i}',
                                      chat_id=f'bc{i}',
                                      stage=f'bs{i}', prompt_sha256=f'{i}' * 64,
                                      now=T0, _preclaim=False)
            self.assertTrue(out['allowed'], out)
            toks.append(out['token'])
        return toks


# ------------------------------------------------------------------ 测试
class TestConcurrentClaims(_SpawnMixin, unittest.TestCase):
    def test_cross_chat_concurrent_claims_caps_and_rotation(self):
        n = 10
        barrier = _CTX.Barrier(n)
        q = _CTX.Queue()
        combos = [('zcode', 'GLM-5.3'), ('qoder', 'Qwen3.8-Max'),
                  ('qoder', 'Qwen3.8-Flash')]
        for i in range(n):
            rt, md = combos[i % 3]
            self._spawn(_claim_worker,
                        (self.store, barrier, q, i, rt, md, f'chat-{i}'))
        results = self._drain(q, n)
        allowed = [r for r in results if r['allowed']]
        denied = [r for r in results if not r['allowed']]
        self.assertLessEqual(len(allowed), 6)
        self.assertTrue(denied, 'with 10 racers on a 6-slot pool some must be denied')
        # 防重：每个 task 至多一行 attempt；allowed 的 task 各不相同。
        rows = self._rows('SELECT * FROM attempts')
        by_task = {}
        for row in rows:
            by_task.setdefault(row['task_id'], []).append(row)
        for task, lst in by_task.items():
            self.assertEqual(len(lst), 1, f'task {task} double-started')
        for r in allowed:
            self.assertEqual(len(by_task[f"mpc-{r['worker']}"]), 1)
        # 各池 ≤2；pool_key 恒等于 runtime+':'+model（无偷换落库）。
        for pk in ('zcode:GLM-5.3', 'qoder:Qwen3.8-Max', 'qoder:Qwen3.8-Flash'):
            act = dp.status(self.store, now=T0)['pools'][pk]['active']
            self.assertLessEqual(act, 2, f'{pk} over capacity: {act}')
        for row in rows:
            self.assertEqual(row['pool_key'], f"{row['runtime']}:{row['model']}")
        # 持久 1:1：committed 计数等于真实主力 claim 数且差 ≤1。
        z = sum(1 for r in rows if r['pool_key'] == 'zcode:GLM-5.3')
        qq = sum(1 for r in rows if r['pool_key'] == 'qoder:Qwen3.8-Max')
        st = dp.status(self.store, now=T0)['rotation']
        self.assertEqual(st['committed_zcode'], z)
        self.assertEqual(st['committed_qoder'], qq)
        self.assertLessEqual(abs(z - qq), 1)
        # 拒绝方只能是 routing_required / capacity_full，绝无静默落错池。
        for r in denied:
            self.assertIn(r['reason'], ('routing_required', 'capacity_full'), r)


class TestConcurrentConsume(_SpawnMixin, unittest.TestCase):
    def test_same_reserved_token_single_winner(self):
        res = dp.reserve(self.store, task_id='mpk', runtime='zcode', model='GLM-5.3',
                         workspace='C:/mp', prompt_sha256='k' * 64, stage='sk',
                         chat_id='ck', now=T0, _preclaim=False)
        self.assertTrue(res['allowed'], res)
        n = 4
        barrier = _CTX.Barrier(n)
        q = _CTX.Queue()
        for i in range(n):
            self._spawn(_consume_worker, (self.store, barrier, q, i, res['token']))
        results = self._drain(q, n)
        winners = [r for r in results if r['allowed']]
        losers = [r for r in results if not r['allowed']]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), n - 1)
        for r in losers:
            self.assertEqual(r['reason'], 'consume_conflict')
        row = self._rows("SELECT * FROM attempts WHERE token=?", (res['token'],))[0]
        self.assertEqual(row['state'], 'running')
        self.assertIsNotNone(row['wrapper_pid'])


class TestSixFullRace(_SpawnMixin, unittest.TestCase):
    def test_release_vs_deadline_vs_reply_single_final_claim(self):
        toks = self._fill_six_via_api()
        scope = {'task_id': 'race', 'stage': 'rs', 'chat_id': 'rc',
                 'workspace': 'C:/mp/race', 'prompt_sha256': 'r' * 64}
        self.assertTrue(dp.ask_record(self.store, task_id='race', scope=scope,
                                      ask_message_id='msg-race', now=T0)['recorded'])
        n = 3
        barrier = _CTX.Barrier(n)
        q = _CTX.Queue()
        go = _CTX.Event()
        # 国内释放方（30 秒即释放，早于 deadline）+ 到期 Luna 方 + 用户回复方并发。
        self._spawn(_finish_worker, (self.store, barrier, q, toks[0], go, 30))
        self._spawn(_claim_due_worker, (self.store, barrier, q, 'race', 301,
                                        json.dumps(scope)))
        self._spawn(_claim_due_worker, (self.store, barrier, q, 'race', 35,
                                        json.dumps(scope)))
        go.set()
        results = self._drain(q, n)
        finals = [r for r in results if r['kind'] == 'claim_due' and r['claimed']]
        self.assertEqual(len(finals), 1, results)
        fin = finals[0]
        # 无论谁先：要么六仍满→Luna，要么已释放→国内回收；恰好一个最终 claim。
        self.assertIn(fin['mode'], ('luna', 'domestic_reclaim'))
        luna_rows = self._rows(
            "SELECT * FROM attempts WHERE pool_key='luna:native' AND task_id='race'")
        domestic_rows = self._rows(
            "SELECT * FROM attempts WHERE task_id='race' "
            "AND pool_key!='luna:native' AND origin='claim-due-domestic-reclaim'")
        self.assertLessEqual(len(luna_rows), 1)
        self.assertLessEqual(len(domestic_rows), 1)
        self.assertEqual(len(luna_rows) + len(domestic_rows), 1)
        # 票据终态确定且只被裁决一次。
        ticket = self._rows("SELECT * FROM luna_tickets WHERE task_id='race'")[0]
        self.assertIn(ticket['state'], ('cancelled', 'claimed'))
        if ticket['state'] == 'claimed':
            self.assertEqual(fin['mode'], 'luna')
            self.assertEqual(ticket['claimed_token'], fin['token'])
        else:
            self.assertEqual(fin['mode'], 'domestic_reclaim')
        # 国内总量不超 6。
        dom = dp.status(self.store, now=T0 + timedelta(seconds=301))['domestic']
        self.assertLessEqual(dom['active'], 6)


class TestPidReuseAndLateFinish(_SpawnMixin, unittest.TestCase):
    def test_live_pid_wrong_birth_held_unknown_then_dead_released(self):
        # 真实存活子进程，但记录一个错误的出生身份 → reconcile 必须 unknown 不回收
        # （PID 复用/身份漂移不得当作原 child 已死而抢占）。
        alive = _CTX.Event()
        child = _CTX.Process(target=_sleep_worker, args=(alive,))
        child.start()
        self._procs.append(child)
        try:
            with closing(dp.connect(self.store)) as conn:
                conn.execute(
                    "INSERT INTO attempts(token, task_id, runtime, model, pool_key, "
                    "state, child_pid, child_created, wrapper_pid, wrapper_created) "
                    "VALUES('pu','pu-task','zcode','GLM-5.3','zcode:GLM-5.3',"
                    "'running', ?, 'WRONG-BIRTH', 4242, 'w')", (child.pid,))
            out = dp.reconcile(self.store, now=T0)
            self.assertNotIn('pu', out['released'])
            self.assertEqual(
                self._rows("SELECT state FROM attempts WHERE token='pu'")[0]['state'],
                'unknown')
            # 确认明确 dead（真实退出）后才可释放。
            alive.set()
            child.join(timeout=JOIN_TIMEOUT)
            self.assertFalse(child.is_alive())
            out2 = dp.reconcile(self.store, now=T0)
            self.assertIn('pu', out2['released'])
        finally:
            alive.set()
            child.join(timeout=JOIN_TIMEOUT)
            if child.is_alive():
                child.terminate()
                child.join(timeout=JOIN_TIMEOUT)

    def test_old_attempt_late_finish_never_releases_new_attempt(self):
        old = dp.select_and_claim(self.store, task_id='lat', runtime='zcode',
                                  model='GLM-5.3', workspace='C:/mp',
                                  prompt_sha256='o' * 64, stage='ls', chat_id='lc',
                                  now=T0, _preclaim=False)
        self.assertTrue(old['allowed'], old)
        self.assertTrue(dp.finish(self.store, old['token'], terminal='cancelled',
                                  now=T0)['released'])
        # 同 task 新 attempt（跨 1:1 轮转请求 Max），旧 token 晚到的重复 finish
        # 只能幂等，绝不影响/释放新在途行。
        new = dp.select_and_claim(self.store, task_id='lat', runtime='qoder',
                                  model='Qwen3.8-Max', workspace='C:/mp',
                                  prompt_sha256='n' * 64, stage='ls', chat_id='lc',
                                  now=T0, _preclaim=False)
        self.assertTrue(new['allowed'], new)
        again = dp.finish(self.store, old['token'], terminal='finished', now=T0)
        self.assertFalse(again['released'])
        self.assertTrue(again.get('idempotent'))
        # select_and_claim 未传 wrapper → 新 attempt 为 reserved（等待 consume CAS）。
        self.assertEqual(
            self._rows("SELECT state FROM attempts WHERE token=?",
                       (new['token'],))[0]['state'], 'reserved')


class TestAdoptLegacyOffline(_SpawnMixin, unittest.TestCase):
    """离线（注入 prober）覆盖 adopt-legacy 可信成功/重复/漂移/unknown/CB 拒绝。
    生产路径用真实 process_identity，本组不提供也不测试任何 fake-alive CLI 开关。"""

    def _receipts(self, tmp, *, pid=8787, created='real-birth', task_id='lg',
                  runtime='zcode', model='GLM-5.3', stage='lgs', chat_id='lgc'):
        rp = Path(tmp) / 'request.json'
        pp = Path(tmp) / 'process.json'
        rp.write_text(json.dumps({
            'task_id': task_id, 'runtime': runtime, 'model': model,
            'workspace': 'C:/legacy', 'prompt_sha256': 'e' * 64,
            'stage': stage, 'chat_id': chat_id}), encoding='utf-8')
        pp.write_text(json.dumps({'pid': pid, 'created': created}), encoding='utf-8')
        return rp, pp

    def test_trusted_adopt_success_then_idempotent_no_double_count(self):
        rp, pp = self._receipts(self._tmp.name)
        prober = lambda pid: {'pid': pid, 'state': 'alive', 'created': 'real-birth'}
        out = dp.adopt_legacy(self.store, task_id='lg', runtime='zcode',
                              model='GLM-5.3', request_path=str(rp),
                              process_path=str(pp), prober=prober, now=T0)
        self.assertTrue(out['adopted'], out)
        self.assertFalse(out['idempotent'])
        self.assertEqual(out['pool_key'], 'zcode:GLM-5.3')
        row = self._rows("SELECT * FROM attempts WHERE token=?", (out['token'],))[0]
        self.assertEqual(row['state'], 'running')
        self.assertEqual(row['origin'], 'adopt-legacy')
        self.assertEqual(row['stage'], 'lgs')
        self.assertEqual(row['chat_id'], 'lgc')
        self.assertEqual(row['prompt_sha256'], 'e' * 64)
        # Z4-D：worker PID+birth 绑定 child_pid/child_created（不是 wrapper——reconcile
        # 在旧 worker 真实退出后立即释放容量，结束即空名额）。
        self.assertEqual(row['child_pid'], 8787)
        self.assertEqual(row['child_created'], 'real-birth')
        self.assertIsNone(row['wrapper_pid'])
        ev = json.loads(row['adopt_evidence'])
        self.assertIn('request_sha256', ev)
        self.assertIn('process_sha256', ev)
        self.assertEqual(ev['created_source'], 'process_receipt')
        self.assertNotIn('argv', json.dumps(ev))
        # 同 PID+创建身份重复 adopt：幂等不双计。
        dup = dp.adopt_legacy(self.store, task_id='lg', runtime='zcode',
                              model='GLM-5.3', request_path=str(rp),
                              process_path=str(pp), prober=prober, now=T0)
        self.assertTrue(dup['adopted'])
        self.assertTrue(dup['idempotent'])
        self.assertEqual(dup['token'], out['token'])
        self.assertEqual(len(self._rows('SELECT * FROM attempts')), 1)
        # 主力计数如实 +1。
        self.assertEqual(dp.status(self.store, now=T0)['rotation']['committed_zcode'], 1)

    def test_identity_drift_and_unknown_and_exited_refused(self):
        rp, pp = self._receipts(self._tmp.name)
        drift = dp.adopt_legacy(self.store, task_id='lg', runtime='zcode',
                                model='GLM-5.3', request_path=str(rp),
                                process_path=str(pp),
                                prober=lambda pid: {'pid': pid, 'state': 'alive',
                                                    'created': 'REUSED'},
                                now=T0)
        self.assertFalse(drift['adopted'])
        self.assertEqual(drift['reason'], 'legacy_identity_drift')
        unknown = dp.adopt_legacy(self.store, task_id='lg', runtime='zcode',
                                  model='GLM-5.3', request_path=str(rp),
                                  process_path=str(pp),
                                  prober=lambda pid: {'pid': pid, 'state': 'unknown',
                                                      'created': 'real-birth'},
                                  now=T0)
        self.assertFalse(unknown['adopted'])
        self.assertEqual(unknown['reason'], 'legacy_alive_unknown')
        exited = dp.adopt_legacy(self.store, task_id='lg', runtime='zcode',
                                 model='GLM-5.3', request_path=str(rp),
                                 process_path=str(pp),
                                 prober=lambda pid: {'pid': pid, 'state': 'dead',
                                                     'created': None},
                                 now=T0)
        self.assertFalse(exited['adopted'])
        self.assertEqual(exited['reason'], 'legacy_process_exited')
        # 没有创建身份的 receipt 一律待核验拒绝。
        pp2 = Path(self._tmp.name) / 'process-noid.json'
        pp2.write_text(json.dumps({'pid': 8787}), encoding='utf-8')
        noid = dp.adopt_legacy(self.store, task_id='lg', runtime='zcode',
                               model='GLM-5.3', request_path=str(rp),
                               process_path=str(pp2),
                               prober=lambda pid: {'pid': pid, 'state': 'alive',
                                                   'created': 'real-birth'}, now=T0)
        self.assertFalse(noid['adopted'])
        self.assertEqual(noid['reason'], 'process_identity_missing')
        # 全部拒绝路径不落任何 attempt。
        self.assertEqual(self._rows('SELECT * FROM attempts'), [])

    def test_cb_combo_refused_without_touching_locks(self):
        rp, pp = self._receipts(self._tmp.name, runtime='cb', model='SomeCB')
        out = dp.adopt_legacy(self.store, task_id='lg', runtime='cb',
                              model='SomeCB', request_path=str(rp),
                              process_path=str(pp),
                              prober=lambda pid: {'pid': pid, 'state': 'alive',
                                                  'created': 'real-birth'}, now=T0)
        self.assertFalse(out['adopted'])
        self.assertEqual(out['reason'], 'adopt_pool_forbidden')
        self.assertEqual(self._rows('SELECT * FROM attempts'), [])
        self.assertEqual(dp.status(self.store, now=T0)['rotation']['committed_zcode'], 0)

    def test_over_cap_honest_accounting_blocks_new_dispatch(self):
        # 预置 2 个在途 zcode（已满），老 worker 仍被如实计入（3>2），新派发被阻止，
        # 老行绝不被丢弃。
        with closing(dp.connect(self.store)) as conn:
            for i in range(2):
                conn.execute(
                    "INSERT INTO attempts(token, task_id, runtime, model, pool_key, "
                    "state) VALUES(?,?, 'zcode','GLM-5.3','zcode:GLM-5.3','running')",
                    (f'pre-{i}', f'pre-task-{i}'))
        rp, pp = self._receipts(self._tmp.name)
        out = dp.adopt_legacy(self.store, task_id='lg', runtime='zcode',
                              model='GLM-5.3', request_path=str(rp),
                              process_path=str(pp),
                              prober=lambda pid: {'pid': pid, 'state': 'alive',
                                                  'created': 'real-birth'}, now=T0)
        self.assertTrue(out['adopted'], out)
        self.assertEqual(dp.status(self.store, now=T0)['pools']['zcode:GLM-5.3']
                         ['active'], 3)
        nxt = dp.select_and_claim(self.store, task_id='after', runtime='qoder',
                                  model='Qwen3.8-Max', workspace='C:/mp',
                                  prompt_sha256='z' * 64, stage='as', chat_id='ac',
                                  now=T0, _preclaim=False)
        # Max 池不受影响可 claim；但请求 zcode 已 3>2 必须被阻止（不许丢老 worker）。
        self.assertTrue(nxt['allowed'], nxt)
        z = dp.select_and_claim(self.store, task_id='after2', runtime='zcode',
                                model='GLM-5.3', workspace='C:/mp/after2',
                                prompt_sha256='y' * 64, stage='as', chat_id='ac',
                                now=T0, _preclaim=False)
        self.assertFalse(z['allowed'])
        self.assertTrue(z['routing_required'])
        rows = self._rows("SELECT COUNT(*) AS n FROM attempts WHERE origin='adopt-legacy'")
        self.assertEqual(rows[0]['n'], 1)


class TestWorkspaceSingleWriterZ4(_SpawnMixin, unittest.TestCase):
    """Z4-B：同真实 workspace 单写入守卫（真实 spawn 并发竞争 + normpath 别名）。"""

    def test_same_real_workspace_race_different_tasks_single_claim(self):
        n = 5
        barrier = _CTX.Barrier(n)
        q = _CTX.Queue()
        for i in range(n):
            self._spawn(_ws_claim_worker,
                        (self.store, barrier, q, i, 'C:/shared-real-ws'))
        results = self._drain(q, n)
        allowed = [r for r in results if r['allowed']]
        denied = [r for r in results if not r['allowed']]
        self.assertEqual(len(allowed), 1, results)
        self.assertEqual(len(denied), n - 1)
        for r in denied:
            self.assertEqual(r['reason'], 'workspace_in_flight', r)
        # 落库恰好一行，绑定胜者 task；容量没有被回收/删除任何占位。
        rows = self._rows('SELECT * FROM attempts')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['task_id'], f"ws-{allowed[0]['worker']}")
        self.assertEqual(rows[0]['state'], 'reserved')

    def test_normpath_alias_same_directory_rejected(self):
        # 用真实临时目录 + 当前 OS 分隔符构造同目录别名：normcase(realpath()) 归一后
        # 指向同一真实目录，仍须单写入拒绝（symlink 受限平台的等价保证）。别名必须
        # 跨 OS 有效——POSIX 上反斜杠是普通文件名字符而非分隔符，故仅 Windows 额外
        # 验证反斜杠/正斜杠表示归一到同一真实目录。
        base_dir = Path(self._tmp.name) / 'alias' / 'dir'
        base_dir.mkdir(parents=True)
        base = str(base_dir)
        sep = os.sep
        parent = str(Path(base).parent)
        name = Path(base).name
        aliases = [
            base + sep,                              # 尾分隔符
            base + sep + os.curdir,                  # dir/.
            base + sep + 'sub' + sep + os.pardir,    # dir/sub/..（父 dot 回收）
            parent + sep + os.curdir + sep + name,   # alias/./dir
        ]
        if os.name == 'nt':
            aliases.append(base.replace('\\', '/'))  # Windows 正斜杠表示
            aliases.append(base.replace('/', '\\'))  # Windows 反斜杠表示
        a = dp.select_and_claim(self.store, task_id='wa', runtime='zcode',
                                model='GLM-5.3', workspace=base,
                                prompt_sha256='a' * 64, now=T0, _preclaim=False)
        self.assertTrue(a['allowed'], a)
        for alias in aliases:
            b = dp.select_and_claim(self.store, task_id='wb', runtime='zcode',
                                    model='GLM-5.3', workspace=alias,
                                    prompt_sha256='b' * 64, now=T0, _preclaim=False)
            self.assertFalse(b['allowed'], alias)
            self.assertEqual(b['reason'], 'workspace_in_flight', alias)
            self.assertFalse(b['sent'])
        # reserve / claim_due 国内回收与 Luna 分支同样不绕过（Luna 票据 scope 同目录）。
        r = dp.reserve(self.store, task_id='wc', runtime='zcode', model='GLM-5.3',
                       workspace=base, prompt_sha256='c' * 64, now=T0,
                       _preclaim=False)
        self.assertFalse(r['allowed'])
        self.assertEqual(r['reason'], 'workspace_in_flight')


class TestLocalPidProbeZ4(_SpawnMixin, unittest.TestCase):
    """Z4-A：qc 缺席时 _local_pid_state 的 Windows GetExitCodeProcess 语义。"""

    def test_exited_child_reported_dead_own_pid_alive(self):
        alive = _CTX.Event()
        child = _CTX.Process(target=_sleep_worker, args=(alive,))
        child.start()
        self._procs.append(child)
        self.assertIn(dp._local_pid_state(child.pid), ('alive', 'unknown'))
        ident = dp.process_identity(child.pid)
        self.assertNotEqual(ident['state'], 'dead')
        alive.set()
        child.join(timeout=JOIN_TIMEOUT)
        self.assertFalse(child.is_alive())
        # 已退出但父进程仍在（句柄可 Open）：必须明确 dead，绝不能凭句柄判活。
        self.assertEqual(dp._local_pid_state(child.pid), 'dead')
        ident2 = dp.process_identity(child.pid)
        self.assertEqual(ident2['state'], 'dead')
        self.assertIn(dp._local_pid_state(os.getpid()), ('alive', 'unknown'))
        self.assertEqual(dp._local_pid_state(os.getpid()), 'alive')


class TestAdoptLegacyRealSchemaZ4(_SpawnMixin, unittest.TestCase):
    """Z4-C：真实旧原件 schema（Qoder model_requested/qodercli、Zcode carrier/
    selection；process.json 只有 pid/state）+ 宿主 child_created 留证。"""

    def _qoder_receipts(self, tmp):
        rp = Path(tmp) / 'q-request.json'
        pp = Path(tmp) / 'q-process.json'
        # 真实旧 Qoder 原件：runtime 是 {node, qodercli} 对象（不是字符串），模型在
        # model_requested；task 来自 dispatch_plan.task_id。process.json 只有 pid/state。
        rp.write_text(json.dumps({
            'model_requested': 'Qwen3.8-Max',
            'runtime': {'node': 'SYNTHETIC_NODE', 'qodercli': 'SYNTHETIC_QODER_CLI'},
            'workspace': 'C:/legacy-q', 'prompt_sha256': 'f' * 64,
            'stage': 'qs', 'chat_id': 'qc',
            'dispatch_plan': {'task_id': 'dq1'}}), encoding='utf-8')
        pp.write_text(json.dumps({'pid': 5555, 'state': 'running'}),
                      encoding='utf-8')
        return rp, pp

    def _zcode_receipts(self, tmp):
        rp = Path(tmp) / 'z-request.json'
        pp = Path(tmp) / 'z-process.json'
        # 真实旧 Zcode 原件：carrier='zcode-sdk' + selection.{providerId, modelId}。
        # providerId 是账号/供应商标识（account:bigmodel-individual-coding-plan），
        # runtime 必须归为 zcode（来自 carrier），绝不能把 providerId 当 runtime。
        rp.write_text(json.dumps({
            'carrier': 'zcode-sdk',
            'selection': {'providerId': 'account:bigmodel-individual-coding-plan',
                          'modelId': 'GLM-5.3'},
            'workspace': 'C:/legacy-z', 'prompt_sha256': 'g' * 64,
            'stage': 'zs'}), encoding='utf-8')
        pp.write_text(json.dumps({'pid': 5556, 'state': 'running',
                                  'started_at_utc': '2026-10-08T01:02:03+00:00'}),
                      encoding='utf-8')
        return rp, pp

    def test_qoder_legacy_schema_adopted_with_host_vouched_created(self):
        rp, pp = self._qoder_receipts(self._tmp.name)
        prober = lambda pid: {'pid': pid, 'state': 'alive', 'created': 'vb-1'}
        out = dp.adopt_legacy(self.store, task_id='dq1', runtime='qoder',
                              model='Qwen3.8-Max', request_path=str(rp),
                              process_path=str(pp), prober=prober, now=T0,
                              child_created='vb-1')
        self.assertTrue(out['adopted'], out)
        self.assertEqual(out['pool_key'], 'qoder:Qwen3.8-Max')
        row = self._rows("SELECT * FROM attempts WHERE token=?", (out['token'],))[0]
        self.assertEqual(row['child_pid'], 5555)
        self.assertEqual(row['child_created'], 'vb-1')
        self.assertEqual(row['task_id'], 'dq1')
        self.assertEqual(row['stage'], 'qs')
        self.assertEqual(row['workspace'], dp._norm_workspace('C:/legacy-q'))
        ev = json.loads(row['adopt_evidence'])
        self.assertEqual(ev['created_source'], 'host_vouched_child_created')
        # 旧原件保持原样（只读解析，绝不改写为新 schema）：runtime 仍是 {node,qodercli}。
        self.assertEqual(json.loads(rp.read_text(encoding='utf-8'))['runtime'],
                         {'node': 'SYNTHETIC_NODE', 'qodercli': 'SYNTHETIC_QODER_CLI'})

    def test_zcode_legacy_schema_adopted_task_from_stage(self):
        rp, pp = self._zcode_receipts(self._tmp.name)
        prober = lambda pid: {'pid': pid, 'state': 'alive', 'created': 'vb-2'}
        out = dp.adopt_legacy(self.store, task_id='zs', runtime='zcode',
                              model='GLM-5.3', request_path=str(rp),
                              process_path=str(pp), prober=prober, now=T0,
                              child_created='vb-2')
        self.assertTrue(out['adopted'], out)
        self.assertEqual(out['pool_key'], 'zcode:GLM-5.3')
        row = self._rows("SELECT * FROM attempts WHERE token=?", (out['token'],))[0]
        self.assertEqual(row['task_id'], 'zs')

    def test_legacy_drift_and_missing_identity_refused(self):
        rp, pp = self._qoder_receipts(self._tmp.name)
        prober = lambda pid: {'pid': pid, 'state': 'alive', 'created': 'vb-1'}
        # 调用组合漂移：原 request 是 Qwen3.8-Max，不能按 GLM-5.3 计入。
        bad = dp.adopt_legacy(self.store, task_id='dq1', runtime='zcode',
                              model='GLM-5.3', request_path=str(rp),
                              process_path=str(pp), prober=prober, now=T0,
                              child_created='vb-1')
        self.assertFalse(bad['adopted'])
        self.assertEqual(bad['reason'], 'request_combo_mismatch')
        # task 与 dispatch_plan.task_id 不一致。
        badtask = dp.adopt_legacy(self.store, task_id='OTHER', runtime='qoder',
                                  model='Qwen3.8-Max', request_path=str(rp),
                                  process_path=str(pp), prober=prober, now=T0,
                                  child_created='vb-1')
        self.assertFalse(badtask['adopted'])
        self.assertEqual(badtask['reason'], 'request_task_mismatch')
        # 缺 child_created 又缺 receipt created：待核验拒绝，不凭裸 PID 接纳。
        noid = dp.adopt_legacy(self.store, task_id='dq1', runtime='qoder',
                               model='Qwen3.8-Max', request_path=str(rp),
                               process_path=str(pp), prober=prober, now=T0)
        self.assertFalse(noid['adopted'])
        self.assertEqual(noid['reason'], 'process_identity_missing')
        # 宿主留证身份与真实探针观测不一致（birth 漂移）→ 拒绝。
        drift = dp.adopt_legacy(self.store, task_id='dq1', runtime='qoder',
                                model='Qwen3.8-Max', request_path=str(rp),
                                process_path=str(pp),
                                prober=lambda pid: {'pid': pid, 'state': 'alive',
                                                    'created': 'REUSED'},
                                now=T0, child_created='vb-1')
        self.assertFalse(drift['adopted'])
        self.assertEqual(drift['reason'], 'legacy_identity_drift')
        # 无法识别的 schema → 拒绝。
        rp2 = Path(self._tmp.name) / 'unk-request.json'
        rp2.write_text(json.dumps({'foo': 'bar'}), encoding='utf-8')
        pp2 = Path(self._tmp.name) / 'unk-process.json'
        pp2.write_text(json.dumps({'pid': 5557}), encoding='utf-8')
        unk = dp.adopt_legacy(self.store, task_id='dq1', runtime='qoder',
                              model='Qwen3.8-Max', request_path=str(rp2),
                              process_path=str(pp2), prober=prober, now=T0,
                              child_created='vb-1')
        self.assertFalse(unk['adopted'])
        self.assertEqual(unk['reason'], 'request_binding_incomplete')
        self.assertEqual(self._rows('SELECT * FROM attempts'), [])

    def test_same_pid_birth_different_task_inputs_never_double_counted(self):
        rp, pp = self._qoder_receipts(self._tmp.name)
        prober = lambda pid: {'pid': pid, 'state': 'alive', 'created': 'vb-1'}
        first = dp.adopt_legacy(self.store, task_id='dq1', runtime='qoder',
                                model='Qwen3.8-Max', request_path=str(rp),
                                process_path=str(pp), prober=prober, now=T0,
                                child_created='vb-1')
        self.assertTrue(first['adopted'], first)
        # 同 PID+birth 换 task 入参（另一份原 request 同 PID 票据）→ 拒绝双计，
        # 源 task 与原 request 精确绑定。
        rp2 = Path(self._tmp.name) / 'q2-request.json'
        pp2 = Path(self._tmp.name) / 'q2-process.json'
        rp2.write_text(json.dumps({
            'task_id': 'dq2', 'runtime': 'zcode', 'model': 'GLM-5.3',
            'workspace': 'C:/legacy-q2', 'prompt_sha256': 'h' * 64,
            'stage': 'qs2', 'chat_id': 'qc2'}), encoding='utf-8')
        pp2.write_text(json.dumps({'pid': 5555, 'state': 'running'}),
                       encoding='utf-8')
        second = dp.adopt_legacy(self.store, task_id='dq2', runtime='zcode',
                                 model='GLM-5.3', request_path=str(rp2),
                                 process_path=str(pp2), prober=prober, now=T0,
                                 child_created='vb-1')
        self.assertFalse(second['adopted'])
        self.assertEqual(second['reason'], 'pid_already_adopted')
        rows = self._rows("SELECT * FROM attempts WHERE origin='adopt-legacy'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['task_id'], 'dq1')


class TestAdoptLegacyRealSpawnLifecycleZ4(_SpawnMixin, unittest.TestCase):
    """Z4-D：真实 spawn 子进程被 adopt → 结束 join → reconcile 释放（结束即空名额）。"""

    def test_real_child_adopted_then_exit_releases_capacity(self):
        alive = _CTX.Event()
        child = _CTX.Process(target=_sleep_worker, args=(alive,))
        child.start()
        self._procs.append(child)
        try:
            created = dp.process_identity(child.pid).get('created')
            self.assertIsNotNone(created)  # 亲生 spawn 子进程必须拿到真实出生身份
            rp = Path(self._tmp.name) / 'request.json'
            pp = Path(self._tmp.name) / 'process.json'
            req_bytes = json.dumps({
                'task_id': 'lg-live', 'runtime': 'zcode', 'model': 'GLM-5.3',
                'workspace': 'C:/legacy-live', 'prompt_sha256': 'i' * 64,
                'stage': 'ls', 'chat_id': 'lc'}).encode('utf-8')
            proc_bytes = json.dumps({'pid': child.pid, 'state': 'running'}).encode(
                'utf-8')
            rp.write_bytes(req_bytes)
            pp.write_bytes(proc_bytes)
            # 真实探针路径（不注入 prober）：alive + birth 完全一致才计入。
            out = dp.adopt_legacy(self.store, task_id='lg-live', runtime='zcode',
                                  model='GLM-5.3', request_path=str(rp),
                                  process_path=str(pp), now=None,
                                  child_created=created)
            self.assertTrue(out['adopted'], out)
            row = self._rows("SELECT * FROM attempts WHERE token=?",
                             (out['token'],))[0]
            self.assertEqual(row['state'], 'running')
            self.assertEqual(row['child_pid'], child.pid)
            self.assertEqual(row['child_created'], str(created))
            self.assertIsNone(row['wrapper_pid'])
            # 活着：reconcile 保持（child 活且 birth 匹配），容量仍占。
            dp.reconcile(self.store, now=None)
            self.assertEqual(
                self._rows("SELECT state FROM attempts WHERE token=?",
                           (out['token'],))[0]['state'], 'running')
            self.assertEqual(
                dp.status(self.store)['pools']['zcode:GLM-5.3']['active'], 1)
            # 真实退出 → reconcile 立即 reconciled_exit 释放（结束即空名额）。
            alive.set()
            child.join(timeout=JOIN_TIMEOUT)
            self.assertFalse(child.is_alive())
            rec = dp.reconcile(self.store, now=None)
            self.assertIn(out['token'], rec['released'])
            self.assertEqual(
                self._rows("SELECT state FROM attempts WHERE token=?",
                           (out['token'],))[0]['state'], 'reconciled_exit')
            self.assertEqual(
                dp.status(self.store)['pools']['zcode:GLM-5.3']['active'], 0)
            # 退出后再 adopt 同 PID 票据 → 明确拒绝，绝不复活/双计。
            again = dp.adopt_legacy(self.store, task_id='lg-live', runtime='zcode',
                                    model='GLM-5.3', request_path=str(rp),
                                    process_path=str(pp), now=None,
                                    child_created=created)
            self.assertFalse(again['adopted'])
            self.assertEqual(again['reason'], 'legacy_process_exited')
            # 旧原件字节不变（只读留证）。
            self.assertEqual(rp.read_bytes(), req_bytes)
            self.assertEqual(pp.read_bytes(), proc_bytes)
            self.assertEqual(
                len(self._rows("SELECT * FROM attempts WHERE origin='adopt-legacy'")),
                1)
        finally:
            alive.set()
            child.join(timeout=JOIN_TIMEOUT)
            if child.is_alive():
                child.terminate()
                child.join(timeout=JOIN_TIMEOUT)


if __name__ == '__main__':
    unittest.main()
