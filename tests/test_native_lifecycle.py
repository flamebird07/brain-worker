# -*- coding: utf-8 -*-
"""临时 SQLite 离线回归：原生 Luna 终态收口（settle-native）。

只写测试、不在本阶段执行；供主脑登记后运行验收。全部用临时 sqlite 文件与
注入时钟，绝不触网、绝不读真实库、绝不声称测过真实 Luna 平台。

覆盖：
1. 合法原生终态回执 → attempt/票据单事务结算，原 task/workspace 释放，
   后续同 workspace 可再 claim；
2. 重复同终态结算幂等、不双结算、不影响新 attempt；
3. 运行中/unknown 终态、缺/错 agent_id、token/scope 漂移、仅 terminal 字符串
   （无真实宿主回执）一律拒绝且不释放；
4. launch_unknown 拒结算且不重派；
5. 国内旧 finish 安全边界不变（reserved 无 child：start_failed 允许、finished
   拒绝 no_child_terminal_unverified），且 Luna attempt 不能再走国内 finish；
6. Z6-A 夹具顺序服从生产轮转（BW-ZCODE-RESTORE-1-20261022：现役组合（含恢复的
   ZCode 1 槽）逐一显式 claim，各独立 workspace，逐一核验 allowed/token/pool_key
   直至全满）；
7. Z6-B 回执 sha256 严格 64 位 ASCII 十六进制（大小写规范化）、string content 按
   UTF-8 原文哈希（不加 JSON 引号）、content 与 sha256 同给必须一致；
8. Z6-C 重复结算只有同终态+同原回执哈希才算合法幂等（不覆盖旧证据），错误
   agent/缺回执/不同终态/scope 漂移全拒；国内 attempt 不能经 native 入口结算；
9. Z6-D terminal 与 success 矛盾（finished+false / native_failed+true）拒绝。"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dispatch_mode_support import user_disabled_pools  # noqa: E402

import dispatch_pool as dp  # noqa: E402
from dispatch_mode_support import insert_sonus_leftover  # noqa: E402
dp.LUNA_DISABLED = False
dp.CAPACITY[dp.LUNA_KEY] = None

T0 = datetime(2026, 10, 8, 15, 0, 0, tzinfo=timezone.utc)  # 北京时间 23:00（主力时段内）
RECEIPT = {'source_tool': 'luna.getAgentStatus',
           'receipt_ref': 'native-run-0001',
           'sha256': 'a' * 64}


def scope_of(task_id, workspace):
    return json.dumps({'task_id': task_id, 'stage': 's1', 'chat_id': 'c1',
                       'workspace': workspace, 'prompt_sha256': 'f' * 64},
                      ensure_ascii=False, sort_keys=True)


class NativeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = os.path.join(self._tmp.name, 'pool.sqlite3')

    def tearDown(self):
        self._tmp.cleanup()

    # ---------- 夹具：现役全满 → ask → 到期 claim Luna → 登记 agent_id ----------
    # Z6-A（BW-ZCODE-RESTORE-1-20261022 起现役组合含恢复的 ZCode）：请求顺序服从
    # 生产轮转，各独立 workspace，逐一核验 allowed/token/pool_key 直至全满。
    def _fill_domestic(self):
        # BW-CODEBUDDY-MAIN-S3 + BW-WORKER-WIDGET-20261018-S3 + BW-OPENCODE-20261019-S3
        # + BW-ZCODE-RESTORE-1-20261022 十五满：codebuddy deepseek×1 + codebuddy GLM×1
        # （各自独立 1 槽，GLM 需显式当轮额度条件授权）+ zcode×1（恢复可派、容量严格 1）
        # + 两内置 Max 各 1 + CN 自定义 Max×2 + 两地区 Flash 各 2 + claude:deepseek-flash×2
        # + 两个 OpenCode 免费模型各 1 独立槽（T0=北京周四 23:00，主力时段内且 claude
        # offhours，OpenCode 无时段门，真实 API 直接 claim）。
        # 全部经显式 executor 逐一确定性 claim（CN 池只能 executor='qodercn'，claude 只能
        # executor='claude'，codebuddy 两模型只能 executor='codebuddy'，OpenCode 经
        # executor='opencode'，ZCode 经 executor='zcode'，AUTO 轮换不保证落某特定主力），
        # 每次核验 allowed/token/pool_key。
        plan = [('codebuddy', 'deepseek-v4.1-flash', 'codebuddy'),
                ('codebuddy', 'glm-5.3-flash', 'codebuddy'),
                ('zcode', 'GLM-5.3', 'zcode'),
                ('qoder', 'Qwen3.8-Max', 'qoder'),
                ('qodercn', 'Qwen3.8-Max', 'qodercn'),
                ('qodercn', 'Qwen-3.8-Max', 'qodercn'),
                ('qodercn', 'Qwen-3.8-Max', 'qodercn'),
                ('qoder', 'Qwen3.8-Flash', 'qoder'),
                ('qoder', 'Qwen3.8-Flash', 'qoder'),
                ('qodercn', 'Qwen3.8-Flash', 'qodercn'),
                ('qodercn', 'Qwen3.8-Flash', 'qodercn'),
                ('claude', 'deepseek-flash', 'claude'),
                ('claude', 'deepseek-flash', 'claude'),
                ('opencode', dp.OPENCODE_DEEPSEEK_MODEL, 'opencode'),
                ('opencode', dp.OPENCODE_GLM_MODEL, 'opencode')]
        tokens = []
        # BW-QODERCN-TOKEN-MAX-DISABLE-20261028：物理 16/16 满池需要目标池两槽 legacy
        # 占位——生产禁用期间该池不可再 reserve，这里临时用空禁用集复现禁用前的物理满池
        # 基线（Luna 生命周期历史场景），fill 结束即恢复真实禁用集；后续断言不变。
        with user_disabled_pools(frozenset()):
            for i, (runtime, model, ex) in enumerate(plan):
                out = dp.reserve(self.store, task_id=f'dom-{i}', runtime=runtime,
                                 model=model, workspace=f'ws-dom-{i}',
                                 prompt_sha256='d' * 64, executor=ex, now=T0,
                                 codebuddy_glm_allowed=(model == 'glm-5.3-flash'))
                self.assertTrue(out.get('allowed') and out.get('token'), out)
                self.assertEqual(out['pool_key'], f'{runtime}:{model}', out)
                tokens.append(out['token'])
        # BW-STRONG-SONUS-20261024：弱档新派上限仍是 15；按真实形态补一条 strong
        # 遗留 Sonus 在途，使物理在途达到 16/16（切档不追杀）。
        insert_sonus_leftover(self.store, reserved_at_utc=dp._iso(T0))
        st = dp.status(self.store, now=T0)
        self.assertEqual(st['domestic']['active'], dp.DOMESTIC_TOTAL_CAPACITY,
                         st['domestic'])
        self.assertEqual(st['domestic']['active'], 16, st['domestic'])
        # 1 个 zcode（BW-ZCODE-RESTORE-1-20261022 容量严格 1）记 committed_zcode；
        # Qoder 主力组（两内置 Max + 自定义 Max 合计）记 committed_qoder=4。
        self.assertEqual(st['rotation']['committed_zcode'], 1, st['rotation'])
        self.assertEqual(st['rotation']['committed_qoder'], 4, st['rotation'])
        return tokens

    def _luna_claimed(self, task_id='task-luna', workspace='ws-luna',
                      agent_id='agent-real-1'):
        self._fill_domestic()
        rec = dp.ask_record(self.store, task_id=task_id,
                            scope=scope_of(task_id, workspace),
                            ask_message_id='msg-1', now=T0)
        self.assertTrue(rec['recorded'], rec)
        out = dp.claim_due(self.store, task_id=task_id, now=T0 + timedelta(seconds=301))
        self.assertTrue(out.get('claimed') and out.get('mode') == 'luna', out)
        if agent_id is not None:
            rid = dp.record_agent_id(self.store, task_id=task_id,
                                     agent_id=agent_id, now=T0 + timedelta(seconds=302))
            self.assertTrue(rid['recorded'], rid)
        return out['token']

    def _attempt(self, token):
        with dp.closing(dp.connect(self.store)) as conn:
            return dict(conn.execute('SELECT * FROM attempts WHERE token=?',
                                     (token,)).fetchone())

    def _ticket(self, task_id):
        with dp.closing(dp.connect(self.store)) as conn:
            return dict(conn.execute('SELECT * FROM luna_tickets WHERE task_id=?',
                                     (task_id,)).fetchone())

    # ---------- 1. 合法原生终态 → 单事务释放，同 workspace 可再 claim ----------
    def test_settle_success_releases_task_and_workspace(self):
        tok = self._luna_claimed()
        out = dp.settle_native(self.store, task_id='task-luna', token=tok,
                               agent_id='agent-real-1', terminal='finished',
                               scope=scope_of('task-luna', 'ws-luna'),
                               receipt=RECEIPT, now=T0 + timedelta(seconds=900))
        self.assertTrue(out['settled'] and out['released'], out)
        att = self._attempt(tok)
        self.assertEqual(att['state'], 'finished')
        self.assertEqual(att['success'], 1)
        ev = json.loads(att['adopt_evidence'])
        self.assertEqual(ev['native_terminal_receipt']['receipt_sha256'],
                         RECEIPT['sha256'])
        self.assertEqual(ev['native_terminal_receipt']['receipt_ref'],
                         RECEIPT['receipt_ref'])
        self.assertEqual(self._ticket('task-luna')['state'], 'settled')
        # workspace 不再被占：同 workspace、不同 task 的新 attempt 不再冲突。
        with dp.closing(dp.connect(self.store)) as conn:
            conflict = dp._workspace_conflict(conn, 'ws-luna')
        self.assertIsNone(conflict, conflict)
        # task 已释放：腾一个现役 qoder Max 名额后，同 task 可重新 claim（不被旧 attempt 挡）。
        # 本例验的是原生结算后的生命周期释放，不是 AUTO 优先级；BW-ZCODE-RETIRE-20261020：
        # zcode 组合在授权门即 unknown_pool，夹具改用释放 qoder:Qwen3.8-Max 行并以显式
        # executor='qoder' 重新 claim 同一组合（该池容量 1，释放后必空；AUTO 会被改派）。
        dp.finish(self.store, self._qmax_token(), terminal='cancelled',
                  now=T0 + timedelta(seconds=901))
        out2 = dp.reserve(self.store, task_id='task-luna', runtime='qoder',
                          model='Qwen3.8-Max', workspace='ws-luna',
                          prompt_sha256='f' * 64, executor='qoder',
                          now=T0 + timedelta(seconds=902))
        self.assertTrue(out2.get('token'), out2)

    def _qmax_token(self):
        with dp.closing(dp.connect(self.store)) as conn:
            row = conn.execute(
                "SELECT token FROM attempts WHERE origin='reserve' "
                "AND pool_key='qoder:Qwen3.8-Max' AND state IN ('reserved','running') "
                "ORDER BY rowid LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        return row['token']

    def test_settle_failure_records_native_failed(self):
        tok = self._luna_claimed()
        out = dp.settle_native(self.store, task_id='task-luna', token=tok,
                               agent_id='agent-real-1', terminal='native_failed',
                               receipt=RECEIPT, now=T0 + timedelta(seconds=900))
        self.assertTrue(out['settled'] and out['released'], out)
        att = self._attempt(tok)
        self.assertEqual(att['state'], 'native_failed')
        self.assertEqual(att['success'], 0)  # 成功/失败分开，绝不伪标 cancelled

    # ---------- 2. 幂等：重复同终态不双结算、不影响新 attempt ----------
    def test_repeat_settle_idempotent(self):
        tok = self._luna_claimed()
        dp.settle_native(self.store, task_id='task-luna', token=tok,
                         agent_id='agent-real-1', terminal='finished',
                         receipt=RECEIPT, now=T0 + timedelta(seconds=900))
        first_end = self._attempt(tok)['ended_at_utc']
        again = dp.settle_native(self.store, task_id='task-luna', token=tok,
                                 agent_id='agent-real-1', terminal='finished',
                                 receipt=RECEIPT, now=T0 + timedelta(seconds=950))
        self.assertTrue(again.get('idempotent'), again)
        self.assertTrue(again['settled'])
        self.assertFalse(again['released'])  # 已是终态，不再次改变任何状态
        att = self._attempt(tok)
        self.assertEqual(att['state'], 'finished')
        self.assertEqual(att['ended_at_utc'], first_end)  # 不双结算、不覆盖时间
        # 已 settled 票据绝不再次升级/重派。
        re_due = dp.claim_due(self.store, task_id='task-luna',
                              now=T0 + timedelta(seconds=1000))
        self.assertFalse(re_due.get('claimed'), re_due)
        self.assertEqual(re_due.get('state'), 'settled')

    # ---------- Z6-C：重复结算的幂等只给合法重放 ----------
    def test_repeat_settle_wrong_identity_or_receipt_refused(self):
        tok = self._luna_claimed()
        dp.settle_native(self.store, task_id='task-luna', token=tok,
                         agent_id='agent-real-1', terminal='finished',
                         receipt=RECEIPT, now=T0 + timedelta(seconds=900))
        first = self._attempt(tok)
        later = T0 + timedelta(seconds=950)
        for kwargs in (dict(agent_id='agent-other', receipt=RECEIPT),
                       dict(agent_id=None, receipt=RECEIPT),
                       dict(agent_id='agent-real-1', receipt=None),
                       dict(agent_id='agent-real-1',
                            receipt={'source_tool': 'luna.getAgentStatus',
                                     'receipt_ref': 'native-run-OTHER',
                                     'sha256': 'b' * 64}),
                       dict(agent_id='agent-real-1', receipt=RECEIPT,
                            terminal='native_failed'),
                       dict(agent_id='agent-real-1', receipt=RECEIPT,
                            scope=scope_of('task-luna', 'ws-OTHER'))):
            call = dict(task_id='task-luna', token=tok, agent_id='agent-real-1',
                        receipt=RECEIPT, terminal='finished', now=later)
            call.update(kwargs)
            out = dp.settle_native(self.store, **call)
            self.assertFalse(out['settled'] and out['released'], (kwargs, out))
            att = self._attempt(tok)
            self.assertEqual(att['state'], 'finished', kwargs)
            self.assertEqual(att['ended_at_utc'], first['ended_at_utc'], kwargs)
            self.assertEqual(att['adopt_evidence'], first['adopt_evidence'], kwargs)

    # ---------- Z6-B：回执哈希口径 ----------
    def test_receipt_sha_must_be_strict_64_ascii_hex(self):
        self.tok = self._luna_claimed()
        base = dict(task_id='task-luna', token=self.tok, agent_id='agent-real-1',
                    terminal='finished', now=T0 + timedelta(seconds=900))
        for bad in ('a' * 63, 'a' * 65, 'z' * 64, 'g' * 64, 'a' * 32, 123, True):
            out = dp.settle_native(self.store,
                                   receipt={'source_tool': 'luna.getAgentStatus',
                                            'receipt_ref': 'native-run-0001',
                                            'sha256': bad}, **base)
            self._assert_refused(out)

    def test_receipt_uppercase_hex_accepted_and_normalized(self):
        tok = self._luna_claimed()
        out = dp.settle_native(self.store, task_id='task-luna', token=tok,
                               agent_id='agent-real-1', terminal='finished',
                               receipt={'source_tool': 'luna.getAgentStatus',
                                        'receipt_ref': 'native-run-0001',
                                        'sha256': 'A' * 64},
                               now=T0 + timedelta(seconds=900))
        self.assertTrue(out['settled'] and out['released'], out)
        self.assertEqual(out['receipt_sha256'], 'a' * 64)

    def test_receipt_content_string_hashed_as_utf8_raw(self):
        self.tok = self._luna_claimed()
        base = dict(task_id='task-luna', token=self.tok, agent_id='agent-real-1',
                    terminal='finished', now=T0 + timedelta(seconds=900))
        raw = '原生回执原文-raw'
        good = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        out = dp.settle_native(self.store,
                               receipt={'source_tool': 'luna.getAgentStatus',
                                        'receipt_ref': 'native-run-0001',
                                        'content': raw}, **base)
        self.assertTrue(out['settled'] and out['released'], out)
        self.assertEqual(out['receipt_sha256'], good)
        att = self._attempt(self.tok)
        self.assertEqual(
            json.loads(att['adopt_evidence'])['native_terminal_receipt']
            ['receipt_sha256'], good)
        # JSON 引号口径（json.dumps(str) 的字节）不是原文哈希，必须拒。
        json_quoted = hashlib.sha256(
            json.dumps(raw, ensure_ascii=False, sort_keys=True)
            .encode('utf-8')).hexdigest()
        out = dp.settle_native(self.store,
                               receipt={'source_tool': 'luna.getAgentStatus',
                                        'receipt_ref': 'native-run-0002',
                                        'content': raw,
                                        'sha256': json_quoted}, **base)
        self.assertFalse(out['settled'], out)

    def test_receipt_content_and_sha_must_agree(self):
        self.tok = self._luna_claimed()
        base = dict(task_id='task-luna', token=self.tok, agent_id='agent-real-1',
                    terminal='finished', now=T0 + timedelta(seconds=900))
        out = dp.settle_native(self.store,
                               receipt={'source_tool': 'luna.getAgentStatus',
                                        'receipt_ref': 'native-run-0001',
                                        'content': 'text', 'sha256': 'c' * 64},
                               **base)
        self._assert_refused(out)
        agree = hashlib.sha256('text'.encode('utf-8')).hexdigest()
        out = dp.settle_native(self.store,
                               receipt={'source_tool': 'luna.getAgentStatus',
                                        'receipt_ref': 'native-run-0001',
                                        'content': 'text', 'sha256': agree}, **base)
        self.assertTrue(out['settled'] and out['released'], out)

    # ---------- Z6-D：terminal 与 success 不得矛盾 ----------
    def test_terminal_success_contradiction_rejected(self):
        self.tok = self._luna_claimed()
        base = dict(task_id='task-luna', token=self.tok, agent_id='agent-real-1',
                    receipt=RECEIPT, now=T0 + timedelta(seconds=900))
        out = dp.settle_native(self.store, terminal='finished', success=False, **base)
        self._assert_refused(out)
        out = dp.settle_native(self.store, terminal='native_failed', success=True,
                               **base)
        self._assert_refused(out)
        # 与 terminal 一致的显式布尔仍接受。
        out = dp.settle_native(self.store, terminal='finished', success=True, **base)
        self.assertTrue(out['settled'] and out['released'], out)
        self.assertEqual(self._attempt(self.tok)['success'], 1)

    # ---------- Z6-C：国内 attempt 禁止经 native 入口结算 ----------
    def test_domestic_attempt_not_settled_via_native_entry(self):
        luna_tok = self._luna_claimed()
        dp.settle_native(self.store, task_id='task-luna', token=luna_tok,
                         agent_id='agent-real-1', terminal='finished',
                         receipt=RECEIPT, now=T0 + timedelta(seconds=900))
        # 腾一个现役 qoder Max 名额后建一个真实国内 attempt（显式 executor='qoder' 保留
        # 原组合；BW-ZCODE-RETIRE-20261020：zcode 已退役不可能再 reserve，该池容量 1，
        # 释放后必空，不因 CodeBuddy 空位被 AUTO 改派）。
        dp.finish(self.store, self._qmax_token(), terminal='cancelled',
                  now=T0 + timedelta(seconds=901))
        out = dp.reserve(self.store, task_id='dom-n', runtime='qoder',
                         model='Qwen3.8-Max', workspace='ws-n', prompt_sha256='a' * 64,
                         executor='qoder', now=T0 + timedelta(seconds=902))
        self.assertTrue(out.get('token'), out)
        dom_tok = out['token']
        later = T0 + timedelta(seconds=903)
        # 该 task 无 Luna 票据 → 拒。
        s = dp.settle_native(self.store, task_id='dom-n', token=dom_tok,
                             agent_id='agent-x', terminal='finished',
                             receipt=RECEIPT, now=later)
        self.assertFalse(s['settled'], s)
        # 有 Luna 票据但 token 是国内 attempt（非票据 claimed_token）→ 仍拒。
        s = dp.settle_native(self.store, task_id='task-luna', token=dom_tok,
                             agent_id='agent-real-1', terminal='finished',
                             receipt=RECEIPT, now=later)
        self.assertFalse(s['settled'] and s['released'], s)
        att = self._attempt(dom_tok)
        self.assertIn(att['state'], dp.ACTIVE_STATES)  # 国内行不被 native 入口动

    # ---------- 3. 拒绝路径（全部不释放） ----------
    def _assert_refused(self, out):
        self.assertFalse(out['settled'] and out['released'], out)
        att = self._attempt(self.tok)
        self.assertIn(att['state'], dp.ACTIVE_STATES)
        self.assertEqual(self._ticket('task-luna')['state'], 'claimed')

    def test_non_terminal_states_rejected(self):
        self.tok = self._luna_claimed()
        for bad in ('running', 'unknown', 'cancelled', 'start_failed'):
            with self.assertRaises(ValueError, msg=bad):
                dp.settle_native(self.store, task_id='task-luna', token=self.tok,
                                 agent_id='agent-real-1', terminal=bad,
                                 receipt=RECEIPT, now=T0 + timedelta(seconds=900))

    def test_wrong_or_missing_agent_id_rejected(self):
        self.tok = self._luna_claimed()
        out = dp.settle_native(self.store, task_id='task-luna', token=self.tok,
                               agent_id='agent-other', terminal='finished',
                               receipt=RECEIPT, now=T0 + timedelta(seconds=900))
        self._assert_refused(out)
        out = dp.settle_native(self.store, task_id='task-luna', token=self.tok,
                               agent_id=None, terminal='finished',
                               receipt=RECEIPT, now=T0 + timedelta(seconds=900))
        self._assert_refused(out)

    def test_no_real_receipt_rejected(self):
        self.tok = self._luna_claimed()
        base = dict(task_id='task-luna', token=self.tok, agent_id='agent-real-1',
                    terminal='finished', now=T0 + timedelta(seconds=900))
        for receipt in (None, 'finished', json.dumps({'terminal': 'finished'}),
                        {'source_tool': 'luna.getAgentStatus'},  # 缺 ref/哈希
                        {'receipt_ref': 'r1', 'sha256': 'a' * 64}):  # 缺 source_tool
            out = dp.settle_native(self.store, receipt=receipt, **base)
            self._assert_refused(out)

    def test_token_and_scope_drift_rejected(self):
        self.tok = self._luna_claimed()
        out = dp.settle_native(self.store, task_id='task-luna', token='wrong-token',
                               agent_id='agent-real-1', terminal='finished',
                               receipt=RECEIPT, now=T0 + timedelta(seconds=900))
        self.assertFalse(out['settled'])
        self._assert_refused(dp.settle_native(
            self.store, task_id='task-luna', token=self.tok,
            agent_id='agent-real-1', terminal='finished',
            scope=scope_of('task-luna', 'ws-OTHER'),
            receipt=RECEIPT, now=T0 + timedelta(seconds=900)))

    # ---------- 4. launch_unknown：拒结算、不重派 ----------
    def test_launch_unknown_protected(self):
        self.tok = self._luna_claimed(agent_id=None)
        dp.mark_launch_unknown(self.store, task_id='task-luna',
                               now=T0 + timedelta(seconds=305))
        out = dp.settle_native(self.store, task_id='task-luna', token=self.tok,
                               agent_id='agent-real-1', terminal='finished',
                               receipt=RECEIPT, now=T0 + timedelta(seconds=900))
        self.assertFalse(out['settled'] and out['released'], out)
        att = self._attempt(self.tok)
        self.assertIn(att['state'], dp.ACTIVE_STATES)  # 仍保护，不释放
        re_due = dp.claim_due(self.store, task_id='task-luna',
                              now=T0 + timedelta(seconds=1000))
        self.assertFalse(re_due.get('claimed'), re_due)  # 不自动重派
        self.assertTrue(re_due.get('already_claimed')
                        or any('launch_unknown' in r for r in re_due['reasons']),
                        re_due)

    # ---------- 5. 国内旧 finish 边界不变；Luna 不能走国内 finish ----------
    def test_domestic_finish_boundary_unchanged(self):
        # 准备用显式 executor='qoder' 固定现役组合，绕过 AUTO 跨层改派
        # （BW-MAIN-PRIORITY-S1 + BW-ZCODE-RETIRE-20261020：原借退役 zcode 组合已失效）；
        # 国内旧 finish 边界断言原样保留。
        out = dp.reserve(self.store, task_id='dom-x', runtime='qoder',
                         model='Qwen3.8-Max', workspace='ws-x', prompt_sha256='a' * 64,
                         executor='qoder', now=T0)
        self.assertTrue(out.get('allowed') and out.get('token'), out)
        tok = out['token']
        fin = dp.finish(self.store, tok, terminal='finished', now=T0)
        self.assertFalse(fin['released'], fin)  # 无 child 证据仍拒绝
        self.assertEqual(fin.get('reason'), 'no_child_terminal_unverified')
        rel = dp.finish(self.store, tok, terminal='cancelled', now=T0)
        self.assertTrue(rel['released'], rel)  # 合法取消照旧可释放

    def test_luna_attempt_cannot_exit_via_domestic_finish(self):
        tok = self._luna_claimed()
        for terminal in ('finished', 'start_failed', 'cancelled'):
            fin = dp.finish(self.store, tok, terminal=terminal,
                            now=T0 + timedelta(seconds=900))
            self.assertFalse(fin['released'], fin)
            self.assertIn(att_state := self._attempt(tok)['state'],
                          dp.ACTIVE_STATES, terminal)


if __name__ == '__main__':
    unittest.main()
